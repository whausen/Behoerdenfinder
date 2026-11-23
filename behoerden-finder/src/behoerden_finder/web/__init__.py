"""Minimale Weboberfläche des Behörden-Finders (nur Standardbibliothek + vorhandene Pipeline).

    behoerden-finder-web [--port 8765] [--backend auto|local|wfs] [--no-live] [--no-semantic]

Läuft lokal auf http://127.0.0.1:8765. Endpunkte:
    GET  /                      Oberfläche (index.html)
    POST /api/suche             {"anliegen", "adresse", "geocoder": "nominatim"|"photon", optional "lat"/"lon",
                                 optional "kategorie" (z. B. nach Auswahl bei mehrdeutigem Ergebnis)}
    POST /api/behoerde-details  {"name", "adresse", "lat", "lon", "punkt"} -> Öffnungszeiten, Anschrift, Telefon ...
                                 aus OpenStreetMap (Standard) oder Google Places (mit GOOGLE_MAPS_API_KEY)
    GET  /api/vorschlaege?q=..  Adressvorschläge beim Tippen (nur Photon; Nominatim verbietet Autovervollständigung)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..geocode import GeocodingError, PhotonGeocoder, make_geocoder
from ..google_places import PlacesClient, api_key, zentrum_von
from ..osm_details import OsmClient
from .. import leistung_details
from ..zusatz_quellen import PvogClient, WebseitenClient, aus_properties
from ..pipeline import Finder, ResolveOptions
from ..render import HEADLINES, kontakt

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
MAX_BODY = 8_000


def decorate(result: dict) -> dict:
    """Ergebnis der Pipeline um Anzeigefelder ergänzen (Überschrift, Kontaktdaten je Treffer)."""
    result["ueberschrift"] = HEADLINES.get(result.get("status"), result.get("status"))
    # Die Oberfläche zeigt keine Hinweise zur Herkunft der Behördenart.
    kat = result.get("kategorie") or {}
    intern = set(kat.pop("hinweise", []) or [])
    result["hinweise"] = [h for h in result.get("hinweise") or [] if h not in intern]
    for match in (result.get("zustaendigkeit") or {}).get("matches") or []:
        match["kontakt"] = kontakt(match.get("properties"))
        amtlich = aus_properties(match.get("properties"))
        if amtlich:
            match["amtlich"] = amtlich
            for k in ("adresse", "telefon", "email", "web"):
                if amtlich.get(k) and not match["kontakt"].get(k):
                    match["kontakt"][k] = amtlich[k]
        z = zentrum_von(match.get("geometry"))
        match["zentrum"] = {"lat": z[0], "lon": z[1]} if z else None
    return result


def details_client(quelle: str = "auto"):
    """Quelle der Zusatzinfos: 'osm' (OpenStreetMap/Overpass, Open Data), 'google' (Places API, Schlüssel nötig),
    'aus' oder 'auto' (Google, wenn ein Schlüssel vorhanden ist, sonst OSM)."""
    if quelle == "auto":
        quelle = "google" if api_key() else "osm"
    if quelle == "google":
        return PlacesClient()
    if quelle == "osm":
        return OsmClient()
    return None


class App:
    def __init__(self, *, matcher=None, geocoders=None, options=None, resolve_fn=None, places=None, pvog=None,
                 webseiten=None):
        self.matcher = matcher
        self.geocoders = geocoders or {"nominatim": make_geocoder("nominatim"), "photon": make_geocoder("photon")}
        self.options = options or ResolveOptions()
        self.resolve_fn = resolve_fn
        self.places = (places or None) if places is not None else details_client("auto")
        self.pvog = (pvog or None) if pvog is not None else PvogClient()
        self.webseiten = (webseiten or None) if webseiten is not None else WebseitenClient()
        self._lock = threading.Lock()

    def search(self, payload: dict) -> tuple[int, dict]:
        anliegen = str(payload.get("anliegen") or "").strip()[:500]
        adresse = str(payload.get("adresse") or "").strip()[:200]
        geocoder = str(payload.get("geocoder") or "nominatim")
        kategorie = str(payload.get("kategorie") or "").strip() or None
        if geocoder not in self.geocoders:
            return 400, {"fehler": f"Unbekannter Geocoder: {geocoder}"}
        coords = None
        if payload.get("lat") is not None and payload.get("lon") is not None:
            try:
                coords = (float(payload["lat"]), float(payload["lon"]))
            except (TypeError, ValueError):
                return 400, {"fehler": "lat/lon müssen Zahlen sein."}
        finder = Finder(matcher=self.matcher, geocoder=self.geocoders[geocoder], options=self.options,
                        include_geometry=True, **({"resolve_fn": self.resolve_fn} if self.resolve_fn else {}))
        try:
            with self._lock:          # Matcher (Elasticsearch, Embeddings) einmal zur Zeit
                result = finder.find(anliegen or None, adresse or None, kategorie=kategorie, coords=coords)
        except ValueError as exc:
            return 400, {"fehler": str(exc)}
        except Exception as exc:     # z. B. Elasticsearch nicht gestartet
            return 503, {"fehler": f"{type(exc).__name__}: {exc}"}
        leistung = result.get("leistung") or {}
        if leistung.get("leistungsschluessel"):
            try:
                land = leistung_details.land_aus_text((result.get("standort") or {}).get("anzeige_name"))
                leistung["details"] = leistung_details.lookup(leistung["leistungsschluessel"], land)
            except Exception as exc:   # Beschreibung ist ein Zusatz
                leistung["details"] = {"gefunden": False, "fehler": str(exc)}
        return 200, decorate(result)

    def details(self, payload: dict) -> tuple[int, dict]:
        """Zusatzinfos zu einer gefundenen Behörde aus mehreren öffentlichen Quellen, in dieser Reihenfolge für die
        Öffnungszeiten: Portalverbund (zuständige Stelle) -> OpenStreetMap bzw. Google -> Webseite der Behörde.
        Gesendet werden nur Name/Ort der Behörde, Leistungsschlüssel und Gemeinde, nie das Anliegen."""
        def zahl(key):
            v = payload.get(key)
            return float(v) if v is not None and v != "" else None
        try:
            zentrum = (zahl("lat"), zahl("lon")) if payload.get("lat") is not None else None
            standort = (zahl("standort_lat"), zahl("standort_lon")) if payload.get("standort_lat") is not None else None
        except (TypeError, ValueError):
            return 400, {"fehler": "Koordinaten müssen Zahlen sein."}
        name = str(payload.get("name") or "")
        out: dict = {"verfuegbar": True, "gefunden": False}
        quellen = []
        adresse = str(payload.get("adresse") or "").strip()
        punkt = bool(payload.get("punkt"))
        # Der Mittelpunkt eines Zuständigkeitsgebiets belegt keinen Behördenstandort.
        if self.places is not None and (adresse or punkt):
            try:
                g = self.places.details(name, adresse, zentrum, punkt=punkt)
                trefferadresse = str(g.get("adresse") or "")
                normalisiert = lambda text: re.sub(r"\W+", "", text.casefold())
                passt = not adresse or normalisiert(adresse) == normalisiert(trefferadresse)
                if g.get("gefunden") and passt:
                    out.update(g)
                    if g.get("oeffnungszeiten"):
                        out["zeiten_quelle"] = {"osm": "OpenStreetMap", "google": "Google"}.get(g.get("quelle"), g.get("quelle"))
                    quellen.append(g.get("quelle"))
            except Exception as exc:
                out["fehler"] = f"{type(exc).__name__}: {exc}"
        if self.pvog is not None and payload.get("leika") and standort:
            try:
                pv = self.pvog.zustaendig(str(payload["leika"]), standort[0], standort[1], name)
            except Exception:
                pv = {}
            if pv.get("online_dienste"):
                out["online_dienste"] = pv["online_dienste"]
                out["gefunden"] = True
            stelle = pv.get("stelle") or {}
            if pv.get("stellen"):
                out["fachstellen"] = [
                    {**{k: s[k] for k in ("name", "rolle", "adresse", "telefon", "email", "web", "lat", "lon") if s.get(k)},
                     "quelle": "Portalverbund (PVOG)", "leistung_id": pv.get("leistung_id")}
                    for s in pv["stellen"] if s.get("name")
                ]
            gleich = True
            if stelle and name:
                from ..osm_details import aehnlichkeit
                gleich = aehnlichkeit(name, stelle.get("name") or "") >= 0.5
            if stelle:
                # Fachlich zugeordnete Stelle separat ausweisen; nicht mit der allgemeinen
                # Verwaltungsstelle vermischen (insbesondere bei abweichendem Namen).
                out["fachstelle"] = {k: stelle[k] for k in ("name", "rolle", "adresse", "telefon", "email", "web", "lat", "lon") if stelle.get(k)}
                out["fachstelle"]["quelle"] = "Portalverbund (PVOG)"
                out["fachstelle"]["leistung_id"] = pv.get("leistung_id")
                quellen.append("pvog")
                out["gefunden"] = True
                if gleich:                    # nur bei derselben Behörde Kontaktdaten ergänzen
                    for k in ("adresse", "telefon", "email", "web", "lat", "lon"):
                        if stelle.get(k) and not out.get(k):
                            out[k] = stelle[k]
                if stelle.get("oeffnungszeiten") and stelle.get("intervalle") and (gleich or not out.get("oeffnungszeiten")):
                    out.update({k: stelle.get(k) for k in ("oeffnungszeiten", "intervalle", "zeiten_quelle", "zeiten_text")})
                    if not gleich:            # andere (meist spezifischere) Stelle der Verwaltung: Namen nennen
                        out["zeiten_quelle"] = f"{stelle['zeiten_quelle']}: {stelle.get('name')}"
                    out["jetzt_geoeffnet"] = None
                if not gleich and stelle.get("web"):
                    out["web_zustaendig"] = stelle["web"]
        if not out.get("oeffnungszeiten") and self.webseiten is not None and not payload.get("ohne_zeiten"):
            kandidaten = [payload.get("web"), out.get("web"), out.get("web_zustaendig")]
            kommunal = re.search(r"stadt|gemeinde|verwaltung|rathaus|bürger|markt|landkreis|kreis", name, re.I)
            if kommunal and standort and self.pvog is not None:
                try:
                    kandidaten.append((self.pvog.gemeinde(standort[0], standort[1]) or {}).get("web"))
                except Exception:
                    pass
            for url in dict.fromkeys(u for u in kandidaten if u):
                w = self.webseiten.oeffnungszeiten(str(url))
                if w.get("oeffnungszeiten"):
                    out.update(w)
                    out["gefunden"] = True
                    quellen.append("webseite")
                    break
        out["quellen"] = quellen
        return 200, out

    def suggest(self, query: str) -> tuple[int, dict]:
        query = (query or "").strip()
        if len(query) < 3:
            return 200, {"vorschlaege": []}
        geo = self.geocoders.get("photon") or PhotonGeocoder()
        try:
            hits = geo.search(query, limit=6)
        except GeocodingError as exc:
            return 200, {"vorschlaege": [], "fehler": str(exc)}
        return 200, {"vorschlaege": [{"text": h.display_name, "lat": h.lat, "lon": h.lon, "genauigkeit": h.precision}
                                     for h in hits]}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Behoerdenfinder/0.1"

        def log_message(self, fmt, *args):  # ruhiger Server: nur Fehler
            if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, status: int, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict):
            self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/api/vorschlaege":
                q = parse_qs(url.query).get("q", [""])[0]
                return self._json(*app.suggest(q))
            name = "index.html" if url.path in ("/", "/index.html") else url.path.lstrip("/")
            path = (STATIC / name).resolve()
            if STATIC not in path.parents or not path.is_file() or path.suffix not in TYPES:
                return self._json(404, {"fehler": "nicht gefunden"})
            self._send(200, path.read_bytes(), TYPES[path.suffix])

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/api/suche", "/api/behoerde-details"):
                return self._json(404, {"fehler": "nicht gefunden"})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                return self._json(400, {"fehler": "Anfrage leer oder zu groß."})
            try:
                payload = json.loads(self.rfile.read(length))
            except ValueError:
                return self._json(400, {"fehler": "Ungültiges JSON."})
            if not isinstance(payload, dict):
                return self._json(400, {"fehler": "Ungültige Anfrage."})
            self._json(*(app.search(payload) if path == "/api/suche" else app.details(payload)))
    return Handler


def main(argv: list[str] | None = None) -> int:
    from ..matching import AuthorityMatcher
    p = argparse.ArgumentParser(prog="behoerden-finder-web", description="Weboberfläche des Behörden-Finders")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--backend", choices=["auto", "local", "wfs"], default="auto")
    p.add_argument("--no-live", action="store_true", help="Keine PVOG-Abfrage, nur Cache")
    p.add_argument("--no-semantic", action="store_true")
    p.add_argument("--matcher-vorrang", action="store_true")
    p.add_argument("--zusatzinfos", choices=["auto", "osm", "google", "aus"], default="auto",
                   help="Öffnungszeiten usw.: OpenStreetMap (Open Data), Google Places (Schlüssel) oder aus; "
                        "auto = Google mit Schlüssel, sonst OSM")
    a = p.parse_args(argv)
    matcher = AuthorityMatcher(semantic=not a.no_semantic, live=not a.no_live, faelle_vorrang=not a.matcher_vorrang)
    app = App(matcher=matcher, options=ResolveOptions(backend=a.backend), places=details_client(a.zusatzinfos) or False)
    server = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    quelle = {"osm": "OpenStreetMap (Overpass)", "google": "Google Places"}.get(getattr(app.places, "name", None), "aus")
    print(f"Zusatzinfos (Öffnungszeiten usw.): {quelle}", flush=True)
    print(f"Behörden-Finder läuft: http://{a.host}:{a.port}  (Beenden mit Ctrl+C)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
