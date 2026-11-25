#!/usr/bin/env python3
# =============================================================================
# TIFF PROCESSOR - Imageserver V2
# =============================================================================
# Entrastert 1-Bit-TIFFs mit dem bisherigen Join-Verfahren zu neutralen
# Graustufen-PNGs. Dateinamen bleiben bis auf die Endung unveraendert.
# Keine Abhaengigkeit von Auftraegen oder Produktinformationen.
# ICC-Druckprofile werden erst beim CMYK-Composite angewendet: Eine einzelne
# Separation enthaelt keine Information ueber die anderen Farbkanäle.
# Originaldateien werden weder geloescht noch verschoben. Das ist Aufgabe
# des Folder Watchers. Die PNG-Datei wird erst nach fertiger Ausgabe sichtbar.
# =============================================================================
import argparse
import logging
import math
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

if __package__:
    from .config_loader import load_config
else:
    from config_loader import load_config

# Kontrollierte Produktionsdaten: sehr grosse Druck-TIFFs sind normal.
# Achtung: Pillow kann trotzdem das gesamte Eingangsbild im RAM dekodieren.
Image.MAX_IMAGE_PIXELS = None


# -----------------------------------------------------------------------------
# FUNKTION: read_tiff_info
# Aufgabe: Dateiformat, Bittiefe, Abmessungen und Aufloesung kontrollieren.
# Parameter: filename - Pfad der Eingabedatei (str oder Path).
# Rueckgabe: Tupel (Breite, Hoehe, DPI in X-Richtung).
# Fehler: OSError bei Lesefehlern, ValueError bei ungueltigen Bilddaten.
# -----------------------------------------------------------------------------
def read_tiff_info(filename):
    with Image.open(filename) as image:
        if image.format != 'TIFF':
            raise ValueError('Input is not a TIFF file.')
        if image.mode != '1':
            raise ValueError(f'Only 1-bit TIFF supported, found {image.mode}')
        width, height = image.size
        dpi_value = image.info.get('dpi', (0, 0))
        dpi = float(dpi_value[0] if isinstance(dpi_value, (tuple, list)) else dpi_value)
        if dpi <= 0 or not math.isfinite(dpi):
            raise ValueError('TIFF has no valid DPI information.')
        return width, height, dpi


# -----------------------------------------------------------------------------
# FUNKTION: select_sampling_parameters
# Aufgabe: Naechsten YAML-DPI-Parametersatz innerhalb der Toleranz auswaehlen.
# Parameter: settings - Konfigurationsdictionary; dpi - TIFF-Aufloesung.
# Rueckgabe: Tupel (factor, algorithm, join) als Integer.
# Fehler: ValueError bei fehlendem oder nicht unterstuetztem Parametersatz.
# -----------------------------------------------------------------------------
def select_sampling_parameters(settings, dpi):
    section = settings['processing']
    tolerance = float(section['dpi_tolerance'])
    # Bei gleicher Abweichung wird wie zuvor der kleinere DPI-Wert bevorzugt.
    candidates = [(abs(dpi - float(item['dpi'])), float(item['dpi']),
                   item['factor'], item['algorithm'], item['join'])
                  for item in section['dpi_configs']]
    if not candidates:
        raise ValueError('No DPI configurations found.')
    difference, target_dpi, factor, algorithm, join = min(candidates)
    if difference > tolerance:
        raise ValueError(f'No matching DPI configuration: TIFF={dpi:g}, nearest={target_dpi:g}, tolerance={tolerance:g}')
    if factor < 8 or factor % 8 != 0:
        raise ValueError(f'Invalid factor {factor}: multiple of 8 required.')
    if join < 0 or join > min(8, factor // 2):
        raise ValueError(f'Invalid join value: {join}')
    if algorithm not in (0, 2):
        raise ValueError(f'Unsupported algorithm: {algorithm}')
    return factor, algorithm, join


# -----------------------------------------------------------------------------
# FUNKTION: count_region
# Aufgabe: Schwarzpixel in einem rechteckigen Teil jedes Ausgabeblocks zaehlen.
# Parameter: tiles - Array (Block, Zeile, Spalte); row_start, row_end -
#   Zeilengrenzen; col_start, col_end - Spaltengrenzen (Ende exklusiv).
# Rueckgabe: int64-Array mit einer Schwarzpixelanzahl pro Block.
# -----------------------------------------------------------------------------
def count_region(tiles, row_start, row_end, col_start, col_end):
    return tiles[:, row_start:row_end, col_start:col_end].sum(axis=(1, 2), dtype=np.int64)


# -----------------------------------------------------------------------------
# FUNKTION: join_to_grayscale
# Aufgabe: Bisherige blockweise Entrasterung inklusive Join-Verteilung.
# Parameter: filename - TIFF-Pfad; factor - Kantenlaenge eines Rasterblocks;
#   join - Breite des Randbereichs (0 deaktiviert die Verteilung).
# Rueckgabe: Pillow-Bild im Modus L, Weiss=255, Schwarz=0.
# Fehler: ValueError bei falschem Bildmodus, OSError bei Lesefehlern.
# -----------------------------------------------------------------------------
def join_to_grayscale(filename, factor, join):
    with Image.open(filename) as image:
        if image.mode != '1':
            raise ValueError('Processing requires a 1-bit TIFF.')
        width, height = image.size
        # Aufrunden: auch unvollstaendige Randbloecke bleiben erhalten.
        out_width = (width + factor - 1) // factor
        out_height = (height + factor - 1) // factor
        density = np.zeros((out_height, out_width), dtype=np.int64)
        area = factor * factor
        for row in range(out_height):
            y = row * factor
            # NumPy verarbeitet nur einen Streifen gleichzeitig. Fehlende
            # Randpixel werden wie im bisherigen Algorithmus weiss aufgefuellt.
            strip = np.asarray(image.crop((0, y, width, min(y + factor, height))).convert('L'))
            black = (strip == 0).astype(np.uint8)
            padded = np.zeros((factor, out_width * factor), dtype=np.uint8)
            padded[:black.shape[0], :width] = black
            tiles = padded.reshape(factor, out_width, factor).transpose(1, 0, 2)
            # Grunddichte entspricht dem Anteil schwarzer Rasterpixel.
            total = count_region(tiles, 0, factor, 0, factor)
            density[row] += (total * 255) // area
            if join == 0:
                continue
            # Eckbereiche und Seitenstreifen getrennt zaehlen. Die Gewichte
            # bleiben exakt wie in der bisherigen Python-Implementierung.
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
            # Dichte nur an existierende Nachbarbloecke abgeben. Am Bildrand
            # gibt es keinen Nachbarn; dort bleibt der Anteil im eigenen Block.
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
        # Modulo 256 bewusst unveraendert: entspricht dem bisherigen Verhalten
        # der unsigned-char-Arithmetik. Keine neue Tonwertkorrektur anwenden.
        gray = 255 - (density % 256).astype(np.uint8)
        return Image.fromarray(gray)


# -----------------------------------------------------------------------------
# FUNKTION: convert_tiff_to_png
# Aufgabe: TIFF umrechnen und fertiges PNG atomar veroeffentlichen.
# Parameter: tiff_file - Eingabepfad; config - geladene ImageServerConfig;
#   logger - Logger des aufrufenden Moduls.
# Rueckgabe: Absoluter Pfad zum fertigen PNG (Path).
# Fehler: Datei-, Konfigurations- und Bildfehler werden an den Aufrufer gegeben.
# -----------------------------------------------------------------------------
def convert_tiff_to_png(tiff_file, config, logger):
    start = time.monotonic()
    tiff_file = Path(tiff_file).resolve()
    if not tiff_file.is_file():
        raise FileNotFoundError(tiff_file)
    before = tiff_file.stat()
    width, height, dpi = read_tiff_info(tiff_file)
    factor, algorithm, join = select_sampling_parameters(config.settings, dpi)
    if algorithm == 0:
        join = 0
    logger.info('TIFF: %s, %dx%d, %.2f dpi, factor=%d, algorithm=%d, join=%d', tiff_file, width, height, dpi, factor, algorithm, join)
    output_dir = config.paths.separation_png
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / (tiff_file.stem + '.png')
    # Eindeutiger temporaerer Name auch bei mehreren Threads/Prozessen.
    # Gleicher Ordner garantiert atomaren Austausch auf demselben Dateisystem.
    fd, name = tempfile.mkstemp(prefix='.' + output_file.name + '.', suffix='.tmp', dir=output_dir)
    temp_file = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            with join_to_grayscale(tiff_file, factor, join) as image:
                output_dpi = dpi * (image.width / width + image.height / height) / 2
                # Keine CMYK-ICC-Daten in ein neutrales Graustufen-PNG einbetten.
                image.save(stream, format='PNG', dpi=(output_dpi, output_dpi))
                output_size = image.size
            stream.flush()
            os.fsync(stream.fileno())
        # Wenn sich die Quelle waehrend der Umrechnung aendert, das Ergebnis
        # verwerfen. Der Watcher kann spaeter einen neuen Versuch starten.
        after = tiff_file.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError(f'TIFF changed during conversion: {tiff_file}')
        # mkstemp legt Dateien mit 0600 an. Fuer den PNG-Austausch mit anderen
        # lokalen Diensten die bisher ueblichen Leserechte setzen.
        os.chmod(temp_file, 0o644)
        os.replace(temp_file, output_file)
        logger.info('PNG written: %s (%dx%d) in %.2f s', output_file, *output_size, time.monotonic() - start)
        return output_file
    finally:
        temp_file.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# FUNKTION: main
# Aufgabe: Einzelne TIFF-Datei mit der YAML-Konfiguration umrechnen.
# Parameter: Keine; Kommandozeilenargumente aus sys.argv.
# Rueckgabe: Exit-Code 0 bei Erfolg, 1 bei Fehler.
# -----------------------------------------------------------------------------
def main():
    from logging.handlers import RotatingFileHandler
    parser = argparse.ArgumentParser(description='Convert 1-bit TIFF to grayscale PNG')
    parser.add_argument('tiff_file')
    parser.add_argument('--config', help='Alternative imageserver.yaml')
    args = parser.parse_args()
    try:
        config = load_config(args.config) if args.config else load_config()
        path = config.log_files['tiff_processor']
        path.parent.mkdir(parents=True, exist_ok=True)
        settings = config.settings['logging']
        logger = logging.getLogger('tiff_processor')
        logger.setLevel(config.log_level)
        handler = RotatingFileHandler(path, maxBytes=settings['max_size_mb'] * 1024 * 1024, backupCount=settings['backup_count'], encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)
        if settings['console_output']:
            logger.addHandler(logging.StreamHandler())
        output = convert_tiff_to_png(args.tiff_file, config, logger)
        print(f'Created: {output}')
        return 0
    except Exception as error:
        if 'logger' in locals():
            logger.exception('TIFF conversion failed')
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

