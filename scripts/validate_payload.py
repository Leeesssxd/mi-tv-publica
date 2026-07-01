#!/usr/bin/env python3
"""
validate_payload.py
===================

Valida de forma estatica un payload M3U en texto plano antes de que sea
procesado por el pipeline asincrono. El analizador comprueba:

  - Cabecera #EXTM3U
  - Emparejamiento correcto de #EXTINF con una URL posterior valida
  - Ausencia de URLs huerfanas
  - Esquema URI http/https en endpoints de transporte
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

EXTM3U_HEADER = "#EXTM3U"
EXTINF_PREFIX = "#EXTINF:"
EXTVLCOPT_PREFIX = "#EXTVLCOPT:"
VALID_URI_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _non_empty_lines(payload: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped:
            lines.append((line_number, stripped))
    return lines


def validate_payload_text(payload: str) -> list[str]:
    errors: list[str] = []
    lines = _non_empty_lines(payload)

    if not lines:
        return ["Línea 1: falta la cabecera #EXTM3U."]

    first_line_number, first_line = lines[0]
    if first_line != EXTM3U_HEADER:
        errors.append(f"Línea {first_line_number}: la primera línea útil debe ser exactamente #EXTM3U.")

    expecting_url = False
    pending_extinf_line: int | None = None

    for line_number, line in lines[1:]:
        if line.startswith(EXTINF_PREFIX):
            if expecting_url:
                errors.append(
                    f"Línea {line_number}: se encontró #EXTINF consecutivo; falta URL para la etiqueta iniciada en la línea {pending_extinf_line}."
                )
            expecting_url = True
            pending_extinf_line = line_number
            continue

        if line.startswith(EXTVLCOPT_PREFIX):
            if not expecting_url:
                errors.append(f"Línea {line_number}: opción #EXTVLCOPT huérfana sin #EXTINF activo.")
            continue

        if line.startswith("#"):
            if expecting_url:
                errors.append(
                    f"Línea {line_number}: directiva no permitida entre #EXTINF y la URL para la etiqueta iniciada en la línea {pending_extinf_line}."
                )
            continue

        if VALID_URI_RE.match(line):
            if not expecting_url:
                errors.append(f"Línea {line_number}: URL huérfana sin #EXTINF previo.")
                continue
            expecting_url = False
            pending_extinf_line = None
            continue

        if expecting_url:
            errors.append(
                f"Línea {line_number}: la URL asociada a la etiqueta iniciada en la línea {pending_extinf_line} debe comenzar con http:// o https://."
            )
            expecting_url = False
            pending_extinf_line = None
            continue

        errors.append(f"Línea {line_number}: línea no reconocida o URI inválida.")

    if expecting_url:
        errors.append(f"Línea {pending_extinf_line}: falta URL para la etiqueta #EXTINF.")

    return errors


def validate_payload_file(payload_path: Path) -> list[str]:
    return validate_payload_text(payload_path.read_text(encoding="utf-8", errors="ignore"))


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("Uso: python scripts/validate_payload.py <archivo.m3u>")
        return 1

    payload_path = Path(args[0])
    try:
        errors = validate_payload_file(payload_path)
    except FileNotFoundError:
        print(f"[ERROR] No se encontró el archivo: {payload_path}")
        return 1

    if errors:
        print("Payload inválido:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Payload válido: sin anomalías estructurales.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
