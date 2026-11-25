#!/usr/bin/env python3
# =============================================================================
# ImageServer V2 - Zentrale YAML-Konfiguration
# =============================================================================
# Dieses Modul liest ausschliesslich die imageserver.yaml. Die Konfiguration
# des Product Interface bleibt unabhaengig. Alle Pfade werden aufgeloest,
# Zeitangaben in Sekunden umgerechnet und Einstellungen vor dem Start geprueft.
# Es werden keine Produktionsverzeichnisse angelegt und keine Bilder verarbeitet.
# =============================================================================
import argparse
import math
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path('/pecom/imgsrv/config/imageserver.yaml')


class ConfigError(ValueError):
    """Ungueltige oder nicht lesbare Imageserver-Konfiguration."""


class UniqueLoader(yaml.SafeLoader):
    """Sicherer YAML-Loader, der doppelte Schluessel nicht stillschweigend akzeptiert."""


# -----------------------------------------------------------------------------
# FUNKTION: unique_mapping
# Aufgabe: YAML-Mappings laden und doppelte Schluessel ablehnen. Dadurch kann
#          eine versehentlich doppelt definierte Einstellung nichts ueberschreiben.
# Uebergabeparameter: loader - YAML-Loader; node - YAML-Mapping-Knoten.
# Rueckgabewert: Dictionary mit den eingelesenen Schluessel-Wert-Paaren.
# Fehler: ConfigError bei doppelten oder nicht textuellen Schluesseln.
# -----------------------------------------------------------------------------
def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise ConfigError(f'Invalid or duplicate YAML key: {key!r}, line {key_node.start_mark.line + 1}')
        result[key] = loader.construct_object(value_node, deep=True)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


@dataclass(frozen=True)
class Paths:
    root_dir: Path
    tiff_in: Path
    tiff_done: Path
    tiff_failed: Path
    separation_png: Path
    composite_png: Path
    logs: Path

    # Aufgabe: Alten Namen fuer das Hauptverzeichnis bereitstellen.
    # Uebergabeparameter: self - aktuelle Paths-Instanz.
    # Rueckgabewert: Absoluter Pfad (Path).
    @property
    def base(self):
        return self.root_dir

    # Aufgabe: Alten Namen fuer den TIFF-Eingang bereitstellen.
    # Uebergabeparameter: self - aktuelle Paths-Instanz.
    # Rueckgabewert: Absoluter Pfad (Path).
    @property
    def tiff_input(self):
        return self.tiff_in

    # Aufgabe: Alten Namen fuer das Separationsverzeichnis bereitstellen.
    # Uebergabeparameter: self - aktuelle Paths-Instanz.
    # Rueckgabewert: Absoluter Pfad (Path).
    @property
    def png_separations(self):
        return self.separation_png

    # Aufgabe: Alten Namen fuer das Composite-Verzeichnis bereitstellen.
    # Uebergabeparameter: self - aktuelle Paths-Instanz.
    # Rueckgabewert: Absoluter Pfad (Path).
    @property
    def png_composite(self):
        return self.composite_png


@dataclass(frozen=True)
class Retention:
    tiff_in: int
    tiff_done: int
    tiff_failed: int
    separation_png: int
    composite_png: int
    jobs_pending: int
    jobs_processing: int
    jobs_done: int
    jobs_failed: int
    cleanup_interval: int

    # Aufgabe: Alten Retention-Namen fuer archivierte TIFFs bereitstellen.
    # Uebergabeparameter: self - aktuelle Retention-Instanz.
    # Rueckgabewert: Aufbewahrungsdauer in Sekunden (int).
    @property
    def tiff(self):
        return self.tiff_done

    # Aufgabe: Alten Retention-Namen fuer Separations-PNGs bereitstellen.
    # Uebergabeparameter: self - aktuelle Retention-Instanz.
    # Rueckgabewert: Aufbewahrungsdauer in Sekunden (int).
    @property
    def png_separations(self):
        return self.separation_png

    # Aufgabe: Alten Retention-Namen fuer Composite-PNGs bereitstellen.
    # Uebergabeparameter: self - aktuelle Retention-Instanz.
    # Rueckgabewert: Aufbewahrungsdauer in Sekunden (int).
    @property
    def png_composite(self):
        return self.composite_png


@dataclass(frozen=True)
class Jobs:
    pending: Path
    processing: Path
    done: Path
    failed: Path
    test: Path


@dataclass(frozen=True)
class ImageServerConfig:
    paths: Paths
    retention: Retention
    jobs: Jobs
    log_files: dict
    config_file: Path
    settings: dict
    log_level: str
    icc_profile: Path


# -----------------------------------------------------------------------------
# FUNKTION: parse_duration
# Aufgabe: Aufbewahrungszeiten in Sekunden umrechnen; 0 deaktiviert die Loeschung.
# Uebergabeparameter: value - YAML-Zeitangabe, z.B. '2d', '48h' oder 0.
# Rueckgabewert: Nichtnegative Anzahl Sekunden (int).
# Fehler: ConfigError bei ungueltiger Zeitangabe.
# -----------------------------------------------------------------------------
def parse_duration(value):
    if type(value) is int and value == 0:
        return 0
    if isinstance(value, str):
        value = value.strip().lower()
        if value == '0':
            return 0
        match = re.fullmatch(r'(\d+)\s*([smhd])', value)
        if match:
            return int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
    raise ConfigError(f'Invalid duration: {value!r} (expected e.g. 2d, 48h or 0)')


# -----------------------------------------------------------------------------
# FUNKTION: required
# Aufgabe: Pflichtparameter lesen und Datentyp pruefen. YAML-Booleans duerfen
#          nicht versehentlich als Integer akzeptiert werden.
# Uebergabeparameter: mapping - Dictionary; key - Parametername;
#                    expected - erlaubter Typ oder Tupel von Typen;
#                    context - Abschnittsname fuer Fehlermeldungen.
# Rueckgabewert: Gepruefter Parameterwert im urspruenglichen Datentyp.
# Fehler: ConfigError bei fehlendem, leerem oder typfalschem Wert.
# -----------------------------------------------------------------------------
def required(mapping, key, expected, context):
    if not isinstance(mapping, dict) or key not in mapping:
        raise ConfigError(f'Missing parameter: {context}.{key}')
    value = mapping[key]
    types = expected if isinstance(expected, tuple) else (expected,)
    if type(value) not in types or (isinstance(value, str) and not value.strip()):
        raise ConfigError(f'Invalid type or empty value: {context}.{key}: {value!r}')
    return value


# -----------------------------------------------------------------------------
# FUNKTION: resolve_path
# Aufgabe: Relative Pfade auf das konfigurierte Basisverzeichnis beziehen.
# Uebergabeparameter: root_dir - Basisverzeichnis (Path); value - Pfadtext.
# Rueckgabewert: Absoluter, normalisierter Pfad (Path).
# Fehler: ConfigError bei ungueltigem Pfad oder nicht aufloesbaren Symlinks.
# -----------------------------------------------------------------------------
def resolve_path(root_dir, value):
    try:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else root_dir / path).resolve()
    except (OSError, ValueError, RuntimeError) as error:
        raise ConfigError(f'Invalid path: {value!r}: {error}') from error


# -----------------------------------------------------------------------------
# FUNKTION: check_int
# Aufgabe: Ganzzahligen Parameter gegen eine Untergrenze pruefen.
# Uebergabeparameter: mapping - Abschnitt; key - Name; minimum - Mindestwert;
#                    context - Abschnittsname.
# Rueckgabewert: Gepruefter Integer.
# Fehler: ConfigError bei falschem Typ oder Unterschreitung der Untergrenze.
# -----------------------------------------------------------------------------
def check_int(mapping, key, minimum, context):
    value = required(mapping, key, int, context)
    if value < minimum:
        raise ConfigError(f'{context}.{key} must be >= {minimum}')
    return value


# -----------------------------------------------------------------------------
# FUNKTION: validate_processing
# Aufgabe: TIFF-Endungen und alle DPI-Parametersaetze vorab pruefen. Die Grenzen
#          entsprechen dem vorhandenen Join-Algorithmus des TIFF Processors.
# Uebergabeparameter: processing - YAML-Abschnitt processing (dict).
# Rueckgabewert: None; bei Erfolg sind alle Einstellungen plausibel.
# Fehler: ConfigError bei ungueltigen oder mehrdeutigen Parametersaetzen.
# -----------------------------------------------------------------------------
def validate_processing(processing):
    extensions = required(processing, 'extensions', list, 'processing')
    if not extensions or any(not isinstance(x, str) or x.lower() not in ('.tif', '.tiff') for x in extensions):
        raise ConfigError('processing.extensions must contain .tif and/or .tiff')
    if len(set(x.lower() for x in extensions)) != len(extensions):
        raise ConfigError('Duplicate TIFF extensions')
    tolerance = required(processing, 'dpi_tolerance', (int, float), 'processing')
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ConfigError('Invalid DPI tolerance')
    configs = required(processing, 'dpi_configs', list, 'processing')
    if not configs:
        raise ConfigError('No DPI configurations')
    seen = set()
    for index, item in enumerate(configs, 1):
        context = f'processing.dpi_configs[{index}]'
        dpi = required(item, 'dpi', (int, float), context)
        if not math.isfinite(dpi) or dpi <= 0 or dpi in seen:
            raise ConfigError(f'{context}: invalid or duplicate DPI')
        seen.add(dpi)
        factor = check_int(item, 'factor', 8, context)
        algorithm = check_int(item, 'algorithm', 0, context)
        join = check_int(item, 'join', 0, context)
        if factor % 8 or algorithm not in (0, 2) or join > min(8, factor // 2):
            raise ConfigError(f'{context}: invalid factor, algorithm or join')
        # Reservierte Altserver-Parameter bleiben im Schema erhalten.
        for key in ('b', 'c', 'd'):
            required(item, key, int, context)


# -----------------------------------------------------------------------------
# FUNKTION: validate_icc_profile
# Aufgabe: Den konfigurierten Pfad zum einzigen verwendeten ICC-Profil
#          aufloesen und bei aktivierter Farbprofilverarbeitung pruefen.
# Uebergabeparameter:
#   composite: YAML-Abschnitt composite (dict).
#   root_dir: Hauptverzeichnis des Imageservers (Path).
# Rueckgabewert: Absoluter Pfad zur ICC-Datei (Path).
# Fehler: ConfigError bei fehlendem Parameter, falschem Datentyp, ungueltiger
#         Dateiendung oder nicht lesbarer Profildatei bei aktivierter ICC-Option.
# -----------------------------------------------------------------------------
def validate_icc_profile(composite, root_dir):
    enabled = required(composite, 'icc_enabled', bool, 'composite')
    value = required(composite, 'icc_profile', str, 'composite')
    profile = resolve_path(root_dir, value)
    if profile.suffix.lower() not in ('.icc', '.icm'):
        raise ConfigError(f'Invalid ICC profile extension: {profile}')

    # Bei deaktivierter ICC-Verarbeitung darf die Datei noch fehlen. Die
    # TIFF-Umrechnung benoetigt kein Farbprofil und soll unabhaengig bleiben.
    if enabled:
        if not profile.is_file():
            raise ConfigError(f'ICC profile not found or not a file: {profile}')
        try:
            # Tatsaechlich oeffnen statt nur Zugriffsrechte zu betrachten.
            # Die fachliche Pruefung des CMYK-Profils erfolgt spaeter im
            # Composite-Modul; hier wird nur die Lesbarkeit geprueft.
            with profile.open('rb') as file:
                if not file.read(1):
                    raise ConfigError(f'ICC profile is empty: {profile}')
        except OSError as error:
            raise ConfigError(f'Cannot read ICC profile: {profile}: {error}') from error
    return profile


# -----------------------------------------------------------------------------
# FUNKTION: load_config
# Aufgabe: YAML einlesen, Pflichtabschnitte pruefen, Pfade und Zeitangaben
#          aufloesen und eine zentrale Konfigurationsstruktur zurueckgeben.
#          Keine Verzeichnisse anlegen, keine Logdateien oeffnen: Damit ist die
#          Funktion auch fuer reine Diagnosen ohne Schreibzugriff verwendbar.
# Uebergabeparameter: config_file - optionaler Pfad zur imageserver.yaml.
# Rueckgabewert: ImageServerConfig mit geprueften Einstellungen.
# Fehler: ConfigError bei Lese-, Syntax- oder Validierungsfehlern.
# -----------------------------------------------------------------------------
def load_config(config_file=DEFAULT_CONFIG):
    config_file = resolve_path(Path.cwd(), config_file)
    try:
        with config_file.open(encoding='utf-8') as file:
            settings = yaml.load(file, Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigError(f'Cannot read configuration: {error}') from error
    sections = {name: required(settings, name, dict, 'root') for name in (
        'server', 'logging', 'paths', 'supervisor', 'watcher', 'processing', 'jobs', 'composite', 'retention')}
    root_value = required(sections['server'], 'root_dir', str, 'server')
    if not Path(root_value).expanduser().is_absolute():
        raise ConfigError('server.root_dir must be absolute')
    root = resolve_path(Path('/'), root_value)
    log = sections['logging']
    log_dir = resolve_path(root, required(log, 'directory', str, 'logging'))
    level = required(log, 'level', str, 'logging').upper()
    if level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        raise ConfigError(f'Invalid log level: {level}')
    check_int(log, 'max_size_mb', 1, 'logging')
    check_int(log, 'backup_count', 0, 'logging')
    required(log, 'console_output', bool, 'logging')
    log_files = {}
    files = required(log, 'files', dict, 'logging')
    for name in ('config_loader', 'imageserver', 'supervisor', 'folder_watcher', 'tiff_processor', 'job_manager', 'composite', 'cleanup'):
        required(files, name, str, 'logging.files')
    for name, filename in files.items():
        # Logdateinamen duerfen das zentrale Logverzeichnis nicht verlassen.
        if not isinstance(filename, str) or Path(filename).name != filename or Path(filename).suffix.lower() != '.log':
            raise ConfigError(f'Invalid log filename for {name}: {filename!r}')
        target = resolve_path(log_dir, filename)
        if target.parent != log_dir:
            raise ConfigError(f'Log path escapes log directory: {target}')
        log_files[name] = target
    if len(set(log_files.values())) != len(log_files):
        raise ConfigError('Duplicate log paths')
    paths = Paths(root_dir=root, logs=log_dir, **{
        key: resolve_path(root, required(sections['paths'], key, str, 'paths'))
        for key in ('tiff_in', 'tiff_done', 'tiff_failed', 'separation_png', 'composite_png')})
    jobs = Jobs(**{key: resolve_path(root, required(sections['jobs'], key, str, 'jobs'))
                   for key in ('pending', 'processing', 'done', 'failed', 'test')})
    # Verhindert beispielsweise, dass der TIFF-Eingang gleichzeitig das Archiv
    # ist. Auch verschachtelte Arbeitsordner werden vorsorglich abgelehnt.
    directories = [v for k, v in asdict(paths).items() if k != 'root_dir'] + list(asdict(jobs).values())
    for i, directory in enumerate(directories):
        if directory == root or directory in root.parents:
            raise ConfigError(f'Unsafe working directory: {directory}')
        if directory.exists() and not directory.is_dir():
            raise ConfigError(f'Not a directory: {directory}')
        for other in directories[:i]:
            if directory == other or directory in other.parents or other in directory.parents:
                raise ConfigError(f'Overlapping working directories: {directory}, {other}')
    retention = Retention(**{key: parse_duration(required(sections['retention'], key, (str, int), 'retention'))
                             for key in Retention.__dataclass_fields__})
    if retention.cleanup_interval <= 0:
        raise ConfigError('retention.cleanup_interval must be positive')
    supervisor = sections['supervisor']
    check_int(supervisor, 'heartbeat_interval_seconds', 1, 'supervisor')
    for key in ('tiff_processing_enabled', 'composite_processing_enabled', 'cleanup_enabled'):
        required(supervisor, key, bool, 'supervisor')
    watcher = sections['watcher']
    for key, minimum in (('scan_interval_seconds', 1), ('file_stable_seconds', 0), ('retry_interval_seconds', 1), ('max_retries', 0)):
        check_int(watcher, key, minimum, 'watcher')
    validate_processing(sections['processing'])
    composite = sections['composite']
    for key, expected in (('output_format', 'png'), ('output_mode', 'RGB')):
        if required(composite, key, str, 'composite') != expected:
            raise ConfigError(f'composite.{key} must be {expected}')
    for key in ('icc_enabled', 'simulate_paper_color'):
        required(composite, key, bool, 'composite')
    for key in ('max_width', 'max_height'):
        check_int(composite, key, 1, 'composite')
    # Das Profil wird zentral konfiguriert, nicht aus Produktdaten ausgewaehlt.
    icc_profile = validate_icc_profile(composite, root)
    return ImageServerConfig(paths, retention, jobs, log_files, config_file, settings, level, icc_profile)


# -----------------------------------------------------------------------------
# FUNKTION: main
# Aufgabe: Kompatiblen direkten Modulaufruf zur Konfigurationspruefung anbieten.
# Uebergabeparameter: Keine; Kommandozeilenargumente werden intern gelesen.
# Rueckgabewert: Exit-Code 0 bei Erfolg, 1 bei Konfigurationsfehler.
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Validate ImageServer YAML configuration')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f'ERROR: {error}')
        return 1
    print(f'Configuration OK: {config.config_file}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

