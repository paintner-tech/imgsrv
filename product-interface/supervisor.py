# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: supervisor.py
#
# BESCHREIBUNG:
# -------------
# Zentraler Supervisor des Product Interface.
#
# Der Supervisor koordiniert die bereits vorhandenen Module zur
# Anforderung, Auswertung und Speicherung von Produktinformationen.
#
# ABLAUF:
# -------
#   1. Produktliste anfordern.
#   2. Auf PIF- und SYC-Datei warten.
#   3. Produktliste auswerten.
#   4. Antwortdateien entfernen.
#   5. Neue und geaenderte Produkte ermitteln.
#   6. Produktdetails einzeln anfordern.
#   7. Produktdetails auswerten.
#   8. Produktdaten als JSON speichern.
#   9. Antwortdateien entfernen.
#  10. Produktcache aktualisieren.
#  11. Bei erfolgreichem Zyklus optional alle aktiven Bildauftraege abgleichen.
#  12. Bis zum naechsten Abfragezyklus warten.
#
# Die Job-Erstellung wird ueber job_creation.enabled aktiviert.
# Der Supervisor verarbeitet keine Bilder und startet keine Worker.
# =============================================================================

import argparse
import copy
import fcntl
import json
import os
import tempfile
import time
import yaml

from pathlib import Path

from library.logger import setup_logging, get_logger

from request_sender import (
    send_product_list_request,
    get_product_detail_filenames,
    send_product_detail_request
)

from product_list_parser import (
    get_response_file,
    parse_product_list
)

from product_cache import (
    load_product_cache,
    get_changed_products,
    mark_product_processed
)

from product_details_parser import parse_product_details

from job_creator import create_jobs

from job_manager import (
    load_active_products,
    synchronize_jobs
)


# =============================================================================
# Globale Einstellungen
# =============================================================================

CONFIG_FILE = Path(
    "/pecom/imgsrv/config/product-interface.yaml"
)

logger = get_logger(__name__)


# =============================================================================
# FUNKTION: load_config
#
# Aufgabe:
#   Liest die YAML-Konfiguration des Product Interface ein.
#
# Uebergabeparameter:
#   filename: Path
#       Pfad zur Konfigurationsdatei.
#
# Rueckgabewert:
#   dict:
#       Vollstaendige Product-Interface-Konfiguration.
# =============================================================================
def load_config(filename: Path) -> dict:

    with filename.open("r", encoding="utf-8") as file:

        config = yaml.safe_load(file)

    if not isinstance(config, dict):

        raise ValueError("Invalid configuration")

    return config


# =============================================================================
# FUNKTION: create_directories
#
# Aufgabe:
#   Erstellt die fuer den Supervisor benoetigten Datenverzeichnisse,
#   sofern diese noch nicht existieren.
#
# Uebergabeparameter:
#   config: dict
#       Product-Interface-Konfiguration.
#
# Rueckgabewert:
#   None
# =============================================================================
def create_directories(config: dict) -> None:

    directories = [
        config["local"]["response_directory"],
        config["directories"]["templates"],
        config["directories"]["temporary"],
        config["directories"]["products"],
        config["directories"]["error"]
    ]

    for directory in directories:

        Path(directory).mkdir(
            parents=True,
            exist_ok=True
        )

    # Auch das Verzeichnis des Produktcaches sicherstellen.
    Path(
        config["product_cache"]["filename"]
    ).parent.mkdir(
        parents=True,
        exist_ok=True
    )


# =============================================================================
# FUNKTION: wait_for_response
#
# Aufgabe:
#   Wartet auf eine vollstaendige Antwort des Produktservers.
#
#   Eine Antwort gilt als eingegangen, wenn sowohl die PIF-Datei
#   als auch die zugehoerige SYC-Datei vorhanden sind.
#
# Uebergabeparameter:
#   config: dict
#       Product-Interface-Konfiguration.
#
#   response_file: Path
#       Erwartete PIF-Antwortdatei.
#
# Rueckgabewert:
#   bool:
#       True  = Antwort vollstaendig vorhanden.
#       False = Timeout erreicht.
# =============================================================================
def wait_for_response(
    config: dict,
    response_file: Path
) -> bool:

    sync_file = response_file.with_suffix(".syc")

    timeout = config["polling"][
        "response_timeout_seconds"
    ]

    interval = config["polling"].get(
        "response_poll_interval_seconds",
        1
    )

    if timeout <= 0 or interval <= 0:

        raise ValueError("Invalid polling configuration")

    deadline = time.monotonic() + timeout

    logger.info(
        "Waiting for response: %s",
        response_file.name
    )

    while True:

        # Beide Dateien muessen vorhanden sein.
        if (
            response_file.is_file()
            and sync_file.is_file()
        ):

            logger.info(
                "Response received: %s",
                response_file.name
            )

            return True

        remaining = deadline - time.monotonic()

        if remaining <= 0:

            break

        time.sleep(min(interval, remaining))

    logger.error(
        "Response timeout: %s",
        response_file.name
    )

    return False


# =============================================================================
# FUNKTION: remove_response_files
#
# Aufgabe:
#   Entfernt die Antwortdateien nach erfolgreicher Verarbeitung.
#
#   Die SYC-Datei wird bewusst zuletzt entfernt, damit der
#   Produktserver die Schnittstelle nicht vorzeitig freigibt.
#
# Uebergabeparameter:
#   response_file: Path
#       Pfad zur PIF-Antwortdatei.
#
# Rueckgabewert:
#   None
# =============================================================================
def remove_response_files(
    response_file: Path
) -> None:

    sync_file = response_file.with_suffix(".syc")

    # Zuerst die eigentliche Datendatei entfernen.
    response_file.unlink(missing_ok=True)

    # Anschliessend die Synchronisationsdatei entfernen.
    sync_file.unlink(missing_ok=True)

    logger.info(
        "Response files removed: %s / %s",
        response_file.name,
        sync_file.name
    )


# =============================================================================
# FUNKTION: archive_response_files
#
# Aufgabe:
#   Verschiebt vorhandene Antwortdateien in das Fehlerverzeichnis.
#
#   Dadurch bleiben fehlerhafte oder alte Antworten fuer eine
#   spaetere Analyse erhalten.
#
# Uebergabeparameter:
#   config: dict
#       Product-Interface-Konfiguration.
#
#   response_file: Path
#       Pfad zur PIF-Antwortdatei.
#
# Rueckgabewert:
#   None
# =============================================================================
def archive_response_files(
    config: dict,
    response_file: Path
) -> None:

    sync_file = response_file.with_suffix(".syc")

    if not response_file.exists() and not sync_file.exists():

        return

    error_directory = Path(
        config["directories"]["error"]
    )

    # Eindeutiges Unterverzeichnis fuer jeden Fehlerfall.
    # Dadurch werden fruehere Fehlerdateien nicht ueberschrieben.
    archive_directory = Path(
        tempfile.mkdtemp(
            prefix="response_",
            dir=error_directory
        )
    )

    # Auch beim Verschieben wird die SYC-Datei zuletzt behandelt.
    for filename in (response_file, sync_file):

        if filename.exists():

            filename.rename(
                archive_directory / filename.name
            )

            logger.warning(
                "Response file archived: %s",
                filename.name
            )


# =============================================================================
# FUNKTION: save_product_data
#
# Aufgabe:
#   Speichert die vollstaendig ausgewerteten Produktdaten als JSON.
#
#   Die Datei wird zuerst temporaer geschrieben und anschliessend
#   atomar umbenannt. Andere Module koennen dadurch niemals eine
#   nur teilweise geschriebene Produktdatei einlesen.
#
# Uebergabeparameter:
#   config: dict
#       Product-Interface-Konfiguration.
#
#   product: dict
#       Produktinformationen aus der Produktliste.
#
#   details: dict
#       Ausgewertete Produktdetails.
#
#   response_file: Path
#       Urspruenglicher Name der Produktantwort.
#
# Rueckgabewert:
#   Path:
#       Pfad zur gespeicherten JSON-Datei.
# =============================================================================
def save_product_data(
    config: dict,
    product: dict,
    details: dict,
    response_file: Path
) -> Path:

    product_directory = Path(
        config["directories"]["products"]
    )

    response_prefix = config["product_details"][
        "response_prefix"
    ]

    # Produktdateiname aus dem bereits validierten
    # Namen der Antwortdatei ableiten.
    product_filename = response_file.stem.removeprefix(
        response_prefix
    )

    if not product_filename:

        raise ValueError("Invalid product filename")

    output_file = product_directory / (
        product_filename + ".json"
    )

    # Verhindert eine versehentliche Ueberschreibung
    # des konfigurierten Produktcaches.
    if output_file.resolve() == Path(
        config["product_cache"]["filename"]
    ).resolve():

        raise ValueError("Product filename conflicts with cache")

    # Alle Parserinformationen uebernehmen.
    # Zusaetzlich den Aenderungszeitpunkt aus der Produktliste
    # innerhalb der Produktdatei speichern.
    product_data = dict(details)

    product_data["product_update"] = product[
        "product_update"
    ]

    temporary_file = None

    try:

        # Temporaere Datei im selben Verzeichnis erzeugen.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=product_directory,
            prefix=".product_",
            suffix=".tmp",
            delete=False
        ) as file:

            temporary_file = Path(file.name)

            json.dump(
                product_data,
                file,
                indent=4,
                ensure_ascii=False
            )

            file.write("\n")

            # Geschriebene Daten an das Betriebssystem uebergeben.
            file.flush()

            os.fsync(file.fileno())

        # Vorhandene Produktdatei atomar ersetzen.
        os.replace(
            temporary_file,
            output_file
        )

    finally:

        # Bei einem Schreibfehler temporaere Datei entfernen.
        if temporary_file is not None:

            temporary_file.unlink(missing_ok=True)

    logger.info(
        "Product data saved: %s",
        output_file
    )

    return output_file


# =============================================================================
# FUNKTION: request_product_list
#
# Aufgabe:
#   Fordert die aktuelle Produktliste an, wartet auf die Antwort
#   und wertet diese aus.
#
# Uebergabeparameter:
#   config: dict
#       Product-Interface-Konfiguration.
#
# Rueckgabewert:
#   list[dict]:
#       Liste aller empfangenen Produkte.
# =============================================================================
def request_product_list(
    config: dict
) -> list[dict]:

    response_file = get_response_file(config)

    # Alte Antworten archivieren, da sie eine neue
    # Uebertragung durch den Produktserver blockieren koennen.
    archive_response_files(
        config,
        response_file
    )

    logger.info("Requesting product list")

    if not send_product_list_request(config):

        raise RuntimeError(
            "Product list request failed"
        )

    if not wait_for_response(
        config,
        response_file
    ):

        raise TimeoutError(
            "Product list response timeout"
        )

    # Fehlerhafte Antwort fuer spaetere Analyse archivieren.
    try:

        products = parse_product_list(config)

    except Exception:

        archive_response_files(
            config,
            response_file
        )

        raise

    logger.info(
        "Product list received: %d products",
        len(products)
    )

    # Die Produktliste befindet sich jetzt im Arbeitsspeicher.
    remove_response_files(response_file)

    return products


# =============================================================================
# FUNKTION: process_product
#
# Aufgabe:
#   Fordert die Details eines einzelnen Produktes an,
#   wertet die Antwort aus und speichert die Produktdaten.
#
#   Der Produktcache wird ausschliesslich nach erfolgreicher
#   Speicherung der Produktdaten aktualisiert.
#
# Uebergabeparameter:
#   config: dict
#       Product-Interface-Konfiguration.
#
#   cache: dict
#       Aktuell geladener Produktcache.
#
#   product: dict
#       Produktinformationen aus der Produktliste.
#
# Rueckgabewert:
#   bool:
#       True  = Produkt erfolgreich verarbeitet.
#       False = Fehler bei der Verarbeitung.
# =============================================================================
def process_product(
    config: dict,
    cache: dict,
    product: dict
) -> bool:

    product_name = product["product_name"]

    logger.info(
        "Processing product: %r",
        product_name
    )

    try:

        # Erwarteten Antwortdateinamen ueber das vorhandene
        # Request-Sender-Modul ermitteln.
        _, response_filename = (
            get_product_detail_filenames(
                config,
                product_name
            )
        )

        response_file = Path(
            config["local"]["response_directory"]
        ) / response_filename

        # Eventuell vorhandene alte Antworten archivieren.
        archive_response_files(
            config,
            response_file
        )

        # Produktdetails anfordern.
        if not send_product_detail_request(
            config,
            product
        ):

            raise RuntimeError(
                "Product detail request failed"
            )

        # Auf die vollstaendige Antwort warten.
        if not wait_for_response(
            config,
            response_file
        ):

            raise TimeoutError(
                "Product detail response timeout"
            )

        # Produktdetails auswerten.
        try:

            details = parse_product_details(
                response_file
            )

            # Eine Einzelanforderung darf nur genau das
            # angeforderte Produkt enthalten.
            if (
                len(details) != 1
                or details[0]["product_name"] != product_name
            ):

                raise ValueError(
                    "Unexpected product in response"
                )

        except Exception:

            archive_response_files(
                config,
                response_file
            )

            raise

        # Vollstaendige Produktdaten dauerhaft speichern.
        save_product_data(
            config,
            product,
            details[0],
            response_file
        )

        # Empfangsdateien erst nach erfolgreicher Speicherung
        # entfernen.
        remove_response_files(response_file)

        # Cache zunaechst auf einer Kopie aktualisieren.
        # Ein Schreibfehler darf den aktiven Cache nicht
        # faelschlicherweise als erfolgreich markieren.
        updated_cache = copy.deepcopy(cache)

        mark_product_processed(
            config,
            updated_cache,
            product
        )

        cache.clear()
        cache.update(updated_cache)

        logger.info(
            "Product successfully processed: %r",
            product_name
        )

        return True

    except Exception:

        logger.exception(
            "Product processing failed: %r",
            product_name
        )

        return False


# =============================================================================
# FUNKTION: synchronize_product_jobs
#
# Aufgabe:
#   Ermittelt den vollstaendigen Sollbestand der Bildauftraege anhand
#   der aktuellen Produktliste und gleicht ihn mit der Queue ab.
#   Auch unveraenderte Produkte muessen beruecksichtigt werden.
#   Der Abgleich erfolgt erst, wenn alle Produktdaten aktuell sind.
#
# Uebergabeparameter:
#   config: dict
#       Product-Interface-Konfiguration inklusive job_creation.
#
#   products: list[dict]
#       Vollstaendige aktuelle Produktliste des Produktservers.
#
# Rueckgabewert:
#   None
#
# Fehler:
#   Ausnahmen werden an run_cycle weitergereicht, damit der Zyklus
#   als fehlgeschlagen gewertet und der Abgleich erneut versucht wird.
# =============================================================================
def synchronize_product_jobs(
    config: dict,
    products: list[dict]
) -> None:

    logger.info(
        "Starting job synchronization for %d active products",
        len(products)
    )

    # Nur Produkte aus der aktuellen Produktliste laden. Alte JSON-Dateien
    # bleiben gespeichert, werden aber nicht in den Sollbestand aufgenommen.
    # Fehlende oder veraltete Daten verhindern den gesamten Queue-Abgleich.
    active_products = load_active_products(config, products)

    # Alle Bildrezepte gemeinsam erzeugen, damit gleiche Rezepte auch
    # produktuebergreifend zusammengefasst werden koennen.
    jobs = create_jobs(active_products)

    logger.info(
        "Job recipes created: products=%d, jobs=%d, composites=%d",
        len(active_products),
        len(jobs),
        sum(len(job["composites"]) for job in jobs)
    )

    # Der Job Manager verwaltet Manifest, Revisionen und Prozesssperre.
    # Keine Queue-Datei direkt im Supervisor schreiben oder entfernen.
    # Auch eine gueltige leere Produktliste wird abgeglichen, damit keine
    # veralteten Auftraege in pending verbleiben.
    synchronize_jobs(config, jobs)


# =============================================================================
# FUNKTION: run_cycle
#
# Aufgabe:
#   Fuehrt einen vollstaendigen Produktabfragezyklus durch.
#   Bei aktiviertem job_creation erfolgt anschliessend der Job-Abgleich,
#   auch wenn keine Produktdetails neu angefordert werden mussten.
#
# Uebergabeparameter:
#   config: dict
#       Product-Interface-Konfiguration.
#
#   force: bool
#       True = Alle Produkte erneut anfordern.
#
# Rueckgabewert:
#   bool:
#       True  = Abfragezyklus erfolgreich abgeschlossen.
#       False = Mindestens ein Fehler aufgetreten.
# =============================================================================
def run_cycle(
    config: dict,
    force: bool = False
) -> bool:

    logger.info(
        "============================================================"
    )

    logger.info(
        "Starting product update cycle"
    )

    # Aktuelle Produktliste anfordern.
    products = request_product_list(config)

    # Aktuellen Produktcache einlesen.
    cache = load_product_cache(config)

    # Dateinamen aller Produkte auf Kollisionen pruefen.
    # Unterschiedliche Produktnamen koennen nach dem
    # Entfernen der Leerzeichen identische Namen ergeben.
    used_filenames = set()

    for product in products:

        _, response_filename = (
            get_product_detail_filenames(
                config,
                product["product_name"]
            )
        )

        if response_filename in used_filenames:

            raise ValueError(
                f"Filename collision: {response_filename}"
            )

        used_filenames.add(response_filename)

    # Neue und geaenderte Produkte ermitteln.
    if force:

        changed_products = products

        logger.info(
            "Forced update of all products"
        )

    else:

        changed_products = get_changed_products(
            products,
            cache
        )

    logger.info(
        "Products requiring update: %d of %d",
        len(changed_products),
        len(products)
    )

    successful = 0
    failed = 0

    # Alle erforderlichen Produktdetails nacheinander anfordern.
    for product in changed_products:

        if process_product(
            config,
            cache,
            product
        ):

            successful += 1

        else:

            failed += 1

    logger.info(
        "Product update cycle completed: "
        "total=%d, successful=%d, failed=%d",
        len(changed_products),
        successful,
        failed
    )

    # Job-Erstellung ist optional. Eine fehlende Sektion bedeutet, dass
    # die bisherige reine Produktverarbeitung unveraendert weiterlaeuft.
    enabled = config.get("job_creation", {}).get("enabled", False)

    if not isinstance(enabled, bool):
        raise ValueError("job_creation.enabled must be a boolean")

    if not enabled:

        logger.debug("Job creation disabled")
        return failed == 0

    # Bei einem Fehler darf kein unvollstaendiger oder veralteter Sollbestand
    # an den Job Manager uebergeben werden. Das bisherige Manifest bleibt
    # erhalten. Beim naechsten Zyklus wird die Produktabfrage wiederholt.
    if failed:

        logger.warning(
            "Job synchronization skipped: %d product updates failed",
            failed
        )
        return False

    # Der Abgleich wird bewusst auch bei changed_products == [] ausgefuehrt.
    # So werden fehlende Queue-Dateien wiederhergestellt und Produkte, die
    # nicht mehr in der aktuellen Produktliste stehen, beruecksichtigt.
    try:

        synchronize_product_jobs(config, products)

    except Exception:

        # Produktcache nicht zuruecksetzen: Die Produktdaten wurden bereits
        # erfolgreich gespeichert. Der Job-Abgleich wird separat wiederholt.
        logger.exception("Job synchronization failed")
        return False

    return True


# =============================================================================
# FUNKTION: main
#
# Aufgabe:
#   Initialisiert den Supervisor und fuehrt die zyklische
#   Produktabfrage aus.
#
# Uebergabeparameter:
#   Keine.
#
# Rueckgabewert:
#   int:
#       0 = Erfolgreich beendet.
#       1 = Fehler aufgetreten.
# =============================================================================
def main() -> int:

    parser = argparse.ArgumentParser(
        description="ImageServer Product Interface Supervisor"
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Execute one product update cycle"
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Request all products regardless of cache"
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_FILE,
        help="Path to configuration file"
    )

    args = parser.parse_args()

    try:

        # Konfiguration und Logging initialisieren.
        config = load_config(args.config)

        setup_logging(config)

        create_directories(config)

        interval = config["polling"][
            "interval_seconds"
        ]

        if interval <= 0:

            raise ValueError(
                "Invalid polling interval"
            )

        # Verhindert, dass zwei Supervisor-Instanzen
        # gleichzeitig dieselben Antwortdateien bearbeiten.
        lock_file = Path(
            config["directories"]["temporary"]
        ) / "supervisor.lock"

        with lock_file.open("a") as lock:

            try:

                fcntl.flock(
                    lock.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB
                )

            except BlockingIOError:

                logger.error(
                    "Another supervisor instance is running"
                )

                return 1

            logger.info(
                "Product Interface Supervisor started"
            )

            logger.info(
                "Polling interval: %d seconds",
                interval
            )

            while True:

                try:

                    successful = run_cycle(
                        config,
                        args.force
                    )

                except Exception:

                    logger.exception(
                        "Product update cycle failed"
                    )

                    successful = False

                # Im Testbetrieb nach einem Zyklus beenden.
                if args.once:

                    return 0 if successful else 1

                logger.info(
                    "Waiting %d seconds for next cycle",
                    interval
                )

                time.sleep(interval)

    except KeyboardInterrupt:

        logger.info(
            "Product Interface Supervisor stopped"
        )

        return 0

    except Exception:

        logger.exception(
            "Supervisor initialization failed"
        )

        return 1


# =============================================================================
# Programmeinstieg
# =============================================================================

if __name__ == "__main__":

    raise SystemExit(main())

