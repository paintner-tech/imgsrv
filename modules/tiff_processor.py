#!/usr/bin/env python3
"""Wandelt 1-Bit-TIFF-Separationen mit dem Join-Algorithmus in PNG um.

Der Algorithmus orientiert sich an rgbTiffJoin2GrayNew() aus rgb.c.
Die Quelldatei wird niemals geloescht oder veraendert.
"""

import argparse
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image
# Unsere Druck-TIFFs können mehrere hundert Millionen Pixel haben.
# Die Dateien stammen aus einer kontrollierten Produktionsumgebung.
# Deshalb deaktivieren wir die Pillow-Pixelbegrenzung.
Image.MAX_IMAGE_PIXELS = None

try:
    from modules.config_loader import load_config
    from modules.library.logger import setup_rotating_logger
except ModuleNotFoundError as error:
    if error.name != 'modules':
        raise
    from config_loader import load_config
    from library.logger import setup_rotating_logger


# Liest Aufloesung und Bildgroesse einer TIFF-Datei.
# Rueckgabe: (Breite, Hoehe, DPI) als Zahlen.
def read_tiff_info(filename):
    with Image.open(filename) as image:
        if image.format != 'TIFF':
            raise ValueError('Die Eingabedatei ist keine TIFF-Datei.')
        if image.mode != '1':
            raise ValueError(f'Nur 1-Bit-TIFF wird unterstuetzt, gefunden: {image.mode}')
        width, height = image.size
        dpi_value = image.info.get('dpi', (0, 0))
        dpi = float(dpi_value[0] if isinstance(dpi_value, (tuple, list)) else dpi_value)
        if dpi <= 0 or not math.isfinite(dpi):
            raise ValueError('TIFF hat keine gueltige DPI-Angabe.')
        return width, height, dpi


# Waehlt den zum TIFF-DPI-Wert passenden Parametersatz aus der INI.
# Rueckgabe: (Faktor, Algorithmus, Join) als Integer.
def select_sampling_parameters(settings, dpi):
    section = settings['processing']
    tolerance = float(section.get('dpi_tolerance', '100'))
    candidates = []
    for key, value in section.items():
        if not key.startswith('dpi_config_'):
            continue
        parts = [part.strip() for part in value.split(',')]
        if len(parts) != 7:
            raise ValueError(f'Falsches Format bei [processing] {key}: 7 Werte erwartet')
        target_dpi = float(parts[0])
        factor, algorithm, join = map(int, parts[1:4])
        candidates.append((abs(dpi - target_dpi), target_dpi, factor, algorithm, join))
    if not candidates:
        raise ValueError('Keine dpi_config_N unter [processing] gefunden.')
    difference, target_dpi, factor, algorithm, join = min(candidates)
    if difference > tolerance:
        raise ValueError(f'Keine passende DPI-Konfiguration: TIFF={dpi:g}, naechster Wert={target_dpi:g}, Toleranz={tolerance:g}')
    if factor < 8 or factor % 8 != 0:
        raise ValueError(f'Ungueltiger Faktor {factor}: Vielfaches von 8 erforderlich.')
    if join < 0 or join > min(8, factor // 2):
        raise ValueError(f'Ungueltiger Join-Wert: {join}')
    if algorithm not in (0, 2):
        raise ValueError(f'Algorithmus {algorithm} ist noch nicht implementiert.')
    return factor, algorithm, join


# Berechnet die Schwarzpixelzahlen fuer einen Ausschnitt eines Blocks.
# Rueckgabe: Ein Integer-Array mit einer Zahl pro Ausgabespalte.
def count_region(tiles, row_start, row_end, col_start, col_end):
    return tiles[:, row_start:row_end, col_start:col_end].sum(axis=(1, 2), dtype=np.int64)


# Entrastert ein 1-Bit-TIFF blockweise nach dem alten Join-Verfahren.
# Rueckgabe: Ein Pillow-Graustufenbild mit Weiss=255 und Schwarz=0.
def join_to_grayscale(filename, factor, join):
    with Image.open(filename) as image:
        if image.mode != '1':
            raise ValueError('Die Verarbeitung erwartet ein 1-Bit-TIFF.')
        width, height = image.size
        out_width = (width + factor - 1) // factor
        out_height = (height + factor - 1) // factor
        density = np.zeros((out_height, out_width), dtype=np.int64)
        area = factor * factor

        for row in range(out_height):
            y = row * factor
            # Nur einen Streifen von factor Zeilen gleichzeitig bearbeiten.
            strip = np.asarray(image.crop((0, y, width, min(y + factor, height))).convert('L'))
            black = (strip == 0).astype(np.uint8)
            padded = np.zeros((factor, out_width * factor), dtype=np.uint8)
            padded[:black.shape[0], :width] = black
            tiles = padded.reshape(factor, out_width, factor).transpose(1, 0, 2)

            total = count_region(tiles, 0, factor, 0, factor)
            density[row] += (total * 255) // area
            if join == 0:
                continue

            # Genau wie im C-Code: 2x Seitenstreifen + 1x Eckbereiche.
            tl = count_region(tiles, 0, join, 0, join)
            tr = count_region(tiles, 0, join, factor - join, factor)
            bl = count_region(tiles, factor - join, factor, 0, join)
            br = count_region(tiles, factor - join, factor, factor - join, factor)
            top = count_region(tiles, 0, join, join, factor - join)
            bottom = count_region(tiles, factor - join, factor, join, factor - join)
            left = count_region(tiles, join, factor - join, 0, join)
            right = count_region(tiles, join, factor - join, factor - join, factor)

            transfer_top = ((2 * top + tl + tr) * 255) // (4 * area)
            transfer_bottom = ((2 * bottom + bl + br) * 255) // (4 * area)
            transfer_left = ((2 * left + tl + bl) * 255) // (4 * area)
            transfer_right = ((2 * right + tr + br) * 255) // (4 * area)

            if row > 0:
                density[row - 1] += transfer_top
                density[row] -= transfer_top
            if row < out_height - 1:
                density[row + 1] += transfer_bottom
                density[row] -= transfer_bottom
            density[row, :-1] += transfer_left[1:]
            density[row, 1:] += transfer_right[:-1]
            density[row, 1:] -= transfer_left[1:]
            density[row, :-1] -= transfer_right[:-1]

        # Das Original rechnet in unsigned-char-Pixeln (Modulo 256).
        # Aus den internen Dichtewerten werden normale PNG-Grauwerte.
        gray = 255 - (density % 256).astype(np.uint8)
        return Image.fromarray(gray, mode='L')


# Erzeugt fuer eine TIFF-Separation eine PNG-Datei mit atomarem Umbenennen.
# Rueckgabe: Vollstaendiger Pfad der erzeugten PNG-Datei (Path).
def convert_tiff_to_png(tiff_file, config, logger):
    tiff_file = Path(tiff_file).resolve()
    if not tiff_file.is_file():
        raise FileNotFoundError(tiff_file)
    width, height, dpi = read_tiff_info(tiff_file)
    factor, algorithm, join = select_sampling_parameters(config.settings, dpi)
    if algorithm == 0:
        join = 0
    logger.info('TIFF: %s, %dx%d, %.2f dpi, Faktor=%d, Algorithmus=%d, Join=%d',
                tiff_file.name, width, height, dpi, factor, algorithm, join)
    output_dir = config.paths.separation_png
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / (tiff_file.stem + '.png')
    temp_file = output_dir / (output_file.name + f'.{os.getpid()}.tmp')

    try:
        image = join_to_grayscale(tiff_file, factor, join)
        output_dpi = dpi * (image.width / width + image.height / height) / 2
        image.save(temp_file, format='PNG', dpi=(output_dpi, output_dpi))
        os.replace(temp_file, output_file)
        logger.info('PNG geschrieben: %s (%dx%d)', output_file, image.width, image.height)
        return output_file
    finally:
        temp_file.unlink(missing_ok=True)


# Liest die Konfiguration und verarbeitet genau eine angegebene TIFF-Datei.
# Rueckgabe: 0 bei Erfolg oder 1 bei einem Fehler.
def main():
    parser = argparse.ArgumentParser(description='TIFF-Separation in Graustufen-PNG umwandeln')
    parser.add_argument('tiff_file', help='Pfad zu einer 1-Bit-TIFF-Datei')
    parser.add_argument('--config', help='Alternativer Pfad zur imageserver.ini')
    args = parser.parse_args()
    try:
        config = load_config(args.config) if args.config else load_config()
        log_path = config.log_files['tiff_processor']
        logger = setup_rotating_logger('tiff_processor', log_path.parent, log_path.name)
        logger.setLevel(config.log_level)
        output_file = convert_tiff_to_png(args.tiff_file, config, logger)
        print(f'Erstellt: {output_file}')
        return 0
    except Exception as error:
        if 'logger' in locals():
            logger.exception('TIFF-Verarbeitung fehlgeschlagen: %s', error)
        print(f'FEHLER: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

