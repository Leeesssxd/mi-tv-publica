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
import time
import random
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

DEFAULT_CONFIG: dict[str, Any] = {
    "timeout_seconds": 10,
    "max_concurrency": 120,
    "validation_timeout_seconds": 12,
    "validation_connect_timeout_seconds": 5,
    "validation_sock_read_timeout_seconds": 8,
    "validation_max_concurrency": 48,
    "validation_candidate_limit": 420,
    "validation_group_buffer_multiplier": 1.35,
    "validation_group_floor": 12,
    "validation_retry_attempts": 2,
    "validation_retry_backoff_base_seconds": 0.35,
    "validation_jitter_min_seconds": 0.05,
    "validation_jitter_max_seconds": 0.2,
    "user_agent": "MiTVPublicaBot/1.0 (+https://github.com)",
    "accept_language": "es-MX,es;q=0.9,en;q=0.6",
    "sort_by": ["group", "name"],
    "group_order": [],
    "priority_channels": [],
    "target_playlist_size": 1200,
    "target_group_quotas": {
        "Familia y TV Abierta": 300,
        "Peliculas - Cine": 260,
        "Peliculas - Drama y Series": 180,
        "Deportes": 180,
        "Noticias": 110,
        "Entretenimiento": 110,
        "Otros": 60,
    },
    "retry_attempts": 3,
    "retry_backoff_base_seconds": 1.0,
    "jitter_min_seconds": 0.5,
    "jitter_max_seconds": 1.5,
    "status_cache_ttl_seconds": 259200,
    "head_first": False,
    "ttfb_limit_ms": 2500,
    "probe_first_segment": False,
    "vlc_network_caching_ms": 2000,
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
STATUS_CACHE_FILE = ROOT_DIR / ".cache" / "delta_validation" / "status_cache.json"
VOD_PLAYLIST_FILE = PUBLIC_DIR / "vod_playlist.m3u"
VOD_STATUS_FILE = PUBLIC_DIR / "vod_status.json"
VOD_STATUS_MD_FILE = PUBLIC_DIR / "vod_status.md"
VOD_WEB_LINKS_FILE = PUBLIC_DIR / "vod_browser_links.txt"

PLAYABLE_CONTENT_HINTS = (
    "mpegurl",
    "video/",
    "audio/",
    "octet-stream",
    "mp2t",
)
DIRECT_VOD_EXTENSIONS = (".m3u8", ".mp4", ".m4v", ".mpd", ".ts", ".webm", ".aac", ".mp3")

RETRIABLE_STATUS_CODES = {429, 503}
QUALITY_PATTERN = re.compile(r"(\d{3,4})p", re.IGNORECASE)
DEFAULT_PRIORITY_ALIASES: dict[str, tuple[str, ...]] = {
    "azteca uno": ("azteca uno", "azteca 1"),
    "canal 5": ("canal 5",),
    "azteca 7": ("azteca 7", "azteca siete"),
    "las estrellas": ("las estrellas", "canal de las estrellas"),
    "tudn": ("tudn",),
    "vix": ("vix", "vix premium", "vix deportes"),
    "dsports": ("dsports", "dsport", "d sports", "directv sports", "dsportplus", "dsport plus"),
    "d sports": ("d sports", "dsports", "dsport", "directv sports", "dsportplus", "dsport plus"),
    "s sports": ("s sports",),
    "canal 7 esports": ("canal 7 esports", "canal 7 e-sports", "esports"),
}
CANONICAL_DISPLAY_BY_TVG_ID: dict[str, str] = {
    "canal5.mx@sd": "Canal 5 Televisa",
    "canal5.mx": "Canal 5 Televisa",
    "azteca7.mx": "Azteca 7",
    "aztecauno.mx": "Azteca Uno",
    "lasestrellas.mx@sd": "Las Estrellas",
    "lasestrellaslatinamerica.mx": "Las Estrellas",
    "tudn.mx": "TUDN",
}
DIAL_MASTER_GRID: dict[int, str] = {
    100: "BARKER_CHANNEL_HD", 101: "AZTECA_UNO_HD", 102: "LAS_ESTRELLAS_HD", 103: "IMAGEN_TV_HD",
    104: "CANAL_4_GDL_HD", 105: "CANAL_5_LOCAL_HD", 106: "CANAL_6_HD", 107: "AZTECA_7_HD",
    108: "MÁS_VISIÓN_HD", 109: "NU9VE_HD", 110: "QUIERO_TV_HD", 111: "ONCE_TV_HD",
    113: "CANAL_13_HD", 114: "CANAL_14_HD", 117: "JALISCO_TV_HD", 120: "TV_UNAM_SD",
    122: "CANAL_22_SD", 125: "CANAL_22.2_SD", 135: "APRENDE_+_SD", 140: "ADN_40_SD",
    141: "A+_SD", 144: "CANAL_44_UDG_HD", 145: "CANAL_DEL_CONGRESO_SD", 146: "JUSTICIA_TV_SD",
    150: "MEGANOTICIAS_MX_HD", 151: "MEGANOTICIAS_HD", 152: "AZTECA_UNO_DELAY_SD", 153: "TELEFÓRMULA_HD",
    154: "CNNE_HD", 155: "MILENIO_TV_HD", 156: "MVSTV_SD", 157: "EL_FINANCIERO_BLOOMBERG_HD",
    160: "MEGANOTICIAS_MX_DELAY_SD", 161: "CNN_HD", 162: "CNNI_HD", 163: "FOX_NEWS_HD",
    164: "BBC_NEWS_HD", 165: "DW_LATINOAMÉRICA_SD", 167: "TV5_MONDE_SD",
    202: "STAR_CHANNEL_HD", 204: "FX_HD", 206: "UNIVERSAL_TV_HD", 207: "AMC_HD",
    208: "TNT_NOVELAS_HD", 209: "AXN_HD", 210: "SONY_HD", 211: "TNT_SERIES_HD",
    212: "WARNER_CHANNEL_HD", 213: "ID_HD", 214: "TELEMUNDO_HD", 215: "A&E_HD",
    216: "E!_HD", 217: "ATRESERIES_HD", 218: "USA_HD", 219: "COMEDY_CENTRAL_HD",
    220: "CORAZÓN_HD", 223: "EL_GOURMET_HD", 224: "MAS_CHIC_SD", 225: "DISCOVERY_H&H_HD",
    226: "PASIONES_HD", 228: "LIFETIME_HD", 230: "SPACE_HD", 232: "ANTENA_3",
    233: "ADULT_SWIM_HD", 234: "AZTECA_INTERNACIONAL_HD", 235: "HOLA_TV_HD", 236: "TVE_HD",
    249: "NICK_JR_HD", 250: "CARTOON_NETWORK_HD", 251: "DISNEY_JR_HD", 252: "CARTOONITO_HD",
    256: "NICKELODEON_HD", 258: "DISCOVERY_KIDS_HD", 260: "DISNEY_CHANNEL_HD", 261: "TOONCAST_HD",
    262: "BABY_FIRST_HD", 263: "BABY_TV_SD", 267: "ONCE_NIÑOS_SD",
    270: "DISCOVERY_CHANNEL_HD", 271: "DISCOVERY_SCIENCE_HD", 272: "TLC", 273: "HGTV_HD",
    274: "FOOD_NETWORK_HD", 275: "DISCOVERY_TURBO_HD", 276: "DISCOVERY_THEATER_HD", 277: "HISTORY_2_HD",
    278: "HISTORY_CHANNEL_HD", 279: "DISCOVERY_WORLD_HD", 280: "NAT_GEO_HD", 282: "ANIMAL_PLANET_HD",
    290: "MARIAVISION_SD", 292: "EWTN_SD", 293: "ESNE_SD", 294: "ENLACE_SD",
    297: "FILM_&_ARTS_HD", 301: "FOX", 302: "ESPN_HD", 303: "TVC_DEPORTES_HD",
    304: "ESPN_2_HD", 305: "TVC_DEPORTES_2_HD", 306: "ESPN_3_HD", 308: "ESPN_4_HD",
    311: "NBA_HD", 312: "NFL_HD", 313: "GOLF_CHANNEL_HD", 317: "MEGA_SPORTS_1_HD",
    320: "AYM_SPORTS_HD", 321: "LAS_HD", 322: "AZTECA_DEPORTES_NETWORK_HD", 323: "PX_SPORTS_HD",
    324: "CLARO_SPORTS_HD", 329: "WWB_SD",
    401: "CINEMA_PLATINO_DELAY_SD", 402: "CINEMA_PLATINO_HD", 403: "PANICO_DELAY_SD", 404: "PANICO_HD",
    405: "SONY_MOVIES", 406: "EUROPA_EUROPA_HD", 408: "STUDIO_UNIVERSAL_HD", 409: "CINEMAX_HD",
    410: "TNT_HD", 415: "CINEMA_PLATINO_2_DELAY_SD", 416: "CINEMA_PLATINO_2_SD", 419: "CMC_DELAY_SD",
    420: "CMC_SD", 422: "EUROCHANNEL_SD", 424: "TCM_HD", 426: "CINE_LATINO_SD",
    428: "MULTICINEMA_HD", 430: "MULTIPREMIER_HD", 431: "CINECANAL_HD", 432: "CÍNEMA_HD",
    602: "VIDEO_ROLA_HD", 622: "CLIC_HD", 624: "HTV_HD", 626: "BEAT_BOX_SD",
    628: "VR_PLUS_HD", 632: "EXA_TV_HD", 1405: "SONY_MOVIES_ALT",
}
REQUESTED_CATALOG_ORDER = [DIAL_MASTER_GRID[dial] for dial in sorted(DIAL_MASTER_GRID)]
CATALOG_ORDER_ALIASES: dict[str, tuple[str, ...]] = {
    "barker channel": ("barker channel", "conecta", "conecta tv", "básico plus", "basico plus"),
    "conecta": ("conecta tv",),
    "azteca uno hd": ("azteca uno",),
    "las estrellas hd": ("las estrellas",),
    "imagen tv hd": ("imagen tv+", "imagen tv"),
    "canal 4 gdl hd": ("tv cuatro 4.1", "canal 4 guadalajara"),
    "canal 5 local hd": ("canal 5 televisa", "canal 5 hd", "canal 5", "canal 5 (1080p)", "canal 5 (720p)"),
    "canal 6 hd": ("canal 6 cdmx",),
    "azteca 7 hd": ("azteca 7", "azteca siete"),
    "tudn hd": ("tudn",),
    "vix deportes hd": ("vix deportes", "vix sports", "vix"),
    "vix premium hd": ("vix premium", "vix"),
    "fifa plus hd": ("fifa+", "fifa plus"),
    "dsports hd": ("dsports", "d sports", "directv sports"),
    "dsports 2 hd": ("dsports 2", "d sports 2"),
    "dsports plus hd": ("dsports plus", "d sports plus", "dsportplus", "dsport plus"),
    "fox sports hd": ("fox sports",),
    "once tv hd": ("once méxico", "once mexico"),
    "canal 13 hd": ("canal 13 michoacán", "canal 13 michoacan", "canal 13"),
    "canal 14 hd": ("canal 14",),
    "jalisco tv hd": ("jalisco tv",),
    "tv unam sd": ("tv unam",),
    "canal 22 sd": ("canal 22 nacional", "canal 22 mexico", "canal 22"),
    "adn 40 sd": ("adn 40",),
    "canal 44 udg hd": ("udg tv canal 44", "canal 44"),
    "canal del congreso sd": ("canal del congreso", "canal parlamento del congreso"),
    "justicia tv sd": ("justicia tv",),
    "telefórmula hd": ("teleformula", "telefórmula"),
    "milenio tv hd": ("milenio",),
    "mvstv sd": ("mvs tv",),
    "azteca internacional hd": ("azteca internacional",),
    "nick jr hd": ("nick jr",),
    "disney jr hd": ("disney jr",),
    "disney channel hd": ("disney channel",),
    "mariavision sd": ("maría visión", "maria visión", "mariavision"),
    "film & arts hd": ("film&arts", "film & arts"),
    "tvc deportes hd": ("tv cuatro 4.3",),
    "tvc deportes 2 hd": ("tv cuatro 4.3",),
    "aym sports hd": ("aym sports",),
    "claro sports hd": ("claro sports",),
    "panico hd": ("panico",),
    "cinemax hd": ("cinemax",),
    "tnt hd": ("tnt hd",),
    "cinecanal hd": ("cinecanal",),
    "clic hd": ("clic",),
    "htv hd": ("htv",),
    "exa tv hd": ("exa tv",),
    "sony movies alt": ("sony movies",),
}
WORLD_CUP_CHANNEL_ALIASES = (
    "tudn",
    "vix",
    "dsports",
    "d sports",
    "claro sports",
    "aym sports",
    "itv deportes",
    "tyc sports",
    "deportv",
    "tvc deportes",
    "fox sports",
    "espn",
)
HARD_PRIORITY_BLOCK = [
    "Azteca Uno",
    "Las Estrellas",
    "Imagen TV",
    "Canal 5 Televisa",
    "Azteca 7",
    "TUDN",
    "ViX",
    "DSPORTS",
    "FIFA+",
    "Claro Sports",
    "FOX Sports",
    "ESPN",
]
HARD_PRIORITY_ALIASES: dict[str, tuple[str, ...]] = {
    "Azteca Uno": ("azteca uno", "azteca 1"),
    "Las Estrellas": ("las estrellas", "canal de las estrellas", "las estrellas hd"),
    "Imagen TV": ("imagen tv", "imagen tv+"),
    "Canal 5 Televisa": ("canal 5 televisa", "canal 5"),
    "Azteca 7": ("azteca 7", "azteca siete"),
    "TUDN": ("tudn",),
    "ViX": ("vix", "vix premium", "vix deportes"),
    "DSPORTS": ("dsports", "d sports", "directv sports", "dsportplus", "dsport plus"),
    "FIFA+": ("fifa+", "fifa plus"),
    "Claro Sports": ("claro sports",),
    "FOX Sports": ("fox sports",),
    "ESPN": ("espn",),
}
REGIONAL_VARIANT_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "Canal 5 Televisa": ("cozumel", "tv cozumel", "xej", "juárez", "juarez"),
}
HARD_PRIORITY_EXCLUSION_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "Azteca Uno": (
        re.compile(r"\bregional\b", re.IGNORECASE),
        re.compile(r"\bxhg\w*\b", re.IGNORECASE),
    ),
    "Las Estrellas": (
        re.compile(r"\bregional\b", re.IGNORECASE),
        re.compile(r"\blocal\b", re.IGNORECASE),
        re.compile(r"\bxhg\w*\b", re.IGNORECASE),
    ),
    "Imagen TV": (
        re.compile(r"\bregional\b", re.IGNORECASE),
        re.compile(r"\blocal\b", re.IGNORECASE),
    ),
    "Canal 5 Televisa": (
        re.compile(r"\bcozumel\b", re.IGNORECASE),
        re.compile(r"\bregional\b", re.IGNORECASE),
        re.compile(r"\blocal\b", re.IGNORECASE),
        re.compile(r"\btv cozumel\b", re.IGNORECASE),
        re.compile(r"\bxhg\w*\b", re.IGNORECASE),
    ),
    "Azteca 7": (
        re.compile(r"\bregional\b", re.IGNORECASE),
        re.compile(r"\blocal\b", re.IGNORECASE),
    ),
}

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
    "dsport",
    "dsports",
    "d sports",
    "directv sports",
    "fox sports",
    "espn",
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
DOCUMENTARY_PATTERNS = (
    "discovery",
    "history",
    "nat geo",
    "national geographic",
    "animal planet",
    "food network",
    "hgtv",
    "tlc",
    "film & arts",
    "film&arts",
    "dw latinoamérica",
    "dw latinoamerica",
    "tv5 monde",
    "docu",
    "science",
)
LOW_PRIORITY_LANGUAGE_PATTERNS = (
    "english",
    "uk ",
    "us entertainment",
    "mandarin",
    "chinese",
    "china",
    "russian",
    "rusia",
    "рус",
    "turk",
    "turkish",
    "hindi",
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
GROUP_DOCUMENTARIES = "Documentales y Cultura"
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
    GROUP_DOCUMENTARIES,
    GROUP_SPORTS_PUBLIC,
    GROUP_SPORTS,
    GROUP_ENTERTAINMENT,
    GROUP_OTHER,
}


@dataclass
class Channel:
    name: str
    url: str
    backup_urls: list[str] = field(default_factory=list)
    group: str = "General"
    country: str = ""
    logo: str = ""
    tvg_id: str = ""

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Channel":
        name = (raw.get("name") or "").strip()
        raw_url = raw.get("url")
        primary_url = ""
        backup_urls: list[str] = []

        if isinstance(raw_url, list):
            normalized_urls = [str(item).strip() for item in raw_url if str(item).strip()]
            if normalized_urls:
                primary_url = normalized_urls[0]
                backup_urls.extend(normalized_urls[1:])
        else:
            primary_url = str(raw_url or "").strip()

        raw_backup = raw.get("backup_url")
        if isinstance(raw_backup, list):
            backup_urls.extend(str(item).strip() for item in raw_backup if str(item).strip())
        else:
            backup_candidate = str(raw_backup or "").strip()
            if backup_candidate:
                backup_urls.append(backup_candidate)

        deduped_backups: list[str] = []
        seen_backups: set[str] = set()
        for candidate in backup_urls:
            normalized_candidate = candidate.casefold()
            if not candidate or normalized_candidate == primary_url.casefold() or normalized_candidate in seen_backups:
                continue
            seen_backups.add(normalized_candidate)
            deduped_backups.append(candidate)

        if not name:
            raise ValueError(f"Canal sin 'name': {raw}")
        if not primary_url:
            raise ValueError(f"Canal sin 'url': {raw}")
        if not (primary_url.startswith("http://") or primary_url.startswith("https://")):
            raise ValueError(f"URL invalida (debe empezar con http/https): {primary_url}")
        for backup_url in deduped_backups:
            if not (backup_url.startswith("http://") or backup_url.startswith("https://")):
                raise ValueError(f"backup_url invalida (debe empezar con http/https): {backup_url}")

        return Channel(
            name=_canonical_display_name(name, str(raw.get("tvg_id") or "").strip()),
            url=primary_url,
            backup_urls=deduped_backups,
            group=(raw.get("group") or "General").strip() or "General",
            country=(raw.get("country") or "").strip(),
            logo=(raw.get("logo") or "").strip(),
            tvg_id=(raw.get("tvg_id") or "").strip(),
        )


def _is_non_playable_catalog_entry(raw: dict[str, Any]) -> bool:
    group = str(raw.get("group") or "").strip()
    availability = str(raw.get("availability") or "").strip().casefold()
    url = str(raw.get("url") or "").strip().casefold()

    if group != "Mi Catálogo Cloud":
        return False
    if availability == "metadata_only":
        return True
    if availability == "templated_routing" and "localhost" in url:
        return True
    return False



@dataclass
class ChannelStatus:
    name: str
    group: str
    country: str
    url: str
    backup_urls: list[str]
    logo: str
    tvg_id: str
    alive: bool
    status_code: int | None
    error: str | None
    state: str = "dead"
    ttfb_ms: int | None = None
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VodStatus:
    name: str
    group: str
    url: str
    tvg_id: str
    playable_in_vlc: bool
    delivery: str
    status_code: int | None
    content_type: str
    error: str | None
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
                custom_routing_rules = user_config.get("custom_routing_rules")
                if custom_routing_rules is None:
                    config["custom_routing_rules"] = {}
                elif isinstance(custom_routing_rules, dict):
                    config["custom_routing_rules"] = custom_routing_rules
                else:
                    print("[WARN] custom_routing_rules invalido, se ignora y se usa {}")
                    config["custom_routing_rules"] = {}
        except json.JSONDecodeError as exc:
            print(f"[WARN] config.json invalido, usando valores por defecto: {exc}")
    else:
        config["custom_routing_rules"] = {}
    if "custom_routing_rules" not in config:
        config["custom_routing_rules"] = {}
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
            if _is_non_playable_catalog_entry(raw):
                continue
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


def load_cloud_catalog_items(sources_path: Path = SOURCES_FILE) -> list[dict[str, Any]]:
    if not sources_path.exists():
        raise FileNotFoundError(f"No se encontro {sources_path}")

    payload = json.loads(sources_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return []

    cloud_catalog = payload.get("cloud_catalog")
    if not isinstance(cloud_catalog, dict):
        return []

    items = cloud_catalog.get("items")
    if not isinstance(items, list):
        return []

    return [item for item in items if isinstance(item, dict)]


def _channel_signature(channel: Channel) -> str:
    urls = [channel.url.casefold(), *[backup.casefold() for backup in channel.backup_urls]]
    return "|".join(sorted(dict.fromkeys(urls)))


def _status_signature(status: ChannelStatus) -> str:
    urls = [status.url.casefold(), *[backup.casefold() for backup in status.backup_urls]]
    return "|".join(sorted(dict.fromkeys(urls)))


def _parse_checked_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_status_cache(cache_path: Path = STATUS_CACHE_FILE) -> dict[str, dict[str, Any]]:
    if not cache_path.exists():
        return {}

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    if not isinstance(payload, dict):
        return {}
    entries = payload.get("channels")
    if not isinstance(entries, list):
        return {}

    cached: dict[str, dict[str, Any]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip().casefold()
        if not url:
            continue
        cached[url] = raw
    return cached


def save_status_cache(statuses: list[ChannelStatus], cache_path: Path = STATUS_CACHE_FILE) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    serialized_channels: list[dict[str, Any]] = []
    for status in statuses:
        payload = status.to_dict()
        payload["signature"] = _status_signature(status)
        serialized_channels.append(payload)
    payload = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channels": serialized_channels,
    }
    cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _cached_status_is_fresh(
    channel: Channel,
    cached_entry: dict[str, Any] | None,
    ttl_seconds: int,
    now: datetime,
) -> bool:
    if not cached_entry:
        return False
    if str(cached_entry.get("state") or "") not in {"alive", "unstable"}:
        return False
    if str(cached_entry.get("signature") or "") != _channel_signature(channel):
        return False
    checked_at = _parse_checked_at(str(cached_entry.get("checked_at") or ""))
    if checked_at is None:
        return False
    return (now - checked_at).total_seconds() <= ttl_seconds


def _status_from_cache(channel: Channel, cached_entry: dict[str, Any]) -> ChannelStatus:
    return ChannelStatus(
        name=channel.name,
        group=channel.group,
        country=channel.country,
        url=str(cached_entry.get("url") or channel.url),
        backup_urls=list(cached_entry.get("backup_urls") or channel.backup_urls),
        logo=channel.logo,
        tvg_id=channel.tvg_id,
        alive=bool(cached_entry.get("alive")),
        status_code=cached_entry.get("status_code"),
        error=cached_entry.get("error"),
        state=str(cached_entry.get("state") or "dead"),
        checked_at=str(cached_entry.get("checked_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )


def _looks_playable(content_type: str) -> bool:
    content_type = (content_type or "").lower()
    if not content_type:
        return True
    return any(hint in content_type for hint in PLAYABLE_CONTENT_HINTS)


def _looks_like_direct_media_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.casefold()
    return any(path.endswith(extension) for extension in DIRECT_VOD_EXTENSIONS)


def _classify_vod_transport(url: str, status_code: int | None, content_type: str) -> tuple[bool, str]:
    lowered_content_type = (content_type or "").casefold()
    if status_code is not None and not (200 <= status_code < 400):
        return False, "error"
    if _looks_like_direct_media_url(url) or _looks_playable(lowered_content_type):
        return True, "direct_media"
    if "text/html" in lowered_content_type:
        return False, "web_page"
    if lowered_content_type:
        return False, "unknown"
    return False, "unknown"


async def _sleep_with_jitter(config: dict[str, Any]) -> None:
    jitter_min = float(config.get("validation_jitter_min_seconds", config.get("jitter_min_seconds", 0.5)))
    jitter_max = float(config.get("validation_jitter_max_seconds", config.get("jitter_max_seconds", 1.5)))
    await asyncio.sleep(random.uniform(jitter_min, jitter_max))


def _validation_timeout(config: dict[str, Any]) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(
        total=float(config.get("validation_timeout_seconds", config.get("timeout_seconds", 10))),
        connect=float(config.get("validation_connect_timeout_seconds", 5)),
        sock_read=float(config.get("validation_sock_read_timeout_seconds", 8)),
    )


def _validation_max_concurrency(config: dict[str, Any]) -> int:
    return max(1, int(config.get("validation_max_concurrency", config.get("max_concurrency", 120))))


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
    active_url: str | None = None,
    state: str,
    status_code: int | None,
    error: str | None,
    ttfb_ms: int | None = None,
) -> ChannelStatus:
    return ChannelStatus(
        name=channel.name,
        group=channel.group,
        country=channel.country,
        url=active_url or channel.url,
        backup_urls=list(channel.backup_urls),
        logo=channel.logo,
        tvg_id=channel.tvg_id,
        alive=state == "alive",
        state=state,
        status_code=status_code,
        error=error,
        ttfb_ms=ttfb_ms,
    )


async def _request_channel_candidate(
    session: aiohttp.ClientSession,
    url: str,
    config: dict[str, Any],
) -> tuple[int | None, str, str | None, int | None]:
    attempts = int(config.get("validation_retry_attempts", config.get("retry_attempts", 3)))
    backoff_base = float(
        config.get("validation_retry_backoff_base_seconds", config.get("retry_backoff_base_seconds", 1.0))
    )
    preferred_methods = ["GET", "HEAD"] if bool(config.get("head_first", True)) else ["GET"]
    retryable_method_fallback = {400, 403, 405, 406, 500, 501}
    ttfb_limit_ms = int(config.get("ttfb_limit_ms", 1500))

    for attempt in range(1, attempts + 1):
        await _sleep_with_jitter(config)
        try:
            for method in preferred_methods:
                request = session.head if method == "HEAD" else session.get
                start = time.perf_counter()
                async with request(
                    url,
                    allow_redirects=True,
                    ssl=False,
                ) as response:
                    content_type = response.headers.get("Content-Type", "")
                    ttfb_ms = int((time.perf_counter() - start) * 1000)
                    if method == "GET":
                        await response.content.read(1)
                    if response.status in RETRIABLE_STATUS_CODES and attempt < attempts:
                        await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                        break
                    if method == "HEAD" and response.status in retryable_method_fallback:
                        continue
                    if ttfb_ms > ttfb_limit_ms:
                        return response.status, content_type, f"TTFB alto ({ttfb_ms} ms)", ttfb_ms
                    return response.status, content_type, None, ttfb_ms
            else:
                continue
        except asyncio.TimeoutError:
            return None, "", "Timeout", None
        except aiohttp.ClientError as exc:
            return None, "", str(exc), None
        except Exception as exc:  # noqa: BLE001
            return None, "", f"Error inesperado: {exc}", None

    return None, "", "Agotado tras reintentos", None


async def _probe_first_segment(
    session: aiohttp.ClientSession,
    manifest_url: str,
    config: dict[str, Any],
) -> str | None:
    if not bool(config.get("probe_first_segment", True)):
        return None
    if ".m3u8" not in manifest_url.casefold():
        return None

    try:
        async with session.get(manifest_url, allow_redirects=True, ssl=False) as response:
            if response.status < 200 or response.status >= 400:
                return None
            manifest_text = await response.text(errors="ignore")
        segment_url = ""
        for line in manifest_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            segment_url = urljoin(str(response.url), stripped)
            break
        if not segment_url:
            return None
        start = time.perf_counter()
        async with session.get(segment_url, allow_redirects=True, ssl=False) as segment_response:
            await segment_response.content.read(1)
            if segment_response.status < 200 or segment_response.status >= 400:
                return f"Segmento inicial inválido (HTTP {segment_response.status})"
        segment_ttfb_ms = int((time.perf_counter() - start) * 1000)
        if segment_ttfb_ms > int(config.get("ttfb_limit_ms", 1500)):
            return f"Primer segmento lento ({segment_ttfb_ms} ms)"
    except Exception as exc:  # noqa: BLE001
        return f"Fallo al probar primer segmento: {exc}"
    return None


async def _request_channel(
    session: aiohttp.ClientSession,
    channel: Channel,
    config: dict[str, Any],
) -> ChannelStatus:
    candidate_urls = [channel.url, *channel.backup_urls]
    last_status_code: int | None = None
    last_error: str | None = None

    for index, candidate_url in enumerate(candidate_urls):
        status_code, content_type, error, ttfb_ms = await _request_channel_candidate(session, candidate_url, config)
        last_status_code = status_code
        last_error = error

        if status_code is None:
            if index < len(candidate_urls) - 1:
                continue
            return _make_status(
                channel,
                active_url=candidate_url,
                state="dead",
                status_code=None,
                error=error,
                ttfb_ms=ttfb_ms,
            )

        if 200 <= status_code < 400:
            if error:
                if index < len(candidate_urls) - 1:
                    continue
                return _make_status(
                    channel,
                    active_url=candidate_url,
                    state="dead",
                    status_code=status_code,
                    error=error,
                    ttfb_ms=ttfb_ms,
                )
            segment_probe_error = await _probe_first_segment(session, candidate_url, config)
            if segment_probe_error:
                if index < len(candidate_urls) - 1:
                    continue
                return _make_status(
                    channel,
                    active_url=candidate_url,
                    state="dead",
                    status_code=status_code,
                    error=segment_probe_error,
                    ttfb_ms=ttfb_ms,
                )
            if _looks_playable(content_type):
                return _make_status(
                    channel,
                    active_url=candidate_url,
                    state="alive",
                    status_code=status_code,
                    error=None,
                    ttfb_ms=ttfb_ms,
                )
            return _make_status(
                channel,
                active_url=candidate_url,
                state="unstable",
                status_code=status_code,
                error=f"Handshake correcto pero contenido inestable ({content_type or 'sin content-type'})",
                ttfb_ms=ttfb_ms,
            )

        if index < len(candidate_urls) - 1:
            continue

        return _make_status(
            channel,
            active_url=candidate_url,
            state="dead",
            status_code=status_code,
            error=f"HTTP {status_code}",
            ttfb_ms=ttfb_ms,
        )

    return _make_status(channel, state="dead", status_code=last_status_code, error=last_error)


async def check_channel(
    session: aiohttp.ClientSession,
    channel: Channel,
    semaphore: asyncio.Semaphore,
    config: dict[str, Any],
) -> ChannelStatus:
    async with semaphore:
        return await _request_channel(session, channel, config)


async def check_all_channels(
    channels: list[Channel],
    config: dict[str, Any],
    *,
    cache_path: Path = STATUS_CACHE_FILE,
) -> list[ChannelStatus]:
    now = datetime.now(timezone.utc)
    ttl_seconds = int(config.get("status_cache_ttl_seconds", 86400))
    cached_statuses = load_status_cache(cache_path)
    reusable_statuses: dict[str, ChannelStatus] = {}
    channels_to_check: list[Channel] = []

    for channel in channels:
        cached_entry = cached_statuses.get(channel.url.casefold())
        if _cached_status_is_fresh(channel, cached_entry, ttl_seconds, now):
            reusable_statuses[channel.url.casefold()] = _status_from_cache(channel, cached_entry)
            continue
        channels_to_check.append(channel)

    if not channels_to_check:
        return [reusable_statuses[channel.url.casefold()] for channel in channels if channel.url.casefold() in reusable_statuses]

    timeout = _validation_timeout(config)
    headers = _build_headers(config)
    semaphore = asyncio.Semaphore(_validation_max_concurrency(config))

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        tasks = [check_channel(session, ch, semaphore, config) for ch in channels_to_check]
        checked_statuses = await asyncio.gather(*tasks)

    checked_by_url = {
        channel.url.casefold(): status
        for channel, status in zip(channels_to_check, checked_statuses, strict=False)
    }
    combined: list[ChannelStatus] = []
    for channel in channels:
        normalized_url = channel.url.casefold()
        if normalized_url in reusable_statuses:
            combined.append(reusable_statuses[normalized_url])
            continue
        status = checked_by_url.get(normalized_url)
        if status is not None:
            combined.append(status)
    return combined


async def _request_vod_item(
    session: aiohttp.ClientSession,
    item: dict[str, Any],
    config: dict[str, Any],
) -> VodStatus:
    name = str(item.get("name") or "Sin nombre").strip() or "Sin nombre"
    url = str(item.get("url") or "").strip()
    tvg_id = str(item.get("tvg_id") or "").strip()
    group = str(item.get("group") or "Mi Catálogo Cloud").strip() or "Mi Catálogo Cloud"

    if not url:
        return VodStatus(
            name=name,
            group=group,
            url=url,
            tvg_id=tvg_id,
            playable_in_vlc=False,
            delivery="error",
            status_code=None,
            content_type="",
            error="URL vacia",
        )

    attempts = int(config.get("retry_attempts", 3))
    backoff_base = float(config.get("retry_backoff_base_seconds", 1.0))

    for attempt in range(1, attempts + 1):
        await _sleep_with_jitter(config)
        try:
            async with session.get(url, allow_redirects=True, ssl=False) as response:
                content_type = response.headers.get("Content-Type", "")
                if response.status in RETRIABLE_STATUS_CODES and attempt < attempts:
                    await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                playable, delivery = _classify_vod_transport(url, response.status, content_type)
                error = None if playable else f"No es stream directo para VLC ({content_type or 'sin content-type'})"
                return VodStatus(
                    name=name,
                    group=group,
                    url=url,
                    tvg_id=tvg_id,
                    playable_in_vlc=playable,
                    delivery=delivery,
                    status_code=response.status,
                    content_type=content_type,
                    error=error,
                )
        except asyncio.TimeoutError:
            if attempt < attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            return VodStatus(
                name=name,
                group=group,
                url=url,
                tvg_id=tvg_id,
                playable_in_vlc=False,
                delivery="error",
                status_code=None,
                content_type="",
                error="Timeout",
            )
        except aiohttp.ClientError as exc:
            if attempt < attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            return VodStatus(
                name=name,
                group=group,
                url=url,
                tvg_id=tvg_id,
                playable_in_vlc=False,
                delivery="error",
                status_code=None,
                content_type="",
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return VodStatus(
                name=name,
                group=group,
                url=url,
                tvg_id=tvg_id,
                playable_in_vlc=False,
                delivery="error",
                status_code=None,
                content_type="",
                error=f"Error inesperado: {exc}",
            )

    return VodStatus(
        name=name,
        group=group,
        url=url,
        tvg_id=tvg_id,
        playable_in_vlc=False,
        delivery="error",
        status_code=None,
        content_type="",
        error="Agotado tras reintentos",
    )


async def check_vod_item(
    session: aiohttp.ClientSession,
    item: dict[str, Any],
    semaphore: asyncio.Semaphore,
    config: dict[str, Any],
) -> VodStatus:
    async with semaphore:
        return await _request_vod_item(session, item, config)


async def check_all_vod_items(items: list[dict[str, Any]], config: dict[str, Any]) -> list[VodStatus]:
    if not items:
        return []

    timeout = _validation_timeout(config)
    headers = _build_headers(config)
    semaphore = asyncio.Semaphore(_validation_max_concurrency(config))

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        tasks = [check_vod_item(session, item, semaphore, config) for item in items]
        return await asyncio.gather(*tasks)


def _normalize_name(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _canonical_aliases(canonical_name: str) -> tuple[str, ...]:
    return HARD_PRIORITY_ALIASES.get(canonical_name, (_normalize_name(canonical_name),))


def _matches_canonical_alias(normalized_name: str, alias: str) -> bool:
    pattern = re.compile(rf"^(?:{re.escape(alias)})(?:$|\s|\()", re.IGNORECASE)
    return bool(pattern.search(normalized_name))


def _is_excluded_priority_variant(canonical_name: str, normalized_name: str) -> bool:
    legacy_exclusions = REGIONAL_VARIANT_EXCLUSIONS.get(canonical_name, ())
    if any(token in normalized_name for token in legacy_exclusions):
        return True
    return any(pattern.search(normalized_name) for pattern in HARD_PRIORITY_EXCLUSION_PATTERNS.get(canonical_name, ()))


def _quality_score(name: str) -> int:
    match = QUALITY_PATTERN.search(name or "")
    if not match:
        normalized = _normalize_name(name)
        if "uhd" in normalized or "4k" in normalized:
            return 2160
        if "fhd" in normalized or "full hd" in normalized:
            return 1080
        if "hd" in normalized:
            return 720
        if "sd" in normalized:
            return 480
        return 0
    return int(match.group(1))


def _canonical_display_name(name: str, tvg_id: str) -> str:
    normalized_tvg_id = _normalize_name(tvg_id)
    if normalized_tvg_id in CANONICAL_DISPLAY_BY_TVG_ID:
        return CANONICAL_DISPLAY_BY_TVG_ID[normalized_tvg_id]
    return name


def _normalize_catalog_label(label: str) -> str:
    normalized = _normalize_name(
        label.replace("_", " ").replace("&amp;", "&").replace("&", " and ").replace("+", " plus ")
    )
    normalized = re.sub(r"\b(hd|sd)\b", " ", normalized)
    normalized = re.sub(r"\bdelay\b", " ", normalized)
    normalized = re.sub(r"\blocal\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _catalog_aliases(label: str) -> tuple[str, ...]:
    normalized = _normalize_catalog_label(label)
    explicit = CATALOG_ORDER_ALIASES.get(normalized)
    if explicit:
        return explicit
    return (normalized,)


def _dial_label_for_status(status: ChannelStatus) -> str | None:
    normalized_name = _normalize_name(_canonical_display_name(status.name, status.tvg_id))
    normalized_tvg_id = _normalize_name(status.tvg_id)

    for label in REQUESTED_CATALOG_ORDER:
        normalized_label = _normalize_catalog_label(label)
        aliases = _catalog_aliases(label)
        if normalized_tvg_id and normalized_tvg_id == normalized_label:
            return label
        if any(alias and _matches_canonical_alias(normalized_name, alias) for alias in aliases):
            return label
    return None


def _dial_rank(status: ChannelStatus) -> int:
    label = _dial_label_for_status(status)
    if label is None:
        return len(REQUESTED_CATALOG_ORDER)
    return REQUESTED_CATALOG_ORDER.index(label)


def _status_dial_preference_key(status: ChannelStatus) -> tuple[int, ...]:
    return (
        _state_rank(status),
        _country_rank(status),
        status.ttfb_ms if status.ttfb_ms is not None else 999999,
        -_quality_score(status.name),
        _normalize_name(status.name),
        status.url.casefold(),
    )


def _select_dial_matches(
    statuses: list[ChannelStatus],
) -> tuple[dict[str, ChannelStatus], dict[str, list[ChannelStatus]], list[ChannelStatus]]:
    candidates_by_label: dict[str, list[ChannelStatus]] = {}
    remainder: list[ChannelStatus] = []

    for status in statuses:
        label = _dial_label_for_status(status)
        if label is None:
            remainder.append(status)
            continue
        candidates_by_label.setdefault(label, []).append(status)

    best_by_label: dict[str, ChannelStatus] = {}
    for label, candidates in candidates_by_label.items():
        best_by_label[label] = min(candidates, key=_status_dial_preference_key)

    return best_by_label, candidates_by_label, remainder


def _catalog_order_rank(status: ChannelStatus, catalog_order: list[str]) -> tuple[int, int]:
    dial_label = _dial_label_for_status(status)
    if dial_label is not None and dial_label in catalog_order:
        return (catalog_order.index(dial_label), 1)

    normalized_name = _normalize_name(status.name)
    world_cup_insert_at = next(
        (index for index, label in enumerate(catalog_order) if _normalize_catalog_label(label) == "azteca 7"),
        -1,
    )

    if world_cup_insert_at >= 0 and any(alias in normalized_name for alias in WORLD_CUP_CHANNEL_ALIASES):
        return (world_cup_insert_at + 1, 0)

    for index, label in enumerate(catalog_order):
        aliases = _catalog_aliases(label)
        if any(alias and alias in normalized_name for alias in aliases):
            return (index, 1)

    return (len(catalog_order) + 1, 1)


def _priority_exact_bonus(status: ChannelStatus, priority_channels: list[str]) -> int:
    normalized_name = _normalize_name(status.name)

    for pattern in priority_channels:
        normalized_pattern = _normalize_name(pattern)
        aliases = DEFAULT_PRIORITY_ALIASES.get(normalized_pattern, (normalized_pattern,))
        if any(
            alias and (
                normalized_name == alias
                or normalized_name.startswith(f"{alias} (")
                or normalized_name.startswith(f"{alias} hd")
                or normalized_name.startswith(f"{alias} televisa")
            )
            for alias in aliases
        ):
            return 0
    return 1


def _priority_rank(status: ChannelStatus, priority_channels: list[str]) -> int:
    normalized_name = _normalize_name(status.name)
    normalized_group = _normalize_name(status.group)

    for index, pattern in enumerate(priority_channels):
        normalized_pattern = _normalize_name(pattern)
        aliases = DEFAULT_PRIORITY_ALIASES.get(normalized_pattern, (normalized_pattern,))
        if any(
            alias and (alias in normalized_name or alias in normalized_group)
            for alias in aliases
        ):
            return index
    return len(priority_channels)


def _hard_priority_name(status: ChannelStatus) -> str | None:
    normalized_name = _normalize_name(_canonical_display_name(status.name, status.tvg_id))
    for canonical_name, aliases in HARD_PRIORITY_ALIASES.items():
        if any(alias and _matches_canonical_alias(normalized_name, alias) for alias in aliases):
            if _is_excluded_priority_variant(canonical_name, normalized_name):
                return None
            return canonical_name
    return None


def _hard_priority_rank(status: ChannelStatus) -> int:
    canonical_name = _hard_priority_name(status)
    if canonical_name is None:
        return len(HARD_PRIORITY_BLOCK)
    return HARD_PRIORITY_BLOCK.index(canonical_name)


def _group_rank(status: ChannelStatus, group_order: list[str]) -> int:
    normalized_group = _normalize_name(status.group)
    normalized_order = [_normalize_name(item) for item in group_order]
    try:
        return normalized_order.index(normalized_group)
    except ValueError:
        return len(group_order)


def _state_rank(status: ChannelStatus) -> int:
    if status.state == "alive":
        return 0
    if status.state == "unstable":
        return 1
    return 2


def _country_rank(status: ChannelStatus) -> int:
    normalized_country = _normalize_name(status.country)
    if normalized_country == "mx":
        return 0
    if normalized_country == "all":
        return 1
    return 2


def _language_tail_rank(status: ChannelStatus) -> int:
    haystack = _normalize_name(f"{status.name} {status.group} {status.country}")
    if any(pattern in haystack for pattern in LOW_PRIORITY_LANGUAGE_PATTERNS):
        return 1
    return 0


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
    if _matches_any(haystack, DOCUMENTARY_PATTERNS):
        return GROUP_DOCUMENTARIES
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


def _identity_name(name: str) -> str:
    normalized = _normalize_name(name)
    normalized = re.sub(r"\([^)]*\)", " ", normalized)
    normalized = re.sub(r"\b(uhd|fhd|hd|sd|4k|1080p|720p|480p|backup|respaldo|latam|latino|mx|us|usa|es)\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _status_identity_key(status: ChannelStatus) -> tuple[str, str]:
    normalized_name = _identity_name(_canonical_display_name(status.name, status.tvg_id))
    for canonical, aliases in DEFAULT_PRIORITY_ALIASES.items():
        if any(alias and alias in normalized_name for alias in aliases):
            normalized_name = canonical
            break
    if status.group == GROUP_FAMILIA and normalized_name:
        return ("family-name", normalized_name)
    if normalized_name:
        return ("name", f"{normalized_name}|{_normalize_name(status.group)}")
    if status.tvg_id.strip():
        return ("tvg", _normalize_name(status.tvg_id))
    return ("url", status.url.casefold())


def _status_preference_key(status: ChannelStatus, priority_channels: list[str]) -> tuple[int, ...]:
    normalized_name = _normalize_name(status.name)
    return (
        -1 if status.state == "alive" else 0,
        -1 if _normalize_name(status.country) == "mx" else 0,
        -1 if _priority_rank(status, priority_channels) < len(priority_channels) else 0,
        _priority_rank(status, priority_channels),
        status.ttfb_ms if status.ttfb_ms is not None else 999999,
        -_quality_score(status.name),
        0 if " hd" in normalized_name or normalized_name.endswith("hd") else 1,
        0 if "(" not in status.name else 1,
        0 if status.tvg_id else 1,
        0 if not _is_excluded_priority_variant(_hard_priority_name(status) or "", normalized_name) else 1,
        normalized_name,
        status.url.casefold(),
    )


def dedupe_statuses_by_identity(
    statuses: list[ChannelStatus],
    priority_channels: list[str] | None = None,
) -> list[ChannelStatus]:
    priorities = priority_channels or []
    best_by_identity: dict[tuple[str, str], ChannelStatus] = {}

    ordered_statuses = sorted(
        statuses,
        key=lambda status: (
            _status_identity_key(status),
            _status_preference_key(status, priorities),
            status.url.casefold(),
        ),
    )

    for status in ordered_statuses:
        identity = _status_identity_key(status)
        existing = best_by_identity.get(identity)
        if existing is None or _status_preference_key(status, priorities) < _status_preference_key(existing, priorities):
            best_by_identity[identity] = status
            existing = status
        if existing is not None and existing is best_by_identity[identity]:
            merged_backups = list(existing.backup_urls)
            seen_backups = {item.casefold() for item in merged_backups}
            for backup_url in status.backup_urls:
                if backup_url.casefold() not in seen_backups and backup_url.casefold() != existing.url.casefold():
                    seen_backups.add(backup_url.casefold())
                    merged_backups.append(backup_url)
            best_by_identity[identity] = replace(existing, backup_urls=merged_backups)

    return list(best_by_identity.values())


def purge_false_positive_variants(statuses: list[ChannelStatus]) -> list[ChannelStatus]:
    filtered: list[ChannelStatus] = []
    for status in statuses:
        canonical_name = _hard_priority_name(status)
        normalized_name = _normalize_name(status.name)
        if canonical_name is None and "canal 5" in normalized_name:
            if _is_excluded_priority_variant("Canal 5 Televisa", normalized_name):
                continue
        filtered.append(status)
    return filtered


def partition_hard_priority_block(
    statuses: list[ChannelStatus],
    *,
    dedupe_urls: bool = True,
) -> tuple[list[ChannelStatus], list[ChannelStatus]]:
    priority_candidates: dict[str, list[ChannelStatus]] = {}
    remainder: list[ChannelStatus] = []

    for status in statuses:
        canonical_name = _hard_priority_name(status)
        if canonical_name is None:
            remainder.append(status)
            continue
        priority_candidates.setdefault(canonical_name, []).append(status)

    ordered_top: list[ChannelStatus] = []
    spillover_priority: list[ChannelStatus] = []
    for canonical_name in HARD_PRIORITY_BLOCK:
        candidates = priority_candidates.get(canonical_name, [])
        if not candidates:
            continue
        best = min(candidates, key=lambda status: _status_preference_key(status, HARD_PRIORITY_BLOCK))
        ordered_top.append(best)
        for candidate in candidates:
            if candidate is not best:
                spillover_priority.append(candidate)

    if dedupe_urls:
        selected_urls = {status.url.casefold() for status in ordered_top}
        final_remainder = [
            *[status for status in spillover_priority if status.url.casefold() not in selected_urls],
            *[status for status in remainder if status.url.casefold() not in selected_urls],
        ]
    else:
        final_remainder = [*spillover_priority, *remainder]
    return ordered_top, final_remainder


def enforce_hard_priority_block(statuses: list[ChannelStatus]) -> list[ChannelStatus]:
    ordered_top, final_remainder = partition_hard_priority_block(statuses)
    return [*ordered_top, *final_remainder]


def sort_statuses(
    statuses: list[ChannelStatus],
    sort_by: list[str],
    group_order: list[str] | None = None,
    priority_channels: list[str] | None = None,
    catalog_order: list[str] | None = None,
) -> list[ChannelStatus]:
    priorities = priority_channels or []
    ordered_groups = group_order or []
    requested_catalog_order = catalog_order or REQUESTED_CATALOG_ORDER
    ordered_top, remainder = partition_hard_priority_block(statuses, dedupe_urls=False)

    def sort_key(status: ChannelStatus) -> tuple:
        return (
            *_catalog_order_rank(status, requested_catalog_order),
            _priority_rank(status, priorities),
            _priority_exact_bonus(status, priorities),
            _state_rank(status),
            _language_tail_rank(status),
            _country_rank(status),
            _group_rank(status, ordered_groups),
            -_quality_score(status.name),
            *tuple(str(getattr(status, field_name, "")).lower() for field_name in sort_by),
        )

    return [*ordered_top, *sorted(remainder, key=sort_key)]


def limit_channels_for_validation(
    channels: list[Channel],
    config: dict[str, Any],
    *,
    priority_channels: list[str] | None = None,
) -> list[Channel]:
    limit = int(config.get("validation_candidate_limit", 0))
    if limit <= 0 or len(channels) <= limit:
        return channels

    priorities = priority_channels or []
    group_order = list(config.get("group_order", []))
    group_quotas = dict(config.get("target_group_quotas", {}))
    buffer_multiplier = float(config.get("validation_group_buffer_multiplier", 1.35))
    group_floor = int(config.get("validation_group_floor", 12))

    ordered = sorted(
        channels,
        key=lambda channel: (
            _priority_rank(channel, priorities),
            _group_rank(channel, group_order),
            0 if _normalize_name(channel.country) == "mx" else 1,
            -_quality_score(channel.name),
            _normalize_name(channel.name),
            channel.url.casefold(),
        ),
    )

    selected: list[Channel] = []
    selected_urls: set[str] = set()

    for channel in ordered:
        if _priority_rank(channel, priorities) >= len(priorities):
            continue
        if channel.url.casefold() in selected_urls:
            continue
        selected.append(channel)
        selected_urls.add(channel.url.casefold())
        if len(selected) >= limit:
            return selected[:limit]

    for group_name, quota in group_quotas.items():
        buffered_quota = max(group_floor, int(quota * buffer_multiplier))
        added = 0
        for channel in ordered:
            if channel.group != group_name:
                continue
            if channel.url.casefold() in selected_urls:
                continue
            selected.append(channel)
            selected_urls.add(channel.url.casefold())
            added += 1
            if len(selected) >= limit:
                return selected[:limit]
            if added >= buffered_quota:
                break

    for channel in ordered:
        if channel.url.casefold() in selected_urls:
            continue
        selected.append(channel)
        selected_urls.add(channel.url.casefold())
        if len(selected) >= limit:
            break

    return selected[:limit]


def select_curated_statuses(
    statuses: list[ChannelStatus],
    *,
    target_size: int,
    group_quotas: dict[str, int],
    priority_channels: list[str] | None = None,
) -> list[ChannelStatus]:
    playable = [status for status in statuses if status.state in {"alive", "unstable"}]
    if target_size <= 0 or len(playable) <= target_size:
        return playable

    priorities = priority_channels or []
    buckets: dict[str, list[ChannelStatus]] = {}
    for status in playable:
        buckets.setdefault(status.group, []).append(status)

    selected: list[ChannelStatus] = []
    selected_urls: set[str] = set()

    best_by_label, _, dial_remainder = _select_dial_matches(playable)
    for label in REQUESTED_CATALOG_ORDER:
        status = best_by_label.get(label)
        if status is None:
            continue
        if status.url in selected_urls:
            continue
        selected.append(status)
        selected_urls.add(status.url)
        if len(selected) >= target_size:
            return selected[:target_size]

    top_block, remainder = partition_hard_priority_block(dial_remainder)

    for status in top_block:
        if status.url in selected_urls:
            continue
        selected.append(status)
        selected_urls.add(status.url)
        if len(selected) >= target_size:
            return selected[:target_size]

    for status in remainder:
        if _hard_priority_name(status) is not None:
            continue
        if _priority_rank(status, priorities) >= len(priorities):
            continue
        if status.url in selected_urls:
            continue
        selected.append(status)
        selected_urls.add(status.url)
        if len(selected) >= target_size:
            return selected[:target_size]

    for group_name, quota in group_quotas.items():
        for status in buckets.get(group_name, [])[:quota]:
            if status.url in selected_urls:
                continue
            selected.append(status)
            selected_urls.add(status.url)

    if len(selected) < target_size:
        for status in remainder:
            if status.url in selected_urls:
                continue
            selected.append(status)
            selected_urls.add(status.url)
            if len(selected) >= target_size:
                break

    return selected[:target_size]


def _escape_m3u_field(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").replace("\r", " ").strip()


def build_m3u(statuses: list[ChannelStatus]) -> str:
    lines = ["#EXTM3U"]
    playable = [status for status in statuses if status.state in {"alive", "unstable"}]
    best_by_label, _, remainder = _select_dial_matches(playable)
    ordered_statuses = [best_by_label[label] for label in REQUESTED_CATALOG_ORDER if label in best_by_label]
    selected_urls = {status.url.casefold() for status in ordered_statuses}
    ordered_statuses.extend(
        sorted(
            [status for status in remainder if status.url.casefold() not in selected_urls],
            key=lambda status: (
                _group_rank(status, []),
                _normalize_name(status.group),
                _normalize_name(status.name),
                status.url.casefold(),
            ),
        )
    )

    for status in ordered_statuses:
        if status.state not in {"alive", "unstable"}:
            continue
        name = _escape_m3u_field(status.name)
        group = _escape_m3u_field(status.group)
        dial_label = _dial_label_for_status(status)
        tvg_id = _escape_m3u_field(dial_label or status.tvg_id)
        logo = _escape_m3u_field(status.logo)
        dial_attributes = ""
        if dial_label is not None:
            dial_number = next(dial for dial, label in DIAL_MASTER_GRID.items() if label == dial_label)
            dial_attributes = f' tvg-chno="{dial_number}"'
        extinf = (
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}"{dial_attributes} '
            f'tvg-logo="{logo}" group-title="{group}",{name}'
        )
        lines.append(extinf)
        lines.append("#EXTVLCOPT:network-caching=2000")
        lines.append("#EXTVLCOPT:http-reconnect=true")
        lines.append(status.url)
        for backup_index, backup_url in enumerate(status.backup_urls, start=1):
            backup_name = _escape_m3u_field(f"{status.name} [Respaldo {backup_index}]")
            lines.append(
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{backup_name}"{dial_attributes} tvg-logo="{logo}" group-title="{group}",{backup_name}'
            )
            lines.append("#EXTVLCOPT:network-caching=2000")
            lines.append("#EXTVLCOPT:http-reconnect=true")
            lines.append(backup_url)
    return "\n".join(lines) + "\n"


def build_vod_m3u(items: list[dict[str, Any]]) -> str:
    lines = ["#EXTM3U"]
    for raw in items:
        name = _escape_m3u_field(str(raw.get("name") or "").strip())
        url = str(raw.get("url") or "").strip()
        if not name or not url:
            continue
        group = _escape_m3u_field(str(raw.get("group") or "Mi Catálogo Cloud").strip())
        tvg_id = _escape_m3u_field(str(raw.get("tvg_id") or "").strip())
        logo = _escape_m3u_field(str(raw.get("logo") or "").strip())
        extinf = (
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" '
            f'tvg-logo="{logo}" group-title="{group}",{name}'
        )
        lines.append(extinf)
        lines.append(url)
    return "\n".join(lines) + "\n"


def build_vod_status_json(statuses: list[VodStatus]) -> str:
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(statuses),
        "playable_in_vlc": sum(1 for status in statuses if status.playable_in_vlc),
        "browser_only": sum(1 for status in statuses if status.delivery == "web_page"),
        "other": sum(1 for status in statuses if status.delivery not in {"direct_media", "web_page"}),
        "items": [status.to_dict() for status in statuses],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def build_vod_status_markdown(statuses: list[VodStatus]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    playable = sum(1 for status in statuses if status.playable_in_vlc)
    browser_only = sum(1 for status in statuses if status.delivery == "web_page")
    other = sum(1 for status in statuses if status.delivery not in {"direct_media", "web_page"})

    lines = [
        "# Estado VOD",
        "",
        f"Última revisión UTC: `{now}`",
        "",
        f"- Items totales: **{len(statuses)}**",
        f"- Compatibles con VLC: **{playable}**",
        f"- Solo navegador: **{browser_only}**",
        f"- Otros/errores: **{other}**",
        "",
        "| Título | Entrega | Código | Content-Type | Error |",
        "|---|---|---|---|---|",
    ]
    for status in statuses:
        lines.append(
            f"| {status.name} | {status.delivery} | {status.status_code if status.status_code is not None else '-'} | {status.content_type or '-'} | {status.error or ''} |"
        )
    return "\n".join(lines) + "\n"


def build_vod_browser_links(statuses: list[VodStatus]) -> str:
    lines: list[str] = []
    for status in statuses:
        if status.delivery == "web_page":
            lines.append(f"{status.name}\t{status.url}")
    return "\n".join(lines) + ("\n" if lines else "")


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


def build_priority_summary(statuses: list[ChannelStatus], priority_channels: list[str]) -> dict[str, Any]:
    found: list[dict[str, Any]] = []
    missing: list[str] = []

    for pattern in priority_channels:
        normalized_pattern = _normalize_name(pattern)
        aliases = DEFAULT_PRIORITY_ALIASES.get(normalized_pattern, (normalized_pattern,))
        match = next(
            (
                status for status in statuses
                if any(
                    alias and (
                        alias in _normalize_name(status.name)
                        or alias in _normalize_name(status.group)
                    )
                    for alias in aliases
                )
            ),
            None,
        )
        if match is None:
            missing.append(pattern)
            continue
        found.append(
            {
                "requested": pattern,
                "matched_name": match.name,
                "group": match.group,
                "country": match.country,
                "state": match.state,
                "url": match.url,
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requested_total": len(priority_channels),
        "found_total": len(found),
        "missing_total": len(missing),
        "found": found,
        "missing": missing,
    }


def build_catalog_summary(statuses: list[ChannelStatus], catalog_order: list[str]) -> dict[str, Any]:
    best_by_label, candidates_by_label, _ = _select_dial_matches(statuses)
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    entries: list[dict[str, Any]] = []

    for dial_number, label in sorted(DIAL_MASTER_GRID.items()):
        match = best_by_label.get(label)
        if match is None:
            missing.append(label)
            entries.append(
                {
                    "dial": dial_number,
                    "requested": label,
                    "tvg_id": label,
                    "reserved": True,
                    "matched_name": None,
                    "group": None,
                    "country": None,
                    "state": "reserved",
                    "url": None,
                }
            )
            continue
        entry = {
            "dial": dial_number,
            "requested": label,
            "tvg_id": label,
            "reserved": match.state not in {"alive", "unstable"},
            "matched_name": match.name,
            "group": match.group,
            "country": match.country,
            "state": match.state,
            "url": match.url,
            "candidate_count": len(candidates_by_label.get(label, [])),
        }
        found.append(entry)
        entries.append(entry)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requested_total": len(DIAL_MASTER_GRID),
        "found_total": len(found),
        "missing_total": len(missing),
        "found": found,
        "missing": missing,
        "entries": entries,
    }


def build_catalog_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Estado del Dial",
        "",
        f"Última revisión UTC: `{summary['generated_at']}`",
        "",
        f"- Posiciones pedidas: **{summary['requested_total']}**",
        f"- Posiciones cubiertas: **{summary['found_total']}**",
        f"- Posiciones faltantes: **{summary['missing_total']}**",
        "",
        "| Dial | Pedido | Coincidencia | País | Estado |",
        "|---|---|---|---|---|",
    ]

    for item in summary.get("entries", summary["found"]):
        matched_name = item["matched_name"] or "Reservado"
        lines.append(
            f"| {item['dial']} | {item['requested']} | {matched_name} | {item['country'] or '-'} | {item['state']} |"
        )

    if summary["missing"]:
        lines.extend(["", "## Faltantes", ""])
        lines.extend(f"- {item}" for item in summary["missing"])

    return "\n".join(lines) + "\n"


def build_priority_summary_markdown(priority_summary: dict[str, Any]) -> str:
    lines = [
        "# Prioridades de canales",
        "",
        f"Última revisión UTC: `{priority_summary['generated_at']}`",
        "",
        f"- Prioridades solicitadas: **{priority_summary['requested_total']}**",
        f"- Prioridades encontradas: **{priority_summary['found_total']}**",
        f"- Prioridades faltantes: **{priority_summary['missing_total']}**",
        "",
        "| Pedido | Coincidencia | Grupo | País | Estado |",
        "|---|---|---|---|---|",
    ]

    for item in priority_summary["found"]:
        lines.append(
            f"| {item['requested']} | {item['matched_name']} | {item['group']} | {item['country']} | {item['state']} |"
        )

    if priority_summary["missing"]:
        lines.extend(
            [
                "",
                "## Faltantes",
                "",
            ]
        )
        lines.extend(f"- {item}" for item in priority_summary["missing"])

    return "\n".join(lines) + "\n"


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


def write_outputs(
    statuses: list[ChannelStatus],
    public_dir: Path = PUBLIC_DIR,
    priority_channels: list[str] | None = None,
    catalog_order: list[str] | None = None,
) -> bool:
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
    priority_summary = build_priority_summary(statuses, priority_channels or [])
    (public_dir / "priority_status.json").write_text(
        json.dumps(priority_summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (public_dir / "priority_status.md").write_text(
        build_priority_summary_markdown(priority_summary),
        encoding="utf-8",
    )
    catalog_summary = build_catalog_summary(statuses, catalog_order or REQUESTED_CATALOG_ORDER)
    (public_dir / "dial_status.json").write_text(
        json.dumps(catalog_summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (public_dir / "dial_status.md").write_text(
        build_catalog_summary_markdown(catalog_summary),
        encoding="utf-8",
    )
    return fallback_used


def write_vod_output(
    items: list[dict[str, Any]],
    vod_statuses: list[VodStatus] | None = None,
    public_dir: Path = PUBLIC_DIR,
) -> None:
    public_dir.mkdir(parents=True, exist_ok=True)
    statuses = vod_statuses or []
    playable_urls = {status.url for status in statuses if status.playable_in_vlc}
    filtered_items = [item for item in items if str(item.get("url") or "").strip() in playable_urls]
    (public_dir / "vod_playlist.m3u").write_text(build_vod_m3u(filtered_items), encoding="utf-8")
    (public_dir / "vod_status.json").write_text(build_vod_status_json(statuses), encoding="utf-8")
    (public_dir / "vod_status.md").write_text(build_vod_status_markdown(statuses), encoding="utf-8")
    (public_dir / "vod_browser_links.txt").write_text(build_vod_browser_links(statuses), encoding="utf-8")


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
    priority_channels = list(config.get("priority_channels", []))
    catalog_order = list(config.get("catalog_channel_order", REQUESTED_CATALOG_ORDER))
    channels = limit_channels_for_validation(
        load_channels(sources_path),
        config,
        priority_channels=priority_channels,
    )
    cloud_catalog_items = load_cloud_catalog_items(sources_path)
    vod_statuses = await check_all_vod_items(cloud_catalog_items, config)

    if not channels:
        print("[WARN] No hay canales validos en sources/channels.json")
        statuses: list[ChannelStatus] = []
        validated_statuses: list[ChannelStatus] = []
    else:
        validated_statuses = await check_all_channels(
            channels,
            config,
            cache_path=STATUS_CACHE_FILE,
        )
        validated_statuses = regroup_statuses(validated_statuses)
        validated_statuses = purge_false_positive_variants(validated_statuses)
        validated_statuses = dedupe_statuses_by_identity(
            validated_statuses,
            priority_channels=priority_channels,
        )
        validated_statuses = enforce_hard_priority_block(validated_statuses)
        validated_statuses = sort_statuses(
            validated_statuses,
            list(config["sort_by"]),
            group_order=list(config.get("group_order", [])),
            priority_channels=priority_channels,
            catalog_order=catalog_order,
        )
        statuses = select_curated_statuses(
            validated_statuses,
            target_size=int(config.get("target_playlist_size", 500)),
            group_quotas=dict(config.get("target_group_quotas", {})),
            priority_channels=priority_channels,
        )
    save_status_cache(validated_statuses, STATUS_CACHE_FILE)

    fallback_used = write_outputs(
        statuses,
        public_dir,
        priority_channels=priority_channels,
        catalog_order=catalog_order,
    )
    write_vod_output(cloud_catalog_items, vod_statuses, public_dir)
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
