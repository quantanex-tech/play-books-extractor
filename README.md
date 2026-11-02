# Play Books Extractor

A self-hosted Docker application and web interface—inspired by [Libation](https://github.com/rmcrackan/libation)—to back up your purchased **Google Play Books** audiobooks (`.m4a`) and accompanying PDFs (`.pdf`) onto local storage, with live progress streaming and scheduled automatic sync.

> [!CAUTION]
> **USE AT YOUR OWN RISK — NO WARRANTY OR LIABILITY**
>
> This software is an independent open-source tool provided **"AS IS"**, **WITHOUT ANY WARRANTY** of any kind, express or implied (including, without limitation, warranties of merchantability, fitness for a particular purpose, or non-infringement). In no event shall the authors, copyright holders, or contributors be liable for any claim, damages, account actions, data loss, or other liability arising from or in connection with the use of this software.
>
> This project is not affiliated with, endorsed by, or supported by Google LLC. It does **not** circumvent cryptographic DRM (CENC-encrypted streams and Adobe `.acsm` tickets are rejected). You are solely responsible for complying with applicable laws and terms of service in your jurisdiction. **Use strictly at your own risk.**

---

## Features

- **Web Authentication via Cookie / Session Import**: Upload or paste Netscape `cookies.txt`, Cookie-Editor JSON, or zero-extension Browser DevTools **Copy as cURL** / `Cookie` headers. Session cookies are stored locally in `/config/cookies.json` (`0600`) and redacted from all API responses.
- **High-Quality Playable Audio (`.m4a`)**: Downloads Google Play Books' native unencrypted `.m4a` (AAC / ISO BMFF) audiobook containers without lossy re-encoding—defaulting to **`Export (high)`** quality (`PLAYBOOKS_EXPORT_QUALITY=high`) and automatically falling back to the unencrypted `/books/listen` web-player stream when a title omits the library `Export` menu option. Playable in VLC, Apple Books/Music, Android, Audiobookshelf, and Plex.
- **Accompanying PDFs Saved Alongside Audio**: Automatically detects and downloads supplemental/companion `.pdf` files next to the `.m4a` file (validating `%PDF-` headers so Adobe `.acsm` DRM tickets are never saved as corrupt PDFs).
- **Scheduled Automatic Sync & Export**: Built-in 5-field crontab scheduler (enabled by default at **`0 2 * * *`**, daily at 2:00 AM) checks for newly purchased or missing audiobooks and exports them automatically. If no new books are available, it stops immediately until the next scheduled check. Configurable directly in the Web UI with crontab helper presets and live validation.
- **Named & ID'd Folders + Disk Reconciliation**: Organises books under `/data/<Sanitized Title> [<VolumeID>]/` and reconciles existing or deleted folders on disk automatically.
- **Real-Time Progress Dashboard**: Streams live `0%–100%` byte-level download progress and schedule updates over Server-Sent Events (`/api/events`).

---

## Quick Start (Docker Compose)

### Prerequisites
- [Docker](https://docs.docker.com/get-docker/) with the Docker Compose plugin (`docker compose`).

### 1. Clone and start the container

```bash
git clone https://github.com/quantanex-tech/play-books-extractor.git
cd play-books-extractor

mkdir -p data config
docker compose up -d --build
```

Open **http://localhost:8080** in your browser.

### 2. Custom host audiobook directory & timezone (optional)

Point `/data` at your existing audiobook library folder (for example, an Audiobookshelf or Plex media path) and set `TZ` so scheduled runs follow your local wall clock:

```bash
PLAYBOOKS_HOST_DATA_DIR=/mnt/media/audiobooks TZ=Europe/London docker compose up -d
```

Or edit `docker-compose.yml` / create a local `.env` file:

```dotenv
PLAYBOOKS_HOST_DATA_DIR=/mnt/media/audiobooks
PLAYBOOKS_HOST_CONFIG_DIR=./config
PLAYBOOKS_PORT=8080
PLAYBOOKS_EXPORT_QUALITY=high
PLAYBOOKS_SCHEDULE_ENABLED=true
PLAYBOOKS_SCHEDULE_CRON=0 2 * * *
TZ=Europe/London
```

> **SELinux note (Fedora / RHEL / CentOS)**: `docker-compose.yml` includes the `:Z` volume label on `/data` and `/config` so rootless Podman and SELinux-enforcing Docker hosts can write cleanly without permission errors.

---

## Authenticating with Google Play Books

Google Play Books has no public download OAuth API and blocks automated headless login forms. Play Books Extractor authenticates using session cookies exported from your own signed-in desktop browser:

1. Sign into [https://play.google.com/books/audiobooks](https://play.google.com/books/audiobooks) in your desktop browser.
2. Choose **either** method:
   - **Zero-extension method (Browser DevTools)**: Press `F12` → open the **Network** tab → reload the page → right-click the top `audiobooks` (or `books`) document request → **Copy → Copy as cURL**, and paste into the **Google Play Books Authentication** panel at `http://localhost:8080`.
   - **Cookie file / JSON**: Export `cookies.txt` (Netscape format) or JSON cookies for `.google.com` / `play.google.com` and upload or paste it into the Web UI.
3. Click **Import & Verify Session**.
4. Click **Sync Google Play Library**, then **Export All Available** (or let the scheduled sync export new purchases automatically).

---

## Scheduled Sync & Crontab Configuration

By default, Play Books Extractor runs an automatic sync and export check **daily at 2:00 AM** (`0 2 * * *`):

1. Syncs your Google Play Books audiobook library and scans `/data` on disk.
2. Queues and exports any newly purchased audiobooks (or any audiobook whose folder was removed from `/data`).
3. If all audiobooks are already extracted in `/data`, stops immediately and does nothing until the next scheduled run.

You can enable/disable the scheduler or customize the schedule in the **Scheduled Sync & Auto-Export** panel in the Web UI using standard 5-field crontab syntax:

```text
┌───────────── minute (0–59)
│ ┌─────────── hour (0–23)
│ │ ┌───────── day of month (1–31)
│ │ │ ┌─────── month (1–12 or JAN–DEC)
│ │ │ │ ┌───── day of week (0–6, Sun=0 or 7, or SUN–SAT)
│ │ │ │ │
* * * * *
```

| Schedule | Crontab Expression |
| :--- | :--- |
| Daily at 2:00 AM *(default)* | `0 2 * * *` |
| Every 6 hours | `0 */6 * * *` |
| Sundays at 3:00 AM | `0 3 * * 0` |
| Every 15 minutes | `*/15 * * * *` |
| Every minute *(useful for testing)* | `* * * * *` |

Changes saved in the Web UI are validated immediately and persisted in `/config/library.db` across container restarts.

---

## Output Folder Layout

Each extracted audiobook is stored in its own deterministic `<Sanitized Title> [<VolumeID>]/` folder inside `/data`:

```text
data/
└── Project Hail Mary [AQAAAEExampleID]/
    ├── Project Hail Mary.m4a
    └── Project Hail Mary.pdf        # included when the audiobook has an accompanying PDF
```

---

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PLAYBOOKS_HOST_DATA_DIR` | `./data` | Host directory mounted to `/data` in `docker-compose.yml`. |
| `PLAYBOOKS_HOST_CONFIG_DIR` | `./config` | Host directory mounted to `/config` (`cookies.json` + `library.db`). |
| `PLAYBOOKS_BIND_HOST` | `127.0.0.1` | Host interface bound by Docker Compose (`0.0.0.0` to expose on LAN). |
| `PLAYBOOKS_PORT` | `8080` | Web UI / API HTTP port. |
| `PLAYBOOKS_EXPORT_QUALITY` | `high` | Preferred Google Play Books export quality (`high` or `standard`). |
| `PLAYBOOKS_SCHEDULE_ENABLED` | `true` | Initial default state for scheduled sync (`true` or `false`). |
| `PLAYBOOKS_SCHEDULE_CRON` | `0 2 * * *` | Initial default 5-field crontab schedule when initializing `/config/library.db`. |
| `TZ` | *(container UTC)* | IANA timezone name for scheduled cron runs (e.g. `Europe/London`, `America/New_York`). |

---

## Running Automated Tests Locally

```bash
uv run --python 3.11 --extra dev pytest -v
```

---

## License & Disclaimer

Licensed under the **GNU General Public License v3.0 (`GPL-3.0`)** — see [LICENSE](LICENSE) for the full license text.

**Disclaimer of Warranty & Limitation of Liability (GPL-3.0 Sections 15 & 16)**:
THERE IS NO WARRANTY FOR THE PROGRAM, TO THE EXTENT PERMITTED BY APPLICABLE LAW. THE COPYRIGHT HOLDERS AND/OR OTHER PARTIES PROVIDE THE PROGRAM **"AS IS"** WITHOUT WARRANTY OF ANY KIND, EITHER EXPRESSED OR IMPLIED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE. IN NO EVENT UNLESS REQUIRED BY APPLICABLE LAW OR AGREED TO IN WRITING WILL ANY COPYRIGHT HOLDER, OR ANY OTHER PARTY WHO MODIFIES AND/OR CONVEYS THE PROGRAM AS PERMITTED ABOVE, BE LIABLE TO YOU FOR DAMAGES, INCLUDING ANY GENERAL, SPECIAL, INCIDENTAL OR CONSEQUENTIAL DAMAGES ARISING OUT OF THE USE OR INABILITY TO USE THE PROGRAM. **USE AT YOUR OWN RISK.**
