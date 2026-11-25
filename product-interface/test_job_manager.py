# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: test_job_manager.py
#
# BESCHREIBUNG:
#   Ohne Parameter: automatische Tests in temporaeren Verzeichnissen.
#   Mit Produkt-JSONs und --output: echter Abgleich einer isolierten Testqueue.
#   Keine Verbindung zum Produktserver und keine Bildverarbeitung.
# =============================================================================
import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from job_manager import (
    get_queue_paths, get_job_state_file, job_is_current, load_active_products,
    load_manifest, queue_lock, read_json, synchronize_jobs,
)


# =============================================================================
# FUNKTION: make_job
# Aufgabe: Erzeugt ein synthetisches Bildrezept im Format des Job Creators.
# Uebergabeparameter: colors (str); outputs (tuple[str]) - Zielbilder.
# Rueckgabewert: dict - synthetischer Auftrag ohne Revision.
# =============================================================================
def make_job(colors="CMYK", outputs=("1.1.2.0.png", "1.1.2.1.png")):
    separations = {c: {"reference_name": f"R{c}", "content_id": f"ID{c}",
                       "icc_profile": "test.icc"} for c in colors}
    recipe = {"separations": separations}
    digest = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
    return dict(schema_version=1, job_id=digest, separations=separations,
                separation_count=len(colors), composites=list(outputs), sources=[])


class JobManagerTests(unittest.TestCase):
    # =========================================================================
    # FUNKTION: setUp
    # Aufgabe: Erstellt eine isolierte Queue fuer jeden Test.
    # Uebergabeparameter: self - Testinstanz.
    # Rueckgabewert: None.
    # =========================================================================
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="imgsrv-job-manager-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {"job_creation": {"output_directory": str(self.root / "pending")}}
        self.paths = get_queue_paths(self.config)

    # =========================================================================
    # FUNKTION: current
    # Aufgabe: Liest einen Auftrag aus dem aktuellen Manifest.
    # Uebergabeparameter: self; job (dict) - Rezept ohne Revision.
    # Rueckgabewert: dict - Auftrag inklusive Revision.
    # =========================================================================
    def current(self, job):
        return load_manifest(self.paths["manifest"])[job["job_id"]]

    # =========================================================================
    # FUNKTION: test_new_unchanged_and_recovery
    # Aufgabe: Erstabgleich, identischer Wiederholungsabgleich, fehlende Datei.
    # Uebergabeparameter: self - Testinstanz. Rueckgabewert: None.
    # =========================================================================
    def test_new_unchanged_and_recovery(self):
        job = make_job()
        stats = synchronize_jobs(self.config, [job])
        self.assertEqual((stats["new"], stats["pending_written"]), (1, 1))
        current = self.current(job)
        path = get_job_state_file(self.config, "pending", current)
        before = path.stat().st_mtime_ns
        manifest_before = self.paths["manifest"].stat().st_mtime_ns
        stats = synchronize_jobs(self.config, [job])
        self.assertEqual((stats["pending_written"], stats["pending_unchanged"]), (0, 1))
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(self.paths["manifest"].stat().st_mtime_ns, manifest_before)
        path.unlink()
        self.assertEqual(synchronize_jobs(self.config, [job])["pending_written"], 1)
        self.assertEqual(read_json(path), current)

    # =========================================================================
    # FUNKTION: test_color_change_and_old_worker
    # Aufgabe: Simuliert 4C -> 1C -> 4C mit einem noch laufenden alten Auftrag.
    # Uebergabeparameter: self - Testinstanz. Rueckgabewert: None.
    # =========================================================================
    def test_color_change_and_old_worker(self):
        four, one = make_job(), make_job("K")
        synchronize_jobs(self.config, [four])
        old = self.current(four)
        running = get_job_state_file(self.config, "running", old)
        with queue_lock(self.config):
            os.rename(get_job_state_file(self.config, "pending", old), running)
            self.assertTrue(job_is_current(self.config, old))
        self.assertEqual(synchronize_jobs(self.config, [four])["running"], 1)
        stats = synchronize_jobs(self.config, [one])
        self.assertEqual((stats["new"], stats["obsolete"]), (1, 1))
        with queue_lock(self.config):
            self.assertFalse(job_is_current(self.config, old))
            self.assertTrue(job_is_current(self.config, self.current(one)))
        self.assertTrue(running.exists())
        synchronize_jobs(self.config, [four])
        new = self.current(four)
        self.assertNotEqual(new["revision"], old["revision"])
        with queue_lock(self.config):
            self.assertFalse(job_is_current(self.config, old))
            self.assertTrue(job_is_current(self.config, new))
        self.assertEqual(len(list(self.paths["pending"].glob("*.job"))), 1)

    # =========================================================================
    # FUNKTION: test_changed_outputs
    # Aufgabe: Gleiche Rezept-ID mit geaenderter Liste der Zielbilder abgleichen.
    # Uebergabeparameter: self - Testinstanz. Rueckgabewert: None.
    # =========================================================================
    def test_changed_outputs(self):
        first = make_job()
        synchronize_jobs(self.config, [first])
        old = self.current(first)
        changed = make_job(outputs=("1.1.2.0.png",))
        self.assertEqual(first["job_id"], changed["job_id"])
        stats = synchronize_jobs(self.config, [changed])
        self.assertEqual((stats["changed"], stats["pending_written"]), (1, 1))
        self.assertNotEqual(self.current(changed)["revision"], old["revision"])
        with queue_lock(self.config):
            self.assertFalse(job_is_current(self.config, old))

    # =========================================================================
    # FUNKTION: test_done_failed_and_empty_list
    # Aufgabe: Abgeschlossene/fehlgeschlagene Jobs nicht erneut einstellen;
    #          eine gueltige leere Produktliste entfernt den Sollbestand.
    # Uebergabeparameter: self - Testinstanz. Rueckgabewert: None.
    # =========================================================================
    def test_done_failed_and_empty_list(self):
        jobs = [make_job("K", ("1.png",)), make_job("CMYK", ("2.png",)),
                make_job("CK", ("3.png",))]
        synchronize_jobs(self.config, jobs)
        for job, state in zip(jobs, ("done", "failed")):
            current = self.current(job)
            with queue_lock(self.config):
                os.rename(get_job_state_file(self.config, "pending", current),
                          get_job_state_file(self.config, state, current))
        stats = synchronize_jobs(self.config, jobs)
        self.assertEqual((stats["done"], stats["failed"], stats["pending_written"]), (1, 1, 0))
        foreign = self.paths["pending"] / "foreign.job"
        foreign.write_text("not managed")
        stats = synchronize_jobs(self.config, [])
        self.assertEqual((stats["obsolete"], stats["pending_removed"]), (3, 1))
        self.assertTrue(foreign.exists())
        self.assertEqual(load_manifest(self.paths["manifest"]), {})

    # =========================================================================
    # FUNKTION: test_conflicts_and_corrupt_manifest
    # Aufgabe: Ungueltige Auftraege und defektes Manifest nicht ueberschreiben.
    # Uebergabeparameter: self - Testinstanz. Rueckgabewert: None.
    # =========================================================================
    def test_conflicts_and_corrupt_manifest(self):
        job = make_job()
        synchronize_jobs(self.config, [job])
        before = self.paths["manifest"].read_bytes()
        with self.assertRaises(ValueError):
            synchronize_jobs(self.config, [job, make_job("K")])
        self.assertEqual(self.paths["manifest"].read_bytes(), before)
        self.paths["manifest"].write_text("broken json")
        with self.assertRaises(ValueError):
            synchronize_jobs(self.config, [job])
        self.assertEqual(self.paths["manifest"].read_text(), "broken json")

    # =========================================================================
    # FUNKTION: test_process_lock
    # Aufgabe: Prueft mit zweitem Python-Prozess, dass flock exklusiv wirkt.
    # Uebergabeparameter: self - Testinstanz. Rueckgabewert: None.
    # =========================================================================
    def test_process_lock(self):
        program = """import fcntl, sys
with open(sys.argv[1], 'a') as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(3)
"""
        with queue_lock(self.config):
            result = subprocess.run([sys.executable, "-c", program, str(self.paths["lock"])], timeout=5)
            self.assertEqual(result.returncode, 3)
        result = subprocess.run([sys.executable, "-c", program, str(self.paths["lock"])], timeout=5)
        self.assertEqual(result.returncode, 0)

    # =========================================================================
    # FUNKTION: test_active_products
    # Aufgabe: Nur aktive JSONs einlesen; fehlende/veraltete Daten ablehnen.
    # Uebergabeparameter: self - Testinstanz. Rueckgabewert: None.
    # =========================================================================
    def test_active_products(self):
        self.config.update(directories={"products": str(self.root)},
                           product_details={"response_prefix": "piians"},
                           product_cache={"filename": str(self.root / "product_cache.json")})
        record = {"product_name": "A B", "product_update": "202611251413"}
        path = self.root / "AB.json"
        path.write_text(json.dumps(dict(record, traces={"1": {}})))
        (self.root / "inactive.json").write_text("ignored")
        # Der produktive Request Sender wird fuer diesen isolierten Test ersetzt.
        sender = types.ModuleType("request_sender")
        sender.get_product_detail_filenames = lambda config, name: ("piireqAB.pif", "piiansAB.pif")
        with patch.dict(sys.modules, {"request_sender": sender}):
            self.assertEqual(len(load_active_products(self.config, [record])), 1)
            with self.assertRaises(ValueError):
                load_active_products(self.config, [dict(record, product_update="202611251414")])
            path.unlink()
            with self.assertRaises(ValueError):
                load_active_products(self.config, [record])


# =============================================================================
# FUNKTION: main
# Aufgabe: Startet die Regressionstests oder den Abgleich realer Produkt-JSONs
#          in einer explizit angegebenen Testqueue (nicht die produktive Queue).
# Uebergabeparameter: Keine direkten; Argumente ueber argparse.
# Rueckgabewert: int - 0 bei Erfolg, 1/2 bei Fehler.
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Test ImageServer Job Manager")
    parser.add_argument("files", nargs="*", type=Path, help="Product JSON files")
    parser.add_argument("--output", type=Path, help="Isolated test queue root (not pending directory)")
    args = parser.parse_args()
    if not args.files:
        if args.output:
            parser.error("--output requires product JSON files")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(JobManagerTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    if args.output is None:
        parser.error("Product JSON mode requires an explicit --output test directory")
    try:
        # Der Echtdatei-Test nutzt den vorhandenen Job Creator unveraendert.
        from job_creator import create_jobs
        root = args.output.resolve()
        production = Path("/pecom/imgsrv/jobs").resolve()
        if root == production or production in root.parents or root in production.parents:
            raise ValueError("Use an isolated test queue, e.g. /tmp/imgsrv-test-manager")
        config = {"job_creation": {"output_directory": str(root / "pending")}}
        products = [read_json(path) for path in args.files]
        if any(not p or "product_name" not in p or "traces" not in p for p in products):
            raise ValueError("Invalid product JSON (do not include product_cache.json)")
        jobs = create_jobs(products)
        stats = synchronize_jobs(config, jobs)
        print(f"Test queue: {root}")
        for name, value in stats.items():
            print(f"{name:20}: {value}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

