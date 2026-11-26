#!/usr/bin/env python3
# =============================================================================
# ImageServer V2 - TEST JOB PROCESSOR
# =============================================================================
# Automatisierter, isolierter Test mit kuenstlichen Graustufen-PNGs.
# Benutzt den unveraenderten Product Interface Job Manager. Alle Dateien,
# einschliesslich Logs und Manifest, liegen in einem temporaeren Verzeichnis.
# Keine Produktanfragen, keine produktiven Bilder und keine produktive Queue.
# =============================================================================
import argparse
import hashlib
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageCms

from composite import resolve_icc_profile
import job_processor

from job_processor import (claim_next, get_job_state_file, get_queue_paths,
                           load_manifest, process_claim, resolve_inputs,
                           run_workers, validate_config)
from job_manager import synchronize_jobs

LOGGER = logging.getLogger("test_job_processor")
LOGGER.addHandler(logging.NullHandler())

# Optional explizit mit --icc-profile das Kundenprofil testen. Ohne
# Angabe wird ein eventuell vorhandenes Ghostscript-Testprofil verwendet.
TEST_PROFILE = Path("/usr/share/color/icc/ghostscript/default_cmyk.icc")


# =============================================================================
# FUNKTION: make_job
# Aufgabe: Erzeugt ein Testrezept im Format des Job Creators.
# Parameter: separations (dict), outputs (list[str]).
# Rueckgabewert: dict - Job ohne Revision (Manager vergibt Revision).
# =============================================================================
def make_job(separations, outputs):
    recipe = {"separations": separations}
    return dict(schema_version=1,
                job_id=hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest(),
                separations=separations, separation_count=len(separations),
                composites=outputs, sources=[])


# =============================================================================
# FUNKTION: make_config
# Aufgabe: Erzeugt Testkonfigurationen mit voneinander getrennten Verzeichnissen.
# Parameter: root (Path) - temporaeres Testverzeichnis.
# Rueckgabewert: tuple(ImageServerConfig-kompatibles Objekt, dict).
# =============================================================================
def make_config(root):
    paths = SimpleNamespace(separation_png=root / "pngs",
                            composite_png=root / "composites", logs=root / "logs")
    paths.separation_png.mkdir()
    config = SimpleNamespace(paths=paths, settings={
        "server": {"root_dir": str(root)},
        "jobs": {"pending": "jobs/pending", "processing": "jobs/running",
                 "done": "jobs/done", "failed": "jobs/failed"},
        "logging": {"level": "INFO", "console_output": False},
        "composite": {"max_width": 100, "max_height": 100,
                      "icc_enabled": False, "simulate_paper_color": False}})
    pii = {"job_creation": {"output_directory": str(root / "jobs/pending")}}
    validate_config(config, pii)
    return config, pii


# =============================================================================
# Testfaelle: Jeder Test arbeitet mit einer eigenen leeren Warteschlange.
# =============================================================================
class TestJobProcessor(unittest.TestCase):
    # Aufgabe: Testverzeichnis und Konfiguration anlegen. Rueckgabe: None.
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="imgsrv-test-")
        self.addCleanup(temp.cleanup)
        self.config, self.pii = make_config(Path(temp.name))
        self.paths = get_queue_paths(self.pii)

    # Aufgabe: Graustufenbild schreiben/ersetzen.
    # Parameter: name (str), gray (int), size (tuple). Rueckgabe: None.
    def png(self, name, gray, size=(8, 6)):
        path = self.config.paths.separation_png / (name + ".png")
        with Image.new("L", size, gray) as image:
            temp = path.with_suffix(".tmp")
            image.save(temp, format="PNG")
            os.replace(temp, path)

    # Aufgabe: Sucht alle bereiten Jobs und bearbeitet sie seriell.
    # Parameter: Keine. Rueckgabe: Liste der erreichten Zustaende.
    def drain(self):
        result = []
        while True:
            claim = claim_next(self.config, self.pii, LOGGER)
            if claim is None:
                return result
            result.append(process_claim(self.config, self.pii, claim, LOGGER))

    # Aufgabe: Liest den RGB-Wert eines fertigen Bilds. Parameter: name (str).
    # Rueckgabe: RGB-Tupel.
    def pixel(self, name):
        with Image.open(self.config.paths.composite_png / name) as image:
            self.assertEqual(image.mode, "RGB")
            return image.getpixel((0, 0))

    # Aufgabe: Testet 1C-4C, alternative Namen und gemeinsame Ausgabedateien.
    # Parameter/Rueckgabe: Keine.
    def test_colors_and_aliases(self):
        jobs = []
        # Aktive Graustufen: K=200, C=180, M=160, Y=140.
        expected = [(200, 200, 200), (125, 200, 200),
                    (125, 105, 200), (125, 105, 85)]
        sep = {}
        for number, (color, gray) in enumerate(zip("KCMY", (200, 180, 160, 140)), 1):
            self.png(color + "id", gray)
            sep[color] = {"reference_name": color + "missing", "content_id": color + "id"}
            jobs.append(make_job(dict(sep), [f"{number}.png", f"{number}.alt.png"]))
        synchronize_jobs(self.pii, jobs)
        self.assertEqual(self.drain(), ["done"] * 4)
        for number, rgb in enumerate(expected, 1):
            self.assertEqual(self.pixel(f"{number}.png"), rgb)
            a = self.config.paths.composite_png / f"{number}.png"
            b = self.config.paths.composite_png / f"{number}.alt.png"
            self.assertEqual(a.stat().st_ino, b.stat().st_ino)
        self.assertEqual(self.drain(), [])

    # Aufgabe: Fehlende Quellen blockieren nur ihren eigenen Job.
    # Parameter/Rueckgabe: Keine.
    def test_missing_input(self):
        self.png("one", 220)
        jobs = [make_job({"K": {"reference_name": "one"}}, ["ready.png"]),
                make_job({"K": {"reference_name": "two"}}, ["wait.png"])]
        synchronize_jobs(self.pii, jobs)
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(len(list(self.paths["pending"].glob("*.job"))), 1)
        self.png("two", 50)
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(self.pixel("wait.png"), (50, 50, 50))

    # Aufgabe: Erkennt neue Eingabedateien und fehlende fertige Ausgaben.
    # Parameter/Rueckgabe: Keine.
    def test_update(self):
        self.png("one", 100)
        synchronize_jobs(self.pii, [make_job({"K": {"reference_name": "one"}}, ["result.png"])])
        self.assertEqual(self.drain(), ["done"])
        self.png("one", 220)
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(self.pixel("result.png"), (220, 220, 220))
        (self.config.paths.composite_png / "result.png").unlink()
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(self.drain(), [])

    # Aufgabe: Bei Manifestwechsel darf ein alter Worker nicht publizieren.
    # Parameter/Rueckgabe: Keine.
    def test_obsolete_revision(self):
        self.png("old", 100)
        self.png("new", 220)
        old = make_job({"K": {"reference_name": "old"}}, ["result.png"])
        new = make_job({"K": {"reference_name": "new"}}, ["result.png"])
        synchronize_jobs(self.pii, [old])
        claim = claim_next(self.config, self.pii, LOGGER)
        self.addCleanup(claim[1].close)
        synchronize_jobs(self.pii, [new])
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(process_claim(self.config, self.pii, claim, LOGGER), "obsolete")
        self.assertEqual(self.pixel("result.png"), (220, 220, 220))

    # Aufgabe: Eingabewechsel zwischen Uebernahme und Publikation wird erkannt.
    # Parameter/Rueckgabe: Keine.
    def test_input_changes_during_render(self):
        self.png("one", 100)
        synchronize_jobs(self.pii, [make_job({"K": {"reference_name": "one"}}, ["result.png"])])
        claim = claim_next(self.config, self.pii, LOGGER)
        self.addCleanup(claim[1].close)
        self.png("one", 220)
        self.assertEqual(process_claim(self.config, self.pii, claim, LOGGER), "pending")
        self.assertFalse((self.config.paths.composite_png / "result.png").exists())
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(self.pixel("result.png"), (220, 220, 220))

    # Aufgabe: Simuliert abgestuerzten Worker durch Freigabe seiner Job-Lease.
    # Parameter/Rueckgabe: Keine.
    def test_recover(self):
        self.png("one", 100)
        synchronize_jobs(self.pii, [make_job({"K": {"reference_name": "one"}}, ["result.png"])])
        claim = claim_next(self.config, self.pii, LOGGER)
        self.addCleanup(claim[1].close)
        self.assertIsNone(claim_next(self.config, self.pii, LOGGER))
        claim[1].close()
        self.assertEqual(self.drain(), ["done"])

    # Aufgabe: Unterschiedliche Geometrie fuehrt zu failed, nicht zu falschem PNG.
    # Parameter/Rueckgabe: Keine.
    def test_bad_size(self):
        self.png("one", 100)
        self.png("two", 150, (7, 6))
        job = make_job({"K": {"reference_name": "one"}, "C": {"reference_name": "two"}}, ["bad.png"])
        synchronize_jobs(self.pii, [job])
        self.assertEqual(self.drain(), ["failed"])
        self.assertEqual(len(list(self.paths["failed"].glob("*.job"))), 1)
        self.assertFalse((self.config.paths.composite_png / "bad.png").exists())
        self.assertEqual(self.drain(), [])

    # Aufgabe: ReferenceName hat Vorrang vor ContentID; auch alte Feldnamen.
    # Parameter/Rueckgabe: Keine.
    def test_reference_preferred(self):
        self.png("ref", 170)
        self.png("cid", 30)
        job = make_job({"K": {"reference": "ref", "content_id": "cid"}}, ["result.png"])
        synchronize_jobs(self.pii, [job])
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(self.pixel("result.png"), (170, 170, 170))

    # Aufgabe: Ungueltige Aliase werden nicht als fehlende Dateien behandelt.
    # Parameter/Rueckgabe: Keine.
    def test_invalid_alias(self):
        job = make_job({"K": {"reference_name": "../unsafe"}}, ["bad.png"])
        synchronize_jobs(self.pii, [job])
        self.assertEqual(self.drain(), ["failed"])
        self.assertEqual(len(list(self.paths["failed"].glob("*.job"))), 1)

    # Aufgabe: Aktiviert Test-ICC-Verzeichnis und kopiert ggf. ein echtes Profil.
    # Parameter: use_real (bool). Rueckgabe: Path zum Testprofil.
    def configure_icc(self, use_real=True):
        directory = Path(self.config.settings["server"]["root_dir"]) / "icc"
        directory.mkdir(exist_ok=True)
        self.config.settings["composite"].update(
            icc_enabled=True, icc_directory=str(directory), icc_profile="default.icc")
        profile = directory / "default.icc"
        if use_real:
            if not TEST_PROFILE.is_file():
                self.skipTest("Kein CMYK-Testprofil; --icc-profile /pfad/datei.icc angeben")
            shutil.copyfile(TEST_PROFILE, profile)
        return profile

    # Aufgabe: Verifiziert Profilwahl, Standardprofil und Pfadsicherheit.
    # Parameter/Rueckgabe: Keine.
    def test_icc_profile_selection(self):
        default = self.configure_icc(False)
        default.write_bytes(b"placeholder")
        special = default.with_name("produkt.icc")
        special.write_bytes(b"placeholder")
        self.assertEqual(resolve_icc_profile(self.config, {}), default)
        sep = {"C": {"icc_profile": "produkt.icc"}, "K": {"icc_profile": "produkt.icc"}}
        self.assertEqual(resolve_icc_profile(self.config, sep), special)
        for invalid in ("../default.icc", "/tmp/default.icc", "bad.png"):
            with self.assertRaises(ValueError):
                resolve_icc_profile(self.config, {"K": {"icc_profile": invalid}})
        with self.assertRaises(FileNotFoundError):
            resolve_icc_profile(self.config, {"K": {"icc_profile": "missing.icc"}})
        with self.assertRaises(ValueError):
            resolve_icc_profile(self.config, {"K": {"icc_profile": "default.icc"},
                                              "C": {"icc_profile": "produkt.icc"}})

    # Aufgabe: Verifiziert 1C bis 4C gegen eine unabhaengige CMYK-Referenz
    # und kontrolliert, dass das RGB-PNG ein sRGB-Ausgabeprofil enthaelt.
    # Parameter/Rueckgabe: Keine.
    def test_icc_colors(self):
        default = self.configure_icc()
        profile = default.with_name("produkt.icc")
        default.rename(profile)
        # Fester, absichtlich ungueltiger Standard. Der Auftrag muss Vorrang haben.
        default.write_bytes(b"invalid default")
        jobs, expected = [], {}
        sep, ink = {}, [0, 0, 0, 0]
        for number, (color, gray) in enumerate(zip("KCMY", (120, 180, 50, 200)), 1):
            self.png(color, gray)
            sep[color] = {"reference_name": color, "icc_profile": profile.name}
            ink["CMYK".index(color)] = 255 - gray
            name = f"{number}.png"
            jobs.append(make_job(dict(sep), [name]))
            with Image.new("CMYK", (1, 1), tuple(ink)) as reference:
                with ImageCms.profileToProfile(
                        reference, str(profile), ImageCms.createProfile("sRGB"),
                        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                        outputMode="RGB") as rgb:
                    expected[name] = rgb.getpixel((0, 0))
        synchronize_jobs(self.pii, jobs)
        self.assertEqual(self.drain(), ["done"] * 4)
        for name, pixel in expected.items():
            self.assertEqual(self.pixel(name), pixel)
            with Image.open(self.config.paths.composite_png / name) as image:
                output = ImageCms.getOpenProfile(io.BytesIO(image.info["icc_profile"]))
                self.assertEqual(output.profile.xcolor_space.strip(), "RGB")
                self.assertIn("sRGB", ImageCms.getProfileDescription(output))
        self.assertEqual(self.drain(), [])

    # Aufgabe: Aktivieren, Profil ersetzen und Deaktivieren erzwingen jeweils
    # eine Neuberechnung, obwohl sich der Job und die Separation nicht aendern.
    # Parameter/Rueckgabe: Keine.
    def test_icc_refresh(self):
        self.png("one", 100)
        job = make_job({"K": {"reference_name": "one"}}, ["result.png"])
        synchronize_jobs(self.pii, [job])
        self.assertEqual(self.drain(), ["done"])
        profile = self.configure_icc()
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(self.drain(), [])
        replacement = profile.with_suffix(".tmp")
        shutil.copyfile(profile, replacement)
        os.replace(replacement, profile)
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(self.drain(), [])
        self.config.settings["composite"]["icc_enabled"] = False
        self.assertEqual(self.drain(), ["done"])
        self.assertEqual(self.pixel("result.png"), (100, 100, 100))

    # Aufgabe: Profilwechsel waehrend des Renderns darf nicht publiziert werden.
    # Parameter/Rueckgabe: Keine.
    def test_icc_change_during_render(self):
        profile = self.configure_icc()
        self.png("one", 100)
        job = make_job({"K": {"reference_name": "one"}}, ["result.png"])
        synchronize_jobs(self.pii, [job])
        claim = claim_next(self.config, self.pii, LOGGER)
        self.addCleanup(claim[1].close)
        real_render = job_processor.create_composite
        # Aufgabe: Simuliert atomaren Profilersatz nach der Berechnung.
        # Parameter: *args, **kwargs des Renderers. Rueckgabe: PNG-Pfad (Path).
        def render_and_replace(*args, **kwargs):
            result = real_render(*args, **kwargs)
            replacement = profile.with_suffix(".tmp")
            shutil.copyfile(profile, replacement)
            os.replace(replacement, profile)
            return result
        with patch.object(job_processor, "create_composite", side_effect=render_and_replace):
            self.assertEqual(process_claim(self.config, self.pii, claim, LOGGER), "pending")
        self.assertFalse((self.config.paths.composite_png / "result.png").exists())
        self.assertEqual(self.drain(), ["done"])

    # Aufgabe: Ungeeignetes RGB-Profil darf nicht als CMYK-Druckprofil dienen.
    # Parameter/Rueckgabe: Keine.
    def test_icc_invalid_profile(self):
        profile = self.configure_icc(False)
        profile.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        self.png("one", 100)
        synchronize_jobs(self.pii, [make_job({"K": {"reference_name": "one"}}, ["bad.png"])])
        self.assertEqual(self.drain(), ["failed"])
        self.assertFalse((self.config.paths.composite_png / "bad.png").exists())

    # Aufgabe: Ein fehlendes Profil eines Jobs darf andere nicht blockieren;
    #          bereits publiziertes Composite bleibt bei Fehler unangetastet.
    # Parameter/Rueckgabe: Keine.
    def test_icc_missing_profile_preserves_result(self):
        self.png("one", 100)
        job = make_job({"K": {"reference_name": "one", "icc_profile": "missing.icc"}}, ["result.png"])
        synchronize_jobs(self.pii, [job])
        self.assertEqual(self.drain(), ["done"])
        output = self.config.paths.composite_png / "result.png"
        old = output.read_bytes()
        self.configure_icc(False)
        self.assertEqual(self.drain(), ["failed"])
        self.assertEqual(output.read_bytes(), old)

    # Aufgabe: Absolute farbmetrische Transformation bei Papiersimulation.
    # Parameter/Rueckgabe: Keine.
    def test_icc_paper_simulation(self):
        profile = self.configure_icc()
        self.png("one", 255)
        job = make_job({"K": {"reference_name": "one"}}, ["white.png"])
        synchronize_jobs(self.pii, [job])
        self.assertEqual(self.drain(), ["done"])
        self.config.settings["composite"]["simulate_paper_color"] = True
        self.assertEqual(self.drain(), ["done"])
        with Image.new("CMYK", (1, 1), (0, 0, 0, 0)) as reference:
            with ImageCms.profileToProfile(reference, str(profile), ImageCms.createProfile("sRGB"),
                                           renderingIntent=ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
                                           outputMode="RGB") as rgb:
                self.assertEqual(self.pixel("white.png"), rgb.getpixel((0, 0)))

    # Aufgabe: Prueft das regulaere Beenden von zwei Worker-Prozessen
    #          mit echtem SIGTERM/SIGINT. Eigene Linux-Prozessgruppe
    #          verhindert Auswirkungen auf den Testprozess.
    # Parameter: group (bool) - Signal an alle Prozesse oder nur an
    #            den Elternprozess; signum (int) - Linux-Signal;
    #            active (bool) - einen laufenden Job regulaer abschliessen.
    # Rueckgabe: None.
    def check_shutdown(self, group, signum, active=False):
        root = Path(self.config.settings["server"]["root_dir"]) / "shutdown"
        root.mkdir()
        script = (
            "from pathlib import Path; "
            "from test_job_processor import make_config; "
            "from job_processor import run_workers; "
            "import sys; "
            "config, pii = make_config(Path(sys.argv[1])); "
        )
        if active:
            script += """
import time
import job_processor
from PIL import Image
from test_job_processor import make_job
from job_manager import synchronize_jobs
with Image.new("L", (8, 6), 120) as image:
    image.save(config.paths.separation_png / "source.png")
synchronize_jobs(pii, [make_job({"K": {"reference_name": "source"}}, ["result.png"])])
original = job_processor.create_composite
# Simuliert eine laufende Bildberechnung.
def slow_render(*args, **kwargs):
    (Path(sys.argv[1]) / "rendering").touch()
    time.sleep(1.2)
    return original(*args, **kwargs)
job_processor.create_composite = slow_render
"""
        script += "\nraise SystemExit(run_workers(config, pii, 2, False, 30))"
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(root)],
            cwd=Path(__file__).resolve().parent,
            start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            logs = [root / "logs" / f"job_processor-{n}.log" for n in (1, 2)]
            deadline = time.monotonic() + 10
            while (not all(log.exists() and "started" in log.read_text() for log in logs)
                   or (active and not (root / "rendering").exists())):
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail("Worker startup failed or timed out")
                time.sleep(0.05)
            # Beide Worker sollen bereits im langen Scan-Intervall warten.
            time.sleep(0.2)
            begin = time.monotonic()
            if group:
                os.killpg(process.pid, signum)
            else:
                process.send_signal(signum)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertLess(time.monotonic() - begin, 3)
            for log in logs:
                self.assertIn("stopped", log.read_text())
            if active:
                self.assertTrue((root / "composites" / "result.png").is_file())
                self.assertEqual(len(list((root / "jobs/done").glob("*.job"))), 1)
                self.assertEqual(len(list((root / "jobs/running").glob("*.job"))), 0)
        finally:
            # Auch bei fehlgeschlagenem Test keine Prozesse zuruecklassen.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)

    # Aufgabe: Simuliert systemd KillMode=control-group (SIGTERM an alle).
    # Parameter/Rueckgabe: Keine.
    def test_shutdown_sigterm_group(self):
        self.check_shutdown(True, signal.SIGTERM)

    # Aufgabe: SIGTERM nur an den Hauptprozess muss beide Worker beenden.
    # Parameter/Rueckgabe: Keine.
    def test_shutdown_sigterm_parent(self):
        self.check_shutdown(False, signal.SIGTERM)

    # Aufgabe: Ein bereits laufender Job soll bei SIGTERM fertig werden.
    # Parameter/Rueckgabe: Keine.
    def test_shutdown_active_job(self):
        self.check_shutdown(True, signal.SIGTERM, active=True)

    # Aufgabe: Ctrl+C muss ebenfalls alle Worker regulaer beenden.
    # Parameter/Rueckgabe: Keine.
    def test_shutdown_sigint_group(self):
        self.check_shutdown(True, signal.SIGINT)

    # Aufgabe: Zwei echte Worker-Prozesse arbeiten dieselbe Queue ab.
    # Parameter/Rueckgabe: Keine.
    def test_parallel_workers(self):
        jobs = []
        for i in range(24):
            self.png(str(i), i, size=(128, 128))
            jobs.append(make_job({"K": {"reference_name": str(i)}}, [f"{i}.png"]))
        synchronize_jobs(self.pii, jobs)
        self.assertEqual(run_workers(self.config, self.pii, 2, True, 0.01), 0)
        self.assertEqual(len(list(self.paths["done"].glob("*.job"))), 24)
        self.assertEqual(len(list(self.paths["pending"].glob("*.job"))), 0)
        self.assertEqual(len(list(self.paths["running"].glob("*.job"))), 0)
        # Job Manager muss unveraenderte done-Auftraege weiterhin erkennen.
        self.assertEqual(synchronize_jobs(self.pii, jobs)["done"], 24)
        for i in range(24):
            self.assertEqual(self.pixel(f"{i}.png"), (i, i, i))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Isolierte Job Processor Tests")
    parser.add_argument("--icc-profile", type=Path, help="Echtes CMYK-Profil fuer ICC-Tests")
    args, remaining = parser.parse_known_args()
    if args.icc_profile is not None:
        TEST_PROFILE = args.icc_profile.resolve()
        if not TEST_PROFILE.is_file():
            parser.error(f"ICC profile not found: {TEST_PROFILE}")
        profile = ImageCms.getOpenProfile(str(TEST_PROFILE))
        if profile.profile.xcolor_space.strip() != "CMYK":
            parser.error(f"Not a CMYK profile: {TEST_PROFILE}")
    unittest.main(argv=[__file__, *remaining], verbosity=2)

