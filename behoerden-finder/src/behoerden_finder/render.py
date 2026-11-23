"""Menschenlesbare Ausgabe des Ergebnisses (für die CLI)."""
from __future__ import annotations

HEADLINES = {
    "zustaendige_behoerde": "Zuständige Behörde",
    "mehrere_kandidaten": "Mehrere mögliche Behörden (die Quellen liefern mehrere Treffer)",
    "naechster_standort": "Keine belegte Zuständigkeitsfläche – nächstgelegene Standorte (nur Anlaufstellen)",
    "ortsunabhaengig": "Zuständigkeit ist nicht vom Wohnort abhängig",
    "kontext_erforderlich": "Zuständigkeit hängt vom Einzelfall ab; keine Ortszuordnung möglich",
    "keine_abdeckung": "Keine belegte Zuständigkeit an diesem Punkt in den integrierten Datenquellen "
                       "(das heißt nicht, dass keine Behörde zuständig ist)",
    "keine_kategorie": "Keine Behördenkategorie ermittelt",
    "kategorie_mehrdeutig": "Behördenkategorie nicht eindeutig",
    "adresse_nicht_gefunden": "Adresse nicht gefunden",
    "resolver_fehler": "Fehler bei der Zuständigkeitsabfrage",
}

MAX_MATCHES = 5

# Die Quellen benennen Kontaktfelder unterschiedlich.
_STREET = ("anschriftStrasse", "strasse")
_NUMBER = ("hausnummer",)
_ZIP = ("anschriftPlz", "plz")
_CITY = ("anschriftOrt", "ort")
_PHONE = ("telefon", "tel")
_MAIL = ("email", "e_mail", "mail")
_WEB = ("homepage", "link", "url", "bayernPortalUrl")


def _first(props: dict, keys) -> str | None:
    for key in keys:
        value = props.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def kontakt(properties: dict | None) -> dict:
    """Adresse, Telefon, E-Mail, Web aus den (je Quelle verschiedenen) Originalattributen."""
    props = properties or {}
    street, number = _first(props, _STREET), _first(props, _NUMBER)
    line = " ".join(x for x in (street, number if street and number and number not in street else None) if x)
    place = " ".join(x for x in (_first(props, _ZIP), _first(props, _CITY)) if x)
    address = ", ".join(x for x in (line, place) if x)
    if not line and props.get("gemfa_municipality") and isinstance(props.get("address"), list):
        for anschrift in props["address"]:
            if not isinstance(anschrift, dict) or not _first(anschrift, ("Strasse",)):
                continue
            art = anschrift.get("{http://www.w3.org/2001/XMLSchema-instance}type")
            if art not in (None, "HausanschriftType"):
                continue
            strasse = _first(anschrift, ("Strasse",))
            ort = " ".join(x for x in (_first(anschrift, ("PLZ",)), _first(anschrift, ("Ort",))) if x)
            address = ", ".join(x for x in (strasse, ort) if x)
            break
    return {k: v for k, v in {"adresse": address or None, "telefon": _first(props, _PHONE),
                              "email": _first(props, _MAIL), "web": _first(props, _WEB)}.items() if v}


def _fmt_distance(meters) -> str | None:
    if not isinstance(meters, (int, float)):
        return None
    if meters < 1000:
        return f"ca. {meters:.0f} m entfernt"
    return f"ca. {f'{meters / 1000:.1f}'.replace('.', ',')} km entfernt"


def render_text(result: dict) -> str:
    lines: list[str] = []
    if result.get("anliegen"):
        lines.append(f"Anliegen:   {result['anliegen']}")
    leistung = result.get("leistung")
    if leistung and leistung.get("titel"):
        key = f" (LeiKa {leistung['leistungsschluessel']})" if leistung.get("leistungsschluessel") else ""
        lines.append(f"Leistung:   {leistung['titel']}{key}")
    cat = result.get("kategorie")
    if cat:
        if cat.get("id"):
            score = f"  · Score {cat['score']}" if cat.get("score") is not None else ""
            origin = {"vorgegeben": " (vorgegeben)",
                      "themenhinweis": " (aus Stichwörtern abgeleitet)",
                      "fallkatalog": " (aus Fall-Katalog abgeleitet, nicht amtlich belegt)"}.get(cat.get("quelle"), "")
            lines.append(f"Kategorie:  {cat.get('label') or cat['id']} [{cat['id']}]{origin}{score}")
        elif cat.get("kandidaten"):
            names = ", ".join(str(c["id"]) for c in cat["kandidaten"][:4])
            lines.append(f"Kategorie:  nicht eindeutig – Kandidaten: {names}")
    ort = result.get("standort")
    if ort:
        name = ort.get("anzeige_name") or result.get("adresse")
        prec = f", {ort['genauigkeit']}" if ort.get("genauigkeit") else ""
        coords = f"lat {ort['lat']:.5f}, lon {ort['lon']:.5f}{prec}"
        if name:
            lines.append(f"Standort:   {name}")
            lines.append(f"            {coords}")
        else:
            lines.append(f"Standort:   {coords}")
    elif result.get("adresse"):
        lines.append(f"Adresse:    {result['adresse']}")

    status = result.get("status")
    lines.append("")
    lines.append(HEADLINES.get(status, status or "Kein Ergebnis") + ":" if status in (
        "zustaendige_behoerde", "mehrere_kandidaten", "naechster_standort") else HEADLINES.get(status, str(status)))
    z = result.get("zustaendigkeit") or {}
    matches = z.get("matches") or []
    for match in matches[:MAX_MATCHES]:
        lines.append(f"  • {match.get('authority_name') or '(ohne Namen)'}")
        info = kontakt(match.get("properties"))
        for label, key in (("Adresse", "adresse"), ("Telefon", "telefon"), ("E-Mail", "email"), ("Web", "web")):
            if key in info:
                lines.append(f"      {label}: {info[key]}")
        geom = match.get("geometry")
        if isinstance(geom, dict) and geom.get("type") == "Point" and not info.get("adresse"):
            lon, lat = geom["coordinates"][:2]
            lines.append(f"      Standort: lat {lat:.5f}, lon {lon:.5f}")
        distance = _fmt_distance(match.get("distance_m"))
        if distance:
            lines.append(f"      {distance}")
        if match.get("source_id"):
            lines.append(f"      Quelle: {match['source_id']}" +
                         (f" · {match['evidence_url']}" if match.get("evidence_url") else ""))
    if len(matches) > MAX_MATCHES:
        lines.append(f"  … und {len(matches) - MAX_MATCHES} weitere (alle mit --json).")
    if result.get("fehler"):
        lines.append(f"  {result['fehler']}")
    for hint in result.get("hinweise") or []:
        lines.append(f"Hinweis: {hint}")
    return "\n".join(lines)
