"""Weitere öffentliche Quellen für Öffnungszeiten und Kontaktdaten einer Behörde (zusätzlich zu OpenStreetMap):

1. Amtliche Datensätze, die der Resolver ohnehin mitliefert (z. B. Finanzamtsverzeichnis GemFA mit
   ``OeffnungszeitListe``, städtische Behördenverzeichnisse mit ``sprechzeiten``) – ohne Netzabfrage.
2. Portalverbund (PVOG, Datenbasis der Zuständigkeitsfinder): zuständige Stelle für Leistung + Gemeinde mit
   Anschrift, Kontakt, Zeiten (``INFO_TIMES``) und Online-Diensten. Die Gemeinde (ARS) kommt aus der
   OSM-Gemeindegrenze am Standort (Nominatim reverse, Tag ``de:regionalschluessel``).
3. Webseite der Behörde: Abschnitt „Öffnungszeiten“/„Sprechzeiten“ automatisch auslesen (Hinweis „bitte prüfen“).
Gesendet werden nur Leistungsschlüssel, Gemeinde, Behördenname bzw. die Webseite der Behörde.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urljoin, urlparse

import requests

from . import oeffnungszeiten as oz

PVOG = "https://pvog.fitko.net/suchdienst/api"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
UA = "Behoerdenfinder-Hackathon/0.1 (https://github.com/KTV02/GdiHackathonChallenge4)"
CACHE_SEKUNDEN = 6 * 3600


def _zeiten(text: str | None, quelle: str) -> dict | None:
    if not text or not text.strip():
        return None
    week = oz.parse(text)
    return {"oeffnungszeiten": oz.zeilen(week) if week else [re.sub(r"\s+", " ", text).strip()[:400]],
            "intervalle": week, "zeiten_quelle": quelle, "zeiten_text": re.sub(r"\s+", " ", text).strip()[:300]}


# --- 1. Amtliche Resolver-Daten -------------------------------------------------------------------------------------

def aus_properties(props: dict | None) -> dict:
    """Kontakt und Öffnungszeiten aus den Originalattributen eines Resolver-Treffers."""
    props = props or {}
    out: dict = {}
    xml = props.get("source_record_xml")
    if isinstance(xml, str) and "<" in xml:
        try:
            root = ET.fromstring(xml)
            ns = {"g": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
            find = (lambda path: root.findall(path, ns)) if ns else (lambda path: root.findall(path.replace("g:", "")))
            for a in find(".//g:Adresse"):
                if a.get("Strasse"):
                    nr = (a.get("HausNr") or "") + (a.get("HausNrZusatz") or "")
                    out["adresse"] = f"{a.get('Strasse')} {nr}".strip() + f", {a.get('PLZ', '')} {a.get('Ort', '')}".rstrip()
                    break
            for k in find(".//g:Kontakt"):
                art, wert = (k.get("Bezeichnung") or "").lower(), k.get("Inhalt")
                if wert and art == "tel":
                    out.setdefault("telefon", wert)
                elif wert and art == "mail":
                    out.setdefault("email", wert)
                elif wert and art == "url":
                    out.setdefault("web", wert)
            zeiten = " ".join((z.text or "") for z in find(".//g:Oeffnungszeit"))
            z = _zeiten(zeiten, "Finanzamtsverzeichnis (BZSt/GemFA)")
            if z:
                out.update(z)
        except ET.ParseError:
            pass
    for key in ("opening_hours", "oeffnungszeiten", "sprechzeiten", "servicezeiten", "öffnungszeiten"):
        if not out.get("oeffnungszeiten") and isinstance(props.get(key), str):
            z = _zeiten(props[key], "amtlicher Datensatz")
            if z:
                out.update(z)
    return out


# --- 2. Portalverbund ------------------------------------------------------------------------------------------------

class PvogClient:
    def __init__(self, *, http_get=None, timeout: float = 12.0, clock=time.monotonic):
        self._get = http_get or requests.get
        self.timeout, self._clock = timeout, clock
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._nominatim_last = 0.0

    def _cached(self, key, fn):
        with self._lock:
            hit = self._cache.get(key)
            if hit and self._clock() - hit[0] < CACHE_SEKUNDEN:
                return hit[1]
        value = fn()
        with self._lock:
            if len(self._cache) > 1000:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (self._clock(), value)
        return value

    def _json(self, url, params, headers=None):
        r = self._get(url, params=params, timeout=self.timeout,
                      headers={"Accept": "application/json", "Accept-Language": "de", "User-Agent": UA, **(headers or {})})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.json()

    def ars(self, lat: float, lon: float) -> str | None:
        """Amtlicher Regionalschlüssel der Gemeinde am Punkt (über die OSM-Gemeindegrenze)."""
        return (self.gemeinde(lat, lon) or {}).get("ars")

    def gemeinde(self, lat: float, lon: float) -> dict | None:
        """{"ars", "name", "web"} der Gemeinde am Punkt (OSM-Gemeindegrenze, Tags de:regionalschluessel/website)."""
        def lookup():
            for zoom in (10, 8):
                with self._lock:
                    wait = 1.1 - (self._clock() - self._nominatim_last)
                    if wait > 0 and self._nominatim_last:
                        time.sleep(wait)
                    self._nominatim_last = self._clock()
                d = self._json(NOMINATIM_REVERSE, {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": zoom, "extratags": 1})
                x = d.get("extratags") or {}
                rs = x.get("de:regionalschluessel")
                if rs and re.fullmatch(r"\d{12}", rs):
                    return {"ars": rs, "name": d.get("name"), "web": x.get("website") or x.get("contact:website")}
            return None
        return self._cached(f"gemeinde|{round(lat, 4)}|{round(lon, 4)}", lookup)

    def zustaendig(self, leika: str, lat: float, lon: float, name: str | None = None) -> dict:
        if not re.fullmatch(r"\d{14}", leika or ""):
            return {}
        ars = self.ars(lat, lon)
        if not ars:
            return {}

        def lookup():
            heute = date.today().isoformat()
            d = self._json(PVOG + "/v3/servicedescriptions/leikaid",
                           {"leikaIds": leika, "ars": ars, "useArsHierarchy": "true", "withOrgIds": "true",
                            "size": 5, "page": 0, "validDate": heute})
            content = d.get("content") or []
            if not content:
                return {}
            lb = content[0]
            out: dict = {"ars": ars, "leistung_id": lb.get("id")}
            online = [{"titel": o.get("title") or "Online-Dienst", "uri": o["uri"]}
                      for o in lb.get("onlineServiceLinks") or [] if str(o.get("uri", "")).startswith("http")]
            if online:
                out["online_dienste"] = online[:3]
            stellen = []
            for oe in (lb.get("organisationIds") or [])[:4]:
                try:
                    stellen.append({**self._json(PVOG + "/v5/organisationunits/detail",
                                              {"q": oe, "ars": ars, "lbId": lb.get("id"), "validDate": heute}), "organisation_id": oe})
                except Exception:
                    continue
            out["stellen"] = [self._stelle(s) for s in stellen]
            return out
        data = self._cached(f"pvog|{leika}|{ars}", lookup)
        if not data:
            return {}
        stellen = data.get("stellen") or []
        best = None
        if stellen:
            from .osm_details import aehnlichkeit
            best = max(stellen, key=lambda s: (aehnlichkeit(name or "", s.get("name") or "") if name else 0,
                                               s.get("rolle") == "Zuständige Stelle"))
        return {**{k: v for k, v in data.items() if k != "stellen"},
                "stellen": stellen,
                **({"stelle": best} if best else {})}

    @staticmethod
    def _stelle(s: dict) -> dict:
        loc = s.get("location") or {}
        out = {"name": s.get("title"), "rolle": (s.get("role") or {}).get("name"), "organisation_id": s.get("organisation_id")}
        for a in loc.get("addresses") or []:
            if a.get("street"):
                out["adresse"] = f"{a.get('street')} {a.get('houseNumber') or ''}".strip() + f", {a.get('zip') or ''} {a.get('city') or ''}".rstrip()
                geo = a.get("geo") or {}
                try:
                    out["lat"], out["lon"] = float(geo["latitude"]), float(geo["longitude"])
                except (KeyError, TypeError, ValueError):
                    pass
                break
        for c in loc.get("communications") or []:
            n, v = (c.get("name") or "").lower(), c.get("value")
            if v and "mail" in n:
                out.setdefault("email", v)
            elif v and "telefon" in n:
                out.setdefault("telefon", v)
        for i in s.get("internetAddresses") or []:
            if str(i.get("uri", "")).startswith("http"):
                out.setdefault("web", i["uri"])
        texte = [x.get("text") or "" for x in s.get("additionalInformation") or [] if x.get("type") == "INFO_TIMES"]
        texte += [x.get("text") or "" for x in loc.get("information") or []
                  if re.search(r"öffnung|sprech|servicezeit", (x.get("title") or "") + (x.get("text") or "")[:80], re.I)]
        text = _html_text(" ".join(texte))
        z = _zeiten(text, "Portalverbund (Zuständigkeitsfinder)")
        if z:
            out.update(z)
        return out


def _html_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    html = re.sub(r"(?i)<br\s*/?>|</(p|li|div|tr|h\d)>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&ndash;", "–"), ("&#8211;", "–"), ("&uuml;", "ü"), ("&ouml;", "ö"),
                 ("&auml;", "ä"), ("&szlig;", "ß"), ("&Ouml;", "Ö")):
        text = text.replace(a, b)
    return re.sub(r"[ \t]+", " ", text)



# --- Hessen: ortsbezogene Leistungs- und Organisationszuordnung -------------------------------------
class HessenClient:
    """Öffentlich erreichbare Portal-JSON-Endpunkte; nicht als stabile API dokumentiert."""
    BASIS = "https://verwaltungsportal.hessen.de"

    def __init__(self, *, http_post=None, timeout=9.0, clock=time.monotonic):
        self._post = http_post or requests.post
        self.timeout, self._clock = timeout, clock
        self._cache = {}
        self._lock = threading.Lock()

    def _request(self, path, params):
        response = self._post(self.BASIS + path, params=params, timeout=self.timeout,
                              headers={"Accept": "application/json", "User-Agent": UA})
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _org_links(html):
        from html.parser import HTMLParser
        from urllib.parse import parse_qs
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.href = None
                self.label = ""
                self.items = []
            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    self.href = dict(attrs).get("href", "")
                    self.label = ""
            def handle_data(self, data):
                if self.href is not None:
                    self.label += data
            def handle_endtag(self, tag):
                if tag == "a" and self.href is not None:
                    if urlparse(self.href).path == "/behoerde":
                        oid = parse_qs(urlparse(self.href).query).get("org_id", [""])[0]
                        if re.fullmatch(r"L\d+_\d+", oid):
                            self.items.append((oid, self.label.strip()))
                    self.href = None
        parser = Links()
        parser.feed(html)
        return list(dict.fromkeys(parser.items))

    def zustaendig(self, leika, ars, suchbegriff=None):
        if not re.fullmatch(r"\d{14}", str(leika or "")) or not re.fullmatch(r"\d{12}", str(ars or "")):
            return {}
        if not str(ars).startswith("06"):
            return {}
        key = (str(leika), str(ars))
        with self._lock:
            cached = self._cache.get(key)
            if cached and self._clock() - cached[0] < CACHE_SEKUNDEN:
                return cached[1]
        query = (suchbegriff or str(leika))[:100]
        search = self._request("/suche", {"lang": "de_DE", "suche": query, "regschl": ars})
        rows = ((search.get("result") or {}).get("leistungenRows") or [])
        exact = next((row for row in rows if str(row.get("leikaSchluessel")) == str(leika)), None)
        if exact is None and query != str(leika):
            search = self._request("/suche", {"lang": "de_DE", "suche": str(leika), "regschl": ars})
            exact = next((row for row in ((search.get("result") or {}).get("leistungenRows") or [])
                          if str(row.get("leikaSchluessel")) == str(leika)), None)
        if not exact or not re.fullmatch(r"L\d+_\d+", str(exact.get("id") or "")):
            return {}
        lid = exact["id"]
        page = self._request("/leistung", {"leistung_id": lid, "regschl": ars, "lang": "de_DE"})
        if str(((page.get("page") or {}).get("data") or {}).get("leistung", {}).get("firstLeika")) != str(leika):
            return {}
        html = (((page.get("page") or {}).get("pageElements") or {}).get("contentHtml") or {}).get("safeHtml") or ""
        stellen = []
        for oid, label in self._org_links(html)[:12]:
            # Organisationsseite für zusätzliche Daten; der Link ist bereits amtlich
            # mit dieser Leistung verknüpft. Keine Rollen aus der Reihenfolge ableiten.
            stelle = {"name": label or "Ansprechpartner", "rolle": "Ansprechpartner zur Leistung",
                      "quelle": "Verwaltungsportal Hessen", "organisation_id": oid,
                      "web": self.BASIS + "/behoerde?org_id=" + oid}
            try:
                org = self._request("/behoerde", {"org_id": oid, "lang": "de_DE"})
                orghtml = (((org.get("page") or {}).get("pageElements") or {}).get("contentHtml") or {}).get("safeHtml") or ""
                title = re.search(r"<h1[^>]*>(.*?)</h1>", orghtml, re.I | re.S)
                if title:
                    stelle["name"] = re.sub(r"<[^>]*>", "", title.group(1)).strip() or stelle["name"]
            except (requests.RequestException, ValueError, KeyError, TypeError):
                pass
            stellen.append(stelle)
        result = {"ars": ars, "leistung_id": lid, "leika": leika,
                  "leistung_url": self.BASIS + "/leistung?leistung_id=" + lid + "&regschl=" + ars,
                  "stellen": stellen, "zuordnung_vorhanden": bool(stellen)}
        with self._lock:
            if len(self._cache) > 1000:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (self._clock(), result)
        return result


# --- 3. Webseite der Behörde -----------------------------------------------------------------------------------------

_SCHLUESSEL = re.compile(r"öffnungszeit|oeffnungszeit|sprechzeit|servicezeit|besuchszeit|publikumsverkehr|opening", re.I)


def _oeffentlich(url: str) -> bool:
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except (OSError, ValueError):
        return False


class WebseitenClient:
    MAX_BYTES = 1_500_000

    def __init__(self, *, http_get=None, timeout: float = 8.0, clock=time.monotonic, pruefe_host=_oeffentlich):
        self._get = http_get or requests.get
        self.timeout, self._clock, self._pruefe = timeout, clock, pruefe_host
        self._cache: dict[str, tuple[float, dict]] = {}

    def _laden(self, url: str) -> str | None:
        if not self._pruefe(url):
            return None
        r = self._get(url, timeout=self.timeout, headers={"User-Agent": UA, "Accept-Language": "de"}, stream=True,
                      allow_redirects=True)
        if r.status_code != 200 or "html" not in (r.headers.get("Content-Type") or "html"):
            return None
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > self.MAX_BYTES:
                break
        return data.decode(r.encoding or "utf-8", errors="replace")

    @staticmethod
    def _aus_text(html: str) -> str | None:
        text = _html_text(html)
        for m in _SCHLUESSEL.finditer(text):
            abschnitt = text[m.start(): m.start() + 700]
            week = oz.parse(abschnitt)
            if week and any(week):
                return abschnitt
        return None

    def oeffnungszeiten(self, url: str | None) -> dict:
        if not url:
            return {}
        url = url if "://" in url else "https://" + url
        hit = self._cache.get(url)
        if hit and self._clock() - hit[0] < CACHE_SEKUNDEN:
            return hit[1]
        result: dict = {}
        try:
            html = self._laden(url)
            abschnitt = self._aus_text(html) if html else None
            if not abschnitt and html:                         # Unterseite "Öffnungszeiten"/"Kontakt" suchen
                links = re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', html, re.I | re.S)
                kandidaten = [h for h, t in links if re.search(r"ffnungszeit|sprechzeit|servicezeit|kontakt|anfahrt|besuch",
                                                             h + " " + t, re.I)]
                for href in kandidaten[:3]:
                    sub = urljoin(url, href)
                    if urlparse(sub).netloc != urlparse(url).netloc:
                        continue
                    html2 = self._laden(sub)
                    abschnitt = self._aus_text(html2) if html2 else None
                    if abschnitt:
                        url = sub
                        break
            if abschnitt:
                week = oz.parse(abschnitt)
                result = {"oeffnungszeiten": oz.zeilen(week), "intervalle": week,
                          "zeiten_quelle": "Webseite der Behörde (automatisch ausgelesen)", "zeiten_url": url,
                          "zeiten_text": re.sub(r"\s+", " ", abschnitt).strip()[:300]}
        except Exception:
            result = {}
        self._cache[url] = (self._clock(), result)
        return result
