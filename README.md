# Stable Fusion Art — Server & Director

An autonomous AI art generation system with a decoupled **Director / Server** architecture:
- **Server (`gallery_server.py`)**: Runs on the GPU machine (e.g. RTX 3060). Hosts Stable Diffusion 1.5, executes image generation jobs (`POST /generate`), tracks asynchronous job statuses (`GET /jobs/<id>`), and serves raw RGB565 framebuffers for CrowPanel P4 displays (`GET /raw/latest.rgb565`).
- **Director (`director.py`)**: Central orchestrator. Manages one or more generation servers, schedules timed generation jobs to idle nodes, aggregates gallery images, exposes a unified config UI (`director.html`), and proxies the freshest P4 framebuffer.

---

## Architecture Overview

```text
┌────────────────────────────────────────────────────────────────────────┐
│                          STABLE FUSION DIRECTOR                        │
│                           (director.py :8091)                          │
│                                                                        │
│   • Multi-Server Health Polling (GET /health)                          │
│   • Intelligent Job Routing (dispatches to busy == false servers)      │
│   • Central Scheduler (configurable interval & jitter)                 │
│   • Gallery Aggregator (merges GET /images across all nodes)           │
│   • P4 Panel Proxy (routes GET /latest.rgb565 from freshest server)    │
│   • Config UI & Management Console (director.html)                     │
└──────────────┬──────────────────────────────────────────┬──────────────┘
               │                                          │
       HTTP API Calls                             HTTP API Calls
               ▼                                          ▼
┌──────────────────────────────┐          ┌──────────────────────────────┐
│        GPU SERVER #1         │          │        GPU SERVER #2         │
│     (gallery_server.py)      │          │     (gallery_server.py)      │
│                              │          │                              │
│ • Local SD 1.5 Generation    │          │ • Local SD 1.5 Generation    │
│ • Async Job Queue (/jobs)    │          │ • Async Job Queue (/jobs)    │
│ • RGB565 Converter (P4)      │          │ • RGB565 Converter (P4)      │
│ • Static Gallery / Raw Store │          │ • Static Gallery / Raw Store │
└──────────────────────────────┘          └──────────────────────────────┘
```

---

## Files

| File | Purpose |
|------|---------|
| `director.py` | Multi-server director service, job scheduler, P4 proxy & health monitor |
| `director.html` | Modern Director dashboard, live gallery, server health pills & config drawer |
| `director_config.json` | Director settings (server list, scheduler interval, prompt pool, P4 policy) |
| `gallery_server.py` | Server backend with Stable Diffusion runner, async job engine & RGB565 endpoint |
| `gallery.html` | Standalone server gallery UI |
| `config.json` / `config_loader.py` | Server-specific configuration and validation |
| `art_generator.py` | Local Stable Diffusion generation pipeline |
| `convert_to_raw.py` | Converts rendered PNGs to 1024×600 RGB565 raw format |
| `gallery_watchdog.py` | Server watchdog service |

---

## Server API Contract

All endpoints run on the server port (default `:8090`):

- `GET /health` — Health status, `name`, `busy`, `queue_depth`, `gallery_count`, and `latest_image_time`.
- `GET /config` — Current server parameters (`name`, `steps`, `guidance_scale`, `width`, `height`, `auth_token`).
- `PUT /config` — Update server parameters.
- `POST /generate` — Submit generation job payload `{"prompt": "...", "steps": 20, ...}`. Returns HTTP 202 with `{"job_id": "..."}`.
- `GET /jobs/<id>` — Poll status of a job (`queued`, `running`, `done`, `error`).
- `GET /jobs` — List active and recent jobs.
- `GET /images` — List generated image filenames.
- `GET /index.json` — Gallery index entries with metadata.
- `GET /raw/latest.rgb565` — 1024×600 RGB565 raw framebuffer for ESP32/P4 displays.

---

## Director API & Endpoints

Director runs on port `:8091` (default):

- `GET /` — Director UI (`director.html`).
- `GET /api/director/health` — Director health status, active servers, and scheduler state.
- `GET /api/director/state` — Full director state snapshot (servers, jobs, aggregated gallery).
- `GET /api/director/config` / `PUT /api/director/config` — Manage director configuration.
- `POST /api/director/trigger` — Trigger immediate generation on next idle server.
- `GET /api/director/servers/<i>/config` / `PUT /api/director/servers/<i>/config` — Proxy configuration to specific node.
- `GET /latest.rgb565` — Proxies the latest raw framebuffer from the freshest online server (tagged with `X-Director-Source`).

---

## Running

### 1. Start Server on GPU Machine
```bash
python gallery_server.py
```

### 2. Start Director
```bash
python director.py
```
Browse director at: `http://localhost:8091` (or your Tailscale / LAN IP).
