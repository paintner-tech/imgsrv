# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: library/logger.py
#
# BESCHREIBUNG:
# -------------
# Dieses Modul stellt das zentrale Logging fuer alle Python-Module
# des Product Interface zur Verfuegung.
#
# Saemtliche Logging-Einstellungen werden aus der zentralen
# Konfigurationsdatei product-interface.yaml ausgelesen.
#
# FUNKTIONSUMFANG:
# ----------------
#   1. Logging-Konfiguration auslesen und ueberpruefen.
#   2. Logverzeichnis bei Bedarf automatisch erstellen.
#   3. Zentrale Logdatei erstellen und verwalten.
#   4. Automatische Rotation der Logdateien durchfuehren.
#   5. Anzahl der Sicherungsdateien begrenzen.
#   6. Konfigurierbares Loglevel bereitstellen.
#   7. Optionale Konsolenausgabe ermoeglichen.
#   8. Modulbezogene Logger bereitstellen.
#
# VERWENDUNG:
# -----------
# Das Hauptprogramm initialisiert das zentrale Logging einmalig
# nach dem Einlesen der Konfiguration:
#
#   setup_logging(config)
#
# Alle weiteren Module beziehen ihren Logger ueber:
#
#   logger = get_logger(__name__)
#
# Dadurch verwenden alle Module dieselbe Logdatei und dieselben
# Logging-Einstellungen.
#
# WICHTIG:
# --------
# Die Logdatei wird ausschliesslich durch den zentralen Logger
# verwaltet. Untergeordnete Logger erhalten keine eigenen Handler.
#
# Dadurch werden doppelte Logeintraege und konkurrierende
# Logrotationen innerhalb desselben Prozesses verhindert.
#
# Der RotatingFileHandler ist fuer mehrere Threads innerhalb
# eines Prozesses geeignet, jedoch nicht fuer mehrere unabhaengige
# Prozesse, die gleichzeitig dieselbe Logdatei rotieren.
# =============================================================================

import logging

from pathlib import Path
from logging.handlers import RotatingFileHandler


# =============================================================================
# Globale Konstanten
# =============================================================================

# Eindeutiger Name des zentralen Product-Interface-Loggers.
LOGGER_NAME = "product-interface"


# =============================================================================
# Initialisiert das zentrale Logging des Product Interface.
#
# Die Logging-Einstellungen werden aus der Konfiguration ausgelesen
# und auf gueltige Werte ueberprueft.
#
# Anschliessend werden das Logverzeichnis, der rotierende
# Datei-Handler und optional der Konsolen-Handler eingerichtet.
#
# Die Funktion muss beim Start des Hauptprogramms aufgerufen werden,
# bevor die ersten regulaeren Logmeldungen geschrieben werden.
#
# Uebergabeparameter:
#   config (dict):
#       Vollstaendige Konfiguration des Product Interface.
#       Die Logging-Einstellungen befinden sich unter "logging".
#
# Rueckgabewert:
#   logging.Logger:
#       Vollstaendig initialisierter zentraler Logger.
#
# Exceptions:
#   KeyError:
#       Ein erforderlicher Konfigurationsparameter fehlt.
#
#   ValueError:
#       Ein Konfigurationsparameter besitzt einen ungueltigen Wert.
#
#   TypeError:
#       Ein Konfigurationsparameter besitzt einen ungueltigen Datentyp.
#
#   OSError:
#       Das Logverzeichnis oder die Logdatei konnte nicht
#       erstellt beziehungsweise geoeffnet werden.
# =============================================================================
def setup_logging(config: dict) -> logging.Logger:

    # -------------------------------------------------------------------------
    # Logging-Konfiguration auslesen
    # -------------------------------------------------------------------------

    log_config = config["logging"]

    log_directory = Path(log_config["directory"])
    log_filename = log_config["filename"]
    log_level_name = log_config["level"]

    max_size_mb = log_config["max_size_mb"]
    backup_count = log_config["backup_count"]
    console_output = log_config["console_output"]

    # -------------------------------------------------------------------------
    # Loglevel ueberpruefen
    # -------------------------------------------------------------------------

    # Das Loglevel muss als Zeichenkette angegeben werden.
    if not isinstance(log_level_name, str):
        raise TypeError(
            "Logging level must be a string"
        )

    # Gross- und Kleinschreibung vereinheitlichen.
    log_level_name = log_level_name.strip().upper()

    # Ausschliesslich bekannte Loglevel akzeptieren.
    valid_levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL
    }

    if log_level_name not in valid_levels:
        raise ValueError(
            f"Invalid logging level: {log_level_name}"
        )

    # Numerischen Wert des ausgewaehlten Loglevels ermitteln.
    log_level = valid_levels[log_level_name]

    # -------------------------------------------------------------------------
    # Dateinamen ueberpruefen
    # -------------------------------------------------------------------------

    # Der Dateiname darf keinen zusaetzlichen Verzeichnispfad
    # enthalten. Das Zielverzeichnis wird separat konfiguriert.
    if not isinstance(log_filename, str):
        raise TypeError(
            "Log filename must be a string"
        )

    if (
        not log_filename.strip()
        or log_filename in (".", "..")
        or Path(log_filename).name != log_filename
    ):
        raise ValueError(
            "Invalid log filename"
        )

    # -------------------------------------------------------------------------
    # Maximale Dateigroesse ueberpruefen
    # -------------------------------------------------------------------------

    # Die maximale Dateigroesse muss eine positive Zahl sein.
    # Boolesche Werte werden ausdruecklich nicht akzeptiert.
    if type(max_size_mb) not in (int, float):
        raise TypeError(
            "Maximum log file size must be numeric"
        )

    if max_size_mb <= 0:
        raise ValueError(
            "Maximum log file size must be greater than zero"
        )

    # Dateigroesse von MiB in Bytes umrechnen.
    max_bytes = int(max_size_mb * 1024 * 1024)

    if max_bytes < 1:
        raise ValueError(
            "Maximum log file size is too small"
        )

    # -------------------------------------------------------------------------
    # Anzahl der Sicherungsdateien ueberpruefen
    # -------------------------------------------------------------------------

    # Mindestens eine Sicherungsdatei muss vorhanden sein,
    # damit die automatische Logrotation stattfinden kann.
    if type(backup_count) is not int or backup_count < 1:
        raise ValueError(
            "Log backup count must be a positive integer"
        )

    # -------------------------------------------------------------------------
    # Konsolenausgabe ueberpruefen
    # -------------------------------------------------------------------------

    # In der YAML-Konfiguration sind ausschliesslich
    # die booleschen Werte true und false erlaubt.
    if type(console_output) is not bool:
        raise TypeError(
            "Console output must be true or false"
        )

    # -------------------------------------------------------------------------
    # Logverzeichnis vorbereiten
    # -------------------------------------------------------------------------

    # Logverzeichnis automatisch erstellen, falls es noch
    # nicht vorhanden ist.
    log_directory.mkdir(
        parents=True,
        exist_ok=True
    )

    # Vollstaendigen Pfad der Logdatei erzeugen.
    log_file = log_directory / log_filename

    # -------------------------------------------------------------------------
    # Zentralen Logger beziehen
    # -------------------------------------------------------------------------

    logger = logging.getLogger(LOGGER_NAME)

    # -------------------------------------------------------------------------
    # Ausgabeformat definieren
    # -------------------------------------------------------------------------

    # Jeder Logeintrag enthaelt:
    #
    #   - Datum und Uhrzeit
    #   - Loglevel
    #   - Modulname
    #   - Zeilennummer
    #   - Logmeldung
    #
    # Modulname und Zeilennummer erleichtern die Zuordnung
    # einer Fehlermeldung zur entsprechenden Stelle im Code.
    formatter = logging.Formatter(
        fmt=(
            "%(asctime)s - "
            "%(levelname)-8s - "
            "%(name)s:%(lineno)d - "
            "%(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # -------------------------------------------------------------------------
    # Rotierenden Datei-Handler erstellen
    # -------------------------------------------------------------------------

    # Bei Erreichen der konfigurierten Dateigroesse wird die
    # aktuelle Logdatei automatisch umbenannt.
    #
    # Beispiel:
    #
    #   product-interface.log   -> product-interface.log.1
    #   product-interface.log.1 -> product-interface.log.2
    #
    # Sobald die maximale Anzahl der Sicherungsdateien
    # erreicht ist, wird die aelteste Datei entfernt.
    #
    # Der Handler wird erstellt, bevor vorhandene Handler
    # entfernt werden. Sollte das Oeffnen der neuen Logdatei
    # fehlschlagen, bleibt der bisherige Logger erhalten.
    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )

    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    # Liste der zukuenftig verwendeten Handler erstellen.
    new_handlers = [file_handler]

    # -------------------------------------------------------------------------
    # Optionale Konsolenausgabe einrichten
    # -------------------------------------------------------------------------

    # Die Konsolenausgabe ist besonders waehrend der
    # Entwicklung und bei manuellen Tests hilfreich.
    #
    # Fuer den spaeteren Betrieb als systemd-Dienst kann sie
    # bei Bedarf ueber die Konfiguration deaktiviert werden.
    if console_output:

        console_handler = logging.StreamHandler()

        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)

        new_handlers.append(console_handler)

    # -------------------------------------------------------------------------
    # Zentralen Logger konfigurieren
    # -------------------------------------------------------------------------

    logger.setLevel(log_level)

    # Weiterleitung an den Python-Root-Logger verhindern.
    # Dadurch werden doppelte Logmeldungen vermieden.
    logger.propagate = False

    # Vorhandene Handler entfernen und schliessen.
    #
    # Dadurch kann setup_logging() bei Bedarf erneut aufgerufen
    # werden, ohne dass Logmeldungen mehrfach ausgegeben werden.
    for old_handler in logger.handlers[:]:

        logger.removeHandler(old_handler)
        old_handler.close()

    # Neue Handler dem zentralen Logger zuordnen.
    for handler in new_handlers:

        logger.addHandler(handler)

    # -------------------------------------------------------------------------
    # Erfolgreiche Initialisierung protokollieren
    # -------------------------------------------------------------------------

    logger.info(
        "============================================================"
    )

    logger.info(
        "Product Interface logging initialized"
    )

    logger.info(
        "Log file: %s",
        log_file
    )

    logger.info(
        "Log level: %s",
        log_level_name
    )

    logger.info(
        "Maximum log file size: %s MiB",
        max_size_mb
    )

    logger.info(
        "Log backup count: %s",
        backup_count
    )

    logger.info(
        "Console output: %s",
        console_output
    )

    logger.info(
        "============================================================"
    )

    # Initialisierten zentralen Logger zurueckgeben.
    return logger


# =============================================================================
# Liefert einen modulbezogenen Logger des Product Interface.
#
# Der zurueckgegebene Logger ist dem zentralen Logger untergeordnet
# und verwendet dessen Loglevel und Ausgabe-Handler.
#
# Die einzelnen Module muessen deshalb keine eigene Logdatei
# oeffnen oder eine eigene Logrotation implementieren.
#
# Die Funktion kann bereits beim Import eines Moduls aufgerufen
# werden. Die eigentliche Logging-Initialisierung erfolgt spaeter
# durch setup_logging() im Hauptprogramm.
#
# Uebergabeparameter:
#   module_name (str):
#       Name des aufrufenden Python-Moduls.
#       Normalerweise wird hierfuer __name__ uebergeben.
#
# Rueckgabewert:
#   logging.Logger:
#       Modulbezogener Logger des Product Interface.
#
# Exceptions:
#   TypeError:
#       Der Modulname ist keine Zeichenkette.
#
#   ValueError:
#       Der Modulname ist leer.
# =============================================================================
def get_logger(module_name: str) -> logging.Logger:

    # Uebergebenen Modulnamen ueberpruefen.
    if not isinstance(module_name, str):
        raise TypeError(
            "Logger module name must be a string"
        )

    if not module_name.strip():
        raise ValueError(
            "Logger module name must not be empty"
        )

    # Beim direkten Start des Hauptprogramms lautet
    # dessen Python-Modulname normalerweise "__main__".
    #
    # Fuer die Logausgabe wird stattdessen der
    # aussagekraeftigere Name "main" verwendet.
    if module_name == "__main__":
        module_name = "main"

    # Untergeordneten Logger des zentralen Loggers beziehen.
    #
    # Beispiel:
    #   product-interface.request_sender
    #   product-interface.library.config_loader
    #   product-interface.main
    logger = logging.getLogger(
        f"{LOGGER_NAME}.{module_name}"
    )

    # NOTSET bewirkt, dass der Logger das Loglevel
    # des uebergeordneten Loggers uebernimmt.
    logger.setLevel(logging.NOTSET)

    # Logmeldungen an den zentralen Logger weiterleiten.
    logger.propagate = True

    # Modulbezogenen Logger zurueckgeben.
    return logger
