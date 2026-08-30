#!/usr/bin/env python3
"""Art server watchdog — checks port 8090, restarts art_server.py if dead."""
import socket
import subprocess
import sys
from pathlib import Path

def is_alive():
    try:
        s = socket.create_connection(('127.0.0.1', 8090), timeout=2)
        s.close()
        return True
    except Exception:
        return False

if not is_alive():
    print("Art server down, restarting...")
    # art_server.py is stdlib-only and lives alongside this watchdog, so launch
    # it with the current interpreter and a relative path (works in the repo
    # and in any deployed scripts directory alike).
    server = Path(__file__).resolve().parent / "art_server.py"
    subprocess.Popen(
        [sys.executable, str(server)],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print("Restarted")
else:
    print("OK")
