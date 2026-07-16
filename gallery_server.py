#!/usr/bin/env python3
"""Serve the AI Art Gallery on port 8090."""
import http.server
import os
import sys

GALLERY = os.path.expanduser("~/.hermes/gallery")

class GalleryHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=GALLERY, **kwargs)

    def log_message(self, format, *args):
        pass  # silent

if __name__ == "__main__":
    os.chdir(GALLERY)
    server = http.server.HTTPServer(("0.0.0.0", 8090), GalleryHandler)
    print(f"Gallery at http://192.168.87.43:8090/")
    server.serve_forever()
