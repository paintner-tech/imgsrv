# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: test_request_sender.py
#
# BESCHREIBUNG:
# -------------
# Eigenstaendiges Testprogramm fuer das Modul request_sender.py.
#
# Das Programm liest die Konfiguration direkt ein und initialisiert
# das zentrale Logging.
#
# Ohne weitere Parameter werden ausschliesslich die PIF-Datei
# und die zugehoerige SYC-Datei erzeugt.
#
# Mit dem Parameter --send werden die Dateien zusaetzlich
# an den Produktserver uebertragen.
#
# Ein automatisches Polling findet nicht statt.
# =============================================================================

import argparse
import yaml

from pathlib import Path

from library.logger import setup_logging, get_logger

from request_sender import (
    create_product_list_request,
    send_product_list_request
)


# Pfad zur Konfigurationsdatei.
CONFIG_FILE = Path(
    "/pecom/imgsrv/config/product-interface.yaml"
)

logger = get_logger(__name__)


# =============================================================================
# Fuehrt einen einmaligen Test des Request-Sender-Moduls durch.
#
# Standardmaessig werden nur die Anforderungsdateien erstellt.
# Eine Uebertragung erfolgt ausschliesslich mit --send.
#
# Uebergabeparameter:
#   Keine
#
# Rueckgabewert:
#   int:
#       0 = Test erfolgreich abgeschlossen.
#       1 = Fehler waehrend der Testausfuehrung.
#
# Exceptions:
#   Erwartete Datei-, Konfigurations- und XML-Fehler werden
#   abgefangen und auf der Konsole ausgegeben.
# =============================================================================
def main() -> int:

    # Kommandozeilenparameter auswerten.
    parser = argparse.ArgumentParser(
        description="Test ImageServer Product Interface"
    )

    parser.add_argument(
        "--send",
        action="store_true",
        help="Transfer request to product server"
    )

    args = parser.parse_args()

    try:

        # Konfiguration direkt einlesen.
        # Der spaetere Config Loader wird hier noch nicht benoetigt.
        with CONFIG_FILE.open(
            "r",
            encoding="utf-8"
        ) as config_file:

            config = yaml.safe_load(config_file)

        # Zentrales Logging initialisieren.
        setup_logging(config)

        logger.info("Starting request sender test")

        # -------------------------------------------------------------
        # TEST 1: Anforderung nur lokal erzeugen
        # -------------------------------------------------------------

        if not args.send:

            request_file, sync_file = (
                create_product_list_request(config)
            )

            print("\nGenerated request file:")
            print(request_file)

            print("\nXML content:")
            print(request_file.read_text(encoding="utf-8"))

            print("\nGenerated synchronization file:")
            print(sync_file)

            print(
                f"Sync file size: "
                f"{sync_file.stat().st_size} bytes"
            )

            print("\nNo files transferred to product server.")

            logger.info("Local request creation test completed")

            return 0

        # -------------------------------------------------------------
        # TEST 2: Anforderung erzeugen und uebertragen
        # -------------------------------------------------------------

        logger.info("Starting request transfer test")

        if send_product_list_request(config):

            print("\nRequest successfully transferred.")

            return 0

        print("\nRequest transfer failed.")

        return 1

    except Exception as error:

        # Im Testprogramm werden auch unerwartete Fehler ausgegeben,
        # damit Probleme waehrend der Entwicklung sichtbar bleiben.
        logger.exception("Request sender test failed")

        print(f"\nERROR: {error}")

        return 1


# =============================================================================
# Programmeinstieg
# =============================================================================

if __name__ == "__main__":
    raise SystemExit(main())
