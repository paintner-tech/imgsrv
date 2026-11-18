# ImageServer – Konfigurationsbasis (V0.1)

Enthalten sind die Konfiguration, der zentrale Konfigurationsloader, eine Dateinamen-Erkennung und eine einfache Konfigurationsprüfung. **Noch keine** Ordnerüberwachung, TIFF-Konvertierung, Composite-Erzeugung oder automatische Löschung. Die Aufbewahrungszeiten sind erst konfiguriert, noch nicht aktiv.

## Installation auf Ubuntu

```bash
sudo mkdir -p /pecom/imgsrv
# Archiv nach /pecom/imgsrv entpacken, danach:
cd /pecom/imgsrv
python3 check_config.py
```

## Aufbau
- `config/imageserver.ini`: zentrale Einstellungen
- `modules/config_loader.py`: Einlesen und Validieren
- `check_config.py`: Test ohne Schreibzugriff
- `tiffs-in/`: Eingangs-TIFFs
- `pngs/`: Einzel-Separationen
- `composites/`: fertige RGB-Vorschauen aus vier Separationen
- `logs/`: Protokolle

Pfadangaben in der Konfiguration dürfen relativ zur Projektwurzel oder absolut sein. Zeitangaben unterstützen `h` (Stunden), `d` (Tage) oder `0` zum Deaktivieren der Löschung.

Vor der späteren Aktivierung von Cleanup: TIFFs nur löschen, wenn die jeweilige Separation erfolgreich verarbeitet und das Ergebnis dauerhaft geschrieben wurde. Temporäre/noch wachsende Dateien nicht verarbeiten oder löschen.
