# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: request_sender.py
#
# BESCHREIBUNG:
# -------------
# Dieses Modul ist für die Erstellung und Übertragung von Anforderungen
# an den Produktserver zuständig.
#
# FUNKTIONSUMFANG:
# ----------------
#   1. XML-Vorlage aus dem Verzeichnis "out" einlesen.
#   2. Verbindungsdaten des Imageservers in die XML-Datei eintragen.
#   3. Abfragezeitraum anhand der Konfiguration berechnen.
#   4. Angepasste PIF-Datei im temporären Verzeichnis speichern.
#   5. Leere SYC-Datei mit identischem Dateinamen erzeugen.
#   6. PIF-Datei per SCP an den Produktserver übertragen.
#   7. Nach erfolgreicher Übertragung die SYC-Datei übertragen.
#
# WICHTIG:
# --------
# Die SYC-Datei signalisiert dem Produktserver, dass die zugehörige
# PIF-Datei vollständig übertragen wurde und verarbeitet werden kann.
#
# Die SYC-Datei darf deshalb erst nach erfolgreicher Übertragung
# der PIF-Datei gesendet werden.
#
# Die Originalvorlage im Verzeichnis "out" wird nicht verändert.
#
# Die SSH-Kommunikation erfolgt ohne Passwortabfrage.
# Sämtliche Einstellungen werden über die Konfiguration vorgegeben.
#
# Die Auswertung der Antwort des Produktservers ist nicht Bestandteil
# dieses Moduls.
# =============================================================================

import re
import logging
import subprocess
import xml.etree.ElementTree as ET

from pathlib import Path
from datetime import date, timedelta
from library.logger import get_logger
from xml.dom import minidom

# Logger des Moduls initialisieren.
# Die zentrale Konfiguration des Loggings erfolgt später
# im Hauptprogramm product_interface.py.
logger = logging.getLogger(__name__)


# =============================================================================
# Erstellt eine Produktlistenanforderung anhand der XML-Vorlage.
#
# Die Originalvorlage wird eingelesen und die darin enthaltenen
# Verbindungsdaten sowie der Produktionszeitraum werden durch
# die Werte aus der Konfiguration ersetzt.
#
# Anschließend werden die PIF-Datei und die zugehörige leere
# SYC-Datei im temporären Verzeichnis abgelegt.
#
# Übergabeparameter:
#   config (dict):
#       Vollständige Konfiguration des Product Interface.
#
# Rückgabewert:
#   tuple[Path, Path]:
#       Pfad zur erzeugten PIF-Datei.
#       Pfad zur erzeugten SYC-Datei.
#
# Exceptions:
#   FileNotFoundError:
#       Die XML-Vorlage wurde nicht gefunden.
#
#   ET.ParseError:
#       Die XML-Vorlage enthält ungültiges XML.
#
#   ValueError:
#       Ein erforderliches XML-Element fehlt oder ein Wert ist ungültig.
#
#   KeyError:
#       Ein erforderlicher Konfigurationsparameter fehlt.
#
#   OSError:
#       Fehler beim Lesen oder Schreiben einer Datei.
# =============================================================================
def create_product_list_request(config: dict) -> tuple[Path, Path]:

    # Benötigte Konfigurationsbereiche auslesen.
    local = config["local"]
    product = config["product_list"]
    directories = config["directories"]

    # Verzeichnisse für Originalvorlagen und temporäre Dateien.
    template_dir = Path(directories["templates"])
    temp_dir = Path(directories["temporary"])

    # Temporäres Verzeichnis automatisch anlegen,
    # falls es noch nicht vorhanden ist.
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Vollständige Dateipfade für Vorlage und Anforderung erzeugen.
    template_file = template_dir / product["request_file"]
    request_file = temp_dir / product["request_file"]

    # Dateinamen der SYC-Datei direkt aus der PIF-Datei ableiten.
    # Dadurch haben beide Dateien garantiert denselben Basisnamen.
    #
    # Beispiel:
    #   pii000.pif -> pii000.syc
    sync_file = request_file.with_suffix(".syc")

    logger.debug("Reading XML template: %s", template_file)

    # -------------------------------------------------------------------------
    # XML-Vorlage einlesen
    # -------------------------------------------------------------------------

    # XML-Dokument einlesen und dessen Wurzelelement ermitteln.
    # Die Originalvorlage wird dabei nicht verändert.
    tree = ET.parse(template_file)
    root = tree.getroot()

    # -------------------------------------------------------------------------
    # Produktionszeitraum berechnen
    # -------------------------------------------------------------------------

    # Aktuelles lokales Datum des Imageservers ermitteln.
    today = date.today()

    # Anzahl der Tage aus der Konfiguration auslesen.
    days_backward = product["days_backward"]
    days_forward = product["days_forward"]

    # Negative Zeitangaben verhindern.
    if days_backward < 0 or days_forward < 0:
        raise ValueError(
            "Production period must not contain negative values"
        )

    # Frühestes Produktionsdatum berechnen.
    min_date = today - timedelta(days=days_backward)

    # Spätestes Produktionsdatum berechnen.
    max_date = today + timedelta(days=days_forward)

    logger.info(
        "Requested production period: %s - %s",
        min_date.strftime("%Y-%m-%d"),
        max_date.strftime("%Y-%m-%d")
    )

    # -------------------------------------------------------------------------
    # Zielpfad für die Antwort des Produktservers erzeugen
    # -------------------------------------------------------------------------

    # Der Produktserver verwendet diesen Pfad, um die Antwort
    # auf dem Imageserver abzulegen.
    #
    # Beispiel:
    # /pecom/frf/ppi/imageserver_V2/pecom/in/piiprdlst
    #response_file = (
    #    Path(local["response_directory"])
    #    / product["response_file"]
    #)

    # Vollstaendigen Pfad der erwarteten Antwortdatei erzeugen.
    response_file = (
        Path(local["response_directory"])
        / product["response_file"]
    )

    # Pruefen, ob die konfigurierte Antwortdatei die
    # erforderliche Dateiendung .pif besitzt.
    if response_file.suffix != ".pif":
        raise ValueError(
            "Product list response filename must end with .pif"
        )

    # Das PII-Interface erwartet in TargetFileName einen
    # Dateinamen ohne Dateiendung.
    #
    # Der Produktserver ergaenzt bei der Rueckuebertragung
    # automatisch die Endungen .pif und .syc.
    target_filename = response_file.with_suffix("")

    # -------------------------------------------------------------------------
    # Neue XML-Werte definieren
    # -------------------------------------------------------------------------

    # Zuordnung der XML-Elemente zu den einzutragenden Werten.
    #
    # Die Elemente werden gezielt anhand ihres XML-Pfades gesucht.
    # Dadurch müssen keine fest vorgegebenen IP-Adressen oder
    # Zeichenketten innerhalb der Vorlage gesucht werden.
    values = {
        ".//DataRequest/InternetAddress":
            str(local["ip"]),

        ".//DataRequest/UserName":
            str(local["username"]),

        ".//DataRequest/TargetFileName":
            str(target_filename),

        ".//MinPlannedProdBegin/DateTime":
            min_date.strftime("%Y%m%d"),

        ".//MaxPlannedProdBegin/DateTime":
            max_date.strftime("%Y%m%d"),
    }

    # -------------------------------------------------------------------------
    # XML-Elemente aktualisieren
    # -------------------------------------------------------------------------

    for xpath, value in values.items():

        # Entsprechendes Element innerhalb des XML-Dokuments suchen.
        element = root.find(xpath)

        # Falls ein erforderliches Element fehlt, darf keine
        # unvollständige Anforderung versendet werden.
        if element is None:
            raise ValueError(
                f"Missing XML element: {xpath}"
            )

        # Vorhandenen Wert durch den neuen Wert ersetzen.
        element.text = value

        logger.debug(
            "XML element updated: %s = %s",
            xpath,
            value
        )

    # -------------------------------------------------------------------------
    # PIF-Datei erzeugen
    # -------------------------------------------------------------------------

    # Angepasstes XML-Dokument im temporären Verzeichnis speichern.
    # Die ursprüngliche Vorlage im Verzeichnis "out" bleibt erhalten.
    tree.write(
        request_file,
        encoding="UTF-8",
        xml_declaration=True
    )

    logger.info(
        "Request file created: %s",
        request_file
    )

    # -------------------------------------------------------------------------
    # SYC-Datei erzeugen
    # -------------------------------------------------------------------------

    # Eine vollständig leere SYC-Datei erzeugen.
    #
    # write_bytes() stellt im Gegensatz zu touch() sicher,
    # dass eine eventuell vorhandene Datei tatsächlich geleert wird.
    sync_file.write_bytes(b"")

    logger.info(
        "Synchronization file created: %s",
        sync_file
    )

    # Pfade beider erzeugten Dateien zurückgeben.
    return request_file, sync_file


# =============================================================================
# Überträgt eine Datei per SCP an den Produktserver.
#
# Die Verbindung erfolgt über SSH ohne interaktive Passwortabfrage.
# IP-Adresse, Benutzername, Zielverzeichnis, SSH-Port und Timeouts
# werden aus der Konfiguration ausgelesen.
#
# Übergabeparameter:
#   filename (Path):
#       Vollständiger Pfad zur lokal gespeicherten Datei.
#
#   config (dict):
#       Vollständige Konfiguration des Product Interface.
#
# Rückgabewert:
#   None:
#       Bei erfolgreicher Übertragung erfolgt keine Rückgabe.
#       Fehler werden als Exception weitergegeben.
#
# Exceptions:
#   subprocess.CalledProcessError:
#       SCP wurde mit einem Fehlercode beendet.
#
#   subprocess.TimeoutExpired:
#       Maximale Übertragungszeit wurde überschritten.
#
#   FileNotFoundError:
#       Die Quelldatei oder das SCP-Programm wurde nicht gefunden.
#
#   KeyError:
#       Ein erforderlicher Konfigurationsparameter fehlt.
#
#   OSError:
#       Fehler beim Starten des SCP-Prozesses.
# =============================================================================
def transfer_file(filename: Path, config: dict) -> None:

    # Verbindungsdaten des Produktservers auslesen.
    remote = config["remote"]

    # Vor dem Start der Übertragung prüfen, ob die lokale
    # Quelldatei tatsächlich vorhanden ist.
    if not filename.is_file():
        raise FileNotFoundError(
            f"Source file not found: {filename}"
        )

    # -------------------------------------------------------------------------
    # Zieladresse erzeugen
    # -------------------------------------------------------------------------

    # Vollständige SCP-Zieladresse zusammensetzen.
    #
    # Format:
    # username@server_ip:/remote/directory/filename
    #
    # Ein eventuell vorhandener abschließender Schrägstrich
    # wird entfernt, um doppelte Schrägstriche zu vermeiden.
    destination = (
        f"{remote['username']}@{remote['ip']}:"
        f"{remote['request_directory'].rstrip('/')}/"
        f"{filename.name}"
    )

    # -------------------------------------------------------------------------
    # SCP-Kommando erstellen
    # -------------------------------------------------------------------------

    # Das Kommando wird als Liste übergeben.
    # Dadurch wird keine zusätzliche Shell zur Ausführung benötigt.
    command = [
        "scp",

        # Konfigurierbarer SSH-Port.
        "-P",
        str(remote["ssh_port"]),

        # Interaktive Passwortabfragen verhindern.
        "-o",
        "BatchMode=yes",

        # Nur bekannte und vertrauenswürdige SSH-Hosts akzeptieren.
        "-o",
        "StrictHostKeyChecking=yes",

        # Pro Aufruf nur einen Verbindungsversuch durchführen.
        "-o",
        "ConnectionAttempts=1",

        # Maximale Wartezeit beim Aufbau der SSH-Verbindung.
        "-o",
        f"ConnectTimeout={remote['ssh_timeout']}",

        # Lokal gespeicherte Quelldatei.
        str(filename),

        # Zieladresse auf dem Produktserver.
        destination
    ]

    logger.info(
        "Transferring file: %s -> %s",
        filename.name,
        destination
    )

    # -------------------------------------------------------------------------
    # Datei übertragen
    # -------------------------------------------------------------------------

    # check=True:
    #   Bei einem Rückgabewert ungleich 0 wird eine Exception ausgelöst.
    #
    # capture_output=True:
    #   Standardausgabe und Fehlerausgabe werden aufgezeichnet.
    #
    # text=True:
    #   Die Ausgaben werden als Text statt als Bytes zurückgegeben.
    #
    # timeout:
    #   Begrenzt die maximale Laufzeit des gesamten SCP-Prozesses.
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=remote["transfer_timeout"]
    )

    # Diese Meldung wird nur ausgegeben, wenn SCP erfolgreich
    # abgeschlossen wurde.
    logger.info(
        "Transfer successful: %s",
        filename.name
    )


# =============================================================================
# Erstellt und versendet eine vollständige Produktlistenanforderung.
#
# Zunächst werden die PIF-Datei und die zugehörige SYC-Datei erzeugt.
# Anschließend werden beide Dateien nacheinander übertragen.
#
# Die SYC-Datei wird ausschließlich dann übertragen, wenn die
# Übertragung der PIF-Datei erfolgreich abgeschlossen wurde.
#
# Diese Funktion stellt die zentrale Schnittstelle des Moduls dar
# und wird später vom Hauptprogramm product_interface.py aufgerufen.
#
# Übergabeparameter:
#   config (dict):
#       Vollständige Konfiguration des Product Interface.
#
# Rückgabewert:
#   bool:
#       True:
#           PIF-Datei und SYC-Datei wurden erfolgreich übertragen.
#
#       False:
#           Erstellung oder Übertragung der Anforderung fehlgeschlagen.
#
# Exceptions:
#   Erwartete Konfigurations-, XML-, Datei- und Übertragungsfehler
#   werden innerhalb dieser Funktion abgefangen und protokolliert.
#
#   Unerwartete Programmfehler werden an das Hauptprogramm
#   weitergegeben.
# =============================================================================
def send_product_list_request(config: dict) -> bool:

    logger.info("Creating product list request")

    try:

        # ---------------------------------------------------------------------
        # Anforderungsdateien erstellen
        # ---------------------------------------------------------------------

        request_file, sync_file = create_product_list_request(
            config
        )

        # ---------------------------------------------------------------------
        # PIF-Datei übertragen
        # ---------------------------------------------------------------------

        # Zuerst die vollständige XML-Anforderung übertragen.
        # Bei einem Übertragungsfehler wird eine Exception ausgelöst.
        transfer_file(request_file, config)

        # ---------------------------------------------------------------------
        # SYC-Datei übertragen
        # ---------------------------------------------------------------------

        # Erst nach erfolgreicher Übertragung der PIF-Datei
        # darf die Synchronisationsdatei übertragen werden.
        #
        # Der Produktserver kann die Anforderung verarbeiten,
        # sobald beide Dateien vollständig vorhanden sind.
        transfer_file(sync_file, config)

        logger.info(
            "Product list request sent successfully"
        )

        return True

    # -------------------------------------------------------------------------
    # Fehlerbehandlung
    # -------------------------------------------------------------------------

    except subprocess.CalledProcessError as error:

        # Fehlercode und Fehlerausgabe des SCP-Prozesses protokollieren.
        logger.error(
            "SCP transfer failed (exit code %s): %s",
            error.returncode,
            (error.stderr or "").strip()
        )

    except subprocess.TimeoutExpired as error:

        # Der SCP-Prozess hat die konfigurierte maximale Laufzeit
        # überschritten und wurde abgebrochen.
        logger.error(
            "SCP transfer timeout after %s seconds",
            error.timeout
        )

    except FileNotFoundError as error:

        # XML-Vorlage, Quelldatei oder SCP-Programm fehlt.
        logger.error(
            "File not found: %s",
            error
        )

    except ET.ParseError as error:

        # Die XML-Vorlage konnte nicht verarbeitet werden.
        logger.error(
            "Invalid XML template: %s",
            error
        )

    except KeyError as error:

        # Ein benötigter Parameter fehlt in der Konfiguration.
        logger.error(
            "Missing configuration parameter: %s",
            error
        )

    except (ValueError, TypeError) as error:

        # Ungültige Konfigurationswerte oder fehlende XML-Elemente.
        logger.error(
            "Invalid request configuration: %s",
            error
        )

    except OSError as error:

        # Allgemeine Fehler beim Lesen, Schreiben oder
        # Ausführen von Dateien.
        logger.error(
            "Operating system error: %s",
            error
        )

    # Bei jedem abgefangenen Fehler False zurückgeben.
    # Das Hauptprogramm kann anhand des Rückgabewerts entscheiden,
    # wie mit der fehlgeschlagenen Anforderung umgegangen wird.
    return False

# =============================================================================
# Erzeugt die individuellen Dateinamen fuer eine Produktdetailanforderung.
#
# Der Produktname wird ausschliesslich fuer die Dateinamen angepasst.
# Saemtliche Leerzeichen werden entfernt.
#
# Der urspruengliche Produktname innerhalb der XML-Anforderung
# bleibt unveraendert.
#
# Beispiel:
#   Produktname: AZ    2024-10-19      AL_5
#
#   Anforderung: pii req + Produktname + .pif
#   Antwort:     pii ans + Produktname + .pif
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#
#   product_name (str):
#       Originaler Produktname aus der empfangenen Produktliste.
#
# Rueckgabewert:
#   tuple[str, str]:
#       Dateiname der Produktdetailanforderung.
#       Dateiname der erwarteten Antwort.
#
# Exceptions:
#   ValueError:
#       Produktname ist leer oder enthaelt unzulaessige Zeichen
#       fuer die Verwendung innerhalb eines Dateinamens.
#
#   KeyError:
#       Erforderliche Konfigurationsparameter fehlen.
# =============================================================================
def get_product_detail_filenames(
    config: dict,
    product_name: str
) -> tuple[str, str]:

    details = config["product_details"]

    # Saemtliche Leerzeichen aus dem Produktnamen entfernen.
    filename_product = "".join(product_name.split())

    # Nur sichere Zeichen innerhalb des Dateinamens zulassen.
    #
    # Dadurch verhindern wir unter anderem, dass Schraegstriche
    # innerhalb eines Produktnamens unbeabsichtigt als
    # Verzeichnispfade interpretiert werden.
    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+",
        filename_product
    ):
        raise ValueError(
            f"Invalid product name for filename: {product_name!r}"
        )

    # Individuellen Namen der Anforderungsdatei erzeugen.
    request_filename = (
        details["request_prefix"]
        + filename_product
        + details["file_extension"]
    )

    # Individuellen Namen der Antwortdatei erzeugen.
    response_filename = (
        details["response_prefix"]
        + filename_product
        + details["file_extension"]
    )

    logger.debug(
        "Product detail filenames: request=%s, response=%s",
        request_filename,
        response_filename
    )

    return request_filename, response_filename

# =============================================================================
# Erstellt eine XML-Produktdetailanforderung fuer ein einzelnes Produkt.
#
# Die Originalvorlage pii001.pif wird eingelesen und angepasst.
#
# Folgende Informationen werden ersetzt:
#
#   - IP-Adresse des Imageservers
#   - Benutzername fuer die Rueckuebertragung
#   - Individueller Dateiname und Pfad der Antwort
#   - Produktname
#
# Die RequestExtensionList bleibt unveraendert.
#
# Anschliessend werden die PIF-Datei und die zugehoerige
# leere SYC-Datei im temporaeren Verzeichnis gespeichert.
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#
#   product (dict):
#       Produktdatensatz aus dem Product List Parser.
#       Der Schluessel "product_name" muss vorhanden sein.
#
# Rueckgabewert:
#   tuple[Path, Path, Path]:
#       Pfad zur erzeugten PIF-Datei.
#       Pfad zur erzeugten SYC-Datei.
#       Erwarteter Pfad zur Antwortdatei.
#
# Exceptions:
#   FileNotFoundError:
#       Die XML-Vorlage wurde nicht gefunden.
#
#   ExpatError:
#       Die XML-Vorlage enthaelt ungueltiges XML.
#
#   ValueError:
#       Die XML-Struktur oder der Produktname ist ungueltig.
#
#   KeyError:
#       Erforderliche Konfigurationsparameter fehlen.
#
#   OSError:
#       Fehler beim Lesen oder Schreiben einer Datei.
# =============================================================================
def create_product_detail_request(
    config: dict,
    product: dict
) -> tuple[Path, Path, Path]:

    local = config["local"]
    details = config["product_details"]
    directories = config["directories"]

    # Originalen Produktnamen inklusive Leerzeichen uebernehmen.
    product_name = product["product_name"]

    logger.info(
        "Creating product detail request: %r",
        product_name
    )

    # -------------------------------------------------------------------------
    # Verzeichnisse und Dateinamen ermitteln
    # -------------------------------------------------------------------------

    template_dir = Path(directories["templates"])
    temp_dir = Path(directories["temporary"])

    temp_dir.mkdir(parents=True, exist_ok=True)

    template_file = (
        template_dir / details["template_file"]
    )

    # Individuelle Anforderungs- und Antwortdateinamen erzeugen.
    request_filename, response_filename = (
        get_product_detail_filenames(
            config,
            product_name
        )
    )

    request_file = temp_dir / request_filename

    # Identischen Basisnamen fuer die SYC-Datei verwenden.
    sync_file = request_file.with_suffix(".syc")

    # Erwarteten vollstaendigen Pfad der Antwortdatei erzeugen.
    response_file = (
        Path(local["response_directory"])
        / response_filename
    )

    # -------------------------------------------------------------------------
    # XML-Vorlage einlesen
    # -------------------------------------------------------------------------

    # minidom verwenden, damit CDATA-Elemente erhalten
    # beziehungsweise gezielt erzeugt werden koennen.
    document = minidom.parse(str(template_file))

    # Request-Element ermitteln.
    request_elements = document.getElementsByTagName(
        "Request"
    )

    if len(request_elements) != 1:
        raise ValueError(
            "Exactly one Request element is required"
        )

    request_element = request_elements[0]

    # -------------------------------------------------------------------------
    # Verbindungsdaten aktualisieren
    # -------------------------------------------------------------------------

    data_request_elements = (
        request_element.getElementsByTagName(
            "DataRequest"
        )
    )

    if len(data_request_elements) != 1:
        raise ValueError(
            "Exactly one DataRequest element is required"
        )

    data_request = data_request_elements[0]

    # In der Originalvorlage wird TargetFileName ohne
    # Dateiendung angegeben.
    #
    # Wir gehen deshalb zunaechst davon aus, dass der
    # Produktserver die Endung .pif selbst ergaenzt.
    target_filename = response_file.with_suffix("")

    values = {
        "InternetAddress": str(local["ip"]),
        "UserName": str(local["username"]),
        "TargetFileName": str(target_filename)
    }

    # Vorhandene XML-Elemente gezielt aktualisieren.
    for tag_name, value in values.items():

        elements = data_request.getElementsByTagName(
            tag_name
        )

        if len(elements) != 1:
            raise ValueError(
                f"Invalid XML element: {tag_name}"
            )

        element = elements[0]

        # Bisherigen Inhalt des Elements entfernen.
        while element.firstChild is not None:
            element.removeChild(element.firstChild)

        # Neuen Textinhalt einfuegen.
        element.appendChild(
            document.createTextNode(value)
        )

    # -------------------------------------------------------------------------
    # Vorhandene Produktnamen entfernen
    # -------------------------------------------------------------------------

    # Die Originalvorlage kann mehrere ProductName-Elemente
    # enthalten. Da wir jedes Produkt einzeln anfordern,
    # werden alle vorhandenen Produktnamen entfernt.
    old_product_names = [
        node
        for node in request_element.childNodes
        if node.nodeType == node.ELEMENT_NODE
        and node.tagName == "ProductName"
    ]

    for old_product_name in old_product_names:

        request_element.removeChild(
            old_product_name
        )

    # -------------------------------------------------------------------------
    # Neues ProductName-Element erzeugen
    # -------------------------------------------------------------------------

    product_element = document.createElement(
        "ProductName"
    )

    # Der Produktname wird als CDATA gespeichert.
    #
    # WICHTIG:
    # Hier werden keinerlei Leerzeichen entfernt.
    product_element.appendChild(
        document.createCDATASection(product_name)
    )

    # -------------------------------------------------------------------------
    # Produktname vor der RequestExtensionList einfuegen
    # -------------------------------------------------------------------------

    # Die Reihenfolge der XML-Elemente aus der
    # Originalvorlage soll erhalten bleiben.
    extension_elements = [
        node
        for node in request_element.childNodes
        if node.nodeType == node.ELEMENT_NODE
        and node.tagName == "RequestExtensionList"
    ]

    if len(extension_elements) != 1:
        raise ValueError(
            "Exactly one RequestExtensionList is required"
        )

    request_element.insertBefore(
        product_element,
        extension_elements[0]
    )

    # -------------------------------------------------------------------------
    # PIF-Datei schreiben
    # -------------------------------------------------------------------------

    # XML-Dokument inklusive CDATA-Struktur und
    # UTF-8-Kodierung speichern.
    request_file.write_bytes(
        document.toxml(encoding="UTF-8")
    )

    # -------------------------------------------------------------------------
    # Leere SYC-Datei erzeugen
    # -------------------------------------------------------------------------

    # Die SYC-Datei muss denselben Basisnamen besitzen
    # wie die zugehoerige PIF-Datei.
    sync_file.write_bytes(b"")

    # -------------------------------------------------------------------------
    # Ergebnis protokollieren
    # -------------------------------------------------------------------------

    logger.info(
        "Product detail request created: %s",
        request_file
    )

    logger.info(
        "Synchronization file created: %s",
        sync_file
    )

    logger.info(
        "Expected product detail response: %s",
        response_file
    )

    return request_file, sync_file, response_file

# =============================================================================
# Erstellt und versendet eine Produktdetailanforderung.
#
# Zuerst wird die PIF-Datei vollstaendig uebertragen.
# Erst danach wird die zugehoerige leere SYC-Datei versendet.
#
# Das Produkt wird durch diese Funktion NICHT als erfolgreich
# verarbeitet im Produktcache markiert.
#
# Dies darf erst nach erfolgreichem Empfang, Auswertung
# und Speicherung der Produktdetails erfolgen.
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#
#   product (dict):
#       Produktdatensatz aus dem Product List Parser.
#
# Rueckgabewert:
#   bool:
#       True  = Anforderung erfolgreich versendet.
#       False = Erstellung oder Uebertragung fehlgeschlagen.
#
# Exceptions:
#   Fehler waehrend der Erstellung oder Uebertragung
#   werden protokolliert.
# =============================================================================
def send_product_detail_request(
    config: dict,
    product: dict
) -> bool:

    try:

        # Individuelle Produktdetailanforderung erzeugen.
        request_file, sync_file, response_file = (
            create_product_detail_request(
                config,
                product
            )
        )

        # Zuerst die XML-Anforderung vollstaendig uebertragen.
        transfer_file(request_file, config)

        # Erst bei erfolgreicher PIF-Uebertragung
        # die Synchronisationsdatei versenden.
        transfer_file(sync_file, config)

        logger.info(
            "Product detail request sent successfully: %r",
            product["product_name"]
        )

        logger.info(
            "Waiting for response file: %s",
            response_file
        )

        # True bestaetigt ausschliesslich den Versand.
        # Der Empfang der Antwort wird hier nicht geprueft.
        return True

    except subprocess.CalledProcessError as error:

        logger.error(
            "Product detail SCP transfer failed: %s",
            (error.stderr or "").strip()
        )

    except subprocess.TimeoutExpired as error:

        logger.error(
            "Product detail transfer timeout: %s seconds",
            error.timeout
        )

    except Exception:

        # Vollstaendigen Traceback protokollieren,
        # damit Fehler bei der Entwicklung nachvollziehbar sind.
        logger.exception(
            "Product detail request failed: %r",
            product.get("product_name")
        )

    return False
