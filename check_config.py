#!/usr/bin/env python3
"""Prueft die Konfiguration ohne Ordner anzulegen oder Dateien zu veraendern."""
from pathlib import Path
from modules.config_loader import load_config, identify_separation

if __name__ == "__main__":
    conf = load_config(Path(__file__).parent / "config/imageserver.ini")
    print("Konfiguration OK")
    for name in ("tiff_in", "separation_png", "composite_png", "logs"):
        print(f"{name:18}: {getattr(conf, name)}")
    for name, sec in conf.retention_seconds.items():
        print(f"Aufbewahrung {name:14}: {sec // 3600} Stunden")
    for filename in ("A2B0BSV072301AAC.tif", "A2B0BSV072301AAK.tif"):
        print(f"{filename}: {identify_separation(filename, conf)}")
