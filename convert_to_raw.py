#!/usr/bin/env python3
"""Convert gallery images to raw RGB565 for P4 panel display.
Run after each new art generation to keep raw files synced."""

import os
import json
from pathlib import Path
from PIL import Image

GALLERY = Path(os.environ.get("HERMES_GALLERY", os.path.expanduser("~/.hermes/gallery")))
RAW_DIR = GALLERY / "raw"
RAW_DIR.mkdir(exist_ok=True)

DISPLAY_W = 1024
DISPLAY_H = 600


def rgb888_to_rgb565(r, g, b):
    """Convert 8-bit RGB to 16-bit RGB565."""
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def convert_to_raw(image_path: Path) -> Path:
    """Convert an image to raw RGB565 bytes, fitting 1024x600 display."""
    img = Image.open(image_path).convert("RGB")
    
    # Scale to fit display (maintaining aspect ratio, center-crop)
    img_ratio = img.width / img.height
    display_ratio = DISPLAY_W / DISPLAY_H
    
    if img_ratio > display_ratio:
        # Image is wider — scale to height, crop width
        new_h = DISPLAY_H
        new_w = int(DISPLAY_H * img_ratio)
    else:
        # Image is taller — scale to width, crop height
        new_w = DISPLAY_W
        new_h = int(DISPLAY_W / img_ratio)
    
    img = img.resize((new_w, new_h), Image.LANCZOS)
    
    # Center crop
    left = (new_w - DISPLAY_W) // 2
    top = (new_h - DISPLAY_H) // 2
    img = img.crop((left, top, left + DISPLAY_W, top + DISPLAY_H))
    
    # Convert to RGB565
    pixels = img.load()
    raw = bytearray(DISPLAY_W * DISPLAY_H * 2)
    idx = 0
    for y in range(DISPLAY_H):
        for x in range(DISPLAY_W):
            r, g, b = pixels[x, y]
            val = rgb888_to_rgb565(r, g, b)
            raw[idx] = val & 0xFF
            raw[idx + 1] = val >> 8
            idx += 2
    
    # Save raw file
    raw_name = image_path.stem + ".rgb565"
    raw_path = RAW_DIR / raw_name
    raw_path.write_bytes(raw)
    
    # Also save as "latest.rgb565"
    latest = RAW_DIR / "latest.rgb565"
    latest.write_bytes(raw)
    
    print(f"  {raw_name} ({len(raw)} bytes)")
    return raw_path


def sync_all():
    """Convert all gallery images to raw, with latest pointing to the newest."""
    index = GALLERY / "index.json"
    images = []
    latest_entry = None
    
    if index.exists():
        for line in index.read_text().strip().split("\n"):
            if line:
                meta = json.loads(line)
                path = GALLERY / meta["file"]
                if path.exists():
                    images.append(path)
                    latest_entry = path  # last one in index.json = newest
    
    # Also check for any PNG files not in index
    for png in sorted(GALLERY.glob("*.png")):
        if png not in images:
            images.append(png)
    
    print(f"Converting {len(images)} images to RGB565...")
    for img_path in images:
        if img_path.exists():
            convert_to_raw(img_path)
    
    # Ensure latest.rgb565 points to the NEWEST image from index.json
    if latest_entry and latest_entry.exists():
        convert_to_raw(latest_entry)  # this overwrites latest.rgb565
    
    print(f"Done. Raw files in {RAW_DIR}/")
    print(f"Latest: {RAW_DIR}/latest.rgb565 (from {latest_entry.name if latest_entry else 'unknown'})")


if __name__ == "__main__":
    sync_all()
