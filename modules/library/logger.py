"""Gemeinsamer Logger fuer alle Module des Imageservers."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


# Erstellt einen Logger mit rotierenden Logdateien.
# Parameter:
#   name: Eindeutiger Name des Moduls, z.B. 'config_loader'.
#   log_dir: Verzeichnis, in dem die Logdatei gespeichert wird.
#   filename: Name der Logdatei, z.B. 'config_loader.log'.
# Rueckgabe: Fertig eingerichteter logging.Logger.
def setup_rotating_logger(name, log_dir, filename):
    log_path = Path(log_dir) / filename
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Bei erneutem Aufruf denselben Logger verwenden, wenn der Pfad passt.
    # Falls der Pfad geaendert wurde, den alten Datei-Handler ersetzen.
    for old_handler in logger.handlers[:]:
        if isinstance(old_handler, RotatingFileHandler):
            if Path(old_handler.baseFilename) == log_path.resolve():
                return logger
        logger.removeHandler(old_handler)
        old_handler.close()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8',
    )
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger

