"""Diagnoseprogramm zur Auswertung einzelner oder mehrerer PIF-Produktdateien."""
import argparse
import json
from product_details_parser import parse_product_details

# -----------------------------------------------------------------------------
# FUNKTION: main
# Aufgabe: Liest Kommandozeilenparameter, ruft den Parser auf und zeigt die
#          ermittelten Composite-Zuordnungen vollständig oder gefiltert an.
# Übergabeparameter: Keine direkten Parameter; Argumente über die Kommandozeile.
# Rückgabewert: int – 0 = Treffer, 1 = keine Treffer, 2 = Fehler.
# -----------------------------------------------------------------------------
def main():
    # CLI-Optionen definieren. Die Suchfilter schließen sich gegenseitig aus.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+')
    filters = parser.add_mutually_exclusive_group()
    filters.add_argument('--reference', help='ReferenceName oder ContentID, ohne Dateiendung')
    filters.add_argument('--trace')
    filters.add_argument('--all', action='store_true', help='Alle TraceElements anzeigen (Standard)')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    # Alle angegebenen Dateien mit derselben Parser-Funktion auswerten.
    try:
        results = []
        for filename in args.files:
            for product in parse_product_details(filename):
                # ReferenceName und ContentID sind gleichwertige Suchbegriffe.
                # Bei einer Mehrfachbelegung werden sämtliche Trace-IDs angezeigt.
                traces = [t for t in product['traces'].values() if
                          (not args.trace or t['trace_id'] == args.trace) and
                          (not args.reference or any(args.reference in (s['reference_name'], s['content_id']) for s in t['separations'].values()))]
                results.append(dict(file=filename, product_name=product['product_name'],
                    page_count=len(product['pages']), trace_count=len(product['traces']),
                    empty_plates=product['empty_plates'], matches=traces))
        # JSON für maschinelle Weiterverarbeitung, Text für die manuelle Diagnose.
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            for result in results:
                print(f"\n{result['product_name']} | pages={result['page_count']} | traces={result['trace_count']} | empty plates={result['empty_plates']}")
                for t in result['matches']:
                    print(f"  {t['trace_id']}.png | page {t['page_number']} | {len(t['separations'])}C")
                    for c, s in sorted(t['separations'].items()):
                        print(f"    {c}: {s['reference_name']} / {s['content_id']} | ICC: {s['icc_profile']}")
                        # Zusätzlich die physische Position jeder Farbe anzeigen.
                        for p in t['placements']:
                            if p['color'] == c:
                                print(f"      Unit {p['printing_unit']} Couple {p['printing_couple']} Rectangle {', '.join(p['rectangle'])}")
        return 0 if any(r['matches'] for r in results) else 1
    # Fehler als verständliche Meldung ausgeben und einen Fehlercode liefern.
    except Exception as exc:
        print(f'ERROR: {exc}')
        return 2

# Nur beim direkten Start ausführen, nicht beim Import als Modul.
if __name__ == '__main__':
    raise SystemExit(main())

