#!/usr/bin/env python3
"""
scrape_channels.py
==================

Descarga el contenido de una URL publica de texto plano o M3U, procesa
payloads grandes por bloques, cachea respuestas localmente y extrae
enlaces .m3u8 para agregarlos a sources/channels.json sin duplicados.

Incluye soporte nativo para listas M3U remotas de gran volumen, como el
indice global de iptv-org.

Este script esta pensado para fuentes autorizadas y publicas.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import aiohttp

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT_DIR / "sources" / "channels.json"
CONFIG_FILE = ROOT_DIR / "config.json"
CACHE_DIR = ROOT_DIR / ".cache" / "sources"
DEFAULT_SOURCE_URL = "https://iptv-org.github.io/iptv/index.m3u"
DEFAULT_IPTVORG_CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "timeout_seconds": 20,
    "max_concurrency": 3,
    "user_agent": "MiTVPublicaBot/1.0 (+https://github.com)",
    "accept_language": "es-MX,es;q=0.9,en;q=0.6",
    "retry_attempts": 3,
    "retry_backoff_base_seconds": 1.0,
    "jitter_min_seconds": 0.5,
    "jitter_max_seconds": 1.5,
    "cache_ttl_seconds": 21600,
    "chunk_size_bytes": 65536,
}

M3U8_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>()\[\]]+?\.m3u8(?:\?[^\s\"'<>()\[\]]*)?",
    re.IGNORECASE,
)
EXTINF_PATTERN = re.compile(r"^#EXTINF:-?\d+\s*(?P<attrs>.*?),(?P<name>.*)$")
ATTR_PATTERN = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
TVG_ID_COUNTRY_PATTERN = re.compile(r"\.([A-Za-z]{2})(?:$|[@._-])")
RETRIABLE_STATUS_CODES = {429, 503}


def load_config(config_path: Path = CONFIG_FILE) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if config_path.exists():
        try:
            user_config = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(user_config, dict):
                config.update(user_config)
        except json.JSONDecodeError as exc:
            print(f"[WARN] config.json invalido, usando valores por defecto: {exc}")
    return config


def load_channels_raw(sources_path: Path = SOURCES_FILE) -> list[dict[str, Any]]:
    if not sources_path.exists():
        return []

    raw = json.loads(sources_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("sources/channels.json debe contener una lista JSON")
    return raw


def save_channels_raw(channels: list[dict[str, Any]], sources_path: Path = SOURCES_FILE) -> None:
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text(json.dumps(channels, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def normalize_url(url: str) -> str:
    cleaned = html.unescape((url or "").strip())
    return cleaned.rstrip(".,;)]}>\"'")


def iter_text_chunks(text: str, chunk_size: int) -> Iterator[str]:
    if chunk_size <= 0:
        yield text
        return
    for start in range(0, len(text), chunk_size):
        yield text[start : start + chunk_size]


def extract_m3u8_links(text: str, chunk_size: int = 65536) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    carry = ""

    for chunk in iter_text_chunks(text or "", chunk_size):
        window = carry + chunk
        for match in M3U8_URL_PATTERN.findall(window):
            normalized = normalize_url(match)
            if normalized and normalized not in seen:
                seen.add(normalized)
                links.append(normalized)
        carry = window[-2048:]

    return links


def build_channel_name(url: str, used_names: set[str]) -> str:
    parsed = urlparse(url)
    stem = Path(parsed.path).stem or parsed.netloc or "Canal Importado"
    base_name = re.sub(r"[-_]+", " ", stem).strip() or "Canal Importado"
    candidate = base_name
    suffix = 2

    while candidate.casefold() in used_names:
        candidate = f"{base_name} ({suffix})"
        suffix += 1

    used_names.add(candidate.casefold())
    return candidate


def ensure_unique_name(name: str, used_names: set[str]) -> str:
    candidate = (name or "").strip() or "Canal Importado"
    if candidate.casefold() not in used_names:
        used_names.add(candidate.casefold())
        return candidate

    suffix = 2
    while f"{candidate} ({suffix})".casefold() in used_names:
        suffix += 1

    unique_name = f"{candidate} ({suffix})"
    used_names.add(unique_name.casefold())
    return unique_name


def parse_extinf_line(line: str) -> dict[str, str] | None:
    match = EXTINF_PATTERN.match(line.strip())
    if not match:
        return None

    attrs = {key: value for key, value in ATTR_PATTERN.findall(match.group("attrs"))}
    attrs["name"] = match.group("name").strip()
    return attrs


def infer_country(attrs: dict[str, str], group: str) -> str:
    tvg_id = (attrs.get("tvg-id") or "").strip()
    match = TVG_ID_COUNTRY_PATTERN.search(tvg_id)
    if match:
        return match.group(1).upper()

    group_upper = group.strip().upper()
    if len(group_upper) == 2 and group_upper.isalpha():
        return group_upper
    return ""


def build_channel_record_from_extinf(attrs: dict[str, str], url: str) -> dict[str, Any]:
    group = (attrs.get("group-title") or "General").strip() or "General"
    return {
        "name": attrs.get("name") or build_channel_name(url, set()),
        "group": group,
        "country": infer_country(attrs, group),
        "url": normalize_url(url),
        "logo": (attrs.get("tvg-logo") or "").strip(),
        "tvg_id": (attrs.get("tvg-id") or "").strip(),
    }


def parse_m3u_file(file_path: Path) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    pending_attrs: dict[str, str] | None = None
    used_names: set[str] = set()

    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF"):
                pending_attrs = parse_extinf_line(line)
                continue
            if line.startswith("#"):
                continue
            if pending_attrs and M3U8_URL_PATTERN.fullmatch(normalize_url(line)):
                channel = build_channel_record_from_extinf(pending_attrs, line)
                channel["name"] = ensure_unique_name(str(channel.get("name", "")).strip(), used_names)
                channels.append(channel)
            pending_attrs = None

    return channels


def file_looks_like_json(file_path: Path) -> bool:
    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        while True:
            char = handle.read(1)
            if not char:
                return False
            if char.isspace():
                continue
            return char in "[{"


def parse_iptv_org_streams(
    streams_path: Path,
    channels_path: Path,
    country_filter: str = "",
) -> list[dict[str, Any]]:
    streams = json.loads(streams_path.read_text(encoding="utf-8"))
    channels = json.loads(channels_path.read_text(encoding="utf-8"))

    country_filter = country_filter.strip().upper()
    channel_map = {
        item["id"]: item
        for item in channels
        if isinstance(item, dict)
        and item.get("id")
        and (not country_filter or str(item.get("country", "")).upper() == country_filter)
    }

    parsed_channels: list[dict[str, Any]] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        channel_id = str(stream.get("channel") or "").strip()
        url = normalize_url(str(stream.get("url") or "").strip())
        if not channel_id or not url:
            continue

        channel_meta = channel_map.get(channel_id)
        if not channel_meta:
            continue

        categories = channel_meta.get("categories") or []
        if isinstance(categories, list) and categories:
            group = str(categories[0]).replace("-", " ").replace("_", " ").title()
        else:
            group = "General"

        title = str(stream.get("title") or "").strip()
        quality = str(stream.get("quality") or "").strip()
        name = channel_meta.get("name") or title or build_channel_name(url, set())
        if quality and quality not in name:
            name = f"{name} ({quality})"

        parsed_channels.append(
            {
                "name": str(name).strip(),
                "group": group,
                "country": str(channel_meta.get("country") or "").strip(),
                "url": url,
                "logo": "",
                "tvg_id": channel_id,
            }
        )

    return parsed_channels


def parse_generic_channel_json(
    source_path: Path,
    *,
    default_country: str,
    default_group: str,
) -> list[dict[str, Any]]:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        items = payload.get("channels") or payload.get("data") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    parsed_channels: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = normalize_url(str(item.get("url") or "").strip())
        if not url:
            continue

        title = (
            str(item.get("name") or "").strip()
            or str(item.get("title") or "").strip()
            or build_channel_name(url, set())
        )
        logo = str(item.get("logo") or item.get("image") or "").strip()
        slug = str(item.get("slug") or "").strip()
        tvg_id = slug or str(item.get("id") or "").strip()

        parsed_channels.append(
            {
                "name": title,
                "group": default_group,
                "country": default_country,
                "url": url,
                "logo": logo,
                "tvg_id": tvg_id,
            }
        )

    return parsed_channels


def merge_channels(
    existing_channels: list[dict[str, Any]],
    discovered_channels: list[dict[str, Any]] | list[str],
    *,
    default_group: str = "Importados",
    default_country: str = "",
) -> tuple[list[dict[str, Any]], int]:
    merged = list(existing_channels)
    existing_urls = {
        normalize_url(str(item.get("url", "")))
        for item in existing_channels
        if isinstance(item, dict)
    }
    used_names = {
        str(item.get("name", "")).strip().casefold()
        for item in existing_channels
        if isinstance(item, dict) and item.get("name")
    }

    added = 0
    for item in discovered_channels:
        if isinstance(item, str):
            normalized_url = normalize_url(item)
            candidate = {
                "name": build_channel_name(normalized_url, used_names),
                "group": default_group,
                "country": default_country,
                "url": normalized_url,
                "logo": "",
                "tvg_id": "",
            }
        else:
            normalized_url = normalize_url(str(item.get("url", "")))
            candidate = {
                "name": (str(item.get("name", "")).strip() or build_channel_name(normalized_url, used_names)),
                "group": (str(item.get("group", "")).strip() or default_group),
                "country": (str(item.get("country", "")).strip() or default_country),
                "url": normalized_url,
                "logo": str(item.get("logo", "")).strip(),
                "tvg_id": str(item.get("tvg_id", "")).strip(),
            }

        if not normalized_url or normalized_url in existing_urls:
            continue

        if isinstance(item, str):
            candidate["name"] = candidate["name"] or build_channel_name(normalized_url, used_names)
        else:
            candidate["name"] = candidate["name"] or build_channel_name(normalized_url, used_names)
            candidate["name"] = ensure_unique_name(candidate["name"], used_names)
        merged.append(candidate)
        existing_urls.add(normalized_url)
        added += 1

    return merged, added


def build_headers(config: dict[str, Any]) -> dict[str, str]:
    return {
        "User-Agent": str(config["user_agent"]),
        "Accept": "text/plain, application/x-mpegURL, application/vnd.apple.mpegurl, */*;q=0.8",
        "Accept-Language": str(config.get("accept_language", "es-MX,es;q=0.9,en;q=0.6")),
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=30, max=100",
    }


async def sleep_with_jitter(config: dict[str, Any]) -> None:
    jitter_min = float(config.get("jitter_min_seconds", 0.5))
    jitter_max = float(config.get("jitter_max_seconds", 1.5))
    await asyncio.sleep(random.uniform(jitter_min, jitter_max))


def cache_path_for_url(source_url: str, cache_dir: Path = CACHE_DIR) -> Path:
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.cache"


def load_cached_path(source_url: str, ttl_seconds: int, cache_dir: Path = CACHE_DIR) -> Path | None:
    cache_path = cache_path_for_url(source_url, cache_dir)
    if not cache_path.exists():
        return None

    age_seconds = time.time() - cache_path.stat().st_mtime
    if age_seconds > ttl_seconds:
        return None
    return cache_path


def load_cached_text(source_url: str, ttl_seconds: int, cache_dir: Path = CACHE_DIR) -> str | None:
    cache_path = load_cached_path(source_url, ttl_seconds, cache_dir)
    if cache_path is None:
        return None
    return cache_path.read_text(encoding="utf-8", errors="ignore")


def save_cached_text(source_url: str, text: str, cache_dir: Path = CACHE_DIR) -> None:
    cache_path = cache_path_for_url(source_url, cache_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(text, encoding="utf-8")


async def fetch_source_to_cache(source_url: str, config: dict[str, Any]) -> Path:
    cached = load_cached_path(
        source_url,
        int(config.get("cache_ttl_seconds", 21600)),
    )
    if cached is not None:
        return cached

    timeout = aiohttp.ClientTimeout(total=float(config["timeout_seconds"]))
    headers = build_headers(config)
    attempts = int(config.get("retry_attempts", 3))
    backoff_base = float(config.get("retry_backoff_base_seconds", 1.0))

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for attempt in range(1, attempts + 1):
            await sleep_with_jitter(config)
            try:
                async with session.get(source_url, allow_redirects=True, ssl=False) as response:
                    if response.status == 401:
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message="No autorizado por el origen (401)",
                            headers=response.headers,
                        )
                    if response.status in RETRIABLE_STATUS_CODES and attempt < attempts:
                        await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                        continue
                    response.raise_for_status()
                    cache_path = cache_path_for_url(source_url)
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    with cache_path.open("w", encoding="utf-8", errors="ignore") as handle:
                        async for chunk in response.content.iter_chunked(int(config.get("chunk_size_bytes", 65536))):
                            handle.write(chunk.decode("utf-8", errors="ignore"))
                    return cache_path
            except asyncio.TimeoutError:
                if attempt < attempts:
                    await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                raise
            except aiohttp.ClientResponseError:
                raise
            except aiohttp.ClientError:
                if attempt < attempts:
                    await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                raise

    raise RuntimeError("No se pudo descargar la fuente tras varios reintentos")


def file_looks_like_m3u(file_path: Path) -> bool:
    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for _ in range(20):
            line = handle.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            return stripped.startswith("#EXTM3U") or stripped.startswith("#EXTINF")
    return file_path.suffix.lower() == ".m3u"


def extract_text_links_from_file(file_path: Path, chunk_size: int) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    carry = ""

    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            window = carry + chunk
            for match in M3U8_URL_PATTERN.findall(window):
                normalized = normalize_url(match)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    links.append(normalized)
            carry = window[-2048:]

    return links


async def run(
    source_url: str | None,
    sources_path: Path,
    config_path: Path,
    *,
    default_group: str,
    default_country: str,
    metadata_url: str | None = None,
) -> int:
    config = load_config(config_path)
    resolved_source_url = source_url or DEFAULT_SOURCE_URL
    cached_file = await fetch_source_to_cache(resolved_source_url, config)

    if file_looks_like_m3u(cached_file):
        discovered_channels: list[dict[str, Any]] | list[str] = parse_m3u_file(cached_file)
        detected_count = len(discovered_channels)
    elif file_looks_like_json(cached_file):
        if metadata_url:
            metadata_file = await fetch_source_to_cache(metadata_url, config)
            discovered_channels = parse_iptv_org_streams(
                cached_file,
                metadata_file,
                country_filter=default_country,
            )
        elif "iptv-org.github.io/api/streams.json" in resolved_source_url:
            metadata_file = await fetch_source_to_cache(DEFAULT_IPTVORG_CHANNELS_URL, config)
            discovered_channels = parse_iptv_org_streams(
                cached_file,
                metadata_file,
                country_filter=default_country,
            )
        else:
            discovered_channels = parse_generic_channel_json(
                cached_file,
                default_country=default_country,
                default_group=default_group,
            )
        detected_count = len(discovered_channels)
    else:
        discovered_channels = extract_text_links_from_file(
            cached_file,
            int(config.get("chunk_size_bytes", 65536)),
        )
        detected_count = len(discovered_channels)

    existing_channels = load_channels_raw(sources_path)
    merged_channels, added = merge_channels(
        existing_channels,
        discovered_channels,
        default_group=default_group,
        default_country=default_country,
    )
    save_channels_raw(merged_channels, sources_path)

    print("=" * 50)
    print("Resumen de importacion de canales")
    print("=" * 50)
    print(f"URL origen:         {resolved_source_url}")
    print(f"Registros leidos:   {detected_count}")
    print(f"Nuevos agregados:   {added}")
    print(f"Total en sources:   {len(merged_channels)}")
    print("=" * 50)
    return added


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Importa canales desde una URL publica de texto plano, JSON o M3U "
            "y los agrega a channels.json."
        )
    )
    parser.add_argument(
        "source_url",
        nargs="?",
        default=DEFAULT_SOURCE_URL,
        help=f"URL publica de texto plano o M3U a inspeccionar (default: {DEFAULT_SOURCE_URL})",
    )
    parser.add_argument("--sources", type=Path, default=SOURCES_FILE, help="Ruta a channels.json")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Ruta a config.json")
    parser.add_argument("--group", default="Importados", help="Grupo por defecto para canales nuevos")
    parser.add_argument("--country", default="", help="Pais por defecto para canales nuevos")
    parser.add_argument("--metadata-url", default=None, help="URL opcional de metadatos para fuentes JSON estructuradas")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(
            run(
                args.source_url,
                args.sources,
                args.config,
                default_group=args.group,
                default_country=args.country,
                metadata_url=args.metadata_url,
            )
        )
    except aiohttp.ClientResponseError as exc:
        print(f"[ERROR] El origen respondio con HTTP {exc.status}: {exc.message}")
        return 1
    except aiohttp.ClientError as exc:
        print(f"[ERROR] Fallo de red al leer la fuente: {exc}")
        return 1
    except asyncio.TimeoutError:
        print("[ERROR] Timeout agotado al leer la fuente remota")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Error inesperado: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
