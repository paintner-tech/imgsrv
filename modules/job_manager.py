# =============================================================================
# JOB MANAGER – Prüfung dateibasierter Bildaufträge (erste Ausbaustufe)
# =============================================================================
# Liest die JSON-Aufträge des Job Creators und ermittelt für jede benötigte
# Separation die vorhandenen TIFF- und PNG-Dateien. Ein TIFF kann von mehreren
# Aufträgen verwendet werden. Es wird weder verschoben noch umgerechnet.
# Die Prüfung erfolgt ausschließlich lesend; auch fertige Composite-Dateien
# werden nur gemeldet. Existenz bedeutet noch nicht Aktualität oder Gültigkeit.
# Keine Datenbank, keine externen Python-Pakete.
# =============================================================================
import hashlib
import json
import re
from pathlib import Path


# -----------------------------------------------------------------------------
# FUNKTION: safe_name
# Aufgabe: Prüft einen aus einem Auftrag übernommenen Dateinamen auf Sicherheit.
# Übergabeparameter: value (str), zu prüfender Name ohne Verzeichnispfad.
# Rückgabewert: str, unveränderter Name.
# Fehler: ValueError bei leerem oder unzulässigem Namen.
# -----------------------------------------------------------------------------
def safe_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._-]+', value) or value in ('.', '..'):
        raise ValueError(f'Unsafe file name: {value!r}')
    return value


# -----------------------------------------------------------------------------
# FUNKTION: load_job
# Aufgabe: Liest einen Auftrag und prüft dessen Aufbau und Rezept-ID.
# Übergabeparameter: filename (str oder Path), Pfad zur .job-Datei.
# Rückgabewert: dict, geprüfter Auftrag im Schema des Job Creators.
# Fehler: ValueError bei ungültigem Inhalt; OSError bei Lesefehlern.
# -----------------------------------------------------------------------------
def load_job(filename):
    path = Path(filename)
    job = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(job, dict) or job.get('schema_version') != 1:
        raise ValueError('Unsupported job schema')
    # Die Rezept-ID ist keine Prüfsumme der tatsächlichen Bilddateien. Sie
    # schützt hier lediglich vor einer versehentlichen Änderung des Rezepts.
    separations = job.get('separations')
    if not isinstance(separations, dict) or not separations or not set(separations) <= set('CMYK'):
        raise ValueError('Invalid separation groups')
    if job.get('separation_count') != len(separations):
        raise ValueError('Invalid separation count')
    for color, sep in separations.items():
        if not isinstance(sep, dict):
            raise ValueError(f'Invalid separation {color}')
        names = [sep.get('reference_name'), sep.get('content_id')]
        if not any(names):
            raise ValueError(f'Missing identifiers for {color}')
        for name in names:
            if name:
                safe_name(name)
        if not isinstance(sep.get('icc_profile'), str):
            raise ValueError(f'Invalid ICC profile for {color}')
    digest = hashlib.sha256(json.dumps({'separations': separations}, sort_keys=True).encode()).hexdigest()
    if job.get('job_id') != digest or path.name != digest + '.job':
        raise ValueError('Job ID or file name does not match recipe')
    outputs = job.get('composites')
    if not isinstance(outputs, list) or not outputs:
        raise ValueError('Missing composite names')
    for name in outputs:
        if not safe_name(name).endswith('.png'):
            raise ValueError('Invalid composite extension')
    if len(set(outputs)) != len(outputs):
        raise ValueError('Duplicate composite names')
    return job


# -----------------------------------------------------------------------------
# FUNKTION: index_files
# Aufgabe: Erfasst vorhandene Bilddateien einmal pro Verzeichnis. Dadurch muss
#          nicht für jede Separation erneut das gesamte Verzeichnis gelesen werden.
# Übergabeparameter:
#   directory: str oder Path, zu lesendes Verzeichnis (nicht rekursiv).
#   extensions: Menge erlaubter Dateiendungen in Kleinschreibung.
# Rückgabewert: dict, Dateistamm -> sortierte Liste von Path-Objekten.
# Fehler: OSError bei fehlendem oder nicht lesbarem Verzeichnis.
# -----------------------------------------------------------------------------
def index_files(directory, extensions):
    index = {}
    for path in sorted(Path(directory).iterdir()):
        # Nur die Endung wird unabhängig von Groß-/Kleinschreibung geprüft.
        # ReferenceName und ContentID bleiben exakt wie in den Produktdaten.
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in extensions:
            index.setdefault(path.stem, []).append(path)
    return index


# -----------------------------------------------------------------------------
# FUNKTION: find_files
# Aufgabe: Sucht Bilder unter beiden alternativen Namen einer Separation.
# Übergabeparameter: separation (dict), index (dict aus index_files).
# Rückgabewert: Liste eindeutiger, sortierter Pfade als Strings; ggf. [].
# -----------------------------------------------------------------------------
def find_files(separation, index):
    # Sind ReferenceName und ContentID gleich, darf dieselbe Datei nicht
    # fälschlich als doppelt vorhandenes Bild gemeldet werden.
    return sorted({str(path) for name in (separation.get('reference_name'), separation.get('content_id'))
                   if name for path in index.get(name, [])})


# -----------------------------------------------------------------------------
# FUNKTION: inspect_job
# Aufgabe: Ermittelt den aktuellen Dateibestand für alle erforderlichen Farben.
# Übergabeparameter:
#   job: geprüfter Auftrag aus load_job.
#   tiffs, pngs: Dateindizes aus index_files.
#   composites: Path oder str, Verzeichnis der Composite-Ausgaben.
# Rückgabewert: dict mit Job-ID, Quellen, Farbstati, Ausgaben und Gesamtstatus.
# Hinweis: Keine Bildprüfung, keine Prüfung auf stabile Dateigröße, kein Schreiben.
# -----------------------------------------------------------------------------
def inspect_job(job, tiffs, pngs, composites):
    colors = {}
    for color, sep in sorted(job['separations'].items()):
        tif = find_files(sep, tiffs)
        png = find_files(sep, pngs)
        # Mehrere Treffer sind zulässig, ihre inhaltliche Identität ist aber
        # nicht bewiesen. Der spätere Worker darf nicht willkürlich auswählen.
        # Ein PNG neben einem TIFF ist ebenfalls kein Beweis für Aktualität.
        if len(tif) > 1 or len(png) > 1:
            status = 'AMBIGUOUS'
        elif png and tif:
            status = 'PNG_AND_TIFF'
        elif png:
            status = 'PNG_PRESENT'
        elif tif:
            status = 'TIFF_PRESENT'
        else:
            status = 'MISSING'
        colors[color] = dict(sep, status=status, tiffs=tif, pngs=png)
    states = {s['status'] for s in colors.values()}
    # Ein 1C-Auftrag wird genau wie ein 4C-Auftrag geprüft: Entscheidend sind
    # ausschließlich die im Auftrag angeforderten Separationen.
    if 'AMBIGUOUS' in states:
        status = 'AMBIGUOUS'
    elif 'MISSING' in states:
        status = 'WAITING'
    elif 'TIFF_PRESENT' in states:
        status = 'TIFF_CONVERSION_REQUIRED'
    else:
        status = 'ALL_PNG_PRESENT'
    outputs = {name: (Path(composites) / name).is_file() for name in job['composites']}
    return dict(job_id=job['job_id'], status=status, separations=colors,
                composites=outputs, sources=job.get('sources', []))


# -----------------------------------------------------------------------------
# FUNKTION: scan_jobs
# Aufgabe: Prüft alle veröffentlichten .job-Dateien eines Verzeichnisses.
# Übergabeparameter: jobs_dir, tiff_dir, png_dir, composite_dir (str oder Path).
# Rückgabewert: Tupel (reports, errors), zwei Listen mit Diagnose-Dictionaries.
# Fehler: OSError bei nicht zugänglichen Verzeichnissen. Einzelne defekte Jobs
#         werden separat gemeldet; die übrigen Aufträge werden weiter geprüft.
# -----------------------------------------------------------------------------
def scan_jobs(jobs_dir, tiff_dir, png_dir, composite_dir):
    tiffs = index_files(tiff_dir, {'.tif', '.tiff'})
    pngs = index_files(png_dir, {'.png'})
    if not Path(composite_dir).is_dir():
        raise NotADirectoryError(str(composite_dir))
    reports, errors = [], []
    # Temporäre Dateien des Job Creators werden ignoriert. Nur vollständig
    # veröffentlichte .job-Dateien gehören in die Auswertung.
    for path in sorted(Path(jobs_dir).iterdir()):
        if path.suffix != '.job' or not path.is_file() or path.is_symlink():
            continue
        try:
            reports.append(inspect_job(load_job(path), tiffs, pngs, composite_dir))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append({'file': str(path), 'error': str(exc)})
    # Auch über getrennt erzeugte Auftragssätze hinweg darf ein Composite-Name
    # nicht von zwei unterschiedlichen Rezepten beansprucht werden.
    owners = {}
    for report in reports:
        for name in report['composites']:
            owners.setdefault(name, []).append(report)
    for name, matches in owners.items():
        if len(matches) > 1:
            for report in matches:
                report['status'] = 'CONFLICT'
            errors.append({'file': name, 'error': 'Conflicting composite recipes'})
    return reports, errors

