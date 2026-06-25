// Montreal Isochrone — MapLibre frontend.
// Free basemap via OpenFreeMap (no API key, no usage quota).
//
// Performance model: the server computes ONE isochrone at the max budget and
// tags every stop/segment with its travel time (seconds from departure). The
// budget is then a pure client-side filter — instant, follows the knob.

const MONTREAL = [-73.5616, 45.5152];
const MAX_BUDGET = 90;
const BUS_COLOR = "#b06d86";       // one muted rose for all bus lines — dulled so the metro/REM/exo spine stays the focus

// hex geometry — must match engine/walk.py. The server sends every walkable hex
// as [travel, q, r]; we build the polygon here. We render an OPAQUE grey hex
// grid over the WHOLE bbox (land + water), and the budget filter punches the
// reachable cells out to reveal the colour map. A full grid tiles exactly, so a
// tiny oversize (just to kill antialias hairlines) is all we need — no erosion.
// HEX_M / HEX_SX / HEX_SY / HEX_SQ3 + hexKey() come from hex.js (loaded first) —
// shared with the engine's projection and CI-checked against it.
const HEX_OVERSIZE = 1.03;
const SENTINEL = 1e9;                    // travel for unreachable cells -> always grey
// Extent of the grey/desaturation zone. Extended SOUTH (and a touch EAST) past the
// walk-graph's OSM_BBOX (45.40,-74.05,45.75,-73.38) so the south-shore bus lines
// are visible, and so the box re-centres on downtown (~45.51,-73.57) — it's the
// view's centre when fully zoomed out. The grid is built a margin beyond this box
// so the letterbox at min-zoom (box wider than the viewport is tall) stays greyed.
const BBOX = { latMin: 45.27, lonMin: -74.05, latMax: 45.75, lonMax: -73.28 };
const BBOX_MARGIN_LAT = 0.05, BBOX_MARGIN_LON = 0.02;
function hexCorners(q, r) {
  const s = HEX_M * HEX_OVERSIZE;
  const cx = HEX_M * HEX_SQ3 * (q + r / 2), cy = HEX_M * 1.5 * r;
  const p = [];
  for (let i = 0; i < 6; i++) {
    const a = (60 * i - 30) * Math.PI / 180;
    p.push([(cx + s * Math.cos(a)) / HEX_SX, (cy + s * Math.sin(a)) / HEX_SY]);
  }
  p.push(p[0]);
  return p;
}

// Enumerate every hex cell whose centre falls in the bbox. Built once; the grid
// geometry never changes — only each cell's `travel` (sentinel vs real) does.
let veilFeatures = null;                 // stable Feature[] over the full grid
let idxByKey = null;                     // "q,r" -> index into veilFeatures
function buildVeilGrid() {
  const rOf = (lat) => lat * HEX_SY / (HEX_M * 1.5);
  const qOf = (lon, r) => lon * HEX_SX / (HEX_M * HEX_SQ3) - r / 2;
  const latLo = BBOX.latMin - BBOX_MARGIN_LAT, latHi = BBOX.latMax + BBOX_MARGIN_LAT;
  const lonLo = BBOX.lonMin - BBOX_MARGIN_LON, lonHi = BBOX.lonMax + BBOX_MARGIN_LON;
  const rMin = Math.floor(rOf(latLo)) - 1, rMax = Math.ceil(rOf(latHi)) + 1;
  veilFeatures = [];
  idxByKey = new Map();
  for (let r = rMin; r <= rMax; r++) {
    const qA = qOf(lonLo, r), qB = qOf(lonHi, r);
    const qMin = Math.floor(Math.min(qA, qB)) - 1, qMax = Math.ceil(Math.max(qA, qB)) + 1;
    for (let q = qMin; q <= qMax; q++) {
      const lon = (HEX_M * HEX_SQ3 * (q + r / 2)) / HEX_SX, lat = (HEX_M * 1.5 * r) / HEX_SY;
      if (lat < latLo || lat > latHi || lon < lonLo || lon > lonHi) continue;
      idxByKey.set(q + "," + r, veilFeatures.length);
      veilFeatures.push({
        type: "Feature",
        id: veilFeatures.length,           // stable id for feature-state reveal
        properties: {},
        geometry: { type: "Polygon", coordinates: [hexCorners(q, r)] },
      });
    }
  }
}

const map = new maplibregl.Map({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/liberty",   // colourful base
  center: MONTREAL,
  zoom: 11,
  maxPitch: 0,            // 2D only — no tilt axis (lighter to render)
  pitchWithRotate: false,
  fadeDuration: 0,
  renderWorldCopies: false,   // never render repeated world copies (cheaper)
  // lock the view to the data extent so you can't pan/zoom out past the grey
  // zone (the hex grid only covers OSM_BBOX, with a small margin). maxBounds =
  // the bbox exactly (the grid extends a hair beyond, so the viewport is always
  // fully covered); minZoom keeps the bbox filling the viewport.
  maxBounds: [[BBOX.lonMin, BBOX.latMin], [BBOX.lonMax, BBOX.latMax]],
  minZoom: 9.8,
  attributionControl: false,   // surfaced (with everything else) in the bottom-right info bubble
});
map.touchPitch.disable(); // keep pan + zoom + rotate; drop the pitch gesture

// Two lightweight overlay maps (no basemap tiles — empty transparent style)
// stacked exactly over the main one and kept in lock-step with its camera:
//  • maskMap  — draws ONLY the grey hex grid; its container uses CSS
//    `mix-blend-mode: saturation` (index.html), desaturating the colour map
//    *behind it* wherever the grid is painted (unreachable) while keeping detail.
//    The budget filter hides the reachable hexes, leaving them in full colour.
//  • spineMap — draws ONLY the transit spine, ABOVE the mask so the coloured
//    lines are never desaturated; they cut cleanly through the grey along their
//    exact geometry (no hexes).
function overlayMap(container) {
  return new maplibregl.Map({
    container,
    style: { version: 8, sources: {}, layers: [] },   // transparent, no tiles
    center: MONTREAL, zoom: 11, maxPitch: 0,
    interactive: false, attributionControl: false, fadeDuration: 0,
    renderWorldCopies: false,
  });
}
const maskMap = overlayMap("maskmap");
const spineMap = overlayMap("spinemap");
// The overlays keep the base map's DEFAULT pixel ratio so they stay pixel-aligned
// with it across device-pixel-ratio changes (browser zoom, or dragging the window
// between a retina and non-retina monitor). A previous optimisation pinned the
// mask to a fixed lower pixelRatio at load; because it was never re-applied, a
// later DPI change left the fog rendered with a visible, sticky offset.
let maskReady = false, spineReady = false;
function syncOverlays() {
  const cam = {
    center: map.getCenter(), zoom: map.getZoom(),
    bearing: map.getBearing(), pitch: map.getPitch(),
  };
  maskMap.jumpTo(cam);
  spineMap.jumpTo(cam);
}
map.on("move", syncOverlays);
// Keep the overlays locked to the main map's ACTUAL camera on every change — not
// only manual pans. resize() can re-clamp the centre (maxBounds) without firing
// "move", and the initial camera settles on load; re-syncing here closes both gaps.
map.on("load", syncOverlays);
map.on("resize", () => { maskMap.resize(); spineMap.resize(); syncOverlays(); });

// Shed per-frame work WHILE the camera is moving, restore it the moment it stops:
//  • drop the panels' backdrop blur (CSS .panning), and
//  • hide the spine arrows (a symbol layer with `symbol-placement: line`, whose
//    label placement is recomputed every frame — expensive during a pan).
// Re-placed once on moveend, so the only cost is at rest.
map.on("movestart", () => {
  document.body.classList.add("panning");
  if (spineReady && spineMap.getLayer("spine-arrows"))
    spineMap.setLayoutProperty("spine-arrows", "visibility", "none");
});
map.on("moveend", () => {
  syncOverlays();                 // final resync once the camera settles (self-heal)
  document.body.classList.remove("panning");
  if (spineReady && spineMap.getLayer("spine-arrows"))
    spineMap.setLayoutProperty("spine-arrows", "visibility", "visible");
});

const state = { origin: null, time: "08:00", modes: new Set(["metro", "bus", "rail"]), budget: 30 };
let originMarker = null;
let ready = false;
let fogCutoff = state.budget * 60;      // current reachable cutoff (for the reveal)

const $ = (id) => document.getElementById(id);
const budgetEl = $("budget"), statusEl = $("status"), timeEl = $("time");
const fmtTime = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

// --- i18n: fully bilingual UI, French by default (Québec). Static text carries
// data-i18n keys (translated in applyLang); dynamic strings (status, legend) read
// the active language at render time. EXO line names + "Montréal Isochrone" are
// proper nouns, kept as-is. Choice persists in localStorage. ---
const I18N = {
  fr: {
    hint: "Cliquez n'importe où pour déposer un point de départ — la carte s'illumine en couleur partout où vous pouvez vous rendre en transport collectif dans le temps imparti.",
    departAt: "Départ à", timeBudget: "Temps de trajet",
    dragScroll: "glissez ou défilez — mise à jour instantanée",
    modes: "Modes", modeMetro: "Métro", modeBus: "Bus", modeRail: "REM / Train",
    legendHead: "Légende", reachKey: "Couleur = accessible · gris = hors d'atteinte",
    grpReach: "Accessibilité", grpMetro: "Métro", grpRem: "REM", grpTrain: "Train · exo", grpBus: "Bus",
    remLabel: "REM (train léger)", allBus: "Tous les circuits d'autobus", linePrefix: "Ligne ",
    metro: { "1": "Ligne verte", "2": "Ligne orange", "4": "Ligne jaune", "5": "Ligne bleue" },
    loading: "Chargement du réseau…",
    ready: (d) => `Prêt · date de service ${d}. Cliquez sur la carte pour commencer.`,
    computing: "Calcul en cours…",
    result: (n, m, ms) => `${n} arrêts accessibles en ${m} min ou moins · ${ms} ms`,
    apiErr: "Impossible de joindre l'API. Le serveur est-il démarré ?",
    queryErr: "Échec de la requête.",
    questsTitle: "Quêtes secondaires",
    questsHint: "Cliquez sur la carte pour des idées de sorties vers les confins du réseau.",
    questsNone: "Rien d'épinglé à portée pour ce budget — montez le temps.",
    questEachWay: (m) => `~${m} min aller`,
    questOuting: (d) => `sortie d'environ ${d}`,
    questMore: "Plus d'info", questMaps: "Maps", questShuffle: "Autres idées",
  },
  en: {
    hint: "Click anywhere to drop a start point — the map lights up in colour wherever you can travel by transit within the budget.",
    departAt: "Depart at", timeBudget: "Time budget",
    dragScroll: "drag or scroll — updates instantly",
    modes: "Modes", modeMetro: "Metro", modeBus: "Bus", modeRail: "REM / Train",
    legendHead: "Legend", reachKey: "Colour = reachable · grey = out of reach",
    grpReach: "Reachability", grpMetro: "Métro", grpRem: "REM", grpTrain: "Train · exo", grpBus: "Bus",
    remLabel: "REM (light rail)", allBus: "All bus routes", linePrefix: "Line ",
    metro: { "1": "Green Line", "2": "Orange Line", "4": "Yellow Line", "5": "Blue Line" },
    loading: "Loading network…",
    ready: (d) => `Ready · service date ${d}. Click the map to start.`,
    computing: "Computing…",
    result: (n, m, ms) => `${n} stops reachable within ${m} min · ${ms} ms`,
    apiErr: "Could not reach API. Is the server running?",
    queryErr: "Query failed.",
    questsTitle: "Side quests",
    questsHint: "Click the map for outing ideas out toward the edge of the network.",
    questsNone: "Nothing pinned in range for this budget — raise the time.",
    questEachWay: (m) => `~${m} min each way`,
    questOuting: (d) => `about ${d} out`,
    questMore: "More info", questMaps: "Maps", questShuffle: "Shuffle",
  },
};
let lang = localStorage.getItem("lang") === "en" ? "en" : "fr";   // FR default (QC)
const t = (k) => I18N[lang][k];

// status is contextual, so we keep its state and re-render it on language change
let statusState = { kind: "loading" };
function renderStatus() {
  const s = statusState, d = I18N[lang];
  statusEl.textContent =
    s.kind === "ready" ? d.ready(s.date) :
    s.kind === "result" ? d.result(s.n, s.m, s.ms) :
    s.kind === "computing" ? d.computing :
    s.kind === "apiErr" ? d.apiErr :
    s.kind === "queryErr" ? d.queryErr : d.loading;
}
function setStatus(kind, extra) { statusState = Object.assign({ kind }, extra); renderStatus(); }

function applyLang() {
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("#lang button").forEach((b) => b.classList.toggle("on", b.dataset.lang === lang));
  renderStatus();
  renderLegend();
  renderInfo();
  if (typeof renderQuests === "function") renderQuests();
}
$("lang").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  lang = b.dataset.lang;
  localStorage.setItem("lang", lang);
  applyLang();
});

// --- bottom-right info bubble: data sources, credits & data-collection policy.
// Built here (not via data-i18n) because it carries links and structure. ---
const REPO_URL = "https://github.com/HenriLD/mtl_isochrone";
const A = (href, txt) => `<a href="${href}" target="_blank" rel="noopener">${txt}</a>`;
function renderInfo() {
  const card = $("infoCard"); if (!card) return;
  const body = card.querySelector(".ibody");
  const osm = A("https://www.openstreetmap.org/copyright", lang === "fr" ? "les contributeurs d'OpenStreetMap" : "OpenStreetMap contributors");
  body.innerHTML = lang === "fr" ? `
    <h2 id="infoTitle">À propos · données</h2>
    <p>Carte libre et gratuite de tout ce qu'on peut atteindre en transport collectif depuis n'importe quel point de la région de Montréal.</p>
    <h3>Sources de données</h3>
    <ul>
      <li><b>Horaires de transport</b> — données ouvertes GTFS de la STM, d'exo et du REM.</li>
      <li><b>Carte et réseau piéton</b> — © ${osm} (ODbL).</li>
      <li><b>Fond de carte</b> — ${A("https://openfreemap.org/", "OpenFreeMap")} · © ${A("https://openmaptiles.org/", "OpenMapTiles")}.</li>
      <li><b>Quêtes (textes et photos)</b> — OpenStreetMap et ${A("https://commons.wikimedia.org/", "Wikidata / Wikimedia Commons")} ; l'auteur et la licence de chaque photo accompagnent les données.</li>
    </ul>
    <h3>Confidentialité</h3>
    <p>Aucun compte, aucun témoin (cookie), aucun traceur ni outil d'analyse tiers. Votre choix de langue est la seule donnée conservée — dans votre navigateur. Les clics sur la carte sont envoyés au serveur uniquement pour calculer les trajets : ils sont traités en mémoire et ne sont jamais enregistrés dans une base de données.</p>
    <div class="ifoot">Logiciel libre — ${A(REPO_URL, "code source sur GitHub")}. © ${osm}.</div>
  ` : `
    <h2 id="infoTitle">About &amp; data</h2>
    <p>A free, open map of everywhere you can reach by public transit from any point in the Montréal region.</p>
    <h3>Data sources</h3>
    <ul>
      <li><b>Transit schedules</b> — STM, exo &amp; REM open GTFS feeds.</li>
      <li><b>Map &amp; walking network</b> — © ${osm} (ODbL).</li>
      <li><b>Basemap tiles</b> — ${A("https://openfreemap.org/", "OpenFreeMap")} · © ${A("https://openmaptiles.org/", "OpenMapTiles")}.</li>
      <li><b>Side-quest text &amp; photos</b> — OpenStreetMap and ${A("https://commons.wikimedia.org/", "Wikidata / Wikimedia Commons")}; each photo's author &amp; licence travel with the data.</li>
    </ul>
    <h3>Privacy</h3>
    <p>No accounts, no cookies, no third-party trackers or analytics. Your language choice is the only thing stored — in your own browser. Map clicks are sent to the server only to compute routes: they're processed in memory and never saved to a database.</p>
    <div class="ifoot">Open source — ${A(REPO_URL, "view the code on GitHub")}. © ${osm}.</div>
  `;
}
function setInfoOpen(open) {
  const card = $("infoCard"), btn = $("infoBtn");
  card.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
}
$("infoBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  setInfoOpen($("infoCard").hidden);   // toggle
});
// click outside or Escape closes the popover
document.addEventListener("click", (e) => {
  const card = $("infoCard");
  if (!card.hidden && !card.contains(e.target) && e.target !== $("infoBtn")) setInfoOpen(false);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setInfoOpen(false); });

// --- controls ---
// paint the filled portion of a range input (native fill isn't stylable)
function paintRange(el) {
  const min = +el.min, max = +el.max, pct = ((+el.value - min) / (max - min)) * 100;
  el.style.background = `linear-gradient(90deg, var(--accent) 0 ${pct}%, var(--track) ${pct}% 100%)`;
}
timeEl.addEventListener("input", () => { state.time = fmtTime(+timeEl.value); $("timeVal").textContent = state.time; paintRange(timeEl); });
timeEl.addEventListener("change", fetchIsochrone);

function setBudget(v) {                 // instant — follows the knob exactly
  state.budget = Math.min(MAX_BUDGET, Math.max(5, v));
  budgetEl.value = state.budget;
  $("budgetVal").textContent = state.budget;
  paintRange(budgetEl);
  drawBudget(state.budget * 60);
}
budgetEl.addEventListener("input", () => setBudget(+budgetEl.value));
budgetEl.addEventListener("wheel", (e) => { e.preventDefault(); setBudget(state.budget + (e.deltaY < 0 ? 1 : -1)); }, { passive: false });
paintRange(timeEl); paintRange(budgetEl);

$("modes").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const m = btn.dataset.mode;
  state.modes.has(m) ? state.modes.delete(m) : state.modes.add(m);
  btn.classList.toggle("on");
  fetchIsochrone();
});

function makeOriginMarker() {
  const el = document.createElement("div");
  el.className = "origin-dot";
  el.innerHTML = '<span class="ring"></span><span class="core"></span>';
  return new maplibregl.Marker({ element: el });
}

map.on("click", (e) => {
  state.origin = [e.lngLat.lng, e.lngLat.lat];
  if (!originMarker) originMarker = makeOriginMarker();
  originMarker.setLngLat(state.origin).addTo(map);
  fetchIsochrone();
});

// --- layers ---
const emptyFC = () => ({ type: "FeatureCollection", features: [] });

// a small white chevron (dark halo) pointing +x, drawn to a canvas for the arrow
function makeArrowIcon() {
  const s = 28, c = document.createElement("canvas");
  c.width = c.height = s;
  const x = c.getContext("2d");
  x.translate(s / 2, s / 2); x.lineCap = "round"; x.lineJoin = "round";
  for (const [w, col] of [[7.5, "rgba(18,26,36,0.55)"], [3.6, "rgba(255,255,255,0.98)"]]) {
    x.beginPath(); x.moveTo(-8, -8); x.lineTo(7, 0); x.lineTo(-8, 8);
    x.strokeStyle = col; x.lineWidth = w; x.stroke();
  }
  return x.getImageData(0, 0, s, s);
}

// Reachable-area reveal lives on the MASK map (maskMap), not here. The grey hex
// grid is drawn there and desaturates the colour basemap below via CSS blend, so
// the unreachable area keeps full map detail in black & white while the reachable
// area (hexes filtered out) stays in full colour. See maskMap.on("load") below.
maskMap.on("load", () => {
  buildVeilGrid();
  // Geometry is uploaded to the GPU exactly ONCE here. The per-query reveal is
  // then driven by feature-state (a cheap per-cell value) instead of re-setData-
  // ing all ~80k hexes every 200 ms as the fog streams — that re-upload was the
  // main click-time stutter. tolerance:0 keeps the hexes from being simplified.
  maskMap.addSource("veil", { type: "geojson", tolerance: 0,
    data: { type: "FeatureCollection", features: veilFeatures } });
  maskMap.addLayer({
    id: "veil",
    type: "fill",
    source: "veil",
    // grey (opacity 1, desaturates the colour map below) where a cell's streamed
    // travel is unknown or over budget; transparent (0, reveals colour) where
    // reachable within the cutoff. Any saturation-0 grey works for the blend.
    paint: { "fill-color": "#808080", "fill-opacity": veilOpacity(state.budget * 60), "fill-antialias": false },
  });
  maskReady = true;
  drawBudget(state.budget * 60);
});

// The transit spine lives on the spineMap (above the desaturation mask) so its
// colours never get greyed — they cut cleanly through the B&W along the exact
// line geometry.
spineMap.on("load", () => {
  spineMap.addSource("journey", { type: "geojson", data: emptyFC() });
  spineMap.addLayer({
    id: "journey-bus-casing", type: "line", source: "journey", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffffff", "line-width": 3.6, "line-opacity": 0.55 },
  });
  spineMap.addLayer({
    id: "journey-bus", type: "line", source: "journey", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 1.8, "line-opacity": 0.95 },
  });
  spineMap.addLayer({
    id: "journey-metro-casing", type: "line", source: "journey", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffffff", "line-width": 7, "line-opacity": 0.9 },
  });
  spineMap.addLayer({
    id: "journey-metro", type: "line", source: "journey", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 4.5, "line-opacity": 1 },
  });

  // outward-flow arrows along the spine (coords go board -> alight = outward)
  spineMap.addImage("flow-arrow", makeArrowIcon(), { pixelRatio: 2 });
  spineMap.addLayer({
    id: "spine-arrows", type: "symbol", source: "journey",
    layout: {
      "symbol-placement": "line", "symbol-spacing": 64, "icon-image": "flow-arrow",
      "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.5, 13, 0.85, 16, 1.2],
      "icon-rotation-alignment": "map", "icon-allow-overlap": true, "icon-ignore-placement": true,
    },
    paint: { "icon-opacity": 0.92 },
  });
  spineReady = true;
  drawBudget(state.budget * 60);
});

map.on("load", async () => {
  try {
    const meta = await (await fetch("/api/meta")).json();
    setStatus("ready", { date: meta.service_date });
  } catch { setStatus("apiErr"); }
  ready = true;
  drawBudget(state.budget * 60);
  fetchLegend();
  loadQuests();
});

// --- legend: the actual rapid-transit lines in the network (from /api/lines),
// grouped Métro / REM / Train; buses are one consolidated colour. ---
// exo train names are proper nouns — same in both languages
const EXO_LABEL = { MA: "Mascouche", SH: "Mont-Saint-Hilaire", VH: "Vaudreuil–Hudson",
  SJ: "Saint-Jérôme", CA: "Candiac", DM: "Deux-Montagnes" };
let legendLines = null;                          // cached /api/lines payload (re-rendered on language change)
async function fetchLegend() {
  try { legendLines = (await (await fetch("/api/lines")).json()).lines || []; } catch { return; }
  renderLegend();
}
function renderLegend() {
  const legend = $("legend");
  if (!legend || !legendLines) return;           // localized placeholder stays until lines load
  const esc = (s) => String(s).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
  const bar = (color) => `<span class="bar" style="background:#${esc(color)}"></span>`;
  const key = (sw, label) => `<div class="key">${sw}<span>${esc(label)}</span></div>`;
  const group = (head, rows) => rows ? `<div class="group"><div class="ghead">${esc(head)}</div>${rows}</div>` : "";
  const metroLabel = t("metro");
  const metro = legendLines.filter((l) => l.type === 1);
  const rem = legendLines.filter((l) => l.type === 0);
  const train = legendLines.filter((l) => l.type === 2);

  let html = group(t("grpReach"), key(`<span class="zone"></span>`, t("reachKey")));
  html += group(t("grpMetro"), metro.map((l) => key(bar(l.color), metroLabel[l.name] || (t("linePrefix") + l.name))).join(""));
  if (rem.length) html += group(t("grpRem"), key(bar(rem[0].color), t("remLabel")));
  html += group(t("grpTrain"), train.map((l) => key(bar(l.color), EXO_LABEL[l.name] || l.name)).join(""));
  html += group(t("grpBus"), key(`<span class="bar bus" style="background:${BUS_COLOR}"></span>`, t("allBus")));
  legend.innerHTML = html;
}

// --- budget: spine filter (instant) + reveal cutoff ---
const IS_BUS = ["==", ["get", "cls"], "bus"];
const IS_SPINE = ["==", ["get", "cls"], "rail"];

// fill-opacity expression for the veil: grey (1) where the cell's streamed travel
// (feature-state) is unknown or beyond the cutoff; transparent (0) where reachable.
function veilOpacity(cut) {
  return ["case", [">", ["coalesce", ["feature-state", "travel"], SENTINEL], cut], 1, 0];
}

function drawBudget(spineCut, fogCut = spineCut) {
  if (spineReady && spineMap.getLayer("journey-metro")) {
    const within = ["<=", ["get", "travel"], spineCut];
    spineMap.setFilter("journey-bus-casing", ["all", IS_BUS, within]);
    spineMap.setFilter("journey-bus", ["all", IS_BUS, within]);
    spineMap.setFilter("journey-metro-casing", ["all", IS_SPINE, within]);
    spineMap.setFilter("journey-metro", ["all", IS_SPINE, within]);
    spineMap.setFilter("spine-arrows", ["all", IS_SPINE, within]);
  }
  // desaturate where travel > budget (unreachable); reachable cells filtered out
  // (left in colour) — instant filter on the mask map.
  fogCutoff = fogCut;
  if (maskReady && maskMap.getLayer("veil")) {
    // instant: re-evaluates the opacity expression over existing feature-state on
    // the GPU — no data re-upload (this is what makes the budget slider free).
    maskMap.setPaintProperty("veil", "fill-opacity", veilOpacity(fogCut));
  }
  if (typeof scheduleRenderQuests === "function") scheduleRenderQuests();   // re-rank quests for the new budget (debounced)
}

// reset every cell back to grey before a new query: drop all reveal state in a
// single call (geometry stays uploaded; cells re-reveal as the new fog streams).
function clearReach() {
  if (maskReady && maskMap.getSource("veil")) maskMap.removeFeatureState({ source: "veil" });
  fogTravel.clear();
}

// --- fetch (only on origin / departure / mode change) ---
let inflight = null;
async function fetchIsochrone() {
  if (!state.origin) return;
  const [lon, lat] = state.origin;
  const params = new URLSearchParams({ lat, lon, time: state.time, modes: [...state.modes].join(",") });
  setStatus("computing");
  if (inflight) inflight.abort();
  inflight = new AbortController();
  const sig = inflight.signal;
  clearReach();                // whole bbox greys out; reachable cells reveal as they stream
  // Fire fog IN PARALLEL with the spine (not after it). The single-flight cache
  // still runs RAPTOR once — whichever request lands first computes, the other
  // waits on its Event — but the fog's ~115 ms proxy round-trip now overlaps the
  // spine's compute instead of starting only after the spine response, so the
  // reveal fills in ~200 ms sooner. (Safe now that the cache coordinates them;
  // the old "parallel is slower" was pre-cache GIL contention from double compute.)
  streamFog(params, sig);
  try {
    const t0 = performance.now();
    const data = await (await fetch(`/api/isochrone?${params}`, { signal: sig })).json();
    renderSpine(data);
    setStatus("result", { n: data.count, m: data.max_budget_min, ms: Math.round(performance.now() - t0) });
    drawBudget(state.budget * 60);
  } catch (err) {
    if (err.name !== "AbortError") setStatus("queryErr");
  }
}

// compact segment: [travel, code, color]; code 0=bus 1=rail/spine
function renderSpine(data) {
  if (!spineReady || !spineMap.getSource("journey")) return;
  spineMap.getSource("journey").setData({
    type: "FeatureCollection",
    features: (data.segments || []).map((s) => {
      const code = s[1], raw = s[2];
      const color = code === 1 ? (raw ? "#" + raw : "#444") : BUS_COLOR;
      return {
        type: "Feature",
        geometry: { type: "LineString", coordinates: s[3] },
        properties: { cls: code === 1 ? "rail" : "bus", color, travel: s[0] },
      };
    }),
  });
}

// stream the fog hexagons (NDJSON [travel,q,r]); the reveal opens as they arrive
async function streamFog(params, sig) {
  try {
    const resp = await fetch(`/api/fog?${params}`, { signal: sig });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line || sig.aborted) continue;
        const c = JSON.parse(line);     // [travel, q, r]
        const idx = idxByKey.get(c[1] + "," + c[2]);
        // cheap per-cell reveal — no geometry re-upload; MapLibre coalesces the
        // re-render so each streamed chunk repaints at most once.
        if (idx !== undefined) maskMap.setFeatureState({ source: "veil", id: idx }, { travel: c[0] });
        fogTravel.set(c[1] + "," + c[2], c[0]);    // for side-quest travel lookup
      }
    }
    if (!sig.aborted) renderQuests();              // suggest quests once the fog is in
  } catch (e) { /* aborted or no fog */ }
}

// --- side quests -----------------------------------------------------------
// Curated POIs surfaced from the CURRENT origin, biased to the far edge of what's
// reachable. A place's travel time is a client-side lookup: map its lat/lon to a
// fog hex (same projection as the engine) and read the streamed travel. So this
// is static JSON + a hex lookup — no extra server work.
const QTYPE_ICON = { eatery: "🍴", park: "🌳", neighborhood: "🏘️", viewpoint: "🌅",
  market: "🛒", landmark: "🏛️", historic: "🗿", art: "🎨" };
let quests = [];
let fogTravel = new Map();          // "q,r" -> travel seconds (filled as fog streams)
let questSeed = 1;
let questMarkers = [];              // [{id, el, marker}] for the on-map pins
let _questTimer = null;
const _esc = (s) => String(s).replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

function questTravel(qst) {              // min travel over the quest's hex + neighbours (hexKey from hex.js)
  const base = hexKey(qst.lon, qst.lat);
  const [q, r] = base.split(",").map(Number);
  let best = fogTravel.get(base);
  for (const [dq, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1], [1, -1], [-1, 1]]) {
    const tv = fogTravel.get((q + dq) + "," + (r + dr));
    if (tv != null && (best == null || tv < best)) best = tv;
  }
  return best == null ? null : best;
}
function _qrng(seed, id) {                // deterministic jitter per (seed, quest) for stable shuffle
  let h = (2166136261 ^ seed) >>> 0;
  for (let i = 0; i < id.length; i++) h = Math.imul(h ^ id.charCodeAt(i), 16777619);
  return ((h >>> 0) % 1000) / 1000;
}
function fmtDur(min) {
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60), m = min % 60;
  return m ? `${h} h ${String(m).padStart(2, "0")}` : `${h} h`;
}
function rankQuests() {
  if (!quests.length || !fogTravel.size) return [];
  const budget = state.budget * 60;
  const scored = [];
  for (const qst of quests) {
    const tv = questTravel(qst);
    if (tv == null || tv > budget) continue;
    const far = tv / budget;                          // 0..1; bias toward the outer band
    const band = far < 0.5 ? far * 0.6 : far;
    const quality = (qst.image ? 0.5 : 0) + (qst.wikipedia ? 0.3 : 0);
    scored.push({ qst, tv, score: band * 1.6 + quality + _qrng(questSeed, qst.id) * 0.3 });
  }
  scored.sort((a, b) => b.score - a.score);
  const picks = [], seenN = new Set();                // spread across neighbourhoods
  for (const s of scored) {
    if (seenN.has(s.qst.neighborhood)) continue;
    seenN.add(s.qst.neighborhood); picks.push(s);
    if (picks.length >= 5) break;
  }
  for (const s of scored) { if (picks.length >= 5) break; if (!picks.includes(s)) picks.push(s); }
  return picks;
}
function questCard(qst, tv, n) {
  const travelMin = Math.round(tv / 60);
  const total = Math.round((2 * travelMin + qst.avg_dwell_min) / 15) * 15;
  const icon = QTYPE_ICON[qst.type] || "📍";
  const blurb = (lang === "fr" ? qst.blurb_fr : qst.blurb_en) || qst.blurb_en || qst.blurb_fr || "";
  const more = qst.website || qst.wikipedia;
  const maps = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(qst.name + " " + qst.lat + "," + qst.lon)}`;
  const img = qst.image
    ? `<div class="qimg" style="background-image:url('${_esc(qst.image)}')"></div>`
    : `<div class="qimg qph">${icon}</div>`;
  const links = (more ? `<a href="${_esc(more)}" target="_blank" rel="noopener">${t("questMore")}</a>` : "")
    + `<a href="${maps}" target="_blank" rel="noopener">${t("questMaps")}</a>`;
  return `<article class="quest" data-qid="${_esc(qst.id)}">${img}<div class="qbody">`
    + `<div class="qname"><span class="qnum">${n}</span>${icon} ${_esc(qst.name)}</div>`
    + `<div class="qmeta">${_esc(qst.neighborhood)} · ${t("questEachWay")(travelMin)} · ${t("questOuting")(fmtDur(total))}</div>`
    + `<div class="qblurb">${_esc(blurb)}</div><div class="qlinks">${links}</div></div></article>`;
}

// highlight a quest's pin AND its card together (hover correlation, both directions)
function setQuestHi(id, on) {
  const m = questMarkers.find((x) => x.id === id);
  if (m) m.el.classList.toggle("hi", on);
  const card = document.querySelector(`#quests .quest[data-qid="${CSS.escape(id)}"]`);
  if (card) card.classList.toggle("hi", on);
}
// Quest pins live in #pinlayer — a transparent overlay stacked ABOVE the spine and
// the desaturation hex mask — so a pin stays visible even when it falls in the grey
// (out-of-budget / unreachable) zone, and the spine never draws over it. We can't
// use a maplibregl.Marker for this: a marker on the main map sits below the overlays,
// and a marker on the top overlay map floats (that map's camera trails the main map
// by a frame). Instead we position the pins ourselves from the MAIN map's live
// projection on every "move" frame, so they're glued with zero sync lag.
const pinLayer = $("pinlayer");
function positionPins() {
  for (const m of questMarkers) {
    const p = map.project([m.lon, m.lat]);
    m.el.style.transform = `translate(${p.x}px, ${p.y}px) translate(-50%, -50%)`;
  }
}
map.on("move", positionPins);
function clearQuestPins() {
  for (const m of questMarkers) m.el.remove();
  questMarkers = [];
}
function addQuestPin(qst, n) {
  // OUTER anchor = the positioned element (its transform is rewritten each frame, so
  // it must stay transition-free or it would ease/lag). INNER dot = visuals + the
  // hover scale (scaling the anchor would fight its positioning transform).
  const el = document.createElement("div");
  el.className = "quest-pin-anchor";
  const dot = document.createElement("div");
  dot.className = "quest-pin";
  dot.textContent = n;
  el.appendChild(dot);
  el.title = qst.name;
  el.addEventListener("mouseenter", () => setQuestHi(qst.id, true));
  el.addEventListener("mouseleave", () => setQuestHi(qst.id, false));
  el.addEventListener("click", (e) => {          // click a pin -> reveal its card
    e.stopPropagation();
    const card = document.querySelector(`#quests .quest[data-qid="${CSS.escape(qst.id)}"]`);
    if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    setQuestHi(qst.id, true);
    setTimeout(() => setQuestHi(qst.id, false), 1100);
  });
  pinLayer.appendChild(el);
  questMarkers.push({ id: qst.id, el, lon: qst.lon, lat: qst.lat });
}
function renderQuests() {
  const el = $("quests"); if (!el) return;
  const body = el.querySelector(".qlist"); if (!body) return;
  clearQuestPins();
  if (!state.origin) { body.innerHTML = `<p class="qempty">${t("questsHint")}</p>`; return; }
  const picks = rankQuests();
  if (!picks.length) { body.innerHTML = `<p class="qempty">${t("questsNone")}</p>`; return; }
  body.innerHTML = picks.map((p, i) => questCard(p.qst, p.tv, i + 1)).join("");
  picks.forEach((p, i) => addQuestPin(p.qst, i + 1));
  positionPins();                              // place them at the current camera now (before any "move")
  body.querySelectorAll(".quest").forEach((card) => {
    const id = card.dataset.qid;
    card.addEventListener("mouseenter", () => setQuestHi(id, true));
    card.addEventListener("mouseleave", () => setQuestHi(id, false));
  });
}
// budget drags fire drawBudget rapidly; debounce the quest re-render (and its pin
// churn) so the slider stays smooth while suggestions still refresh promptly.
function scheduleRenderQuests() { clearTimeout(_questTimer); _questTimer = setTimeout(renderQuests, 150); }
async function loadQuests() {
  try { quests = await (await fetch("side_quests.json")).json(); } catch { quests = []; }
  renderQuests();
}
$("questShuffle").addEventListener("click", () => { questSeed++; renderQuests(); });

// apply the saved/default language now that every translatable element + the
// dynamic renderers (status, legend) are defined.
applyLang();
