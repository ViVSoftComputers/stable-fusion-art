#!/usr/bin/env python3
"""Gallery server watchdog — checks port 8090, restarts if dead."""
import socket
import subprocess
import sys
import os

def is_alive():
    try:
        s = socket.create_connection(('127.0.0.1', 8090), timeout=2)
        s.close()
        return True
    except:
        return False

if not is_alive():
    print("Gallery server down, restarting...")
    subprocess.Popen(
        [r"C:\Users\ViV\AppData\Local\Programs\Python\Python312\python.exe",
         os.path.expanduser("~/.hermes/scripts/gallery_server.py")],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print("Restarted")
else:
    print("OK")
