#!/usr/bin/env python3
# =============================================================================
# FOLDER WATCHER - Imageserver V2
# =============================================================================
# Ueberwacht ausschliesslich den TIFF-Eingang. Jede stabile Datei wird sofort
# entrastert, unabhaengig von Produktinformationen und anderen Separationen.
# Erst nach erfolgreicher PNG-Ausgabe wird das Original archiviert.
# Keine Composite-Erstellung, keine Retention und keine SQL-Datenbank.
# Eine Instanz pro Imageserver; spaeter vom Supervisor aufrufbar.
# =============================================================================
import argparse
import ctypes
import errno
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sys
import time
import uuid
from threading import Event
from PIL import UnidentifiedImageError

if __package__:
    from .config_loader import load_config
    from .tiff_processor import convert_tiff_to_png
else:
    from config_loader import load_config
    from tiff_processor import convert_tiff_to_png


# -----------------------------------------------------------------------------
# FUNKTION: file_signature
# Aufgabe: Identitaet und Aenderungsstand einer Datei erfassen.
# Uebergabeparameter: filename - Pfad der Datei.
# Rueckgabewert: Tupel aus Geraet, Inode, Groesse, mtime und ctime.
# Fehler: OSError, wenn die Datei nicht mehr erreichbar ist.
# -----------------------------------------------------------------------------
def file_signature(filename):
    s = filename.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


# -----------------------------------------------------------------------------
# FUNKTION: list_tiff_files
# Aufgabe: Nur regulaere TIFF-Dateien im Eingangsordner auflisten.
# Uebergabeparameter: config - gepruefte ImageServerConfig.
# Rueckgabewert: Sortierte Liste von Path-Objekten.
# Fehler: OSError bei nicht lesbarem Eingangsordner.
# -----------------------------------------------------------------------------
def list_tiff_files(config):
    allowed = {x.lower() for x in config.settings['processing']['extensions']}
    return sorted(p for p in config.paths.tiff_in.iterdir()
                  if not p.is_symlink() and p.is_file() and p.suffix.lower() in allowed)


# -----------------------------------------------------------------------------
# FUNKTION: is_file_stable
# Aufgabe: Beobachten, wie lange die Datei tatsaechlich unveraendert bleibt.
# Uebergabeparameter: filename - TIFF-Pfad; stable_seconds - Mindestdauer;
#   observations - Dictionary mit Signatur und monotonem Beobachtungszeitpunkt.
# Rueckgabewert: True bei Stabilitaet, sonst False.
# Fehler: OSError bei Zugriffsfehlern, ausser verschwundenen Dateien.
# -----------------------------------------------------------------------------
def is_file_stable(filename, stable_seconds, observations):
    try:
        signature = file_signature(filename)
    except FileNotFoundError:
        observations.pop(filename, None)
        return False
    now = time.monotonic()
    previous = observations.get(filename)
    # Nicht auf mtime allein verlassen: der Sender kann alte Zeitstempel setzen.
    # Auch bei 0 Sekunden werden mindestens zwei Beobachtungen verlangt.
    if previous is None or previous[0] != signature:
        observations[filename] = (signature, now)
        return False
    return now - previous[1] >= stable_seconds


# -----------------------------------------------------------------------------
# FUNKTION: archive_tiff
# Aufgabe: Original verlustfrei archivieren, ohne vorhandene TIFFs zu ersetzen.
# Uebergabeparameter: filename - Quellpfad; directory - Zielordner;
#   expected - zuletzt gepruefte Dateisignatur.
# Rueckgabewert: Pfad der archivierten Datei.
# Fehler: OSError oder RuntimeError; Original bleibt bei Fehler erhalten.
# -----------------------------------------------------------------------------
def archive_tiff(filename, directory, expected):
    if file_signature(filename) != expected:
        raise RuntimeError(f'TIFF changed before archiving: {filename}')
    target = directory / filename.name
    # Linux renameat2 mit RENAME_NOREPLACE: atomar verschieben, niemals
    # vorhandene Archivdateien ueberschreiben. Keine Hardlink-Rechte noetig.
    # Bei fehlender Kernel-/Dateisystemunterstuetzung sicher abbrechen.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise OSError(errno.ENOSYS, 'renameat2 unavailable')
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    while True:
        if file_signature(filename) != expected:
            raise RuntimeError(f'TIFF changed before archiving: {filename}')
        if rename(-100, os.fsencode(filename), -100, os.fsencode(target), 1) == 0:
            return target
        code = ctypes.get_errno()
        if code != errno.EEXIST:
            raise OSError(code, os.strerror(code), str(filename))
        target = directory / f'{filename.stem}.{uuid.uuid4().hex}{filename.suffix}'



# -----------------------------------------------------------------------------
# FUNKTION: setup_logger
# Aufgabe: Modul-Logging gemaess YAML konfigurieren.
# Uebergabeparameter: config - Konfiguration; name - Modulname.
# Rueckgabewert: logging.Logger mit rotierender Datei und optionaler Konsole.
# Fehler: OSError bei nicht beschreibbarer Logdatei.
# -----------------------------------------------------------------------------
def setup_logger(config, name):
    settings = config.settings['logging']
    path = config.log_files[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(config.log_level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter('%(asctime)s - %(levelname)-8s - %(name)s: %(message)s')
    handler = RotatingFileHandler(path, maxBytes=settings['max_size_mb'] * 1024**2,
                                  backupCount=settings['backup_count'], encoding='utf-8')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    if settings['console_output']:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


# -----------------------------------------------------------------------------
# FUNKTION: process_tiffs
# Aufgabe: Einen Scan ausfuehren, stabile TIFFs umrechnen und archivieren.
# Uebergabeparameter: config - YAML-Konfiguration; logger - Watcher-Logger;
#   tiff_logger - Konverter-Logger; observations - Stabilitaetszustand;
#   retries - Fehlerzustand pro Datei; stop - optionales Stop-Event;
#   completed - Cache erfolgreicher Umrechnungen (ueber Scans beibehalten).
# Rueckgabewert: Dictionary mit Scanstatistik.
# Fehler: Scanfehler werden an Aufrufer weitergegeben; Dateifehler behandelt.
# -----------------------------------------------------------------------------
def process_tiffs(config, logger, tiff_logger, observations, retries, stop=None, completed=None):
    if completed is None:
        completed = {}
    settings = config.settings['watcher']
    files = list_tiff_files(config)
    stats = dict(found=len(files), converted=0, waiting=0, deferred=0, failed=0, quarantined=0, archive_failed=0)
    # Entfernte Dateien nicht dauerhaft im Arbeitsspeicher behalten.
    for mapping in (observations, retries, completed):
        for path in set(mapping) - set(files):
            mapping.pop(path, None)
    for filename in files:
        count = 0
        if stop is not None and stop.is_set():
            break
        try:
            if not is_file_stable(filename, settings['file_stable_seconds'], observations):
                stats['waiting'] += 1
                continue
            signature = observations[filename][0]
            if file_signature(filename) != signature:
                observations.pop(filename, None)
                stats['waiting'] += 1
                continue
            # Ein neu geschriebener Inhalt erhaelt einen neuen Fehlerzaehler.
            old_signature, count, deadline = retries.get(filename, (signature, 0, 0))
            if old_signature != signature:
                count, deadline = 0, 0
            if time.monotonic() < deadline:
                stats['deferred'] += 1
                continue
            # PNG nur wiederverwenden, wenn sowohl Quelle als auch Ausgabe
            # unveraendert sind. Ein fremdes oder altes PNG reicht nicht aus.
            cached = completed.get(filename)
            reuse = False
            if cached and cached[0] == signature:
                try:
                    reuse = file_signature(cached[1]) == cached[2]
                except FileNotFoundError:
                    pass
            if reuse:
                output = cached[1]
                logger.info('Retrying archive only: %s', filename)
            else:
                completed.pop(filename, None)
                logger.info('Processing TIFF: %s', filename)
                try:
                    output = Path(convert_tiff_to_png(filename, config, tiff_logger))
                except (ValueError, UnidentifiedImageError) as error:
                    count += 1
                    if count > settings['max_retries']:
                        try:
                            target = archive_tiff(filename, config.paths.tiff_failed, signature)
                        except Exception:
                            stats['archive_failed'] += 1
                            retries[filename] = (signature, count, time.monotonic() + settings['retry_interval_seconds'])
                            logger.exception('Quarantine archive failed: %s', filename)
                            continue
                        logger.error('TIFF quarantined: %s -> %s: %s', filename, target, error)
                        stats['quarantined'] += 1
                        observations.pop(filename, None)
                        retries.pop(filename, None)
                        continue
                    raise
                if not output.is_file():
                    raise RuntimeError(f'PNG missing after conversion: {output}')
                if file_signature(filename) != signature:
                    raise RuntimeError(f'TIFF changed after conversion: {filename}')
                completed[filename] = (signature, output, file_signature(output))
                count = 0
            # Archivierungsfehler sind Infrastrukturfehler, keine TIFF-Fehler.
            try:
                target = archive_tiff(filename, config.paths.tiff_done, signature)
            except Exception:
                stats['archive_failed'] += 1
                retries[filename] = (signature, 0, time.monotonic() + settings['retry_interval_seconds'])
                logger.exception('PNG ready; TIFF archive failed: %s', filename)
                continue
            logger.info('TIFF completed: %s -> %s; archived: %s', filename, output, target)
            completed.pop(filename, None)
            observations.pop(filename, None)
            retries.pop(filename, None)
            stats['converted'] += 1
        except Exception:
            # Bei Infrastrukturfehlern Eingangsdatei behalten und spaeter erneut
            # versuchen. Nach einem Absturz kann ein PNG erneut berechnet werden.
            stats['failed'] += 1
            if filename in observations:
                retries[filename] = (observations[filename][0], count,
                                     time.monotonic() + settings['retry_interval_seconds'])
            logger.exception('TIFF processing failed: %s', filename)
    return stats


# -----------------------------------------------------------------------------
# FUNKTION: run_watcher
# Aufgabe: Verzeichnisse vorbereiten, Instanz sperren und regelmaessig scannen.
# Uebergabeparameter: config - YAML-Konfiguration; stop - threading.Event;
#   once - True fuer zwei Beobachtungen und einen begrenzten Testdurchlauf.
# Rueckgabewert: Exit-Code 0 bei regulaerem Ende, 1 bei Scanfehler im Einmaltest.
# Fehler: OSError/RuntimeError bei Startproblemen oder belegter Instanzsperre.
# -----------------------------------------------------------------------------
def run_watcher(config, stop, once=False):
    if not config.settings['supervisor']['tiff_processing_enabled']:
        print('TIFF processing disabled in configuration')
        return 0
    paths = config.paths
    for directory in (paths.tiff_in, paths.tiff_done, paths.tiff_failed, paths.separation_png, paths.logs):
        directory.mkdir(parents=True, exist_ok=True)
    if any(p.stat().st_dev != paths.tiff_in.stat().st_dev for p in (paths.tiff_done, paths.tiff_failed)):
        raise RuntimeError('TIFF input and archives must be on the same filesystem')
    settings = config.settings['watcher']
    # Sperre wird beim Prozessende automatisch freigegeben, auch nach Absturz.
    with (paths.logs / 'folder_watcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another folder watcher is already running') from error
        logger = setup_logger(config, 'folder_watcher')
        tiff_logger = setup_logger(config, 'tiff_processor')
        logger.info('Watcher started: input=%s, PNG=%s, done=%s, failed=%s', paths.tiff_in, paths.separation_png, paths.tiff_done, paths.tiff_failed)
        logger.info('Polling=%s s, stability=%s s, retry=%s s, max retries=%s', settings['scan_interval_seconds'], settings['file_stable_seconds'], settings['retry_interval_seconds'], settings['max_retries'])
        observations, retries, completed = {}, {}, {}
        if once:
            # Ein einzelner Scan ohne vorherige Beobachtung kann keine Datei
            # freigeben. Deshalb im Test zuerst Signaturen erfassen und warten.
            for path in list_tiff_files(config):
                is_file_stable(path, settings['file_stable_seconds'], observations)
            stop.wait(max(settings['file_stable_seconds'], settings['scan_interval_seconds']))
        heartbeat = 0
        while not stop.is_set():
            try:
                stats = process_tiffs(config, logger, tiff_logger, observations, retries, stop, completed)
                logger.debug('Scan: %s', stats)
                now = time.monotonic()
                if now >= heartbeat:
                    logger.info('Watcher active: %s', stats)
                    heartbeat = now + config.settings['supervisor']['heartbeat_interval_seconds']
                if once:
                    return int(bool(stats['failed'] or stats['quarantined'] or stats['archive_failed']))
            except Exception:
                logger.exception('Directory scan failed')
                if once:
                    return 1
            stop.wait(settings['scan_interval_seconds'])
        logger.info('Watcher stopped')
    return 0


# -----------------------------------------------------------------------------
# FUNKTION: main
# Aufgabe: Kommandozeile und kontrolliertes Beenden per SIGINT/SIGTERM.
# Uebergabeparameter: Keine; Argumente aus sys.argv.
# Rueckgabewert: Exit-Code 0 bei Erfolg, 1 bei Fehler.
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Watch TIFF input and convert to PNG')
    parser.add_argument('--config', help='Alternative imageserver.yaml')
    parser.add_argument('--once', action='store_true', help='Observe, wait, then process one scan')
    args = parser.parse_args()
    stop = Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        config = load_config(args.config) if args.config else load_config()
        return run_watcher(config, stop, args.once)
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

