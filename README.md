# Stable Fusion Art — Server & Director

An autonomous AI art generation system with a decoupled **Director / Server**
architecture:

- **Server (`art_server.py`)** — runs on the GPU machine (e.g. RTX 3060), hosts
  Stable Diffusion 1.5, executes generation jobs (`POST /generate`), tracks async
  job status (`GET /jobs/<id>`), and serves the image gallery.
- **Director (`director.py`)** — central orchestrator. Manages one or more
  generation servers, schedules timed jobs to idle nodes, aggregates the gallery
  across servers, and serves the management UI plus a fullscreen browser kiosk
  for wall displays / smart TVs.

---

## Architecture Overview

```text
┌────────────────────────────────────────────────────────────┐
│                      STABLE FUSION DIRECTOR                │
│                       (director.py :8091)                  │
│                                                            │
│  • Multi-server health polling  (GET /health)              │
│  • Job routing to idle servers  (busy == false)            │
│  • Central scheduler            (interval & jitter)        │
│  • Gallery aggregation          (merges GET /images)       │
│  • Config UI / management console (director.html)          │
│  • Browser kiosk  (display.html, /display)                 │
└──────────────┬───────────────────────────┬─────────────────┘
               │                           │
       HTTP API calls              HTTP API calls
               ▼                           ▼
┌──────────────────────────────┐  ┌──────────────────────────────┐
│        GPU SERVER #1         │  │        GPU SERVER #2         │
│        (art_server.py)       │  │        (art_server.py)       │
│  • Local SD 1.5 generation   │  │  • Local SD 1.5 generation   │
│  • Async job queue (/jobs)   │  │  • Async job queue (/jobs)   │
│  • Static gallery / images   │  │  • Static gallery / images   │
└──────────────────────────────┘  └──────────────────────────────┘
```

---

## Files

| File | Purpose |
|------|---------|
| `art_server.py` | Server backend: Stable Diffusion runner, async job engine, gallery + static image serving |
| `director.py` | Director service: scheduler, health monitor, gallery aggregator, management UI + browser kiosk |
| `director.html` | Director dashboard: gallery, server health, config drawer |
| `display.html` | **Fullscreen browser kiosk** — auto-refreshes to the freshest image (for TVs / wall displays) |
| `director_config.json` | Director settings (server list, scheduler interval, prompt pool) |
| `art_generator.py` | Local Stable Diffusion generation pipeline |
| `gallery.html` | Standalone server gallery UI |
| `gallery_watchdog.py` | Server watchdog (checks port 8090, restarts `art_server.py`) |
| `director_watchdog.py` | Director watchdog (checks port 8091) |
| `API.md` | Detailed server API contract |

---

## Server API Contract

All endpoints on the server port (default `:8090`). See **`API.md`** for the
full contract with examples.

- `GET /health` — status, `busy`, `queue_depth`, `gallery_count`, `latest_image_time`
- `GET /config` / `PUT /config` — read / update generation params
- `POST /generate` — submit a job `{"prompt": "...", ...}` → `202 {"job_id": "..."}`
- `GET /jobs/<id>` — poll job status (`queued | running | done | failed`)
- `GET /jobs` — list jobs
- `GET /images` — gallery metadata, newest first
- `GET /<file.png>` — serve a generated image
- `GET /` — the standalone `gallery.html` viewer

---

## Director API & Endpoints

Director runs on port `:8091` (default):

- `GET /` — director management UI (`director.html`)
- `GET /display` — **browser kiosk** (fullscreen, auto-refresh, crossfade)
- `GET /api/director/health` — director health + server states
- `GET /api/director/state` — full state snapshot (servers, scheduler, aggregated gallery)
- `GET /api/director/gallery` — aggregated gallery (`{"count", "images"}`)
- `GET /api/director/latest` — single freshest image `{"url", "file", "server_name", "prompt", "timestamp"}`
- `GET /api/director/config` / `PUT /api/director/config` — manage director config
- `POST /api/director/trigger` — fire one generation now on an idle server
- `GET /api/director/servers` — list configured servers
- `GET /api/director/servers/<i>/config` / `PUT ...` — proxy config to a server

The kiosk (`/display`) polls `/api/director/latest`, preloads the next image, and
crossfades when a new one appears — no white flash, keyboardless, safe to leave
on a TV indefinitely.

---

## Running

### 1. Start the server on the GPU machine
```bash
python art_server.py
```

### 2. Start the director
```bash
python director.py
```

### 3. View
- Management UI: `http://localhost:8091` (or your Tailscale / LAN IP)
- **Wall display / TV**: open `http://<director-ip>:8091/display` and go fullscreen

Both services have watchdogs (registered as cron jobs) that restart them if they
die — `gallery_watchdog.py` for the server, `director_watchdog.py` for the
director.
