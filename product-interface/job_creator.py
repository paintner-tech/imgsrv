# =============================================================================
# JOB CREATOR – Dateibasierte Erzeugung von Bildaufträgen
# =============================================================================
# Dieses Modul übernimmt die bereits geprüften Ergebnisse des Product Details
# Parsers. Es liest selbst kein XML und verarbeitet weder TIFFs noch PNGs.
#
# Ein Auftrag beschreibt einen Bildinhalt: die benötigten Farbseparationen,
# deren alternative Dateinamen (ReferenceName / ContentID), die ICC-Profile
# und alle gewünschten Composite-Dateinamen (PTTraceElementIdentification).
# Identische Bildrezepte werden zu einem Auftrag zusammengefasst, damit der
# spätere Image Processor das Composite nur einmal berechnen muss.
#
# Die Aufträge werden als JSON-Dateien mit der Endung .job gespeichert.
# Es gibt keine Datenbank und keine Abhängigkeit von zusätzlichen Paketen.
# Die Funktionen zur Berechnung und zum Schreiben sind bewusst getrennt:
# Der Tester kann alle Aufträge anzeigen, ohne das Dateisystem zu verändern.
# =============================================================================
import hashlib
import json
import os
from pathlib import Path
import tempfile


# -----------------------------------------------------------------------------
# FUNKTION: create_jobs
# Aufgabe:
#   Fasst identische Bildrezepte aus einem oder mehreren Produkten zusammen.
#   Ermittelt sämtliche Ausgabedateinamen und die zugehörigen Produktseiten.
# Übergabeparameter:
#   products: Liste der vom Product Details Parser geprüften Produkt-Dictionaries
#             mit product_name und traces (Trace-ID -> Trace-Dictionary).
# Rückgabewert:
#   Liste von Job-Dictionaries, sortiert nach dem ersten Composite-Dateinamen.
#   Jeder Job enthält schema_version, job_id, separations, separation_count,
#   composites und sources. Bei leerer Eingabeliste wird [] zurückgegeben.
# Fehler:
#   ValueError bei unsicherer Trace-ID, nicht unterstützten Anzeigeattributen
#   oder widersprüchlichen Bildrezepten für denselben Ausgabedateinamen.
# -----------------------------------------------------------------------------
def create_jobs(products):
    # Zwei unabhängige Indizes im Arbeitsspeicher: Bildrezept -> Auftrag und
    # Ausgabedateiname -> Bildrezept. Der zweite Index verhindert, dass zwei
    # unterschiedliche Bilder unter demselben PNG-Namen veröffentlicht werden.
    jobs = {}
    outputs = {}
    for product in products:
        for tid, trace in product['traces'].items():
            # Die Trace-ID wird später Teil eines Dateinamens. Pfadtrenner und
            # andere unerwartete Zeichen dürfen nicht in einen Auftrag gelangen.
            if not tid or any(c not in '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ._-' for c in tid) or tid in ('.', '..'):
                raise ValueError(f'Unsafe trace ID: {tid}')

            # Die Bedeutung alternativer Traces und PrepareDisplay=no ist noch
            # nicht geklärt. Deshalb diese Fälle nicht stillschweigend ignorieren
            # oder ein möglicherweise falsches Composite in Auftrag geben.
            if any(p['standard'] != 'yes' or p['prepare_display'] != 'yes' for p in trace['placements']):
                raise ValueError(f'Unsupported trace/display flags for {tid}')

            # Nur die bildbestimmenden Angaben bilden die Job-ID. Produktname,
            # Plattenposition und Trace-ID sind bewusst nicht Teil des Rezepts:
            # derselbe Inhalt kann an mehreren Positionen verwendet werden.
            # Die Separationen enthalten auch die ICC-Profile. Unterschiedliche
            # Profile dürfen somit nicht versehentlich zusammengefasst werden.
            # sort_keys stellt sicher, dass die Reihenfolge der Farben im XML
            # keinen Einfluss auf die SHA-256-Prüfsumme hat. Die Prüfsumme ist
            # eine Rezept-ID, keine Prüfsumme des tatsächlichen TIFF-Inhalts.
            recipe = {'separations': trace['separations']}
            digest = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
            output = tid + '.png'

            # Ein Zielname darf nur einem einzigen Bildrezept zugeordnet sein.
            if output in outputs and outputs[output] != digest:
                raise ValueError(f'Conflicting output {output}')
            outputs[output] = digest

            # Existiert das Rezept bereits, wird der bestehende Auftrag erweitert.
            # Die Anzahl der Farben ergibt sich aus den geprüften Separationen:
            # Eine 1C-Seite benötigt daher nur eine einzige Separation, eine
            # 4C-Seite dagegen alle vier. Es wird nicht pauschal auf CMYK gewartet.
            job = jobs.setdefault(digest, dict(schema_version=1, job_id=digest,
                separations=trace['separations'], separation_count=len(trace['separations']),
                composites=[], sources=[]))
            if output not in job['composites']:
                job['composites'].append(output)

            # Die Herkunft bleibt für Service und Fehlersuche erhalten, auch
            # wenn mehrere Seiten oder Produkte dasselbe Bildrezept verwenden.
            source = dict(product_name=product['product_name'], page_number=trace['page_number'], trace_id=tid)
            if source not in job['sources']:
                job['sources'].append(source)

    # Stabile Sortierung: Wiederholte Auswertungen derselben Produktdaten sollen
    # unabhängig von der Eingabereihenfolge identische Auftragsdateien ergeben.
    for job in jobs.values():
        job['composites'].sort()
        job['sources'].sort(key=lambda s: (s['product_name'], s['trace_id'], s['page_number']))
    return sorted(jobs.values(), key=lambda j: j['composites'][0])


# -----------------------------------------------------------------------------
# FUNKTION: write_jobs
# Aufgabe:
#   Speichert die erzeugten Aufträge als einzelne JSON-Dateien (*.job).
#   Bereits vorhandene identische Aufträge werden nicht erneut geschrieben.
#   Abweichende Aufträge werden bewusst nicht überschrieben.
# Übergabeparameter:
#   jobs: Liste der von create_jobs erzeugten Job-Dictionaries.
#   directory: Zielverzeichnis als String oder pathlib.Path.
# Rückgabewert:
#   Liste von pathlib.Path-Objekten für neue und bereits identische Aufträge.
# Fehler:
#   FileExistsError bei abweichendem Inhalt eines vorhandenen Auftrags;
#   weitere Dateisystemfehler werden an den Aufrufer weitergereicht.
# Hinweis:
#   Die Veröffentlichung erfolgt atomar pro Datei, nicht für den gesamten
#   Auftragssatz. Bei einem Schreibfehler können frühere Jobs vorhanden bleiben.
# -----------------------------------------------------------------------------
def write_jobs(jobs, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    # Vor dem Schreiben sämtliche Zielnamen und Inhalte prüfen. Dadurch wird
    # ein bereits erkennbarer Konflikt nicht erst nach mehreren neuen Jobs
    # festgestellt. sort_keys und indent sorgen für ein stabiles, lesbares JSON.
    planned = []
    for job in jobs:
        path = directory / (job['job_id'] + '.job')
        content = json.dumps(job, indent=2, ensure_ascii=False, sort_keys=True) + '\n'
        if path.exists() and path.read_text(encoding='utf-8') != content:
            raise FileExistsError(f'Changed job already exists: {path}; use a fresh directory')
        planned.append((path, content))

    for path, content in planned:
        # Erneute Prüfung, falls seit der Vorprüfung ein anderer Prozess einen
        # Auftrag erzeugt hat. Identische Aufträge behalten ihren Zeitstempel.
        if path.exists():
            if path.read_text(encoding='utf-8') != content:
                raise FileExistsError(f'Changed job already exists: {path}')
            continue

        # Temporäre Datei im selben Verzeichnis anlegen. Der spätere Job Manager
        # darf ausschließlich *.job beachten, niemals diese temporären Dateien.
        # So kann er keine nur teilweise geschriebene JSON-Datei einlesen.
        fd, temp = tempfile.mkstemp(prefix='.job-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())

            # Ein Hardlink veröffentlicht die vollständig geschriebene Datei
            # unter ihrem endgültigen Namen, ohne bestehende Dateien zu ersetzen.
            # Im Gegensatz zu os.replace kann damit ein paralleler Schreiber
            # nicht versehentlich einen bereits vorhandenen Auftrag überschreiben.
            try:
                os.link(temp, path)
            except FileExistsError:
                if path.read_text(encoding='utf-8') != content:
                    raise
        finally:
            # Auch bei einem Fehler die temporäre Datei entfernen. Nach einem
            # erfolgreichen Hardlink bleibt die endgültige .job-Datei erhalten.
            os.unlink(temp)

    return [path for path, _ in planned]

