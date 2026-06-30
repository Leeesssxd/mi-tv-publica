#!/usr/bin/env python3
"""
build_playlist.py
==================

Lee una lista de canales desde sources/channels.json, revisa de forma
asincrona si cada stream responde correctamente, y genera:

  - public/playlist.m3u   -> canales vivos y temporalmente inestables
  - public/status.json    -> estado detallado de TODOS los canales
  - public/status.md      -> tabla legible en Markdown

Este script verifica URLs que el usuario ya agrego manualmente y tiene
autorizacion para consultar. Para reducir carga sobre los origenes usa
concurrencia conservadora, jitter y reintentos con backoff.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

DEFAULT_CONFIG: dict[str, Any] = {
    "timeout_seconds": 10,
    "max_concurrency": 5,
    "user_agent": "MiTVPublicaBot/1.0 (+https://github.com)",
    "accept_language": "es-MX,es;q=0.9,en;q=0.6",
    "sort_by": ["group", "name"],
    "group_order": [],
    "priority_channels": [],
    "retry_attempts": 3,
    "retry_backoff_base_seconds": 1.0,
    "jitter_min_seconds": 0.5,
    "jitter_max_seconds": 1.5,
    "stream_selection": {
        "mode": "strict",
        "min_width": 1920,
        "min_height": 1080,
        "min_average_bandwidth": 4500000,
        "preferred_frame_rates": [60.0, 30.0, 25.0],
        "allowed_video_codecs": ["avc1"],
        "allowed_audio_codecs": ["mp4a.40.2"],
    },
}

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT_DIR / "sources" / "channels.json"
PUBLIC_DIR = ROOT_DIR / "public"
CONFIG_FILE = ROOT_DIR / "config.json"

PLAYABLE_CONTENT_HINTS = (
    "mpegurl",
    "video/",
    "audio/",
    "octet-stream",
    "mp2t",
)

RETRIABLE_STATUS_CODES = {429, 503}
QUALITY_PATTERN = re.compile(r"(\d{3,4})p", re.IGNORECASE)

FAMILY_PATTERNS = (
    "azteca uno",
    "azteca 7",
    "azteca internacional",
    "b15 ",
    "canal 10 durango",
    "canal 10 cancún",
    "canal 10 cancun",
    "canal 5",
    "las estrellas",
    "canal 13 michoacán",
    "canal 13 bajío",
    "canal 13 bajio",
    "canal 13 campeche",
    "canal 13 chiapas",
    "canal 13 guadalajara",
    "canal 13 oaxaca",
    "canal 13 puebla",
    "canal 13 tabasco",
    "canal 13 tapachula",
    "canal 11",
    "canal 14",
    "canal 21",
    "tv unam",
    "canal 22",
    "canal 6 cdmx",
    "capital 21",
    "canal 28",
    "imagen tv",
    "mexiquense tv",
    "jalisco tv",
    "telemax",
    "canal 44",
    "tv de puebla",
    "canal 10 chiapas",
    "tvp ",
    "canal 66",
    "tele saltillo",
    "mvs tv",
    "set televisión",
    "set television",
    "tv más",
    "tv mas",
    "8ntv",
    "canal 26 aguascalientes",
    "nayarit comunica",
    "rcg tv",
    "rtq querétaro",
    "rtq queretaro",
    "sipse",
    "sistema michoacano",
    "super channel 12",
    "tele yucatan",
    "tv buap",
    "once méxico",
    "once mexico",
    "tv cuatro",
    "sqcs canal 4",
    "trc televisión",
    "trc television",
    "canal 12 iguala",
    "canal 15 ilce",
    "ingenio tv",
    "iertbcs",
    "icrtv colima",
    "canal 33 tijuana",
    "california medios",
    "canal 30 cintalapa",
    "lobo tv",
    "nueve tv",
    "rtg",
    "sizart",
    "tv ug",
    "umtv",
    "unison tv",
    "tv independencia",
    "tv lobo durango",
    "tv mar la paz",
    "tv mar los cabos",
    "tv mar puerto vallarta",
    "tv guanajuato",
    "tv libertad",
    "tv ujat",
    "tlaxcala televisión",
    "tlaxcala television",
    "tele uv",
    "ultra tv puebla",
    "uacj-tv",
    "uacj tv",
    "antena tv",
    "c9ntv",
    "visión televisión",
    "vision television",
    "radiotele morelia",
    "tv pública",
    "tv publica",
    "estrella tv",
    "univision",
    "américa tv",
    "america tv",
    "el trece",
    "bravo tv",
    "tv publica marcos paz",
)

NEWS_PATTERNS = (
    "milenio",
    "telediario",
    "multimedios",
    "c4 en alerta",
    "adn40",
    "adn 40",
    "noticias",
    "notigram",
    "teleformula",
    "fórmula",
    "formula",
    "amx noticias",
    "estrella news",
    "congreso",
    "imagen tv+",
    "justicia tv",
    "meganoticias",
    "canal 26",
    "canal e",
    "tn",
    "la nacion +",
    "asi sucede",
    "así sucede",
)

SPORTS_PATTERNS = (
    "claro sports",
    "itv deportes",
    "aym sports",
    "deportes",
    "sports",
    "wpt",
    "combate",
    "tv cuatro 4.3",
    "tudn",
)

MOVIE_CINE_PATTERNS = (
    "mx nuestro cine",
    "filmex",
    "golden",
    "runtime films",
    "runtime latino",
    "runtime español",
    "runtime espanyol",
    "cine",
)

MOVIE_ACTION_PATTERNS = (
    "runtime acción",
    "runtime accion",
    "acción mexicana",
    "accion mexicana",
)

MOVIE_COMEDY_PATTERNS = (
    "runtime comedia",
    "comedy central",
    "comedia",
)

MOVIE_CRIME_PATTERNS = (
    "runtime crimen",
    "crimen",
)

MOVIE_HORROR_PATTERNS = (
    "runtime terror",
    "terror",
    "panico",
)

MOVIE_DRAMA_PATTERNS = (
    "runtime cine y series",
    "runtime films",
    "runtime latino",
    "runtime español",
    "runtime espanol",
    "runtime espanyol",
    "corazón fast",
    "corazon fast",
    "drama",
    "novelas",
    "series",
)

MOVIE_FAMILY_PATTERNS = (
    "runtime familia",
    "familia",
    "kids",
    "estrella games",
)

GROUP_FAMILIA = "Familia y TV Abierta"
GROUP_NEWS = "Noticias"
GROUP_MOVIES_CINE = "Peliculas - Cine"
GROUP_MOVIES_ACTION = "Peliculas - Accion"
GROUP_MOVIES_COMEDY = "Peliculas - Comedia"
GROUP_MOVIES_CRIME = "Peliculas - Crimen"
GROUP_MOVIES_HORROR = "Peliculas - Terror"
GROUP_MOVIES_DRAMA = "Peliculas - Drama y Series"
GROUP_MOVIES_FAMILY = "Peliculas - Familiar"
GROUP_SPORTS_PUBLIC = "Deportes Públicos Internacionales"
GROUP_SPORTS = "Deportes"
GROUP_ENTERTAINMENT = "Entretenimiento"
GROUP_OTHER = "Otros"
CANONICAL_GROUPS = {
    GROUP_FAMILIA,
    GROUP_NEWS,
    GROUP_MOVIES_CINE,
    GROUP_MOVIES_ACTION,
    GROUP_MOVIES_COMEDY,
    GROUP_MOVIES_CRIME,
    GROUP_MOVIES_HORROR,
    GROUP_MOVIES_DRAMA,
    GROUP_MOVIES_FAMILY,
    GROUP_SPORTS_PUBLIC,
    GROUP_SPORTS,
    GROUP_ENTERTAINMENT,
    GROUP_OTHER,
}


@dataclass
class Channel:
    name: str
    url: str
    group: str = "General"
    country: str = ""
    logo: str = ""
    tvg_id: str = ""

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Channel":
        name = (raw.get("name") or "").strip()
        url = (raw.get("url") or "").strip()
        if not name:
            raise ValueError(f"Canal sin 'name': {raw}")
        if not url:
            raise ValueError(f"Canal sin 'url': {raw}")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"URL invalida (debe empezar con http/https): {url}")

        return Channel(
            name=name,
            url=url,
            group=(raw.get("group") or "General").strip() or "General",
            country=(raw.get("country") or "").strip(),
            logo=(raw.get("logo") or "").strip(),
            tvg_id=(raw.get("tvg_id") or "").strip(),
        )


@dataclass
class ChannelStatus:
    name: str
    group: str
    country: str
    url: str
    logo: str
    tvg_id: str
    alive: bool
    status_code: int | None
    error: str | None
    state: str = "dead"
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def load_channels(sources_path: Path = SOURCES_FILE) -> list[Channel]:
    if not sources_path.exists():
        raise FileNotFoundError(f"No se encontro {sources_path}")

    raw_payload = json.loads(sources_path.read_text(encoding="utf-8"))
    raw_list = _extract_channel_entries(raw_payload)

    channels: list[Channel] = []
    seen_urls: set[str] = set()
    for index, raw in enumerate(raw_list):
        try:
            channel = Channel.from_dict(raw)
            normalized_url = channel.url.casefold()
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            channels.append(channel)
        except ValueError as exc:
            print(f"[WARN] Canal #{index} invalido, se omite: {exc}")
    return channels


def _extract_channel_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        raise ValueError("sources/channels.json debe contener una lista o un objeto compatible")

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


def _looks_playable(content_type: str) -> bool:
    content_type = (content_type or "").lower()
    if not content_type:
        return True
    return any(hint in content_type for hint in PLAYABLE_CONTENT_HINTS)


async def _sleep_with_jitter(config: dict[str, Any]) -> None:
    jitter_min = float(config.get("jitter_min_seconds", 0.5))
    jitter_max = float(config.get("jitter_max_seconds", 1.5))
    await asyncio.sleep(random.uniform(jitter_min, jitter_max))


def _build_headers(config: dict[str, Any]) -> dict[str, str]:
    return {
        "User-Agent": str(config["user_agent"]),
        "Accept": "*/*",
        "Accept-Language": str(config.get("accept_language", "es-MX,es;q=0.9,en;q=0.6")),
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=30, max=100",
    }


def _make_status(
    channel: Channel,
    *,
    state: str,
    status_code: int | None,
    error: str | None,
) -> ChannelStatus:
    return ChannelStatus(
        name=channel.name,
        group=channel.group,
        country=channel.country,
        url=channel.url,
        logo=channel.logo,
        tvg_id=channel.tvg_id,
        alive=state == "alive",
        state=state,
        status_code=status_code,
        error=error,
    )


async def _request_channel(
    session: aiohttp.ClientSession,
    channel: Channel,
    config: dict[str, Any],
) -> ChannelStatus:
    attempts = int(config.get("retry_attempts", 3))
    backoff_base = float(config.get("retry_backoff_base_seconds", 1.0))

    for attempt in range(1, attempts + 1):
        await _sleep_with_jitter(config)
        try:
            async with session.get(
                channel.url,
                allow_redirects=True,
                ssl=False,
            ) as response:
                content_type = response.headers.get("Content-Type", "")
                if response.status in RETRIABLE_STATUS_CODES and attempt < attempts:
                    await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue

                if response.status == 401:
                    return _make_status(
                        channel,
                        state="dead",
                        status_code=response.status,
                        error="No autorizado por el origen (401)",
                    )

                if 200 <= response.status < 400:
                    if _looks_playable(content_type):
                        return _make_status(
                            channel,
                            state="alive",
                            status_code=response.status,
                            error=None,
                        )
                    return _make_status(
                        channel,
                        state="unstable",
                        status_code=response.status,
                        error=f"Handshake correcto pero contenido inestable ({content_type or 'sin content-type'})",
                    )

                return _make_status(
                    channel,
                    state="dead",
                    status_code=response.status,
                    error=f"HTTP {response.status}",
                )
        except asyncio.TimeoutError:
            if attempt < attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            return _make_status(
                channel,
                state="dead",
                status_code=None,
                error="Timeout",
            )
        except aiohttp.ClientError as exc:
            message = str(exc)
            if attempt < attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            return _make_status(
                channel,
                state="dead",
                status_code=None,
                error=message,
            )
        except Exception as exc:  # noqa: BLE001
            return _make_status(
                channel,
                state="dead",
                status_code=None,
                error=f"Error inesperado: {exc}",
            )

    return _make_status(channel, state="dead", status_code=None, error="Agotado tras reintentos")


async def check_channel(
    session: aiohttp.ClientSession,
    channel: Channel,
    semaphore: asyncio.Semaphore,
    config: dict[str, Any],
) -> ChannelStatus:
    async with semaphore:
        return await _request_channel(session, channel, config)


async def check_all_channels(
    channels: list[Channel], config: dict[str, Any]
) -> list[ChannelStatus]:
    timeout = aiohttp.ClientTimeout(total=float(config["timeout_seconds"]))
    headers = _build_headers(config)
    semaphore = asyncio.Semaphore(int(config["max_concurrency"]))

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        tasks = [check_channel(session, ch, semaphore, config) for ch in channels]
        return await asyncio.gather(*tasks)


def _normalize_name(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _quality_score(name: str) -> int:
    match = QUALITY_PATTERN.search(name or "")
    if not match:
        return 0
    return int(match.group(1))


def _priority_rank(status: ChannelStatus, priority_channels: list[str]) -> int:
    normalized_name = _normalize_name(status.name)
    normalized_group = _normalize_name(status.group)

    for index, pattern in enumerate(priority_channels):
        normalized_pattern = _normalize_name(pattern)
        if normalized_pattern and (
            normalized_pattern in normalized_name or normalized_pattern in normalized_group
        ):
            return index
    return len(priority_channels)


def _group_rank(status: ChannelStatus, group_order: list[str]) -> int:
    normalized_group = _normalize_name(status.group)
    normalized_order = [_normalize_name(item) for item in group_order]
    try:
        return normalized_order.index(normalized_group)
    except ValueError:
        return len(group_order)


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in value for pattern in patterns)


def classify_group(name: str, current_group: str) -> str:
    normalized_name = _normalize_name(name)
    normalized_group = _normalize_name(current_group)
    haystack = f"{normalized_name} {normalized_group}".strip()

    if current_group in CANONICAL_GROUPS:
        return current_group
    if _matches_any(haystack, FAMILY_PATTERNS):
        return GROUP_FAMILIA
    if _matches_any(haystack, NEWS_PATTERNS):
        return GROUP_NEWS
    if _matches_any(haystack, SPORTS_PATTERNS):
        return GROUP_SPORTS
    if _matches_any(haystack, MOVIE_ACTION_PATTERNS):
        return GROUP_MOVIES_ACTION
    if _matches_any(haystack, MOVIE_COMEDY_PATTERNS):
        return GROUP_MOVIES_COMEDY
    if _matches_any(haystack, MOVIE_CRIME_PATTERNS):
        return GROUP_MOVIES_CRIME
    if _matches_any(haystack, MOVIE_HORROR_PATTERNS):
        return GROUP_MOVIES_HORROR
    if _matches_any(haystack, MOVIE_DRAMA_PATTERNS):
        return GROUP_MOVIES_DRAMA
    if _matches_any(haystack, MOVIE_FAMILY_PATTERNS):
        return GROUP_MOVIES_FAMILY
    if _matches_any(haystack, MOVIE_CINE_PATTERNS) or "movies" in normalized_group:
        return GROUP_MOVIES_CINE
    if any(token in normalized_group for token in ("entertainment", "music", "culture", "family", "kids")):
        return GROUP_ENTERTAINMENT
    if normalized_group in {"general", "publico", "educativo", "education", "undefined"}:
        return GROUP_ENTERTAINMENT
    return GROUP_OTHER


def regroup_statuses(statuses: list[ChannelStatus]) -> list[ChannelStatus]:
    return [replace(status, group=classify_group(status.name, status.group)) for status in statuses]


def sort_statuses(
    statuses: list[ChannelStatus],
    sort_by: list[str],
    group_order: list[str] | None = None,
    priority_channels: list[str] | None = None,
) -> list[ChannelStatus]:
    priorities = priority_channels or []
    ordered_groups = group_order or []

    def sort_key(status: ChannelStatus) -> tuple:
        return (
            _priority_rank(status, priorities),
            _group_rank(status, ordered_groups),
            -_quality_score(status.name),
            *tuple(str(getattr(status, field_name, "")).lower() for field_name in sort_by),
        )

    return sorted(statuses, key=sort_key)


def _escape_m3u_field(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").replace("\r", " ").strip()


def build_m3u(statuses: list[ChannelStatus]) -> str:
    lines = ["#EXTM3U"]
    for status in statuses:
        if status.state not in {"alive", "unstable"}:
            continue
        name = _escape_m3u_field(status.name)
        group = _escape_m3u_field(status.group)
        tvg_id = _escape_m3u_field(status.tvg_id)
        logo = _escape_m3u_field(status.logo)
        extinf = (
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" '
            f'tvg-logo="{logo}" group-title="{group}",{name}'
        )
        lines.append(extinf)
        lines.append(status.url)
    return "\n".join(lines) + "\n"


def has_playable_channels(statuses: list[ChannelStatus]) -> bool:
    return any(status.state in {"alive", "unstable"} for status in statuses)


def build_status_json(statuses: list[ChannelStatus]) -> str:
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(statuses),
        "alive": sum(1 for s in statuses if s.state == "alive"),
        "unstable": sum(1 for s in statuses if s.state == "unstable"),
        "dead": sum(1 for s in statuses if s.state == "dead"),
        "channels": [s.to_dict() for s in statuses],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def build_status_markdown(statuses: list[ChannelStatus]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = len(statuses)
    alive = sum(1 for s in statuses if s.state == "alive")
    unstable = sum(1 for s in statuses if s.state == "unstable")
    dead = sum(1 for s in statuses if s.state == "dead")

    lines = [
        "# Estado de canales",
        "",
        f"Última revisión UTC: `{now}`",
        "",
        f"- Canales totales: **{total}**",
        f"- Canales vivos: **{alive}**",
        f"- Canales inestables: **{unstable}**",
        f"- Canales muertos: **{dead}**",
        "",
        "| Canal | Grupo | País | Estado | Código | Error |",
        "|---|---|---|---|---|---|",
    ]
    for s in statuses:
        if s.state == "alive":
            estado = "✅ Vivo"
        elif s.state == "unstable":
            estado = "⚠️ Inestable"
        else:
            estado = "❌ Muerto"
        codigo = s.status_code if s.status_code is not None else "-"
        error = s.error or ""
        lines.append(f"| {s.name} | {s.group} | {s.country} | {estado} | {codigo} | {error} |")

    return "\n".join(lines) + "\n"


def write_outputs(statuses: list[ChannelStatus], public_dir: Path = PUBLIC_DIR) -> bool:
    public_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = public_dir / "playlist.m3u"
    fallback_used = False
    new_playlist = build_m3u(statuses)

    # Preserve the last known good playlist during a temporary upstream collapse.
    if has_playable_channels(statuses) or not playlist_path.exists():
        playlist_path.write_text(new_playlist, encoding="utf-8")
    else:
        previous_playlist = playlist_path.read_text(encoding="utf-8", errors="ignore")
        if previous_playlist.strip() and previous_playlist.strip() != "#EXTM3U":
            fallback_used = True
        else:
            playlist_path.write_text(new_playlist, encoding="utf-8")

    (public_dir / "status.json").write_text(build_status_json(statuses), encoding="utf-8")
    (public_dir / "status.md").write_text(build_status_markdown(statuses), encoding="utf-8")
    return fallback_used


def print_summary(statuses: list[ChannelStatus]) -> None:
    total = len(statuses)
    alive = sum(1 for s in statuses if s.state == "alive")
    unstable = sum(1 for s in statuses if s.state == "unstable")
    dead = sum(1 for s in statuses if s.state == "dead")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("=" * 50)
    print("Resumen de revision de canales")
    print("=" * 50)
    print(f"Fecha/hora (UTC): {now}")
    print(f"Total revisados:   {total}")
    print(f"Vivos:             {alive}")
    print(f"Inestables:        {unstable}")
    print(f"Muertos:           {dead}")
    print("=" * 50)


async def run(sources_path: Path, public_dir: Path, config_path: Path) -> list[ChannelStatus]:
    config = load_config(config_path)
    channels = load_channels(sources_path)

    if not channels:
        print("[WARN] No hay canales validos en sources/channels.json")
        statuses: list[ChannelStatus] = []
    else:
        statuses = await check_all_channels(channels, config)
        statuses = regroup_statuses(statuses)
        statuses = sort_statuses(
            statuses,
            list(config["sort_by"]),
            group_order=list(config.get("group_order", [])),
            priority_channels=list(config.get("priority_channels", [])),
        )

    fallback_used = write_outputs(statuses, public_dir)
    if fallback_used:
        print("[WARN] Se conservo la ultima playlist valida por una caida masiva del upstream.")
    print_summary(statuses)
    return statuses


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Genera playlist.m3u a partir de canales legales/publicos.")
    parser.add_argument("--sources", type=Path, default=SOURCES_FILE, help="Ruta a channels.json")
    parser.add_argument("--public-dir", type=Path, default=PUBLIC_DIR, help="Carpeta de salida")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Ruta a config.json")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args.sources, args.public_dir, args.config))
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Error inesperado: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
