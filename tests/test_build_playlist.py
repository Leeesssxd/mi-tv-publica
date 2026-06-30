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
    build_m3u,
    build_status_json,
    build_status_markdown,
    has_playable_channels,
    load_channels,
    sort_statuses,
    write_outputs,
)


# ---------------------------------------------------------------------------
# Fixtures auxiliares
# ---------------------------------------------------------------------------

def make_status(name: str, group: str, alive: bool, **kwargs) -> ChannelStatus:
    defaults = dict(
        country="MX",
        url=f"https://example.com/{name.lower().replace(' ', '-')}.m3u8",
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
