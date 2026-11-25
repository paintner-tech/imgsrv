# =============================================================================
# TEST JOB CREATOR – Diagnoseprogramm für die Auftragserzeugung
# =============================================================================
# Liest eine oder mehrere PIF-Dateien über den Product Details Parser und
# übergibt die Ergebnisse an den Job Creator. Standardmäßig werden nur die
# geplanten Aufträge angezeigt. --all zeigt das komplette JSON jedes Auftrags.
# Erst mit --output werden tatsächlich .job-Dateien erzeugt. Dadurch kann
# die Auftragserzeugung gefahrlos mit echten Kundendaten untersucht werden.
# =============================================================================
import argparse
import json
import sys
from product_details_parser import parse_product_details
from job_creator import create_jobs, write_jobs


# -----------------------------------------------------------------------------
# FUNKTION: main
# Aufgabe:
#   Wertet Kommandozeilenparameter aus, liest die Produktantwortdateien,
#   erzeugt Bildaufträge und zeigt diese an. Schreibt sie nur bei --output.
# Übergabeparameter:
#   Keine direkten Parameter; argparse liest die Kommandozeile (sys.argv).
# Rückgabewert:
#   int: 0 bei erfolgreicher Auswertung, 2 bei einem Verarbeitungsfehler.
#   argparse beendet das Programm bei --help oder ungültigen Argumenten selbst.
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Inspect composite jobs from PIF/XML files')
    parser.add_argument('files', nargs='+', help='One or more product response files')
    parser.add_argument('--output', help='Write jobs to this test directory (optional)')
    parser.add_argument('--all', action='store_true', help='Print full job JSON')
    args = parser.parse_args()
    try:
        # Alle Produkte zuerst einlesen. Dadurch können identische Bildrezepte
        # auch über mehrere PIF-Dateien hinweg zusammengefasst werden.
        products = []
        for filename in args.files:
            products.extend(parse_product_details(filename))
        jobs = create_jobs(products)

        # Die Detailansicht zeigt exakt die Daten, die später in den .job-Dateien
        # stehen. Ohne --all genügt eine kompakte Zeile je Bildauftrag.
        for job in jobs:
            print(json.dumps(job, indent=2, ensure_ascii=False, sort_keys=True) if args.all else
                  f"{job['separation_count']}C | {', '.join(job['composites'])}")
        output_count = sum(len(j['composites']) for j in jobs)
        print(f"Jobs: {len(jobs)} | Output PNG names: {output_count}")
        print(f"Avoided duplicate composite calculations: {output_count - len(jobs)}")

        # Keine implizite Verwendung des produktiven Job-Verzeichnisses.
        # Nur der explizite Parameter --output erlaubt Dateisystemänderungen.
        if args.output:
            print(f'Job files available: {len(write_jobs(jobs, args.output))}')
        return 0
    except Exception as exc:
        # Fehler auf stderr melden und einen von null verschiedenen Exitcode
        # liefern, damit Shell-Skripte einen fehlgeschlagenen Test erkennen.
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


# Beim Import durch andere Programme darf keine Testauswertung starten.
if __name__ == '__main__':
    raise SystemExit(main())

