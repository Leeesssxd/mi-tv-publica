# 📺 Mi TV Pública

Genera una playlist `.m3u` para VLC (y cualquier reproductor compatible)
a partir de canales **públicos, gratuitos o autorizados** que tú mismo
agregas manualmente. El proyecto revisa automáticamente cuáles canales
están vivos y publica un link `.m3u` siempre actualizado, usando
GitHub Actions, sin necesidad de tener un servidor propio.

## ⚠️ Qué NO hace este proyecto

- ❌ No incluye canales privados ni de paga por defecto.
- ❌ No desbloquea, descifra ni evade restricciones de ningún servicio.
- ❌ No extrae streams desde apps, plataformas de streaming ni APIs privadas.
- ❌ No es una herramienta de piratería.

Es, simplemente, un **organizador y verificador** de enlaces `.m3u8`/`.m3u`
que tú agregas porque tienes permiso legal de usarlos (canales públicos,
de gobierno, educativos, de prueba, o streams propios/autorizados).

## ✅ Qué SÍ hace

1. Guarda tu lista de canales en `sources/channels.json`.
2. Revisa de forma asíncrona si cada canal responde correctamente.
3. Genera `public/playlist.m3u` listo para abrir en VLC.
4. Genera `public/status.json` y `public/status.md` con el estado de cada canal.
5. Se actualiza solo cada 6 horas vía GitHub Actions (o cuando tú quieras, manualmente).

## 🗂️ Estructura del proyecto

```text
mi-tv-publica/
├── sources/channels.json       # Tu lista de canales (la editas tú)
├── scripts/build_playlist.py   # Script principal
├── scripts/scrape_channels.py  # Importa enlaces .m3u8 desde una URL pública
├── public/                     # Archivos generados (playlist, estado)
├── tests/test_build_playlist.py
├── tests/test_scrape_channels.py
├── .github/workflows/update.yml
├── config.json                 # Configuración opcional
├── requirements.txt
├── README.md
├── CLAUDE.md                   # Bitácora técnica del proyecto
├── .gitignore
└── LICENSE
```

## ➕ Cómo agregar (o quitar) canales

Edita `sources/channels.json`. Cada canal se ve así:

```json
{
  "name": "Mi Canal Legal",
  "group": "TV Pública",
  "country": "MX",
  "url": "https://mi-stream-autorizado.com/live.m3u8",
  "logo": "https://mi-stream-autorizado.com/logo.png",
  "tvg_id": "mi.canal"
}
```

Campos:

| Campo     | Obligatorio | Descripción                                   |
|-----------|:-----------:|------------------------------------------------|
| `name`    | ✅          | Nombre del canal                               |
| `url`     | ✅          | URL del stream (debe empezar con http/https)   |
| `group`   | ❌          | Categoría (ej. "Noticias", "Deportes")         |
| `country` | ❌          | País del canal                                 |
| `logo`    | ❌          | URL del logo                                   |
| `tvg_id`  | ❌          | ID para EPG/XMLTV                              |

Para quitar un canal, simplemente borra su objeto del arreglo JSON.

## 🖥️ Cómo ejecutarlo localmente

Requiere Python 3.11 o superior.

```bash
pip install -r requirements.txt
python scripts/build_playlist.py
```

Esto generará/actualizará los archivos dentro de `public/`.

Si además quieres importar enlaces `.m3u8` desde una URL pública de texto:

```bash
python scripts/scrape_channels.py https://example.com/listado.txt --group Importados --country MX
```

Ese comando:

- descarga el contenido con `aiohttp`
- detecta URLs `.m3u8` con expresiones regulares
- agrega solo canales nuevos a `sources/channels.json`
- evita duplicados por URL

Para correr las pruebas:

```bash
pytest
```

## ☁️ Cómo subirlo a GitHub

1. Crea un repositorio nuevo en GitHub, por ejemplo `mi-tv-publica`.
2. Dentro de esta carpeta, ejecuta:

```bash
git init
git add .
git commit -m "Proyecto inicial: mi-tv-publica"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/mi-tv-publica.git
git push -u origin main
```

## ⚙️ Cómo activar GitHub Actions

No necesitas hacer nada especial: en cuanto subas el repositorio, GitHub
detecta el archivo `.github/workflows/update.yml` y lo activa automáticamente.

- Se ejecutará **cada 6 horas** de forma automática (en horario UTC).
- También puedes ejecutarlo manualmente: ve a la pestaña **Actions** de tu
  repositorio → selecciona el workflow **"Actualizar playlist"** → botón
  **"Run workflow"**.

> Nota: la primera vez que el workflow intente hacer `git push`, asegúrate
> de que en **Settings → Actions → General → Workflow permissions** esté
> seleccionado **"Read and write permissions"**.

## 🔗 Link final para pegar en VLC

Una vez subido a GitHub, tu playlist estará disponible en:

```text
https://raw.githubusercontent.com/TU_USUARIO/mi-tv-publica/main/public/playlist.m3u
```

Reemplaza `TU_USUARIO` por tu usuario real de GitHub.

Para este repo, los links quedan así:

```text
Playlist VLC:
https://raw.githubusercontent.com/Leeesssxd/mi-tv-publica/main/public/playlist.m3u

Estado JSON:
https://raw.githubusercontent.com/Leeesssxd/mi-tv-publica/main/public/status.json

Estado Markdown:
https://raw.githubusercontent.com/Leeesssxd/mi-tv-publica/main/public/status.md

Telemetría privada:
https://raw.githubusercontent.com/Leeesssxd/mi-tv-publica/main/public/telemetry_status.json
```

### En VLC (computadora)

1. Abre VLC.
2. Ve a **Medio → Abrir ubicación de red** (`Ctrl+N`).
3. Pega el link de arriba y presiona **Reproducir**.

### En VLC para Fire TV

1. Instala **VLC for Fire TV** desde la tienda de Amazon.
2. Ábrelo y selecciona **"Stream"** o **"Conexión de red"**.
3. Pega el mismo link `.m3u`.

### En VLC para celular (Android/iOS)

1. Abre la app de VLC.
2. Busca la opción **"Stream"** / **"Flujo de red"**.
3. Pega el link y reprodúcelo. También puedes guardarlo como favorito.

## 📋 Cómo revisar el estado de los canales

Abre `public/status.md` directamente en GitHub para ver una tabla con:

- Total de canales revisados.
- Cuáles están vivos (✅) o muertos (❌).
- Código HTTP y mensaje de error si aplica.

También existe `public/status.json` con el mismo detalle en formato JSON,
útil si quieres construir tu propio panel o app encima.

## 🌐 Publicar con GitHub Pages (opcional)

Si quieres tener una página web simple mostrando el estado:

1. Ve a **Settings → Pages**.
2. En "Source", selecciona la rama `main` y la carpeta `/public`.
3. Guarda. GitHub te dará una URL tipo
   `https://TU_USUARIO.github.io/mi-tv-publica/status.md` (o `index.html`
   si decides crear uno).

## 🔧 Configuración opcional (`config.json`)

```json
{
  "timeout_seconds": 10,
  "max_concurrency": 10,
  "user_agent": "Mozilla/5.0 (compatible; MiTVPublicaBot/1.0; +https://github.com)",
  "sort_by": ["group", "name"]
}
```

- `timeout_seconds`: segundos máximos de espera por canal.
- `max_concurrency`: cuántos canales se revisan en paralelo.
- `user_agent`: encabezado enviado al revisar cada stream.
- `sort_by`: orden de la playlist final (por defecto: grupo, luego nombre).

## 🚀 Qué puedes mejorar después

- Agregar soporte EPG/XMLTV para mostrar guía de programación en tu reproductor.
- Crear una página HTML (`public/index.html`) con buscador y filtros por grupo/país.
- Agregar un badge de estado del workflow en este README.
- Notificaciones (ej. Discord/Telegram) cuando un canal cae.
- Separar canales por archivos según categoría si la lista crece mucho.

---

Para el detalle técnico de cómo se construyó este proyecto y posibles
mejoras futuras, revisa **[CLAUDE.md](./CLAUDE.md)**.
