# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: test_product_details_request.py
#
# BESCHREIBUNG:
# -------------
# Eigenstaendiges Testprogramm zur Erstellung und Uebertragung
# individueller Produktdetailanforderungen.
#
# TESTABLAUF:
# -----------
#   1. Konfiguration einlesen.
#   2. Logging initialisieren.
#   3. Empfangene Produktliste auswerten.
#   4. Produktcache einlesen.
#   5. Neue und geaenderte Produkte ermitteln.
#   6. Individuelle Anforderungsdateien erzeugen.
#   7. Optional eine einzelne Anforderung versenden.
#
# Ohne Parameter werden keine Dateien uebertragen.
#
# Der Produktcache wird durch dieses Testprogramm
# nicht veraendert.
# =============================================================================

import argparse
import yaml

from pathlib import Path

from library.logger import setup_logging, get_logger

from product_list_parser import parse_product_list

from product_cache import (
    load_product_cache,
    get_changed_products
)

from request_sender import (
    get_product_detail_filenames,
    create_product_detail_request,
    send_product_detail_request
)


# Pfad zur Konfigurationsdatei.
CONFIG_FILE = Path(
    "/pecom/imgsrv/config/product-interface.yaml"
)

logger = get_logger(__name__)


# =============================================================================
# Fuehrt einen einmaligen Test der Produktdetailanforderungen durch.
#
# Beruecksichtigt ausschliesslich neue und geaenderte Produkte.
#
# Optional kann ueber --send eine einzelne Produktanforderung
# an den Produktserver uebertragen werden.
#
# Uebergabeparameter:
#   Keine.
#
# Rueckgabewert:
#   int:
#       0 = Test erfolgreich abgeschlossen.
#       1 = Fehler waehrend der Testausfuehrung.
#
# Exceptions:
#   Auftretende Fehler werden abgefangen und protokolliert.
# =============================================================================
def main() -> int:

    parser = argparse.ArgumentParser(
        description="Test product detail requests"
    )

    parser.add_argument(
        "--send",
        type=int,
        metavar="PRODUCT_NUMBER",
        help="Transfer selected product detail request"
    )

    # Optional alle neuen und geaenderten Produkte anfordern.
    parser.add_argument(
        "--send-all",
        action="store_true",
        help="Transfer all new and changed product detail requests"
    )

    args = parser.parse_args()

    try:

        # ---------------------------------------------------------------------
        # Konfiguration und Logging initialisieren
        # ---------------------------------------------------------------------

        with CONFIG_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:

            config = yaml.safe_load(file)

        setup_logging(config)

        logger.info(
            "Starting product detail request test"
        )

        # ---------------------------------------------------------------------
        # Produktliste und Produktcache einlesen
        # ---------------------------------------------------------------------

        products = parse_product_list(config)

        cache = load_product_cache(config)

        # Ausschliesslich neue und geaenderte Produkte ermitteln.
        changed_products = get_changed_products(
            products,
            cache
        )

        print()
        print("=" * 70)
        print("ImageServer - Product Detail Requests")
        print("=" * 70)

        print(f"Total products    : {len(products)}")
        print(f"Required requests : {len(changed_products)}")

        # Keine Anforderung notwendig, wenn alle
        # Produkte bereits erfolgreich verarbeitet wurden.
        if not changed_products:

            print("No new or changed products found.")

            return 0

        # ---------------------------------------------------------------------
        # Dateinamen auf Kollisionen pruefen
        # ---------------------------------------------------------------------

        # Unterschiedliche Produktnamen koennen nach dem
        # Entfernen der Leerzeichen identische Dateinamen
        # ergeben.
        #
        # Deshalb alle Produkte vor der Dateierstellung
        # auf solche Namenskollisionen pruefen.
        used_filenames = {}

        for product in products:

            product_name = product["product_name"]

            request_filename, _ = (
                get_product_detail_filenames(
                    config,
                    product_name
                )
            )

            if request_filename in used_filenames:

                raise ValueError(
                    f"Filename collision: {request_filename}"
                )

            used_filenames[request_filename] = product_name

        # ---------------------------------------------------------------------
        # Anforderungsdateien erzeugen
        # ---------------------------------------------------------------------

        for index, product in enumerate(
            changed_products,
            start=1
        ):

            request_file, sync_file, response_file = (
                create_product_detail_request(
                    config,
                    product
                )
            )

            print()
            print(f"Product {index}")
            print("-" * 70)

            print(
                f"Product name : "
                f"{product['product_name']!r}"
            )

            print(
                f"Last update  : "
                f"{product['product_update']}"
            )

            print(f"Request file : {request_file.name}")
            print(f"Sync file    : {sync_file.name}")
            print(f"Response file: {response_file.name}")

            print(
                f"Sync size    : "
                f"{sync_file.stat().st_size} bytes"
            )

        # -------------------------------------------------------------------------
        # Alle neuen und geaenderten Produkte anfordern
        # -------------------------------------------------------------------------

        if args.send_all:

            successful = 0
            failed = 0

            # Alle erforderlichen Produktanforderungen nacheinander
            # an den Produktserver uebertragen.
            for product in changed_products:

                logger.info(
                    "Sending product detail request: %r",
                    product["product_name"]
                )

                # Erfolgreiche und fehlgeschlagene Uebertragungen zaehlen.
                # Bei einem Fehler wird mit dem naechsten Produkt
                # fortgefahren.
                if send_product_detail_request(config, product):
                    successful += 1
                else:
                    failed += 1

            # Zusammenfassung aller Uebertragungen protokollieren.
            logger.info(
                "Transfer completed: total=%d, successful=%d, failed=%d",
                len(changed_products),
                successful,
                failed
            )

            print(f"\nSuccessful requests: {successful}")
            print(f"Failed requests    : {failed}")

            # Fehlercode zurueckgeben, falls mindestens eine
            # Produktanforderung nicht uebertragen werden konnte.
            if failed > 0:
                return 1

        # -------------------------------------------------------------------------
        # Einzelnes Produkt anfordern
        # -------------------------------------------------------------------------

        elif args.send is not None:

            # Uebergebene Produktnummer auf Gueltigkeit pruefen.
            if not 1 <= args.send <= len(changed_products):

                raise ValueError(
                    "Invalid product number"
                )

            selected_product = changed_products[
                args.send - 1
            ]

            print()
            print(
                f"Sending request for: "
                f"{selected_product['product_name']!r}"
            )

            if not send_product_detail_request(
                config,
                selected_product
            ):

                return 1

            print("Request successfully transferred.")

        else:

            print()
            print("No files transferred to product server.")

        logger.info(
            "Product detail request test completed"
        )

        return 0

    except Exception as error:

        logger.exception(
            "Product detail request test failed"
        )

        print(f"ERROR: {error}")

        return 1


# =============================================================================
# Programmeinstieg
# =============================================================================

if __name__ == "__main__":
    raise SystemExit(main())
