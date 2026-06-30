import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from generate_trending_vod import (  # noqa: E402
    CLOUD_GROUP_NAME,
    DEFAULT_EPISODE,
    DEFAULT_SEASON,
    TEMPLATE_MOVIE,
    TEMPLATE_TV,
    TRENDING_VOD,
    build_templated_url,
    build_vod_item,
    load_env_overrides,
    normalize_media_type,
    resolve_templates,
    run,
)


def test_trending_vod_contiene_50_titulos():
    assert len(TRENDING_VOD) == 50


def test_resolve_templates_usa_defaults_inocuos_si_no_hay_config(monkeypatch):
    monkeypatch.delenv("VOD_TEMPLATE_MOVIE", raising=False)
    monkeypatch.delenv("VOD_TEMPLATE_TV", raising=False)
    monkeypatch.setattr("generate_trending_vod.load_env_overrides", lambda env_path=None: {})

    movie_template, tv_template = resolve_templates({})

    assert movie_template == TEMPLATE_MOVIE
    assert tv_template == TEMPLATE_TV


def test_resolve_templates_acepta_override_desde_config(monkeypatch):
    monkeypatch.setattr("generate_trending_vod.load_env_overrides", lambda env_path=None: {})
    movie_template, tv_template = resolve_templates(
        {
            "vod_template_movie": "https://vod.example/movie/{id}",
            "vod_template_tv": "https://vod.example/tv/{id}",
        }
    )

    assert movie_template == "https://vod.example/movie/{id}"
    assert tv_template == "https://vod.example/tv/{id}"


def test_resolve_templates_acepta_claves_mayusculas_desde_config(monkeypatch):
    monkeypatch.setattr("generate_trending_vod.load_env_overrides", lambda env_path=None: {})
    movie_template, tv_template = resolve_templates(
        {
            "VOD_TEMPLATE_MOVIE": "https://vod.example/movie/{id}",
            "VOD_TEMPLATE_TV": "https://vod.example/tv/{id}",
        }
    )

    assert movie_template == "https://vod.example/movie/{id}"
    assert tv_template == "https://vod.example/tv/{id}"


def test_load_env_overrides_lee_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'VOD_TEMPLATE_MOVIE=https://env.example/movie/{id}\n'
        'VOD_TEMPLATE_TV="https://env.example/tv/{id}"\n',
        encoding="utf-8",
    )

    overrides = load_env_overrides(env_file)

    assert overrides["VOD_TEMPLATE_MOVIE"] == "https://env.example/movie/{id}"
    assert overrides["VOD_TEMPLATE_TV"] == "https://env.example/tv/{id}"


def test_resolve_templates_acepta_custom_routing_rules(monkeypatch):
    monkeypatch.delenv("VOD_TEMPLATE_MOVIE", raising=False)
    monkeypatch.delenv("VOD_TEMPLATE_TV", raising=False)
    monkeypatch.setattr("generate_trending_vod.load_env_overrides", lambda env_path=None: {})

    movie_template, tv_template = resolve_templates(
        {
            "custom_routing_rules": {
                "cloud_catalog": {
                    "vod_template_movie": "https://routing.example/movie/{id}",
                    "vod_template_tv": "https://routing.example/tv/{id}",
                }
            }
        }
    )

    assert movie_template == "https://routing.example/movie/{id}"
    assert tv_template == "https://routing.example/tv/{id}"


def test_build_templated_url_formatea_movie_y_series():
    movie_url = build_templated_url(
        {"media_type": "movie", "tmdb_id": "123"},
        "https://example.test/movie/{id}?s={season}&e={episode}",
        "https://example.test/tv/{id}?s={season}&e={episode}",
    )
    series_url = build_templated_url(
        {"media_type": "series", "tmdb_id": "456"},
        "https://example.test/movie/{id}?s={season}&e={episode}",
        "https://example.test/tv/{id}?s={season}&e={episode}",
    )

    assert movie_url == f"https://example.test/movie/123?s={DEFAULT_SEASON}&e={DEFAULT_EPISODE}"
    assert series_url == f"https://example.test/tv/456?s={DEFAULT_SEASON}&e={DEFAULT_EPISODE}"


def test_normalize_media_type_distingue_movie_y_series():
    assert normalize_media_type({"media_type": "movie"}) == "movie"
    assert normalize_media_type({"media_type": "series"}) == "series"
    assert normalize_media_type({"media_type": "tv"}) == "series"


def test_build_vod_item_genera_ruteo_template_y_enlaces_legales_de_referencia():
    item = build_vod_item(TRENDING_VOD[0])
    assert item["group"] == CLOUD_GROUP_NAME
    assert item["availability"] == "templated_routing"
    assert item["url"].startswith("https://localhost/")
    assert item["routing_mode"] == "placeholder"
    assert item["reference_url"].startswith("https://www.themoviedb.org/")
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
    assert payload["cloud_catalog"]["items"][0]["availability"] == "templated_routing"
