"""
Stable Fusion Director
======================

A long-running process that orchestrates one or more Stable Fusion servers.

Responsibilities
----------------
* Owns the generation schedule. Fires ``POST /generate`` on an idle server on
  a configurable interval (the server's local cron is disabled in favor of this).
* Polls ``GET /health`` on every configured server, tracks busy/queue/gallery
  counts, and routes generation to the first server reporting ``busy == false``.
* Aggregates ``GET /images`` from every server into a single gallery view.
* Proxies ``/latest.rgb565`` from the configured "primary" server so the
  CrowPanel P4 wall display can fetch from the director instead of a specific
  server box.

The director does NOT generate images. It only talks to servers over HTTP.

Configuration
-------------
Reads ``director_config.json`` from the same directory. The file is created
with sane defaults on first start. Edits can be made through the web UI
(gear icon) or by editing the file directly. The director validates the file
on boot and refuses to start with a broken config.

Endpoints
---------
* ``GET  /``                              -> the director UI (director.html)
* ``GET  /api/director/health``           -> director's own health
* ``GET  /api/director/state``            -> full state (servers, scheduler, gallery, recent jobs)
* ``GET  /api/director/config``           -> current config
* ``PUT  /api/director/config``           -> merge-update config
* ``POST /api/director/trigger``          -> fire one generation now
* ``GET  /latest.rgb565``                 -> proxy from primary server (P4 endpoint)
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------

WORKDIR = Path(__file__).parent.resolve()
CONFIG_PATH = WORKDIR / "director_config.json"
HTML_PATH = WORKDIR / "director.html"
CONFIGURE_P4_HTML_PATH = WORKDIR / "configure_p4.html"
DISPLAY_HTML_PATH = WORKDIR / "display.html"
LOG_PATH = WORKDIR / "director.log"

DEFAULT_CONFIG: dict = {
    "director": {
        "host": "0.0.0.0",
        "port": 8091,
        "name": "stable-fusion-director",
    },
    "servers": [
        {"name": "Local", "base_url": "http://127.0.0.1:8090", "auth_token": ""},
    ],
    "scheduler": {
        "enabled": True,
        "interval_seconds": 1800,
        "jitter_seconds": 60,
    },
    "generation": {
        # Default params sent to the server on every POST /generate.
        # The server's own /config is the authoritative per-server setting;
        # these are the director's defaults for the request body.
        "prompt_pool": [
            "a misty mountain range at golden hour, painted in the style of Hudson River School art",
            "a cyberpunk cityscape at night with neon reflections in the rain",
            "a serene Japanese garden in autumn with a stone lantern and maple leaves",
            "an astronaut floating in deep space with Earth reflected in the visor",
            "a dragon perched on a cliff above the clouds at sunrise",
            "a vintage steam locomotive crossing a wooden trestle bridge in autumn",
            "a bioluminescent forest at night with glowing mushrooms and fireflies",
            "a coastal lighthouse in a thunderstorm with dramatic waves",
            "a futuristic solarpunk village with green terraces and wind turbines",
            "a crystal cave with a single shaft of sunlight piercing the darkness",
            "a robot tending a small garden in a post-industrial landscape",
            "an ancient library carved into a cliff face, lit by floating lanterns",
            "a hot air balloon festival at dawn over rolling hills",
            "a sunken city underwater with schools of fish swimming through arches",
            "a snow-covered village at twilight with smoke rising from chimneys",
        ],
        "params": {
            "steps": 20,
            "guidance_scale": 7.5,
            "width": 768,
            "height": 448,
        },
    },
    "poll": {
        "health_interval_seconds": 10,
        "gallery_interval_seconds": 30,
    },
    "p4": {
        # "freshest" = the server with the highest gallery_count (most
        # recent successful image). Falls back to source_server_index when
        # all servers are offline or have no health data.
        "strategy": "freshest",
        "source_server_index": 0,
    },
}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_stop = threading.Event()
_cfg_holder: dict = {"cfg": {}}  # set in main()

_state: dict = {
    "started_at": time.time(),
    "config_path": str(CONFIG_PATH),
    "servers": [],          # one entry per configured server, with health info
    "scheduler": {
        "enabled": True,
        "interval_seconds": 1800,
        "in_flight": False,
        "last_fired_at": None,
        "next_fire_at": None,
        "recent_jobs": [],   # most-recent first, capped
    },
    "aggregated_gallery": [],
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def deep_merge(defaults: dict, override: dict) -> dict:
    """Recursive dict merge: override wins on scalar values, recurse on dicts."""
    out = json.loads(json.dumps(defaults))
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return deep_merge(DEFAULT_CONFIG, user)
        except Exception as e:
            log(f"config load error ({e}); using defaults")
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    """Atomic write: write to .tmp, then os.replace. Handles absolute paths."""
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def validate_config(cfg: dict) -> list:
    """Return a list of {path, message} errors. Empty list == valid."""
    errors: list = []
    port = int(cfg.get("director", {}).get("port", 8091) or 0)
    if not (1 <= port <= 65535):
        errors.append({"path": "director.port", "message": "must be 1-65535"})
    sched = cfg.get("scheduler", {})
    interval = int(sched.get("interval_seconds", 0) or 0)
    if interval < 30:
        errors.append({"path": "scheduler.interval_seconds",
                       "message": "must be >= 30 seconds"})
    jitter = int(sched.get("jitter_seconds", 0) or 0)
    if jitter < 0:
        errors.append({"path": "scheduler.jitter_seconds",
                       "message": "must be >= 0"})
    servers = cfg.get("servers") or []
    if not servers:
        errors.append({"path": "servers", "message": "at least one server required"})
    for i, s in enumerate(servers):
        if not isinstance(s, dict):
            errors.append({"path": f"servers[{i}]", "message": "must be an object"})
            continue
        url = (s.get("base_url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            errors.append({"path": f"servers[{i}].base_url",
                           "message": "must start with http:// or https://"})
        if not (s.get("name") or "").strip():
            errors.append({"path": f"servers[{i}].name", "message": "name is required"})
    p4_index = int(cfg.get("p4", {}).get("source_server_index", 0) or 0)
    if servers and (p4_index < 0 or p4_index >= len(servers)):
        errors.append({"path": "p4.source_server_index",
                       "message": f"must be 0..{len(servers) - 1}"})
    return errors


# ---------------------------------------------------------------------------
# HTTP helpers (server -> director)
# ---------------------------------------------------------------------------

def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def http_get(url: str, token: str = "", timeout: float = 5.0) -> tuple[int, Any]:
    req = urllib.request.Request(url, method="GET")
    for k, v in _auth_header(token).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                try:
                    return r.status, json.loads(data)
                except Exception:
                    return r.status, data.decode("utf-8", errors="replace")
            return r.status, data
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, str(e)


def http_put(url: str, body: Any, token: str = "",
             timeout: float = 10.0) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Content-Type", "application/json")
    for k, v in _auth_header(token).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = r.read()
            try:
                return r.status, json.loads(resp)
            except Exception:
                return r.status, resp.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, str(e)


def http_post(url: str, body: dict, token: str = "",
              timeout: float = 10.0) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in _auth_header(token).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = r.read()
            try:
                return r.status, json.loads(resp)
            except Exception:
                return r.status, resp.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, str(e)


def proxy_binary(url: str, token: str = "", timeout: float = 30.0
                 ) -> tuple[int, bytes, dict]:
    """Stream a binary file (latest.rgb565) from a server. Returns (status, body, headers)."""
    req = urllib.request.Request(url, method="GET")
    for k, v in _auth_header(token).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, b"", {}
    except Exception:
        return 0, b"", {}


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

def scheduler_loop() -> None:
    """Periodically POST /generate to an idle server."""
    while not _stop.is_set():
        try:
            cfg = _cfg_holder["cfg"]
            sched = cfg.get("scheduler", {})
            with _lock:
                in_flight = _state["scheduler"]["in_flight"]
            if sched.get("enabled", False) and not in_flight:
                fire_one_generation()
        except Exception as e:
            log(f"scheduler error: {e}")
        # Recompute next_fire_at
        with _lock:
            last = _state["scheduler"]["last_fired_at"]
            interval = _cfg_holder["cfg"].get("scheduler",
                                              {}).get("interval_seconds", 1800)
            _state["scheduler"]["next_fire_at"] = (
                None if last is None else last + interval
            )
        interval = _cfg_holder["cfg"].get("scheduler",
                                          {}).get("interval_seconds", 1800)
        jitter = _cfg_holder["cfg"].get("scheduler",
                                        {}).get("jitter_seconds", 0)
        wait = interval + random.uniform(0, max(0, jitter))
        _stop.wait(wait)


def _build_generate_body() -> dict:
    """Pick a random prompt from the director's pool, merge director's
    default generation params. The server may override with its own
    /config (steps, guidance_scale, width, height)."""
    with _lock:
        cfg = _cfg_holder["cfg"]
    pool = cfg.get("generation", {}).get("prompt_pool") or ["abstract art"]
    prompt = random.choice(pool)
    params = dict(cfg.get("generation", {}).get("params") or {})
    return {"prompt": prompt, **params}


def fire_one_generation() -> None:
    """Pick an idle server, POST /generate, record the job. in_flight
    stays True for the lifetime of the job (polled in a worker thread)."""
    with _lock:
        if _state["scheduler"]["in_flight"]:
            return
        _state["scheduler"]["in_flight"] = True
        _state["scheduler"]["last_fired_at"] = time.time()

    try:
        server = pick_idle_server()
        if not server:
            log("no idle server; skipping generation")
            with _lock:
                _state["scheduler"]["recent_jobs"].insert(0, {
                    "fired_at": time.time(),
                    "server": "-",
                    "url": "-",
                    "status": 0,
                    "body": "no idle server available",
                    "job_id": None,
                    "job_status": "skipped",
                })
                _state["scheduler"]["recent_jobs"] = (
                    _state["scheduler"]["recent_jobs"][:50]
                )
            return
        url = server["base_url"].rstrip("/") + "/generate"
        body = _build_generate_body()
        status, resp = http_post(url, body, server.get("auth_token", ""))
        # The server returns either {"job_id": "..."} or {"id": "..."};
        # accept either, plus a job object.
        job_id = None
        if isinstance(resp, dict):
            job_id = resp.get("job_id") or resp.get("id")
        record = {
            "fired_at": time.time(),
            "server": server.get("name", "?"),
            "url": url,
            "status": status,
            "body": _safe_short(resp if isinstance(resp, (dict, str))
                                 else str(resp)),
            "prompt": body.get("prompt", "")[:120],
            "job_id": job_id,
            "job_status": "queued" if (status in (200, 202) and job_id)
                          else ("error" if status else "no_response"),
        }
        with _lock:
            _state["scheduler"]["recent_jobs"].insert(0, record)
            _state["scheduler"]["recent_jobs"] = (
                _state["scheduler"]["recent_jobs"][:50]
            )
        log(f"generation fired: server={server.get('name')} status={status} "
            f"job_id={job_id}")
        if record["job_status"] == "queued":
            # Watch the job in a worker thread; it'll release in_flight.
            threading.Thread(target=_watch_job,
                             args=(server, job_id, record["fired_at"]),
                             daemon=True,
                             name=f"jobwatch-{job_id}").start()
        else:
            with _lock:
                _state["scheduler"]["in_flight"] = False
    except Exception as e:
        log(f"generation fire error: {e}")
        with _lock:
            _state["scheduler"]["in_flight"] = False


def _watch_job(server: dict, job_id: str, fired_at: float) -> None:
    """Poll GET /jobs/<id> until the job is done or 5 minutes elapse.
    Updates recent_jobs in place; releases in_flight on exit."""
    deadline = time.time() + 300  # hard cap at 5 min
    last_status = None
    try:
        while time.time() < deadline:
            url = server["base_url"].rstrip("/") + f"/jobs/{job_id}"
            status, body = http_get(url, server.get("auth_token", ""))
            if status == 200 and isinstance(body, dict):
                last_status = body.get("status", "unknown")
                if last_status in ("done", "error", "failed", "cancelled"):
                    break
            time.sleep(2.0)
        else:
            last_status = "timeout"
    except Exception as e:
        last_status = f"watch_error: {e}"
    finally:
        # Patch the matching record in recent_jobs.
        with _lock:
            for j in _state["scheduler"]["recent_jobs"]:
                if j.get("job_id") == job_id and j.get("fired_at") == fired_at:
                    j["job_status"] = last_status or "unknown"
                    break
            _state["scheduler"]["in_flight"] = False
        log(f"job watch done: job_id={job_id} status={last_status}")


def pick_idle_server() -> Optional[dict]:
    with _lock:
        states = list(_state["servers"])
    cfg_servers = {s["base_url"]: s for s in _cfg_holder["cfg"].get("servers", [])}
    for st in states:
        cfg = cfg_servers.get(st["base_url"])
        if not cfg:
            continue
        if st.get("status") == "online" and st.get("busy") is False:
            return cfg
    return None


def health_loop() -> None:
    while not _stop.is_set():
        try:
            poll_health()
        except Exception as e:
            log(f"health poll loop error: {e}")
        interval = _cfg_holder["cfg"].get("poll",
                                          {}).get("health_interval_seconds", 10)
        _stop.wait(interval)


def poll_health() -> None:
    with _lock:
        cfg = _cfg_holder["cfg"]
    servers = cfg.get("servers", [])
    new_states = []
    for s in servers:
        url = s.get("base_url", "").rstrip("/") + "/health"
        t0 = time.time()
        status, body = http_get(url, s.get("auth_token", ""), timeout=4.0)
        dt = time.time() - t0
        st = {
            "name": s.get("name", "?"),
            "base_url": s.get("base_url"),
            "status": "online" if status == 200 else "offline",
            "http_status": status,
            "latency_ms": round(dt * 1000, 1),
            "last_checked_at": time.time(),
        }
        if isinstance(body, dict):
            st["server_name"] = body.get("name")
            st["busy"] = body.get("busy")
            st["queue_depth"] = body.get("queue_depth")
            st["gallery_count"] = body.get("gallery_count")
            st["latest_image_time"] = body.get("latest_image_time")
        else:
            st["error"] = _safe_short(body)
        new_states.append(st)
    with _lock:
        _state["servers"] = new_states


def gallery_loop() -> None:
    while not _stop.is_set():
        try:
            poll_gallery()
        except Exception as e:
            log(f"gallery poll loop error: {e}")
        interval = _cfg_holder["cfg"].get("poll",
                                          {}).get("gallery_interval_seconds", 30)
        _stop.wait(interval)


def poll_gallery() -> None:
    with _lock:
        cfg = _cfg_holder["cfg"]
    servers = cfg.get("servers", [])
    all_images = []
    for s in servers:
        url = s.get("base_url", "").rstrip("/") + "/images"
        status, body = http_get(url, s.get("auth_token", ""), timeout=6.0)
        # The server returns {"images": [...]}; tolerate a bare list too.
        if status == 200 and isinstance(body, dict):
            body = body.get("images", [])
        if status == 200 and isinstance(body, list):
            for item in body:
                if isinstance(item, str):
                    all_images.append({
                        "file": item,
                        "server_name": s.get("name", "?"),
                        "base_url": s.get("base_url"),
                    })
                elif isinstance(item, dict):
                    all_images.append({
                        **item,
                        "server_name": s.get("name", "?"),
                        "base_url": s.get("base_url"),
                    })
    all_images.sort(key=lambda x: str(x.get("file", "")), reverse=True)
    with _lock:
        _state["aggregated_gallery"] = all_images[:300]


# ---------------------------------------------------------------------------
# HTTP Handler (Director API + UI)
# ---------------------------------------------------------------------------

class DirectorHandler(BaseHTTPRequestHandler):
    server_version = "StableFusionDirector/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: Any,
              content_type: str = "application/json",
              extra_headers: Optional[dict] = None) -> None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            content_type = "application/json"
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        elif isinstance(body, bytes):
            payload = body
        else:
            payload = str(body).encode("utf-8")

        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except Exception:
            pass

    def _read_json(self) -> tuple[bool, Any, str]:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            try:
                raw = _read_chunked(self.rfile)
            except Exception as e:
                return False, None, f"chunked read failed: {e}"
        else:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 256 * 1024:
                return False, None, "body too large (max 256KB)"
            try:
                raw = self.rfile.read(length)
            except Exception as e:
                return False, None, f"read failed: {e}"
        try:
            return True, json.loads(raw.decode("utf-8")), ""
        except Exception as e:
            return False, None, f"invalid JSON: {e}"

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/configure-p4":
            if not CONFIGURE_P4_HTML_PATH.exists():
                self._send(404, {"error": "configure_p4.html not found"})
                return
            try:
                html = CONFIGURE_P4_HTML_PATH.read_text(encoding="utf-8")
                self._send(200, html, "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, {"error": f"read html failed: {e}"})
            return
        if path in ("/display", "/display/"):
            if not DISPLAY_HTML_PATH.exists():
                self._send(404, {"error": "display.html not found"})
                return
            try:
                html = DISPLAY_HTML_PATH.read_text(encoding="utf-8")
                self._send(200, html, "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, {"error": f"read html failed: {e}"})
            return
        if path in ("/", "/index.html", "/gallery", "/fullscreen"):
            if not HTML_PATH.exists():
                self._send(404, {"error": "director.html not found"})
                return
            try:
                html = HTML_PATH.read_text(encoding="utf-8")
                self._send(200, html, "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, {"error": f"read html failed: {e}"})
            return
        if path == "/api/director/health":
            with _lock:
                srv_summary = [{
                    "name": s.get("name"),
                    "base_url": s.get("base_url"),
                    "status": s.get("status"),
                    "busy": s.get("busy"),
                    "queue_depth": s.get("queue_depth"),
                    "gallery_count": s.get("gallery_count"),
                    "latest_image_time": s.get("latest_image_time"),
                    "latency_ms": s.get("latency_ms"),
                } for s in _state["servers"]]
                sched = dict(_state["scheduler"])
                sched.pop("recent_jobs", None)
                body = {
                    "status": "ok",
                    "name": _cfg_holder["cfg"].get("director", {}).get(
                        "name", "stable-fusion-director"),
                    "uptime_seconds": int(time.time() - _state["started_at"]),
                    "servers": srv_summary,
                    "scheduler": sched,
                    "p4_strategy": _cfg_holder["cfg"].get("p4", {}).get(
                        "strategy", "freshest"),
                }
            self._send(200, body)
            return
        if path == "/api/director/state":
            with _lock:
                body = {
                    "status": "ok",
                    "uptime_seconds": int(time.time() - _state["started_at"]),
                    "config": _cfg_holder["cfg"],
                    "servers": _state["servers"],
                    "scheduler": _state["scheduler"],
                    "gallery_count": len(_state["aggregated_gallery"]),
                    "aggregated_gallery": _state["aggregated_gallery"],
                    "gallery_sample": _state["aggregated_gallery"][:60],
                }
            self._send(200, body)
            return
        if path == "/api/director/gallery":
            with _lock:
                body = {
                    "count": len(_state["aggregated_gallery"]),
                    "images": _state["aggregated_gallery"],
                }
            self._send(200, body)
            return
        if path == "/api/director/latest":
            # Return the single freshest image + a browser-ready absolute URL.
            # aggregated_gallery is sorted newest-first (by timestamped filename).
            with _lock:
                gallery = _state.get("aggregated_gallery", [])
            if not gallery:
                self._send(200, {"url": None, "file": None, "server_name": None,
                                 "prompt": None, "timestamp": None})
                return
            latest = gallery[0]
            file = latest.get("file")
            base = (latest.get("base_url") or "").rstrip("/")
            url = f"{base}/{file}" if (base and file) else None
            self._send(200, {
                "url": url,
                "file": file,
                "server_name": latest.get("server_name"),
                "prompt": latest.get("prompt"),
                "timestamp": latest.get("timestamp"),
            })
            return
        if path == "/api/director/config":
            with _lock:
                cfg = json.loads(json.dumps(_cfg_holder["cfg"]))
            self._send(200, cfg)
            return
        if path.startswith("/api/director/servers/") and path.endswith("/config"):
            try:
                idx = int(path.rsplit("/", 2)[-2])
            except ValueError:
                self._send(400, {"error": "server index must be an int"})
                return
            cfg_servers = _cfg_holder["cfg"].get("servers", [])
            if idx < 0 or idx >= len(cfg_servers):
                self._send(404, {"error": f"no server at index {idx}"})
                return
            src = cfg_servers[idx]
            url = src["base_url"].rstrip("/") + "/config"
            status, body = http_get(url, src.get("auth_token", ""))
            if isinstance(body, (dict, list)):
                self._send(status or 502, body)
            else:
                self._send(status or 502,
                           {"error": f"upstream returned {status}",
                            "raw": str(body)[:200] if body else None})
            return
        if path == "/api/director/servers":
            with _lock:
                cfg_servers = _cfg_holder["cfg"].get("servers", [])
                state_servers = list(_state["servers"])
            out = []
            for i, s in enumerate(cfg_servers):
                row = {"index": i, "name": s.get("name"),
                       "base_url": s.get("base_url")}
                for st in state_servers:
                    if st.get("base_url") == s.get("base_url"):
                        row["last_health"] = st
                        break
                out.append(row)
            self._send(200, {"servers": out})
            return
        if path == "/latest.rgb565":
            cfg = _cfg_holder["cfg"]
            servers = cfg.get("servers", [])
            if not servers:
                self._send(502, {"error": "no servers configured"})
                return
            strategy = cfg.get("p4", {}).get("strategy", "freshest")
            chosen_index = None
            with _lock:
                state_servers = list(_state["servers"])
            if strategy == "freshest" and state_servers:
                best = None
                for st in state_servers:
                    if st.get("status") != "online":
                        continue
                    t = st.get("latest_image_time")
                    if t is None:
                        continue
                    if best is None or t > best[1]:
                        best = (st, t)
                if best is not None:
                    target_url = best[0]["base_url"]
                    for i, s in enumerate(servers):
                        if s["base_url"] == target_url:
                            chosen_index = i
                            break
            if chosen_index is None:
                chosen_index = int(cfg.get("p4", {}).get(
                    "source_server_index", 0) or 0)
                if chosen_index < 0 or chosen_index >= len(servers):
                    chosen_index = 0
            src = servers[chosen_index]
            url = src["base_url"].rstrip("/") + "/raw/latest.rgb565"
            status, data, headers = proxy_binary(
                url, src.get("auth_token", ""), timeout=60.0)
            if status == 200 and data:
                ct = headers.get("Content-Type", "application/octet-stream")
                extra = {"Content-Length": str(len(data)),
                         "X-Director-Source": src.get("name", "?")}
                self._send(200, data, ct, extra)
            else:
                self._send(status or 502,
                           {"error": f"upstream returned {status} for {url}"})
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._send(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/director/trigger":
            with _lock:
                if _state["scheduler"]["in_flight"]:
                    self._send(409, {"error": "already firing"})
                    return
            threading.Thread(target=fire_one_generation,
                             daemon=True, name="trigger").start()
            self._send(202, {"ok": True, "message": "generation fired"})
            return
        self._send(404, {"error": "not found", "path": path})

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/director/config":
            ok, new, err = self._read_json()
            if not ok:
                self._send(400, {"error": err})
                return
            if not isinstance(new, dict):
                self._send(400, {"error": "body must be a JSON object"})
                return
            merged = deep_merge(_cfg_holder["cfg"], new)
            errors = validate_config(merged)
            if errors:
                self._send(422, {"error": "validation failed", "fields": errors})
                return
            try:
                save_config(merged)
            except Exception as e:
                self._send(500, {"error": f"save failed: {e}"})
                return
            _cfg_holder["cfg"] = merged
            with _lock:
                _state["scheduler"]["enabled"] = merged.get(
                    "scheduler", {}).get("enabled", True)
                _state["scheduler"]["interval_seconds"] = merged.get(
                    "scheduler", {}).get("interval_seconds", 1800)
            log("config updated")
            self._send(200, {"ok": True, "config": merged})
            return
        if path.startswith("/api/director/servers/") and path.endswith("/config"):
            try:
                idx = int(path.rsplit("/", 2)[-2])
            except ValueError:
                self._send(400, {"error": "server index must be an int"})
                return
            cfg_servers = _cfg_holder["cfg"].get("servers", [])
            if idx < 0 or idx >= len(cfg_servers):
                self._send(404, {"error": f"no server at index {idx}"})
                return
            ok, payload, err = self._read_json()
            if not ok:
                self._send(400, {"error": err})
                return
            src = cfg_servers[idx]
            url = src["base_url"].rstrip("/") + "/config"
            status, body = http_put(url, payload, src.get("auth_token", ""))
            if isinstance(body, (dict, list)):
                self._send(status or 502, body)
            else:
                self._send(status or 502,
                           {"error": f"upstream returned {status}",
                            "raw": str(body)[:200] if body else None})
            return
        self._send(404, {"error": "not found", "path": path})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_short(body: Any, limit: int = 200) -> Any:
    """Coerce body to a JSON-safe value, truncating long strings."""
    if isinstance(body, (str, int, float, bool, list, dict)) or body is None:
        if isinstance(body, str) and len(body) > limit:
            return body[:limit] + "..."
        return body
    return str(body)[:limit]


def _read_chunked(rfile) -> bytes:
    """Read an HTTP/1.1 chunked transfer body to completion."""
    out = bytearray()
    while True:
        size_line = rfile.readline().strip()
        if not size_line:
            break
        size_str = size_line.split(b";", 1)[0].decode("ascii", errors="replace")
        try:
            size = int(size_str, 16)
        except ValueError:
            break
        if size == 0:
            rfile.readline()
            break
        out.extend(rfile.read(size))
        rfile.readline()
    return bytes(out)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    cfg = load_config()
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            log(f"config error: {e['path']}: {e['message']}")
        log("refusing to start; fix director_config.json")
        return 2
    _cfg_holder["cfg"] = cfg
    try:
        save_config(cfg)
    except Exception as e:
        log(f"could not write director_config.json: {e}")

    with _lock:
        _state["scheduler"]["enabled"] = cfg.get("scheduler", {}).get(
            "enabled", True)
        _state["scheduler"]["interval_seconds"] = cfg.get(
            "scheduler", {}).get("interval_seconds", 1800)

    log(f"director starting: {cfg.get('director', {}).get('name')}")
    log(f"servers: {[s.get('name') + '@' + s.get('base_url') for s in cfg.get('servers', [])]}")
    log(f"scheduler: enabled={cfg.get('scheduler', {}).get('enabled')}, "
        f"interval={cfg.get('scheduler', {}).get('interval_seconds')}s")

    try:
        poll_health()
    except Exception as e:
        log(f"initial health poll error: {e}")
    try:
        poll_gallery()
    except Exception as e:
        log(f"initial gallery poll error: {e}")

    threads = [
        threading.Thread(target=health_loop, daemon=True, name="health"),
        threading.Thread(target=gallery_loop, daemon=True, name="gallery"),
        threading.Thread(target=scheduler_loop, daemon=True, name="scheduler"),
    ]
    for t in threads:
        t.start()

    host = cfg.get("director", {}).get("host", "0.0.0.0")
    port = int(cfg.get("director", {}).get("port", 8091))
    server = ThreadingHTTPServer((host, port), DirectorHandler)
    log(f"director listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down (KeyboardInterrupt)")
    finally:
        _stop.set()
        try:
            server.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
