import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from cloud_vod import (  # noqa: E402
    CLOUD_GROUP_NAME,
    build_authenticated_stream_url,
    clean_media_title,
    extract_media_entries,
    upsert_cloud_catalog,
)


def test_clean_media_title_limpia_extension_y_separadores():
    assert clean_media_title("Mi.Pelicula_1080p.mkv") == "Mi Pelicula 1080p"


def test_extract_media_entries_recoge_archivos_de_arbol_virtual():
    payload = {
        "items": [
            {
                "name": "Peliculas",
                "type": "directory",
                "children": [
                    {"name": "Film One.mp4", "path": "movies/Film One.mp4", "id": "1"},
                    {"name": "Season 1", "type": "folder", "children": []},
                ],
            },
            {"name": "Serie.mkv", "path": "series/Serie.mkv", "id": "2"},
        ]
    }

    items = extract_media_entries(payload)

    assert [item["id"] for item in items] == ["1", "2"]


def test_build_authenticated_stream_url_reemplaza_placeholders():
    item = {"path": "movies/Film One.mp4", "id": "abc123"}
    url = build_authenticated_stream_url(
        "https://cloud.example/stream/{path}?token={token}&id={id}",
        "secret",
        item,
    )
    assert url == "https://cloud.example/stream/movies/Film%20One.mp4?token=secret&id=abc123"


def test_build_authenticated_stream_url_agrega_query_si_no_hay_placeholders():
    item = {"path": "series/Episode 1.mkv", "id": "ep1"}
    url = build_authenticated_stream_url("https://cloud.example/stream", "secret", item)
    assert "token=secret" in url
    assert "id=ep1" in url
    assert "path=series%2FEpisode+1.mkv" in url


def test_upsert_cloud_catalog_convierte_lista_plana_a_payload_compatible():
    payload = [{"name": "Base", "group": "General", "country": "MX", "url": "https://a.com/live.m3u8"}]
    updated = upsert_cloud_catalog(
        payload,
        [{"name": "Movie One", "group": CLOUD_GROUP_NAME, "country": "ZZ", "url": "https://cloud.example/a"}],
    )

    assert isinstance(updated, dict)
    assert "channels" in updated
    assert updated["cloud_catalog"]["name"] == CLOUD_GROUP_NAME
    assert updated["cloud_catalog"]["items"][0]["name"] == "Movie One"
