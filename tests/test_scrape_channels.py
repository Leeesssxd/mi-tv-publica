"""
Pruebas unitarias para scrape_channels.py

Cubren la extraccion por regex y el merge en channels.json sin hacer
peticiones HTTP reales.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scrape_channels import (  # noqa: E402
    DEFAULT_IPTVORG_CHANNELS_URL,
    DEFAULT_LOCAL_PRIVATE_SOURCES,
    DEFAULT_SECONDARY_SOURCES,
    DEFAULT_SOURCE_URL,
    detect_payload_kind,
    ensure_local_private_sources_file,
    ensure_unique_name,
    extract_m3u8_links,
    extract_text_links_from_file,
    file_looks_like_m3u,
    file_looks_like_json,
    infer_country,
    iter_text_chunks,
    load_cached_text,
    merge_channels,
    normalize_url,
    parse_json_teles_channel_json,
    parse_extinf_line,
    parse_generic_channel_json,
    parse_iptv_org_streams,
    parse_m3u_file,
    run,
    save_cached_text,
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
        }
    ]


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


def test_ensure_unique_name_agrega_sufijo_si_ya_existe():
    used_names = {"canal demo"}
    assert ensure_unique_name("Canal Demo", used_names) == "Canal Demo (2)"


def test_cache_roundtrip(tmp_path):
    url = "https://example.com/fuente.txt"
    save_cached_text(url, "contenido", cache_dir=tmp_path)
    cached = load_cached_text(url, ttl_seconds=60, cache_dir=tmp_path)
    assert cached == "contenido"


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

    async def fake_import_single_source(
        source_url,
        sources_path,
        config,
        *,
        default_group,
        default_country,
        metadata_url=None,
        category_filter=None,
    ):
        calls.append(source_url)
        if "bad.example" in source_url:
            raise Exception("getaddrinfo failed")
        payload = json.loads(sources_path.read_text(encoding="utf-8"))
        payload["channels"].append(
            {
                "name": "Canal Bueno",
                "group": default_group,
                "country": default_country,
                "url": "https://good.example/live.m3u8",
                "logo": "",
                "tvg_id": "",
            }
        )
        sources_path.write_text(json.dumps(payload), encoding="utf-8")
        return 1, 1

    monkeypatch.setattr(scrape_channels, "import_single_source", fake_import_single_source)
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
                {"source_url": "http://127.0.0", "group": "Deportes Locales", "country": "ALL"},
                {"source_url": "http://127.0.0.1/feed.m3u", "group": "Deportes Locales", "country": "ALL"},
            ]
        ),
        encoding="utf-8",
    )

    calls: list[str] = []

    async def fake_import_single_source(
        source_url,
        sources_path,
        config,
        *,
        default_group,
        default_country,
        metadata_url=None,
        category_filter=None,
    ):
        calls.append(source_url)
        if source_url == "http://127.0.0":
            raise Exception("handshake failed")
        payload = json.loads(sources_path.read_text(encoding="utf-8"))
        payload["channels"].append(
            {
                "name": "Canal Local",
                "group": default_group,
                "country": default_country,
                "url": "http://127.0.0.1/live.m3u8",
                "logo": "",
                "tvg_id": "",
            }
        )
        sources_path.write_text(json.dumps(payload), encoding="utf-8")
        return 1, 1

    monkeypatch.setattr(scrape_channels, "import_single_source", fake_import_single_source)
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
