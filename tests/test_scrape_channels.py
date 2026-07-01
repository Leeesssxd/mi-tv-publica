"""
Pruebas unitarias para scrape_channels.py

Cubren la extraccion por regex y el merge en channels.json sin hacer
peticiones HTTP reales.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scrape_channels import (  # noqa: E402
    DEFAULT_IPTVORG_CHANNELS_URL,
    DEFAULT_LOCAL_PRIVATE_SOURCES,
    DEFAULT_SECONDARY_SOURCES,
    DEFAULT_SOURCE_URL,
    build_discovered_mirrors_payload,
    collect_channels_from_m3u_lines,
    curate_private_channels,
    dedupe_discovered_channels,
    detect_payload_kind,
    discover_github_recursive_mirrors,
    ensure_local_private_sources_file,
    ensure_unique_name,
    extract_structured_candidates_from_text,
    extract_m3u8_links,
    extract_text_links_from_file,
    file_looks_like_m3u,
    file_looks_like_json,
    infer_country,
    is_supported_playlist_url,
    iter_text_chunks,
    load_any_cached_path,
    load_cached_text,
    load_discovered_mirrors_pool,
    load_env_file,
    load_secret_upstream_pools,
    merge_channels,
    normalize_and_match_advanced,
    normalize_url,
    normalize_source_url,
    parse_json_teles_channel_json,
    parse_extinf_line,
    parse_generic_channel_json,
    is_excluded_regional_variant,
    parse_iptv_org_streams,
    parse_m3u_file,
    should_keep_channel_by_geo,
    resolve_source_url,
    run,
    save_cached_text,
    save_discovered_mirrors_payload,
    update_quarantine_entry,
    write_telemetry_report,
)


def test_extract_m3u8_links_detecta_y_deduplica_urls():
    text = """
    Canal 1: https://example.com/live/main.m3u8
    Canal 2: https://example.com/live/main.m3u8
    Canal 3: https://cdn.example.org/otro/index.m3u8?token=abc123
    """

    urls = extract_m3u8_links(text)

    assert urls == [
        "https://example.com/live/main.m3u8",
        "https://cdn.example.org/otro/index.m3u8?token=abc123",
    ]


def test_detect_payload_kind_distingue_m3u_json_y_texto():
    assert detect_payload_kind("#EXTM3U\n#EXTINF:-1,Demo") == "m3u"
    assert detect_payload_kind('{"channels": []}') == "json"
    assert detect_payload_kind("https://example.com/live.m3u8") == "text"


def test_ensure_local_private_sources_file_lo_crea_si_no_existe(tmp_path):
    local_file = tmp_path / "local_private_sources.json"

    payload = ensure_local_private_sources_file(local_file)

    assert local_file.exists()
    assert payload == DEFAULT_LOCAL_PRIVATE_SOURCES


def test_load_env_file_parsea_variables_simples(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PRIVATE_SOURCE_1=http://example.com/feed\nOTRA=valor\n", encoding="utf-8")
    assert load_env_file(env_file) == {
        "PRIVATE_SOURCE_1": "http://example.com/feed",
        "OTRA": "valor",
    }


def test_load_secret_upstream_pools_admite_urls_y_diccionarios():
    payload = load_secret_upstream_pools(
        json.dumps(
            [
                "http://example.com/feed.m3u",
                {"source_url": "http://example.com/extra.m3u", "group": "Privados", "country": "ALL"},
            ]
        )
    )
    assert payload == [
        {"source_url": "http://example.com/feed.m3u", "group": "Privados Cloud", "country": "ALL"},
        {"source_url": "http://example.com/extra.m3u", "group": "Privados", "country": "ALL"},
    ]


def test_load_discovered_mirrors_pool_normaliza_diales_y_prioriza_mx(tmp_path):
    mirrors_file = tmp_path / "discovered_mirrors.json"
    mirrors_file.write_text(
        json.dumps(
            {
                "105": [
                    {"url": "https://example.com/all.m3u8", "geo": "ALL", "verified": False},
                    {"url": "https://example.com/mx.m3u8", "geo": "MX", "verified": False},
                ]
            }
        ),
        encoding="utf-8",
    )

    payload = load_discovered_mirrors_pool(mirrors_file)

    assert [item["url"] for item in payload] == [
        "https://example.com/mx.m3u8",
        "https://example.com/all.m3u8",
    ]
    assert all(item["tvg_id"] == "CANAL_5_LOCAL_HD" for item in payload)
    assert payload[0]["source"] == "discovered_mirrors"


def test_load_discovered_mirrors_pool_aplica_normalizador_avanzado_al_raw_name(tmp_path):
    mirrors_file = tmp_path / "discovered_mirrors.json"
    mirrors_file.write_text(
        json.dumps(
            {
                "302": [
                    {"url": "https://example.com/espn3.m3u8", "geo": "MX", "raw_name": "ESPN 3 Mexico 1080p", "verified": False},
                ]
            }
        ),
        encoding="utf-8",
    )

    payload = load_discovered_mirrors_pool(mirrors_file)

    assert payload[0]["tvg_id"] == "ESPN_3_HD"
    assert payload[0]["name"] == "ESPN 3 Mexico 1080p"


def test_load_discovered_mirrors_pool_omite_dominio_mac_tv_live(tmp_path):
    mirrors_file = tmp_path / "discovered_mirrors.json"
    mirrors_file.write_text(
        json.dumps(
            {
                "105": [
                    {"url": "https://mac-tv.live/live/canal5.m3u8", "geo": "MX", "verified": False},
                    {"url": "https://example.com/canal5.m3u8", "geo": "MX", "verified": False},
                ]
            }
        ),
        encoding="utf-8",
    )

    payload = load_discovered_mirrors_pool(mirrors_file)

    assert [item["url"] for item in payload] == ["https://example.com/canal5.m3u8"]


def test_extract_structured_candidates_from_text_extrae_canales_y_fuentes_anidadas():
    text = "\n".join(
        [
            "#EXTM3U",
            '#EXTINF:-1,ESPN 3 Mexico 1080p',
            "https://streams.example/espn3.m3u8",
            "https://raw.githubusercontent.com/user/repo/main/lista.m3u",
        ]
    )

    candidates, nested_sources = extract_structured_candidates_from_text(text)

    assert candidates == [{"raw_name": "ESPN 3 Mexico 1080p", "url": "https://streams.example/espn3.m3u8"}]
    assert "https://raw.githubusercontent.com/user/repo/main/lista.m3u" in nested_sources


def test_collect_channels_from_m3u_lines_descarta_temprano_lo_ajeno_a_grilla():
    lines = iter(
        [
            "#EXTM3U\n",
            "#EXTINF:-1,Canal Irrelevante Internacional\n",
            "https://example.com/irrelevante.m3u8\n",
            "#EXTINF:-1,ESPN 3 Mexico 1080p\n",
            "https://example.com/espn3.m3u8\n",
        ]
    )

    channels = collect_channels_from_m3u_lines(
        lines,
        default_group="Privados",
        default_country="ALL",
        early_filter=True,
    )

    assert len(channels) == 1
    assert channels[0]["tvg_id"] == "ESPN_3_HD"
    assert channels[0]["url"] == "https://example.com/espn3.m3u8"


def test_collect_channels_from_m3u_lines_omite_mac_tv_live():
    lines = iter(
        [
            "#EXTM3U\n",
            "#EXTINF:-1,Canal 5 Mexico\n",
            "https://mac-tv.live/live/canal5.m3u8\n",
            "#EXTINF:-1,Canal 5 Mexico\n",
            "https://example.com/canal5.m3u8\n",
        ]
    )

    channels = collect_channels_from_m3u_lines(
        lines,
        default_group="Privados",
        default_country="MX",
        early_filter=True,
    )

    assert [channel["url"] for channel in channels] == ["https://example.com/canal5.m3u8"]


def test_build_discovered_mirrors_payload_agrupa_por_dial():
    payload = build_discovered_mirrors_payload(
        [
            {"raw_name": "ESPN 3 Mexico 1080p", "url": "https://streams.example/espn3.m3u8"},
            {"raw_name": "CNN International", "url": "https://streams.example/cnni.m3u8"},
        ]
    )

    assert payload["306"][0]["raw_name"] == "ESPN 3 Mexico 1080p"
    assert payload["162"][0]["raw_name"] == "CNN International"


def test_discover_github_recursive_mirrors_respeta_profundidad_y_unifica_resultado(monkeypatch):
    import scrape_channels

    payloads = {
        "https://api.github.com/search/code?q=iptv+test": json.dumps(
            {
                "items": [
                    {"html_url": "https://github.com/user/repo/blob/main/lista.m3u"},
                ]
            }
        ),
        "https://raw.githubusercontent.com/user/repo/main/lista.m3u": "\n".join(
            [
                "#EXTM3U",
                "#EXTINF:-1,ESPN 3 Mexico 1080p",
                "https://streams.example/espn3.m3u8",
                "https://raw.githubusercontent.com/user/repo/main/extra.m3u",
            ]
        ),
        "https://raw.githubusercontent.com/user/repo/main/extra.m3u": "\n".join(
            [
                "#EXTM3U",
                "#EXTINF:-1,CNN International",
                "https://streams.example/cnni.m3u8",
            ]
        ),
    }

    async def fake_fetch_remote_text(session, source_url, config):
        return payloads.get(source_url, "")

    monkeypatch.setattr(scrape_channels, "fetch_remote_text", fake_fetch_remote_text)

    payload = asyncio.run(
        discover_github_recursive_mirrors(
            {
                "timeout_seconds": 1,
                "retry_attempts": 1,
                "retry_backoff_base_seconds": 0,
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "user_agent": "pytest",
                "accept_language": "es",
                "github_crawler_enabled": True,
                "github_crawler_max_depth": 3,
                "github_crawler_max_follow_urls": 10,
                "github_crawler_max_candidates": 10,
                "github_search_terms": ["iptv test"],
            }
        )
    )

    assert payload["306"][0]["url"] == "https://streams.example/espn3.m3u8"
    assert payload["162"][0]["url"] == "https://streams.example/cnni.m3u8"


def test_normalize_and_match_advanced_resuelve_clusters_pedidos():
    assert normalize_and_match_advanced("CN") == "CARTOON_NETWORK_HD"
    assert normalize_and_match_advanced("Cartoonito HD") == "CARTOONITO_HD"
    assert normalize_and_match_advanced("Nick Jr MX 720p") == "NICK_JR_HD"
    assert normalize_and_match_advanced("Nick HD") == "NICKELODEON_HD"
    assert normalize_and_match_advanced("Disney Jr Latino") == "DISNEY_JR_HD"
    assert normalize_and_match_advanced("Disney Channel 1080p") == "DISNEY_CHANNEL_HD"
    assert normalize_and_match_advanced("Baby First") == "BABY_FIRST_HD"
    assert normalize_and_match_advanced("Baby TV") == "BABY_TV_SD"
    assert normalize_and_match_advanced("Star Channel") == "STAR_CHANNEL_HD"
    assert normalize_and_match_advanced("Sony Movies") == "SONY_MOVIES"
    assert normalize_and_match_advanced("Sony HD") == "SONY_HD"
    assert normalize_and_match_advanced("Studio Universal") == "STUDIO_UNIVERSAL_HD"
    assert normalize_and_match_advanced("Universal TV") == "UNIVERSAL_TV_HD"
    assert normalize_and_match_advanced("Cinema Platino 2") == "CINEMA_PLATINO_2_SD"
    assert normalize_and_match_advanced("Cinema Platino") == "CINEMA_PLATINO_HD"
    assert normalize_and_match_advanced("Cine Canal") == "CINECANAL_HD"
    assert normalize_and_match_advanced("ESPN 4 MX") == "ESPN_4_HD"
    assert normalize_and_match_advanced("TVC Deportes 2") == "TVC_DEPORTES_2_HD"
    assert normalize_and_match_advanced("Claro Sports") == "CLARO_SPORTS_HD"
    assert normalize_and_match_advanced("Meganoticias MX") == "MEGANOTICIAS_MX_HD"
    assert normalize_and_match_advanced("Meganoticias") == "MEGANOTICIAS_HD"
    assert normalize_and_match_advanced("CNN en Español") == "CNNE_HD"
    assert normalize_and_match_advanced("CNN International") == "CNNI_HD"
    assert normalize_and_match_advanced("CNN") == "CNN_HD"


def test_resolve_source_url_usa_source_env(monkeypatch):
    monkeypatch.setenv("PRIVATE_SOURCE_9", "http://example.com/env.m3u")
    assert resolve_source_url({"source_env": "PRIVATE_SOURCE_9"}, {}) == "http://example.com/env.m3u"


def test_ensure_local_private_sources_file_omite_json_invalido(tmp_path, capsys):
    local_file = tmp_path / "local_private_sources.json"
    local_file.write_text("{bad", encoding="utf-8")

    payload = ensure_local_private_sources_file(local_file)

    assert payload == []
    captured = capsys.readouterr()
    assert "local_private_sources.json invalido" in captured.out


def test_parse_extinf_line_extrae_atributos():
    line = '#EXTINF:-1 tvg-id="abc.us" tvg-logo="https://img/logo.png" group-title="News",ABC News'
    attrs = parse_extinf_line(line)
    assert attrs == {
        "tvg-id": "abc.us",
        "tvg-logo": "https://img/logo.png",
        "group-title": "News",
        "name": "ABC News",
    }


def test_extract_m3u8_links_limpia_puntuacion_final():
    text = 'Mira esto: "https://example.com/live/main.m3u8?auth=1"),'

    urls = extract_m3u8_links(text)

    assert urls == ["https://example.com/live/main.m3u8?auth=1"]


def test_normalize_url_recorta_basura_final():
    assert normalize_url(" https://example.com/a.m3u8'); ") == "https://example.com/a.m3u8"


def test_normalize_source_url_homologa_mayusculas_y_basura_final():
    assert (
        normalize_source_url(" HTTP://Example.com/Feed.m3u?x=1); ")
        == "http://example.com/feed.m3u?x=1"
    )


def test_is_supported_playlist_url_acepta_http_y_https_sin_extension():
    assert is_supported_playlist_url("http://provider.example:8080/get.php?username=u&password=p&type=m3u_plus") is True
    assert is_supported_playlist_url("https://provider.example/live/12345") is True
    assert is_supported_playlist_url("udp://239.0.0.1:1234") is False


def test_iter_text_chunks_parte_payload_grande():
    chunks = list(iter_text_chunks("abcdefghij", 4))
    assert chunks == ["abcd", "efgh", "ij"]


def test_parse_m3u_file_convierte_a_formato_local(tmp_path):
    m3u_file = tmp_path / "index.m3u"
    m3u_file.write_text(
        '#EXTM3U\n'
        '#EXTINF:-1 tvg-id="abc.us" tvg-logo="https://img/logo.png" group-title="News",ABC News\n'
        'https://stream.example.com/live.m3u8\n'
        '#EXTINF:-1 group-title="Sports",Canal Dos\n'
        'https://stream.example.com/other.mp4\n',
        encoding="utf-8",
    )

    channels = parse_m3u_file(m3u_file)

    assert channels == [
        {
            "name": "ABC News",
            "group": "News",
            "country": "US",
            "url": "https://stream.example.com/live.m3u8",
            "logo": "https://img/logo.png",
            "tvg_id": "abc.us",
        },
        {
            "name": "Canal Dos",
            "group": "Sports",
            "country": "",
            "url": "https://stream.example.com/other.mp4",
            "logo": "",
            "tvg_id": "",
        },
    ]


def test_parse_m3u_file_acepta_urls_http_sin_extension_m3u8(tmp_path):
    m3u_file = tmp_path / "index.m3u"
    m3u_file.write_text(
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="Privados",Canal Premium\n'
        "http://provider.example:8080/live/usuario/clave/12345\n",
        encoding="utf-8",
    )

    channels = parse_m3u_file(m3u_file)

    assert channels == [
        {
            "name": "Canal Premium",
            "group": "Privados",
            "country": "",
            "url": "http://provider.example:8080/live/usuario/clave/12345",
            "logo": "",
            "tvg_id": "",
        }
    ]


def test_curate_private_channels_prioriza_mx_deportes_y_filtra_adultos():
    curated = curate_private_channels(
        [
            {"name": "Canal 5 MX", "group": "General", "country": "MX", "url": "http://a", "logo": "", "tvg_id": "canal5.mx"},
            {"name": "ESPN Deportes", "group": "Sports", "country": "MX", "url": "http://b", "logo": "", "tvg_id": "espn.mx"},
            {"name": "Movie Gold", "group": "Movies", "country": "ALL", "url": "http://c", "logo": "", "tvg_id": "movie"},
            {"name": "Adult XXX", "group": "Adult", "country": "ALL", "url": "http://d", "logo": "", "tvg_id": "adult"},
        ],
        max_items=10,
        priority_patterns=["Canal 5"],
    )

    assert [item["name"] for item in curated] == ["Canal 5 MX", "ESPN Deportes", "Movie Gold"]
    assert [item["group"] for item in curated] == ["Familia y TV Abierta", "Deportes", "Peliculas - Cine"]


def test_should_keep_channel_by_geo_rescata_mundial_y_filtra_paises_bloqueados():
    assert should_keep_channel_by_geo(
        {"name": "Mundial ESPN 4K", "group": "Sports", "country": "TR", "tvg_id": "", "url": "https://a"}
    ) is True
    assert should_keep_channel_by_geo(
        {"name": "Turkey Sports", "group": "Sports", "country": "TR", "tvg_id": "", "url": "https://b"}
    ) is False
    assert should_keep_channel_by_geo(
        {"name": "Canal 5 Televisa", "group": "TV Abierta", "country": "MX", "tvg_id": "canal5.mx", "url": "https://c"}
    ) is True


def test_dedupe_discovered_channels_conserva_el_mejor_candidato_por_url():
    deduped = dedupe_discovered_channels(
        [
            {"name": "Generic Sports", "group": "Sports", "country": "ALL", "url": "https://stream.example/live.m3u8", "logo": "", "tvg_id": ""},
            {"name": "TUDN MX", "group": "Sports", "country": "MX", "url": "https://stream.example/live.m3u8", "logo": "https://img/logo.png", "tvg_id": "tudn.mx"},
        ],
        priority_patterns=["TUDN"],
    )

    assert len(deduped) == 1
    assert deduped[0]["name"] == "TUDN MX"


def test_is_excluded_regional_variant_descarta_canal_5_cozumel():
    assert is_excluded_regional_variant(
        {"name": "Canal 5 TV Cozumel (1080p)", "group": "Familia", "country": "MX", "tvg_id": "", "url": "https://a"}
    ) is True
    assert is_excluded_regional_variant(
        {"name": "Canal 5 Televisa", "group": "Familia", "country": "MX", "tvg_id": "", "url": "https://b"}
    ) is False


def test_parse_m3u_file_asegura_nombres_unicos_en_cargas_masivas(tmp_path):
    m3u_file = tmp_path / "index.m3u"
    m3u_file.write_text(
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="General",Canal Demo\n'
        "https://stream.example.com/uno.m3u8\n"
        '#EXTINF:-1 group-title="General",Canal Demo\n'
        "https://stream.example.com/dos.m3u8\n",
        encoding="utf-8",
    )

    channels = parse_m3u_file(m3u_file)

    assert [channel["name"] for channel in channels] == ["Canal Demo", "Canal Demo (2)"]


def test_file_looks_like_m3u_detecta_playlist(tmp_path):
    m3u_file = tmp_path / "index.m3u"
    m3u_file.write_text("#EXTM3U\n#EXTINF:-1,Canal\nhttps://a.com/b.m3u8\n", encoding="utf-8")
    assert file_looks_like_m3u(m3u_file) is True


def test_file_looks_like_json_detecta_json(tmp_path):
    json_file = tmp_path / "streams.json"
    json_file.write_text('[{"channel":"abc.mx","url":"https://a.com/live.m3u8"}]', encoding="utf-8")
    assert file_looks_like_json(json_file) is True


def test_infer_country_detecta_codigo_en_tvg_id_con_sufijo():
    attrs = {"tvg-id": "Canal22.mx@SD"}
    assert infer_country(attrs, "General") == "MX"


def test_parse_iptv_org_streams_convierte_a_formato_local(tmp_path):
    streams_file = tmp_path / "streams.json"
    channels_file = tmp_path / "channels.json"
    streams_file.write_text(
        '[{"channel":"abc.mx","title":"ABC","url":"https://a.com/live.m3u8","quality":"720p"}]',
        encoding="utf-8",
    )
    channels_file.write_text(
        '[{"id":"abc.mx","name":"ABC Mexico","country":"MX","categories":["news"]}]',
        encoding="utf-8",
    )

    parsed = parse_iptv_org_streams(streams_file, channels_file, country_filter="MX")

    assert parsed == [
        {
            "name": "ABC Mexico (720p)",
            "group": "News",
            "country": "MX",
            "url": "https://a.com/live.m3u8",
            "logo": "",
            "tvg_id": "abc.mx",
        }
    ]


def test_parse_iptv_org_streams_filtra_por_categoria(tmp_path):
    streams_file = tmp_path / "streams.json"
    channels_file = tmp_path / "channels.json"
    streams_file.write_text(
        '[{"channel":"sport.no","title":"Sport","url":"https://a.com/sport.m3u8"},{"channel":"news.no","title":"News","url":"https://a.com/news.m3u8"}]',
        encoding="utf-8",
    )
    channels_file.write_text(
        '[{"id":"sport.no","name":"Sport Norge","country":"NO","categories":["sports"]},{"id":"news.no","name":"News Norge","country":"NO","categories":["news"]}]',
        encoding="utf-8",
    )

    parsed = parse_iptv_org_streams(streams_file, channels_file, country_filter="NO", category_filter=["sports"])

    assert parsed == [
        {
            "name": "Sport Norge",
            "group": "Sports",
            "country": "NO",
            "url": "https://a.com/sport.m3u8",
            "logo": "",
            "tvg_id": "sport.no",
        }
    ]


def test_parse_generic_channel_json_convierte_a_formato_local(tmp_path):
    source_file = tmp_path / "channels.json"
    source_file.write_text(
        '{"channels":[{"title":"Canal 14","slug":"canal-14","url":"https://a.com/live.m3u8","logo":"https://a.com/logo.png"}]}',
        encoding="utf-8",
    )

    parsed = parse_generic_channel_json(
        source_file,
        default_country="MX",
        default_group="Publico",
    )

    assert parsed == [
        {
            "name": "Canal 14",
            "group": "Publico",
            "country": "MX",
            "url": "https://a.com/live.m3u8",
            "logo": "https://a.com/logo.png",
            "tvg_id": "canal-14",
        }
    ]


def test_parse_json_teles_channel_json_convierte_senales_m3u8(tmp_path):
    source_file = tmp_path / "channels.json"
    source_file.write_text(
        """
        {
          "channels": [
            {
              "id": "net-tv",
              "name": "Net TV",
              "logo": "https://example.com/logo.png",
              "country": "ar",
              "category": "news",
              "signals": [
                {"type": "m3u8", "url": "https://example.com/live/chunks.m3u8"},
                {"type": "iframe", "url": "https://example.com/embed"}
              ]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    parsed = parse_json_teles_channel_json(source_file)

    assert parsed == [
        {
            "name": "Net TV",
            "group": "News",
            "country": "AR",
            "url": "https://example.com/live/chunks.m3u8",
            "logo": "https://example.com/logo.png",
            "tvg_id": "net-tv",
        }
    ]


def test_parse_json_teles_channel_json_acepta_catalogo_actual(tmp_path):
    source_file = tmp_path / "canales.json"
    source_file.write_text(
        """
        {
          "nordic-sport": {
            "nombre": "Nordic Sport",
            "logo": "https://example.com/logo.png",
            "país": "no",
            "categoría": "sports",
            "señales": {
              "m3u8_url": ["https://example.com/live/nordic.m3u8"],
              "iframe_url": ["https://example.com/embed"]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    parsed = parse_json_teles_channel_json(source_file, country_filter="NO")

    assert parsed == [
        {
            "name": "Nordic Sport",
            "group": "Sports",
            "country": "NO",
            "url": "https://example.com/live/nordic.m3u8",
            "logo": "https://example.com/logo.png",
            "tvg_id": "nordic-sport",
        }
    ]


def test_parse_json_teles_channel_json_filtra_por_pais(tmp_path):
    source_file = tmp_path / "channels.json"
    source_file.write_text(
        """
        {
          "channels": [
            {
              "id": "net-tv",
              "name": "Net TV",
              "logo": "https://example.com/logo.png",
              "country": "ar",
              "category": "news",
              "signals": [{"type": "m3u8", "url": "https://example.com/ar.m3u8"}]
            },
            {
              "id": "nmas",
              "name": "NMás",
              "logo": "https://example.com/logo2.png",
              "country": "mx",
              "category": "news",
              "signals": [{"type": "m3u8", "url": "https://example.com/mx.m3u8"}]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    parsed = parse_json_teles_channel_json(source_file, country_filter="MX")

    assert parsed == [
        {
            "name": "NMás",
            "group": "News",
            "country": "MX",
            "url": "https://example.com/mx.m3u8",
            "logo": "https://example.com/logo2.png",
            "tvg_id": "nmas",
        }
    ]


def test_extract_text_links_from_file_detecta_enlaces(tmp_path):
    source_file = tmp_path / "feed.txt"
    source_file.write_text("uno https://a.com/b.m3u8 dos https://c.com/d.m3u8", encoding="utf-8")
    urls = extract_text_links_from_file(source_file, 8)
    assert urls == ["https://a.com/b.m3u8", "https://c.com/d.m3u8"]


def test_merge_channels_agrega_solo_nuevos_sin_duplicar_por_url():
    existing = [
        {
            "name": "Existente",
            "group": "Base",
            "country": "MX",
            "url": "https://example.com/live/main.m3u8",
            "logo": "",
            "tvg_id": "",
        }
    ]

    merged, added = merge_channels(
        existing,
        [
            "https://example.com/live/main.m3u8",
            "https://cdn.example.org/otro/index.m3u8",
        ],
        default_group="Importados",
        default_country="US",
    )

    assert added == 1
    assert len(merged) == 2
    assert merged[1]["url"] == "https://cdn.example.org/otro/index.m3u8"
    assert merged[1]["group"] == "Importados"
    assert merged[1]["country"] == "US"


def test_merge_channels_preserva_metadatos_de_m3u():
    existing = []
    merged, added = merge_channels(
        existing,
        [
            {
                "name": "ABC News",
                "group": "News",
                "country": "US",
                "url": "https://stream.example.com/live.m3u8",
                "logo": "https://img/logo.png",
                "tvg_id": "abc.us",
            }
        ],
        default_group="Importados",
        default_country="MX",
    )
    assert added == 1
    assert merged[0]["name"] == "ABC News"
    assert merged[0]["group"] == "News"
    assert merged[0]["country"] == "US"


def test_merge_channels_genera_nombres_unicos():
    existing = [
        {
            "name": "main",
            "group": "Base",
            "country": "",
            "url": "https://example.com/uno/main.m3u8",
            "logo": "",
            "tvg_id": "",
        }
    ]

    merged, added = merge_channels(
        existing,
        ["https://example.com/dos/main.m3u8"],
    )

    assert added == 1
    assert merged[1]["name"] == "main (2)"


def test_merge_channels_renombra_registros_descubiertos_con_nombre_duplicado():
    existing = [
        {
            "name": "Canal Demo",
            "group": "Base",
            "country": "MX",
            "url": "https://example.com/base.m3u8",
            "logo": "",
            "tvg_id": "",
        }
    ]

    merged, added = merge_channels(
        existing,
        [
            {
                "name": "Canal Demo",
                "group": "Importados",
                "country": "MX",
                "url": "https://example.com/nuevo.m3u8",
                "logo": "",
                "tvg_id": "canal-demo",
            }
        ],
    )

    assert added == 1
    assert merged[1]["name"] == "Canal Demo (2)"


def test_merge_channels_promueve_mirror_a_backup_url():
    existing = [
        {
            "name": "Canal Demo",
            "group": "Base",
            "country": "MX",
            "url": "https://example.com/main.m3u8",
            "logo": "",
            "tvg_id": "canal-demo",
        }
    ]

    merged, added = merge_channels(
        existing,
        [
            {
                "name": "Canal Demo",
                "group": "Base",
                "country": "MX",
                "url": "https://example.com/backup.m3u8",
                "logo": "",
                "tvg_id": "canal-demo",
            }
        ],
    )

    assert added == 1
    assert len(merged) == 1
    assert merged[0]["backup_url"] == "https://example.com/backup.m3u8"


def test_merge_channels_promueve_prioritario_a_primario_y_conserva_backup():
    existing = [
        {
            "name": "Canal 5",
            "group": "Familia y TV Abierta",
            "country": "MX",
            "url": "https://example.com/public.m3u8",
            "logo": "",
            "tvg_id": "canal5.mx",
        }
    ]

    merged, added = merge_channels(
        existing,
        [
            {
                "name": "Canal 5",
                "group": "Familia y TV Abierta",
                "country": "MX",
                "url": "https://example.com/private.m3u8",
                "logo": "",
                "tvg_id": "canal5.mx",
            }
        ],
        preferred_primary_patterns=["Canal 5"],
    )

    assert added == 1
    assert merged[0]["url"] == "https://example.com/private.m3u8"
    assert merged[0]["backup_url"] == "https://example.com/public.m3u8"


def test_merge_channels_usa_identidad_de_dial_para_unificar_aliases():
    existing = [
        {
            "name": "Canal 5 Televisa",
            "group": "Familia y TV Abierta",
            "country": "MX",
            "url": "https://example.com/public.m3u8",
            "logo": "",
            "tvg_id": "",
        }
    ]

    merged, added = merge_channels(
        existing,
        [
            {
                "name": "Canal 5 (1080p)",
                "group": "Familia y TV Abierta",
                "country": "MX",
                "url": "https://example.com/mirror.m3u8",
                "logo": "",
                "tvg_id": "",
            }
        ],
    )

    assert added == 1
    assert len(merged) == 1
    preserved_urls = {merged[0]["url"]}
    backup_value = merged[0].get("backup_url")
    if isinstance(backup_value, list):
        preserved_urls.update(backup_value)
    elif backup_value:
        preserved_urls.add(backup_value)
    assert preserved_urls == {
        "https://example.com/public.m3u8",
        "https://example.com/mirror.m3u8",
    }


def test_ensure_unique_name_agrega_sufijo_si_ya_existe():
    used_names = {"canal demo"}
    assert ensure_unique_name("Canal Demo", used_names) == "Canal Demo (2)"


def test_cache_roundtrip(tmp_path):
    url = "https://example.com/fuente.txt"
    save_cached_text(url, "contenido", cache_dir=tmp_path)
    cached = load_cached_text(url, ttl_seconds=60, cache_dir=tmp_path)
    assert cached == "contenido"


def test_load_cached_path_encuentra_cache_m3u_por_hash(tmp_path):
    from scrape_channels import cache_path_for_url, load_cached_path

    url = "https://example.com/fuente.m3u"
    cached_path = cache_path_for_url(url, tmp_path).with_suffix(".m3u")
    cached_path.write_text("#EXTM3U\n", encoding="utf-8")

    assert load_any_cached_path(url, tmp_path) == cached_path
    assert load_cached_path(url, ttl_seconds=60, cache_dir=tmp_path) == cached_path


def test_update_quarantine_entry_cuarentena_tras_tres_restrictivos():
    state: dict[str, dict[str, object]] = {}
    source_spec = {"source_env": "PRIVATE_SOURCE_1", "group": "Privados", "country": "ALL"}
    for _ in range(3):
        entry = update_quarantine_entry(
            state,
            "env:PRIVATE_SOURCE_1",
            source_spec,
            "http://example.com/private",
            status="error",
            http_status=403,
        )
    assert entry["consecutive_failures"] == 3
    assert entry["quarantined"] is True


def test_write_telemetry_report_genera_json(tmp_path):
    report_file = tmp_path / "telemetry_status.json"
    write_telemetry_report(
        [
            {"status": "success", "quarantined": False},
            {"status": "error", "quarantined": True},
        ],
        report_file,
    )
    payload = json.loads(report_file.read_text(encoding="utf-8"))
    assert payload["total_sources"] == 2
    assert payload["healthy_sources"] == 1
    assert payload["quarantined_sources"] == 1


def test_default_source_url_apunta_a_iptv_org():
    assert DEFAULT_SOURCE_URL == "https://iptv-org.github.io/iptv/countries/mx.m3u"


def test_default_metadata_url_apunta_a_iptv_org():
    assert DEFAULT_IPTVORG_CHANNELS_URL == "https://iptv-org.github.io/api/channels.json"


def test_default_secondary_sources_incluyen_mexico_y_noruega():
    assert any(source.get("country") == "MX" for source in DEFAULT_SECONDARY_SOURCES)
    assert any(source.get("country") == "NO" for source in DEFAULT_SECONDARY_SOURCES)


def test_run_omite_fuente_secundaria_caida_y_sigue_con_las_demas(tmp_path, monkeypatch, capsys):
    import scrape_channels

    sources_path = tmp_path / "channels.json"
    sources_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "secondary_sources": [
                    {"source_url": "https://bad.example/one.m3u", "group": "Importados", "country": "MX"},
                    {"source_url": "https://good.example/two.m3u", "group": "Importados", "country": "MX"},
                ]
            }
        ),
        encoding="utf-8",
    )
    local_sources_path = tmp_path / "local_private_sources.json"
    local_sources_path.write_text("[]", encoding="utf-8")

    calls: list[str] = []

    async def fake_discover_single_source(
        source_url,
        config,
        *,
        default_group,
        default_country,
        metadata_url=None,
        category_filter=None,
        max_channels=None,
        preferred_primary_patterns=None,
        curate_private=False,
    ):
        calls.append(source_url)
        if "bad.example" in source_url:
            raise Exception("getaddrinfo failed")
        return (
            [
                {
                    "name": "Canal Bueno",
                    "group": default_group,
                    "country": default_country,
                    "url": f"https://good.example/{len(calls)}.m3u8",
                    "logo": "",
                    "tvg_id": "",
                }
            ],
            1,
        )

    monkeypatch.setattr(scrape_channels, "discover_single_source", fake_discover_single_source)
    monkeypatch.setattr(scrape_channels, "LOCAL_PRIVATE_SOURCES_FILE", local_sources_path)

    added = asyncio.run(
        run(
            "https://primary.example/list.m3u",
            sources_path,
            config_path,
            default_group="Base",
            default_country="MX",
        )
    )

    assert added == 2
    assert calls == [
        "https://primary.example/list.m3u",
        "https://bad.example/one.m3u",
        "https://good.example/two.m3u",
    ]
    payload = json.loads(sources_path.read_text(encoding="utf-8"))
    assert len(payload["channels"]) == 2
    captured = capsys.readouterr()
    assert "Fuente secundaria omitida" in captured.out


def test_run_omite_fuente_local_caida_y_sigue_con_pipeline(tmp_path, monkeypatch, capsys):
    import scrape_channels

    sources_path = tmp_path / "channels.json"
    sources_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"secondary_sources": []}), encoding="utf-8")
    local_sources_path = tmp_path / "local_private_sources.json"
    local_sources_path.write_text(
        json.dumps(
            [
                {"source_env": "PRIVATE_SOURCE_1", "group": "Deportes Locales", "country": "ALL"},
                {"source_env": "PRIVATE_SOURCE_2", "group": "Deportes Locales", "country": "ALL"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRIVATE_SOURCE_1", "http://127.0.0")
    monkeypatch.setenv("PRIVATE_SOURCE_2", "http://127.0.0.1/feed.m3u")
    monkeypatch.setattr(scrape_channels, "QUARANTINE_SOURCES_FILE", tmp_path / "quarantine_sources.json")

    calls: list[str] = []

    async def fake_discover_single_source(
        source_url,
        config,
        *,
        default_group,
        default_country,
        metadata_url=None,
        category_filter=None,
        max_channels=None,
        preferred_primary_patterns=None,
        curate_private=False,
    ):
        calls.append(source_url)
        if source_url == "http://127.0.0":
            raise Exception("handshake failed")
        return (
            [
                {
                    "name": "Canal Local",
                    "group": default_group,
                    "country": default_country,
                    "url": f"http://127.0.0.1/live-{len(calls)}.m3u8",
                    "logo": "",
                    "tvg_id": "",
                }
            ],
            1,
        )

    monkeypatch.setattr(scrape_channels, "discover_single_source", fake_discover_single_source)
    monkeypatch.setattr(scrape_channels, "LOCAL_PRIVATE_SOURCES_FILE", local_sources_path)

    added = asyncio.run(
        run(
            "https://primary.example/list.m3u",
            sources_path,
            config_path,
            default_group="Base",
            default_country="MX",
        )
    )

    assert added == 2
    assert calls == [
        "https://primary.example/list.m3u",
        "http://127.0.0",
        "http://127.0.0.1/feed.m3u",
    ]
    payload = json.loads(sources_path.read_text(encoding="utf-8"))
    assert len(payload["channels"]) == 2
    captured = capsys.readouterr()
    assert "Fuente local omitida" in captured.out


def test_run_reporta_403_local_como_rechazo_de_origen(tmp_path, monkeypatch, capsys):
    import scrape_channels

    sources_path = tmp_path / "channels.json"
    sources_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"secondary_sources": []}), encoding="utf-8")
    local_sources_path = tmp_path / "local_private_sources.json"
    local_sources_path.write_text(
        json.dumps(
            [
                {"source_env": "PRIVATE_SOURCE_1", "group": "Privados", "country": "ALL"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRIVATE_SOURCE_1", "http://provider.example/forbidden")
    monkeypatch.setattr(scrape_channels, "QUARANTINE_SOURCES_FILE", tmp_path / "quarantine_sources.json")

    async def fake_discover_single_source(
        source_url,
        config,
        *,
        default_group,
        default_country,
        metadata_url=None,
        category_filter=None,
        max_channels=None,
        preferred_primary_patterns=None,
        curate_private=False,
    ):
        if source_url == "https://primary.example/list.m3u":
            return ([], 1)
        raise scrape_channels.aiohttp.ClientResponseError(
            request_info=SimpleNamespace(real_url=source_url),
            history=(),
            status=403,
            message="Acceso prohibido por el origen (403).",
            headers=None,
        )

    monkeypatch.setattr(scrape_channels, "discover_single_source", fake_discover_single_source)
    monkeypatch.setattr(scrape_channels, "LOCAL_PRIVATE_SOURCES_FILE", local_sources_path)

    asyncio.run(
        run(
            "https://primary.example/list.m3u",
            sources_path,
            config_path,
            default_group="Base",
            default_country="MX",
        )
    )

    captured = capsys.readouterr()
    assert "Fuente local rechazada por el origen" in captured.out


def test_run_deduplica_fuentes_repetidas_entre_config_y_archivo_local(tmp_path, monkeypatch):
    import scrape_channels

    sources_path = tmp_path / "channels.json"
    sources_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    repeated_url = "http://provider.example/get.php?username=u&password=p&type=m3u_plus"
    config_path.write_text(
        json.dumps(
            {
                "secondary_sources": [
                    {"source_url": repeated_url, "group": "Importados", "country": "ALL"},
                ]
            }
        ),
        encoding="utf-8",
    )
    local_sources_path = tmp_path / "local_private_sources.json"
    local_sources_path.write_text(
        json.dumps(
            [
                {"source_url": repeated_url, "group": "Privados", "country": "ALL"},
            ]
        ),
        encoding="utf-8",
    )

    calls: list[str] = []

    async def fake_discover_single_source(
        source_url,
        config,
        *,
        default_group,
        default_country,
        metadata_url=None,
        category_filter=None,
        max_channels=None,
        preferred_primary_patterns=None,
        curate_private=False,
    ):
        calls.append(source_url)
        return ([], 1)

    monkeypatch.setattr(scrape_channels, "discover_single_source", fake_discover_single_source)
    monkeypatch.setattr(scrape_channels, "LOCAL_PRIVATE_SOURCES_FILE", local_sources_path)

    asyncio.run(
        run(
            "https://primary.example/list.m3u",
            sources_path,
            config_path,
            default_group="Base",
            default_country="MX",
        )
    )

    assert calls == [
        "https://primary.example/list.m3u",
        repeated_url,
    ]


def test_run_inyecta_discovered_mirrors_antes_de_deduplicar(tmp_path, monkeypatch):
    import scrape_channels

    sources_path = tmp_path / "channels.json"
    sources_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"secondary_sources": []}), encoding="utf-8")
    local_sources_path = tmp_path / "local_private_sources.json"
    local_sources_path.write_text("[]", encoding="utf-8")
    mirrors_path = tmp_path / "discovered_mirrors.json"
    mirrors_path.write_text(
        json.dumps(
            {
                "105": [
                    {"url": "https://mirror.example/canal5.m3u8", "geo": "MX", "verified": False},
                ]
            }
        ),
        encoding="utf-8",
    )

    async def fake_discover_single_source(
        source_url,
        config,
        *,
        default_group,
        default_country,
        metadata_url=None,
        category_filter=None,
        max_channels=None,
        preferred_primary_patterns=None,
        curate_private=False,
    ):
        return ([], 0)

    monkeypatch.setattr(scrape_channels, "discover_single_source", fake_discover_single_source)
    monkeypatch.setattr(scrape_channels, "LOCAL_PRIVATE_SOURCES_FILE", local_sources_path)
    monkeypatch.setattr(scrape_channels, "DISCOVERED_MIRRORS_FILE", mirrors_path)

    added = asyncio.run(
        run(
            "https://primary.example/list.m3u",
            sources_path,
            config_path,
            default_group="Base",
            default_country="MX",
        )
    )

    assert added == 1
    payload = json.loads(sources_path.read_text(encoding="utf-8"))
    assert payload["channels"][0]["url"] == "https://mirror.example/canal5.m3u8"
    assert payload["channels"][0]["tvg_id"] == "CANAL_5_LOCAL_HD"


def test_run_genera_telemetria_y_cuarentena_tras_tres_403(tmp_path, monkeypatch):
    import scrape_channels

    sources_path = tmp_path / "channels.json"
    sources_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"secondary_sources": []}), encoding="utf-8")
    local_sources_path = tmp_path / "local_private_sources.json"
    local_sources_path.write_text(
        json.dumps(
            [
                {"source_env": "PRIVATE_SOURCE_1", "group": "Privados", "country": "ALL"},
            ]
        ),
        encoding="utf-8",
    )
    telemetry_path = tmp_path / "telemetry_status.json"
    quarantine_path = tmp_path / "quarantine_sources.json"

    monkeypatch.setenv("PRIVATE_SOURCE_1", "http://provider.example/forbidden")
    monkeypatch.setattr(scrape_channels, "LOCAL_PRIVATE_SOURCES_FILE", local_sources_path)
    monkeypatch.setattr(scrape_channels, "TELEMETRY_STATUS_FILE", telemetry_path)
    monkeypatch.setattr(scrape_channels, "QUARANTINE_SOURCES_FILE", quarantine_path)

    async def fake_discover_single_source(
        source_url,
        config,
        *,
        default_group,
        default_country,
        metadata_url=None,
        category_filter=None,
        max_channels=None,
        preferred_primary_patterns=None,
        curate_private=False,
    ):
        if source_url == "https://primary.example/list.m3u":
            return ([], 1)
        raise scrape_channels.aiohttp.ClientResponseError(
            request_info=SimpleNamespace(real_url=source_url),
            history=(),
            status=403,
            message="Acceso prohibido por el origen (403).",
            headers=None,
        )

    monkeypatch.setattr(scrape_channels, "discover_single_source", fake_discover_single_source)

    for _ in range(3):
        asyncio.run(
            run(
                "https://primary.example/list.m3u",
                sources_path,
                config_path,
                default_group="Base",
                default_country="MX",
            )
        )

    telemetry_payload = json.loads(telemetry_path.read_text(encoding="utf-8"))
    quarantine_payload = json.loads(quarantine_path.read_text(encoding="utf-8"))
    assert telemetry_payload["total_sources"] == 1
    assert telemetry_payload["sources"][0]["consecutive_failures"] == 3
    assert telemetry_payload["sources"][0]["quarantined"] is True
    assert quarantine_payload["env:PRIVATE_SOURCE_1"]["quarantined"] is True
