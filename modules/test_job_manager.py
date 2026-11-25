# =============================================================================
# TEST JOB MANAGER – Lesendes Diagnoseprogramm für Bildaufträge
# =============================================================================
# Standardmäßig wird jobs/test geprüft. Mit --all werden sämtliche Farben,
# Dateitreffer und Composite-Ziele ausgegeben. Keine Dateien werden verändert.
# Die Pfade sind für den Prototyp vorbelegt und per Kommandozeile änderbar.
# Die spätere Anbindung an imageserver.ini bleibt bewusst getrennt.
# =============================================================================
import argparse
from collections import Counter
import json
import sys
from pathlib import Path
try:
    from .job_manager import scan_jobs
except ImportError:
    from job_manager import scan_jobs


# -----------------------------------------------------------------------------
# FUNKTION: has_matches
# Aufgabe: Erkennt Aufträge mit mindestens einer vorhandenen Bilddatei.
# Übergabeparameter: report (dict), Diagnose eines Auftrags aus scan_jobs.
# Rückgabewert: bool, True bei TIFF-, PNG- oder Composite-Treffer.
# Hinweis: Vorhandensein ist kein Nachweis der Aktualität oder Bildgültigkeit.
# -----------------------------------------------------------------------------
def has_matches(report):
    return (any(s['tiffs'] or s['pngs'] for s in report['separations'].values())
            or any(report['composites'].values()))


# -----------------------------------------------------------------------------
# FUNKTION: report_sort_key
# Aufgabe: Sortiert Treffer zuerst, danach nach Produkt und Seitennummer.
# Übergabeparameter: report (dict), Diagnose eines Auftrags.
# Rückgabewert: Tupel mit Trefferpriorität, Produktname, Seitennummer und Job-ID.
# -----------------------------------------------------------------------------
def report_sort_key(report):
    source = (report['sources'] or [{}])[0]
    return (not has_matches(report), str(source.get('product_name', '')),
            int(source.get('page_number', 0)), report['job_id'])


# -----------------------------------------------------------------------------
# FUNKTION: print_report
# Aufgabe: Zeigt gefundene und fehlende Separationen sowie Composite-Ziele an.
# Übergabeparameter:
#   report: dict, Diagnose eines Auftrags aus scan_jobs.
#   directories: dict, tatsächlich geprüfte Verzeichnisse als Path-Objekte.
#   detailed: bool, True für vollständige Detailausgabe.
# Rückgabewert: None; Ausgabe erfolgt auf stdout.
# -----------------------------------------------------------------------------
def print_report(report, directories, detailed):
    print(f"\n{report['status']} | {', '.join(report['composites'])}")
    if not detailed:
        return
    print('-' * 72)
    print(f"Job file: {directories['Jobs'] / (report['job_id'] + '.job')}")
    for source in report['sources']:
        print(f"Product: {source.get('product_name')} | Page: {source.get('page_number')} | Trace: {source.get('trace_id')}")
    # Die Anzahl beschreibt die Dateiverfügbarkeit, nicht die Freigabe zur
    # Verarbeitung. Mehrdeutige Treffer bleiben ausdrücklich gekennzeichnet.
    seps = report['separations']
    available = sum(bool(s['tiffs'] or s['pngs']) for s in seps.values())
    print(f'Separations available: {available}/{len(seps)}')
    for color in 'CMYK':
        if color not in seps:
            continue
        sep = seps[color]
        print(f"  {color}: {sep.get('reference_name')} / {sep.get('content_id')} | {sep['status']}")
        print(f"     ICC: {sep.get('icc_profile')}")
        for label, key, directory in [('TIFF', 'tiffs', 'TIFFs'), ('PNG', 'pngs', 'PNGs')]:
            if sep[key]:
                for filename in sep[key]:
                    print(f'     {label} FOUND: {filename}')
            else:
                print(f"     {label} NOT FOUND in {directories[directory]}")
    for name, exists in report['composites'].items():
        print(f"  Composite {'FOUND' if exists else 'NOT FOUND'}: {directories['Composites'] / name}")
    missing = [c for c in 'CMYK' if c in seps and seps[c]['status'] == 'MISSING']
    if missing:
        print('  Missing colors: ' + ', '.join(missing))


# -----------------------------------------------------------------------------
# FUNKTION: main
# Aufgabe: Liest Kommandozeilenparameter, prüft Aufträge und zeigt Ergebnisse.
# Übergabeparameter: Keine; argparse verwendet sys.argv.
# Rückgabewert: int, 0 bei erfolgreicher Diagnose, 2 bei Prüf-/Lesefehlern.
#               Wartende Aufträge sind kein Programmfehler.
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Inspect image jobs without modifying files')
    parser.add_argument('--root', default='/pecom/imgsrv', help='Imageserver root directory')
    parser.add_argument('--jobs', help='Job directory (default: ROOT/jobs/test)')
    parser.add_argument('--tiffs', help='TIFF directory (default: ROOT/tiffs-in)')
    parser.add_argument('--pngs', help='Separation PNG directory (default: ROOT/pngs)')
    parser.add_argument('--composites', help='Composite directory (default: ROOT/composites/_01)')
    parser.add_argument('--all', action='store_true', help='Show all separation and output details')
    parser.add_argument('--found', action='store_true', help='Show only jobs with existing image files')
    parser.add_argument('--reference', help='Filter by exact ReferenceName or ContentID')
    parser.add_argument('--trace', help='Filter by TraceElementIdentification')
    parser.add_argument('--json', action='store_true', help='Output machine-readable JSON')
    args = parser.parse_args()
    root = Path(args.root)
    # Tatsächlich verwendete Suchpfade einmal bestimmen und auch ausgeben.
    directories = dict(Jobs=Path(args.jobs or root/'jobs/test').absolute(),
                       TIFFs=Path(args.tiffs or root/'tiffs-in').absolute(),
                       PNGs=Path(args.pngs or root/'pngs').absolute(),
                       Composites=Path(args.composites or root/'composites/_01').absolute())
    try:
        reports, errors = scan_jobs(*directories.values())
    except OSError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    # Filter erst nach der vollständigen Prüfung anwenden, damit Konflikte mit
    # anderen Aufträgen nicht durch eine gezielte Abfrage verborgen werden.
    if args.reference:
        reports = [r for r in reports if any(args.reference in (s.get('reference_name'), s.get('content_id'))
                                            for s in r['separations'].values())]
    if args.trace:
        reports = [r for r in reports if args.trace + '.png' in r['composites']]
    # Die Gesamtstatistik bleibt auch bei --found erhalten. Suchfilter nach
    # Referenz oder Trace begrenzen dagegen bewusst den ausgewerteten Bereich.
    summary = dict(Counter(r['status'] for r in reports))
    total = len(reports)
    found = sum(has_matches(r) for r in reports)
    if args.found:
        reports = [r for r in reports if has_matches(r)]
    # Treffer zuerst; innerhalb einer Gruppe nach Produkt und Seitennummer.
    reports.sort(key=report_sort_key)
    if args.json:
        print(json.dumps(dict(reports=reports, errors=errors, summary=summary,
                              directories={k: str(v) for k, v in directories.items()},
                              total=total, found=found), indent=2, ensure_ascii=False))
    else:
        print('=' * 72 + '\nIMAGESERVER - JOB MANAGER\n' + '=' * 72)
        for label, directory in directories.items():
            print(f'{label:12}: {directory}')
        for report in reports:
            print_report(report, directories, args.all or args.found)
        print('\n' + '=' * 72 + '\nSUMMARY\n' + '=' * 72)
        print(f'Jobs checked: {total} | With image matches: {found} | Shown: {len(reports)}')
        print(f'Errors: {len(errors)} | Status: {summary}')
        for error in errors:
            print(f"ERROR: {error['file']}: {error['error']}", file=sys.stderr)
    return 2 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())

