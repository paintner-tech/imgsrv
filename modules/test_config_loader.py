#!/usr/bin/env python3
# =============================================================================
# Diagnoseprogramm fuer den YAML Config Loader des Imageservers.
# --all zeigt sowohl Originalwerte als auch aufgeloeste Pfade und Sekunden.
# --json liefert dieselben Daten maschinenlesbar. Ohne Parameter erfolgt
# eine kurze Pruefung. Es werden keine Produktionsdateien veraendert.
# =============================================================================
import argparse
import json
import sys
from dataclasses import asdict
import yaml

if __package__:
    from .config_loader import load_config, ConfigError, DEFAULT_CONFIG
else:
    from config_loader import load_config, ConfigError, DEFAULT_CONFIG


# -----------------------------------------------------------------------------
# FUNKTION: diagnostic_data
# Aufgabe: Alle eingelesenen und aufgeloesten Einstellungen zusammenstellen.
# Uebergabeparameter: config - gepruefte ImageServerConfig-Instanz.
# Rueckgabewert: Dictionary mit Originalwerten und normalisierten Werten.
# -----------------------------------------------------------------------------
def diagnostic_data(config):
    # Path-Objekte werden fuer die YAML-/JSON-Ausgabe zu Strings umgewandelt.
    return {
        'config_file': str(config.config_file),
        'resolved_paths': {key: str(value) for key, value in asdict(config.paths).items()},
        'resolved_jobs': {key: str(value) for key, value in asdict(config.jobs).items()},
        'retention_seconds': asdict(config.retention),
        'resolved_log_files': {key: str(value) for key, value in config.log_files.items()},
        'log_level': config.log_level,
        # Der aufgeloeste Profilpfad ist auch bei deaktivierter ICC-Option sichtbar.
        'icc': {
            'enabled': config.settings['composite']['icc_enabled'],
            'profile_path': str(config.icc_profile),
            'profile_available': config.icc_profile.is_file(),
        },
        'settings': config.settings,
    }


# -----------------------------------------------------------------------------
# FUNKTION: main
# Aufgabe: Kommandozeilenargumente auswerten, Konfiguration laden und anzeigen.
# Uebergabeparameter: Keine; Argumente werden aus sys.argv gelesen.
# Rueckgabewert: Exit-Code 0 bei Erfolg, 1 bei ungueltiger Konfiguration.
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='ImageServer YAML configuration diagnostics')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG), help='Alternative YAML file')
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--all', action='store_true', help='Show all raw and resolved settings')
    output.add_argument('--json', action='store_true', help='Show all settings as JSON')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(diagnostic_data(config), indent=2, ensure_ascii=False))
    elif args.all:
        print('Configuration OK')
        print(yaml.safe_dump(diagnostic_data(config), sort_keys=False, allow_unicode=True))
    else:
        print(f'Configuration OK: {config.config_file}')
        print(f'Root directory: {config.paths.root_dir}')
        print(f'Log level: {config.log_level}')
        print(f'DPI configurations: {len(config.settings["processing"]["dpi_configs"])}')
        print('Use --all for full diagnostic output.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

