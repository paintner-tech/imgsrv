# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: product_cache.py
#
# BESCHREIBUNG:
# -------------
# Dieses Modul verwaltet den zuletzt erfolgreich verarbeiteten
# Stand der einzelnen Produkte.
#
# FUNKTIONSUMFANG:
# ----------------
#   1. Gespeicherten Produktcache einlesen.
#   2. Neue Produkte erkennen.
#   3. Geaenderte Produkte erkennen.
#   4. Unveraenderte Produkte von der Abfrage ausschliessen.
#   5. Erfolgreich verarbeitete Produkte im Cache aktualisieren.
#   6. Produktcache dauerhaft als JSON-Datei speichern.
#
# WICHTIG:
# --------
# Der vollstaendige Produktname wird als Schluessel verwendet.
# Alle darin enthaltenen Leerzeichen bleiben erhalten.
#
# Ein Produkt darf erst dann im Cache aktualisiert werden,
# wenn die zugehoerigen Produktdetails vollstaendig empfangen,
# erfolgreich ausgewertet und dauerhaft gespeichert wurden.
#
# Das erfolgreiche Versenden einer Anforderung allein reicht
# NICHT aus, um ein Produkt als verarbeitet zu markieren.
#
# Dieses Modul versendet selbst keine Produktanforderungen.
# =============================================================================

import json
import os

from pathlib import Path
from tempfile import NamedTemporaryFile

from library.logger import get_logger


# Modulbezogenen Logger des zentralen Logging-Moduls beziehen.
logger = get_logger(__name__)


# =============================================================================
# Ermittelt den vollstaendigen Pfad zur Produktcache-Datei.
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#
# Rueckgabewert:
#   Path:
#       Vollstaendiger Pfad zur Produktcache-Datei.
#
# Exceptions:
#   KeyError:
#       Erforderlicher Konfigurationsparameter fehlt.
#
#   TypeError:
#       Konfigurationsparameter besitzt einen ungueltigen Datentyp.
# =============================================================================
def get_cache_file(config: dict) -> Path:

    return Path(
        config["product_cache"]["filename"]
    )


# =============================================================================
# Liest den zuletzt gespeicherten Produktcache ein.
#
# Falls noch keine Cache-Datei vorhanden ist, wird ein leerer
# Cache zurueckgegeben. Dadurch werden beim ersten Start
# saemtliche Produkte als neu erkannt.
#
# Eine beschaedigte Cache-Datei wird nicht automatisch
# ueberschrieben. Stattdessen wird ein Fehler ausgeloest.
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#
# Rueckgabewert:
#   dict:
#       Gespeicherte Produkte mit ihren Aenderungszeitstempeln.
#
# Exceptions:
#   OSError:
#       Cache-Datei konnte nicht gelesen werden.
#
#   json.JSONDecodeError:
#       Cache-Datei enthaelt ungueltiges JSON.
#
#   ValueError:
#       Cache-Datei besitzt eine ungueltige Struktur.
# =============================================================================
def load_product_cache(config: dict) -> dict:

    cache_file = get_cache_file(config)

    # Beim ersten Programmstart existiert noch kein Cache.
    if not cache_file.exists():

        logger.info(
            "No existing product cache found"
        )

        return {}

    logger.info(
        "Loading product cache: %s",
        cache_file
    )

    # Vorhandenen Produktcache einlesen.
    with cache_file.open(
        "r",
        encoding="utf-8"
    ) as file:

        cache = json.load(file)

    # Grundstruktur des Caches ueberpruefen.
    if not isinstance(cache, dict):

        raise ValueError(
            "Invalid product cache structure"
        )

    # Alle gespeicherten Produktdatensaetze ueberpruefen.
    for product_name, product_update in cache.items():

        if (
            not isinstance(product_name, str)
            or not product_name.strip()
            or not isinstance(product_update, str)
            or len(product_update) != 12
            or not product_update.isascii()
            or not product_update.isdigit()
        ):

            raise ValueError(
                f"Invalid cache entry: {product_name!r}"
            )

    logger.info(
        "Product cache loaded: %d products",
        len(cache)
    )

    return cache


# =============================================================================
# Ermittelt alle neuen oder geaenderten Produkte.
#
# Die aktuelle Produktliste wird mit dem zuletzt erfolgreich
# verarbeiteten Produktstand verglichen.
#
# Ein Produkt wird zurueckgegeben, wenn:
#
#   - Der Produktname noch nicht im Cache vorhanden ist.
#   - Der Aenderungszeitstempel vom gespeicherten Wert abweicht.
#
# Der Produktcache wird durch diese Funktion nicht veraendert.
#
# Uebergabeparameter:
#   products (list[dict]):
#       Aktuelle Produktliste des Produktservers.
#
#   cache (dict):
#       Zuletzt erfolgreich verarbeiteter Produktstand.
#
# Rueckgabewert:
#   list[dict]:
#       Liste aller neuen oder geaenderten Produkte.
#
# Exceptions:
#   KeyError:
#       Erforderliche Produktinformationen fehlen.
#
#   ValueError:
#       Ein Produktname ist mehrfach vorhanden.
# =============================================================================
def get_changed_products(
    products: list[dict],
    cache: dict
) -> list[dict]:

    changed_products = []

    # Zaehler fuer die spaetere Zusammenfassung.
    new_count = 0
    changed_count = 0
    unchanged_count = 0

    # Doppelte Produktnamen innerhalb einer Produktliste erkennen.
    seen_products = set()

    # Alle Produkte der aktuellen Produktliste ueberpruefen.
    for product in products:

        # Produktname inklusive saemtlicher Leerzeichen uebernehmen.
        product_name = product["product_name"]

        # Aktuellen Aenderungszeitstempel ermitteln.
        product_update = product["product_update"]

        # Doppelte Produktnamen nicht akzeptieren, da ansonsten
        # keine eindeutige Zuordnung zum Cache moeglich ist.
        if product_name in seen_products:

            raise ValueError(
                f"Duplicate product name: {product_name!r}"
            )

        seen_products.add(product_name)

        # -------------------------------------------------------------
        # Neues Produkt
        # -------------------------------------------------------------

        if product_name not in cache:

            logger.info(
                "New product detected: %r",
                product_name
            )

            changed_products.append(product)
            new_count += 1

            continue

        # -------------------------------------------------------------
        # Geaendertes Produkt
        # -------------------------------------------------------------

        # Gespeicherten Zeitstempel aus dem Cache auslesen.
        cached_update = cache[product_name]

        # Auf Abweichung und nicht ausschliesslich auf einen
        # neueren Zeitstempel pruefen.
        #
        # Dadurch werden auch Produkte erneut angefordert,
        # deren Zeitstempel beispielsweise durch einen Restore
        # auf einen aelteren Stand zurueckgesetzt wurde.
        if product_update != cached_update:

            logger.info(
                "Changed product detected: %r | %s -> %s",
                product_name,
                cached_update,
                product_update
            )

            changed_products.append(product)
            changed_count += 1

            continue

        # -------------------------------------------------------------
        # Unveraendertes Produkt
        # -------------------------------------------------------------

        logger.debug(
            "Product unchanged: %r",
            product_name
        )

        unchanged_count += 1

    # Zusammenfassung der Produktpruefung protokollieren.
    logger.info(
        "Product check completed: "
        "total=%d, new=%d, changed=%d, unchanged=%d",
        len(products),
        new_count,
        changed_count,
        unchanged_count
    )

    return changed_products


# =============================================================================
# Speichert den aktuellen Produktcache dauerhaft als JSON-Datei.
#
# Die Daten werden zunaechst in eine temporaere Datei im selben
# Verzeichnis geschrieben. Anschliessend wird die bisherige
# Cache-Datei atomar ersetzt.
#
# Dadurch wird verhindert, dass bei einem Programmabbruch
# waehrend des Schreibens eine unvollstaendige JSON-Datei
# als gueltiger Produktcache zurueckbleibt.
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#
#   cache (dict):
#       Zu speichernder Produktcache.
#
# Rueckgabewert:
#   None
#
# Exceptions:
#   OSError:
#       Cache-Datei konnte nicht geschrieben oder ersetzt werden.
#
#   TypeError:
#       Cache enthaelt nicht serialisierbare Daten.
# =============================================================================
def save_product_cache(
    config: dict,
    cache: dict
) -> None:

    cache_file = get_cache_file(config)

    # Zielverzeichnis bei Bedarf automatisch erstellen.
    cache_file.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp_file = None

    try:

        # Temporaere Datei im selben Verzeichnis erzeugen.
        # Dadurch kann os.replace() die Datei atomar ersetzen.
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_file.parent,
            prefix=".product-cache-",
            suffix=".tmp",
            delete=False
        ) as file:

            temp_file = Path(file.name)

            # Produktcache als lesbare JSON-Datei schreiben.
            json.dump(
                cache,
                file,
                indent=4,
                ensure_ascii=False,
                sort_keys=True
            )

            file.write("\n")

            # Gepufferte Daten an das Betriebssystem uebergeben.
            file.flush()

            # Dateiinhalte auf den Datentraeger schreiben.
            os.fsync(file.fileno())

        # Bestehende Cache-Datei atomar ersetzen.
        os.replace(
            temp_file,
            cache_file
        )

        logger.debug(
            "Product cache saved: %d products",
            len(cache)
        )

    finally:

        # Bei einem Fehler eventuell verbliebene temporaere
        # Datei entfernen.
        if temp_file is not None:

            temp_file.unlink(missing_ok=True)


# =============================================================================
# Markiert ein Produkt als erfolgreich verarbeitet.
#
# Diese Funktion darf erst nach erfolgreichem Empfang,
# Auswertung und Speicherung der Produktdetails aufgerufen
# werden.
#
# Der Aenderungszeitstempel wird dauerhaft gespeichert.
# Falls das Speichern fehlschlaegt, bleibt der bisherige
# Cache im Arbeitsspeicher unveraendert.
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#
#   cache (dict):
#       Aktueller Produktcache.
#
#   product (dict):
#       Erfolgreich verarbeitetes Produkt.
#
# Rueckgabewert:
#   None
#
# Exceptions:
#   KeyError:
#       Erforderliche Produktinformationen fehlen.
#
#   OSError:
#       Produktcache konnte nicht gespeichert werden.
# =============================================================================
def mark_product_processed(
    config: dict,
    cache: dict,
    product: dict
) -> None:

    product_name = product["product_name"]
    product_update = product["product_update"]

    # Kopie erstellen, damit der aktuelle Cache bei einem
    # Fehler waehrend des Speicherns unveraendert bleibt.
    updated_cache = cache.copy()

    # Neuen erfolgreich verarbeiteten Produktstand eintragen.
    updated_cache[product_name] = product_update

    # Zuerst den aktualisierten Cache dauerhaft speichern.
    save_product_cache(
        config,
        updated_cache
    )

    # Erst nach erfolgreicher Speicherung den Cache
    # im Arbeitsspeicher aktualisieren.
    cache.update(updated_cache)

    logger.info(
        "Product marked as processed: %r | Update: %s",
        product_name,
        product_update
    )
