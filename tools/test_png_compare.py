#!/usr/bin/env python3
# =============================================================================
# ImageServer V2 - PNG PIXEL COMPARISON
# =============================================================================
# Modul: test_png_compare.py
#
# BESCHREIBUNG:
#   Vergleicht zwei gleich grosse RGB-PNGs pixelweise (8 Bit je Kanal).
#   Es werden die gespeicherten RGB-Werte verglichen, ohne eine weitere
#   ICC-Farbtransformation, Skalierung oder Interpolation vorzunehmen.
#   Optional: einzelne Pixel anzeigen oder ein Differenzbild erzeugen.
#   Die Eingabedateien werden nicht veraendert.
#
# AUFRUF:
#   python3 test_png_compare.py bild1.png bild2.png
#   python3 test_png_compare.py bild1.png bild2.png --pixel 100 200
#   python3 test_png_compare.py bild1.png bild2.png --diff /tmp/diff.png
#
# ABHAENGIGKEITEN:
#   Pillow, NumPy (bereits Bestandteil des ImageServers).
# =============================================================================

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image


# =============================================================================
# FUNKTION: read_rgb
# Aufgabe: Liest die gespeicherten RGB-Werte einer 8-Bit-RGB-PNG-Datei.
#          Keine ICC-Umrechnung und keine Konvertierung anderer Bildmodi.
# Uebergabeparameter: filename (Path) - Pfad zur Bilddatei.
# Rueckgabewert: np.ndarray (Hoehe, Breite, 3), Datentyp uint8.
# Fehler: OSError bei Lesefehler, ValueError bei falschem Bildformat.
# =============================================================================
def read_rgb(filename: Path) -> np.ndarray:
    with Image.open(filename) as image:
        if image.format != "PNG" or image.mode != "RGB":
            raise ValueError(f"RGB-PNG erforderlich: {filename} ({image.format}, {image.mode})")
        # PNG IHDR: Bittiefe an Byte 24. Pillow kann 16-Bit-RGB intern auf
        # 8 Bit reduzieren; solche Dateien daher bewusst nicht akzeptieren.
        with filename.open("rb") as stream:
            header = stream.read(29)
        if len(header) != 29 or header[24] != 8 or header[25] != 2:
            raise ValueError(f"8-Bit-Truecolor-PNG erforderlich: {filename}")
        return np.array(image, dtype=np.uint8)


# =============================================================================
# FUNKTION: compare_pixels
# Aufgabe: Berechnet absolute Abweichungen pro Pixel und RGB-Kanal.
# Uebergabeparameter: first, second (np.ndarray) - gleich grosse RGB-Bilder.
# Rueckgabewert: np.ndarray mit absoluten Abweichungen (int16, 0 bis 255).
# Fehler: ValueError bei unterschiedlichen Bildabmessungen.
# =============================================================================
def compare_pixels(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    if first.shape != second.shape:
        raise ValueError(
            f"Bildabmessungen unterscheiden sich: "
            f"{first.shape[1]}x{first.shape[0]} und "
            f"{second.shape[1]}x{second.shape[0]}"
        )

    # Vor der Subtraktion in vorzeichenbehafteten Datentyp umwandeln.
    # Bei uint8 wuerden negative Ergebnisse sonst ueberlaufen.
    return np.abs(first.astype(np.int16) - second.astype(np.int16))


# =============================================================================
# FUNKTION: print_statistics
# Aufgabe: Gibt Anzahl geaenderter Pixel sowie mittlere und maximale
#          RGB-Abweichungen aus.
# Uebergabeparameter: diff (np.ndarray) - absolute RGB-Abweichungen.
# Rueckgabewert: None.
# =============================================================================
def print_statistics(diff: np.ndarray) -> None:
    total = diff.shape[0] * diff.shape[1]
    changed = int(np.count_nonzero(np.any(diff != 0, axis=2)))
    print(f"Pixel insgesamt          : {total}")
    print(f"Identische Pixel         : {total - changed} ({100 * (total - changed) / total:.2f} %)")
    print(f"Unterschiedliche Pixel   : {changed} ({100 * changed / total:.2f} %)")
    print(f"Mittlere RGB-Abweichung  : {diff.mean():.4f} von 255")
    print(f"Maximale RGB-Abweichung  : {diff.max()} von 255")

    for index, color in enumerate("RGB"):
        channel = diff[:, :, index]
        print(f"Kanal {color}: Mittelwert {channel.mean():.4f}, Maximum {channel.max()}")


# =============================================================================
# FUNKTION: print_pixel
# Aufgabe: Zeigt die RGB-Werte beider Bilder an einer bestimmten Position.
# Uebergabeparameter: first, second (np.ndarray); x, y (int) - Koordinaten,
#                    Ursprung links oben bei (0, 0).
# Rueckgabewert: None.
# Fehler: ValueError bei Koordinaten ausserhalb des Bildes.
# =============================================================================
def print_pixel(first: np.ndarray, second: np.ndarray, x: int, y: int) -> None:
    height, width = first.shape[:2]
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(f"Pixel ({x}, {y}) liegt ausserhalb des Bildes")

    rgb_a = tuple(int(value) for value in first[y, x])
    rgb_b = tuple(int(value) for value in second[y, x])
    print(f"\nPixel ({x}, {y})")
    print(f"Bild 1 (RGB): {rgb_a}")
    print(f"Bild 2 (RGB): {rgb_b}")
    print(f"Differenz   : {tuple(abs(a - b) for a, b in zip(rgb_a, rgb_b))}")


# =============================================================================
# FUNKTION: save_difference
# Aufgabe: Speichert ein RGB-Differenzbild mit optionaler Verstaerkung.
#          Identische Farbkanaele bleiben schwarz; Abweichungen werden hell.
# Uebergabeparameter: diff (np.ndarray); output (Path) - neue PNG-Datei;
#                    factor (int) - Verstaerkung zwischen 1 und 255.
# Rueckgabewert: None.
# Fehler: FileExistsError bei bereits vorhandener Zieldatei.
# =============================================================================
def save_difference(diff: np.ndarray, output: Path, factor: int) -> None:
    # int32 verhindert einen Ueberlauf auch bei faktor=255.
    pixels = np.clip(diff.astype(np.int32) * factor, 0, 255).astype(np.uint8)

    # Exklusiv neu anlegen: Eingabebilder und bestehende Dateien schuetzen.
    with Image.fromarray(pixels) as image:
        with output.open("xb") as stream:
            image.save(stream, format="PNG")

    print(f"\nDifferenzbild (Faktor {factor}): {output}")


# =============================================================================
# FUNKTION: main
# Aufgabe: Liest Argumente, vergleicht die Bilder und zeigt die Ergebnisse an.
# Uebergabeparameter: Keine (Kommandozeile ueber argparse).
# Rueckgabewert: int - 0 bei erfolgreichem Vergleich, 1 bei Fehler.
#                Unterschiedliche Pixel sind kein Programmfehler.
# =============================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vergleicht die gespeicherten RGB-Pixelwerte zweier PNGs."
    )
    parser.add_argument("first", type=Path, help="Erstes RGB-PNG (Referenz)")
    parser.add_argument("second", type=Path, help="Zweites RGB-PNG (Vergleich)")
    parser.add_argument("--pixel", nargs=2, type=int, metavar=("X", "Y"),
                        help="RGB-Werte an Position X/Y anzeigen")
    parser.add_argument("--diff", type=Path, metavar="PNG",
                        help="Optionales Differenzbild speichern")
    parser.add_argument("--amplify", type=int, default=4, metavar="FAKTOR",
                        help="Verstaerkung des Differenzbildes (1-255, Standard: 4)")
    args = parser.parse_args()

    if not 1 <= args.amplify <= 255:
        parser.error("--amplify muss zwischen 1 und 255 liegen")

    try:
        first = read_rgb(args.first)
        second = read_rgb(args.second)
        diff = compare_pixels(first, second)

        print(f"Bild 1: {args.first}")
        print(f"Bild 2: {args.second}")
        print(f"Aufloesung: {first.shape[1]} x {first.shape[0]}\n")
        print_statistics(diff)

        if args.pixel is not None:
            print_pixel(first, second, *args.pixel)
        if args.diff is not None:
            save_difference(diff, args.diff, args.amplify)
        return 0
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

