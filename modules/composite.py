#!/usr/bin/env python3
# =============================================================================
# ImageServer V2 - COMPOSITE
# =============================================================================
# Erzeugt RGB-PNGs aus explizit zugeordneten Graustufen-Separationen (1-4C).
# Keine Suche nach Farbkennzeichen im Dateinamen. Keine Queue-Verwaltung.
# Wahlweise mathematische Farbmischung oder ICC-Transformation CMYK -> sRGB.
# =============================================================================
import argparse
import os
import tempfile
import stat
from contextlib import ExitStack
from pathlib import Path

import numpy as np
from PIL import Image


# =============================================================================
# FUNKTION: validate_settings
# Aufgabe: Prueft die unterstuetzten Composite-Parameter der YAML.
# Uebergabeparameter: config - ImageServerConfig.
# Rueckgabewert: tuple[int, int] - maximale Breite und Hoehe.
# =============================================================================
def validate_settings(config):
    settings = config.settings["composite"]
    if settings.get("output_format", "png").lower() != "png":
        raise ValueError("Only PNG output is supported")
    if settings.get("output_mode", "RGB").upper() != "RGB":
        raise ValueError("Only RGB output is supported")
    for key in ("icc_enabled", "simulate_paper_color"):
        if type(settings.get(key, False)) is not bool:
            raise ValueError(f"composite.{key} must be a YAML boolean")
    if settings.get("simulate_paper_color", False) and not settings.get("icc_enabled", False):
        raise ValueError("Paper color simulation requires ICC color management")
    width = settings.get("max_width", 1440)
    height = settings.get("max_height", 2560)
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise ValueError("Invalid composite dimensions")
    return width, height


# =============================================================================
# FUNKTION: resolve_icc_profile
# Aufgabe: Ermittelt das gemeinsame Druckprofil aller aktiven Separationen.
#          Profil aus dem Auftrag hat Vorrang. Nur wenn kein Profil angegeben
#          ist, wird das konfigurierte Standardprofil benutzt.
# Uebergabeparameter:
#   config: ImageServerConfig.
#   separations: dict[str, dict] oder None (manueller Test mit Standardprofil).
# Rueckgabewert: Path zum ICC-Profil oder None bei deaktiviertem ICC.
# Fehler: Fehlende Dateien, ungueltige Namen und widerspruechliche Profile
#         werden nicht stillschweigend durch ein anderes Profil ersetzt.
# =============================================================================
def resolve_icc_profile(config, separations=None):
    validate_settings(config)
    settings = config.settings["composite"]
    if not settings.get("icc_enabled", False):
        return None

    # Ein CMYK-Profil beschreibt die gemeinsame Druckbedingung, nicht vier
    # einzeln anzuwendende Farbkorrekturen.
    profiles = set()
    for color, separation in (separations or {}).items():
        name = separation.get("icc_profile")
        if name is None or name == "":
            continue
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Invalid ICC profile for {color}: {name!r}")
        profiles.add(name)
    if len(profiles) > 1:
        raise ValueError(f"Conflicting ICC profiles: {sorted(profiles)}")

    root = Path(config.settings["server"]["root_dir"])
    default = settings.get("icc_profile")
    # Die alte Konfiguration enthielt einen absoluten Standardprofilpfad.
    # Bei explizitem icc_directory gelten nur Dateinamen aus diesem Ordner.
    legacy = Path(default) if isinstance(default, str) and Path(default).is_absolute() else None
    directory = (root / settings.get("icc_directory",
                                     str(legacy.parent) if legacy else "icc")).resolve()
    if profiles:
        name = next(iter(profiles))
    elif legacy and "icc_directory" not in settings:
        name = legacy.name
    else:
        name = default
    if (not isinstance(name, str) or not name or name != Path(name).name
            or "/" in name or "\\" in name or "\x00" in name
            or Path(name).suffix.lower() not in (".icc", ".icm")
            or not Path(name).stem.strip()):
        raise ValueError(f"Invalid ICC profile filename: {name!r}")

    profile = (directory / name).resolve()
    if not profile.is_relative_to(directory):
        raise ValueError(f"ICC profile outside configured directory: {profile}")
    if not stat.S_ISREG(profile.stat().st_mode):
        raise ValueError(f"ICC profile is not a regular file: {profile}")
    return profile


# =============================================================================
# FUNKTION: create_icc_transform
# Aufgabe: Erstellt eine LittleCMS-Transformation vom CMYK-Druckfarbraum
#          nach sRGB. Pro Composite einmal aufbauen, fuer alle Streifen nutzen.
# Uebergabeparameter: profile_path (Path), simulate_paper_color (bool), logger.
# Rueckgabewert: tuple (ImageCmsTransform, bytes des sRGB-Ausgabeprofils).
# Fehler: Nicht lesbare bzw. ungeeignete ICC-Profile fuehren zum Jobfehler.
# =============================================================================
def create_icc_transform(profile_path, simulate_paper_color, logger):
    from PIL import ImageCms
    source = ImageCms.getOpenProfile(str(profile_path))
    if source.profile.xcolor_space.strip() != "CMYK":
        raise ValueError(f"ICC profile must describe CMYK: {profile_path}")
    target = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    intent = (ImageCms.Intent.ABSOLUTE_COLORIMETRIC if simulate_paper_color
              else ImageCms.Intent.RELATIVE_COLORIMETRIC)
    transform = ImageCms.buildTransform(source, target, "CMYK", "RGB",
                                        renderingIntent=intent)
    logger.info("ICC transformation: %s -> sRGB, intent=%s",
                profile_path, intent.name)
    return transform, target.tobytes()


# =============================================================================
# FUNKTION: combine_icc
# Aufgabe: Kehrt die Grauwerte in Farbauftragswerte um und wendet die zuvor
#          aufgebaute ICC-Transformation auf einen CMYK-Bildstreifen an.
#          PNG-Weiss=255 entspricht keinem Farbauftrag (CMYK=0).
# Uebergabeparameter: strips (list[ndarray] in CMYK-Reihenfolge), transform.
# Rueckgabewert: PIL.Image im RGB-Modus; Aufrufer schliesst das Bild.
# =============================================================================
def combine_icc(strips, transform):
    from PIL import ImageCms
    # Explizite Kanalreihenfolge C, M, Y, K. Nicht Image.convert("CMYK")
    # auf ein schon gemischtes RGB-Bild anwenden.
    ink = 255 - np.stack(strips, axis=-1)
    height, width = ink.shape[:2]
    with Image.frombytes("CMYK", (width, height), ink.tobytes()) as cmyk:
        return ImageCms.applyTransform(cmyk, transform)


# =============================================================================
# FUNKTION: combine_cmyk
# Aufgabe: Rechnet CMYK-Grauwerte (Weiss=255) in RGB um.
# Uebergabeparameter: cyan, magenta, yellow, black - uint8-Graustufenarrays.
# Rueckgabewert: uint8-NumPy-Array (Hoehe, Breite, 3).
# =============================================================================
def combine_cmyk(cyan, magenta, yellow, black):
    c = 255 - cyan.astype(np.int16)
    m = 255 - magenta.astype(np.int16)
    y = 255 - yellow.astype(np.int16)
    k = 255 - black.astype(np.int16)
    rgb = np.empty((*cyan.shape, 3), dtype=np.uint8)
    rgb[:, :, 0] = 255 - np.minimum(255, c + k)
    rgb[:, :, 1] = 255 - np.minimum(255, m + k)
    rgb[:, :, 2] = 255 - np.minimum(255, y + k)
    return rgb


# =============================================================================
# FUNKTION: create_composite
# Aufgabe: Berechnet genau ein RGB-Bild aus 1-4 vorgegebenen Separationen.
#           Nicht benoetigte Farben sind weiss. Die Verarbeitung erfolgt
#           streifenweise. Das Ziel wird erst nach vollstaendigem Schreiben
#           atomar ersetzt. Keine TIFFs oder Quell-PNGs werden geaendert.
# Uebergabeparameter:
#   files: dict[str, Path] - aktive Farbgruppen C/M/Y/K und PNG-Pfade.
#   output_file: Path - Ausgabedatei (beim Worker ausschliesslich Staging).
#   config: ImageServerConfig; logger: logging.Logger.
#   icc_profile: Path zum auftragsbezogenen Druckprofil; None fuer Standard.
# Rueckgabewert: Path - fertig geschriebene RGB-PNG-Datei.
# =============================================================================
def create_composite(files, output_file, config, logger, icc_profile=None):
    max_width, max_height = validate_settings(config)
    if not files or not set(files) <= set("CMYK"):
        raise ValueError("Expected one to four active CMYK separations")
    settings = config.settings["composite"]
    transform = None
    output_profile = None
    if settings.get("icc_enabled", False):
        profile = icc_profile if icc_profile is not None else resolve_icc_profile(config)
        transform, output_profile = create_icc_transform(
            profile, settings.get("simulate_paper_color", False), logger)
    output_file = Path(output_file)
    if any(output_file.resolve() == Path(path).resolve() for path in files.values()):
        raise ValueError("Composite must not overwrite a separation")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        images = {}
        size = None
        dpi = None
        # Alle aktiven Separationen muessen dieselbe Pixelgeometrie haben.
        for color, filename in files.items():
            image = stack.enter_context(Image.open(filename))
            if image.format != "PNG" or image.mode != "L":
                raise ValueError(f"Separation {color} must be an 8-bit grayscale PNG")
            if size is None:
                size = image.size
                dpi = image.info.get("dpi")
            elif image.size != size:
                raise ValueError(f"Separation {color}: size {image.size} != {size}")
            images[color] = image
        width, height = size
        logger.info("Rendering %dC, %dx%d: %s", len(images), width, height, files)
        result = stack.enter_context(Image.new("RGB", size))
        for top in range(0, height, 256):
            bottom = min(top + 256, height)
            white = np.full((bottom - top, width), 255, dtype=np.uint8)
            strips = []
            for color in "CMYK":
                if color in images:
                    with images[color].crop((0, top, width, bottom)) as strip:
                        strips.append(np.array(strip, dtype=np.uint8))
                else:
                    strips.append(white)
            rgb = (combine_icc(strips, transform) if transform is not None
                   else Image.fromarray(combine_cmyk(*strips)))
            with rgb:
                result.paste(rgb, (0, top))
        result.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        options = {}
        # Im PNG wird das AUSGABEPROFIL (sRGB) eingebettet. Das Druckprofil
        # ist bereits angewendet worden und passt nicht zu RGB-Pixeldaten.
        if output_profile is not None:
            options["icc_profile"] = output_profile
        if isinstance(dpi, tuple) and len(dpi) == 2 and min(dpi) > 0:
            options["dpi"] = (dpi[0] * result.width / width,
                              dpi[1] * result.height / height)
        fd, name = tempfile.mkstemp(prefix=".composite-", suffix=".tmp",
                                    dir=output_file.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                result.save(stream, format="PNG", **options)
                # HTTP-Server soll fertige Vorschaubilder lesen koennen.
                os.fchmod(stream.fileno(), 0o644)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, output_file)
        finally:
            Path(name).unlink(missing_ok=True)
        logger.info("RGB PNG rendered: %s (%dx%d)", output_file,
                    result.width, result.height)
    return output_file


# =============================================================================
# FUNKTION: main
# Aufgabe: Manueller Test mit expliziten Farbdateien und Ausgabepfad.
# Uebergabeparameter: Keine; Argumente aus sys.argv.
# Rueckgabewert: int - 0 bei Erfolg, 1 bei Fehler.
# =============================================================================
def main():
    import logging
    if __package__:
        from .config_loader import load_config
    else:
        from config_loader import load_config
    parser = argparse.ArgumentParser(description="Render explicit grayscale PNG separations")
    for color in "CMYK":
        parser.add_argument("--" + color, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config")
    parser.add_argument("--icc-profile", help="Override default ICC filename for manual test")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("composite")
    try:
        config = load_config(args.config) if args.config else load_config()
        files = {c: getattr(args, c) for c in "CMYK" if getattr(args, c)}
        metadata = {"test": {"icc_profile": args.icc_profile}} if args.icc_profile else None
        profile = resolve_icc_profile(config, metadata)
        create_composite(files, args.output, config, logger, icc_profile=profile)
        return 0
    except Exception:
        logger.exception("Composite failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

