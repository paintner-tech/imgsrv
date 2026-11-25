# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: product_list_parser.py
#
# BESCHREIBUNG:
# -------------
# Dieses Modul wertet die vom Produktserver empfangene
# XML-Produktliste aus.
#
# FUNKTIONSUMFANG:
# ----------------
#   1. Antwortdatei anhand der Konfiguration ermitteln.
#   2. XML-Datei einlesen.
#   3. XML-Struktur ueberpruefen.
#   4. Einzelne Produktdatensaetze extrahieren.
#   5. Produktinformationen auf Vollstaendigkeit pruefen.
#   6. Produktliste als Python-Datenstruktur zurueckgeben.
#   7. Ergebnisse und Fehler protokollieren.
#
# WICHTIG:
# --------
# Der Produktname wird exakt aus der XML-Datei uebernommen.
# Leerzeichen innerhalb des Produktnamens sowie fuehrende
# und nachgestellte Leerzeichen bleiben erhalten.
#
# Der Produktname wird spaeter fuer die Anforderung der
# einzelnen Produktinformationen benoetigt.
#
# Dieses Modul versendet keine Anforderungen und veraendert
# oder loescht keine empfangenen Dateien.
#
# Die Pruefung, ob die Antwortdatei vollstaendig uebertragen
# wurde, erfolgt spaeter durch die Ablaufsteuerung.
# =============================================================================

import xml.etree.ElementTree as ET

from pathlib import Path
from datetime import datetime

from library.logger import get_logger


# Modulbezogenen Logger des zentralen Logging-Moduls beziehen.
logger = get_logger(__name__)


# =============================================================================
# Ermittelt den vollstaendigen Pfad zur empfangenen Produktliste.
#
# Das Antwortverzeichnis und der Dateiname werden aus der
# Konfiguration des Product Interface ausgelesen.
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#
# Rueckgabewert:
#   Path:
#       Vollstaendiger Pfad zur XML-Produktliste.
#
# Exceptions:
#   KeyError:
#       Ein erforderlicher Konfigurationsparameter fehlt.
#
#   TypeError:
#       Ein Konfigurationsparameter besitzt einen
#       ungueltigen Datentyp.
# =============================================================================
# =============================================================================
def get_response_file(config: dict) -> Path:

    # Konfiguriertes Antwortverzeichnis ermitteln.
    response_directory = Path(
        config["local"]["response_directory"]
    )

    # Vollstaendigen Dateinamen aus der Konfiguration auslesen.
    response_filename = config["product_list"]["response_file"]

    # Vollstaendigen Dateipfad erzeugen.
    response_file = response_directory / response_filename

    # Fehlerhafte Konfiguration fruehzeitig erkennen.
    if response_file.suffix != ".pif":
        raise ValueError(
            "Product list response filename must end with .pif"
        )

    return response_file

# =============================================================================
# Liest den Textinhalt eines erforderlichen XML-Elements aus.
#
# Die Zeichenkette wird unveraendert zurueckgegeben.
# Insbesondere werden keine Leerzeichen entfernt.
#
# Uebergabeparameter:
#   element (ET.Element):
#       XML-Element, innerhalb dessen gesucht werden soll.
#
#   xpath (str):
#       Relativer XML-Pfad zum gesuchten Element.
#
# Rueckgabewert:
#   str:
#       Unveraenderter Textinhalt des XML-Elements.
#
# Exceptions:
#   ValueError:
#       Das gesuchte Element fehlt oder enthaelt keinen
#       nutzbaren Text.
# =============================================================================
def get_required_text(
    element: ET.Element,
    xpath: str
) -> str:

    # Gesuchtes XML-Element ermitteln.
    result = element.find(xpath)

    # Fehlende oder leere Elemente nicht akzeptieren.
    #
    # strip() wird ausschliesslich fuer die Pruefung verwendet.
    # Der eigentliche Textinhalt bleibt unveraendert.
    if (
        result is None
        or result.text is None
        or not result.text.strip()
    ):

        raise ValueError(
            f"Missing or empty XML element: {xpath}"
        )

    return result.text


# =============================================================================
# Wertet einen einzelnen Produktdatensatz aus.
#
# Aus dem XML-Element ProdSelDat werden der Produktname,
# der Auftragsname, die Produktgruppe und der Zeitpunkt
# der letzten Produktaenderung ausgelesen.
#
# Uebergabeparameter:
#   element (ET.Element):
#       XML-Element eines einzelnen Produktdatensatzes.
#
# Rueckgabewert:
#   dict:
#       Produktinformationen mit folgenden Schluesseln:
#
#       product_name (str):
#           Vollstaendiger, unveraenderter Produktname.
#
#       order (str):
#           Auftragsname ohne aeussere Leerzeichen.
#
#       product_group (str):
#           Produktgruppe ohne aeussere Leerzeichen.
#
#       product_update (str):
#           Original-Zeitstempel im Format YYYYMMDDHHMM.
#
#       product_update_datetime (datetime):
#           Konvertierter Zeitstempel fuer spaetere Vergleiche.
#
# Exceptions:
#   ValueError:
#       Ein erforderliches XML-Element fehlt oder der
#       Zeitstempel besitzt ein ungueltiges Format.
# =============================================================================
def parse_product(element: ET.Element) -> dict:

    # -------------------------------------------------------------------------
    # Produktname auslesen
    # -------------------------------------------------------------------------

    # Der Produktname dient als eindeutige Referenz fuer
    # spaetere Produktdetailanforderungen.
    #
    # WICHTIG:
    # Keine Leerzeichen entfernen!
    product_name = get_required_text(
        element,
        "ProductName"
    )

    # -------------------------------------------------------------------------
    # Auftragsname und Produktgruppe auslesen
    # -------------------------------------------------------------------------

    # Bei diesen Feldern werden aeussere Leerzeichen entfernt.
    # Die Werte dienen zunaechst ausschliesslich der Information.
    order = get_required_text(
        element,
        "Order"
    ).strip()

    product_group = get_required_text(
        element,
        "ProductGroup"
    ).strip()

    # -------------------------------------------------------------------------
    # Zeitpunkt der letzten Produktaenderung auslesen
    # -------------------------------------------------------------------------

    product_update = get_required_text(
        element,
        "ProdUpdate/DateTime"
    ).strip()

    # Beispiel:
    #   202611240905
    #
    # Bedeutung:
    #   2026-11-24 09:05
    #
    # Das Originalformat wird zusaetzlich gespeichert,
    # damit es spaeter fuer Vergleiche verwendet werden kann.
    if (
        len(product_update) != 12
        or not product_update.isascii()
        or not product_update.isdigit()
    ):
        raise ValueError(
            f"Invalid product update timestamp: {product_update}"
        )

    product_update_datetime = datetime.strptime(
        product_update,
        "%Y%m%d%H%M"
    )

    # -------------------------------------------------------------------------
    # Produktinformationen zusammenstellen
    # -------------------------------------------------------------------------

    product = {
        "product_name": product_name,
        "order": order,
        "product_group": product_group,
        "product_update": product_update,
        "product_update_datetime": product_update_datetime
    }

    logger.debug(
        "Product parsed: %r, update: %s",
        product_name,
        product_update
    )

    return product


# =============================================================================
# Liest und verarbeitet die vollstaendige XML-Produktliste.
#
# Alle enthaltenen ProdSelDat-Elemente werden einzeln
# ausgewertet und in einer Produktliste gespeichert.
#
# Eine gueltige Produktliste darf auch leer sein.
# Dies ist beispielsweise der Fall, wenn im angeforderten
# Produktionszeitraum keine Produkte vorhanden sind.
#
# Bei fehlerhaften Produktdatensaetzen wird die Verarbeitung
# abgebrochen. Dadurch wird verhindert, dass eine
# unvollstaendige Produktliste spaeter als gueltig gilt.
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#
# Rueckgabewert:
#   list[dict]:
#       Liste aller gefundenen Produktdatensaetze.
#       Eine leere Liste bedeutet, dass keine Produkte
#       vorhanden sind.
#
# Exceptions:
#   FileNotFoundError:
#       Die Antwortdatei ist nicht vorhanden.
#
#   ET.ParseError:
#       Die Antwortdatei enthaelt ungueltiges XML.
#
#   ValueError:
#       Die XML-Struktur oder ein Produktdatensatz
#       ist ungueltig.
#
#   KeyError:
#       Ein erforderlicher Konfigurationsparameter fehlt.
#
#   OSError:
#       Die Antwortdatei konnte nicht gelesen werden.
# =============================================================================
def parse_product_list(config: dict) -> list[dict]:

    # -------------------------------------------------------------------------
    # Antwortdatei ermitteln
    # -------------------------------------------------------------------------

    response_file = get_response_file(config)

    logger.info(
        "Reading product list: %s",
        response_file
    )

    # Vorhandensein der Antwortdatei pruefen.
    if not response_file.is_file():

        raise FileNotFoundError(
            f"Product list not found: {response_file}"
        )

    # -------------------------------------------------------------------------
    # XML-Datei einlesen
    # -------------------------------------------------------------------------

    # ElementTree liest die XML-Datei inklusive CDATA-Inhalten.
    #
    # Die externe DTD-Datei piiprdlst.dtd wird fuer diese
    # Art der XML-Auswertung nicht benoetigt.
    tree = ET.parse(response_file)

    root = tree.getroot()

    # Erwartetes XML-Wurzelelement ueberpruefen.
    if root.tag != "JDF":

        raise ValueError(
            f"Unexpected XML root element: {root.tag}"
        )

    # -------------------------------------------------------------------------
    # ProductList-Element ermitteln
    # -------------------------------------------------------------------------

    product_list_element = root.find(
        "./ResourcePool/Resource/ProductList"
    )

    if product_list_element is None:

        raise ValueError(
            "ProductList element not found in XML"
        )

    # -------------------------------------------------------------------------
    # Einzelne Produktdatensaetze ermitteln
    # -------------------------------------------------------------------------

    product_elements = product_list_element.findall(
        "./ProdSelDat"
    )

    logger.info(
        "Product records found: %d",
        len(product_elements)
    )

    # Liste fuer die ausgewerteten Produkte vorbereiten.
    products = []

    # -------------------------------------------------------------------------
    # Produktdatensaetze einzeln verarbeiten
    # -------------------------------------------------------------------------

    for index, element in enumerate(
        product_elements,
        start=1
    ):

        try:

            product = parse_product(element)

        except ValueError as error:

            # Fehlerhaften Datensatz eindeutig protokollieren
            # und die Verarbeitung der Produktliste abbrechen.
            logger.error(
                "Invalid product record %d: %s",
                index,
                error
            )

            raise

        # Gueltiges Produkt zur Ergebnisliste hinzufuegen.
        products.append(product)

        logger.info(
            "Product found: %r | Last update: %s",
            product["product_name"],
            product["product_update"]
        )

    # -------------------------------------------------------------------------
    # Ergebnis protokollieren
    # -------------------------------------------------------------------------

    logger.info(
        "Product list successfully parsed: %d products",
        len(products)
    )

    # Vollstaendige Produktliste an den Aufrufer zurueckgeben.
    return products
