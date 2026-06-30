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
LOCAL_PRIVATE_SOURCES_FILE = ROOT_DIR / "sources" / "local_private_sources.json"
QUARANTINE_SOURCES_FILE = ROOT_DIR / "sources" / "quarantine_sources.json"
CONFIG_FILE = ROOT_DIR / "config.json"
CACHE_DIR = ROOT_DIR / ".cache" / "sources"
ENV_FILE = ROOT_DIR / ".env"
ENV_EXAMPLE_FILE = ROOT_DIR / ".env.example"
PUBLIC_DIR = ROOT_DIR / "public"
TELEMETRY_STATUS_FILE = PUBLIC_DIR / "telemetry_status.json"
SECRET_UPSTREAM_POOLS_ENV = "SECRET_UPSTREAM_POOLS"
DEFAULT_SOURCE_URL = "https://iptv-org.github.io/iptv/countries/mx.m3u"
DEFAULT_IPTVORG_CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
DEFAULT_SECONDARY_SOURCES: list[dict[str, Any]] = [
    {
        "source_url": "https://raw.githubusercontent.com/Alplox/json-teles/main/canales.json",
        "group": "Familia y TV Abierta",
        "country": "MX",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/no.m3u",
        "group": "Deportes Públicos Internacionales",
        "country": "NO",
    },
]
DEFAULT_LOCAL_PRIVATE_SOURCES: list[dict[str, str]] = [
    {
        "source_env": "PRIVATE_SOURCE_1",
        "group": "Deportes Locales",
        "country": "ALL",
    }
]

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
    "max_secret_sources": 1,
    "max_private_channels_per_source": 500,
}

M3U8_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>()\[\]]+?\.m3u8(?:\?[^\s\"'<>()\[\]]*)?",
    re.IGNORECASE,
)
EXTINF_PATTERN = re.compile(r"^#EXTINF:-?\d+\s*(?P<attrs>.*?),(?P<name>.*)$")
ATTR_PATTERN = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
TVG_ID_COUNTRY_PATTERN = re.compile(r"\.([A-Za-z]{2})(?:$|[@._-])")
RETRIABLE_STATUS_CODES = {429, 503}
M3U_HEADER = "#EXTM3U"
RESTRICTIVE_STATUS_CODES = {401, 403, 503}
QUARANTINE_THRESHOLD = 3


def detect_payload_kind(text: str) -> str:
    stripped = (text or "").lstrip("\ufeff").lstrip()
    if stripped.startswith(M3U_HEADER) or stripped.startswith("#EXTINF"):
        return "m3u"
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    return "text"


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


def load_env_file(env_path: Path | None = None) -> dict[str, str]:
    env_path = env_path or ENV_FILE
    if not env_path.exists():
        return {}

    resolved: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            resolved[key] = value
    return resolved


def ensure_env_example_file(env_example_path: Path | None = None) -> None:
    env_example_path = env_example_path or ENV_EXAMPLE_FILE
    if env_example_path.exists():
        return

    lines = [
        "# Variables locales para fuentes privadas autorizadas",
        "PRIVATE_SOURCE_1=http://provider.example:8080/get.php?username=USER1&password=PASS1&type=m3u_plus",
        "PRIVATE_SOURCE_2=http://provider.example:8080/get.php?username=USER2&password=PASS2&type=m3u_plus",
        "",
        "# Solo para GitHub Actions: lista JSON de fuentes remotas autorizadas.",
        '# SECRET_UPSTREAM_POOLS=["http://provider.example/feed.m3u", {"source_url": "http://provider.example/extra.m3u", "group": "Privados", "country": "ALL"}]',
    ]
    env_example_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_sources_payload(sources_path: Path = SOURCES_FILE) -> Any:
    if not sources_path.exists():
        return []

    return json.loads(sources_path.read_text(encoding="utf-8"))


def ensure_local_private_sources_file(
    local_sources_path: Path | None = None,
) -> list[dict[str, str]]:
    local_sources_path = local_sources_path or LOCAL_PRIVATE_SOURCES_FILE
    if not local_sources_path.exists():
        local_sources_path.parent.mkdir(parents=True, exist_ok=True)
        local_sources_path.write_text(
            json.dumps(DEFAULT_LOCAL_PRIVATE_SOURCES, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return list(DEFAULT_LOCAL_PRIVATE_SOURCES)

    try:
        payload = json.loads(local_sources_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[WARN] local_private_sources.json invalido, se omite: {exc}")
        return []

    if not isinstance(payload, list):
        print("[WARN] local_private_sources.json debe contener una lista, se omite.")
        return []

    sanitized_items: list[dict[str, str]] = []
    mutated = False
    next_env_index = 1

    for item in payload:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        raw_url = str(candidate.get("source_url") or "").strip()
        source_env = str(candidate.get("source_env") or "").strip()
        if raw_url and not source_env:
            candidate.pop("source_url", None)
            candidate["source_env"] = f"PRIVATE_SOURCE_{next_env_index}"
            next_env_index += 1
            mutated = True
        elif source_env.startswith("PRIVATE_SOURCE_"):
            try:
                next_env_index = max(next_env_index, int(source_env.removeprefix("PRIVATE_SOURCE_")) + 1)
            except ValueError:
                pass
        sanitized_items.append(candidate)

    if mutated:
        local_sources_path.write_text(
            json.dumps(sanitized_items, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    return sanitized_items


def resolve_source_url(source_spec: dict[str, Any], env_values: dict[str, str]) -> str:
    source_env = str(source_spec.get("source_env") or "").strip()
    if source_env:
        return str(env_values.get(source_env) or os.getenv(source_env) or "").strip()
    return str(source_spec.get("source_url") or "").strip()


def load_secret_upstream_pools(secret_value: str | None) -> list[dict[str, Any]]:
    if not secret_value:
        return []
    try:
        payload = json.loads(secret_value)
    except json.JSONDecodeError as exc:
        print(f"[WARN] {SECRET_UPSTREAM_POOLS_ENV} invalido, se omite: {exc}")
        return []

    if not isinstance(payload, list):
        print(f"[WARN] {SECRET_UPSTREAM_POOLS_ENV} debe contener una lista JSON, se omite.")
        return []

    resolved: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, str):
            url = item.strip()
            if url:
                resolved.append({"source_url": url, "group": "Privados Cloud", "country": "ALL"})
            continue
        if isinstance(item, dict):
            resolved.append(item)
    return resolved


def limit_discovered_channels(
    discovered_channels: list[dict[str, Any]] | list[str],
    *,
    max_items: int | None,
) -> list[dict[str, Any]] | list[str]:
    if max_items is None or max_items <= 0:
        return discovered_channels
    return discovered_channels[:max_items]


def load_quarantine_state(quarantine_path: Path | None = None) -> dict[str, dict[str, Any]]:
    quarantine_path = quarantine_path or QUARANTINE_SOURCES_FILE
    if not quarantine_path.exists():
        return {}
    try:
        payload = json.loads(quarantine_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if isinstance(value, dict)
    }


def save_quarantine_state(state: dict[str, dict[str, Any]], quarantine_path: Path | None = None) -> None:
    quarantine_path = quarantine_path or QUARANTINE_SOURCES_FILE
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    quarantine_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_source_fingerprint(source_spec: dict[str, Any], resolved_url: str) -> str:
    source_env = str(source_spec.get("source_env") or "").strip()
    if source_env:
        return f"env:{source_env}"
    return normalize_source_url(resolved_url)


def extract_http_status(exc: Exception) -> int | None:
    if isinstance(exc, aiohttp.ClientResponseError):
        return int(exc.status)
    return None


def update_quarantine_entry(
    state: dict[str, dict[str, Any]],
    fingerprint: str,
    source_spec: dict[str, Any],
    resolved_url: str,
    *,
    status: str,
    http_status: int | None,
) -> dict[str, Any]:
    entry = dict(state.get(fingerprint) or {})
    entry["source_env"] = str(source_spec.get("source_env") or "").strip()
    entry["group"] = str(source_spec.get("group") or "").strip()
    entry["country"] = str(source_spec.get("country") or "").strip()
    entry["source_url_hint"] = resolved_url.split("?", 1)[0] if resolved_url else ""
    entry["last_status"] = status
    entry["last_http_status"] = http_status
    entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if http_status in RESTRICTIVE_STATUS_CODES:
        entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
    elif status == "success":
        entry["consecutive_failures"] = 0
        entry["quarantined"] = False
    else:
        entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0)

    if int(entry.get("consecutive_failures") or 0) >= QUARANTINE_THRESHOLD:
        entry["quarantined"] = True
    else:
        entry.setdefault("quarantined", False)

    state[fingerprint] = entry
    return entry


def write_telemetry_report(records: list[dict[str, Any]], output_path: Path | None = None) -> None:
    output_path = output_path or TELEMETRY_STATUS_FILE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_sources": len(records),
        "healthy_sources": sum(1 for item in records if item.get("status") == "success"),
        "quarantined_sources": sum(1 for item in records if item.get("quarantined")),
        "sources": records,
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _extract_channel_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        raise ValueError("sources/channels.json debe contener una lista o un objeto compatible")

    if isinstance(payload.get("channels"), list):
        extracted = [item for item in payload["channels"] if isinstance(item, dict)]
        for key, value in payload.items():
            if key in {"channels", "cloud_catalog"}:
                continue
            if isinstance(value, (list, dict)):
                extracted.extend(_extract_channel_entries(value))
        return extracted

    extracted: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    if "name" in item and "url" in item:
                        extracted.append(item)
                    else:
                        for nested in item.values():
                            visit(nested)
        elif isinstance(value, dict):
            if "name" in value and "url" in value:
                extracted.append(value)
            else:
                for nested in value.values():
                    visit(nested)

    visit(payload)
    return extracted


def load_channels_raw(sources_path: Path = SOURCES_FILE) -> list[dict[str, Any]]:
    return _extract_channel_entries(load_sources_payload(sources_path))


def save_channels_raw(channels: list[dict[str, Any]], sources_path: Path = SOURCES_FILE) -> None:
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text(json.dumps(channels, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_sources_payload(payload: Any, sources_path: Path = SOURCES_FILE) -> None:
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def normalize_url(url: str) -> str:
    cleaned = html.unescape((url or "").strip())
    return cleaned.rstrip(".,;)]}>\"'")


def is_supported_playlist_url(url: str) -> bool:
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_source_url(url: str) -> str:
    return normalize_url(url).casefold()


def describe_source_error(prefix: str, source_url: str, exc: Exception) -> str:
    if isinstance(exc, aiohttp.ClientResponseError) and exc.status in {401, 403}:
        return f"[INFO] {prefix} rechazada por el origen: {source_url} -> HTTP {exc.status} ({exc.message})"
    return f"[WARN] {prefix} omitida por fallo de red o parsing: {source_url} -> {exc}"


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
            if pending_attrs and is_supported_playlist_url(line):
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
    category_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    streams = json.loads(streams_path.read_text(encoding="utf-8"))
    channels = json.loads(channels_path.read_text(encoding="utf-8"))

    country_filter = country_filter.strip().upper()
    normalized_categories = {str(item).strip().casefold() for item in (category_filter or []) if str(item).strip()}
    channel_map = {
        item["id"]: item
        for item in channels
        if isinstance(item, dict)
        and item.get("id")
        and (not country_filter or str(item.get("country", "")).upper() == country_filter)
        and (
            not normalized_categories
            or normalized_categories.intersection(
                {str(category).strip().casefold() for category in (item.get("categories") or [])}
            )
        )
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


def parse_json_teles_channel_json(
    source_path: Path,
    *,
    country_filter: str = "",
) -> list[dict[str, Any]]:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("channels"), list):
        items = payload.get("channels") or []
        payload_mode = "legacy"
    elif isinstance(payload, dict):
        items = list(payload.items())
        payload_mode = "catalog"
    else:
        items = []
        payload_mode = "unknown"
    allowed_countries = {
        token.strip().upper()
        for token in country_filter.split(",")
        if token.strip()
    }

    parsed_channels: list[dict[str, Any]] = []
    for item in items:
        if payload_mode == "legacy":
            if not isinstance(item, dict):
                continue

            name = str(item.get("name") or "").strip()
            country = str(item.get("country") or "").strip().upper()
            if allowed_countries and country not in allowed_countries:
                continue
            category = str(item.get("category") or "").strip().lower()
            logo = str(item.get("logo") or "").strip()
            tvg_id = str(item.get("id") or "").strip()

            if category == "news":
                group = "News"
            elif category == "sports":
                group = "Sports"
            elif category == "movies":
                group = "Movies"
            elif category == "kids":
                group = "Kids"
            elif category == "culture":
                group = "Culture"
            elif category == "entertainment":
                group = "Entertainment"
            else:
                group = "General"

            signals = item.get("signals") or []
            for signal in signals:
                if not isinstance(signal, dict):
                    continue
                if str(signal.get("type") or "").strip().lower() != "m3u8":
                    continue

                url = normalize_url(str(signal.get("url") or "").strip())
                if not url:
                    continue

                parsed_channels.append(
                    {
                        "name": name or build_channel_name(url, set()),
                        "group": group,
                        "country": country,
                        "url": url,
                        "logo": logo,
                        "tvg_id": tvg_id,
                    }
                )
            continue

        if payload_mode != "catalog":
            continue

        slug, value = item
        if not isinstance(value, dict):
            continue

        name = str(value.get("nombre") or "").strip()
        country = str(value.get("país") or "").strip().upper()
        if allowed_countries and country not in allowed_countries:
            continue

        category = str(value.get("categoría") or "").strip().lower()
        if category == "news":
            group = "News"
        elif category == "sports":
            group = "Sports"
        elif category == "movies":
            group = "Movies"
        elif category == "kids":
            group = "Kids"
        elif category == "culture":
            group = "Culture"
        elif category == "entertainment":
            group = "Entertainment"
        else:
            group = "General"

        signals = value.get("señales") or {}
        m3u8_urls = signals.get("m3u8_url") if isinstance(signals, dict) else []
        if not isinstance(m3u8_urls, list):
            continue

        for raw_url in m3u8_urls:
            url = normalize_url(str(raw_url or "").strip())
            if not url:
                continue
            parsed_channels.append(
                {
                    "name": name or build_channel_name(url, set()),
                    "group": group,
                    "country": country,
                    "url": url,
                    "logo": str(value.get("logo") or "").strip(),
                    "tvg_id": str(slug).strip(),
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

        failover_match = None
        if not isinstance(item, str):
            candidate_name = str(candidate.get("name", "")).strip().casefold()
            candidate_country = str(candidate.get("country", "")).strip().casefold()
            candidate_tvg_id = str(candidate.get("tvg_id", "")).strip().casefold()
            for existing_item in merged:
                if not isinstance(existing_item, dict):
                    continue
                existing_tvg_id = str(existing_item.get("tvg_id", "")).strip().casefold()
                existing_name = str(existing_item.get("name", "")).strip().casefold()
                existing_country = str(existing_item.get("country", "")).strip().casefold()
                same_channel = bool(
                    (candidate_tvg_id and existing_tvg_id and candidate_tvg_id == existing_tvg_id)
                    or (
                        not candidate_tvg_id
                        and not existing_tvg_id
                        and candidate_name
                        and candidate_name == existing_name
                        and candidate_country == existing_country
                        and str(candidate.get("group", "")).strip().casefold()
                        == str(existing_item.get("group", "")).strip().casefold()
                    )
                )
                if not same_channel:
                    continue
                existing_primary = normalize_url(str(existing_item.get("url", "")))
                existing_backup = normalize_url(str(existing_item.get("backup_url", "")))
                if normalized_url in {existing_primary, existing_backup}:
                    failover_match = "duplicate"
                    break
                if not existing_item.get("backup_url"):
                    existing_item["backup_url"] = normalized_url
                    existing_urls.add(normalized_url)
                    added += 1
                    failover_match = "promoted_to_backup"
                    break
            if failover_match:
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


async def import_single_source(
    source_url: str,
    sources_path: Path,
    config: dict[str, Any],
    *,
    default_group: str,
    default_country: str,
    metadata_url: str | None = None,
    category_filter: list[str] | None = None,
    max_channels: int | None = None,
) -> tuple[int, int]:
    cached_file = await fetch_source_to_cache(source_url, config)

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
                category_filter=category_filter,
            )
        elif "iptv-org.github.io/api/streams.json" in source_url:
            metadata_file = await fetch_source_to_cache(DEFAULT_IPTVORG_CHANNELS_URL, config)
            discovered_channels = parse_iptv_org_streams(
                cached_file,
                metadata_file,
                country_filter=default_country,
                category_filter=category_filter,
            )
        elif "raw.githubusercontent.com/alplox/json-teles" in source_url.casefold():
            discovered_channels = parse_json_teles_channel_json(
                cached_file,
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

    discovered_channels = limit_discovered_channels(
        discovered_channels,
        max_items=max_channels,
    )
    detected_count = len(discovered_channels)

    payload = load_sources_payload(sources_path)
    existing_channels = _extract_channel_entries(payload)
    merged_channels, added = merge_channels(
        existing_channels,
        discovered_channels,
        default_group=default_group,
        default_country=default_country,
    )
    if isinstance(payload, dict) and isinstance(payload.get("channels"), list):
        payload["channels"] = merged_channels
        save_sources_payload(payload, sources_path)
    else:
        save_channels_raw(merged_channels, sources_path)

    return detected_count, added


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


def load_any_cached_path(source_url: str, cache_dir: Path = CACHE_DIR) -> Path | None:
    cache_stub = cache_path_for_url(source_url, cache_dir)
    if cache_stub.exists():
        return cache_stub

    matches = sorted(cache_dir.glob(f"{cache_stub.stem}.*"), key=lambda item: item.stat().st_mtime, reverse=True)
    for match in matches:
        if match.is_file():
            return match
    return None


def load_cached_path(source_url: str, ttl_seconds: int, cache_dir: Path = CACHE_DIR) -> Path | None:
    cache_path = load_any_cached_path(source_url, cache_dir)
    if cache_path is None or not cache_path.exists():
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
                    if response.status == 403:
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=(
                                "Acceso prohibido por el origen (403). "
                                "La URL fue alcanzada, pero el proveedor rechazo la solicitud."
                            ),
                            headers=response.headers,
                        )
                    if response.status in RETRIABLE_STATUS_CODES and attempt < attempts:
                        await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                        continue
                    response.raise_for_status()
                    response_text = await response.text(errors="ignore")
                    payload_kind = detect_payload_kind(response_text)
                    cache_path = cache_path_for_url(source_url)
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    if payload_kind == "m3u":
                        cache_path = cache_path.with_suffix(".m3u")
                    elif payload_kind == "json":
                        cache_path = cache_path.with_suffix(".json")
                    cache_path.write_text(response_text, encoding="utf-8")
                    return cache_path
            except asyncio.TimeoutError:
                stale_cached = load_any_cached_path(source_url)
                if stale_cached is not None:
                    print(f"[INFO] Reutilizando cache vencido para {source_url} tras timeout.")
                    return stale_cached
                if attempt < attempts:
                    await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                raise
            except aiohttp.ClientResponseError:
                stale_cached = load_any_cached_path(source_url)
                if stale_cached is not None:
                    print(f"[INFO] Reutilizando cache vencido para {source_url} tras HTTP rechazado.")
                    return stale_cached
                raise
            except aiohttp.ClientError:
                stale_cached = load_any_cached_path(source_url)
                if stale_cached is not None:
                    print(f"[INFO] Reutilizando cache vencido para {source_url} tras fallo de red.")
                    return stale_cached
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
            return detect_payload_kind(stripped) == "m3u"
    return file_path.suffix.lower() == ".m3u"


def file_looks_like_text(file_path: Path) -> bool:
    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for _ in range(20):
            line = handle.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            return detect_payload_kind(stripped) == "text"
    return True


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
    ensure_env_example_file()
    env_values = load_env_file()
    quarantine_state = load_quarantine_state()
    telemetry_records: list[dict[str, Any]] = []
    resolved_source_url = source_url or DEFAULT_SOURCE_URL
    attempted_sources = {normalize_source_url(resolved_source_url)}
    detected_count, added = await import_single_source(
        resolved_source_url,
        sources_path,
        config,
        default_group=default_group,
        default_country=default_country,
        metadata_url=metadata_url,
    )

    secondary_sources = config.get("secondary_sources")
    source_batch = secondary_sources if isinstance(secondary_sources, list) else DEFAULT_SECONDARY_SOURCES
    total_detected = detected_count
    total_added = added
    for source_spec in source_batch:
        if not isinstance(source_spec, dict):
            continue
        batch_url = str(source_spec.get("source_url") or "").strip()
        normalized_batch_url = normalize_source_url(batch_url)
        if not batch_url or normalized_batch_url in attempted_sources:
            continue
        attempted_sources.add(normalized_batch_url)
        try:
            batch_detected, batch_added = await import_single_source(
                batch_url,
                sources_path,
                config,
                default_group=str(source_spec.get("group") or default_group).strip() or default_group,
                default_country=str(source_spec.get("country") or default_country).strip(),
                metadata_url=source_spec.get("metadata_url"),
                category_filter=source_spec.get("categories") if isinstance(source_spec.get("categories"), list) else None,
            )
            total_detected += batch_detected
            total_added += batch_added
        except Exception as exc:  # noqa: BLE001
            print(describe_source_error("Fuente secundaria", batch_url, exc))
            continue

    private_source_specs = [
        *load_secret_upstream_pools(os.getenv(SECRET_UPSTREAM_POOLS_ENV))[: int(config.get("max_secret_sources", 1))],
        *ensure_local_private_sources_file(LOCAL_PRIVATE_SOURCES_FILE),
    ]
    for source_spec in private_source_specs:
        fingerprint = build_source_fingerprint(source_spec, resolve_source_url(source_spec, env_values))
        quarantine_entry = quarantine_state.get(fingerprint) or {}
        resolved_local_url = resolve_source_url(source_spec, env_values)
        telemetry_record = {
            "source_env": str(source_spec.get("source_env") or "").strip(),
            "group": str(source_spec.get("group") or "").strip(),
            "country": str(source_spec.get("country") or "").strip(),
            "source_url_hint": resolved_local_url.split("?", 1)[0] if resolved_local_url else "",
            "status": "skipped",
            "http_status": None,
            "quarantined": bool(quarantine_entry.get("quarantined")),
            "consecutive_failures": int(quarantine_entry.get("consecutive_failures") or 0),
        }
        try:
            batch_url = resolved_local_url
            normalized_batch_url = normalize_source_url(batch_url)
            if not batch_url:
                telemetry_record["status"] = "missing_env"
                telemetry_records.append(telemetry_record)
                continue
            if bool(quarantine_entry.get("quarantined")):
                telemetry_record["status"] = "quarantined"
                telemetry_record["quarantined"] = True
                telemetry_records.append(telemetry_record)
                print(f"[INFO] Fuente local en cuarentena, se omite: {telemetry_record['source_env'] or telemetry_record['source_url_hint']}")
                continue
            if normalized_batch_url in attempted_sources:
                telemetry_record["status"] = "duplicate"
                telemetry_records.append(telemetry_record)
                continue
            attempted_sources.add(normalized_batch_url)
            batch_detected, batch_added = await import_single_source(
                batch_url,
                sources_path,
                config,
                default_group=str(source_spec.get("group") or default_group).strip() or default_group,
                default_country=str(source_spec.get("country") or default_country).strip(),
                metadata_url=source_spec.get("metadata_url"),
                category_filter=source_spec.get("categories") if isinstance(source_spec.get("categories"), list) else None,
                max_channels=int(config.get("max_private_channels_per_source", 500)),
            )
            total_detected += batch_detected
            total_added += batch_added
            updated_entry = update_quarantine_entry(
                quarantine_state,
                fingerprint,
                source_spec,
                batch_url,
                status="success",
                http_status=200,
            )
            telemetry_record["status"] = "success"
            telemetry_record["http_status"] = 200
            telemetry_record["consecutive_failures"] = int(updated_entry.get("consecutive_failures") or 0)
            telemetry_record["quarantined"] = bool(updated_entry.get("quarantined"))
        except Exception as exc:  # noqa: BLE001
            http_status = extract_http_status(exc)
            updated_entry = update_quarantine_entry(
                quarantine_state,
                fingerprint,
                source_spec,
                batch_url,
                status="error",
                http_status=http_status,
            )
            telemetry_record["status"] = "error"
            telemetry_record["http_status"] = http_status
            telemetry_record["consecutive_failures"] = int(updated_entry.get("consecutive_failures") or 0)
            telemetry_record["quarantined"] = bool(updated_entry.get("quarantined"))
            print(describe_source_error("Fuente local", telemetry_record["source_env"] or batch_url, exc))
            if telemetry_record["quarantined"]:
                print(f"[INFO] Fuente local movida a cuarentena: {telemetry_record['source_env'] or telemetry_record['source_url_hint']}")
        telemetry_records.append(telemetry_record)

    merged_channels = _extract_channel_entries(load_sources_payload(sources_path))
    save_quarantine_state(quarantine_state)
    write_telemetry_report(telemetry_records)

    print("=" * 50)
    print("Resumen de importacion de canales")
    print("=" * 50)
    print(f"URL origen:         {resolved_source_url}")
    print(f"Registros leidos:   {total_detected}")
    print(f"Nuevos agregados:   {total_added}")
    print(f"Total en sources:   {len(merged_channels)}")
    print("=" * 50)
    return total_added


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
