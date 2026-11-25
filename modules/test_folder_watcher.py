#!/usr/bin/env python3
# =============================================================================
# Diagnose und Funktionstest des TIFF Folder Watchers.
# Standard und --all sind rein lesend. --self-test arbeitet ausschliesslich
# in einem temporaeren Verzeichnis. --once verarbeitet echte Eingangsdateien.
# =============================================================================
import argparse
from dataclasses import replace
import logging
from pathlib import Path
import tempfile
from threading import Event
from unittest.mock import patch
import time
from PIL import Image

if __package__:
    from . import folder_watcher as watcher
    from .config_loader import load_config
    from .folder_watcher import list_tiff_files, is_file_stable, process_tiffs, run_watcher
else:
    import folder_watcher as watcher
    from config_loader import load_config
    from folder_watcher import list_tiff_files, is_file_stable, process_tiffs, run_watcher


# -----------------------------------------------------------------------------
# FUNKTION: diagnose
# Aufgabe: Eingangsdateien und erwartete Zielpfade anzeigen; optional zwei
#          Beobachtungen mit Stabilitaetswartezeit durchfuehren.
# Uebergabeparameter: config - gepruefte Konfiguration; all_files - Detailausgabe.
# Rueckgabewert: 0 bei erfolgreicher Diagnose; keine Dateien werden veraendert.
# Fehler: OSError bei nicht lesbaren Verzeichnissen.
# -----------------------------------------------------------------------------
def diagnose(config, all_files):
    paths = config.paths
    settings = config.settings['watcher']
    for name in ('tiff_in', 'separation_png', 'tiff_done', 'tiff_failed'):
        print(f'{name:18s}: {getattr(paths, name)}')
    print(f"Polling: {settings['scan_interval_seconds']} s; stability: {settings['file_stable_seconds']} s")
    files = list_tiff_files(config)
    print(f'TIFF files found: {len(files)}')
    if all_files and files:
        observations = {}
        for path in files:
            is_file_stable(path, settings['file_stable_seconds'], observations)
        print('Observing files for stability...', flush=True)
        time.sleep(max(settings['file_stable_seconds'], settings['scan_interval_seconds']))
        # Auch neu eingetroffene Dateien anzeigen; sie sind noch nicht stabil.
        for path in list_tiff_files(config):
            stable = is_file_stable(path, settings['file_stable_seconds'], observations)
            png = paths.separation_png / (path.stem + '.png')
            print(f'\n{"STABLE" if stable else "WAITING"}: {path}')
            print(f'  PNG target: {png} ({"exists" if png.is_file() else "missing"})')
            print(f'  Archive target: {paths.tiff_done / path.name}')
    return 0


# -----------------------------------------------------------------------------
# FUNKTION: self_test
# Aufgabe: Stabilitaet, echte TIFF-Konvertierung, Archivierung und Quarantaene
#          ohne Zugriff auf produktive Bildverzeichnisse pruefen.
# Uebergabeparameter: config - gepruefte Konfiguration als Vorlage.
# Rueckgabewert: 0 bei Erfolg; AssertionError bei fehlerhaftem Ergebnis.
# -----------------------------------------------------------------------------
def self_test(config):
    with tempfile.TemporaryDirectory(prefix='imgsrv-watcher-') as directory:
        root = Path(directory)
        paths = replace(config.paths, root_dir=root, tiff_in=root/'in',
                        tiff_done=root/'done', tiff_failed=root/'failed',
                        separation_png=root/'png', logs=root/'logs')
        for path in (paths.tiff_in, paths.tiff_done, paths.tiff_failed, paths.separation_png, paths.logs):
            path.mkdir()
        # Testwerte nur in einer Kopie aendern, nicht in der Originalkonfiguration.
        settings = dict(config.settings)
        settings['watcher'] = dict(settings['watcher'], file_stable_seconds=0, max_retries=0, retry_interval_seconds=0)
        test_config = replace(config, paths=paths, settings=settings)
        dpi = settings['processing']['dpi_configs'][0]['dpi']
        src = paths.tiff_in/'158574.tiff'
        with Image.new('1', (64, 64), 0) as image:
            image.save(src, dpi=(dpi, dpi))
        bad = paths.tiff_in/'invalid.tif'
        bad.write_bytes(b'not a TIFF')
        logger = logging.getLogger('watcher-self-test')
        logger.addHandler(logging.NullHandler())
        observations, retries = {}, {}
        first = process_tiffs(test_config, logger, logger, observations, retries)
        assert first['waiting'] == 2, first
        second = process_tiffs(test_config, logger, logger, observations, retries)
        assert second['converted'] == 1 and second['quarantined'] == 1, second
        assert not src.exists() and (paths.tiff_done/src.name).is_file()
        assert not bad.exists() and (paths.tiff_failed/bad.name).is_file()
        with Image.open(paths.separation_png/'158574.png') as png:
            assert png.mode == 'L'
            png.load()
        print('PASS: stability, TIFF conversion, PNG readback, archive, quarantine')
        # Bereits archivierten Namen erneut senden und Archivierungsfehler
        # simulieren. Das vorhandene Archiv darf nicht ueberschrieben werden.
        old_archive = paths.tiff_done/src.name
        old_content = old_archive.read_bytes()
        with Image.new('1', (64, 64), 1) as image:
            image.save(src, dpi=(dpi, dpi))
        observations, retries, completed = {}, {}, {}
        process_tiffs(test_config, logger, logger, observations, retries, completed=completed)
        with patch.object(watcher, 'archive_tiff', side_effect=PermissionError('simulated')):
            result = process_tiffs(test_config, logger, logger, observations, retries, completed=completed)
            assert result['archive_failed'] == 1 and src.exists() and completed, result
        # Zweite Konvertierung waere ein Testfehler; nur Archivierung erlaubt.
        with patch.object(watcher, 'convert_tiff_to_png', side_effect=AssertionError('unexpected conversion')):
            result = process_tiffs(test_config, logger, logger, observations, retries, completed=completed)
            assert result['converted'] == 1 and not src.exists() and not completed, result
        assert old_archive.read_bytes() == old_content
        assert len(list(paths.tiff_done.iterdir())) == 2
        print('PASS: archive retry without conversion, archive collision protection')
    return 0


# -----------------------------------------------------------------------------
# FUNKTION: main
# Aufgabe: CLI-Parameter verarbeiten und gewaehlten Test ausfuehren.
# Uebergabeparameter: Keine; Argumente aus der Kommandozeile.
# Rueckgabewert: 0 bei Erfolg, 1 bei Fehler.
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='ImageServer TIFF watcher diagnostics')
    parser.add_argument('--config', help='Alternative imageserver.yaml')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--all', action='store_true', help='Show all TIFFs and stability')
    mode.add_argument('--self-test', action='store_true', help='Test using temporary files only')
    mode.add_argument('--once', action='store_true', help='Actually convert and archive input TIFFs')
    args = parser.parse_args()
    try:
        config = load_config(args.config) if args.config else load_config()
        if args.self_test:
            return self_test(config)
        if args.once:
            return run_watcher(config, Event(), once=True)
        return diagnose(config, args.all)
    except Exception as error:
        print(f'ERROR: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

