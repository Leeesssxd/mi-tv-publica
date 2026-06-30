#!/usr/bin/env python3
"""
generate_trending_vod.py
========================

Puebla el bloque "Mi Catálogo Cloud" con un conjunto estático de títulos
cinematográficos y series populares usando sólo metadatos públicos
predefinidos. No consulta APIs privadas ni genera enlaces de reproducción
no autorizados.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT_DIR / "sources" / "channels.json"
CONFIG_FILE = ROOT_DIR / "config.json"
CLOUD_GROUP_NAME = "Mi Catálogo Cloud"
TEMPLATE_MOVIE = os.getenv("VOD_TEMPLATE_MOVIE", "https://localhost/movie/{id}")
TEMPLATE_TV = os.getenv("VOD_TEMPLATE_TV", "https://localhost/tv/{id}")

TRENDING_VOD: list[dict[str, Any]] = [
    {"name": "The Bear", "media_type": "series", "year": 2022, "imdb_id": "tt14452776", "tmdb_id": "136315"},
    {"name": "House of the Dragon", "media_type": "series", "year": 2022, "imdb_id": "tt11198330", "tmdb_id": "94997"},
    {"name": "The Last of Us", "media_type": "series", "year": 2023, "imdb_id": "tt3581920", "tmdb_id": "100088"},
    {"name": "Andor", "media_type": "series", "year": 2022, "imdb_id": "tt9253284", "tmdb_id": "83867"},
    {"name": "Severance", "media_type": "series", "year": 2022, "imdb_id": "tt11280740", "tmdb_id": "95396"},
    {"name": "Fallout", "media_type": "series", "year": 2024, "imdb_id": "tt12637874", "tmdb_id": "106379"},
    {"name": "Shogun", "media_type": "series", "year": 2024, "imdb_id": "tt2788316", "tmdb_id": "126308"},
    {"name": "The White Lotus", "media_type": "series", "year": 2021, "imdb_id": "tt13406094", "tmdb_id": "111803"},
    {"name": "The Boys", "media_type": "series", "year": 2019, "imdb_id": "tt1190634", "tmdb_id": "76479"},
    {"name": "Bridgerton", "media_type": "series", "year": 2020, "imdb_id": "tt8740790", "tmdb_id": "91239"},
    {"name": "Wednesday", "media_type": "series", "year": 2022, "imdb_id": "tt13443470", "tmdb_id": "119051"},
    {"name": "True Detective", "media_type": "series", "year": 2014, "imdb_id": "tt2356777", "tmdb_id": "46648"},
    {"name": "The Gentlemen", "media_type": "series", "year": 2024, "imdb_id": "tt13210838", "tmdb_id": "236994"},
    {"name": "Ripley", "media_type": "series", "year": 2024, "imdb_id": "tt11016042", "tmdb_id": "138575"},
    {"name": "3 Body Problem", "media_type": "series", "year": 2024, "imdb_id": "tt13016388", "tmdb_id": "100757"},
    {"name": "Presumed Innocent", "media_type": "series", "year": 2024, "imdb_id": "tt17677860", "tmdb_id": "158572"},
    {"name": "The Penguin", "media_type": "series", "year": 2024, "imdb_id": "tt15435876", "tmdb_id": "194764"},
    {"name": "Nobody Wants This", "media_type": "series", "year": 2024, "imdb_id": "tt28052847", "tmdb_id": "243106"},
    {"name": "The Substance", "media_type": "movie", "year": 2024, "imdb_id": "tt17526714", "tmdb_id": "933260"},
    {"name": "Dune: Part Two", "media_type": "movie", "year": 2024, "imdb_id": "tt15239678", "tmdb_id": "693134"},
    {"name": "Inside Out 2", "media_type": "movie", "year": 2024, "imdb_id": "tt22022452", "tmdb_id": "1022789"},
    {"name": "Civil War", "media_type": "movie", "year": 2024, "imdb_id": "tt17279496", "tmdb_id": "929590"},
    {"name": "Furiosa: A Mad Max Saga", "media_type": "movie", "year": 2024, "imdb_id": "tt12037194", "tmdb_id": "786892"},
    {"name": "Kingdom of the Planet of the Apes", "media_type": "movie", "year": 2024, "imdb_id": "tt11389872", "tmdb_id": "653346"},
    {"name": "Deadpool & Wolverine", "media_type": "movie", "year": 2024, "imdb_id": "tt6263850", "tmdb_id": "533535"},
    {"name": "Twisters", "media_type": "movie", "year": 2024, "imdb_id": "tt12584954", "tmdb_id": "718821"},
    {"name": "Beetlejuice Beetlejuice", "media_type": "movie", "year": 2024, "imdb_id": "tt2049403", "tmdb_id": "917496"},
    {"name": "Alien: Romulus", "media_type": "movie", "year": 2024, "imdb_id": "tt18412256", "tmdb_id": "945961"},
    {"name": "The Wild Robot", "media_type": "movie", "year": 2024, "imdb_id": "tt29623480", "tmdb_id": "1184918"},
    {"name": "Flow", "media_type": "movie", "year": 2024, "imdb_id": "tt4772188", "tmdb_id": "823219"},
    {"name": "Anora", "media_type": "movie", "year": 2024, "imdb_id": "tt28607951", "tmdb_id": "1064213"},
    {"name": "Challengers", "media_type": "movie", "year": 2024, "imdb_id": "tt16426418", "tmdb_id": "937287"},
    {"name": "The Fall Guy", "media_type": "movie", "year": 2024, "imdb_id": "tt1684562", "tmdb_id": "746036"},
    {"name": "A Quiet Place: Day One", "media_type": "movie", "year": 2024, "imdb_id": "tt13433802", "tmdb_id": "762441"},
    {"name": "Longlegs", "media_type": "movie", "year": 2024, "imdb_id": "tt23468450", "tmdb_id": "1226578"},
    {"name": "Gladiator II", "media_type": "movie", "year": 2024, "imdb_id": "tt9218128", "tmdb_id": "558449"},
    {"name": "Wicked", "media_type": "movie", "year": 2024, "imdb_id": "tt1262426", "tmdb_id": "402431"},
    {"name": "Moana 2", "media_type": "movie", "year": 2024, "imdb_id": "tt13622970", "tmdb_id": "1241982"},
    {"name": "Sonic the Hedgehog 3", "media_type": "movie", "year": 2024, "imdb_id": "tt18259086", "tmdb_id": "939243"},
    {"name": "Mickey 17", "media_type": "movie", "year": 2025, "imdb_id": "tt12299608", "tmdb_id": "696506"},
    {"name": "Superman", "media_type": "movie", "year": 2025, "imdb_id": "tt5950044", "tmdb_id": "1061474"},
    {"name": "The Fantastic Four: First Steps", "media_type": "movie", "year": 2025, "imdb_id": "tt10676052", "tmdb_id": "617126"},
    {"name": "Mission: Impossible - The Final Reckoning", "media_type": "movie", "year": 2025, "imdb_id": "tt9603208", "tmdb_id": "575265"},
    {"name": "Elio", "media_type": "movie", "year": 2025, "imdb_id": "tt22773644", "tmdb_id": "1022787"},
    {"name": "Jurassic World Rebirth", "media_type": "movie", "year": 2025, "imdb_id": "tt31036941", "tmdb_id": "1234821"},
    {"name": "Captain America: Brave New World", "media_type": "movie", "year": 2025, "imdb_id": "tt14513804", "tmdb_id": "822119"},
    {"name": "Thunderbolts*", "media_type": "movie", "year": 2025, "imdb_id": "tt20969586", "tmdb_id": "986056"},
    {"name": "Avatar: Fire and Ash", "media_type": "movie", "year": 2025, "imdb_id": "tt1757678", "tmdb_id": "83533"},
    {"name": "The Batman Part II", "media_type": "movie", "year": 2026, "imdb_id": "tt19850008", "tmdb_id": "1061473"},
    {"name": "Wake Up Dead Man: A Knives Out Mystery", "media_type": "movie", "year": 2025, "imdb_id": "tt29622761", "tmdb_id": "1093237"},
]


def load_sources_payload(sources_path: Path = SOURCES_FILE) -> Any:
    if not sources_path.exists():
        return []
    return json.loads(sources_path.read_text(encoding="utf-8"))


def load_config(config_path: Path = CONFIG_FILE) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_env_overrides(env_path: Path | None = None) -> dict[str, str]:
    dot_env_path = env_path or (ROOT_DIR / ".env")
    if not dot_env_path.exists():
        return {}

    overrides: dict[str, str] = {}
    for raw_line in dot_env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cleaned_key = key.strip()
        cleaned_value = value.strip().strip('"').strip("'")
        if cleaned_key:
            overrides[cleaned_key] = cleaned_value
    return overrides


def save_sources_payload(payload: Any, sources_path: Path = SOURCES_FILE) -> None:
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def tmdb_path(item: dict[str, Any]) -> str:
    kind = "movie" if item["media_type"] == "movie" else "tv"
    return f"https://www.themoviedb.org/{kind}/{item['tmdb_id']}"


def imdb_path(item: dict[str, Any]) -> str:
    return f"https://www.imdb.com/title/{item['imdb_id']}/"


def youtube_search_path(item: dict[str, Any]) -> str:
    query = f"{item['name']} {item['year']} trailer oficial"
    from urllib.parse import quote_plus
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def resolve_templates(config: dict[str, Any] | None = None) -> tuple[str, str]:
    config = config or {}
    custom_routing_rules = config.get("custom_routing_rules")
    routing_rules = custom_routing_rules if isinstance(custom_routing_rules, dict) else {}
    cloud_rules = routing_rules.get("cloud_catalog")
    cloud_catalog_rules = cloud_rules if isinstance(cloud_rules, dict) else {}
    env_overrides = load_env_overrides()
    movie_template = str(
        os.getenv("VOD_TEMPLATE_MOVIE")
        or env_overrides.get("VOD_TEMPLATE_MOVIE")
        or cloud_catalog_rules.get("VOD_TEMPLATE_MOVIE")
        or cloud_catalog_rules.get("vod_template_movie")
        or routing_rules.get("VOD_TEMPLATE_MOVIE")
        or routing_rules.get("vod_template_movie")
        or config.get("VOD_TEMPLATE_MOVIE")
        or config.get("vod_template_movie")
        or TEMPLATE_MOVIE
    ).strip()
    tv_template = str(
        os.getenv("VOD_TEMPLATE_TV")
        or env_overrides.get("VOD_TEMPLATE_TV")
        or cloud_catalog_rules.get("VOD_TEMPLATE_TV")
        or cloud_catalog_rules.get("vod_template_tv")
        or routing_rules.get("VOD_TEMPLATE_TV")
        or routing_rules.get("vod_template_tv")
        or config.get("VOD_TEMPLATE_TV")
        or config.get("vod_template_tv")
        or TEMPLATE_TV
    ).strip()
    return movie_template, tv_template


def build_templated_url(item: dict[str, Any], movie_template: str, tv_template: str) -> str:
    media_type = str(item.get("media_type") or "").strip().casefold()
    tmdb_id = str(item["tmdb_id"]).strip()
    if media_type == "movie":
        return movie_template.format(id=tmdb_id)
    if media_type in {"series", "tv"}:
        return f"{tv_template.format(id=tmdb_id).rstrip('/')}/1/1"
    raise ValueError(f"media_type no soportado: {item.get('media_type')}")


def build_vod_item(
    item: dict[str, Any],
    movie_template: str = TEMPLATE_MOVIE,
    tv_template: str = TEMPLATE_TV,
) -> dict[str, Any]:
    suffix = "Pelicula" if item["media_type"] == "movie" else "Serie"
    templated_url = build_templated_url(item, movie_template, tv_template)
    route_is_placeholder = "localhost" in templated_url.casefold()
    return {
        "name": f"{item['name']} ({item['year']})",
        "group": CLOUD_GROUP_NAME,
        "country": "ZZ",
        "url": templated_url,
        "logo": "",
        "tvg_id": item["imdb_id"],
        "media_type": item["media_type"],
        "catalog_kind": suffix,
        "imdb_id": item["imdb_id"],
        "tmdb_id": item["tmdb_id"],
        "year": item["year"],
        "reference_url": tmdb_path(item),
        "imdb_url": imdb_path(item),
        "trailer_search_url": youtube_search_path(item),
        "availability": "templated_routing",
        "routing_mode": "placeholder" if route_is_placeholder else "configured",
    }


def upsert_cloud_catalog(payload: Any, cloud_items: list[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(payload, list):
        base_payload: dict[str, Any] = {"channels": payload}
    elif isinstance(payload, dict):
        base_payload = dict(payload)
        if not isinstance(base_payload.get("channels"), list):
            base_payload["channels"] = []
    else:
        base_payload = {"channels": []}

    base_payload["cloud_catalog"] = {
        "name": CLOUD_GROUP_NAME,
        "group": CLOUD_GROUP_NAME,
        "country": "ZZ",
        "items": cloud_items,
    }
    return base_payload


def run(sources_path: Path = SOURCES_FILE, config_path: Path = CONFIG_FILE) -> int:
    payload = load_sources_payload(sources_path)
    config = load_config(config_path)
    movie_template, tv_template = resolve_templates(config)
    cloud_items = [build_vod_item(item, movie_template, tv_template) for item in TRENDING_VOD]
    updated_payload = upsert_cloud_catalog(payload, cloud_items)
    save_sources_payload(updated_payload, sources_path)
    print("=" * 50)
    print("Resumen de catalogo VOD estatico")
    print("=" * 50)
    print(f"Titulos cargados:    {len(cloud_items)}")
    print(f"Bloque actualizado:  {CLOUD_GROUP_NAME}")
    print(f"Template movie:      {movie_template}")
    print(f"Template tv:         {tv_template}")
    print("=" * 50)
    return len(cloud_items)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Puebla Mi Catálogo Cloud con metadatos VOD estáticos.")
    parser.add_argument("--sources", type=Path, default=SOURCES_FILE, help="Ruta a channels.json")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Ruta a config.json")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        run(args.sources, args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Error inesperado: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
