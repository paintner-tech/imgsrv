#!/usr/bin/env python3
# =============================================================================
# Diagnose und Einzeltest fuer den TIFF Processor. Ohne --convert werden
# keine PNGs geschrieben. Mit --convert wird bewusst nur ein TIFF verarbeitet.
# Das Original bleibt erhalten; der Folder Watcher wird nicht gestartet.
# =============================================================================
import argparse
import logging
import sys
from pathlib import Path
from dataclasses import replace
from PIL import Image

if __package__:
    from .config_loader import load_config
    from .tiff_processor import read_tiff_info, select_sampling_parameters, convert_tiff_to_png
else:
    from config_loader import load_config
    from tiff_processor import read_tiff_info, select_sampling_parameters, convert_tiff_to_png


# -----------------------------------------------------------------------------
# FUNKTION: main
# Aufgabe: TIFF und ausgewaehlte Parameter anzeigen, optional umrechnen.
# Parameter: Keine; Kommandozeilenargumente aus sys.argv.
# Rueckgabe: Exit-Code 0 bei Erfolg, 1 bei Fehler.
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='TIFF processor diagnostics')
    parser.add_argument('tiff_file', type=Path)
    parser.add_argument('--config', help='Alternative imageserver.yaml')
    parser.add_argument('--all', action='store_true', help='Show full diagnostic details')
    parser.add_argument('--convert', action='store_true', help='Actually write the PNG')
    parser.add_argument('--output', type=Path, help='Alternative PNG directory')
    args = parser.parse_args()
    try:
        config = load_config(args.config) if args.config else load_config()
        # Ein separates Testverzeichnis verhindert unbeabsichtigtes Ersetzen
        # von produktiven PNGs. Ohne --output gilt die zentrale Konfiguration.
        if args.output:
            config = replace(config, paths=replace(config.paths, separation_png=args.output.resolve()))
        source = args.tiff_file.resolve()
        width, height, dpi = read_tiff_info(source)
        factor, algorithm, join = select_sampling_parameters(config.settings, dpi)
        target = config.paths.separation_png / (source.stem + '.png')
        print(f'Input:  {source}')
        print(f'Output: {target}')
        print(f'Size: {width} x {height}; DPI: {dpi:g}; factor: {factor}; algorithm: {algorithm}; join: {join if algorithm == 2 else 0}')
        if args.all:
            print(f'Config: {config.config_file}')
            print(f'Input bytes: {source.stat().st_size}')
            print(f'Expected PNG: {(width + factor - 1)//factor} x {(height + factor - 1)//factor}, mode L')
            print(f'ICC profile configured: {config.icc_profile}')
            print('ICC transformation: deferred to composite processing')
            print(f'Target already exists: {target.exists()}')
        if args.convert:
            logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
            result = convert_tiff_to_png(source, config, logging.getLogger('test_tiff_processor'))
            with Image.open(result) as image:
                image.load()
                print(f'Created: {result}; mode={image.mode}; size={image.size}; bytes={result.stat().st_size}')
        else:
            print('Diagnostic only. Use --convert to write a PNG.')
        return 0
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

