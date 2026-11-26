#!/usr/bin/env python3
# =============================================================================
# ImageServer V2 - JOB PROCESSOR
# =============================================================================
# Verarbeitet Bildauftraege aus der gemeinsamen dateibasierten Warteschlange.
# Die Product Interface Supervisor bleibt fuer den Sollbestand zustaendig.
# Mehrere Prozesse koennen unabhaengig Bilder berechnen. Die gemeinsame
# Queue-Sperre wird nur fuer Zustandswechsel und Veroeffentlichung gehalten.
# Fehlende Separationen lassen einen Auftrag in pending; andere Auftraege
# werden trotzdem bearbeitet. Keine Datenbank und keine TIFF-Verarbeitung.
# =============================================================================

import argparse
import fcntl
import logging
import multiprocessing
import os
import re
import signal
import stat
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

if __package__:
    from .composite import create_composite, validate_settings, resolve_icc_profile
else:
    from composite import create_composite, validate_settings, resolve_icc_profile

# Gemeinsame Queue-Funktionen NICHT kopieren: Der Product Interface Manager
# und alle Worker muessen dieselbe Sperre und dasselbe Dateiformat verwenden.
interface_dir = Path(__file__).resolve().parents[1] / "product-interface"
if interface_dir.is_dir():
    sys.path.insert(0, str(interface_dir))
from job_manager import (JOB_ID, REVISION, get_queue_paths, queue_lock,
                         get_job_state_file, load_manifest, read_json,
                         sync_directory, write_json_atomic, job_is_current)

BASENAME = re.compile(r"[A-Za-z0-9._-]+")


# =============================================================================
# FUNKTION: validate_config
# Aufgabe: Prueft die gemeinsamen Queue-Pfade und die Render-Konfiguration.
# Parameter: config - ImageServerConfig; pii (dict) - Product Interface YAML.
# Rueckgabewert: None. Fehler: ValueError bei widerspruechlicher Konfiguration.
# =============================================================================
def validate_config(config, pii):
    root = Path(config.settings["server"]["root_dir"])
    paths = get_queue_paths(pii)
    for state, key in (("pending", "pending"), ("running", "processing"),
                       ("done", "done"), ("failed", "failed")):
        configured = (root / config.settings["jobs"][key]).resolve()
        if paths[state] != configured:
            raise ValueError(f"Queue mismatch: jobs.{key}={configured}, "
                             f"Product Interface={paths[state]}")
        paths[state].mkdir(parents=True, exist_ok=True)
    if len({paths[s].stat().st_dev for s in ("pending", "running", "done", "failed")}) != 1:
        raise ValueError("Queue directories must share one filesystem")
    if config.paths.separation_png.resolve() == config.paths.composite_png.resolve():
        raise ValueError("Separation and composite directories must differ")
    validate_settings(config)


# =============================================================================
# FUNKTION: resolve_inputs
# Aufgabe: Findet fuer jede benoetigte Farbe ReferenceName.png oder alternativ
#          ContentID.png. Erzeugt einen Dateistempel zur Aenderungserkennung.
# Parameter: job (dict); directory (Path) - Separationsverzeichnis.
# Rueckgabewert: tuple[dict, dict] (Dateien, Dateistempel) oder None, solange
#                mindestens eine benoetigte Separation fehlt.
# =============================================================================
def resolve_inputs(job, directory):
    files, signatures = {}, {}
    for color, separation in job["separations"].items():
        # Unterstuetzt auch die kurzen Feldnamen aelterer Parser-Versionen.
        aliases = []
        for group in (("reference_name", "reference", "ref", "ref_name", "ReferenceName"),
                      ("content_id", "content", "ContentID")):
            values = {str(separation[k]) for k in group
                      if separation.get(k) is not None and separation.get(k) != ""}
            if len(values) > 1:
                raise ValueError(f"Conflicting aliases for separation {color}")
            aliases.extend(values)
        if not aliases:
            raise ValueError(f"No ReferenceName or ContentID for separation {color}")
        for name in aliases:
            if not BASENAME.fullmatch(name) or name in (".", ".."):
                raise ValueError(f"Unsafe separation name: {name!r}")
            path = directory / (name + ".png")
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Not a regular file: {path}")
            files[color] = path
            signatures[color] = [path.name, info.st_dev, info.st_ino,
                                 info.st_size, info.st_mtime_ns, info.st_ctime_ns]
            break
        else:
            return None
    return files, signatures


# =============================================================================
# FUNKTION: move_state
# Aufgabe: Verschiebt eine Jobdatei atomar ohne bestehende Jobs zu ersetzen.
# Parameter: source, target (Path).
# Rueckgabewert: None. Aufrufer muss die gemeinsame Queue-Sperre halten.
# =============================================================================
def move_state(source, target):
    if target.exists():
        raise FileExistsError(target)
    os.rename(source, target)
    sync_directory(source.parent)
    sync_directory(target.parent)


# =============================================================================
# FUNKTION: receipt_path
# Aufgabe: Bestimmt die separate Datei mit den verwendeten Eingabedateistempeln.
# Parameter: done_file (Path) - unveraenderte Jobdatei in done.
# Rueckgabewert: Path - Ergebnisprotokoll; keine Aenderung am Jobschema.
# =============================================================================
def receipt_path(done_file):
    return done_file.with_suffix(".receipt.json")


# =============================================================================
# FUNKTION: render_settings
# Aufgabe: Liefert die bildbestimmenden Einstellungen fuer die Aenderungspruefung.
#          Bei ICC zusaetzlich Profilpfad und Dateistempel speichern. Aenderung
#          des Profils oder Aktivierung von ICC invalidiert alte Ergebnisse.
# Parameter: config - ImageServerConfig; job (dict) - Auftrag mit Separationen.
# Rueckgabewert: dict - Renderparameter, bei ICC mit Profilidentitaet.
# =============================================================================
def render_settings(config, job):
    width, height = validate_settings(config)
    result = {"renderer": 1, "max_width": width, "max_height": height}
    profile = resolve_icc_profile(config, job["separations"])
    if profile is not None:
        info = profile.stat()
        result["icc_profile"] = {
            "path": str(profile),
            "signature": [info.st_dev, info.st_ino, info.st_size,
                          info.st_mtime_ns, info.st_ctime_ns],
            "simulate_paper_color": config.settings["composite"].get("simulate_paper_color", False),
        }
    return result


# =============================================================================
# FUNKTION: recover_running
# Aufgabe: Stellt abgebrochene Jobs wieder her. Jeder aktive Worker haelt
#          zusaetzlich eine flock-Sperre auf seiner Jobdatei. Nach SIGKILL
#          oder Prozessabsturz gibt Linux diese Sperre automatisch frei.
# Parameter: pii (dict); manifest (dict); logger.
# Rueckgabewert: None. Aufrufer muss queue_lock halten.
# =============================================================================
def recover_running(pii, manifest, logger):
    for path in get_queue_paths(pii)["running"].glob("*.job"):
        parts = path.stem.split(".")
        if len(parts) != 2 or not JOB_ID.fullmatch(parts[0]) or not REVISION.fullmatch(parts[1]):
            continue
        with path.open("rb") as lease:
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            job = read_json(path)
            if (job is None or job.get("job_id") != parts[0]
                    or job.get("revision") != parts[1]):
                raise ValueError(f"Invalid running job: {path}")
            if manifest.get(parts[0]) != job:
                path.unlink()
                sync_directory(path.parent)
                logger.info("Obsolete interrupted job removed: %s", path.name)
                continue
            # Manager und Worker duerfen keine doppelten Zustaende hinterlassen.
            if any(get_job_state_file(pii, s, job).exists() for s in ("done", "failed", "pending")):
                raise ValueError(f"Duplicate state for interrupted job: {path}")
            move_state(path, get_job_state_file(pii, "pending", job))
            logger.warning("Interrupted job returned to pending: %s", path.name)


# =============================================================================
# FUNKTION: refresh_done
# Aufgabe: Plant fertige aktuelle Jobs erneut ein, wenn sich ein Eingabe-PNG
#          oder Renderparameter geaendert haben oder ein Ausgabe-PNG fehlt.
#          Alte Revisionen werden nicht reaktiviert. Fehlende Quellen allein
#          invalidieren ein fertiges Bild nicht (z.B. nach Archivierung).
# Parameter: config, pii (dict), manifest (dict), logger.
# Rueckgabewert: None. Aufrufer muss queue_lock halten.
# =============================================================================
def refresh_done(config, pii, manifest, logger):
    for job in manifest.values():
        done = get_job_state_file(pii, "done", job)
        if not done.exists():
            continue
        if read_json(done) != job:
            raise ValueError(f"Invalid done job: {done}")
        record = read_json(receipt_path(done))
        try:
            inputs = resolve_inputs(job, config.paths.separation_png)
        except (ValueError, OSError):
            logger.exception("Cannot inspect sources of completed job %s", job["job_id"])
            inputs = None
        missing = any(not (config.paths.composite_png / name).is_file()
                      for name in job["composites"])
        changed = False
        if inputs is not None:
            try:
                changed = (record is None or record.get("inputs") != inputs[1]
                           or record.get("render_settings") != render_settings(config, job))
            except (ValueError, OSError):
                # Fehlerhafte / fehlende Profile duerfen den Queue-Scan nicht
                # abbrechen. Job erneut einplanen; der Worker meldet den
                # konkreten Fehler und belaesst ein altes PNG unangetastet.
                changed = True
        if missing or changed:
            if any(get_job_state_file(pii, s, job).exists() for s in ("pending", "running", "failed")):
                raise ValueError(f"Duplicate state for done job: {done}")
            move_state(done, get_job_state_file(pii, "pending", job))
            receipt_path(done).unlink(missing_ok=True)
            logger.info("Composite requires update: %s", job["job_id"])


# =============================================================================
# FUNKTION: claim_next
# Aufgabe: Sucht einen vollstaendig vorbereiteten aktuellen Auftrag, sperrt
#          dessen Datei und verschiebt ihn von pending nach running.
# Parameter: config, pii (dict), logger.
# Rueckgabewert: tuple(job, lease, files, signatures) oder None.
#                Der Aufrufer muss lease nach der Verarbeitung schliessen.
# =============================================================================
def claim_next(config, pii, logger):
    paths = get_queue_paths(pii)
    with queue_lock(pii):
        if not paths["manifest"].exists():
            return None
        manifest = load_manifest(paths["manifest"])
        recover_running(pii, manifest, logger)
        refresh_done(config, pii, manifest, logger)
        for path in sorted(paths["pending"].glob("*.job")):
            job = manifest.get(path.stem)
            if job is None or read_json(path) != job:
                continue
            if any(get_job_state_file(pii, s, job).exists() for s in ("running", "done", "failed")):
                raise ValueError(f"Duplicate state for pending job: {path}")
            try:
                inputs = resolve_inputs(job, config.paths.separation_png)
            except (ValueError, OSError):
                logger.exception("Invalid inputs for %s", path.name)
                # Ueber normale Verarbeitung in failed ueberfuehren,
                # damit auch --once einen Fehlercode zurueckgibt.
                inputs = ({}, {})
            if inputs is None:
                continue
            lease = path.open("rb")
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                move_state(path, get_job_state_file(pii, "running", job))
            except BaseException:
                lease.close()
                raise
            logger.info("Job claimed: %s (%dC)", job["job_id"], job["separation_count"])
            return job, lease, *inputs
    return None


# =============================================================================
# FUNKTION: process_claim
# Aufgabe: Berechnet ein Bild ausserhalb der Queue-Sperre und veroeffentlicht
#          alle Ausgaben erst nach erneuter Manifest- und Quellenpruefung.
# Parameter: config, pii (dict), claim (tuple aus claim_next), logger.
# Rueckgabewert: str - done, pending, obsolete oder failed.
# Hinweis: Die einzelnen PNG-Dateien werden atomar ersetzt, nicht der gesamte
#          Satz. Nach einem Absturz wird die laufende Revision neu berechnet.
# =============================================================================
def process_claim(config, pii, claim, logger):
    job, lease, files, signature = claim
    running = get_job_state_file(pii, "running", job)
    output_dir = config.paths.composite_png
    render_options = None
    try:
        render_options = render_settings(config, job)
        profile_options = render_options.get("icc_profile")
        profile = Path(profile_options["path"]) if profile_options else None
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".job-render-", dir=output_dir) as tmp:
            stage = Path(tmp)
            rendered = create_composite(files, stage / "rendered.tmp", config, logger,
                                        icc_profile=profile)
            # Einmal berechnen, weitere Dateinamen als Hardlinks vorbereiten.
            # Alle Dateien liegen auf demselben Dateisystem.
            for name in job["composites"]:
                os.link(rendered, stage / name)
            with queue_lock(pii):
                if not job_is_current(pii, job):
                    running.unlink()
                    sync_directory(running.parent)
                    logger.info("Obsolete result discarded: %s", job["job_id"])
                    return "obsolete"
                current = resolve_inputs(job, config.paths.separation_png)
                if (current is None or current[1] != signature
                        or render_settings(config, job) != render_options):
                    move_state(running, get_job_state_file(pii, "pending", job))
                    logger.info("Inputs or ICC profile changed during render; job returned to pending: %s", job["job_id"])
                    return "pending"
                for name in job["composites"]:
                    os.replace(stage / name, output_dir / name)
                sync_directory(output_dir)
                done = get_job_state_file(pii, "done", job)
                write_json_atomic(receipt_path(done), {"inputs": signature,
                                                       "render_settings": render_options})
                move_state(running, done)
                logger.info("Job completed: %s (%d composites)", job["job_id"], len(job["composites"]))
                return "done"
    except Exception:
        logger.exception("Job processing failed: %s", job["job_id"])
        with queue_lock(pii):
            if not running.exists():
                raise
            if not job_is_current(pii, job):
                running.unlink()
                sync_directory(running.parent)
                return "obsolete"
            # Wenn der Folder Watcher gerade ein PNG ersetzt hat, ist der
            # Fehler voruebergehend. Nicht als dauerhaft fehlgeschlagen werten.
            try:
                current = resolve_inputs(job, config.paths.separation_png)
                changed = current is None or current[1] != signature
                if not changed and render_options is not None:
                    changed = render_settings(config, job) != render_options
            except (OSError, ValueError):
                changed = False
            target = "pending" if changed else "failed"
            move_state(running, get_job_state_file(pii, target, job))
            return target
    finally:
        lease.close()


# =============================================================================
# FUNKTION: setup_logger
# Aufgabe: Richtet eigenes rotierendes Log pro Worker ein; keine konkurrierende
#          Rotation derselben Datei durch verschiedene Worker eines Starts.
# Parameter: config - ImageServerConfig; worker_id (int).
# Rueckgabewert: logging.Logger.
# =============================================================================
def setup_logger(config, worker_id):
    settings = config.settings["logging"]
    directory = Path(config.paths.logs)
    directory.mkdir(parents=True, exist_ok=True)
    base = Path(settings.get("files", {}).get("job_processor", "job_processor.log"))
    filename = base.with_name(f"{base.stem}-{worker_id}{base.suffix}")
    logger = logging.getLogger(f"imageserver.job_processor.{worker_id}")
    logger.setLevel(settings.get("level", "INFO"))
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s - %(levelname)-8s - %(name)s:%(lineno)d - %(message)s")
    handler = RotatingFileHandler(directory / filename,
                                  maxBytes=int(settings.get("max_size_mb", 10)) * 1024 * 1024,
                                  backupCount=int(settings.get("backup_count", 5)),
                                  encoding="utf-8")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    if settings.get("console_output", True):
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


# =============================================================================
# FUNKTION: wait_for_next_scan
# Aufgabe: Wartet bis zum naechsten Scan oder bis ein Stop angefordert wird.
#          Kurze Schlafintervalle erlauben schnelles Beenden auch bei langen
#          konfigurierten Scanintervallen. Keine Semaphore / kein Event.
# Parameter: should_stop (Callable[[], bool]), interval (float, Sekunden).
# Rueckgabewert: None.
# =============================================================================
def wait_for_next_scan(should_stop, interval):
    deadline = time.monotonic() + interval
    while not should_stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.2))


# =============================================================================
# FUNKTION: worker_loop
# Aufgabe: Bearbeitet vorbereitete Auftraege, ohne auf fehlende Farben zu
#          blockieren. Beendet sich bei --once nach dem letzten bereiten Job.
#          Bei SIGTERM/SIGINT den bereits uebernommenen Auftrag beenden,
#          danach keinen neuen Auftrag mehr uebernehmen.
# Parameter: config, pii, worker_id (int), stop (gemeinsames RawValue),
#            once (bool), interval (float), optional logger.
# Rueckgabewert: int - 0 bei Erfolg, 1 falls ein Job fehlgeschlagen ist.
# =============================================================================
def worker_loop(config, pii, worker_id, stop, once, interval, logger=None):
    stop_requested = False

    # Aufgabe: Lokale Stop-Anforderung setzen. Keine Locks, kein Logging
    #          und kein multiprocessing.Event.set() im Signalhandler!
    # Parameter: signum (int), frame (Frame oder None). Rueckgabe: None.
    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    # Aufgabe: Lokale und gemeinsame Stop-Anforderung auswerten.
    # Parameter: Keine. Rueckgabe: bool.
    def should_stop():
        return stop_requested or bool(stop.value)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if logger is None:
        logger = setup_logger(config, worker_id)
    logger.info("Job Processor %d started", worker_id)
    failed = False
    while not should_stop():
        claim = claim_next(config, pii, logger)
        if claim is None:
            if once:
                break
            wait_for_next_scan(should_stop, interval)
            continue
        # Falls das Stop-Signal waehrend der Uebernahme ankommt, ist die
        # Job-Lease bereits aktiv. Diesen Auftrag regulaer abschliessen.
        result = process_claim(config, pii, claim, logger)
        failed |= result == "failed"
        if result == "pending":
            wait_for_next_scan(should_stop, interval)
    logger.info("Job Processor %d stopped", worker_id)
    return int(failed)


# =============================================================================
# FUNKTION: worker_entry
# Aufgabe: Prozess-Einstieg; uebergibt den Rueckgabecode an den Elternprozess.
# Parameter: *args - Parameter von worker_loop.
# Rueckgabewert: None (Prozess endet mit SystemExit).
# =============================================================================
def worker_entry(*args):
    raise SystemExit(worker_loop(*args))


# =============================================================================
# FUNKTION: run_workers
# Aufgabe: Startet die konfigurierten unabhaengigen Linux-Prozesse und wartet
#          auf deren Ende. Bei SIGTERM keine neuen Auftraege mehr uebernehmen.
#          Der Elternprozess signalisiert den Stop ausserhalb des Handlers
#          ueber ein gemeinsames, sperrfreies Byte (nur 0 -> 1).
# Parameter: config, pii, workers (int), once (bool), interval (float).
# Rueckgabewert: int - 0 bei Erfolg, sonst 1.
# =============================================================================
def run_workers(config, pii, workers, once, interval):
    context = multiprocessing.get_context("fork")
    # Ein Byte im gemeinsamen Speicher. Nur der Elternprozess schreibt
    # einmalig 1 fuer Stop; Worker lesen. Keine Lock-Abhaengigkeit von
    # anderen Prozessen, auch wenn ein Worker unerwartet abstuerzt.
    stop = context.RawValue("b", 0)
    stop_requested = False

    # Aufgabe: Signal nur in lokale Variable uebernehmen.
    # Parameter: signum (int), frame (Frame oder None). Rueckgabe: None.
    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    processes = [context.Process(target=worker_entry,
                                args=(config, pii, number, stop, once, interval),
                                name=f"job-processor-{number}")
                 for number in range(1, workers + 1)]
    started = []
    unexpected_exit = False
    try:
        for process in processes:
            if stop_requested:
                stop.value = 1
                break
            process.start()
            started.append(process)
        while any(process.is_alive() for process in started):
            if stop_requested:
                stop.value = 1
            for process in started:
                process.join(timeout=0.2)
                if stop_requested:
                    stop.value = 1
                # Im Dauerbetrieb muss jede unerwartet beendete Worker-
                # Instanz zum Neustart der Gruppe fuehren. Bei --once
                # darf ein Worker regulaer frueher fertig sein.
                if process.exitcode is not None and not once and not stop.value:
                    unexpected_exit = True
                    stop.value = 1
        if not once and started and not stop_requested and not stop.value:
            unexpected_exit = True
        return int(unexpected_exit or any(process.exitcode != 0 for process in started))
    finally:
        # Auch bei einer Exception waehrend Start / Ueberwachung beenden.
        stop.value = 1
        try:
            for process in started:
                process.join()
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)


# =============================================================================
# FUNKTION: main
# Aufgabe: Liest beide YAML-Dateien, prueft die Aktivierung und startet Worker.
# Parameter: Keine; Optionen aus sys.argv.
# Rueckgabewert: int - 0 bei Erfolg, 1 bei Fehler.
# =============================================================================
def main():
    if __package__:
        from .config_loader import load_config
    else:
        from config_loader import load_config
    parser = argparse.ArgumentParser(description="ImageServer job processor")
    parser.add_argument("--config", help="Path to imageserver.yaml")
    parser.add_argument("--product-interface-config", help="Path to product-interface.yaml")
    parser.add_argument("--workers", type=int, help="Override configured worker count")
    parser.add_argument("--once", action="store_true", help="Process ready jobs, then exit")
    args = parser.parse_args()
    try:
        config = load_config(args.config) if args.config else load_config()
        if not config.settings["supervisor"].get("composite_processing_enabled", False):
            print("Composite processing is disabled in imageserver.yaml")
            return 0
        settings = config.settings.get("job_processing", {})
        workers = args.workers if args.workers is not None else settings.get("workers", 2)
        interval = float(settings.get("scan_interval_seconds", 2))
        if type(workers) is not int or workers < 1 or not 0 < interval < float("inf"):
            raise ValueError("Invalid worker count or scan interval")
        pii_file = Path(args.product_interface_config or settings.get(
            "product_interface_config",
            Path(config.settings["server"]["root_dir"]) / "config/product-interface.yaml"))
        with pii_file.open(encoding="utf-8") as stream:
            pii = yaml.safe_load(stream)
        validate_config(config, pii)
        # Eine gestartete Processor-Gruppe pro Queue. Innerhalb der Gruppe
        # sichern die Job-Leases jeden Einzelauftrag separat ab.
        lock_path = get_queue_paths(pii)["lock"].parent / ".job-processor.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as instance:
            try:
                fcntl.flock(instance.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Job Processor already running for this queue") from error
            result = run_workers(config, pii, workers, args.once, interval)
            with queue_lock(pii):
                paths = get_queue_paths(pii)
                print("Queue: " + ", ".join(
                    f"{state}={len(list(paths[state].glob('*.job')))}"
                    for state in ("pending", "running", "done", "failed")))
            return result
    except Exception:
        logging.exception("Job Processor failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

