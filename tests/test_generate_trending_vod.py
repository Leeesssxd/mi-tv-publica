import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from generate_trending_vod import (  # noqa: E402
    CLOUD_GROUP_NAME,
    TRENDING_VOD,
    build_vod_item,
    run,
)


def test_trending_vod_contiene_50_titulos():
    assert len(TRENDING_VOD) == 50


def test_build_vod_item_genera_enlaces_legales_de_referencia():
    item = build_vod_item(TRENDING_VOD[0])
    assert item["group"] == CLOUD_GROUP_NAME
    assert item["availability"] == "metadata_only"
    assert item["url"].startswith("https://www.themoviedb.org/")
    assert item["imdb_url"].startswith("https://www.imdb.com/title/")
    assert item["trailer_search_url"].startswith("https://www.youtube.com/results?")


def test_run_actualiza_cloud_catalog_en_payload_dict(tmp_path):
    sources_path = tmp_path / "channels.json"
    sources_path.write_text(
        json.dumps({"channels": [{"name": "Base", "group": "General", "country": "MX", "url": "https://a.com"}]}),
        encoding="utf-8",
    )

    loaded = run(sources_path)
    payload = json.loads(sources_path.read_text(encoding="utf-8"))

    assert loaded == 50
    assert payload["cloud_catalog"]["name"] == CLOUD_GROUP_NAME
    assert len(payload["cloud_catalog"]["items"]) == 50
