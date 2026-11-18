#!/usr/bin/env python3
"""Ueberwacht TIFF-Eingang und PNG-Separationen des Imageservers.

Ablauf:
  1. Vollstaendig geschriebene TIFFs nach PNG konvertieren.
  2. Erfolgreich konvertierte TIFFs nach tiffs-done verschieben.
  3. Aus vier fertigen C/M/Y/K-PNGs ein Composite-PNG erzeugen.

Das Modul loescht keine Dateien. Die spaetere Bereinigung uebernimmt cleanup.py.
Fuer den Anfang ist nur EINE laufende Watcher-Instanz vorgesehen.
"""

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

try:
    from modules.config_loader import load_config
    from modules.library.logger import setup_rotating_logger
    from modules.tiff_processor import convert_tiff_to_png
    from modules.composite import create_composite, composite_filename
except ModuleNotFoundError as error:
    # Ermoeglicht auch: cd /pecom/imgsrv/modules && python3 folder_watcher.py
    if error.name != 'modules':
        raise
    from config_loader import load_config
    from library.logger import setup_rotating_logger
    from tiff_processor import convert_tiff_to_png
    from composite import create_composite, composite_filename


# Prueft, ob sich eine Datei seit dem vorigen Scan nicht geaendert hat.
# Zusaetzlich muss die letzte Aenderung mindestens stable_seconds zurueckliegen.
# Rueckgabe: True (stabil) oder False (noch nicht stabil).
def is_file_stable(filename, stable_seconds, observations):
    try:
        stat = filename.stat()
    except FileNotFoundError:
        observations.pop(filename, None)
        return False

    current = (stat.st_size, stat.st_mtime_ns)
    previous = observations.get(filename)
    observations[filename] = current
    return previous == current and time.time() - stat.st_mtime >= stable_seconds


# Sucht alle TIFFs mit erlaubten Endungen im konfigurierten Eingangsordner.
# Rueckgabe: Alphabetisch sortierte Liste von Path-Objekten.
def list_tiff_files(config):
    extensions = config.settings.get('filenames', 'extensions', fallback='.tif,.tiff')
    allowed = {item.strip().lower() for item in extensions.split(',') if item.strip()}
    if not allowed:
        raise ValueError('Keine TIFF-Endungen in [filenames] extensions konfiguriert.')
    return sorted(path for path in config.paths.tiff_in.iterdir()
                  if path.is_file() and path.suffix.lower() in allowed)


# Sucht alle PNG-Separationen im konfigurierten Ausgabeordner.
# Rueckgabe: Alphabetisch sortierte Liste von Path-Objekten.
def list_png_files(config):
    return sorted(path for path in config.paths.separation_png.iterdir()
                  if path.is_file() and path.suffix.lower() == '.png')


# Liest die vier Farbkennzeichen und ihre Position aus der INI.
# Rueckgabe: Tupel (Position, Dictionary mit C/M/Y/K-Kennzeichen).
def get_color_settings(config):
    section = config.settings['filenames']
    position = section.getint('color_position', fallback=-1)
    colors = {
        'C': section.get('cyan', 'C'),
        'M': section.get('magenta', 'M'),
        'Y': section.get('yellow', 'Y'),
        'K': section.get('black', 'K'),
    }
    if any(len(value) != 1 for value in colors.values()):
        raise ValueError('Farbkennzeichen muessen genau ein Zeichen lang sein.')
    if len({value.upper() for value in colors.values()}) != 4:
        raise ValueError('Farbkennzeichen muessen verschieden sein.')
    return position, colors


# Ermittelt den Seitennamen und Farbkanal einer Separation anhand der INI.
# Beispiel bei color_position=-1: Auftrag_C.png -> (Auftrag_, 'C').
# Rueckgabe: Tupel (Seitenschluessel, Farbkanal) oder None bei fremden Dateinamen.
def identify_separation(png_file, position, colors):
    stem = png_file.stem
    index = position if position >= 0 else len(stem) + position
    if index < 0 or index >= len(stem):
        return None
    for channel, letter in colors.items():
        if stem[index].upper() == letter.upper():
            key = stem[:index] + stem[index + 1:]
            if key:
                return key, channel
    return None


# Verschiebt eine erfolgreich verarbeitete TIFF-Datei nach tiffs-done.
# Vorhandene Dateien im Ziel werden NICHT ueberschrieben.
# Rueckgabe: Neuer vollstaendiger Dateipfad (Path).
def move_tiff_to_done(filename, done_dir):
    done_dir.mkdir(parents=True, exist_ok=True)
    target = done_dir / filename.name
    if target.exists():
        raise FileExistsError(f'TIFF liegt bereits in tiffs-done: {target}')
    # Beide Ordner liegen normalerweise auf demselben Dateisystem.
    # shutil.move kann bei verschiedenen Dateisystemen auch kopieren.
    return Path(shutil.move(str(filename), str(target)))


# Wandelt alle stabilen TIFFs um und verschiebt sie bei Erfolg nach tiffs-done.
# Bei Fehlern bleibt das Original in tiffs-in und wird spaeter erneut versucht.
# Rueckgabe: Anzahl der erfolgreich bearbeiteten TIFF-Dateien (int).
def process_tiffs(config, logger, stable_seconds, observations, retry_after):
    successful = 0
    now = time.monotonic()
    for filename in list_tiff_files(config):
        if now < retry_after.get(filename, 0):
            continue
        if not is_file_stable(filename, stable_seconds, observations):
            continue
        try:
            destination = config.paths.tiff_done / filename.name
            if destination.exists():
                # Nicht ueberschreiben: evtl. derselbe Dateiname, aber andere Daten.
                raise FileExistsError(f'Archiv-Datei existiert bereits: {destination}')

            logger.info('TIFF-Verarbeitung startet: %s (%d Bytes)',
                        filename.name, filename.stat().st_size)
            start = time.monotonic()
            png_file = convert_tiff_to_png(filename, config, logger)
            if not png_file or not Path(png_file).is_file():
                raise RuntimeError('TIFF-Prozessor hat kein fertiges PNG geliefert.')

            logger.info('TIFF erfolgreich konvertiert: %s -> %s (%.1f s)',
                        filename.name, Path(png_file).name, time.monotonic() - start)
            moved_to = move_tiff_to_done(filename, config.paths.tiff_done)
            observations.pop(filename, None)
            retry_after.pop(filename, None)
            successful += 1
            logger.info('TIFF archiviert: %s -> %s', filename, moved_to)
        except Exception:
            # Eine defekte Datei soll nicht alle paar Sekunden das Log fluten.
            logger.exception('TIFF konnte nicht verarbeitet/verschoben werden: %s; '
                             'naechster Versuch fruehestens in 60 s', filename)
            retry_after[filename] = time.monotonic() + 60
    return successful


# Gruppiert PNG-Dateien nach Seite und Farbkanal.
# Rueckgabe: Dictionary {Seitenschluessel: {'C': Path, 'M': Path, ...}}.
def group_png_separations(config):
    position, colors = get_color_settings(config)
    grouped = {}
    for filename in list_png_files(config):
        result = identify_separation(filename, position, colors)
        if result is None:
            continue
        page, channel = result
        grouped.setdefault(page, {})[channel] = filename
    return grouped


# Prueft, ob das Composite existiert und neuer als alle vier Separationen ist.
# Rueckgabe: True (aktuell) oder False (fehlt bzw. veraltet).
def composite_is_current(destination, separations):
    if not destination.is_file():
        return False
    try:
        created = destination.stat().st_mtime_ns
        return all(filename.stat().st_mtime_ns <= created
                   for filename in separations.values())
    except FileNotFoundError:
        return False


# Erstellt aus vollstaendigen, stabilen C/M/Y/K-Saetzen die Composite-PNGs.
# Unvollstaendige Saetze bleiben bis zum naechsten Scan liegen.
# Rueckgabe: Anzahl der erfolgreich erstellten Composite-PNGs (int).
def process_composites(config, logger, stable_seconds, observations, retry_after):
    successful = 0
    now = time.monotonic()
    groups = group_png_separations(config)
    for page, files in groups.items():
        if set(files) != {'C', 'M', 'Y', 'K'}:
            logger.debug('PNG-Satz unvollstaendig: %s, vorhanden: %s',
                         page, ','.join(sorted(files)))
            continue
        reference = files['C']
        target = config.paths.composite_png / composite_filename(reference, config)
        if composite_is_current(target, files):
            continue
        if now < retry_after.get(page, 0):
            continue
        stable_results = [is_file_stable(filename, stable_seconds, observations)
                          for filename in files.values()]
        if not all(stable_results):
            continue

        try:
            logger.info('Composite-Erstellung startet: %s (C=%s M=%s Y=%s K=%s)',
                        target.name, *(files[channel].name for channel in 'CMYK'))
            start = time.monotonic()
            result = create_composite(reference, config, logger)
            if not result or not Path(result).is_file():
                raise RuntimeError('Composite-Modul hat keine Ausgabedatei geliefert.')
            logger.info('Composite erfolgreich erstellt: %s (%.1f s)',
                        result, time.monotonic() - start)
            retry_after.pop(page, None)
            successful += 1
        except Exception:
            logger.exception('Composite fehlgeschlagen: %s; '
                             'naechster Versuch fruehestens in 60 s', page)
            retry_after[page] = time.monotonic() + 60
    return successful


# Startet den Watcher, laedt die INI und wiederholt die Ordnerpruefung.
# Aufruf mit --once fuehrt genau einen Scan durch (nuetzlich zum Testen).
# Rueckgabe: Exit-Code 0 bei regulaerem Ende, 1 bei Startfehler.
def main():
    parser = argparse.ArgumentParser(description='Imageserver: TIFF- und PNG-Ordner ueberwachen')
    parser.add_argument('--config', help='Alternativer Pfad zur imageserver.ini')
    parser.add_argument('--once', action='store_true', help='Nur einmal scannen und beenden')
    args = parser.parse_args()

    try:
        config = load_config(args.config) if args.config else load_config()
        log_path = config.log_files['folder_watcher']
        logger = setup_rotating_logger('folder_watcher', log_path.parent, log_path.name)
        logger.setLevel(config.log_level)
        scan_interval = config.settings.getint('watcher', 'scan_interval_seconds', fallback=5)
        stable_seconds = config.settings.getint('watcher', 'file_stable_seconds', fallback=15)
        if scan_interval < 1 or stable_seconds < 0:
            raise ValueError('scan_interval_seconds muss >= 1 und file_stable_seconds >= 0 sein.')
        for directory in (config.paths.tiff_in, config.paths.tiff_done,
                          config.paths.separation_png, config.paths.composite_png):
            directory.mkdir(parents=True, exist_ok=True)
        get_color_settings(config)  # Fehlerhafte Farbkonfiguration sofort erkennen.
    except Exception as error:
        print(f'FEHLER beim Start des Folder Watchers: {error}', file=sys.stderr)
        return 1

    logger.info('Folder Watcher gestartet (PID=%d)', os.getpid())
    logger.info('TIFF-Eingang: %s; TIFF-Archiv: %s', config.paths.tiff_in, config.paths.tiff_done)
    logger.info('PNG-Separationen: %s; Composites: %s',
                config.paths.separation_png, config.paths.composite_png)
    logger.info('Scanintervall: %d s; Mindestalter/Dateistabilitaet: %d s; Log-Level: %s',
                scan_interval, stable_seconds, config.log_level)

    # Merkt sich Dateigroesse und Aenderungszeit aus dem vorherigen Durchlauf.
    # So wird eine Datei nie schon beim ersten Sehen verarbeitet.
    observations = {}
    retry_tiff = {}
    retry_composite = {}
    scans = 0
    try:
        while True:
            scans += 1
            try:
                tiff_count = process_tiffs(config, logger, stable_seconds,
                                           observations, retry_tiff)
                composite_count = process_composites(config, logger, stable_seconds,
                                                     observations, retry_composite)
                logger.debug('Scan %d abgeschlossen: %d TIFFs, %d Composites verarbeitet',
                             scans, tiff_count, composite_count)
                if scans % 60 == 0:
                    logger.info('Watcher aktiv: %d Scans; letzter Scan: %d TIFFs, %d Composites',
                                scans, tiff_count, composite_count)
            except Exception:
                logger.exception('Fehler beim Scannen der Ordner; Watcher laeuft weiter')
                if args.once:
                    return 1
            if args.once:
                break
            time.sleep(scan_interval)
    except KeyboardInterrupt:
        logger.info('Folder Watcher durch Benutzer beendet (Strg+C)')

    logger.info('Folder Watcher beendet; insgesamt %d Scans', scans)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

