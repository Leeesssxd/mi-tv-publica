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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus, urlparse

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
DISCOVERED_MIRRORS_FILE = ROOT_DIR / "sources" / "discovered_mirrors.json"
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
        "source_url": "https://iptv-org.github.io/iptv/countries/ar.m3u",
        "group": "Importados",
        "country": "AR",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/bo.m3u",
        "group": "Importados",
        "country": "BO",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/cl.m3u",
        "group": "Importados",
        "country": "CL",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/co.m3u",
        "group": "Importados",
        "country": "CO",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/cr.m3u",
        "group": "Importados",
        "country": "CR",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/ec.m3u",
        "group": "Importados",
        "country": "EC",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/es.m3u",
        "group": "Importados",
        "country": "ES",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/no.m3u",
        "group": "Deportes Públicos Internacionales",
        "country": "NO",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/pe.m3u",
        "group": "Importados",
        "country": "PE",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/py.m3u",
        "group": "Importados",
        "country": "PY",
    },
    {
        "source_url": "https://iptv-org.github.io/iptv/countries/uy.m3u",
        "group": "Importados",
        "country": "UY",
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
    "timeout_seconds": 300,
    "connect_timeout_seconds": 15,
    "sock_read_timeout_seconds": 30,
    "max_concurrency": 2,
    "source_fetch_concurrency": 2,
    "user_agent": "MiTVPublicaBot/1.0 (+https://github.com)",
    "accept_language": "es-MX,es;q=0.9,en;q=0.6",
    "retry_attempts": 3,
    "retry_backoff_base_seconds": 1.0,
    "jitter_min_seconds": 0.5,
    "jitter_max_seconds": 1.5,
    "cache_ttl_seconds": 21600,
    "chunk_size_bytes": 65536,
    "max_secret_sources": 8,
    "max_private_channels_per_source": 1200,
    "private_source_scan_line_limit": 15000,
    "geo_filter_enabled": True,
    "global_source_aggregate_limit": 150000,
    "github_crawler_enabled": True,
    "github_crawler_max_depth": 3,
    "github_crawler_max_seed_urls": 16,
    "github_crawler_max_follow_urls": 120,
    "github_crawler_max_candidates": 400,
    "github_search_terms": [
        "iptv mexico m3u",
        "iptv deportes m3u",
        "canales mexico m3u",
        "githubusercontent m3u mexico",
    ],
}

M3U8_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>()\[\]]+?\.m3u8(?:\?[^\s\"'<>()\[\]]*)?",
    re.IGNORECASE,
)
TEXT_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>()\[\]]+",
    re.IGNORECASE,
)
EXTINF_PATTERN = re.compile(r"^#EXTINF:-?\d+\s*(?P<attrs>.*?),(?P<name>.*)$")
ATTR_PATTERN = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
TVG_ID_COUNTRY_PATTERN = re.compile(r"\.([A-Za-z]{2})(?:$|[@._-])")
RETRIABLE_STATUS_CODES = {429, 503}
M3U_HEADER = "#EXTM3U"
RESTRICTIVE_STATUS_CODES = {401, 403, 503}
QUARANTINE_THRESHOLD = 3
CURATED_BUCKET_LIMITS: dict[str, int] = {
    "Familia y TV Abierta": 180,
    "Deportes": 140,
    "Peliculas - Cine": 170,
    "Peliculas - Drama y Series": 110,
    "Noticias": 50,
    "Entretenimiento": 50,
    "Otros": 30,
}
ADULT_OR_LOW_TRUST_PATTERNS = (
    "adult",
    "xxx",
    "porn",
    "18+",
    "brazzers",
    "playboy",
    "hot ",
    "hustler",
    "venus",
    "sex",
)
FAMILY_PATTERNS = (
    "canal 5",
    "azteca 7",
    "azteca uno",
    "las estrellas",
    "canal 13",
    "once",
    "canal 14",
    "tv unam",
    "canal 22",
    "imagen tv",
    "capital 21",
    "canal 26",
    "multimedios",
    "televisa",
)
SPORTS_PATTERNS = (
    "sports",
    "deportes",
    "espn",
    "fox sports",
    "claro sports",
    "tdn",
    "tudn",
    "wpt",
    "ufc",
    "boxing",
    "nba",
    "nfl",
    "mlb",
    "liga mx",
)
MOVIE_PATTERNS = (
    "movie",
    "movies",
    "pelicula",
    "peliculas",
    "cine",
    "cinema",
    "film",
    "films",
    "golden",
    "runtime",
    "filmex",
    "series",
)
NEWS_PATTERNS = (
    "news",
    "noticias",
    "milenio",
    "telediario",
    "adn40",
    "foro tv",
    "cnn",
    "bbc",
    "al jazeera",
)
ENTERTAINMENT_PATTERNS = (
    "entretenimiento",
    "entertainment",
    "comedy",
    "musica",
    "music",
    "mtv",
    "telehit",
    "variedades",
)
GEO_RESCUE_PATTERNS = (
    "vix",
    "dsports",
    "d sports",
    "tudn",
    "espn",
    "fox sports",
    "mundial",
)
GEO_WHITELIST_COUNTRIES = {"MX", "AR", "CL", "CO", "PE", "LATAM", "US", "USA", "ES"}
MX_TEXT_PATTERN = re.compile(r"\b(mx|mex|mexico|mexico city|cdmx)\b", re.IGNORECASE)
EXCLUDED_STREAM_DOMAINS = ("mac-tv.live",)
GEO_BLACKLIST_COUNTRIES = {
    "TR",
    "TUR",
    "IN",
    "CN",
    "CH",
    "RU",
    "BR",
    "PT",
    "AE",
    "SA",
    "QA",
    "KW",
    "OM",
    "BH",
    "JO",
    "IQ",
    "IR",
    "SY",
    "LB",
    "IL",
    "EG",
    "DZ",
    "MA",
    "TN",
    "PK",
    "BD",
    "UA",
    "BY",
    "RS",
    "RO",
    "BG",
    "PL",
    "CZ",
    "SK",
    "HU",
}
GEO_WHITELIST_TEXT_PATTERN = re.compile(
    r"\b(?:mx|mex|mexico|latam|latino|latina|latinoamerica|latin america|"
    r"arg|argentina|cl|chile|co|colombia|pe|peru|es|espana|spain|us|usa|"
    r"united states|español|espanol|televisa|azteca|estrellas|tudn|vix)\b",
    re.IGNORECASE,
)
GEO_BLACKLIST_TEXT_PATTERN = re.compile(
    r"\b(?:turk|turkey|turkiye|hindi|india|indian|china|chinese|arab|arabic|"
    r"saudi|emirates|dubai|qatar|russia|russian|ukraine|ukrainian|belarus|"
    r"serbia|croatia|romania|romanian|bulgaria|bulgarian|poland|polish|"
    r"hungary|hungarian|czech|brazil|brasil|brazilian|portugal|portuguese)\b",
    re.IGNORECASE,
)
CANONICAL_VARIANT_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "canal 5": ("cozumel", "tv cozumel", "xej", "juárez", "juarez"),
}
TOP_FIXED_PRIORITY = [
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
TOP_FIXED_ALIASES: dict[str, tuple[str, ...]] = {
    "Azteca Uno": ("azteca uno", "azteca 1"),
    "Las Estrellas": ("las estrellas", "canal de las estrellas"),
    "Imagen TV": ("imagen tv", "imagen tv+"),
    "Canal 5 Televisa": ("canal 5 televisa", "canal 5"),
    "Azteca 7": ("azteca 7", "azteca siete"),
    "TUDN": ("tudn",),
    "ViX": ("vix", "vix deportes", "vix premium"),
    "DSPORTS": ("dsports", "d sports", "directv sports", "dsportplus", "dsport plus"),
    "FIFA+": ("fifa+", "fifa plus"),
    "Claro Sports": ("claro sports",),
    "FOX Sports": ("fox sports",),
    "ESPN": ("espn",),
}
STRONG_VARIANT_BLACKLIST: dict[str, tuple[re.Pattern[str], ...]] = {
    "Canal 5 Televisa": (
        re.compile(r"\bcozumel\b", re.IGNORECASE),
        re.compile(r"\btv cozumel\b", re.IGNORECASE),
        re.compile(r"\bxhg\w*\b", re.IGNORECASE),
        re.compile(r"\bregional\b", re.IGNORECASE),
        re.compile(r"\blocal\b", re.IGNORECASE),
    ),
    "Las Estrellas": (
        re.compile(r"\bxhg\w*\b", re.IGNORECASE),
        re.compile(r"\bregional\b", re.IGNORECASE),
        re.compile(r"\blocal\b", re.IGNORECASE),
    ),
    "Azteca Uno": (
        re.compile(r"\bxhg\w*\b", re.IGNORECASE),
        re.compile(r"\bregional\b", re.IGNORECASE),
    ),
    "Imagen TV": (
        re.compile(r"\bregional\b", re.IGNORECASE),
        re.compile(r"\blocal\b", re.IGNORECASE),
    ),
    "Azteca 7": (
        re.compile(r"\bregional\b", re.IGNORECASE),
        re.compile(r"\blocal\b", re.IGNORECASE),
    ),
}
QUALITY_PATTERN = re.compile(r"(\d{3,4})p", re.IGNORECASE)
GITHUB_RAW_HOSTS = ("raw.githubusercontent.com", "gist.githubusercontent.com")
GITHUB_HTML_HOSTS = ("github.com",)


def detect_payload_kind(text: str) -> str:
    stripped = (text or "").lstrip("\ufeff").lstrip()
    if stripped.startswith(M3U_HEADER) or stripped.startswith("#EXTINF"):
        return "m3u"
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    return "text"


def _normalized_text(*parts: str) -> str:
    return " ".join(part.strip().casefold() for part in parts if part).strip()


def _matches_canonical_alias(normalized_name: str, alias: str) -> bool:
    pattern = re.compile(rf"^(?:{re.escape(alias)})(?:$|\s|\()", re.IGNORECASE)
    return bool(pattern.search(normalized_name))


def _normalize_catalog_label(label: str) -> str:
    normalized = _normalized_text(
        label.replace("_", " ").replace("&amp;", "&").replace("&", " and ").replace("+", " plus ")
    )
    normalized = re.sub(r"\b(hd|sd|alt)\b", " ", normalized)
    normalized = re.sub(r"\bdelay\b", " ", normalized)
    normalized = re.sub(r"\blocal\b", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _dial_aliases(label: str) -> tuple[str, ...]:
    normalized = _normalize_catalog_label(label)
    explicit = {
        "barker channel": ("barker channel", "conecta", "conecta tv", "básico plus", "basico plus"),
        "azteca uno": ("azteca uno", "azteca 1"),
        "las estrellas": ("las estrellas", "canal de las estrellas"),
        "imagen tv": ("imagen tv", "imagen tv+"),
        "canal 4 gdl": ("tv cuatro 4.1", "canal 4 guadalajara"),
        "canal 5": ("canal 5 televisa", "canal 5 hd", "canal 5"),
        "canal 6": ("canal 6 cdmx",),
        "azteca 7": ("azteca 7", "azteca siete"),
        "tudn": ("tudn",),
        "vix deportes": ("vix deportes", "vix sports", "vix"),
        "vix premium": ("vix premium", "vix"),
        "fifa plus": ("fifa+", "fifa plus"),
        "dsports": ("dsports", "d sports", "directv sports"),
        "dsports 2": ("dsports 2", "d sports 2"),
        "dsports plus": ("dsports plus", "d sports plus", "dsportplus", "dsport plus"),
        "fox sports": ("fox sports",),
        "once tv": ("once méxico", "once mexico"),
        "canal 13": ("canal 13 michoacán", "canal 13 michoacan", "canal 13"),
        "canal 14": ("canal 14",),
        "jalisco tv": ("jalisco tv",),
        "tv unam": ("tv unam",),
        "canal 22": ("canal 22 nacional", "canal 22 mexico", "canal 22"),
        "adn 40": ("adn 40",),
        "canal 44 udg": ("udg tv canal 44", "canal 44"),
        "canal del congreso": ("canal del congreso", "canal parlamento del congreso"),
        "justicia tv": ("justicia tv",),
        "telefórmula": ("teleformula", "telefórmula"),
        "milenio tv": ("milenio",),
        "mvstv": ("mvs tv",),
        "azteca internacional": ("azteca internacional",),
        "nick jr": ("nick jr",),
        "disney jr": ("disney jr",),
        "disney channel": ("disney channel",),
        "mariavision": ("maría visión", "maria visión", "mariavision"),
        "film and arts": ("film&arts", "film & arts"),
        "tvc deportes": ("tv cuatro 4.3",),
        "tvc deportes 2": ("tv cuatro 4.3",),
        "aym sports": ("aym sports",),
        "claro sports": ("claro sports",),
        "panico": ("panico",),
        "cinemax": ("cinemax",),
        "tnt": ("tnt hd", "tnt"),
        "cinecanal": ("cinecanal",),
        "clic": ("clic",),
        "htv": ("htv",),
        "exa tv": ("exa tv",),
        "sony movies": ("sony movies",),
    }.get(normalized)
    if explicit:
        return explicit
    return (normalized,)


def _preprocess_raw_name(raw_name: str) -> str:
    normalized = (raw_name or "").casefold()
    normalized = normalized.replace("+", " plus ")
    normalized = re.sub(r"[^a-z0-9+\s]+", " ", normalized)
    normalized = re.sub(
        r"\b(?:hd|sd|fhd|uhd|4k|1080p|720p|latino|lat|es|mx|mexico)\b",
        " ",
        normalized,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_and_match_advanced(raw_name: str) -> str:
    normalized = _preprocess_raw_name(raw_name)
    raw_lower = (raw_name or "").casefold()
    if not normalized:
        return ""

    if re.search(r"\bcartoonito\b", normalized):
        return "CARTOONITO_HD"

    if re.search(r"\b(cartoon|cn)\b", normalized):
        if re.search(r"\bnetwork\b", normalized) or normalized == "cn":
            return "CARTOON_NETWORK_HD"

    if re.search(r"\bnick\b", normalized):
        if re.search(r"\bjr\b", normalized):
            return "NICK_JR_HD"
        return "NICKELODEON_HD"

    if re.search(r"\bdisney\b", normalized):
        if re.search(r"\bjr\b", normalized):
            return "DISNEY_JR_HD"
        return "DISNEY_CHANNEL_HD"

    if re.search(r"\bbaby\b", normalized):
        if re.search(r"\bfirst\b", normalized):
            return "BABY_FIRST_HD"
        if re.search(r"\btv\b", normalized):
            return "BABY_TV_SD"

    if re.search(r"\bstar\b", normalized) and re.search(r"\b(channel|ch|action|class)\b", normalized):
        return "STAR_CHANNEL_HD"

    if re.search(r"\bsony\b", normalized):
        if re.search(r"\b(movie|movies|cine)\b", normalized):
            return "SONY_MOVIES"
        return "SONY_HD"

    if re.search(r"\buniversal\b", normalized):
        if re.search(r"\bstudio\b", normalized):
            return "STUDIO_UNIVERSAL_HD"
        return "UNIVERSAL_TV_HD"

    if re.search(r"\b(cinema|cine)\b", normalized):
        if re.search(r"\bplatino\b", normalized):
            if re.search(r"\b2\b", normalized):
                return "CINEMA_PLATINO_2_SD"
            return "CINEMA_PLATINO_HD"
        if re.search(r"\bcanal\b", normalized):
            return "CINECANAL_HD"
        if re.search(r"\blatino\b", raw_lower):
            return "CINE_LATINO_SD"

    if re.search(r"\bespn\b", normalized):
        if re.search(r"\b4\b", normalized):
            return "ESPN_4_HD"
        if re.search(r"\b3\b", normalized):
            return "ESPN_3_HD"
        if re.search(r"\b2\b", normalized):
            return "ESPN_2_HD"
        return "ESPN_HD"

    if re.search(r"\btvc\b", normalized) and re.search(r"\bdeporte", normalized):
        if re.search(r"\b2\b", normalized):
            return "TVC_DEPORTES_2_HD"
        return "TVC_DEPORTES_HD"

    if re.search(r"\bclaro\b", normalized) and re.search(r"\bsport", normalized):
        return "CLARO_SPORTS_HD"

    if re.search(r"\bmeganoticia", normalized):
        if re.search(r"\bmx\b", raw_lower):
            return "MEGANOTICIAS_MX_HD"
        return "MEGANOTICIAS_HD"

    if re.search(r"\bcnn\b", normalized) or re.search(r"\bcnne\b|\bcnni\b", normalized):
        if re.search(r"\b(cnne|espanol|español|en espanol|en español)\b", raw_lower):
            return "CNNE_HD"
        if re.search(r"\b(cnni|international)\b", raw_lower):
            return "CNNI_HD"
        return "CNN_HD"

    return ""


def is_followable_playlist_reference(url: str) -> bool:
    normalized_url = normalize_url(url)
    if not normalized_url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(normalized_url)
    host = (parsed.netloc or "").casefold()
    path = (parsed.path or "").casefold()
    if host in GITHUB_RAW_HOSTS and path.endswith((".m3u", ".m3u8", ".json", ".txt")):
        return True
    if host in GITHUB_HTML_HOSTS and "/raw/" in path:
        return True
    if path.endswith((".m3u", ".m3u8", ".json")):
        return True
    return False


def build_github_search_urls(config: dict[str, Any]) -> list[str]:
    terms = config.get("github_search_terms", DEFAULT_CONFIG["github_search_terms"])
    if not isinstance(terms, list):
        return []
    urls: list[str] = []
    for term in terms[: int(config.get("github_crawler_max_seed_urls", 16))]:
        raw_term = str(term).strip()
        if not raw_term:
            continue
        urls.append(f"https://api.github.com/search/code?q={quote_plus(raw_term)}")
    return urls


def _extract_named_stream_candidates_from_m3u(text: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    pending_name = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs = parse_extinf_line(line)
            pending_name = str((attrs or {}).get("name") or "").strip()
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")) and pending_name:
            candidates.append({"raw_name": pending_name, "url": normalize_url(line)})
            pending_name = ""
    return candidates


def _extract_named_stream_candidates_from_json_payload(payload: Any) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []

    def visit(value: Any, context_name: str = "") -> None:
        if isinstance(value, dict):
            candidate_name = str(
                value.get("name")
                or value.get("title")
                or value.get("channel")
                or value.get("label")
                or context_name
                or ""
            ).strip()
            candidate_url = normalize_url(str(value.get("url") or value.get("src") or value.get("file") or "").strip())
            if candidate_name and candidate_url.startswith(("http://", "https://")):
                candidates.append({"raw_name": candidate_name, "url": candidate_url})
            for nested in value.values():
                visit(nested, candidate_name)
            return
        if isinstance(value, list):
            for item in value:
                visit(item, context_name)

    visit(payload)
    return candidates


def extract_structured_candidates_from_text(text: str) -> tuple[list[dict[str, str]], list[str]]:
    stripped = (text or "").lstrip("\ufeff").strip()
    nested_sources: list[str] = []
    seen_nested: set[str] = set()

    for match in TEXT_URL_PATTERN.findall(text or ""):
        normalized = normalize_url(match)
        if is_followable_playlist_reference(normalized) and normalized not in seen_nested:
            seen_nested.add(normalized)
            nested_sources.append(normalized)

    if detect_payload_kind(stripped) == "m3u":
        return _extract_named_stream_candidates_from_m3u(text), nested_sources

    if detect_payload_kind(stripped) == "json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return [], nested_sources
        return _extract_named_stream_candidates_from_json_payload(payload), nested_sources

    return [], nested_sources


def build_discovered_mirrors_payload(candidates: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
    payload: dict[str, list[dict[str, Any]]] = {}
    seen_per_dial: dict[str, set[str]] = {}

    for candidate in candidates:
        raw_name = str(candidate.get("raw_name") or "").strip()
        url = normalize_url(str(candidate.get("url") or "").strip())
        if not raw_name or not url:
            continue
        matched_label = normalize_and_match_advanced(raw_name)
        if not matched_label or matched_label not in DIAL_MASTER_GRID.values():
            continue
        dial = next((key for key, value in DIAL_MASTER_GRID.items() if value == matched_label), None)
        if dial is None:
            continue
        dial_key = str(dial)
        seen_urls = seen_per_dial.setdefault(dial_key, set())
        if url.casefold() in seen_urls:
            continue
        seen_urls.add(url.casefold())
        payload.setdefault(dial_key, []).append(
            {
                "url": url,
                "raw_name": raw_name,
                "geo": "MX" if re.search(r"\b(mx|mexico|mex)\b", raw_name, re.IGNORECASE) else "ALL",
                "verified": False,
            }
        )

    for dial_key, items in payload.items():
        items.sort(key=lambda item: (str(item.get("geo") or "").upper() != "MX", str(item.get("url") or "").casefold()))
    return payload


def _dial_grid_label(item: dict[str, Any]) -> str | None:
    advanced_match = normalize_and_match_advanced(str(item.get("name") or ""))
    if advanced_match:
        return advanced_match

    normalized_name = _normalized_text(str(item.get("name") or ""))
    normalized_tvg_id = _normalized_text(str(item.get("tvg_id") or ""))
    for label in DIAL_MASTER_GRID.values():
        normalized_label = _normalize_catalog_label(label)
        if normalized_tvg_id and normalized_tvg_id == normalized_label:
            return label
        if any(alias and _matches_canonical_alias(normalized_name, alias) for alias in _dial_aliases(label)):
            return label
    return None


def _top_priority_name(item: dict[str, Any]) -> str | None:
    grid_label = _dial_grid_label(item)
    if grid_label == "AZTECA_UNO_HD":
        return "Azteca Uno"
    if grid_label == "LAS_ESTRELLAS_HD":
        return "Las Estrellas"
    if grid_label == "IMAGEN_TV_HD":
        return "Imagen TV"
    if grid_label == "CANAL_5_LOCAL_HD":
        return "Canal 5 Televisa"
    if grid_label == "AZTECA_7_HD":
        return "Azteca 7"
    if grid_label == "TUDN_HD":
        return "TUDN"
    if grid_label in {"VIX_DEPORTES_HD", "VIX_PREMIUM_HD"}:
        return "ViX"
    if grid_label in {"DSPORTS_HD", "DSPORTS_2_HD", "DSPORTS_PLUS_HD"}:
        return "DSPORTS"
    if grid_label == "FIFA_PLUS_HD":
        return "FIFA+"
    if grid_label == "CLARO_SPORTS_HD":
        return "Claro Sports"
    if grid_label == "FOX_SPORTS_HD":
        return "FOX Sports"
    if grid_label in {"ESPN_HD", "ESPN_2_HD", "ESPN_3_HD", "ESPN_4_HD"}:
        return "ESPN"
    return None


def _quality_score(name: str) -> int:
    match = QUALITY_PATTERN.search(name or "")
    if match:
        return int(match.group(1))
    normalized = _normalized_text(name)
    if "4k" in normalized or "uhd" in normalized:
        return 2160
    if "1080" in normalized or "full hd" in normalized or "fhd" in normalized:
        return 1080
    if "hd" in normalized:
        return 720
    if "sd" in normalized:
        return 480
    return 0


def _identity_name(name: str) -> str:
    normalized = _normalized_text(name)
    normalized = re.sub(r"\([^)]*\)", " ", normalized)
    normalized = re.sub(r"\b(uhd|fhd|hd|sd|4k|1080p|720p|480p|latam|latino|mx|us|usa|es)\b", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _discovered_identity_key(item: dict[str, Any]) -> tuple[str, str]:
    dial_label = _dial_grid_label(item)
    if dial_label is not None:
        return ("dial", dial_label)

    canonical_name = _top_priority_name(item)
    if canonical_name is not None:
        return ("top", canonical_name)

    tvg_id = _normalized_text(str(item.get("tvg_id") or ""))
    if tvg_id:
        return ("tvg", tvg_id)

    normalized_name = _identity_name(str(item.get("name") or ""))
    normalized_group = _normalized_text(str(item.get("group") or ""))
    normalized_country = _normalized_text(str(item.get("country") or ""))
    if normalized_name:
        return ("name", f"{normalized_name}|{normalized_group}|{normalized_country}")
    return ("url", normalize_url(str(item.get("url") or "")).casefold())


def _matches_any_pattern(text: str, patterns: tuple[str, ...] | list[str]) -> bool:
    normalized = text.casefold()
    return any(pattern.casefold() in normalized for pattern in patterns)


def _candidate_country_markers(item: dict[str, Any]) -> set[str]:
    markers: set[str] = set()
    explicit_country = str(item.get("country") or "").strip().upper()
    if explicit_country:
        markers.add(explicit_country)

    tvg_id = str(item.get("tvg_id") or "").strip()
    inferred_country = infer_country({"tvg-id": tvg_id}, str(item.get("group") or ""))
    if inferred_country:
        markers.add(inferred_country.upper())

    return markers


def _candidate_geo_text(item: dict[str, Any]) -> str:
    return _normalized_text(
        str(item.get("name") or ""),
        str(item.get("group") or ""),
        str(item.get("country") or ""),
        str(item.get("tvg_id") or ""),
    )


def is_geo_rescue_candidate(item: dict[str, Any]) -> bool:
    return _matches_any_pattern(_candidate_geo_text(item), GEO_RESCUE_PATTERNS)


def is_excluded_regional_variant(item: dict[str, Any]) -> bool:
    normalized_text = _candidate_geo_text(item)
    for canonical_name, excluded_tokens in CANONICAL_VARIANT_EXCLUSIONS.items():
        if canonical_name not in normalized_text:
            continue
        if any(token in normalized_text for token in excluded_tokens):
            return True
    for patterns in STRONG_VARIANT_BLACKLIST.values():
        if any(pattern.search(normalized_text) for pattern in patterns):
            canonical_match = any(
                alias and _matches_canonical_alias(normalized_text, alias)
                for aliases in TOP_FIXED_ALIASES.values()
                for alias in aliases
            )
            if canonical_match:
                return True
    return False


def should_keep_channel_by_geo(item: dict[str, Any]) -> bool:
    if is_geo_rescue_candidate(item):
        return True

    country_markers = _candidate_country_markers(item)
    if country_markers & GEO_BLACKLIST_COUNTRIES:
        return False
    if country_markers & GEO_WHITELIST_COUNTRIES:
        return True

    text = _candidate_geo_text(item)
    if GEO_BLACKLIST_TEXT_PATTERN.search(text):
        return False
    if GEO_WHITELIST_TEXT_PATTERN.search(text):
        return True

    return False


def normalize_discovered_item(
    item: dict[str, Any] | str,
    *,
    default_group: str,
    default_country: str,
) -> dict[str, Any] | None:
    if isinstance(item, str):
        normalized_url = normalize_url(item)
        if not normalized_url:
            return None
        return {
            "name": build_channel_name(normalized_url, set()),
            "group": default_group,
            "country": default_country,
            "url": normalized_url,
            "logo": "",
            "tvg_id": "",
        }

    normalized_url = normalize_url(str(item.get("url") or ""))
    if not normalized_url:
        return None
    return {
        "name": str(item.get("name") or "").strip(),
        "group": (str(item.get("group") or "").strip() or default_group),
        "country": (str(item.get("country") or "").strip() or default_country),
        "url": normalized_url,
        "logo": str(item.get("logo") or "").strip(),
        "tvg_id": str(item.get("tvg_id") or "").strip(),
    }


def filter_discovered_channels(
    discovered_channels: list[dict[str, Any] | str],
    *,
    default_group: str,
    default_country: str,
    geo_filter_enabled: bool = True,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for raw_item in discovered_channels:
        item = normalize_discovered_item(
            raw_item,
            default_group=default_group,
            default_country=default_country,
        )
        if item is None:
            continue
        if is_low_trust_channel(item):
            continue
        if is_excluded_regional_variant(item):
            continue
        if geo_filter_enabled and not should_keep_channel_by_geo(item):
            continue
        filtered.append(item)
    return filtered


def discovered_channel_hash(item: dict[str, Any]) -> str:
    normalized_url = normalize_url(str(item.get("url") or ""))
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


def discovered_channel_preference_score(
    item: dict[str, Any],
    priority_patterns: list[str] | None = None,
) -> int:
    score = curated_channel_score(item, priority_patterns)
    if is_geo_rescue_candidate(item):
        score += 250
    if str(item.get("country") or "").strip().upper() in {"MX", "US", "USA"}:
        score += 25
    if str(item.get("tvg_id") or "").strip():
        score += 10
    if str(item.get("logo") or "").strip():
        score += 5
    return score


def discovered_channel_preference_key(
    item: dict[str, Any],
    priority_patterns: list[str] | None = None,
) -> tuple[int, int, int, int, str, str]:
    dial_label = _dial_grid_label(item)
    canonical_name = _top_priority_name(item)
    mx_preferred = 0 if str(item.get("country") or "").strip().upper() == "MX" else 1
    return (
        0 if dial_label is not None else 1,
        0 if canonical_name is not None else 1,
        mx_preferred,
        -discovered_channel_preference_score(item, priority_patterns),
        -_quality_score(str(item.get("name") or "")),
        _normalized_text(str(item.get("name") or "")),
        normalize_url(str(item.get("url") or "")).casefold(),
    )


def discovered_channel_primary_merit(
    item: dict[str, Any],
    priority_patterns: list[str] | None = None,
) -> tuple[int, int, int, int, int]:
    dial_label = _dial_grid_label(item)
    canonical_name = _top_priority_name(item)
    mx_preferred = 0 if str(item.get("country") or "").strip().upper() == "MX" else 1
    return (
        0 if dial_label is not None else 1,
        0 if canonical_name is not None else 1,
        mx_preferred,
        -discovered_channel_preference_score(item, priority_patterns),
        -_quality_score(str(item.get("name") or "")),
    )


def dedupe_discovered_channels(
    discovered_channels: list[dict[str, Any]],
    *,
    priority_patterns: list[str] | None = None,
) -> list[dict[str, Any]]:
    best_by_url: dict[str, dict[str, Any]] = {}
    for item in discovered_channels:
        normalized_url = normalize_url(str(item.get("url") or "")).casefold()
        if not normalized_url:
            continue
        existing = best_by_url.get(normalized_url)
        if existing is None or discovered_channel_preference_key(item, priority_patterns) < discovered_channel_preference_key(
            existing,
            priority_patterns,
        ):
            best_by_url[normalized_url] = item

    best_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for item in sorted(
        best_by_url.values(),
        key=lambda candidate: (
            _discovered_identity_key(candidate),
            discovered_channel_preference_key(candidate, priority_patterns),
        ),
    ):
        identity = _discovered_identity_key(item)
        existing = best_by_identity.get(identity)
        if existing is None or discovered_channel_preference_key(item, priority_patterns) < discovered_channel_preference_key(
            existing,
            priority_patterns,
        ):
            best_by_identity[identity] = item

    deduped = list(best_by_identity.values())
    deduped.sort(key=lambda candidate: discovered_channel_preference_key(candidate, priority_patterns))
    return deduped


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


def save_discovered_mirrors_payload(
    payload: dict[str, list[dict[str, Any]]],
    mirrors_path: Path | None = None,
) -> None:
    mirrors_path = mirrors_path or DISCOVERED_MIRRORS_FILE
    mirrors_path.parent.mkdir(parents=True, exist_ok=True)
    mirrors_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _dial_label_to_display_name(label: str) -> str:
    normalized = (
        label.replace("_", " ")
        .replace("PLUS", "+")
        .replace(" and ", " & ")
        .strip()
    )
    normalized = re.sub(r"\b(HD|SD|ALT|DELAY)\b", " ", normalized, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", normalized).strip() or label


def load_discovered_mirrors_pool(
    mirrors_path: Path | None = None,
) -> list[dict[str, Any]]:
    mirrors_path = mirrors_path or DISCOVERED_MIRRORS_FILE
    if not mirrors_path.exists():
        return []

    try:
        payload = json.loads(mirrors_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[WARN] discovered_mirrors.json invalido, se omite: {exc}")
        return []

    if not isinstance(payload, dict):
        print("[WARN] discovered_mirrors.json debe contener un objeto por dial, se omite.")
        return []

    discovered: list[dict[str, Any]] = []
    for raw_dial, entries in sorted(payload.items(), key=lambda item: int(str(item[0])) if str(item[0]).isdigit() else 999999):
        try:
            dial = int(str(raw_dial).strip())
        except ValueError:
            continue
        grid_label = DIAL_MASTER_GRID.get(dial)
        if grid_label is None or not isinstance(entries, list):
            continue
        for entry in sorted(
            [item for item in entries if isinstance(item, dict)],
            key=lambda item: (
                str(item.get("geo") or "").strip().upper() != "MX",
                normalize_url(str(item.get("url") or "")).casefold(),
            ),
        ):
            url = normalize_url(str(entry.get("url") or ""))
            if not url or is_excluded_stream_url(url):
                continue
            raw_name = str(entry.get("raw_name") or entry.get("name") or "").strip()
            matched_label = normalize_and_match_advanced(raw_name) if raw_name else ""
            label = matched_label or grid_label
            if label not in DIAL_MASTER_GRID.values():
                continue
            display_name = raw_name or _dial_label_to_display_name(label)
            country = str(entry.get("geo") or entry.get("country") or "").strip().upper() or "ALL"
            candidate = {
                "name": display_name,
                "group": categorize_curated_channel({"name": display_name, "group": "", "country": country}),
                "country": country,
                "url": url,
                "logo": "",
                "tvg_id": label,
                "verified": bool(entry.get("verified")),
                "source": "discovered_mirrors",
            }
            discovered.append(candidate)
    return discovered


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


def build_network_timeout(config: dict[str, Any]) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(
        total=float(config.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"])),
        connect=float(config.get("connect_timeout_seconds", DEFAULT_CONFIG["connect_timeout_seconds"])),
        sock_read=float(config.get("sock_read_timeout_seconds", DEFAULT_CONFIG["sock_read_timeout_seconds"])),
    )


def build_source_semaphore(config: dict[str, Any]) -> asyncio.Semaphore:
    configured = int(config.get("source_fetch_concurrency", config.get("max_concurrency", 2)))
    return asyncio.Semaphore(max(1, min(configured, 2)))


async def fetch_remote_text(
    session: aiohttp.ClientSession,
    source_url: str,
    config: dict[str, Any],
    *,
    semaphore: asyncio.Semaphore | None = None,
) -> str:
    attempts = int(config.get("retry_attempts", 3))
    backoff_base = float(config.get("retry_backoff_base_seconds", 1.0))
    active_semaphore = semaphore or build_source_semaphore(config)

    for attempt in range(1, attempts + 1):
        await sleep_with_jitter(config)
        try:
            async with active_semaphore:
                async with session.get(source_url, allow_redirects=True, ssl=False) as response:
                    if response.status in RETRIABLE_STATUS_CODES and attempt < attempts:
                        await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                        continue
                    if response.status >= 400:
                        return ""
                    return await response.text(errors="ignore")
        except aiohttp.ClientError:
            if attempt < attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            return ""
        except asyncio.TimeoutError:
            return ""
    return ""


def _extract_github_api_seed_urls(payload: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    if not isinstance(payload, dict):
        return urls
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        html_url = normalize_url(str(item.get("html_url") or ""))
        if "/blob/" in html_url:
            raw_url = html_url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
            if is_followable_playlist_reference(raw_url) and raw_url not in seen:
                seen.add(raw_url)
                urls.append(raw_url)
    return urls


async def discover_github_recursive_mirrors(
    config: dict[str, Any],
    *,
    session: aiohttp.ClientSession | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if not bool(config.get("github_crawler_enabled", True)):
        return {}

    timeout = build_network_timeout(config)
    headers = build_headers(config)
    owns_session = session is None
    active_session = session or aiohttp.ClientSession(timeout=timeout, headers=headers)
    active_semaphore = semaphore or build_source_semaphore(config)

    max_depth = int(config.get("github_crawler_max_depth", 3))
    max_follow_urls = int(config.get("github_crawler_max_follow_urls", 120))
    max_candidates = int(config.get("github_crawler_max_candidates", 400))

    queue: list[tuple[str, int]] = [(url, 0) for url in build_github_search_urls(config)]
    visited: set[str] = set()
    candidates: list[dict[str, str]] = []

    try:
        while queue and len(visited) < max_follow_urls and len(candidates) < max_candidates:
            current_url, depth = queue.pop(0)
            normalized_current = normalize_url(current_url)
            if not normalized_current or normalized_current in visited or depth > max_depth:
                continue
            visited.add(normalized_current)

            response_text = await fetch_remote_text(
                active_session,
                normalized_current,
                config,
                semaphore=active_semaphore,
            )
            if not response_text:
                continue

            if normalized_current.startswith("https://api.github.com/search/code?"):
                try:
                    api_payload = json.loads(response_text)
                except json.JSONDecodeError:
                    continue
                for seed_url in _extract_github_api_seed_urls(api_payload):
                    if seed_url not in visited and len(queue) < max_follow_urls:
                        queue.append((seed_url, depth))
                continue

            structured_candidates, nested_sources = extract_structured_candidates_from_text(response_text)
            candidates.extend(structured_candidates[: max_candidates - len(candidates)])

            if depth >= max_depth:
                continue
            for nested_url in nested_sources:
                normalized_nested = normalize_url(nested_url)
                if (
                    normalized_nested
                    and normalized_nested not in visited
                    and len(queue) < max_follow_urls
                ):
                    queue.append((normalized_nested, depth + 1))
    finally:
        if owns_session:
            await active_session.close()

    return build_discovered_mirrors_payload(candidates)


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


def is_excluded_stream_url(url: str) -> bool:
    host = (urlparse(normalize_url(url)).netloc or "").casefold()
    return any(domain in host for domain in EXCLUDED_STREAM_DOMAINS)


def _is_mx_candidate_text(*parts: str) -> bool:
    haystack = " ".join(part for part in parts if part).strip()
    return bool(MX_TEXT_PATTERN.search(haystack))


@dataclass
class M3UCollector:
    used_names: set[str] = field(default_factory=set)
    default_group: str = "General"
    default_country: str = ""
    early_filter: bool = False
    max_channels: int | None = None
    channels: list[dict[str, Any]] = field(default_factory=list)
    pending_attrs: dict[str, str] | None = None
    pending_match: str = ""
    pending_keep: bool = False
    pending_is_mx: bool = False
    matched_channels: int = 0

    def process_line(self, raw_line: str) -> bool:
        line = raw_line.strip()
        if not line:
            return False

        if line.startswith("#EXTINF"):
            self.pending_attrs = parse_extinf_line(line)
            raw_name = str((self.pending_attrs or {}).get("name") or "").strip()
            matched_label = normalize_and_match_advanced(raw_name) if self.early_filter else ""
            inferred_country = infer_country(self.pending_attrs or {}, str((self.pending_attrs or {}).get("group-title") or ""))
            self.pending_is_mx = _is_mx_candidate_text(
                raw_name,
                str((self.pending_attrs or {}).get("group-title") or ""),
                str((self.pending_attrs or {}).get("tvg-id") or ""),
                inferred_country,
                self.default_country,
            )
            self.pending_match = matched_label
            self.pending_keep = (not self.early_filter) or bool(matched_label) or self.pending_is_mx
            return False

        if line.startswith("#"):
            return False

        if not self.pending_attrs:
            return False

        if not is_supported_playlist_url(line):
            self.pending_attrs = None
            self.pending_match = ""
            self.pending_keep = False
            self.pending_is_mx = False
            return False

        channel_url = normalize_url(line)
        if is_excluded_stream_url(channel_url):
            self.pending_attrs = None
            self.pending_match = ""
            self.pending_keep = False
            self.pending_is_mx = False
            return False

        if self.pending_keep:
            channel = build_channel_record_from_extinf(self.pending_attrs, channel_url)
            if channel.get("group") == "General" and self.default_group:
                channel["group"] = self.default_group
            if not channel.get("country"):
                channel["country"] = "MX" if self.pending_is_mx else self.default_country
            if self.pending_match:
                channel["tvg_id"] = self.pending_match
                self.matched_channels += 1
            channel["name"] = ensure_unique_name(str(channel.get("name", "")).strip(), self.used_names)
            self.channels.append(channel)
            if self.max_channels is not None and len(self.channels) >= self.max_channels:
                self.pending_attrs = None
                return True

        self.pending_attrs = None
        self.pending_match = ""
        self.pending_keep = False
        self.pending_is_mx = False
        return False


def collect_channels_from_m3u_lines(
    lines: Iterator[str],
    *,
    default_group: str = "General",
    default_country: str = "",
    early_filter: bool = False,
    max_channels: int | None = None,
) -> list[dict[str, Any]]:
    collector = M3UCollector(
        default_group=default_group,
        default_country=default_country,
        early_filter=early_filter,
        max_channels=max_channels,
    )
    for raw_line in lines:
        if collector.process_line(raw_line):
            break
    return collector.channels


def parse_m3u_file(file_path: Path) -> list[dict[str, Any]]:
    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        return collect_channels_from_m3u_lines(handle)


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


def categorize_curated_channel(item: dict[str, Any]) -> str:
    text = _normalized_text(
        str(item.get("name") or ""),
        str(item.get("group") or ""),
        str(item.get("tvg_id") or ""),
    )
    if _matches_any_pattern(text, SPORTS_PATTERNS):
        return "Deportes"
    if _matches_any_pattern(text, MOVIE_PATTERNS):
        if "series" in text:
            return "Peliculas - Drama y Series"
        return "Peliculas - Cine"
    if _matches_any_pattern(text, NEWS_PATTERNS):
        return "Noticias"
    if _matches_any_pattern(text, FAMILY_PATTERNS):
        return "Familia y TV Abierta"
    if _matches_any_pattern(text, ENTERTAINMENT_PATTERNS):
        return "Entretenimiento"
    return "Otros"


def is_low_trust_channel(item: dict[str, Any]) -> bool:
    text = _normalized_text(
        str(item.get("name") or ""),
        str(item.get("group") or ""),
        str(item.get("tvg_id") or ""),
        str(item.get("url") or ""),
    )
    return _matches_any_pattern(text, ADULT_OR_LOW_TRUST_PATTERNS)


def curated_channel_score(item: dict[str, Any], priority_patterns: list[str] | None = None) -> int:
    text = _normalized_text(
        str(item.get("name") or ""),
        str(item.get("group") or ""),
        str(item.get("tvg_id") or ""),
    )
    country = str(item.get("country") or "").strip().upper()
    category = categorize_curated_channel(item)
    score = 0

    if country == "MX":
        score += 120
    elif country == "ALL":
        score += 40

    if category == "Familia y TV Abierta":
        score += 80
    elif category == "Deportes":
        score += 70
    elif category.startswith("Peliculas"):
        score += 60
    elif category == "Noticias":
        score += 35
    elif category == "Entretenimiento":
        score += 30

    if "mx" in text or "mex" in text or "latino" in text or "español" in text or "espanol" in text:
        score += 20

    if priority_patterns and _matches_any_pattern(text, tuple(priority_patterns)):
        score += 200

    return score


def curate_private_channels(
    discovered_channels: list[dict[str, Any]] | list[str],
    *,
    max_items: int,
    priority_patterns: list[str] | None = None,
) -> list[dict[str, Any]] | list[str]:
    if not discovered_channels or isinstance(discovered_channels[0], str):
        return limit_discovered_channels(discovered_channels, max_items=max_items)

    normalized_items: list[dict[str, Any]] = []
    for raw_item in discovered_channels:
        if not isinstance(raw_item, dict):
            continue
        if is_low_trust_channel(raw_item):
            continue
        item = dict(raw_item)
        item["group"] = categorize_curated_channel(item)
        normalized_items.append(item)

    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in normalized_items:
        bucket = str(item.get("group") or "Otros")
        buckets.setdefault(bucket, []).append(item)

    for items in buckets.values():
        items.sort(key=lambda candidate: curated_channel_score(candidate, priority_patterns), reverse=True)

    curated: list[dict[str, Any]] = []
    for bucket_name, bucket_limit in CURATED_BUCKET_LIMITS.items():
        curated.extend(buckets.get(bucket_name, [])[:bucket_limit])

    seen_urls: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in sorted(curated, key=lambda candidate: curated_channel_score(candidate, priority_patterns), reverse=True):
        normalized_url = normalize_url(str(item.get("url") or ""))
        if not normalized_url or normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        deduped.append(item)

    return deduped[:max_items]


def merge_channels(
    existing_channels: list[dict[str, Any]],
    discovered_channels: list[dict[str, Any]] | list[str],
    *,
    default_group: str = "Importados",
    default_country: str = "",
    preferred_primary_patterns: list[str] | None = None,
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
            candidate_identity = _discovered_identity_key(candidate)
            for existing_item in merged:
                if not isinstance(existing_item, dict):
                    continue
                existing_tvg_id = str(existing_item.get("tvg_id", "")).strip().casefold()
                existing_name = str(existing_item.get("name", "")).strip().casefold()
                existing_country = str(existing_item.get("country", "")).strip().casefold()
                existing_identity = _discovered_identity_key(existing_item)
                same_channel = bool(
                    (candidate_tvg_id and existing_tvg_id and candidate_tvg_id == existing_tvg_id)
                    or candidate_identity == existing_identity
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
                existing_as_candidate = {
                    "name": str(existing_item.get("name") or ""),
                    "group": str(existing_item.get("group") or ""),
                    "country": str(existing_item.get("country") or ""),
                    "url": existing_primary,
                    "logo": str(existing_item.get("logo") or ""),
                    "tvg_id": str(existing_item.get("tvg_id") or ""),
                }
                candidate_text = _normalized_text(
                    str(candidate.get("name") or ""),
                    str(candidate.get("group") or ""),
                    str(candidate.get("tvg_id") or ""),
                )
                candidate_matches_preferred = bool(
                    preferred_primary_patterns
                    and _matches_any_pattern(candidate_text, tuple(preferred_primary_patterns))
                )
                candidate_is_better = candidate_matches_preferred or discovered_channel_primary_merit(
                    candidate,
                    preferred_primary_patterns,
                ) < discovered_channel_primary_merit(
                    existing_as_candidate,
                    preferred_primary_patterns,
                )
                if candidate_is_better:
                    old_primary = existing_primary
                    existing_item["url"] = normalized_url
                    backup_values = [old_primary, existing_backup]
                    deduped_backups = []
                    seen_backups: set[str] = set()
                    for backup_candidate in backup_values:
                        normalized_backup = normalize_url(backup_candidate)
                        if (
                            normalized_backup
                            and normalized_backup != normalized_url
                            and normalized_backup not in seen_backups
                        ):
                            seen_backups.add(normalized_backup)
                            deduped_backups.append(normalized_backup)
                    if deduped_backups:
                        existing_item["backup_url"] = deduped_backups if len(deduped_backups) > 1 else deduped_backups[0]
                    existing_urls.add(normalized_url)
                    added += 1
                    failover_match = "promoted_to_primary"
                    break
                backup_values = existing_item.get("backup_url")
                if isinstance(backup_values, list):
                    merged_backups = [normalize_url(str(value)) for value in backup_values]
                elif backup_values:
                    merged_backups = [normalize_url(str(backup_values))]
                else:
                    merged_backups = []
                if normalized_url not in merged_backups:
                    merged_backups.append(normalized_url)
                    merged_backups = [value for value in merged_backups if value and value != existing_primary]
                    existing_item["backup_url"] = merged_backups if len(merged_backups) > 1 else merged_backups[0]
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

    merged.sort(
        key=lambda candidate: (
            0 if _dial_grid_label(candidate) is not None else 1,
            next((dial for dial, label in DIAL_MASTER_GRID.items() if label == _dial_grid_label(candidate)), 999999),
            0 if _top_priority_name(candidate) is not None else 1,
            TOP_FIXED_PRIORITY.index(_top_priority_name(candidate)) if _top_priority_name(candidate) is not None else len(TOP_FIXED_PRIORITY),
            _normalized_text(str(candidate.get("group") or "")),
            _normalized_text(str(candidate.get("name") or "")),
            normalize_url(str(candidate.get("url") or "")).casefold(),
        )
    )
    return merged, added


async def discover_single_source(
    source_url: str,
    config: dict[str, Any],
    *,
    default_group: str,
    default_country: str,
    metadata_url: str | None = None,
    category_filter: list[str] | None = None,
    max_channels: int | None = None,
    preferred_primary_patterns: list[str] | None = None,
    curate_private: bool = False,
    semaphore: asyncio.Semaphore | None = None,
) -> tuple[list[dict[str, Any]], int]:
    if curate_private:
        discovered_channels, detected_count = await discover_large_m3u_source(
            source_url,
            config,
            default_group=default_group,
            default_country=default_country,
            max_channels=max_channels or int(DEFAULT_CONFIG["max_private_channels_per_source"]),
            semaphore=semaphore,
        )
        normalized_channels = filter_discovered_channels(
            discovered_channels,
            default_group=default_group,
            default_country=default_country,
            geo_filter_enabled=bool(config.get("geo_filter_enabled", True)),
        )
        normalized_channels = curate_private_channels(
            normalized_channels,
            max_items=max_channels or int(DEFAULT_CONFIG["max_private_channels_per_source"]),
            priority_patterns=preferred_primary_patterns,
        )
        return normalized_channels, len(normalized_channels)

    cached_file = await fetch_source_to_cache(source_url, config, semaphore=semaphore)

    if file_looks_like_m3u(cached_file):
        discovered_channels: list[dict[str, Any]] | list[str] = parse_m3u_file(cached_file)
        detected_count = len(discovered_channels)
    elif file_looks_like_json(cached_file):
        if metadata_url:
            metadata_file = await fetch_source_to_cache(metadata_url, config, semaphore=semaphore)
            discovered_channels = parse_iptv_org_streams(
                cached_file,
                metadata_file,
                country_filter=default_country,
                category_filter=category_filter,
            )
        elif "iptv-org.github.io/api/streams.json" in source_url:
            metadata_file = await fetch_source_to_cache(
                DEFAULT_IPTVORG_CHANNELS_URL,
                config,
                semaphore=semaphore,
            )
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

    normalized_channels = filter_discovered_channels(
        discovered_channels,
        default_group=default_group,
        default_country=default_country,
        geo_filter_enabled=bool(config.get("geo_filter_enabled", True)),
    )

    if not curate_private:
        normalized_channels = limit_discovered_channels(
            normalized_channels,
            max_items=max_channels or int(config.get("global_source_aggregate_limit", 150000)),
        )
    detected_count = len(normalized_channels)
    return normalized_channels, detected_count


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
    preferred_primary_patterns: list[str] | None = None,
    curate_private: bool = False,
) -> tuple[int, int]:
    discovered_channels, detected_count = await discover_single_source(
        source_url,
        config,
        default_group=default_group,
        default_country=default_country,
        metadata_url=metadata_url,
        category_filter=category_filter,
        max_channels=max_channels,
        preferred_primary_patterns=preferred_primary_patterns,
        curate_private=curate_private,
    )
    payload = load_sources_payload(sources_path)
    existing_channels = _extract_channel_entries(payload)
    merged_channels, added = merge_channels(
        existing_channels,
        discovered_channels,
        default_group=default_group,
        default_country=default_country,
        preferred_primary_patterns=preferred_primary_patterns,
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


async def fetch_source_to_cache(
    source_url: str,
    config: dict[str, Any],
    *,
    semaphore: asyncio.Semaphore | None = None,
) -> Path:
    cached = load_cached_path(
        source_url,
        int(config.get("cache_ttl_seconds", 21600)),
    )
    if cached is not None:
        return cached

    timeout = build_network_timeout(config)
    headers = build_headers(config)
    attempts = int(config.get("retry_attempts", 3))
    backoff_base = float(config.get("retry_backoff_base_seconds", 1.0))
    active_semaphore = semaphore or build_source_semaphore(config)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for attempt in range(1, attempts + 1):
            await sleep_with_jitter(config)
            try:
                async with active_semaphore:
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


async def discover_large_m3u_source(
    source_url: str,
    config: dict[str, Any],
    *,
    default_group: str,
    default_country: str,
    max_channels: int | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> tuple[list[dict[str, Any]], int]:
    timeout = build_network_timeout(config)
    headers = build_headers(config)
    attempts = int(config.get("retry_attempts", 3))
    backoff_base = float(config.get("retry_backoff_base_seconds", 1.0))
    active_semaphore = semaphore or build_source_semaphore(config)
    line_limit = int(config.get("private_source_scan_line_limit", 15000))

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for attempt in range(1, attempts + 1):
            await sleep_with_jitter(config)
            try:
                async with active_semaphore:
                    async with session.get(source_url, allow_redirects=True, ssl=False) as response:
                        if response.status in RETRIABLE_STATUS_CODES and attempt < attempts:
                            await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                            continue
                        response.raise_for_status()

                        collector = M3UCollector(
                            default_group=default_group,
                            default_country=default_country,
                            early_filter=True,
                            max_channels=max_channels,
                        )
                        lines_processed = 0
                        while True:
                            raw_line = await response.content.readline()
                            if not raw_line:
                                break
                            lines_processed += 1
                            if collector.process_line(raw_line.decode("utf-8", errors="ignore")):
                                break
                            if line_limit > 0 and lines_processed >= line_limit and collector.matched_channels == 0:
                                print(
                                    f"[WARN] Corte temprano en {source_url}: "
                                    f"{lines_processed} lineas sin coincidencias de la grilla."
                                )
                                break

                        return collector.channels, len(collector.channels)
            except asyncio.TimeoutError:
                if attempt < attempts:
                    await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                raise
            except aiohttp.ClientError:
                if attempt < attempts:
                    await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                raise

    return [], 0


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
    source_semaphore = build_source_semaphore(config)
    discovered_mirrors_payload = await discover_github_recursive_mirrors(
        config,
        semaphore=source_semaphore,
    )
    if discovered_mirrors_payload:
        save_discovered_mirrors_payload(discovered_mirrors_payload)
    discovered_mirror_pool = load_discovered_mirrors_pool()
    telemetry_records: list[dict[str, Any]] = []
    resolved_source_url = source_url or DEFAULT_SOURCE_URL
    attempted_sources = {normalize_source_url(resolved_source_url)}
    aggregated_discovered: list[dict[str, Any]] = []

    primary_discovered, detected_count = await discover_single_source(
        resolved_source_url,
        config,
        default_group=default_group,
        default_country=default_country,
        metadata_url=metadata_url,
        semaphore=source_semaphore,
    )
    aggregated_discovered.extend(primary_discovered)

    secondary_sources = config.get("secondary_sources")
    source_batch = secondary_sources if isinstance(secondary_sources, list) else DEFAULT_SECONDARY_SOURCES
    total_detected = detected_count
    for source_spec in source_batch:
        if not isinstance(source_spec, dict):
            continue
        batch_url = str(source_spec.get("source_url") or "").strip()
        normalized_batch_url = normalize_source_url(batch_url)
        if not batch_url or normalized_batch_url in attempted_sources:
            continue
        attempted_sources.add(normalized_batch_url)
        try:
            batch_discovered, batch_detected = await discover_single_source(
                batch_url,
                config,
                default_group=str(source_spec.get("group") or default_group).strip() or default_group,
                default_country=str(source_spec.get("country") or default_country).strip(),
                metadata_url=source_spec.get("metadata_url"),
                category_filter=source_spec.get("categories") if isinstance(source_spec.get("categories"), list) else None,
                semaphore=source_semaphore,
            )
            aggregated_discovered.extend(batch_discovered)
            total_detected += batch_detected
        except Exception as exc:  # noqa: BLE001
            print(describe_source_error("Fuente secundaria", batch_url, exc))
            continue

    private_source_specs = [
        *load_secret_upstream_pools(os.getenv(SECRET_UPSTREAM_POOLS_ENV))[: int(config.get("max_secret_sources", 1))],
        *ensure_local_private_sources_file(LOCAL_PRIVATE_SOURCES_FILE),
    ]
    private_source_jobs: list[tuple[dict[str, Any], str, str, dict[str, Any]]] = []
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
            private_source_jobs.append((source_spec, fingerprint, batch_url, telemetry_record))
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

    async def process_private_source(
        source_spec: dict[str, Any],
        fingerprint: str,
        batch_url: str,
        telemetry_record: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        local_telemetry = dict(telemetry_record)
        batch_discovered, batch_detected = await discover_single_source(
            batch_url,
            config,
            default_group=str(source_spec.get("group") or default_group).strip() or default_group,
            default_country=str(source_spec.get("country") or default_country).strip(),
            metadata_url=source_spec.get("metadata_url"),
            category_filter=source_spec.get("categories") if isinstance(source_spec.get("categories"), list) else None,
            max_channels=int(config.get("max_private_channels_per_source", 500)),
            preferred_primary_patterns=list(config.get("priority_channels", [])),
            curate_private=True,
            semaphore=source_semaphore,
        )
        updated_entry = update_quarantine_entry(
            quarantine_state,
            fingerprint,
            source_spec,
            batch_url,
            status="success",
            http_status=200,
        )
        local_telemetry["status"] = "success"
        local_telemetry["http_status"] = 200
        local_telemetry["consecutive_failures"] = int(updated_entry.get("consecutive_failures") or 0)
        local_telemetry["quarantined"] = bool(updated_entry.get("quarantined"))
        return batch_discovered, batch_detected, local_telemetry

    private_results = await asyncio.gather(
        *(process_private_source(*job) for job in private_source_jobs),
        return_exceptions=True,
    )
    for job, result in zip(private_source_jobs, private_results):
        source_spec, fingerprint, batch_url, telemetry_record = job
        if isinstance(result, Exception):
            http_status = extract_http_status(result)
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
            print(describe_source_error("Fuente local", telemetry_record["source_env"] or batch_url, result))
            if telemetry_record["quarantined"]:
                print(f"[INFO] Fuente local movida a cuarentena: {telemetry_record['source_env'] or telemetry_record['source_url_hint']}")
            telemetry_records.append(telemetry_record)
            continue

        batch_discovered, batch_detected, updated_telemetry = result
        aggregated_discovered.extend(batch_discovered)
        total_detected += batch_detected
        telemetry_records.append(updated_telemetry)

    aggregated_discovered.extend(discovered_mirror_pool)

    payload = load_sources_payload(sources_path)
    existing_channels = _extract_channel_entries(payload)
    deduped_global = dedupe_discovered_channels(
        aggregated_discovered,
        priority_patterns=list(config.get("priority_channels", [])),
    )
    merged_channels, total_added = merge_channels(
        existing_channels,
        deduped_global,
        default_group=default_group,
        default_country=default_country,
        preferred_primary_patterns=list(config.get("priority_channels", [])),
    )
    if isinstance(payload, dict) and isinstance(payload.get("channels"), list):
        payload["channels"] = merged_channels
        save_sources_payload(payload, sources_path)
    else:
        save_channels_raw(merged_channels, sources_path)

    save_quarantine_state(quarantine_state)
    write_telemetry_report(telemetry_records)

    print("=" * 50)
    print("Resumen de importacion de canales")
    print("=" * 50)
    print(f"URL origen:         {resolved_source_url}")
    print(f"Fuentes procesadas: {len(attempted_sources)}")
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
