#!/usr/bin/env python3
"""Erzeugt aus vier Graustufen-PNG-Separationen ein farbiges RGB-PNG.

Grundlage: rgbGray2TrueNew() aus dem alten Imageserver (rgb.c).
Die Separationen bleiben unveraendert.
"""

import argparse
import os
from pathlib import Path

import numpy as np
from PIL import Image

try:
    from modules.config_loader import load_config
    from modules.library.logger import setup_rotating_logger
except ModuleNotFoundError as error:
    if error.name != 'modules':
        raise
    from config_loader import load_config
    from library.logger import setup_rotating_logger


# Findet zu einer PNG-Datei die vier zusammengehoerenden Separationen.
# Rueckgabe: Dictionary mit den Schluesseln C, M, Y und K und den Dateipfaden.
def find_separations(one_png, config):
    one_png = Path(one_png).resolve()
    section = config.settings['filenames']
    color_position = int(section.get('color_position', '-1'))
    colors = {
        'C': section.get('cyan', 'C'),
        'M': section.get('magenta', 'M'),
        'Y': section.get('yellow', 'Y'),
        'K': section.get('black', 'K'),
    }
    if any(len(value) != 1 for value in colors.values()):
        raise ValueError('Die Farbkennzeichen in [filenames] muessen je ein Zeichen haben.')
    if len({value.upper() for value in colors.values()}) != 4:
        raise ValueError('Die vier Farbkennzeichen muessen unterschiedlich sein.')
    if one_png.suffix.lower() != '.png':
        raise ValueError('Bitte eine Separations-PNG-Datei angeben.')
    if not one_png.is_file():
        raise FileNotFoundError(one_png)
    if one_png.parent != config.paths.separation_png.resolve():
        raise ValueError(f'Die PNG-Datei muss in {config.paths.separation_png} liegen.')

    stem = one_png.stem
    position = color_position if color_position >= 0 else len(stem) + color_position
    if position < 0 or position >= len(stem):
        raise ValueError(f'color_position={color_position} liegt ausserhalb des Dateinamens: {stem}')
    if stem[position].upper() not in {value.upper() for value in colors.values()}:
        raise ValueError(f'An Position {color_position} steht kein Farbkennzeichen: {one_png.name}')

    result = {}
    # Gross-/Kleinschreibung der Dateiendung wird nicht erraten: Der TIFF-Prozessor schreibt .png.
    for color, letter in colors.items():
        name = stem[:position] + letter + stem[position + 1:] + '.png'
        filename = one_png.parent / name
        if not filename.is_file():
            raise FileNotFoundError(f'Separation {color} fehlt: {filename}')
        result[color] = filename
    return result


# Bestimmt aus dem Namen einer Separation den Namen des fertigen 4C-PNGs.
# Rueckgabe: Dateiname ohne Farbkennzeichen, mit Endung .png.
def composite_filename(one_png, config):
    stem = Path(one_png).stem
    position_config = config.settings.getint('filenames', 'color_position', fallback=-1)
    position = position_config if position_config >= 0 else len(stem) + position_config
    name = stem[:position] + stem[position + 1:]
    # Trenner unmittelbar vor der ehemaligen Farbangabe entfernen, z.B. Auftrag_C -> Auftrag.
    if position == len(stem) - 1:
        name = name.rstrip('_.- ')
    if not name:
        raise ValueError('Aus dem Dateinamen kann kein Auftragsname abgeleitet werden.')
    return name + '.png'


# Prueft, ob alle vier PNGs Graustufenbilder mit identischer Groesse sind.
# Rueckgabe: (Breite, Hoehe, DPI) der Bilder.
def inspect_separations(files):
    size = None
    dpi = None
    for color, filename in files.items():
        with Image.open(filename) as image:
            if image.mode != 'L':
                raise ValueError(f'Separation {color} muss 8-Bit-Graustufen-PNG sein, gefunden: {image.mode}')
            if size is None:
                size = image.size
                dpi_value = image.info.get('dpi', (0, 0))
                dpi = float(dpi_value[0] if isinstance(dpi_value, tuple) else dpi_value)
            elif image.size != size:
                raise ValueError(f'Bildgroesse von {color} weicht ab: {image.size} statt {size}')
    return size[0], size[1], dpi


# Mischt CMYK-Dichten zu RGB, wie rgbGray2TrueNew() ohne ICC-Wuerfel.
# Rueckgabe: NumPy-Array der Form (Hoehe, Breite, 3), Datentyp uint8.
def combine_cmyk(cyan, magenta, yellow, black):
    # Der TIFF-Prozessor schreibt normale PNG-Grauwerte: Weiss=255, Schwarz=0.
    # Der alte C-Code verarbeitet dagegen Farbdichten: Weiss=0, Schwarz=255.
    c = 255 - cyan.astype(np.int16)
    m = 255 - magenta.astype(np.int16)
    y = 255 - yellow.astype(np.int16)
    k = 255 - black.astype(np.int16)
    rgb = np.empty((*cyan.shape, 3), dtype=np.uint8)
    rgb[:, :, 0] = 255 - np.minimum(255, c + k)
    rgb[:, :, 1] = 255 - np.minimum(255, m + k)
    rgb[:, :, 2] = 255 - np.minimum(255, y + k)
    return rgb


# Erstellt das zusammengesetzte RGB-PNG aus vier vorhandenen Separationen.
# Rueckgabe: Pfad zum fertigen PNG (Path). Bei Fehler bleibt das bisherige Ziel unangetastet.
def create_composite(one_png, config, logger):
    settings = config.settings['composite']
    if settings.get('output_format', 'png').lower() != 'png':
        raise ValueError('Derzeit wird nur output_format = png unterstuetzt.')
    if settings.get('output_mode', 'RGB').upper() != 'RGB':
        raise ValueError('Derzeit wird nur output_mode = RGB unterstuetzt.')
    if settings.getboolean('icc_enabled', fallback=False):
        raise ValueError('icc_enabled = true ist noch nicht implementiert.')
    if settings.getboolean('simulate_paper_color', fallback=False):
        raise ValueError('simulate_paper_color = true ist noch nicht implementiert.')

    max_width = int(settings.get('max_width', '1440'))
    max_height = int(settings.get('max_height', '2560'))
    if max_width <= 0 or max_height <= 0:
        raise ValueError('max_width und max_height muessen groesser als 0 sein.')

    files = find_separations(one_png, config)
    width, height, dpi = inspect_separations(files)
    logger.info('Erzeuge 4C aus: %s', ', '.join(str(files[c].name) for c in 'CMYK'))
    logger.info('Ausgangsgroesse: %dx%d', width, height)

    # In Streifen arbeiten, um bei grossen Bildern wenig RAM zu verbrauchen.
    result = Image.new('RGB', (width, height))
    images = [Image.open(files[color]) for color in 'CMYK']
    try:
        for top in range(0, height, 256):
            bottom = min(height, top + 256)
            strips = [np.asarray(img.crop((0, top, width, bottom)), dtype=np.uint8) for img in images]
            result.paste(Image.fromarray(combine_cmyk(*strips), mode='RGB'), (0, top))
    finally:
        for image in images:
            image.close()

    # Nur verkleinern, nicht vergroessern; Seitenverhaeltnis beibehalten.
    result.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    output_dir = config.paths.composite_png
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / composite_filename(one_png, config)
    temp_file = output_dir / (output_file.name + f'.{os.getpid()}.tmp')
    try:
        # DPI auf die verringerte Pixelzahl anpassen.
        options = {}
        if dpi and dpi > 0:
            output_dpi = dpi * (result.width / width + result.height / height) / 2
            options['dpi'] = (output_dpi, output_dpi)
        result.save(temp_file, format='PNG', **options)
        os.replace(temp_file, output_file)
    finally:
        temp_file.unlink(missing_ok=True)

    logger.info('4C-PNG geschrieben: %s (%dx%d)', output_file, result.width, result.height)
    return output_file


# Startet das Modul zum Testen mit einer angegebenen Separation.
# Rueckgabe: 0 bei Erfolg, 1 bei Fehler.
def main():
    parser = argparse.ArgumentParser(description='Erzeugt ein 4C-RGB-PNG aus C/M/Y/K-PNGs')
    parser.add_argument('png_file', help='Pfad zu einer der vier PNG-Separationen')
    parser.add_argument('--config', help='Alternativer Pfad zur imageserver.ini')
    args = parser.parse_args()
    logger = None
    try:
        config = load_config(args.config) if args.config else load_config()
        log_path = config.log_files['composite']
        logger = setup_rotating_logger('composite', log_path.parent, log_path.name)
        logger.setLevel(config.log_level)
        output_file = create_composite(args.png_file, config, logger)
        print(f'Erstellt: {output_file}')
        return 0
    except Exception as error:
        if logger:
            logger.exception('4C-Erstellung fehlgeschlagen: %s', error)
        print(f'FEHLER: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

