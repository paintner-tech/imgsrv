# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: test_product_list_parser.py
#
# BESCHREIBUNG:
# -------------
# Eigenstaendiges Testprogramm zur Auswertung einer bereits
# empfangenen XML-Produktliste.
#
# Das Programm erzeugt und versendet keine Anforderungen.
# Die vorhandene Antwortdatei wird ausschliesslich gelesen.
# =============================================================================

import yaml

from pathlib import Path

from library.logger import setup_logging, get_logger
from product_list_parser import parse_product_list


# Pfad zur zentralen Konfigurationsdatei.
CONFIG_FILE = Path(
    "/pecom/imgsrv/config/product-interface.yaml"
)

logger = get_logger(__name__)


# =============================================================================
# Fuehrt einen einmaligen Test des Product List Parsers durch.
#
# Liest die Konfiguration ein, initialisiert das Logging
# und gibt alle gefundenen Produktinformationen aus.
#
# Uebergabeparameter:
#   Keine.
#
# Rueckgabewert:
#   int:
#       0 = Test erfolgreich.
#       1 = Fehler waehrend der Testausfuehrung.
#
# Exceptions:
#   Fehler werden abgefangen und auf der Konsole sowie
#   bei initialisiertem Logging in der Logdatei ausgegeben.
# =============================================================================
def main() -> int:

    try:

        # Konfigurationsdatei einlesen.
        with CONFIG_FILE.open(
            "r",
            encoding="utf-8"
        ) as config_file:

            config = yaml.safe_load(config_file)

        # Zentrales Logging initialisieren.
        setup_logging(config)

        logger.info("Starting product list parser test")

        # Vorhandene XML-Produktliste auswerten.
        products = parse_product_list(config)

        # Anzahl der gefundenen Produkte ausgeben.
        print()
        print("=" * 70)
        print("ImageServer - Product List")
        print("=" * 70)
        print(f"Products found: {len(products)}")

        # Alle Produktinformationen auf der Konsole ausgeben.
        for index, product in enumerate(
            products,
            start=1
        ):

            print()
            print(f"Product {index}")
            print("-" * 70)

            # repr() macht fuehrende und nachgestellte
            # Leerzeichen durch Anfuehrungszeichen sichtbar.
            print(
                f"Product name  : "
                f"{product['product_name']!r}"
            )

            print(
                f"Name length   : "
                f"{len(product['product_name'])}"
            )

            print(
                f"Order         : "
                f"{product['order']}"
            )

            print(
                f"Product group : "
                f"{product['product_group']}"
            )

            print(
                f"Last update   : "
                f"{product['product_update_datetime']}"
            )

        print()
        print("=" * 70)
        print("Product list parser test completed")
        print("=" * 70)

        logger.info(
            "Product list parser test completed successfully"
        )

        return 0

    except Exception as error:

        logger.exception(
            "Product list parser test failed"
        )

        print(f"ERROR: {error}")

        return 1


# =============================================================================
# Programmeinstieg
# =============================================================================

if __name__ == "__main__":
    raise SystemExit(main())
