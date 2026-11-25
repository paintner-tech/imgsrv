# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: test_product_cache.py
#
# BESCHREIBUNG:
# -------------
# Testet die Erkennung neuer und geaenderter Produkte.
#
# TESTABLAUF:
# -----------
#   1. Empfangene Produktliste auswerten.
#   2. Alle Produkte als neu erkennen.
#   3. Erfolgreiche Produktverarbeitung simulieren.
#   4. Cache erneut von der Festplatte einlesen.
#   5. Unveraenderte Produkte erkennen.
#   6. Aenderung eines Produkts simulieren.
#   7. Ausschliesslich das geaenderte Produkt erkennen.
#
# Es werden keine Anforderungen an den Produktserver gesendet.
# Der produktive Produktcache wird nicht veraendert.
# =============================================================================

import yaml

from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta

from library.logger import setup_logging, get_logger
from product_list_parser import parse_product_list

from product_cache import (
    load_product_cache,
    get_changed_products,
    mark_product_processed
)


CONFIG_FILE = Path(
    "/pecom/imgsrv/config/product-interface.yaml"
)

logger = get_logger(__name__)


# =============================================================================
# Fuehrt den vollstaendigen Produktcache-Test durch.
#
# Uebergabeparameter:
#   Keine.
#
# Rueckgabewert:
#   int:
#       0 = Alle Tests erfolgreich.
#       1 = Mindestens ein Fehler aufgetreten.
#
# Exceptions:
#   Auftretende Fehler werden abgefangen und protokolliert.
# =============================================================================
def main() -> int:

    try:

        # Konfiguration einlesen.
        with CONFIG_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:

            config = yaml.safe_load(file)

        setup_logging(config)

        # Vorhandene Produktliste auswerten.
        products = parse_product_list(config)

        if not products:

            raise ValueError(
                "Product list is empty. Test not possible."
            )

        # Temporaeren Testcache verwenden.
        with TemporaryDirectory() as temp_directory:

            config["product_cache"] = {
                "filename": str(
                    Path(temp_directory)
                    / "product_cache.json"
                )
            }

            # ---------------------------------------------------------
            # TEST 1: Alle Produkte sind neu
            # ---------------------------------------------------------

            print("\nTEST 1: New products")

            cache = load_product_cache(config)

            changed = get_changed_products(
                products,
                cache
            )

            print(
                f"Products requiring details: {len(changed)}"
            )

            if len(changed) != len(products):

                raise RuntimeError("TEST 1 failed")

            # Erfolgreiche Verarbeitung simulieren.
            for product in changed:

                mark_product_processed(
                    config,
                    cache,
                    product
                )

            print("TEST 1: PASSED")

            # ---------------------------------------------------------
            # TEST 2: Keine Produkte wurden geaendert
            # ---------------------------------------------------------

            print("\nTEST 2: Unchanged products")

            # Neustart simulieren und Cache erneut einlesen.
            cache = load_product_cache(config)

            changed = get_changed_products(
                products,
                cache
            )

            print(
                f"Products requiring details: {len(changed)}"
            )

            if changed:

                raise RuntimeError("TEST 2 failed")

            print("TEST 2: PASSED")

            # ---------------------------------------------------------
            # TEST 3: Ein Produkt wurde geaendert
            # ---------------------------------------------------------

            print("\nTEST 3: Changed product")

            # Zeitstempel des ersten Produkts um eine Minute
            # erhoehen, um eine Produktaenderung zu simulieren.
            old_update = datetime.strptime(
                products[0]["product_update"],
                "%Y%m%d%H%M"
            )

            new_update = old_update + timedelta(minutes=1)

            products[0]["product_update"] = (
                new_update.strftime("%Y%m%d%H%M")
            )

            products[0]["product_update_datetime"] = new_update

            changed = get_changed_products(
                products,
                cache
            )

            print(
                f"Products requiring details: {len(changed)}"
            )

            for product in changed:

                print(
                    f"Changed product: "
                    f"{product['product_name']!r}"
                )

            if (
                len(changed) != 1
                or changed[0]["product_name"]
                != products[0]["product_name"]
            ):

                raise RuntimeError("TEST 3 failed")

            print("TEST 3: PASSED")

            # ---------------------------------------------------------
            # TEST 4: Produktverarbeitung fehlgeschlagen
            # ---------------------------------------------------------

            print("\nTEST 4: Retry unprocessed product")

            # Das geaenderte Produkt bewusst NICHT als erfolgreich
            # verarbeitet markieren.
            #
            # Bei der naechsten Pruefung muss es deshalb erneut
            # in der Ergebnisliste enthalten sein.
            cache = load_product_cache(config)

            changed = get_changed_products(
                products,
                cache
            )

            if (
                len(changed) != 1
                or changed[0]["product_name"]
                != products[0]["product_name"]
            ):

                raise RuntimeError("TEST 4 failed")

            print("TEST 4: PASSED")

        print("\nAll product cache tests passed.")

        logger.info(
            "Product cache test completed successfully"
        )

        return 0

    except Exception as error:

        logger.exception(
            "Product cache test failed"
        )

        print(f"ERROR: {error}")

        return 1


# =============================================================================
# Programmeinstieg
# =============================================================================

if __name__ == "__main__":
    raise SystemExit(main())
