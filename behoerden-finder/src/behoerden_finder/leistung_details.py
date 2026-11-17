"""Leistungsbeschreibung aus den beim PVOG-Abruf gespeicherten JZuFi-Daten (Tabelle ``jzufi_services`` der
FIM-Datenbank): Kurzbeschreibung, Voraussetzungen, erforderliche Unterlagen, Kosten, Fristen, Ablauf, Formulare,
Hinweis auf Online-Erledigung. Liest nur lokal, keine Netzabfrage.

Die Texte stammen von der jeweiligen Landesredaktion (meist Berlin, Hamburg oder Rheinland-Pfalz, je nachdem, wo
die Leistung beim Abruf gefunden wurde). Regelungen und Unterlagen können in anderen Ländern abweichen; die
Ausgabe nennt deshalb das Land, aus dem der Text stammt.
"""
from __future__ import annotations

import json
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

from . import faelle

# Codes der Codeliste xzufi.leistungstextmodul (wie in den Daten beobachtet)
TEXTMODULE = {"24": "kurztext", "06": "beschreibung", "09": "voraussetzungen", "11": "ablauf", "16": "hinweise",
              "14": "formulare", "15": "weitere_infos", "07": "rechtsgrundlage"}
LAENDER = {"01": "Schleswig-Holstein", "02": "Hamburg", "03": "Niedersachsen", "04": "Bremen",
           "05": "Nordrhein-Westfalen", "06": "Hessen", "07": "Rheinland-Pfalz", "08": "Baden-Württemberg",
           "09": "Bayern", "10": "Saarland", "11": "Berlin", "12": "Brandenburg", "13": "Mecklenburg-Vorpommern",
           "14": "Sachsen", "15": "Sachsen-Anhalt", "16": "Thüringen"}
ONLINE = re.compile(r"online|elster|digital beantrag|internet beantrag|e-mail beantrag|onlinedienst|onlineantrag|"
                    r"onlineverfahren|über das (service)?portal", re.I)
_EN = re.compile(r"\b(the|you|your|is|are|of|to|and|can|must|for|with|this)\b", re.I)
_DE = re.compile(r"\b(der|die|das|und|sie|ist|sind|ihr|ihre|mit|für|bei|nicht|oder|zu|den|dem)\b", re.I)


class _Text(HTMLParser):
    """HTML -> Absätze/Listenpunkte (reiner Text) und Links."""

    BLOCK = {"p", "li", "br", "div", "ul", "ol", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.teile, self.links, self._buf, self._href, self._atext = [], [], [], None, []

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if t:
            self.teile.append(t)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self._flush()
        if tag == "a":
            self._href, self._atext = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            titel = re.sub(r"\s+", " ", "".join(self._atext)).strip()
            if self._href.startswith(("http://", "https://")):
                self.links.append({"titel": titel or self._href, "uri": self._href})
            self._href = None
        if tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        self._buf.append(data)
        if self._href is not None:
            self._atext.append(data)


def _deutsch(teil: str) -> bool:
    return not (len(_EN.findall(teil)) >= 2 and len(_EN.findall(teil)) > 2 * len(_DE.findall(teil)))


def text(values) -> tuple[list[str], list[dict]]:
    p = _Text()
    for v in values or []:
        if isinstance(v, dict) and v.get("languageCode", "de") == "de" and v.get("value"):
            p.feed(str(v["value"]) + "<br>")
    p.close()
    p._flush()
    teile = [t for t in dict.fromkeys(p.teile) if _deutsch(t)]
    return teile, p.links


def auswerten(raw: dict) -> dict:
    out: dict = {}
    links: list[dict] = []
    for m in raw.get("modulText") or []:
        name = TEXTMODULE.get(((m.get("leikaTextmodul") or {}).get("code")))
        if not name:
            continue
        teile, l = text(m.get("inhalt"))
        for w in m.get("weiterfuehrenderLink") or []:
            if w.get("uri", "").startswith("http"):
                l.append({"titel": w.get("titel") or w["uri"], "uri": w["uri"]})
        if name in ("formulare", "weitere_infos", "rechtsgrundlage"):
            if l:
                out.setdefault(name, []).extend(l)
        elif teile:
            out.setdefault(name, []).extend(teile)
        links += l
    unterlagen = []
    for m in raw.get("modulDokument") or []:
        for v in m.get("beschreibungEingehend") or []:
            teile, l = text([v])
            links += l
            if len(teile) > 1 and len(teile[0]) <= 80:      # "<strong>Titel</strong><br>Erläuterung"
                teile = [f"{teile[0]}: {' '.join(teile[1:])}"]
            unterlagen += teile
    if unterlagen:
        out["unterlagen"] = unterlagen
    for modul, name in (("modulKosten", "kosten"), ("modulFrist", "fristen"), ("modulBearbeitungsdauer", "dauer")):
        teile = []
        for m in raw.get(modul) or []:
            teile += text(m.get("beschreibung"))[0]
        if teile:
            out[name] = teile
    portal = [{"titel": p.get("titel") or "Leistung im Serviceportal", "uri": p["uri"]}
              for p in raw.get("modulUrsprungsportal") or [] if str(p.get("uri", "")).startswith("http")]
    if portal:
        out["portal"] = portal[:1]
    # Online-Erledigung: Hinweise in Ablauf/Beschreibung/Hinweisen oder Online-Links
    online_links = [l for l in links if ONLINE.search(l["titel"]) or ONLINE.search(l["uri"])]
    online_text = [t for k in ("ablauf", "hinweise", "beschreibung", "kurztext") for t in out.get(k, []) if ONLINE.search(t)]
    out["online"] = {"moeglich": bool(online_links or online_text),
                     "links": list({l["uri"]: l for l in online_links}.values())[:3],
                     "hinweis": online_text[0] if online_text else None}
    if raw.get("kennzeichenFormularPersoenlich") is True:
        out["persoenlich"] = True
    gebiet = [g.get("value") for g in raw.get("gueltigkeitGebietID") or [] if g.get("value")]
    out["land"] = LAENDER.get(str(gebiet[0])[:2]) if gebiet else None
    return out


def lookup(leistungsschluessel: str, land: str | None = None, db_path: str | Path | None = None) -> dict:
    """Liefert die ausführlichste Beschreibung für das ermittelte Bundesland."""
    key = re.sub(r"\D", "", leistungsschluessel or "")
    if len(key) != 14 or not land:
        return {"gefunden": False}

    path = Path(db_path) if db_path else faelle.default_db()
    if not path.exists():
        return {"gefunden": False}

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    try:
        rows = con.execute(
            "SELECT raw_json FROM jzufi_services WHERE keys_json LIKE ?",
            (f'%"{key}"%',),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()

    kandidaten = []
    for (raw,) in rows:
        try:
            details = auswerten(json.loads(raw))
        except (ValueError, TypeError):
            continue
        if details.get("land") == land:
            kandidaten.append(details)

    if not kandidaten:
        return {"gefunden": False}

    beste_beschreibung = max(
        kandidaten,
        key=lambda details: sum(len(wert) for wert in details.values() if isinstance(wert, list)),
    )
    return {"gefunden": True, **beste_beschreibung}


def land_aus_text(text: str | None) -> str | None:
    for name in LAENDER.values():
        if text and name in text:
            return name
    return None
