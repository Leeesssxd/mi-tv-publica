import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_payload import main, validate_payload_text  # noqa: E402


def test_validate_payload_text_acepta_extinf_con_extvlcopt_y_url():
    payload = "\n".join(
        [
            "#EXTM3U",
            '#EXTINF:-1 tvg-id="demo",Canal Demo',
            "#EXTVLCOPT:network-caching=2000",
            "#EXTVLCOPT:http-reconnect=true",
            "https://example.com/live/demo.m3u8",
        ]
    )

    assert validate_payload_text(payload) == []


def test_validate_payload_text_detecta_errores_estructurales():
    payload = "\n".join(
        [
            "#EXTINF:-1,Sin cabecera",
            '#EXTINF:-1 tvg-id="demo",Canal Demo',
            '#EXTINF:-1 tvg-id="otro",Canal Dos',
            "ftp://example.com/invalido",
            "https://example.com/huerfana.m3u8",
        ]
    )

    errors = validate_payload_text(payload)

    assert any("debe ser exactamente #EXTM3U" in error for error in errors)
    assert any("se encontró #EXTINF consecutivo" in error for error in errors)
    assert any("URL huérfana" in error for error in errors)


def test_main_retorna_1_si_hay_anomalias(tmp_path, capsys):
    payload_file = tmp_path / "payload.m3u"
    payload_file.write_text("#EXTM3U\n#EXTINF:-1,Canal\n", encoding="utf-8")

    exit_code = main([str(payload_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Payload inválido" in captured.out


def test_main_retorna_0_si_el_archivo_esta_limpio(tmp_path, capsys):
    payload_file = tmp_path / "payload.m3u"
    payload_file.write_text(
        "#EXTM3U\n#EXTINF:-1,Canal\nhttps://example.com/live.m3u8\n",
        encoding="utf-8",
    )

    exit_code = main([str(payload_file)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Payload válido" in captured.out
