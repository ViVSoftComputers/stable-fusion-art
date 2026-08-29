#!/usr/bin/env python3
"""
art_server.py — SD 1.5 art generation server (the "engine").

Serves two things on one port:
  1. The static gallery (images + gallery.html + index.json)
  2. A JSON HTTP API for generating images, reading/writing config, and
     checking health.

Designed for a multi-server / one-director topology:
  - Each server instance binds 0.0.0.0 (reachable on LAN + any overlay like
    Tailscale) and is identified by its ip:port.
  - A director can address N servers uniformly via the same API and use
    GET /health to decide where to send work.

Endpoints (JSON unless noted):
  GET  /                 -> gallery.html (static)
  GET  /<file>           -> any file in the gallery dir (static)
  GET  /index.json       -> image metadata (one JSON object per line)
  GET  /images           -> {"images": [...]} list of metadata, newest first
  GET  /health           -> {"status":"ok","busy":bool,"queue_depth":N,"gallery_count":N}
  GET  /config           -> current generation config
  PUT  /config           -> update generation config (JSON body)
  POST /generate         -> {"prompt":"...", "steps":20, ...} -> {"job_id":"..."}
  GET  /jobs             -> all jobs
  GET  /jobs/<id>        -> one job (status, image, error)

Auth: if the config file sets "auth_token" to a non-empty value, the API
endpoints (/generate, /config, /jobs, /health) require that value in the
`Authorization: Bearer <token>` or `X-Auth-Token` header. Static gallery
serving stays open so browsers can view images.

Generation is async: a single background worker pops jobs off a queue and runs
them one at a time (SD 1.5 holds the GPU, so serializing is correct).
Each job spawns `art_generator.py --one '<json>'` as a subprocess so the model
load is isolated and never contends with other GPU processes (e.g. a local LLM).
"""

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Paths & constants
# --------------------------------------------------------------------------- #

GALLERY = Path(os.environ.get("HERMES_GALLERY", os.path.expanduser("~/.hermes/gallery")))
GALLERY.mkdir(parents=True, exist_ok=True)

HERE = Path(__file__).resolve().parent
ART_GENERATOR = HERE / "art_generator.py"

# The Python that has torch + diffusers installed (pinned 3.12 env).
PYTHON = os.environ.get(
    "ART_PYTHON",
    r"C:\Users\ViV\AppData\Local\Programs\Python\Python312\python.exe",
)

CONFIG_PATH = HERE / "server_config.json"

HOST = os.environ.get("ART_HOST", "0.0.0.0")
PORT = int(os.environ.get("ART_PORT", "8090"))

DEFAULT_CONFIG = {
    "name": "",                  # human label shown in /health (multi-server UI)
    "steps": 20,
    "guidance_scale": 7.5,
    "width": 768,
    "height": 448,
    "auth_token": "",            # empty => auth disabled
}


# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #

def load_config():
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(data)
            return cfg
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# --------------------------------------------------------------------------- #
#  Job store
# --------------------------------------------------------------------------- #

# job_id -> {"id", "status": queued|running|done|failed,
#            "prompt", "params", "image", "error", "created", "finished"}
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_job_queue = queue.Queue()


def _set_job(job_id, **fields):
    with _JOBS_LOCK:
        _JOBS[job_id].update(fields)


# --------------------------------------------------------------------------- #
#  Worker (async generation)
# --------------------------------------------------------------------------- #

def _clean_env():
    """Environment for subprocesses with PYTHONPATH stripped.

    The server may run under a virtualenv (e.g. the Hermes agent venv). If that
    venv's site-packages leak into the Python 3.12 subprocess via PYTHONPATH,
    numpy/PIL/torch resolve to the wrong (cp311) builds and fail at import.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def worker_loop():
    """Pop jobs one at a time and run them via art_generator.py subprocess."""
    while True:
        job = _job_queue.get()
        job_id = job["id"]
        params = job["params"]
        try:
            _set_job(job_id, status="running")
            payload = {
                "prompt": job["prompt"],
                "steps": params.get("steps"),
                "guidance_scale": params.get("guidance_scale"),
                "width": params.get("width"),
                "height": params.get("height"),
            }
            proc = subprocess.run(
                [PYTHON, str(ART_GENERATOR), "--one", json.dumps(payload)],
                capture_output=True, text=True, timeout=600,
                env=_clean_env(),
            )
            # Parse RESULT:<json> from stdout.
            result = None
            for line in proc.stdout.splitlines():
                if line.startswith("RESULT:"):
                    result = json.loads(line[len("RESULT:"):])
                    break
            if result and result.get("ok"):
                _set_job(job_id, status="done",
                         image=result.get("file"), error=None)
            else:
                err = (result or {}).get("error") or "unknown generation failure"
                _set_job(job_id, status="failed", error=err)
        except subprocess.TimeoutExpired:
            _set_job(job_id, status="failed", error="timeout (600s)")
        except Exception as exc:  # noqa: BLE001
            _set_job(job_id, status="failed", error=str(exc))
        finally:
            _set_job(job_id, finished=time.time())
            _job_queue.task_done()


# --------------------------------------------------------------------------- #
#  HTTP handler
# --------------------------------------------------------------------------- #

class ArtServer(BaseHTTPRequestHandler):
    server_version = "art_server/1.0"

    # ---- helpers ----
    def _cfg(self):
        # Load fresh each request so PUT /config is visible immediately.
        return self.server.config

    def _authorized(self):
        token = self._cfg().get("auth_token", "")
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):] == token
        return self.headers.get("X-Auth-Token", "") == token

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """Read request body, handling both Content-Length and chunked encoding.

        This is the fix for http.client hangs: some clients (and the requests
        library with certain settings) send Transfer-Encoding: chunked, which a
        naive Content-Length read silently misses.
        """
        length = self.headers.get("Content-Length")
        if length:
            return self.rfile.read(int(length))

        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                try:
                    size = int(line.split(b";")[0], 16)
                except (ValueError, IndexError):
                    size = 0
                if size == 0:
                    # read trailing CRLF after the 0 chunk
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()  # CRLF after each chunk
            return b"".join(chunks)
        return b""

    def _log(self, msg):
        pass  # silent like the original gallery server

    # ---- routing ----
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        cfg = self._cfg()

        # ---- API routes ----
        if path == "/health":
            if not self._authorized():
                return self._send_json({"error": "unauthorized"}, 401)
            with _JOBS_LOCK:
                busy = any(j["status"] in ("queued", "running") for j in _JOBS.values())
                queue_depth = _job_queue.qsize()
            pngs = list(GALLERY.glob("*.png"))
            return self._send_json({
                "status": "ok",
                "name": cfg.get("name", ""),
                "busy": busy,
                "queue_depth": queue_depth,
                "gallery_count": len(pngs),
            })

        if path == "/config":
            if not self._authorized():
                return self._send_json({"error": "unauthorized"}, 401)
            return self._send_json(cfg)

        if path == "/images":
            return self._send_json({"images": self._list_images()})

        # P4 panel framebuffer — the raw RGB565 the panel fetches.
        if path == "/raw/latest.rgb565":
            return self._serve_raw()

        if path == "/jobs":
            if not self._authorized():
                return self._send_json({"error": "unauthorized"}, 401)
            with _JOBS_LOCK:
                jobs = list(_JOBS.values())
            jobs.sort(key=lambda j: j.get("created", 0), reverse=True)
            return self._send_json({"jobs": jobs})

        if path.startswith("/jobs/"):
            if not self._authorized():
                return self._send_json({"error": "unauthorized"}, 401)
            job_id = path[len("/jobs/"):]
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
            if not job:
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(job)

        # ---- static gallery ----
        self._serve_static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/generate":
            if not self._authorized():
                return self._send_json({"error": "unauthorized"}, 401)
            try:
                req = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._send_json({"error": "invalid JSON body"}, 400)
            prompt = (req.get("prompt") or "").strip()
            if not prompt:
                return self._send_json({"error": "prompt is required"}, 400)
            cfg = self._cfg()
            params = {
                "steps": req.get("steps", cfg.get("steps", 20)),
                "guidance_scale": req.get("guidance_scale", cfg.get("guidance_scale", 7.5)),
                "width": req.get("width", cfg.get("width", 768)),
                "height": req.get("height", cfg.get("height", 448)),
            }
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "status": "queued",
                "prompt": prompt,
                "params": params,
                "image": None,
                "error": None,
                "created": time.time(),
                "finished": None,
            }
            with _JOBS_LOCK:
                _JOBS[job_id] = job
            _job_queue.put(job)
            return self._send_json({"job_id": job_id, "status": "queued"}, 202)
        return self._send_json({"error": "not found"}, 404)

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        if path == "/config":
            if not self._authorized():
                return self._send_json({"error": "unauthorized"}, 401)
            try:
                req = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._send_json({"error": "invalid JSON body"}, 400)
            cfg = self._cfg()
            for key in DEFAULT_CONFIG:
                if key in req:
                    cfg[key] = req[key]
            self.server.config = cfg
            save_config(cfg)
            return self._send_json(cfg)
        return self._send_json({"error": "not found"}, 404)

    # ---- static serving ----
    def _serve_raw(self):
        """Serve the P4 panel's RGB565 framebuffer (latest.rgb565)."""
        raw_path = GALLERY / "raw" / "latest.rgb565"
        if not raw_path.is_file():
            return self._send_json({"error": "no raw frame yet"}, 404)
        data = raw_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _list_images(self):
        items = []
        seen = set()
        index = GALLERY / "index.json"
        if index.exists():
            for line in index.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    meta = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = meta.get("file", "")
                if name and (GALLERY / name).exists() and name not in seen:
                    seen.add(name)
                    items.append(meta)
        # Also include any PNG not in index.json
        for png in sorted(GALLERY.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True):
            if png.name not in seen:
                seen.add(png.name)
                items.append({"file": png.name, "prompt": "", "style": "SD 1.5",
                              "timestamp": ""})
        return items

    def _serve_static(self, path):
        # The gallery UI page lives in the repo, not the image gallery dir.
        if path == "/":
            target = HERE / "gallery.html"
            ctype = "text/html"
        else:
            name = path.lstrip("/")
            target = (GALLERY / name).resolve()
            # Prevent path traversal outside the gallery dir.
            if not str(target).startswith(str(GALLERY.resolve())):
                return self._send_json({"error": "forbidden"}, 403)
            ctype = "image/png" if target.suffix == ".png" else \
                    "text/html" if target.suffix == ".html" else \
                    "application/json" if target.suffix == ".json" else \
                    "application/octet-stream"
        if not target.is_file():
            return self._send_json({"error": "not found"}, 404)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main():
    # Start worker thread (daemon so it dies with the process).
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

    server = ThreadingHTTPServer((HOST, PORT), ArtServer)
    server.config = load_config()
    print(f"Art server on http://{HOST}:{PORT}/  (gallery: {GALLERY})")
    print("Endpoints: /generate /jobs /config /health /images + static gallery")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
