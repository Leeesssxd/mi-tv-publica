# CLAUDE.md — Bitácora técnica de "Mi TV Pública"

Este archivo documenta qué se construyó, por qué se tomaron ciertas
decisiones, y qué se puede mejorar en el futuro. Está pensado para que
tanto Angel como una futura sesión de Claude puedan retomar el proyecto
sin perder contexto.

## 1. Qué es este proyecto

Un generador de playlists `.m3u` para reproducir, en VLC u otro
reproductor compatible, una lista de **streams públicos, gratuitos o
autorizados** que el propio usuario agrega manualmente. Incluye
verificación automática de disponibilidad y actualización periódica vía
GitHub Actions, sin requerir servidor propio.

**Alcance ético/legal del proyecto:** esta herramienta es un organizador
y verificador de URLs que el usuario ya posee/conoce y tiene derecho a
usar. Los dos canales de
ejemplo incluidos (`NASA TV Public` y un stream de prueba público de
`test-streams.mux.dev`) son streams genuinamente públicos/de prueba,
usados solo para que el proyecto funcione "out of the box".

## 2. Archivos creados

```text
mi-tv-publica/
├── sources/channels.json          # Lista editable de canales (2 ejemplos públicos)
├── scripts/build_playlist.py      # Lógica completa: carga, verificación, generación
├── tests/test_build_playlist.py   # 11 pruebas unitarias (sin red real)
├── public/                        # Se genera/regenera al ejecutar el script
│   ├── playlist.m3u
│   ├── status.json
│   └── status.md
├── .github/workflows/update.yml   # Cron cada 6h + workflow_dispatch
├── config.json                    # timeout, concurrencia, user-agent, orden
├── requirements.txt                # aiohttp + pytest
├── README.md                      # Guía para principiantes
├── CLAUDE.md                      # Este archivo
├── .gitignore
└── LICENSE                        # MIT + nota de uso responsable
```

## 3. Decisiones de diseño y por qué

- **`aiohttp` + `asyncio.Semaphore`**: se pidió revisión asíncrona; el
  semáforo limita la concurrencia (configurable vía `config.json`) para
  no saturar servidores ni gatillar rate-limiting.
- **`ssl=False` en las peticiones**: muchos streams públicos/de prueba
  usan certificados o configuraciones TLS poco estrictas; se prioriza
  que la verificación no falle por eso. Si se requiere mayor rigor de
  seguridad, esto se puede revertir fácilmente (una línea).
- **Heurística de "vivo"**: código HTTP 2xx/3xx **y** `Content-Type`
  compatible con video/audio/HLS (o ausencia de `Content-Type`, para no
  descartar servidores mal configurados que igual funcionan en VLC).
- **Dataclasses (`Channel`, `ChannelStatus`)** en vez de dicts sueltos:
  hace el código más legible, autocompletable y fácil de testear.
- **Separación de lógica pura vs. I/O de red**: las funciones
  `build_m3u`, `build_status_json`, `build_status_markdown`,
  `sort_statuses`, `Channel.from_dict` no tocan la red, por lo que las
  pruebas son rápidas y deterministas (no dependen de internet ni de que
  un canal de ejemplo siga vivo).
- **Cron `"0 */6 * * *"`**: tal como pidió Angel, sin clave `timezone`
  dentro de `schedule` (GitHub Actions no la soporta).
- **Commit condicional en el workflow**: usa `git diff --cached --quiet`
  para no generar commits vacíos cuando nada cambió.
- **`config.json` opcional**: implementado como mejora sugerida en el
  brief original (timeout, concurrencia, user-agent, orden).



## 4. Pruebas incluidas

`tests/test_build_playlist.py` cubre:

- Validación de campos obligatorios (`name`, `url`) y URL con esquema http/https.
- Que `load_channels` ignore canales inválidos sin tumbar el proceso.
- Orden por grupo y luego nombre.
- Que el M3U generado solo incluya canales vivos.
- Formato correcto del M3U (`#EXTM3U`, `#EXTINF`, URL).
- Limpieza de comillas problemáticas en el M3U.
- M3U vacío (solo encabezado) cuando no hay canales vivos.
- `status.json` válido y con conteos correctos.
- `status.md` con tabla y resumen correctos.

No se prueban las llamadas HTTP reales (serían pruebas frágiles/lentas);
si se quiere cubrir esa parte, se recomienda usar `aioresponses` o
`pytest-aiohttp` con mocks (ver sección de mejoras futuras).

## 5. Cómo retomar/extender este proyecto

Ideas concretas para una próxima sesión:

1. **Mocks de red para `check_channel`**: agregar pruebas con
   `aioresponses` que simulen 200/404/timeout sin red real.
2. **`public/index.html`**: página simple (HTML+JS, sin frameworks) que
   lea `status.json` y muestre una tabla/buscador filtrable por grupo o
   país. Encaja con el patrón de "single-file HTML" ya usado en otros
   proyectos de Angel (ej. Balneario Vista Bella).
3. **Badge de GitHub Actions** en el README:
   `![Update](https://github.com/USUARIO/REPO/actions/workflows/update.yml/badge.svg)`.
4. **Soporte EPG/XMLTV opcional**: generar un `epg.xml` básico a partir
   de `tvg_id`, sin bloquear el MVP si no hay datos de programación.
5. **Notificaciones de canal caído**: webhook a Discord/Telegram cuando
   un canal pasa de vivo a muerto entre corridas (requeriría comparar
   contra el `status.json` anterior, ya versionado en git).
6. **Separar `channels.json` en varios archivos** (por país o categoría)
   si la lista crece mucho, con un paso de "merge" antes de revisar.
7. **Endurecer TLS**: quitar `ssl=False` si Angel decide priorizar
   verificación estricta de certificados sobre compatibilidad amplia.

## 6. Cómo correrlo / probarlo (resumen rápido)

```bash
pip install -r requirements.txt
python scripts/build_playlist.py   # genera public/playlist.m3u, status.json, status.md
pytest                              # corre las 11 pruebas unitarias
```

## 7. Stack técnico

- Python 3.11+
- `aiohttp` para verificación asíncrona de streams
- `pytest` para pruebas
- GitHub Actions (cron + workflow_dispatch) para automatización
- Sin backend propio: todo vive en el repositorio (`public/` se sirve vía
  `raw.githubusercontent.com` o GitHub Pages opcional)
