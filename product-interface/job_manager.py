# =============================================================================
# ImageServer - Product Interface
# =============================================================================
# Modul: job_manager.py
#
# BESCHREIBUNG:
#   Gleicht den gewuenschten Auftragsbestand mit der dateibasierten
#   Warteschlange ab. Der Job Creator bleibt fuer die Bildrezepte zustaendig.
#   Dieses Modul verarbeitet keine Bilder und benoetigt keine Datenbank.
#
#   manifest.json enthaelt den aktuell freigegebenen Auftragsbestand.
#   Jeder Auftrag erhaelt eine Revision, die sich nur bei einer Aenderung
#   oder erneuten Aktivierung aendert. Alte Worker-Ergebnisse koennen damit
#   auch dann erkannt werden, wenn eine Rezept-ID spaeter wieder auftaucht.
#
#   Alle Aenderungen der Queue und spaeter die Job-Uebernahme sowie die
#   endgueltige Bildveroeffentlichung muessen dieselbe flock-Sperre benutzen.
#   Die eigentliche Bildberechnung erfolgt ausserhalb der Sperre.
# =============================================================================

import copy
import fcntl
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("product-interface.job_manager")
JOB_ID = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{32}")
OUTPUT = re.compile(r"[A-Za-z0-9._-]+\.png")


# =============================================================================
# FUNKTION: get_queue_paths
# Aufgabe: Ermittelt alle Queue-Pfade aus der bestehenden YAML-Konfiguration.
# Uebergabeparameter: config (dict) - Product-Interface-Konfiguration.
# Rueckgabewert: dict[str, Path] - Verzeichnisse, Manifest und Sperrdatei.
# =============================================================================
def get_queue_paths(config: dict) -> dict:
    settings = config["job_creation"]
    pending = Path(settings["output_directory"]).resolve()
    root = pending.parent

    # Bestehende Konfiguration reicht aus; optionale Pfade koennen bei
    # Bedarf separat angegeben werden. Standard: Geschwister von pending.
    paths = {"pending": pending}
    for state in ("running", "done", "failed"):
        paths[state] = Path(settings.get(state + "_directory", root / state)).resolve()
    paths["manifest"] = Path(settings.get("manifest_file", root / "manifest.json")).resolve()
    paths["lock"] = Path(settings.get("lock_file", root / ".queue.lock")).resolve()

    if len(set(paths.values())) != len(paths):
        raise ValueError("Queue paths must be distinct")
    return paths


# =============================================================================
# FUNKTION: queue_lock
# Aufgabe: Exklusive, prozessuebergreifende Sperre fuer kurze Queue-Operationen.
# Uebergabeparameter: config (dict) - gemeinsame Queue-Konfiguration.
# Rueckgabewert: Context Manager; innerhalb des with-Blocks wird None geliefert.
# Hinweis: Nicht verschachteln. Sperrdatei niemals loeschen oder ersetzen.
# =============================================================================
@contextmanager
def queue_lock(config: dict):
    lock = get_queue_paths(config)["lock"]
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


# =============================================================================
# FUNKTION: read_json
# Aufgabe: Liest eine JSON-Datei; fehlende Dateien werden separat behandelt.
# Uebergabeparameter: path (Path) - einzulesende Datei.
# Rueckgabewert: JSON-Inhalt oder None, wenn die Datei nicht existiert.
# =============================================================================
def read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


# =============================================================================
# FUNKTION: sync_directory
# Aufgabe: Sichert Verzeichniseintraege nach atomaren Aenderungen auf Linux.
# Uebergabeparameter: directory (Path) - betroffenes Verzeichnis.
# Rueckgabewert: None.
# =============================================================================
def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# =============================================================================
# FUNKTION: write_json_atomic
# Aufgabe: Schreibt ein JSON-Objekt vollstaendig und ersetzt die Zieldatei atomar.
# Uebergabeparameter: path (Path) - Zieldatei; data (dict) - JSON-Daten.
# Rueckgabewert: None.
# =============================================================================
def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".job-manager-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


# =============================================================================
# FUNKTION: load_active_products
# Aufgabe: Laedt ausschliesslich Produktdateien der aktuellen Produktliste.
#           Veraltete oder fehlende Produktdaten brechen den Abgleich ab.
# Uebergabeparameter: config (dict); products (list[dict]) - Produktliste
#                    vom product_list_parser, inklusive product_update.
# Rueckgabewert: list[dict] - vollstaendige, aktuelle Produktdaten.
# =============================================================================
def load_active_products(config: dict, products: list[dict]) -> list[dict]:
    # Lokaler Import: Der Queue-Manager kann auch ohne Produktserver getestet
    # werden. Die Namenslogik bleibt ausschliesslich im Request Sender.
    from request_sender import get_product_detail_filenames

    directory = Path(config["directories"]["products"])
    prefix = config["product_details"]["response_prefix"]
    active = []
    used = set()
    for product in products:
        name = product["product_name"]
        _, filename = get_product_detail_filenames(config, name)
        response = Path(filename)
        if response.name != str(filename) or not response.stem.startswith(prefix):
            raise ValueError(f"Invalid response filename: {filename}")
        stem = response.stem.removeprefix(prefix)
        if not stem or stem in (".", ".."):
            raise ValueError(f"Invalid product filename: {filename}")
        path = directory / (stem + ".json")
        if path in used or path.resolve() == Path(config["product_cache"]["filename"]).resolve():
            raise ValueError(f"Product filename collision: {path}")
        used.add(path)
        data = read_json(path)
        if (data is None or data.get("product_name") != name
                or data.get("product_update") != product["product_update"]
                or not isinstance(data.get("traces"), dict) or not data["traces"]):
            raise ValueError(f"Missing, invalid or outdated product data: {path}")
        active.append(data)
    return active


# =============================================================================
# FUNKTION: validate_jobs
# Aufgabe: Prueft Job-IDs, Dateinamen und eindeutige Composite-Zuordnungen,
#           bevor irgendeine Queue-Datei veraendert wird.
# Uebergabeparameter: jobs (list[dict]) - Ausgabe des Job Creators.
# Rueckgabewert: dict[str, dict] - Auftraege nach Job-ID, als unabhaengige Kopie.
# =============================================================================
def validate_jobs(jobs: list[dict]) -> dict:
    desired = {}
    outputs = set()
    for source in jobs:
        job = copy.deepcopy(source)
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or job_id in desired:
            raise ValueError(f"Invalid or duplicate job ID: {job_id}")
        if job.get("schema_version") != 1 or "revision" in job:
            raise ValueError(f"Unsupported job schema: {job_id}")
        colors = job.get("separations")
        if (not isinstance(colors, dict) or not colors or not set(colors) <= set("CMYK")
                or job.get("separation_count") != len(colors)):
            raise ValueError(f"Invalid separations: {job_id}")
        names = job.get("composites")
        if not isinstance(names, list) or not names or not isinstance(job.get("sources"), list):
            raise ValueError(f"Invalid job structure: {job_id}")
        for name in names:
            if (not isinstance(name, str) or not OUTPUT.fullmatch(name)
                    or name in ("..png", "...png") or name in outputs):
                raise ValueError(f"Invalid or conflicting composite filename: {name}")
            outputs.add(name)
        desired[job_id] = job
    return desired


# =============================================================================
# FUNKTION: load_manifest
# Aufgabe: Liest den gueltigen Auftragsbestand. Ein beschaedigtes Manifest
#           wird nicht stillschweigend durch ein leeres ersetzt.
# Uebergabeparameter: path (Path) - Manifest-Datei.
# Rueckgabewert: dict[str, dict] - Job-ID -> Auftrag inklusive Revision.
# =============================================================================
def load_manifest(path: Path) -> dict:
    data = read_json(path)
    if data is None:
        return {}
    if data.get("schema_version") != 1 or not isinstance(data.get("jobs"), dict):
        raise ValueError(f"Invalid job manifest: {path}")
    jobs = data["jobs"]
    originals = []
    for job_id, job in jobs.items():
        if (not isinstance(job, dict) or job.get("job_id") != job_id
                or not isinstance(job.get("revision"), str)
                or not REVISION.fullmatch(job["revision"])):
            raise ValueError(f"Invalid manifest job: {job_id}")
        originals.append({k: v for k, v in job.items() if k != "revision"})
    validate_jobs(originals)
    return jobs


# =============================================================================
# FUNKTION: get_job_state_file
# Aufgabe: Liefert den Dateinamen fuer einen Auftrag in einem Queue-Zustand.
#           In running/done/failed verhindert die Revision Namenskollisionen.
# Uebergabeparameter: config (dict); state (str); job (dict) inkl. Revision.
# Rueckgabewert: Path - Pfad zur Auftragsdatei.
# =============================================================================
def get_job_state_file(config: dict, state: str, job: dict) -> Path:
    if state not in ("pending", "running", "done", "failed"):
        raise ValueError(f"Invalid job state: {state}")
    job_id, revision = job["job_id"], job["revision"]
    if not JOB_ID.fullmatch(job_id) or not REVISION.fullmatch(revision):
        raise ValueError("Invalid job identity")
    name = job_id if state == "pending" else f"{job_id}.{revision}"
    return get_queue_paths(config)[state] / (name + ".job")


# =============================================================================
# FUNKTION: job_is_current
# Aufgabe: Prueft, ob der Auftrag noch exakt dem freigegebenen Manifest entspricht.
# Uebergabeparameter: config (dict); job (dict) - Auftrag inklusive Revision.
# Rueckgabewert: bool - True nur fuer den aktuell gueltigen Auftrag.
# WICHTIG: Aufrufer MUSS queue_lock halten, und zwar bis nach der Uebernahme
#          bzw. Veroeffentlichung. Allein die Abfrage ist keine Sperre!
# =============================================================================
def job_is_current(config: dict, job: dict) -> bool:
    manifest = load_manifest(get_queue_paths(config)["manifest"])
    return manifest.get(job["job_id"]) == job


# =============================================================================
# FUNKTION: synchronize_jobs
# Aufgabe: Gleicht den vollstaendigen aktiven Sollbestand mit der Queue ab.
#           Unveraenderte Auftraege behalten ihre Revision und Zeitstempel.
#           Veraltete pending-Dateien werden entfernt, laufende nie angefasst.
# Uebergabeparameter: config (dict); jobs (list[dict]) - ALLE aktiven Jobs,
#                    nicht nur diejenigen eines geaenderten Produktes.
# Rueckgabewert: dict[str, int] - Statistik des Abgleichs.
# Fehler: Exceptions an den Supervisor weiterreichen; Wiederholung ist moeglich.
# =============================================================================
def synchronize_jobs(config: dict, jobs: list[dict]) -> dict:
    desired = validate_jobs(jobs)
    paths = get_queue_paths(config)
    stats = dict(total=len(desired), new=0, changed=0, obsolete=0,
                 pending_written=0, pending_removed=0, pending_unchanged=0,
                 running=0, done=0, failed=0)

    with queue_lock(config):
        for state in ("pending", "running", "done", "failed"):
            paths[state].mkdir(parents=True, exist_ok=True)
        if len({paths[s].stat().st_dev for s in ("pending", "running", "done", "failed")}) != 1:
            raise ValueError("Queue directories must share one filesystem")

        old = load_manifest(paths["manifest"])
        current = {}
        stats["obsolete"] = len(old.keys() - desired.keys())
        for job_id, job in desired.items():
            previous = old.get(job_id)
            # Auch geaenderte Ausgabedateinamen bei gleicher Rezept-ID
            # erfordern eine neue Revision. A -> B -> A erhaelt ebenfalls
            # eine neue Revision, wenn A zwischenzeitlich ungueltig war.
            if previous is not None and {k: v for k, v in previous.items() if k != "revision"} == job:
                current[job_id] = previous
            else:
                stats["new" if previous is None else "changed"] += 1
                current[job_id] = dict(job, revision=uuid.uuid4().hex)

        # Zuerst den Sollbestand freigeben. Bei einem Absturz waehrend des
        # nachfolgenden Queue-Abgleichs blockiert die Manifest-Pruefung alte
        # Auftraege; der naechste Zyklus stellt fehlende pending-Dateien her.
        if current != old or not paths["manifest"].exists():
            write_json_atomic(paths["manifest"], {"schema_version": 1, "jobs": current})

        # Nur eigene *.job-Dateien bearbeiten; fremde Dateien nicht loeschen.
        for path in paths["pending"].glob("*.job"):
            if JOB_ID.fullmatch(path.stem) and path.stem not in current:
                path.unlink()
                sync_directory(path.parent)
                stats["pending_removed"] += 1
                logger.info("Obsolete pending job removed: %s", path.name)

        for job_id, job in current.items():
            pending = get_job_state_file(config, "pending", job)
            state_found = None
            # Bereits uebernommene oder beendete Revisionen nicht erneut
            # einplanen. Alte Revisionen in diesen Ordnern zaehlen nicht.
            for state in ("running", "done", "failed"):
                path = get_job_state_file(config, state, job)
                data = read_json(path)
                if data is not None:
                    if data != job or state_found is not None:
                        raise ValueError(f"Inconsistent job state: {path}")
                    state_found = state
            if state_found:
                stats[state_found] += 1
                if pending.exists():
                    pending.unlink()
                    sync_directory(pending.parent)
                    stats["pending_removed"] += 1
                continue

            if read_json(pending) == job:
                stats["pending_unchanged"] += 1
            else:
                write_json_atomic(pending, job)
                stats["pending_written"] += 1
                logger.debug("Pending job written: %s", pending.name)

    logger.info("Job synchronization completed: %s", ", ".join(f"{k}={v}" for k, v in stats.items()))
    return stats

