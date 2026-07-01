"""
Pruebas unitarias para build_playlist.py

No requieren red: prueban la lógica pura (parseo de canales, generación de
M3U/Markdown/JSON, orden y limpieza de campos) usando objetos ChannelStatus
construidos a mano en lugar de hacer peticiones HTTP reales.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_playlist import (  # noqa: E402
    Channel,
    ChannelStatus,
    VodStatus,
    build_m3u,
    build_priority_summary,
    build_priority_summary_markdown,
    build_vod_browser_links,
    build_status_json,
    build_status_markdown,
    build_vod_m3u,
    build_vod_status_json,
    build_vod_status_markdown,
    classify_group,
    has_playable_channels,
    _classify_vod_transport,
    load_channels,
    load_cloud_catalog_items,
    load_config,
    regroup_statuses,
    select_curated_statuses,
    sort_statuses,
    write_vod_output,
    write_outputs,
)


# ---------------------------------------------------------------------------
# Fixtures auxiliares
# ---------------------------------------------------------------------------

def make_status(name: str, group: str, alive: bool, **kwargs) -> ChannelStatus:
    defaults = dict(
        country="MX",
        url=f"https://example.com/{name.lower().replace(' ', '-')}.m3u8",
        backup_urls=[],
        logo="",
        tvg_id="",
        status_code=200 if alive else 404,
        error=None if alive else "Not Found",
        state="alive" if alive else "dead",
    )
    defaults.update(kwargs)
    return ChannelStatus(name=name, group=group, alive=alive, **defaults)


# ---------------------------------------------------------------------------
# Channel.from_dict / load_channels
# ---------------------------------------------------------------------------

def test_channel_from_dict_valida_campos_obligatorios():
    channel = Channel.from_dict({"name": "Canal A", "url": "https://a.com/x.m3u8"})
    assert channel.name == "Canal A"
    assert channel.group == "General"  # valor por defecto
    assert channel.country == ""


def test_channel_from_dict_acepta_backup_url_y_lista_de_urls():
    channel = Channel.from_dict(
        {
            "name": "Canal A",
            "url": ["https://a.com/main.m3u8", "https://a.com/mirror.m3u8"],
            "backup_url": "https://a.com/failover.m3u8",
        }
    )

    assert channel.url == "https://a.com/main.m3u8"
    assert channel.backup_urls == ["https://a.com/mirror.m3u8", "https://a.com/failover.m3u8"]


def test_channel_from_dict_normaliza_nombre_canonico_de_tv_abierta():
    channel = Channel.from_dict(
        {
            "name": "Canal 5 (1080p)",
            "url": "https://a.com/main.m3u8",
            "tvg_id": "Canal5.mx@SD",
        }
    )

    assert channel.name == "Canal 5 Televisa"


def test_channel_from_dict_rechaza_sin_name():
    with pytest.raises(ValueError):
        Channel.from_dict({"url": "https://a.com/x.m3u8"})


def test_channel_from_dict_rechaza_sin_url():
    with pytest.raises(ValueError):
        Channel.from_dict({"name": "Canal A"})


def test_channel_from_dict_rechaza_url_invalida():
    with pytest.raises(ValueError):
        Channel.from_dict({"name": "Canal A", "url": "ftp://a.com/x.m3u8"})


def test_load_channels_ignora_entradas_invalidas(tmp_path, capsys):
    data = [
        {"name": "Canal Bueno", "url": "https://a.com/x.m3u8"},
        {"name": "", "url": "https://b.com/x.m3u8"},  # inválido: sin nombre
        {"name": "Canal Sin URL"},                      # inválido: sin url
    ]
    sources_file = tmp_path / "channels.json"
    sources_file.write_text(json.dumps(data), encoding="utf-8")

    channels = load_channels(sources_file)

    assert len(channels) == 1
    assert channels[0].name == "Canal Bueno"
    captured = capsys.readouterr()
    assert "se omite" in captured.out


def test_load_channels_acepta_payload_multinivel_y_deduplica_por_url(tmp_path):
    data = {
        "channels": [
            {"name": "Base", "url": "https://a.com/x.m3u8", "group": "General"},
        ],
        "regional_tiers": {
            "strict_verified": [
                {"name": "DeporTV Oficial", "url": "https://b.com/live.m3u8", "group": "Deportes Públicos Internacionales"},
            ],
            "flex_verified": [
                {"name": "Duplicado", "url": "https://a.com/x.m3u8", "group": "Deportes Públicos Internacionales"},
            ],
        },
    }
    sources_file = tmp_path / "channels.json"
    sources_file.write_text(json.dumps(data), encoding="utf-8")

    channels = load_channels(sources_file)

    assert [channel.name for channel in channels] == ["Base", "DeporTV Oficial"]


def test_load_channels_omite_items_metadata_only_del_catalogo_cloud(tmp_path):
    data = {
        "channels": [
            {"name": "Base", "url": "https://a.com/x.m3u8", "group": "General"},
        ],
        "cloud_catalog": {
            "name": "Mi Catálogo Cloud",
            "items": [
                {
                    "name": "Movie One (2024)",
                    "url": "https://www.themoviedb.org/movie/1",
                    "group": "Mi Catálogo Cloud",
                    "availability": "metadata_only",
                }
            ],
        },
    }
    sources_file = tmp_path / "channels.json"
    sources_file.write_text(json.dumps(data), encoding="utf-8")

    channels = load_channels(sources_file)

    assert [channel.name for channel in channels] == ["Base"]


def test_load_channels_omite_items_templated_routing_y_localhost(tmp_path):
    data = {
        "channels": [
            {"name": "Base", "url": "https://a.com/x.m3u8", "group": "General"},
            {
                "name": "Serie Template",
                "url": "https://localhost/tv/456/1/1",
                "group": "Mi Catálogo Cloud",
                "availability": "templated_routing",
            },
        ]
    }
    sources_file = tmp_path / "channels.json"
    sources_file.write_text(json.dumps(data), encoding="utf-8")

    channels = load_channels(sources_file)

    assert [channel.name for channel in channels] == ["Base"]


def test_load_channels_conserva_items_templated_routing_configurados(tmp_path):
    data = {
        "channels": [
            {
                "name": "Movie Template Configurado",
                "url": "https://vod.example/movie/123",
                "group": "Mi Catálogo Cloud",
                "availability": "templated_routing",
            }
        ]
    }
    sources_file = tmp_path / "channels.json"
    sources_file.write_text(json.dumps(data), encoding="utf-8")

    channels = load_channels(sources_file)

    assert [channel.name for channel in channels] == ["Movie Template Configurado"]


def test_load_cloud_catalog_items_extrae_items_del_bloque_cloud(tmp_path):
    sources_file = tmp_path / "channels.json"
    sources_file.write_text(
        json.dumps(
            {
                "channels": [{"name": "Base", "url": "https://a.com/x.m3u8"}],
                "cloud_catalog": {
                    "items": [
                        {"name": "Movie One (2024)", "url": "https://vod.example/movie/1"},
                        {"name": "Series One (2024)", "url": "https://vod.example/tv/2/1/1"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    items = load_cloud_catalog_items(sources_file)

    assert [item["name"] for item in items] == ["Movie One (2024)", "Series One (2024)"]


def test_select_curated_statuses_respeta_cupos_y_rellena_hasta_objetivo():
    statuses = [
        make_status("Canal 5", "Familia y TV Abierta", True),
        make_status("Azteca 7", "Familia y TV Abierta", True),
        make_status("ESPN", "Deportes", True),
        make_status("Fox Sports", "Deportes", True),
        make_status("Golden", "Peliculas - Cine", True),
        make_status("Runtime Cine y Series", "Peliculas - Drama y Series", True),
        make_status("Milenio", "Noticias", True),
        make_status("Telehit", "Entretenimiento", True),
        make_status("Otro 1", "Otros", True),
        make_status("Otro 2", "Otros", True),
    ]

    selected = select_curated_statuses(
        statuses,
        target_size=6,
        group_quotas={
            "Familia y TV Abierta": 2,
            "Deportes": 1,
            "Peliculas - Cine": 1,
            "Noticias": 1,
            "Entretenimiento": 1,
        },
    )

    assert len(selected) == 6
    assert any(status.group == "Familia y TV Abierta" for status in selected)
    assert any(status.group == "Deportes" for status in selected)
    assert any(status.group == "Peliculas - Cine" for status in selected)


def test_select_curated_statuses_conserva_prioritarios_antes_del_recorte():
    statuses = [
        make_status("Azteca Uno", "Familia y TV Abierta", True),
        make_status("Azteca 7", "Familia y TV Abierta", True),
        make_status("Canal 5 (1080p)", "Familia y TV Abierta", True),
        make_status("Las Estrellas HD", "Familia y TV Abierta", True),
        make_status("TUDN (1080p)", "Deportes", True),
        make_status("Otro 1", "Otros", True),
        make_status("Otro 2", "Otros", True),
    ]

    selected = select_curated_statuses(
        statuses,
        target_size=4,
        group_quotas={"Familia y TV Abierta": 1, "Deportes": 1, "Otros": 1},
        priority_channels=["Azteca Uno", "Canal 5", "Las Estrellas", "TUDN"],
    )

    assert [status.name for status in selected] == [
        "Azteca Uno",
        "Canal 5 (1080p)",
        "Las Estrellas HD",
        "TUDN (1080p)",
    ]


def test_load_config_conserva_custom_routing_rules_si_es_dict(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"custom_routing_rules": {"catalog_cloud": {"enabled": True}}}),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config["custom_routing_rules"] == {"catalog_cloud": {"enabled": True}}


def test_load_config_ignora_custom_routing_rules_invalido(tmp_path, capsys):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"custom_routing_rules": ["bad"]}), encoding="utf-8")

    config = load_config(config_file)

    assert config["custom_routing_rules"] == {}
    captured = capsys.readouterr()
    assert "custom_routing_rules invalido" in captured.out


# ---------------------------------------------------------------------------
# Orden de canales
# ---------------------------------------------------------------------------

def test_sort_statuses_por_grupo_y_nombre():
    statuses = [
        make_status("Zeta", "B", True),
        make_status("Alfa", "A", True),
        make_status("Beta", "A", True),
    ]
    ordered = sort_statuses(statuses, ["group", "name"])
    names = [s.name for s in ordered]
    assert names == ["Alfa", "Beta", "Zeta"]


def test_sort_statuses_respeta_group_order():
    statuses = [
        make_status("Runtime Acción", "Peliculas - Accion", True),
        make_status("Runtime Crimen", "Peliculas - Crimen", True),
        make_status("Milenio", "Noticias", True),
        make_status("Azteca Uno", "Familia y TV Abierta", True),
    ]

    ordered = sort_statuses(
        statuses,
        ["group", "name"],
        group_order=["Familia y TV Abierta", "Noticias", "Peliculas - Accion", "Peliculas - Crimen"],
    )

    assert [status.name for status in ordered] == ["Azteca Uno", "Milenio", "Runtime Acción", "Runtime Crimen"]


def test_sort_statuses_prioriza_canales_configurados_y_mejor_calidad():
    statuses = [
        make_status("Milenio Televisión (720p)", "News", True),
        make_status("Canal 5 (720p)", "General", True),
        make_status("Canal 5 (1080p)", "General", True),
        make_status("Otro Canal (1080p)", "General", True),
    ]

    ordered = sort_statuses(
        statuses,
        ["group", "name"],
        priority_channels=["Canal 5", "Milenio"],
    )

    names = [s.name for s in ordered]
    assert names == [
        "Canal 5 (1080p)",
        "Canal 5 (720p)",
        "Milenio Televisión (720p)",
        "Otro Canal (1080p)",
    ]


def test_sort_statuses_prefiere_vivos_y_mx_entre_empates():
    statuses = [
        make_status("Canal 5 (1080p)", "Familia y TV Abierta", False, state="unstable", status_code=200, error="Handshake correcto"),
        make_status("Canal 5 (1080p)", "Familia y TV Abierta", True, country="ALL"),
        make_status("Canal 5 (1080p)", "Familia y TV Abierta", True, country="MX"),
    ]

    ordered = sort_statuses(
        statuses,
        ["group", "name"],
        priority_channels=["Canal 5"],
    )

    assert [(status.country, status.state) for status in ordered] == [
        ("MX", "alive"),
        ("ALL", "alive"),
        ("MX", "unstable"),
    ]


def test_sort_statuses_usa_aliases_de_prioridad_para_dsports():
    statuses = [
        make_status("DSPORTPLUS", "Entretenimiento", True, country="ALL"),
        make_status("Otro Canal", "Entretenimiento", True),
    ]

    ordered = sort_statuses(
        statuses,
        ["group", "name"],
        priority_channels=["DSPORTS"],
    )

    assert [status.name for status in ordered] == ["DSPORTPLUS", "Otro Canal"]


def test_sort_statuses_prefiere_nombre_principal_sobre_version_regional():
    statuses = [
        make_status("Canal 5 TV Cozumel (1080p)", "Familia y TV Abierta", True),
        make_status("Canal 5 Televisa", "Familia y TV Abierta", True),
    ]

    ordered = sort_statuses(
        statuses,
        ["group", "name"],
        priority_channels=["Canal 5"],
    )

    assert [status.name for status in ordered] == ["Canal 5 Televisa", "Canal 5 TV Cozumel (1080p)"]


def test_sort_statuses_respeta_orden_fijo_de_catalogo():
    statuses = [
        make_status("Jalisco TV (720p)", "Familia y TV Abierta", True),
        make_status("Azteca Uno", "Familia y TV Abierta", True),
        make_status("Canal 5 Televisa", "Familia y TV Abierta", True),
        make_status("Las Estrellas", "Familia y TV Abierta", True),
    ]

    ordered = sort_statuses(statuses, ["group", "name"])

    assert [status.name for status in ordered] == [
        "Azteca Uno",
        "Las Estrellas",
        "Canal 5 Televisa",
        "Jalisco TV (720p)",
    ]


def test_sort_statuses_inserta_bloque_mundial_despues_de_azteca_7():
    statuses = [
        make_status("Azteca 7", "Familia y TV Abierta", True),
        make_status("TUDN", "Deportes", True),
        make_status("Claro Sports (720p)", "Deportes", True),
        make_status("Jalisco TV (720p)", "Familia y TV Abierta", True),
    ]

    ordered = sort_statuses(statuses, ["group", "name"])

    names = [status.name for status in ordered]
    assert names[0] == "Azteca 7"
    assert names.index("TUDN") < names.index("Jalisco TV (720p)")
    assert names.index("Claro Sports (720p)") < names.index("Jalisco TV (720p)")


def test_classify_group_ubica_canales_en_secciones_usuario():
    assert classify_group("Azteca Uno", "General") == "Familia y TV Abierta"
    assert classify_group("Milenio Televisión (720p)", "News") == "Noticias"
    assert classify_group("Runtime Acción", "Movies") == "Peliculas - Accion"
    assert classify_group("Runtime Comedia", "Comedy") == "Peliculas - Comedia"
    assert classify_group("Runtime Crimen", "Movies") == "Peliculas - Crimen"
    assert classify_group("Runtime Terror", "Movies") == "Peliculas - Terror"
    assert classify_group("Runtime Familia", "Family") == "Peliculas - Familiar"
    assert classify_group("AyM Sports", "Sports") == "Deportes"
    assert classify_group("Imagen TV+ (720p)", "General") == "Familia y TV Abierta"


def test_regroup_statuses_reescribe_grupos_para_salida():
    statuses = [
        make_status("Azteca 7", "General", True),
        make_status("Telediario Now", "News", True),
    ]

    regrouped = regroup_statuses(statuses)

    assert [status.group for status in regrouped] == ["Familia y TV Abierta", "Noticias"]


def test_classify_group_respeta_grupo_canonico_manual():
    assert classify_group("TyC Sports", "Deportes") == "Deportes"
    assert classify_group("La Nacion +", "Noticias") == "Noticias"
    assert classify_group("RTVE La 1 Oficial (720p)", "Deportes Públicos Internacionales") == "Deportes Públicos Internacionales"


def test_classify_group_reconoce_noticias_y_tv_abierta_adicionales():
    assert classify_group("TN", "General") == "Noticias"
    assert classify_group("Canal 26", "General") == "Noticias"
    assert classify_group("America TV", "General") == "Familia y TV Abierta"


def test_classify_group_reconoce_aliases_extra_de_usuario():
    assert classify_group("Azteca Internacional (1080p)", "Entertainment") == "Familia y TV Abierta"
    assert classify_group("Once México (1080p)", "Entertainment") == "Familia y TV Abierta"
    assert classify_group("Multimedios Monterrey (720p)", "Entertainment") == "Noticias"
    assert classify_group("Golden (240p)", "Entertainment") == "Peliculas - Cine"
    assert classify_group("DSPORTPLUS", "Entertainment") == "Deportes"


def test_classify_group_reconoce_mas_tv_publica_regional():
    assert classify_group("Canal 13 Puebla (720p)", "Entertainment") == "Familia y TV Abierta"
    assert classify_group("ICRTV Colima (1080p)", "Other") == "Familia y TV Abierta"
    assert classify_group("TV Mar La Paz (1080p)", "Entertainment") == "Familia y TV Abierta"
    assert classify_group("Unison TV (1080p)", "Entertainment") == "Familia y TV Abierta"
    assert classify_group("Antena TV", "Entertainment") == "Familia y TV Abierta"


# ---------------------------------------------------------------------------
# Generación de M3U
# ---------------------------------------------------------------------------

def test_build_m3u_solo_incluye_canales_vivos():
    statuses = [
        make_status("Vivo", "Grupo", True),
        make_status("Muerto", "Grupo", False),
    ]
    m3u = build_m3u(statuses)
    assert "Vivo" in m3u
    assert "Muerto" not in m3u
    assert m3u.startswith("#EXTM3U")


def test_build_m3u_tambien_incluye_canales_inestables():
    statuses = [
        make_status("Inestable", "Grupo", False, state="unstable", status_code=200, error="Handshake correcto"),
        make_status("Muerto", "Grupo", False),
    ]
    m3u = build_m3u(statuses)
    assert "Inestable" in m3u
    assert "Muerto" not in m3u


def test_build_m3u_expone_urls_de_respaldo_como_entradas_visibles():
    statuses = [
        make_status(
            "Canal 5 (1080p)",
            "Familia y TV Abierta",
            True,
            url="https://example.com/main.m3u8",
            backup_urls=["https://example.com/backup.m3u8"],
        )
    ]

    m3u = build_m3u(statuses)

    assert "Canal 5 (1080p) [Respaldo 1]" in m3u
    assert "https://example.com/backup.m3u8" in m3u


def test_build_priority_summary_reporta_encontrados_y_faltantes():
    statuses = [
        make_status("Azteca Uno", "Familia y TV Abierta", True),
        make_status("DSPORTPLUS", "Deportes", True, country="ALL"),
    ]

    summary = build_priority_summary(statuses, ["Azteca Uno", "DSPORTS", "ViX"])

    assert summary["found_total"] == 2
    assert summary["missing_total"] == 1
    assert summary["missing"] == ["ViX"]
    assert summary["found"][1]["matched_name"] == "DSPORTPLUS"


def test_build_priority_summary_markdown_contiene_faltantes():
    summary = {
        "generated_at": "2026-06-30T00:00:00Z",
        "requested_total": 2,
        "found_total": 1,
        "missing_total": 1,
        "found": [
            {
                "requested": "Azteca Uno",
                "matched_name": "Azteca Uno",
                "group": "Familia y TV Abierta",
                "country": "MX",
                "state": "alive",
                "url": "https://example.com/a.m3u8",
            }
        ],
        "missing": ["ViX"],
    }

    md = build_priority_summary_markdown(summary)

    assert "Azteca Uno" in md
    assert "## Faltantes" in md
    assert "- ViX" in md


def test_build_m3u_es_valido_y_tiene_extinf_y_url():
    statuses = [make_status("Canal Público Ejemplo", "TV Pública", True)]
    m3u = build_m3u(statuses)
    lines = m3u.strip().split("\n")
    assert lines[0] == "#EXTM3U"
    assert lines[1].startswith("#EXTINF:-1")
    assert lines[2].startswith("https://")


def test_build_m3u_limpia_comillas_problematicas():
    status = make_status('Canal "Raro"', "Grupo", True)
    m3u = build_m3u([status])
    assert '"Raro"' not in m3u  # las comillas internas deben limpiarse
    assert "'Raro'" in m3u


def test_build_m3u_sin_canales_vivos_devuelve_solo_encabezado():
    statuses = [make_status("Muerto", "Grupo", False)]
    m3u = build_m3u(statuses)
    assert m3u.strip() == "#EXTM3U"


def test_build_vod_m3u_incluye_todos_los_items_sin_revision_http():
    m3u = build_vod_m3u(
        [
            {"name": "Movie One (2024)", "group": "Mi Catálogo Cloud", "url": "https://vod.example/movie/1", "tvg_id": "tt1", "logo": ""},
            {"name": "Series One (2024)", "group": "Mi Catálogo Cloud", "url": "https://vod.example/tv/2/1/1", "tvg_id": "tt2", "logo": ""},
        ]
    )

    assert m3u.startswith("#EXTM3U")
    assert "Movie One (2024)" in m3u
    assert "Series One (2024)" in m3u
    assert "https://vod.example/tv/2/1/1" in m3u


def test_classify_vod_transport_detecta_stream_directo_y_web():
    assert _classify_vod_transport("https://example.com/video.m3u8", 200, "application/vnd.apple.mpegurl") == (True, "direct_media")
    assert _classify_vod_transport("https://example.com/watch/123", 200, "text/html; charset=utf-8") == (False, "web_page")


def test_has_playable_channels_detecta_alive_e_unstable():
    statuses = [
        make_status("Muerto", "Grupo", False),
        make_status("Inestable", "Grupo", False, state="unstable", status_code=200, error="Handshake correcto"),
    ]
    assert has_playable_channels(statuses) is True


def test_write_outputs_conserva_playlist_previa_si_todo_cae(tmp_path):
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "playlist.m3u").write_text(
        "#EXTM3U\n#EXTINF:-1,Canal Respaldo\nhttps://example.com/respaldo.m3u8\n",
        encoding="utf-8",
    )

    statuses = [make_status("Muerto", "Grupo", False)]
    fallback_used = write_outputs(statuses, public_dir)

    assert fallback_used is True
    assert "Canal Respaldo" in (public_dir / "playlist.m3u").read_text(encoding="utf-8")


def test_write_vod_output_crea_playlist_independiente(tmp_path):
    public_dir = tmp_path / "public"
    items = [
        {"name": "Movie One (2024)", "group": "Mi Catálogo Cloud", "url": "https://vod.example/movie/1", "tvg_id": "tt1", "logo": ""}
    ]
    vod_statuses = [
        VodStatus(
            name="Movie One (2024)",
            group="Mi Catálogo Cloud",
            url="https://vod.example/movie/1",
            tvg_id="tt1",
            playable_in_vlc=True,
            delivery="direct_media",
            status_code=200,
            content_type="application/vnd.apple.mpegurl",
            error=None,
        )
    ]

    write_vod_output(items, vod_statuses, public_dir)

    vod_playlist = (public_dir / "vod_playlist.m3u").read_text(encoding="utf-8")
    assert "Movie One (2024)" in vod_playlist
    assert "https://vod.example/movie/1" in vod_playlist


def test_write_vod_output_separa_items_solo_web(tmp_path):
    public_dir = tmp_path / "public"
    items = [
        {"name": "Movie One (2024)", "group": "Mi Catálogo Cloud", "url": "https://vod.example/watch/1", "tvg_id": "tt1", "logo": ""}
    ]
    vod_statuses = [
        VodStatus(
            name="Movie One (2024)",
            group="Mi Catálogo Cloud",
            url="https://vod.example/watch/1",
            tvg_id="tt1",
            playable_in_vlc=False,
            delivery="web_page",
            status_code=200,
            content_type="text/html",
            error="No es stream directo para VLC",
        )
    ]

    write_vod_output(items, vod_statuses, public_dir)

    vod_playlist = (public_dir / "vod_playlist.m3u").read_text(encoding="utf-8")
    browser_links = (public_dir / "vod_browser_links.txt").read_text(encoding="utf-8")
    assert vod_playlist.strip() == "#EXTM3U"
    assert "Movie One (2024)" in browser_links


def test_build_vod_status_outputs_resumen_y_detalle():
    statuses = [
        VodStatus(
            name="Movie One (2024)",
            group="Mi Catálogo Cloud",
            url="https://vod.example/movie/1",
            tvg_id="tt1",
            playable_in_vlc=True,
            delivery="direct_media",
            status_code=200,
            content_type="application/vnd.apple.mpegurl",
            error=None,
        ),
        VodStatus(
            name="Series One (2024)",
            group="Mi Catálogo Cloud",
            url="https://vod.example/watch/2",
            tvg_id="tt2",
            playable_in_vlc=False,
            delivery="web_page",
            status_code=200,
            content_type="text/html",
            error="No es stream directo para VLC",
        ),
    ]

    payload = json.loads(build_vod_status_json(statuses))
    markdown = build_vod_status_markdown(statuses)
    browser_links = build_vod_browser_links(statuses)

    assert payload["playable_in_vlc"] == 1
    assert payload["browser_only"] == 1
    assert "Compatibles con VLC" in markdown
    assert "Series One (2024)" in browser_links


# ---------------------------------------------------------------------------
# Generación de status.json
# ---------------------------------------------------------------------------

def test_build_status_json_es_json_valido_y_completo():
    statuses = [
        make_status("Vivo", "Grupo", True),
        make_status("Inestable", "Grupo", False, state="unstable", status_code=200, error="Handshake correcto"),
        make_status("Muerto", "Grupo", False),
    ]
    raw = build_status_json(statuses)
    payload = json.loads(raw)  # debe poder parsearse sin error

    assert payload["total"] == 3
    assert payload["alive"] == 1
    assert payload["unstable"] == 1
    assert payload["dead"] == 1
    assert len(payload["channels"]) == 3
    assert "generated_at" in payload


# ---------------------------------------------------------------------------
# Generación de status.md
# ---------------------------------------------------------------------------

def test_build_status_markdown_contiene_tabla_y_resumen():
    statuses = [
        make_status("Vivo", "Grupo", True),
        make_status("Inestable", "Grupo", False, state="unstable", status_code=200, error="Handshake correcto"),
        make_status("Muerto", "Grupo", False),
    ]
    md = build_status_markdown(statuses)
    assert "# Estado de canales" in md
    assert "Canales totales: **3**" in md
    assert "✅ Vivo" in md
    assert "⚠️ Inestable" in md
    assert "❌ Muerto" in md
