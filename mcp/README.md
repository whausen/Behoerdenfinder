# Behördenfinder – MCP-Server

MCP-Schnittstelle (Model Context Protocol) zur Ermittlung
zuständiger Behörden in Deutschland.

## Öffentlicher MCP-Endpunkt

https://behoerdenfinder.hack4gdi.de/mcp

Transport: Streamable HTTP

## Werkzeuge

### behoerde_finden

Ermittelt die zuständige Behörde anhand einer Adresse,
einer Behördenkategorie und optionaler LeiKa-Kennungen.

Beispiel:
- Kategorie: stadtverwaltung
- Adresse: Schillerplatz 1, 55116 Mainz
- LeiKa: 99110009104000

### zustaendigkeit_finden

Ermittelt administrative Zuständigkeiten anhand
geografischer Informationen.

## Datenquellen

- BZB-Open / VG250 – BKG
- PVOG – FITKO / Dataport
- OpenStreetMap / Nominatim

## Lokaler Start

Voraussetzung: Python 3.11+

Installation:

    pip install -r requirements.txt

Start des MCP-Servers:

    python mcp_server.py --host 127.0.0.1 --port 8000

Der lokale MCP-Endpunkt ist anschließend unter
http://localhost:8000/mcp erreichbar.

Für die öffentliche Bereitstellung wird ein
HTTPS-Reverse-Proxy empfohlen.

## Repository

https://github.com/whausen/Behoerdenfinder

Die MCP-Komponente befindet sich im Verzeichnis `mcp/`.
