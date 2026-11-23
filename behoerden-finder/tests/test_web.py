"""Weboberfläche: Server-Endpunkte mit Test-Doubles, Photon-Antworten parsen."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from behoerden_finder.geocode import GeocodeResult, GeoHit, PhotonGeocoder, _photon_hit
from behoerden_finder.web import App, make_handler
from helpers import FakeGeocoder, FakeMatcher, FakeResponse, outcome, polygon_result


class FakeSuggest:
    name = "photon"

    def search(self, q, limit=5):
        return [GeoHit(50.0, 8.27, "Schillerstraße 13, 55116 Mainz", "hausnummer")]

    def locate(self, q):
        return GeocodeResult(q, GeoHit(50.0, 8.27, "Schillerstraße 13, 55116 Mainz", "hausnummer"))


class FakePlaces:
    verfuegbar = True

    def __init__(self):
        self.calls = []

    def details(self, name, adresse, zentrum, punkt=False):
        self.calls.append((name, adresse, zentrum))
        return {"verfuegbar": True, "gefunden": True, "adresse": adresse, "oeffnungszeiten": ["Montag: 08:00–12:00"]}


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hit = GeoHit(50.0, 8.27, "Musterstraße 12, Mainz", "hausnummer")
        cls.resolve_calls = []

        def resolve(category, lon, lat, **opts):
            cls.resolve_calls.append((category, lon, lat))
            return polygon_result(category)
        app = App(matcher=FakeMatcher(outcome("finanzamt")), resolve_fn=resolve,
                  geocoders={"nominatim": FakeGeocoder(GeocodeResult("x", hit)), "photon": FakeSuggest()},
                  places=FakePlaces())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def post(self, payload):
        req = urllib.request.Request(self.base + "/api/suche", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_index_and_static_files(self):
        for path, ctype in (("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")):
            with urllib.request.urlopen(self.base + path) as r:
                self.assertTrue(r.headers["Content-Type"].startswith(ctype))
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/../web/__init__.py")

    def test_search_returns_polygon_and_contact(self):
        status, d = self.post({"anliegen": "Schenkung melden", "adresse": "Musterstraße 12, Mainz"})
        self.assertEqual(status, 200)
        self.assertEqual(d["status"], "zustaendige_behoerde")
        m = d["zustaendigkeit"]["matches"][0]
        self.assertEqual(m["geometry"]["type"], "MultiPolygon")          # Fläche für die Karte bleibt erhalten
        self.assertEqual(m["kontakt"]["adresse"], "Schillerstraße 13, 55116 Mainz")
        self.assertEqual(d["ueberschrift"], "Zuständige Behörde")
        self.assertEqual(d["standort"]["quelle"], "nominatim")
        self.assertEqual(d["leistung"]["titel"], "Schenkung anzeigen")
        self.assertEqual(d["zustaendigkeit"]["matches"][0]["source_id"], "gemfa_vg25")   # Quelle bleibt

    def test_category_origin_hints_are_hidden(self):
        from behoerden_finder.web import decorate
        d = decorate({"status": "zustaendige_behoerde", "leistung": None,
                      "kategorie": {"id": "finanzamt", "hinweise": ["Herkunft"]},
                      "hinweise": ["Herkunft", "Koordinate nicht hausgenau"], "zustaendigkeit": None})
        self.assertEqual(d["hinweise"], ["Koordinate nicht hausgenau"])
        self.assertNotIn("hinweise", d["kategorie"])

    def test_selected_suggestion_coordinates_skip_geocoding(self):
        status, d = self.post({"anliegen": "x y z", "adresse": "egal", "lat": 49.5, "lon": 8.1, "geocoder": "photon"})
        self.assertEqual(status, 200)
        self.assertEqual(self.resolve_calls[-1][1:], (8.1, 49.5))

    def test_photon_geocoder_label(self):
        status, d = self.post({"anliegen": "Schenkung", "adresse": "Schillerstraße 13 Mainz", "geocoder": "photon"})
        self.assertEqual(d["standort"]["quelle"], "photon")

    def test_details_endpoint_sends_only_authority(self):
        req = urllib.request.Request(self.base + "/api/behoerde-details", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"name": "Finanzamt Mainz-Mitte", "adresse": "Schillerstraße 13",
                                                      "lat": 50.0, "lon": 8.27}).encode())
        with urllib.request.urlopen(req) as r:
            d = json.loads(r.read())
        self.assertEqual(d["oeffnungszeiten"], ["Montag: 08:00–12:00"])

    def test_match_has_center_for_details(self):
        status, d = self.post({"anliegen": "Schenkung", "adresse": "Musterstraße 12, Mainz"})
        self.assertEqual(d["zustaendigkeit"]["matches"][0]["zentrum"], {"lat": 0.5, "lon": 0.5})

    def test_bad_requests(self):
        self.assertEqual(self.post({"anliegen": "x", "adresse": "y", "geocoder": "google"})[0], 400)
        self.assertEqual(self.post({"anliegen": "", "adresse": ""})[0], 400)

    def test_suggestions(self):
        with urllib.request.urlopen(self.base + "/api/vorschlaege?q=Schillerstr") as r:
            d = json.loads(r.read())
        self.assertEqual(d["vorschlaege"][0]["lat"], 50.0)
        with urllib.request.urlopen(self.base + "/api/vorschlaege?q=ab") as r:
            self.assertEqual(json.loads(r.read())["vorschlaege"], [])


class PhotonTest(unittest.TestCase):
    FEATURE = {"geometry": {"coordinates": [8.2711, 49.9987]},
               "properties": {"street": "Schillerstraße", "housenumber": "13", "postcode": "55116", "city": "Mainz",
                              "state": "Rheinland-Pfalz", "countrycode": "DE", "osm_type": "N", "osm_id": 42}}

    def test_parse(self):
        h = _photon_hit(self.FEATURE)
        self.assertEqual((h.lat, h.lon, h.precision, h.osm_type), (49.9987, 8.2711, "hausnummer", "node"))
        self.assertEqual(h.display_name, "Schillerstraße 13, 55116 Mainz, Rheinland-Pfalz")

    def test_foreign_hits_are_dropped(self):
        f = {**self.FEATURE, "properties": {**self.FEATURE["properties"], "countrycode": "AT"}}
        self.assertIsNone(_photon_hit(f))

    def test_search_uses_photon_params(self):
        calls = []

        def get(url, params, headers, timeout):
            calls.append(params)
            return FakeResponse({"features": [self.FEATURE]})
        g = PhotonGeocoder(http_get=get, sleep=lambda s: None)
        self.assertEqual(g.locate("Schillerstraße 13 Mainz").best.precision, "hausnummer")
        self.assertIn("bbox", calls[0])


if __name__ == "__main__":
    unittest.main()
