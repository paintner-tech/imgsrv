# =============================================================================
# PRODUCT DETAILS PARSER – Auswertung der PII-Produktantwort
# =============================================================================
# Dieses Modul liest eine PIF-Datei (XML) und ermittelt, welche TIFF-Separationen
# für welches Composite benötigt werden. Es erzeugt selbst keine Bilder oder
# Auftragsdateien und verändert die Eingabedatei nicht.
#
# Die Zuordnung erfolgt innerhalb eines PTPrintingCouple über das Rectangle.
# ReferenceName und ContentID sind alternative Namen derselben Separation.
# PTTraceElementIdentification bestimmt den späteren Composite-Dateinamen.
# Die Page-Informationen dienen als Sollvorgabe für die benötigten Farben.
# Mehrfach belegte Seiten bleiben als einzelne TraceElements erhalten; erst
# der Job Creator fasst identische Bildinhalte zu einem Auftrag zusammen.
# =============================================================================
import logging
from collections import defaultdict
from decimal import Decimal
import xml.etree.ElementTree as ET

LOG = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# FUNKTION: rectangle
# Aufgabe: Liest die vier Koordinaten eines Rectangle und macht sie vergleichbar.
# Übergabeparameter:
#   element: XML-Element (PTPlate oder PTTraceElement), das ein Rectangle enthält.
# Rückgabewert:
#   Tuple (x, y, width, height) aus vier Decimal-Werten.
# Fehler: ValueError bei fehlendem Rectangle oder nicht endlichen Koordinaten;
#         ungültige bzw. fehlende Attribute lösen ebenfalls eine Ausnahme aus.
# -----------------------------------------------------------------------------
def rectangle(element):
    # Nur das direkt untergeordnete Rectangle verwenden.
    rect = element.find('Rectangle')
    if rect is None:
        raise ValueError('Missing Rectangle')
    # Decimal vermeidet Rundungsfehler und behandelt 350.0 und 350.00 gleich.
    # Die Reihenfolge der vier Werte ist Bestandteil des Zuordnungsschlüssels.
    values = tuple(Decimal(rect.attrib[k]) for k in ('x', 'y', 'width', 'height'))
    if not all(v.is_finite() for v in values):
        raise ValueError('Invalid Rectangle')
    return values


# -----------------------------------------------------------------------------
# FUNKTION: parse_product_details
# Aufgabe: Liest alle Produkte der Antwortdatei, ordnet die Druckplatten den
#          TraceElements zu und prüft die Vollständigkeit der Farbseparationen.
# Übergabeparameter:
#   filename: Pfad zur PIF-/XML-Datei als String oder pathlib.Path.
# Rückgabewert:
#   Liste von Dictionaries, je eines pro Produkt, mit folgenden Schlüsseln:
#     product_name: Produktname (innere Leerzeichen bleiben erhalten).
#     pages: Seitennummer -> Farbgruppe -> SeparationReference.
#     traces: Trace-ID -> Seitennummer, Separationen und Plattenpositionen.
#     empty_plates: Anzahl unbelegter Plattenpositionen.
# Fehler: Bei ungültigen oder widersprüchlichen Daten wird eine Ausnahme
#         ausgelöst; es werden keine unvollständigen Ergebnisse zurückgegeben.
# -----------------------------------------------------------------------------
def parse_product_details(filename):
    # ElementTree liest das XML vollständig ein. XML- und Dateifehler werden
    # an den Aufrufer weitergegeben, der über Logging und Wiederholung entscheidet.
    root = ET.parse(filename).getroot()
    products = []
    # Eine Antwort kann mehrere Produkte enthalten. Ihre Zuordnungen dürfen
    # nicht vermischt werden, daher erhält jedes Produkt eigene Dictionaries.
    for product in root.findall('.//ProductInfo/Product'):
        name = (product.findtext('ProductName') or '').strip()
        if not name:
            raise ValueError('Missing ProductName')
        # SCHRITT 1: Logische Seitenstruktur lesen.
        # Die Page-Einträge definieren, welche Farben tatsächlich erwartet werden.
        # Eine K-Seite darf später schon nach einer Separation vollständig sein.
        pages = {}
        for page in product.findall('Page'):
            number = int(page.findtext('seqPageNumber'))
            if number in pages:
                raise ValueError(f'Duplicate page {number}')
            # Pro Seite wird für jede CMYK-Farbgruppe genau eine Referenz erwartet.
            # Sonderfarben und Mehrfachbelegungen werden bewusst nicht geraten.
            colors = {}
            for color in page.findall('Color'):
                group = color.find('ColorGroup').get('Group')
                ref = (color.findtext('SeparationReference') or '').strip()
                if group not in 'CMYK' or len(group) != 1 or not ref or group in colors:
                    raise ValueError(f'{name}: unsupported/invalid page color {group!r}, page {number}')
                colors[group] = ref
            pages[number] = colors
        # SCHRITT 2: Physische Druckplatten und TraceElements zuordnen.
        # Ein TraceElement kann in mehreren Druckwerken vorkommen, weil jede
        # Farbe separat aufgebracht wird. Alle Farben werden hier gesammelt.
        traces = {}
        empty = 0
        for press in product.findall('Press'):
            # ICC-Profile stehen in PlatePosition. Der Schlüssel enthält Seite,
            # Farbe, Druckeinheit und PrintingCouple, damit Profile unterschiedlicher
            # Positionen nicht versehentlich miteinander vermischt werden.
            # Ein Set erkennt widersprüchliche Profilnamen derselben Position.
            profiles = defaultdict(set)
            for pos in press.findall('PlatePosition'):
                group = pos.find('ColorGroup').get('Group')
                key = (int(pos.findtext('seqPageNumber')), group,
                       pos.findtext('PrUnit'), pos.findtext('PrCouple'))
                profiles[key].add((pos.findtext('ICCProfileName') or '').strip())
            # Rectangle-Koordinaten wiederholen sich in anderen PrintingCouples.
            # Deshalb den Positionsindex bei jedem PrintingCouple neu aufbauen.
            for couple in press.findall('PlateTraceRelation/PTPrintingCouple'):
                unit, cp = couple.get('PrintingUnit'), couple.get('PrintingCouple')
                positions = defaultdict(list)
                # SCHRITT 2a: Druckplatten nach ihren vier Koordinaten indizieren.
                # Eine Liste je Position erlaubt das Erkennen mehrdeutiger Treffer.
                for plate in couple.findall('PTPlateList/PTPlate'):
                    ids = {p.get('Type'): (p.get('Value') or '').strip()
                           for p in plate.findall('PTPlateIdentification/PTPlateIdentificationPart')}
                    ref, cid = ids.get('ReferenceName', ''), ids.get('ContentID', '')
                    # Leere Platten sind normale unbelegte Positionen und liefern
                    # keine TIFF-Datei. Sie werden nur für die Statistik gezählt.
                    if not ref and not cid:
                        empty += 1
                        continue
                    group = plate.find('ColorGroup').get('Group')
                    positions[rectangle(plate)].append((ref, cid, group))
                matched = set()
                # SCHRITT 2b: Zu jedem TraceElement die passende Platte suchen.
                # Alternative Traces und PrepareDisplay werden gespeichert, aber
                # noch nicht gefiltert: Ihre genaue Bedeutung ist nicht geklärt.
                for trace in couple.findall('PTTraceList/PTTrace'):
                    seen = set()
                    for el in trace.findall('PTTraceElementList/PTTraceElement'):
                        tid = (el.findtext('PTTraceElementIdentification') or '').strip()
                        page = int(el.findtext('PTTraceElementSeqNum'))
                        rect = rectangle(el)
                        # Genau eine Platte muss passen. Doppelte Koordinaten
                        # innerhalb desselben Trace oder fehlende Treffer sind Fehler.
                        if not tid or rect in seen or len(positions.get(rect, [])) != 1:
                            raise ValueError(f'{name}: ambiguous/missing plate for {tid}, unit {unit}, couple {cp}, {rect}')
                        seen.add(rect)
                        matched.add(rect)
                        ref, cid, group = positions[rect][0]
                        # Die physische Platte muss zur logischen Seite passen.
                        # Bei fehlendem ReferenceName kann die Page-Referenz als
                        # Ersatz dienen, sofern die Platte eine ContentID besitzt.
                        expected = pages.get(page, {}).get(group)
                        if not expected or (ref and expected != ref):
                            raise ValueError(f'{name}: page/plate mismatch {tid}: {group} {ref} != {expected}')
                        # Das passende ICC-Profil ermitteln. Ein fehlendes Profil
                        # bleibt leer; widersprüchliche Profile sind ein Fehler.
                        prof = profiles.get((page, group, unit, cp), set())
                        if len(prof) > 1:
                            raise ValueError(f'Conflicting ICC profiles for {tid}/{group}')
                        sep = dict(reference_name=ref or expected, content_id=cid,
                                   icc_profile=next(iter(prof), ''))
                        # Beim ersten Auftreten einen Composite-Datensatz anlegen.
                        # Weitere Druckwerke ergänzen die anderen Farbseparationen.
                        # Gleiche Separationen dürfen an anderen Trace-IDs vorkommen.
                        record = traces.setdefault(tid, dict(trace_id=tid, page_number=page,
                            separations={}, placements=[]))
                        if record['page_number'] != page or (group in record['separations'] and record['separations'][group] != sep):
                            raise ValueError(f'Conflicting trace assignment {tid}/{group}')
                        record['separations'][group] = sep
                        # Physische Position für Diagnose und spätere Erweiterungen
                        # behalten; sie gehört nicht zum TIFF-Dateinamen.
                        record['placements'].append(dict(color=group, printing_unit=unit,
                            printing_couple=cp, rectangle=[str(v) for v in rect],
                            prepare_display=el.get('PrepareDisplay'), standard=trace.get('Standard'),
                            trace_identification=trace.get('TraceIdentification')))
                # Keine belegte Platte darf ohne Trace-Zuordnung bleiben.
                if set(positions) != matched:
                    raise ValueError(f'{name}: unmatched plates in {unit}/{cp}')
        if not traces:
            raise ValueError(f'{name}: no trace assignments')
        # SCHRITT 3: Soll- und Ist-Farbgruppen vergleichen. Eine fehlende Farbe
        # darf niemals versehentlich als absichtlich einfarbige Seite gelten.
        for tid, record in traces.items():
            if set(record['separations']) != set(pages[record['page_number']]):
                raise ValueError(f'{name}: incomplete color assignment for {tid}')
        # Erst nach erfolgreicher Prüfung das vollständige Produkt übernehmen.
        products.append(dict(product_name=name, pages=pages, traces=traces, empty_plates=empty))
        LOG.info('Parsed %s: %d pages, %d traces', name, len(pages), len(traces))
    if not products:
        raise ValueError(f'No products in {filename}')
    return products

