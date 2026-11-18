#!/usr/bin/env python3
"""Laedt die zentrale Konfiguration des Imageservers."""

import argparse
import configparser
import re
from dataclasses import dataclass
from pathlib import Path

try:
    from modules.library.logger import setup_rotating_logger
except ModuleNotFoundError as error:
    # Beim direkten Aufruf aus dem Ordner modules ist 'modules' nicht im Suchpfad.
    if error.name != 'modules':
        raise
    from library.logger import setup_rotating_logger

DEFAULT_CONFIG = Path('/pecom/imgsrv/config/imageserver.ini')


class ConfigError(ValueError):
    """Fehler in der Imageserver-Konfiguration."""


@dataclass
class Paths:
    root_dir: Path
    tiff_in: Path
    tiff_done: Path
    separation_png: Path
    composite_png: Path
    logs: Path

    # Alte Attributnamen bleiben vorerst verwendbar.
    @property
    def base(self):
        return self.root_dir

    @property
    def tiff_input(self):
        return self.tiff_in

    @property
    def png_separations(self):
        return self.separation_png

    @property
    def png_composite(self):
        return self.composite_png


@dataclass
class Retention:
    tiff_in: int
    tiff_done: int
    separation_png: int
    composite_png: int
    cleanup_interval: int

    # Alter Name aus der ersten Version, entspricht jetzt tiff_done.
    @property
    def tiff(self):
        return self.tiff_done

    @property
    def png_separations(self):
        return self.separation_png

    @property
    def png_composite(self):
        return self.composite_png


@dataclass
class ImageServerConfig:
    paths: Paths
    retention: Retention
    log_files: dict
    config_file: Path
    settings: configparser.ConfigParser
    log_level: str


# Wandelt eine Zeitangabe (z.B. 2d oder 48h) in Sekunden um.
# Rueckgabe: Anzahl Sekunden als Integer; 0 bedeutet deaktiviert.
def parse_duration(value):
    value = value.strip().lower()
    if value == '0':
        return 0
    match = re.fullmatch(r'(\d+)\s*([smhd])', value)
    if match is None:
        raise ConfigError(f'Ungueltige Zeitangabe: {value!r} (z.B. 15m, 1h, 2d oder 0)')
    amount = int(match.group(1))
    unit = match.group(2)
    seconds_per_unit = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return amount * seconds_per_unit[unit]


# Liest einen benoetigten Eintrag aus der Konfiguration.
# Rueckgabe: Text des Eintrags; bei Fehler wird ConfigError ausgeloest.
def required(settings, section, name):
    if not settings.has_section(section):
        raise ConfigError(f'Fehlender Abschnitt [{section}].')
    if not settings.has_option(section, name):
        raise ConfigError(f'Fehlender Parameter [{section}] {name}.')
    value = settings.get(section, name).strip()
    if not value:
        raise ConfigError(f'Leerer Parameter [{section}] {name}.')
    return value


# Ergaenzt relative Pfade um das Basisverzeichnis aus [server] root_dir.
# Rueckgabe: Absoluter Dateipfad oder Verzeichnispfad (Path).
def resolve_path(root_dir, value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root_dir / path
    return path.resolve()


# Liest und prueft die imageserver.ini und richtet den Logger ein.
# Rueckgabe: ImageServerConfig mit Pfaden, Aufbewahrungszeiten und allen INI-Werten.
def load_config(config_file=DEFAULT_CONFIG):
    config_file = Path(config_file).expanduser().resolve()
    if not config_file.is_file():
        raise ConfigError(f'Konfigurationsdatei nicht gefunden: {config_file}')

    settings = configparser.ConfigParser(interpolation=None)
    try:
        with config_file.open('r', encoding='utf-8') as file:
            settings.read_file(file)
    except (OSError, UnicodeError, configparser.Error) as error:
        raise ConfigError(f'Konfiguration konnte nicht gelesen werden: {error}') from error

    root_dir = resolve_path(Path('/'), required(settings, 'server', 'root_dir'))
    log_level = settings.get('server', 'log_level', fallback='INFO').strip().upper()
    if log_level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        raise ConfigError(f'Ungueltiger log_level: {log_level}')

    paths = Paths(
        root_dir=root_dir,
        tiff_in=resolve_path(root_dir, required(settings, 'paths', 'tiff_in')),
        tiff_done=resolve_path(root_dir, required(settings, 'paths', 'tiff_done')),
        separation_png=resolve_path(root_dir, required(settings, 'paths', 'separation_png')),
        composite_png=resolve_path(root_dir, required(settings, 'paths', 'composite_png')),
        logs=resolve_path(root_dir, required(settings, 'paths', 'logs')),
    )
    directory_list = [paths.tiff_in, paths.tiff_done, paths.separation_png, paths.composite_png, paths.logs]
    if len(set(directory_list)) != len(directory_list):
        raise ConfigError('Die Eingangs-, Ausgabe- und Log-Verzeichnisse muessen verschieden sein.')

    retention = Retention(
        tiff_in=parse_duration(required(settings, 'retention', 'tiff_in')),
        tiff_done=parse_duration(required(settings, 'retention', 'tiff_done')),
        separation_png=parse_duration(required(settings, 'retention', 'separation_png')),
        composite_png=parse_duration(required(settings, 'retention', 'composite_png')),
        cleanup_interval=parse_duration(required(settings, 'retention', 'cleanup_interval')),
    )
    if retention.cleanup_interval == 0:
        raise ConfigError('cleanup_interval darf nicht 0 sein.')

    if not settings.has_section('LOGGING'):
        raise ConfigError('Fehlender Abschnitt [LOGGING].')
    log_files = {}
    for name, value in settings.items('LOGGING'):
        if not value.strip():
            raise ConfigError(f'Logdateipfad fuer {name} ist leer.')
        log_path = resolve_path(root_dir, value)
        if log_path.suffix.lower() != '.log':
            raise ConfigError(f'Logdatei fuer {name} muss auf .log enden: {log_path}')
        log_files[name] = log_path
    if 'config_loader' not in log_files:
        raise ConfigError('Fehlender Parameter [LOGGING] config_loader.')
    if len(set(log_files.values())) != len(log_files):
        raise ConfigError('Mehrere Module verwenden dieselbe Logdatei.')

    log_path = log_files['config_loader']
    try:
        logger = setup_rotating_logger('config_loader', log_path.parent, log_path.name)
    except OSError as error:
        raise ConfigError(f'Logger konnte nicht erstellt werden: {error}') from error
    logger.setLevel(log_level)
    config = ImageServerConfig(paths, retention, log_files, config_file, settings, log_level)
    logger.info('Konfiguration geladen: %s', config_file)
    logger.info('Pfade: %s', config.paths)
    logger.info('Aufbewahrung: %s', config.retention)
    return config


# Testet die Konfiguration und zeigt die wichtigsten Werte auf der Konsole an.
# Rueckgabe: 0 bei Erfolg, 1 bei Fehler.
def main():
    parser = argparse.ArgumentParser(description='Imageserver-Konfiguration pruefen')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f'FEHLER: {error}')
        return 1

    print(f'Konfiguration OK: {config.config_file}')
    print('\nVerzeichnisse:')
    for name in ('root_dir', 'tiff_in', 'tiff_done', 'separation_png', 'composite_png', 'logs'):
        print(f'  {name:18s} {getattr(config.paths, name)}')
    print('\nAufbewahrung (Sekunden):')
    for name in ('tiff_in', 'tiff_done', 'separation_png', 'composite_png', 'cleanup_interval'):
        print(f'  {name:18s} {getattr(config.retention, name)}')
    print('\nLogdateien:')
    for name, path in config.log_files.items():
        print(f'  {name:18s} {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

