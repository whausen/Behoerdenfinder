"use strict";
// Behördenfinder – Oberfläche. Ruft /api/suche und /api/vorschlaege des lokalen Servers auf.

const TOPPLUS_GRAU = "https://sgx.geodatenzentrum.de/wmts_topplus_open/tile/1.0.0/web_grau/default/WEBMERCATOR/{z}/{y}/{x}.png";
const FLAECHE = { color: "#007c84", weight: 2, fillColor: "#00a0aa", fillOpacity: 0.25 };
const QUELLE_TEXT = {
  matcher: ["amtliche Daten (FIM/PVOG)", true],
  vorgegeben: ["ausgewählt", true],
  fallkatalog: ["Fall-Katalog, nicht amtlich belegt", false],
  themenhinweis: ["aus Stichwörtern, nicht amtlich belegt", false],
};

const $ = (id) => document.getElementById(id);
const form = $("suche"), out = $("ergebnis"), adresse = $("adresse"), liste = $("vorschlaege"), los = $("los");
let gewaehlt = null;            // {lat, lon, text} aus der Vorschlagsliste
let auswahl = -1;

// --- Karte -------------------------------------------------------------------------------------------------
const karte = L.map("karte", { zoomControl: true }).setView([51.1, 10.4], 6);
L.tileLayer(TOPPLUS_GRAU, {
  maxZoom: 18,
  attribution: '&copy; <a href="https://www.bkg.bund.de">BKG</a> (' + new Date().getFullYear() +
    ') <a href="https://www.govdata.de/dl-de/by-2-0">dl-de/by-2-0</a>, ' +
    '<a href="https://sgx.geodatenzentrum.de/web_public/gdz/datenquellen/datenquellen_topplusopen.html">Datenquellen</a>',
}).addTo(karte);
const ebene = L.featureGroup().addTo(karte);

// --- kleine DOM-Helfer (kein innerHTML mit Fremddaten) ------------------------------------------------------
function el(tag, attrs = {}, ...kinder) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const k of kinder) if (k != null) e.append(k);
  return e;
}
function link(href, text) {
  try {
    const u = new URL(href.includes("://") ? href : "https://" + href);
    if (!["http:", "https:"].includes(u.protocol)) return text || href;
    return el("a", { href: u.href, target: "_blank", rel: "noopener", text: text || href });
  } catch { return text || href; }
}
function entfernung(m) {
  if (typeof m !== "number") return null;
  return m < 1000 ? `ca. ${Math.round(m)} m entfernt` : `ca. ${(m / 1000).toFixed(1).replace(".", ",")} km entfernt`;
}

// --- Adressvorschläge (nur Photon) ----------------------------------------------------------------------------
function geocoder() { return form.elements.geocoder.value; }
let timer = null, letzteAnfrage = 0;
adresse.addEventListener("input", () => {
  gewaehlt = null;
  clearTimeout(timer);
  if (geocoder() !== "photon" || adresse.value.trim().length < 3) return schliessen();
  timer = setTimeout(async () => {
    const nr = ++letzteAnfrage;
    try {
      const r = await fetch("/api/vorschlaege?q=" + encodeURIComponent(adresse.value.trim()));
      const d = await r.json();
      if (nr === letzteAnfrage) zeigeVorschlaege(d.vorschlaege || []);
    } catch { schliessen(); }
  }, 300);
});
function zeigeVorschlaege(v) {
  liste.replaceChildren(...v.map((s, i) => {
    const li = el("li", { role: "option", id: "v" + i, text: s.text });
    li.addEventListener("mousedown", (e) => { e.preventDefault(); waehle(s); });
    return li;
  }));
  liste._daten = v; auswahl = -1;
  liste.hidden = v.length === 0;
}
function waehle(s) { gewaehlt = s; adresse.value = s.text; schliessen(); }
function schliessen() { liste.hidden = true; liste.replaceChildren(); auswahl = -1; }
adresse.addEventListener("keydown", (e) => {
  if (liste.hidden) return;
  const n = liste.children.length;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    auswahl = (auswahl + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    [...liste.children].forEach((li, i) => li.setAttribute("aria-selected", i === auswahl));
  } else if (e.key === "Enter" && auswahl >= 0) {
    e.preventDefault(); waehle(liste._daten[auswahl]);
  } else if (e.key === "Escape") schliessen();
});
adresse.addEventListener("blur", () => setTimeout(schliessen, 100));
form.elements.geocoder.forEach((r) => r.addEventListener("change", () => { gewaehlt = null; schliessen(); }));

// --- Lade-Animation (Amtsstempel mit wechselnden Sprüchen) ---------------------------------------------------
const SPRUECHE = [
  "Anliegen wird zugeordnet …", "Akten werden gewälzt …", "Stempelkissen wird nachgetränkt …",
  "Formular in dreifacher Ausfertigung wird angelegt …", "Zuständigkeit wird geprüft …",
  "Wartemarke Nr. 4711 wird gezogen …", "Amtsschimmel wird gesattelt …", "Aktenzeichen wird vergeben …",
  "Kaffee für die Sachbearbeitung wird gekocht …", "Adresse wird auf der Karte gesucht …",
  "Zuständigkeitsflächen werden ausgemessen …", "Umlaufmappe macht die Runde …",
];
function ladeAnimation() {
  const spruch = el("p", { class: "spruch", text: SPRUECHE[0] });
  const szene = el("div", { class: "stempel-szene", role: "status" },
    el("div", { class: "buehne", "aria-hidden": "true" },
      el("div", { class: "papier" }), el("div", { class: "abdruck", text: "ERLEDIGT" }),
      el("div", { class: "stempel" }, el("div", { class: "griff" }), el("div", { class: "hals" }), el("div", { class: "platte" }))),
    spruch);
  out.replaceChildren(szene);
  let i = 0;
  const t = setInterval(() => {
    spruch.classList.add("weg");
    setTimeout(() => { i = (i + 1 + Math.floor(Math.random() * 2)) % SPRUECHE.length; spruch.textContent = SPRUECHE[i];
                       spruch.classList.remove("weg"); }, 250);
  }, 1600);
  return () => clearInterval(t);
}

// --- Suche ------------------------------------------------------------------------------------------------------
form.addEventListener("submit", (e) => { e.preventDefault(); suche(); });

async function suche(kategorie) {
  const body = { anliegen: $("anliegen").value.trim(), adresse: adresse.value.trim(), geocoder: geocoder() };
  if (kategorie) body.kategorie = kategorie;
  if (gewaehlt && gewaehlt.text === body.adresse) { body.lat = gewaehlt.lat; body.lon = gewaehlt.lon; }
  los.disabled = true; los.textContent = "Suche läuft …";
  const stoppen = ladeAnimation();
  try {
    const r = await fetch("/api/suche", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.fehler || "HTTP " + r.status);
    zeige(d);
  } catch (err) {
    out.replaceChildren(el("p", { class: "fehler", text: "Die Suche ist fehlgeschlagen: " + err.message }));
  } finally {
    stoppen();
    los.disabled = false; los.textContent = "Zuständige Behörde finden";
  }
}

function zeige(d) {
  const teile = [];
  const kat = d.kategorie || {};
  if (d.leistung && d.leistung.titel) {
    const det = d.leistung.details || {};
    const zeile = el("p", { class: "meta" }, el("b", { text: "Leistung: " }), d.leistung.titel);
    if (det.gefunden && det.online) zeile.append(el("span", { class: "badge" + (det.online.moeglich ? " amtlich" : ""),
      text: det.online.moeglich ? "auch online möglich" : "kein Online-Dienst genannt" }));
    teile.push(zeile);
    if (det.gefunden) teile.push(leistungsDetails(det));
  }
  if (kat.id) {
    const [text, amtlich] = QUELLE_TEXT[kat.quelle] || [kat.quelle || "", false];
    teile.push(el("p", { class: "meta" }, el("b", { text: "Behördenart: " }), kat.label || kat.id,
                  el("span", { class: "badge" + (amtlich ? " amtlich" : ""), text })));
  }
  if (d.standort) {
    const s = d.standort;
    teile.push(el("p", { class: "meta" }, el("b", { text: "Standort: " }), s.anzeige_name || `${s.lat.toFixed(5)}, ${s.lon.toFixed(5)}`,
                  s.quelle ? el("span", { class: "badge", text: s.quelle }) : null));
  }
  teile.push(el("h2", { text: d.ueberschrift || "Kein Ergebnis" }));

  if (d.status === "kategorie_mehrdeutig" && (kat.kandidaten || []).length) {
    const box = el("div", {}, el("p", { class: "meta", text: "Bitte wählen Sie die passende Behördenart:" }));
    for (const k of kat.kandidaten.slice(0, 5)) {
      const b = el("button", { type: "button", class: "chip", text: k.id });
      b.addEventListener("click", () => suche(k.id));
      box.append(b);
    }
    teile.push(box);
  }

  const treffer = ((d.zustaendigkeit || {}).matches) || [];
  for (const m of treffer.slice(0, 5)) {
    const k = m.kontakt || {};
    const statusMarke = el("span", { class: "status" });
    const karteEintrag = el("div", { class: "behoerde" }, el("h3", { text: m.authority_name || "(ohne Namen)" }, " ", statusMarke));
    if (k.adresse) karteEintrag.append(el("p", { text: k.adresse }));
    if (k.telefon) karteEintrag.append(el("p", {}, "Tel. ", el("a", { href: "tel:" + k.telefon.replace(/[^\d+]/g, ""), text: k.telefon })));
    if (k.email) karteEintrag.append(el("p", {}, el("a", { href: "mailto:" + k.email, text: k.email })));
    if (k.web) karteEintrag.append(el("p", {}, link(k.web, "Webseite")));
    const dist = entfernung(m.distance_m);
    if (dist) karteEintrag.append(el("p", { class: "quelle", text: dist }));
    if (m.source_id) karteEintrag.append(el("p", { class: "quelle" }, "Quelle: " + m.source_id,
                                            m.evidence_url ? " · " : null, m.evidence_url ? link(m.evidence_url, "Beleg") : null));
    const extra = el("div", { class: "extra" });
    const navi = el("a", { class: "navi", target: "_blank", rel: "noopener", href: navigationsLink(m, null, d.standort),
                           text: "Route in Google Maps" });
    karteEintrag.append(extra, navi);
    if (teile.filter((t) => t.classList && t.classList.contains("behoerde")).length < 3)
      zusatzinfos(m, extra, navi, d.standort, statusMarke, (d.leistung || {}).leistungsschluessel);
    teile.push(karteEintrag);
  }
  if (treffer.length > 5) teile.push(el("p", { class: "meta", text: `… und ${treffer.length - 5} weitere` }));
  if (d.fehler) teile.push(el("p", { class: "fehler", text: d.fehler }));
  if ((d.hinweise || []).length) teile.push(el("ul", { class: "hinweise" }, ...d.hinweise.map((h) => el("li", { text: h }))));
  out.replaceChildren(...teile);
  zeichne(d, treffer);
}

// --- Zusatzinfos über Google (Öffnungszeiten, genaue Anschrift) und Navigation ------------------------------------
function navigationsLink(m, g, standort) {
  const p = new URLSearchParams({ api: "1", travelmode: "transit" });
  const k = m.kontakt || {};
  if (k.adresse) {
    p.set("destination", k.adresse);
  } else if (g && g.gefunden && (g.adresse || g.place_id || g.lat == null)) {
    p.set("destination", g.adresse ? `${g.name || m.authority_name || ""}, ${g.adresse}` : (g.name || m.authority_name));
    if (g.place_id) p.set("destination_place_id", g.place_id);
  } else if (g && g.gefunden) {
    p.set("destination", `${g.lat},${g.lon}`);
  } else if (m.geometry && m.geometry.type === "Point") {
    p.set("destination", `${m.geometry.coordinates[1]},${m.geometry.coordinates[0]}`);
  } else {
    p.set("destination", m.authority_name || "");
  }
  if (standort) p.set("origin", `${standort.lat},${standort.lon}`);
  return "https://www.google.com/maps/dir/?" + p.toString();
}

// --- Geöffnet / geschlossen nach der Uhrzeit des Browsers ----------------------------------------------------
const WOCHENTAG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];
function oeffnungsStatus(intervalle, jetzt = new Date()) {
  if (!Array.isArray(intervalle) || intervalle.length !== 7) return null;
  const tag = (jetzt.getDay() + 6) % 7;                      // 0 = Montag
  const hm = String(jetzt.getHours()).padStart(2, "0") + ":" + String(jetzt.getMinutes()).padStart(2, "0");
  for (const [von, bis] of intervalle[tag]) {
    if (von <= hm && hm < bis) return { offen: true, text: bis === "24:00" ? (von === "00:00" ? "rund um die Uhr geöffnet" : "geöffnet bis Mitternacht") : `geöffnet bis ${bis} Uhr` };
  }
  const spaeter = intervalle[tag].filter(([von]) => von > hm).map(([von]) => von).sort()[0];
  if (spaeter) return { offen: false, text: `geschlossen · öffnet heute um ${spaeter} Uhr` };
  for (let i = 1; i <= 7; i++) {
    const t = (tag + i) % 7, erste = intervalle[t].map(([von]) => von).sort()[0];
    if (erste) return { offen: false, text: `geschlossen · öffnet ${i === 1 ? "morgen" : WOCHENTAG[t]} um ${erste} Uhr` };
  }
  return { offen: false, text: "geschlossen" };
}
function zeigeStatus(marke, intervalle) {
  const aktualisieren = () => {
    const s = oeffnungsStatus(intervalle);
    if (!s || !marke.isConnected) return false;
    marke.className = "status " + (s.offen ? "offen" : "zu");
    marke.textContent = s.text;
    marke.title = "Nach der Uhrzeit Ihres Geräts (" + new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) + " Uhr)";
    return true;
  };
  if (aktualisieren()) { const t = setInterval(() => { if (!aktualisieren()) clearInterval(t); }, 60000); }
}

async function zusatzinfos(m, ziel, navi, standort, statusMarke, leika) {
  const z = m.zentrum || (standort ? { lat: standort.lat, lon: standort.lon } : null);
  ziel.append(el("p", { class: "quelle", text: "Öffnungszeiten werden geladen …" }));
  let g;
  try {
    const r = await fetch("/api/behoerde-details", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: m.authority_name || "", adresse: (m.kontakt || {}).adresse || "", lat: z && z.lat,
                             lon: z && z.lon, punkt: !!(m.geometry && m.geometry.type === "Point"), leika: leika || null,
                             standort_lat: standort && standort.lat, standort_lon: standort && standort.lon,
                             web: (m.kontakt || {}).web || null,
                             ohne_zeiten: !!(m.amtlich && m.amtlich.oeffnungszeiten) }) });
    g = await r.json();
  } catch { g = null; }
  ziel.replaceChildren();
  const a = m.amtlich || {};
  if (a.oeffnungszeiten) g = { ...(g || {}), verfuegbar: true, gefunden: true, oeffnungszeiten: a.oeffnungszeiten,
                               intervalle: a.intervalle, zeiten_quelle: a.zeiten_quelle, zeiten_text: a.zeiten_text };
  if (!g || !g.verfuegbar || !g.gefunden) {
    ziel.append(el("p", { class: "quelle", text: "Öffnungszeiten: in den offenen Daten nicht gefunden – bitte auf der Webseite der Behörde prüfen." }));
    return;
  }
  navi.href = navigationsLink(m, g, standort);
  const k = m.kontakt || {};
  if (g.adresse && !k.adresse) ziel.append(el("p", {}, el("b", { text: "Anschrift: " }), g.adresse));
  if (g.oeffnungszeiten && g.oeffnungszeiten.length) {
    const heute = (new Date().getDay() + 6) % 7;
    ziel.append(el("p", {}, el("b", { text: "Öffnungszeiten" })),
      el("ul", { class: "zeiten" }, ...g.oeffnungszeiten.map((t, i) => el("li", { text: t, class: g.oeffnungszeiten.length === 7 && i === heute ? "heute" : "" }))));
    if (g.zeiten_quelle) ziel.append(el("p", { class: "quelle" }, "Öffnungszeiten laut " + g.zeiten_quelle,
      g.zeiten_url ? " · " : null, g.zeiten_url ? link(g.zeiten_url, "Seite") : null,
      /Webseite/.test(g.zeiten_quelle) ? " – bitte prüfen" : ""));
    if (g.zeiten_text) ziel.append(el("p", { class: "quelle", text: "Originalangabe: „" + g.zeiten_text + "“" }));
    if (statusMarke) zeigeStatus(statusMarke, g.intervalle);
  } else {
    ziel.append(el("p", { class: "quelle", text: "Öffnungszeiten: in den offenen Daten nicht eingetragen – bitte auf der Webseite der Behörde prüfen." }));
  }
  if (g.telefon && !k.telefon) ziel.append(el("p", {}, "Tel. ", el("a", { href: "tel:" + g.telefon.replace(/[^\d+]/g, ""), text: g.telefon })));
  if (g.web && !k.web) ziel.append(el("p", {}, link(g.web, "Webseite")));
  if (g.online_dienste && g.online_dienste.length)
    ziel.append(el("p", {}, el("b", { text: "Online erledigen: " }),
      ...g.online_dienste.flatMap((o, i) => [i ? " · " : null, link(o.uri, o.titel)]).filter(Boolean)));
  if (g.email && !k.email) ziel.append(el("p", {}, el("a", { href: "mailto:" + g.email, text: g.email })));
  const osm = g.quelle === "osm";
  if (g.quelle) ziel.append(el("p", { class: "quelle" }, "Zusatzinfos: ",
    osm ? link("https://www.openstreetmap.org/copyright", "© OpenStreetMap-Mitwirkende (ODbL)") : "Daten von Google",
    g.maps_url ? " · " : null, g.maps_url ? link(g.maps_url, osm ? "Objekt in OSM" : "in Google Maps") : null,
    " – Treffer zum Behördennamen, bitte prüfen"));
}

// --- Leistungsbeschreibung (aus den gespeicherten Portalverbund-Daten) ---------------------------------------
function punktListe(items) { return el("ul", { class: "punkte" }, ...items.map((t) => el("li", { text: t }))); }
function linkliste(items) { return el("ul", { class: "punkte" }, ...items.map((l) => el("li", {}, link(l.uri, l.titel)))); }

function leistungsDetails(det) {
  const box = el("details", { class: "leistung" }, el("summary", { text: "Mehr zur Leistung: Unterlagen, Kosten, Ablauf" }));
  const abschnitt = (titel, inhalt) => { if (inhalt) box.append(el("h4", { text: titel }), inhalt); };
  const kurz = det.kurztext || (det.beschreibung || []).slice(0, 2);
  if (kurz && kurz.length) box.append(el("p", { class: "kurz", text: kurz.join(" ") }));
  if (det.online) {
    const o = det.online;
    if (o.moeglich) {
      const p = el("p", {}, el("b", { text: "Online erledigen: " }), o.hinweis ? o.hinweis : "laut Beschreibung möglich");
      box.append(p);
      if (o.links.length) box.append(linkliste(o.links));
    } else if (det.persoenlich) {
      box.append(el("p", {}, el("b", { text: "Online erledigen: " }), "in der Beschreibung ist kein Online-Dienst genannt; persönliches Erscheinen bzw. Formular vorgesehen."));
    }
  }
  abschnitt("Was Sie brauchen", det.unterlagen && det.unterlagen.length ? punktListe(det.unterlagen) : null);
  abschnitt("Voraussetzungen", det.voraussetzungen && punktListe(det.voraussetzungen));
  abschnitt("Kosten", det.kosten && punktListe(det.kosten));
  abschnitt("Fristen", det.fristen && punktListe(det.fristen));
  abschnitt("Bearbeitungsdauer", det.dauer && punktListe(det.dauer));
  abschnitt("Ablauf", det.ablauf && punktListe(det.ablauf.slice(0, 8)));
  abschnitt("Hinweise", det.hinweise && punktListe(det.hinweise.slice(0, 5)));
  abschnitt("Formulare", det.formulare && linkliste(det.formulare.slice(0, 6)));
  const quelle = el("p", { class: "quelle" }, "Quelle: Leistungsbeschreibung im Portalverbund" +
    (det.land ? ` (${det.land})` : "") + " – Regelungen können je nach Bundesland abweichen.");
  if (det.portal && det.portal.length) quelle.append(" · ", link(det.portal[0].uri, "Originalseite"));
  box.append(quelle);
  return box;
}

function zeichne(d, treffer) {
  ebene.clearLayers();
  for (const m of treffer) {
    const g = m.geometry;
    if (!g) continue;
    if (g.type === "Point") {
      L.circleMarker([g.coordinates[1], g.coordinates[0]], { radius: 7, color: "#007c84", weight: 2,
        fillColor: "#00a0aa", fillOpacity: 0.9 }).bindTooltip(m.authority_name || "").addTo(ebene);
    } else {
      L.geoJSON(g, { style: FLAECHE }).bindTooltip(m.authority_name || "", { sticky: true }).addTo(ebene);
    }
  }
  if (d.standort) {
    L.circleMarker([d.standort.lat, d.standort.lon], { radius: 6, color: "#000", weight: 2, fillColor: "#fff",
      fillOpacity: 1 }).bindTooltip("Ihre Adresse").addTo(ebene);
  }
  if (ebene.getLayers().length) karte.fitBounds(ebene.getBounds(), { padding: [30, 30], maxZoom: 15 });
}
