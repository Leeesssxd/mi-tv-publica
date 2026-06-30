#!/usr/bin/env python3
"""
cloud_vod.py
============

Conector ligero para catálogos VOD alojados en un backend WebDAV / Debrid.
Lee un arbol virtual de medios mediante un endpoint parametrizado por
variables de entorno y actualiza un bloque dedicado dentro de
sources/channels.json sin alterar el resto del catalogo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import aiohttp

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT_DIR / "sources" / "channels.json"
CONFIG_FILE = ROOT_DIR / "config.json"

DEFAULT_TIMEOUT_SECONDS = 20
CLOUD_GROUP_NAME = "Mi Catálogo Cloud"
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".ts"}


def load_config(config_path: Path = CONFIG_FILE) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_sources_payload(sources_path: Path = SOURCES_FILE) -> Any:
    if not sources_path.exists():
        return []
    return json.loads(sources_path.read_text(encoding="utf-8"))


def save_sources_payload(payload: Any, sources_path: Path = SOURCES_FILE) -> None:
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def clean_media_title(raw_name: str) -> str:
    name = Path((raw_name or "").strip()).stem
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Media Cloud"


def item_name(item: dict[str, Any]) -> str:
    for key in ("name", "title", "filename", "basename", "label"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    path_value = item_path(item)
    return Path(path_value).name if path_value else ""


def item_path(item: dict[str, Any]) -> str:
    for key in ("path", "full_path", "relative_path", "filepath", "file", "url_path"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def item_identifier(item: dict[str, Any]) -> str:
    for key in ("id", "file_id", "stream_id", "uuid"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def is_media_file(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False

    item_type = str(item.get("type") or item.get("kind") or "").strip().lower()
    if item_type in {"dir", "directory", "folder"}:
        return False

    name = item_name(item)
    path_value = item_path(item)
    for candidate in (name, path_value):
        suffix = Path(candidate).suffix.lower()
        if suffix in MEDIA_EXTENSIONS:
            return True
    return False


def extract_media_entries(payload: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if is_media_file(value):
                entries.append(value)
                return
            for key in ("children", "items", "entries", "files", "results", "data", "nodes"):
                nested = value.get(key)
                if isinstance(nested, (list, dict)):
                    visit(nested)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return entries


def build_authenticated_stream_url(base_url: str, api_key: str, item: dict[str, Any]) -> str:
    path_value = item_path(item).lstrip("/")
    identifier = item_identifier(item)
    url = base_url

    replacements = {
        "{token}": quote(api_key, safe=""),
        "{api_key}": quote(api_key, safe=""),
        "{path}": quote(path_value, safe="/"),
        "{id}": quote(identifier, safe=""),
    }
    for placeholder, replacement in replacements.items():
        url = url.replace(placeholder, replacement)

    if "{path}" not in base_url and path_value:
        parsed = urlparse(url)
        joined_path = parsed.path.rstrip("/") + "/" + quote(path_value, safe="/")
        url = urlunparse(parsed._replace(path=joined_path))

    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if api_key and "token" not in query and "api_key" not in query:
        query["token"] = api_key
    if identifier and "{id}" not in base_url and "id" not in query:
        query["id"] = identifier
    if path_value and "{path}" not in base_url and "path" not in query:
        query["path"] = path_value

    return urlunparse(parsed._replace(query=urlencode(query)))


def build_cloud_record(item: dict[str, Any], stream_url_template: str, api_key: str) -> dict[str, Any]:
    return {
        "name": clean_media_title(item_name(item)),
        "group": CLOUD_GROUP_NAME,
        "country": "ZZ",
        "url": build_authenticated_stream_url(stream_url_template, api_key, item),
        "logo": "",
        "tvg_id": item_identifier(item),
    }


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_names: dict[str, int] = {}

    for record in records:
        url = str(record.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        name = str(record.get("name") or "").strip() or "Media Cloud"
        key = name.casefold()
        if key in seen_names:
            seen_names[key] += 1
            name = f"{name} ({seen_names[key]})"
        else:
            seen_names[key] = 1

        normalized = dict(record)
        normalized["name"] = name
        deduped.append(normalized)

    return deduped


def upsert_cloud_catalog(payload: Any, cloud_records: list[dict[str, Any]]) -> dict[str, Any]:
    base_payload: dict[str, Any]
    if isinstance(payload, list):
        base_payload = {"channels": payload}
    elif isinstance(payload, dict):
        base_payload = dict(payload)
        if not isinstance(base_payload.get("channels"), list):
            existing_channels = base_payload.get("channels")
            if isinstance(existing_channels, list):
                pass
            else:
                base_payload["channels"] = []
    else:
        base_payload = {"channels": []}

    base_payload["cloud_catalog"] = {
        "name": CLOUD_GROUP_NAME,
        "group": CLOUD_GROUP_NAME,
        "country": "ZZ",
        "items": dedupe_records(cloud_records),
    }
    return base_payload


async def fetch_cloud_tree(api_key: str, endpoint: str, timeout_seconds: float) -> Any:
    headers = {
        "User-Agent": "MiTVPublicaCloudConnector/1.0",
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {api_key}",
        "X-API-Key": api_key,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(endpoint, ssl=False) as response:
            response.raise_for_status()
            return await response.json(content_type=None)


async def run(
    *,
    sources_path: Path,
    config_path: Path,
    api_key: str | None,
    endpoint: str | None,
) -> int:
    api_key = api_key or os.getenv("CLOUD_API_KEY", "").strip()
    endpoint = endpoint or os.getenv("CLOUD_STREAM_URL", "").strip()
    config = load_config(config_path)
    timeout_seconds = float(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))

    existing_payload = load_sources_payload(sources_path)
    if not api_key or not endpoint:
        updated_payload = upsert_cloud_catalog(existing_payload, [])
        save_sources_payload(updated_payload, sources_path)
        print("[WARN] CLOUD_API_KEY/CLOUD_STREAM_URL no configurados; se creo bloque vacio de Mi Catálogo Cloud.")
        return 0

    tree_payload = await fetch_cloud_tree(api_key, endpoint, timeout_seconds)
    items = extract_media_entries(tree_payload)
    cloud_records = [build_cloud_record(item, endpoint, api_key) for item in items]
    updated_payload = upsert_cloud_catalog(existing_payload, cloud_records)
    save_sources_payload(updated_payload, sources_path)

    print("=" * 50)
    print("Resumen de catalogo cloud")
    print("=" * 50)
    print(f"Registros detectados: {len(items)}")
    print(f"Registros inyectados: {len(dedupe_records(cloud_records))}")
    print("=" * 50)
    return len(cloud_records)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Actualiza el bloque Mi Catálogo Cloud en channels.json.")
    parser.add_argument("--sources", type=Path, default=SOURCES_FILE, help="Ruta a channels.json")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Ruta a config.json")
    parser.add_argument("--api-key", default=None, help="Override para CLOUD_API_KEY")
    parser.add_argument("--endpoint", default=None, help="Override para CLOUD_STREAM_URL")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(
            run(
                sources_path=args.sources,
                config_path=args.config,
                api_key=args.api_key,
                endpoint=args.endpoint,
            )
        )
    except aiohttp.ClientError as exc:
        print(f"[ERROR] Fallo de red en catalogo cloud: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Error inesperado: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
