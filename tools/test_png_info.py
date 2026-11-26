#!/usr/bin/env python3
# =============================================================================
# ImageServer V2 - PNG INFO
# =============================================================================
# Modul: test_png_info.py
#
# BESCHREIBUNG:
#   Zeigt die Bildabmessungen und das Vorhandensein eines eingebetteten
#   ICC-Profils fuer eine oder mehrere PNG-Dateien an.
#   Die Eingabedateien werden nicht veraendert.
#
# AUFRUF:
#   python3 test_png_info.py bild1.png bild2.png
#   python3 test_png_info.py /pecom/imgsrv/composites/*.png
#
# ABHAENGIGKEIT:
#   Pillow (bereits Bestandteil des ImageServers).
# =============================================================================

import argparse
import sys
from pathlib import Path

from PIL import Image


# =============================================================================
# FUNKTION: print_png_info
# Aufgabe: Liest die PNG-Metadaten und gibt Aufloesung und ICC-Status aus.
# Uebergabeparameter: filename (Path) - Pfad zur PNG-Datei.
# Rueckgabewert: None.
# Fehler: OSError bei nicht lesbarer Datei, ValueError bei falschem Format.
# =============================================================================
def print_png_info(filename: Path) -> None:
    with Image.open(filename) as image:
        if image.format != "PNG":
            raise ValueError("Datei ist kein PNG")

        # Prueft nur, ob ein ICC-Profil eingebettet ist, nicht dessen Qualitaet.
        has_icc = bool(image.info.get("icc_profile"))
        print(f"{filename} {image.size} ICC: {has_icc}")


# =============================================================================
# FUNKTION: main
# Aufgabe: Liest die Dateinamen aus der Kommandozeile und wertet sie einzeln aus.
#          Ein Fehler bei einer Datei verhindert nicht die weiteren Pruefungen.
# Uebergabeparameter: Keine; Kommandozeilenargumente ueber argparse.
# Rueckgabewert: int - 0 wenn alle Dateien lesbar sind, sonst 1.
# =============================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Zeigt Aufloesung und ICC-Status von PNG-Dateien an."
    )
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        metavar="PNG",
        help="Eine oder mehrere PNG-Dateien"
    )
    args = parser.parse_args()

    exit_code = 0
    for filename in args.files:
        try:
            print_png_info(filename)
        except (OSError, ValueError) as error:
            print(f"ERROR: {filename}: {error}", file=sys.stderr)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

