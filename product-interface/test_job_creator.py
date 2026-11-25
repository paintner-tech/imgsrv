# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: test_job_creator.py
#
# BESCHREIBUNG:
# -------------
# Testprogramm zur Erzeugung von Bildauftraegen.
#
# Unterstuetzte Eingabedateien:
#   - PIF/XML-Antwortdateien des Produktservers
#   - JSON-Produktdateien des Supervisors
#
# Ohne --output werden keine Auftragsdateien geschrieben.
# Mit --all werden die vollstaendigen Auftragsdaten angezeigt.
# =============================================================================

import argparse
import json
import sys

from pathlib import Path
from collections import Counter

from product_details_parser import parse_product_details
from job_creator import create_jobs, write_jobs


# =============================================================================
# FUNKTION: load_products
#
# Aufgabe:
#   Liest Produktdaten aus einer PIF-, XML- oder JSON-Datei.
#
# Uebergabeparameter:
#   filename: Path
#       Pfad zur Produktdatei.
#
# Rueckgabewert:
#   list[dict]:
#       Liste der eingelesenen Produkte.
# =============================================================================
def load_products(filename: Path) -> list[dict]:

    # Bereits ausgewertete Produktdaten einlesen.
    if filename.suffix.lower() == ".json":

        with filename.open(
            "r",
            encoding="utf-8"
        ) as file:

            product = json.load(file)

        # Sicherstellen, dass es sich um Produktdaten
        # und nicht um den Product Cache handelt.
        if (
            not isinstance(product, dict)
            or "product_name" not in product
            or "traces" not in product
            or not isinstance(product["traces"], dict)
        ):

            raise ValueError(
                f"Invalid product JSON: {filename}"
            )

        return [product]

    # Originale XML-Antwort ueber den vorhandenen
    # Product Details Parser auswerten.
    if filename.suffix.lower() in (".pif", ".xml"):

        return parse_product_details(filename)

    raise ValueError(
        f"Unsupported file format: {filename}"
    )


# =============================================================================
# FUNKTION: main
#
# Aufgabe:
#   Liest Produktdateien ein, erzeugt Bildauftraege
#   und zeigt die Ergebnisse an.
#
#   Optional werden die Auftraege als JOB-Dateien
#   gespeichert.
#
# Uebergabeparameter:
#   Keine direkten Parameter.
#   Argumente werden ueber die Kommandozeile uebergeben.
#
# Rueckgabewert:
#   int:
#       0 = Test erfolgreich.
#       2 = Fehler waehrend der Verarbeitung.
# =============================================================================
def main() -> int:

    parser = argparse.ArgumentParser(
        description="Test ImageServer Job Creator"
    )

    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="Product PIF, XML or JSON files"
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Print complete job JSON"
    )

    parser.add_argument(
        "--output",
        type=Path,
        help="Write jobs to specified directory"
    )

    args = parser.parse_args()

    try:

        products = []

        # ---------------------------------------------------------------------
        # Produktdaten einlesen
        # ---------------------------------------------------------------------

        for filename in args.files:

            print(f"Reading product file: {filename}")

            products.extend(
                load_products(filename)
            )

        print()
        print("=" * 70)
        print("ImageServer - Job Creator")
        print("=" * 70)

        print(f"Products loaded: {len(products)}")

        # ---------------------------------------------------------------------
        # Bildauftraege erzeugen
        # ---------------------------------------------------------------------

        jobs = create_jobs(products)

        color_statistics = Counter()

        output_count = 0

        # ---------------------------------------------------------------------
        # Erzeugte Auftraege anzeigen
        # ---------------------------------------------------------------------

        for index, job in enumerate(jobs, start=1):

            separation_count = job["separation_count"]

            color_statistics[separation_count] += 1

            output_count += len(job["composites"])

            if args.all:

                print()
                print(f"Job {index}")
                print("-" * 70)

                print(
                    json.dumps(
                        job,
                        indent=4,
                        ensure_ascii=False
                    )
                )

            else:

                colors = ",".join(
                    sorted(job["separations"])
                )

                print(
                    f"Job {index:4} | "
                    f"{separation_count}C | "
                    f"{colors:7} | "
                    f"{', '.join(job['composites'])}"
                )

        # ---------------------------------------------------------------------
        # Zusammenfassung ausgeben
        # ---------------------------------------------------------------------

        print()
        print("=" * 70)
        print("Job statistics")
        print("=" * 70)

        print(f"Products             : {len(products)}")
        print(f"Jobs                 : {len(jobs)}")
        print(f"Composite filenames  : {output_count}")

        print(
            f"Duplicate calculations avoided: "
            f"{output_count - len(jobs)}"
        )

        print()

        for count in range(1, 5):

            print(
                f"{count}C jobs: "
                f"{color_statistics[count]}"
            )

        # ---------------------------------------------------------------------
        # Auftragsdateien optional speichern
        # ---------------------------------------------------------------------

        if args.output:

            files = write_jobs(
                jobs,
                args.output
            )

            print()
            print(f"Job directory: {args.output}")
            print(f"Job files available: {len(files)}")

        else:

            print()
            print("No job files written.")

        return 0

    except Exception as error:

        print(
            f"ERROR: {error}",
            file=sys.stderr
        )

        return 2


# =============================================================================
# Programmeinstieg
# =============================================================================

if __name__ == "__main__":

    raise SystemExit(main())
