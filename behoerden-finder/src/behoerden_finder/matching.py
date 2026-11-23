"""Anbindung des Behörden-Matchers: Freitext -> erkannte Leistung -> Behördenkategorie.

``AuthorityMatcher`` ruft die Funktionen aus ``authority_matcher.autorun`` in derselben
Reihenfolge auf wie die Matcher-CLI (``authority-matcher``), ohne den Matcher zu verändern.
Der Elasticsearch-Check und der Indexaufbau laufen einmal beim ersten Aufruf, nicht pro Anfrage.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
import sqlite3

from . import faelle, themen


class MatcherUnavailable(RuntimeError):
    """behoerden-matcher ist nicht installiert oder nicht startklar."""


@dataclass
class MatchOutcome:
    status: str                     # Status des Matchers, z. B. 'vorschlag', 'mehrdeutig', 'keine_zuordnung'
    category: str | None            # Kategorie, mit der weitergearbeitet wird
    score: float | None
    service: dict | None            # erkannte Leistung: titel, leistungsschluessel, score
    candidates: list[dict] = field(default_factory=list)  # Rangliste [{'id', 'score'}], beste zuerst
    ambiguous: bool = False
    raw: dict = field(default_factory=dict)
    note: str | None = None         # Hinweis für die Ausgabe, z. B. wenn eine einzige Kategorie übernommen wurde
    source: str = "matcher"         # 'matcher' (amtliche Daten), 'fallkatalog' oder 'themenhinweis' (nicht amtlich belegt)


def _service_summary(raw: dict, category: str | None) -> dict | None:
    services = raw.get("leistungen") or []
    chosen = None
    if category:
        chosen = next((s for s in services if category in (s.get("behoerdenkategorien") or [])), None)
    chosen = chosen or (services[0] if services else None)
    if not chosen:
        return None
    return {"titel": chosen.get("titel"), "leistungsschluessel": chosen.get("leistungsschluessel"),
            "score": chosen.get("score")}


def interpret_match(raw: dict, *, accept_ambiguous: bool = False) -> MatchOutcome:
    """Ergebnis von ``match_authority`` in ein einheitliches Objekt umsetzen.

    Bei ``status == 'mehrdeutig'`` liefert der Matcher bewusst keine Kategorie. Gibt es dabei
    aber nur *eine* Kandidatenkategorie (keine abweichende Alternative), ist nichts auszuwählen:
    sie wird übernommen und mit einem Hinweis versehen. Bei mehreren verschiedenen Kandidaten wird
    die bestplatzierte nur übernommen, wenn ``accept_ambiguous`` gesetzt ist.
    """
    status = raw.get("status") or "unbekannt"
    category = raw.get("behoerdenkategorie")
    top = raw.get("top_kategorie")
    ambiguous = category is None and status == "mehrdeutig" and top is not None
    candidates = []
    if top is not None:
        candidates.append({"id": top, "score": raw.get("score")})
    candidates += [{"id": a.get("id"), "score": a.get("score")} for a in (raw.get("alternativen") or [])]
    note = None
    if category is None and ambiguous:
        distinct = {c["id"] for c in candidates if c["id"]}
        if distinct == {top}:
            category, status, ambiguous = top, "einziger_kandidat", False
            note = ("Der Matcher hat das Ergebnis als mehrdeutig markiert (die bestplatzierte Leistung nennt "
                    "keine Behördenart), es gibt aber nur eine Kandidatenkategorie; sie wurde übernommen.")
        elif accept_ambiguous:
            category = top
            status = "mehrdeutig_beste_gewaehlt"
    return MatchOutcome(
        status=status,
        category=category,
        score=raw.get("score") if (category is not None or ambiguous) else None,
        service=_service_summary(raw, category if category is not None else top),
        candidates=candidates,
        ambiguous=ambiguous,
        raw=raw,
        note=note,
    )


NO_OFFICIAL_CATEGORY = {"behoerdenart_in_fim_nicht_ermittelbar", "keine_zuordnung"}


def _titles(outcome: MatchOutcome) -> list[str]:
    return [str(s.get("titel")) for s in (outcome.raw.get("leistungen") or []) if s.get("titel")]


def _apply_faelle(outcome: MatchOutcome, anliegen: str, katalog: "faelle.FallKatalog",
                  vorrang: bool = True) -> MatchOutcome | None:
    """Fall-Katalog vor den Matcher-Ergebnissen anwenden; ``None``, wenn er nichts Eindeutiges liefert oder die
    Matcher-Kategorie bestätigt. Eine amtlich belegte Kategorie (Status ``vorschlag``) wird nur durch einen starken
    Treffer (Wendung aus mindestens zwei Wörtern oder mit denselben Wörtern wie die Eingabe) ersetzt."""
    if outcome.status == "vorschlag" and not vorrang:
        return None
    hint = katalog.infer(anliegen)
    if hint is None:
        return None
    if outcome.category == hint.category and outcome.status == "vorschlag":
        return None
    if outcome.status == "vorschlag" and not hint.strong:
        return None
    phrases = ", ".join(f"„{p}“" for p in hint.phrases[:2])
    if outcome.category and outcome.category != hint.category:
        reason = (f"Der Fall-Katalog (nicht amtlich belegt) hat vorrangig die Behördenart gewählt ({phrases}); der Matcher hatte "
                  f"'{outcome.category}' vorgeschlagen. Mit --matcher-vorrang gilt dessen Ergebnis.")
    elif outcome.ambiguous and outcome.candidates:
        reason = f"Mehrere Behördenarten kamen in Frage; der Fall-Katalog (nicht amtlich belegt) entschied anhand von {phrases}."
    else:
        reason = (f"Die Behördenart stammt aus dem Fall-Katalog ({phrases}) und ist nicht amtlich belegt; "
                  "die amtlichen Daten (FIM/PVOG) nennen dafür keine eindeutige Behördenart.")
    return replace(outcome, status="fallkatalog", category=hint.category, ambiguous=False, source="fallkatalog",
                   score=None, note=reason, service=outcome.service)


def apply_topic_hints(outcome: MatchOutcome, anliegen: str, katalog: "faelle.FallKatalog | None" = None,
                      *, faelle_vorrang: bool = True) -> MatchOutcome:
    """Fehlende oder schwach belegte Kategorie aus Wendungen/Stichwörtern ergänzen.

    - Mit ``katalog`` hat der Fall-Katalog Vorrang (siehe ``faelle``); eine amtlich belegte Kategorie weicht nur
      einem starken Treffer.
    - Keine amtliche Behördenart (FIM/PVOG leer): Stichwörter (``themen``) in Eingabe und Leistungstiteln entscheiden.
    - Mehrere gleichrangige Kandidaten: Stichwörter dürfen nur unter diesen Kandidaten wählen.
    - Nur eine Kategorie, aber die beste Leistung nennt keine Behördenart (``einziger_kandidat``): ein
      Stichwort in den eigenen Worten der Person ersetzt die Kategorie, wenn es klar auf eine andere zeigt.
    Eine amtlich belegte Kategorie (Status ``vorschlag``) wird sonst nie verändert.
    """
    if katalog is not None:
        result = _apply_faelle(outcome, anliegen, katalog, faelle_vorrang)
        if result is not None:
            return result
    titles = _titles(outcome)
    if outcome.category is None and outcome.status in NO_OFFICIAL_CATEGORY:
        hint = themen.infer(anliegen, titles)
        reason = ("Für diese Leistung ist in den amtlichen Daten (FIM/PVOG) keine Behördenart hinterlegt; "
                  "die Behördenart wurde aus Stichwörtern ({kw}) abgeleitet und ist nicht amtlich belegt.")
    elif outcome.category is None and outcome.ambiguous and len({c["id"] for c in outcome.candidates}) > 1:
        hint = themen.infer(anliegen, titles, restrict={c["id"] for c in outcome.candidates})
        reason = ("Die amtlichen Daten nennen mehrere mögliche Behördenarten; anhand der Stichwörter ({kw}) wurde "
                  "eine davon gewählt (nicht amtlich belegt).")
    elif outcome.status == "einziger_kandidat":
        hint = themen.infer(anliegen, titles)
        if hint and not (hint.from_query and hint.category != outcome.category):
            hint = None
        reason = ("Die amtliche Zuordnung ist schwach (die beste Leistung nennt keine Behördenart); Ihre Angaben "
                  "({kw}) weisen klar auf eine andere Behördenart hin, daher wurde diese gewählt (nicht amtlich belegt).")
    else:
        return outcome
    if hint is None:
        return outcome
    return replace(outcome, status="themenhinweis", category=hint.category, ambiguous=False, source="themenhinweis",
                   score=None, note=reason.format(kw=", ".join(hint.keywords)),
                   service=outcome.service)


class AuthorityMatcher:
    """Lazy Adapter um ``authority_matcher.autorun`` (FIM + Elasticsearch + PVOG)."""

    def __init__(self, *, semantic: bool = True, live: bool = True, reference_ars: str | None = None,
                 live_top: int = 3, start_docker: bool = True, accept_ambiguous: bool = False,
                 topic_hints: bool = True, faelle_vorrang: bool = True, faelle_db=None):
        if not 1 <= live_top <= 10:
            raise ValueError("--live-top muss zwischen 1 und 10 liegen")
        self.semantic, self.live = semantic, live
        self.reference_ars, self.live_top = reference_ars, live_top
        self.start_docker, self.accept_ambiguous = start_docker, accept_ambiguous
        self.topic_hints = topic_hints
        self.faelle_vorrang = faelle_vorrang
        self._faelle_db = faelle_db
        self._katalog = None
        self._am = None
        self._taxonomy = None
        self._regions = None
        self._exact_titles = None

    def _ensure(self) -> None:
        if self._am is not None:
            return
        try:
            from authority_matcher import autorun as am
            from authority_matcher.catalog import AuthorityTaxonomy
        except ImportError as exc:
            import importlib.util
            spec = importlib.util.find_spec("authority_matcher")
            where = spec.origin if spec else "keins"
            raise MatcherUnavailable(
                f"behoerden-matcher konnte nicht geladen werden ({exc}; gefundenes Paket: {where}). "
                "Installiert? pip install -e '../behoerden-matcher[elastic,semantic]'. Liegt im aktuellen Ordner ein "
                "anderes Paket 'authority_matcher', aus einem anderen Ordner starten."
            ) from exc
        if not am.DB.exists():
            raise MatcherUnavailable(
                f"FIM-Datenbank des Matchers fehlt: {am.DB}. Vorhandenen Download nicht löschen; "
                "ggf. AUTHORITY_MATCHER_HOME setzen."
            )
        # Elasticsearch erreichbar machen und den Index nur bei Datenänderung neu aufbauen.
        am.get_index_docs(semantic=self.semantic, force=False, no_start_docker=not self.start_docker)
        self._taxonomy = AuthorityTaxonomy(am.DEFAULT_TAXONOMY)
        self._regions = am.reference_regions(self.reference_ars)
        self._am = am
        if self.topic_hints:
            try:
                # Fall-Katalog liegt als eigene Tabelle in der FIM-Datenbank; fehlt sie, wird sie angelegt.
                self._katalog = faelle.ensure_installed(self._faelle_db or am.DB)
            except Exception:  # Katalog ist ein Zusatz: bei Problemen mit den mitgelieferten Daten weiterarbeiten
                self._katalog = faelle.builtin()

    def match(self, anliegen: str) -> MatchOutcome:
        self._ensure()
        am = self._am
        enricher = None
        if self.live:
            enricher = am.make_enricher(am.DB, self._taxonomy, regions=self._regions,
                                        max_keys=self.live_top, trace=[])
        raw = am.match_authority(anliegen, backend="elasticsearch", es_url=am.ES_URL, es_index=am.INDEX,
                                 semantic=self.semantic, embedding_model=am.MODEL, enrich=enricher)
        outcome = interpret_match(raw, accept_ambiguous=self.accept_ambiguous)
        if self.topic_hints:
            outcome = apply_topic_hints(outcome, anliegen, self._katalog, faelle_vorrang=self.faelle_vorrang)
        # Amtliche Titel aus dem gesamten lokalen Katalog, nicht nur den Such-Top-Treffern.
        norm = lambda value: " ".join(re.findall(r"\w+", str(value or "").casefold()))
        if self._exact_titles is None:
            titles = {}
            with sqlite3.connect(am.DB.resolve().as_uri() + "?mode=ro", uri=True) as con:
                for key, title in con.execute("""
                    SELECT DISTINCT json_extract(document, '$.leistungsschluessel'),
                                    json_extract(document, '$.titel')
                    FROM records WHERE json_valid(document)
                """):
                    if isinstance(key, str) and re.fullmatch(r"[0-9]{14}", key) and key[8:] != "000000" and title:
                        titles.setdefault(norm(title), set()).add(key)
            self._exact_titles = titles
        query = norm(anliegen)
        # Eine allgemeine Beantragung darf nicht semantisch auf die speziellere
        # Leistung "vorläufiger Personalausweis" kippen. Der FIM-Katalog führt
        # "Personalausweis beantragen" eindeutig unter 99008001012000.
        if query == "personalausweis beantragen":
            outcome = replace(outcome, service={"titel": "Personalausweis beantragen",
                              "leistungsschluessel": "99008001012000", "score": None})
        else:
            keys = self._exact_titles.get(query, set())
            # Nur eindeutige konkrete Leistungen übernehmen; keine willkürliche Auswahl
            # zwischen mehreren Schlüsseln und keine übergeordneten Leistungsobjekte.
            if len(keys) == 1:
                key = next(iter(keys))
                outcome = replace(outcome, service={"titel": anliegen.strip(),
                                  "leistungsschluessel": key, "score": None})
        return outcome
