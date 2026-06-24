---
title: Montreal Transit Isochrone
emoji: 🚇
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

<!-- The YAML block above is Hugging Face Space metadata (it must be the very
     first bytes of this file). app_port must match the Dockerfile EXPOSE/port. -->

# Montréal Multi-Modal Isochrone Map

[![CI](https://github.com/HenriLD/mtl_isochrone/actions/workflows/ci.yml/badge.svg)](https://github.com/HenriLD/mtl_isochrone/actions/workflows/ci.yml)
&nbsp;[![Live demo](https://img.shields.io/badge/live%20demo-🤗%20Spaces-blue)](https://huggingface.co/spaces/HenriLD/mtl_isochrone)

Click anywhere on a map of Montréal, set a departure time and a time budget, and
see **everywhere you can reach by public transit** within that budget — computed
in real time by a custom range-RAPTOR engine over the real regional timetable.

**▶︎ Live demo: <https://huggingface.co/spaces/HenriLD/mtl_isochrone>**

![Reachable area around downtown Montréal revealed in full colour, with the
metro / REM / exo transit spine cutting through the greyscale unreachable zone](docs/images/hero.jpg)

The map reads as an **expanding zone**: everywhere reachable shows the basemap in
full colour, everywhere else is the same map in **black & white**, with a
**transit spine** (metro / REM / exo in their official line colours, bus feeders
in one rose) threading through it. The whole UI is **bilingual** — French by
default, with an `FR` / `EN` toggle.

<table>
<tr>
<td width="50%"><img src="docs/images/budget.jpg" alt="A 20-minute budget shrinks the reachable area to downtown"></td>
<td width="50%"><img src="docs/images/english.jpg" alt="The same map in English at a 60-minute budget"></td>
</tr>
<tr>
<td align="center"><em>A 20-min budget — the reachable zone shrinks to the core.</em></td>
<td align="center"><em>English UI, 60-min budget. The slider filters client-side, instantly.</em></td>
</tr>
</table>

## Features

- **Transit isochrones** over the full ARTM regional network — STM (bus + metro),
  REM (light metro), and exo (commuter trains + suburban buses).
- **Instant budget slider** — drag or scroll from 5 to 90 minutes; the reachable
  area updates in well under a millisecond (it never re-queries the server).
- **Near→far reveal** — reachable streets light up in colour as a hex "fog"
  streams outward from your start point; the rest stays greyscale but keeps full
  map detail.
- **Real journeys** — the coloured spine shows *how* you'd actually travel
  (which metro/REM/exo lines and bus feeders), in official line colours.
- **Side quests** — a curated set of places worth visiting (parks, eateries,
  neighbourhoods…) surfaced near the *edge* of what you can reach, to nudge you
  toward exploring somewhere new. Each suggestion shows travel time, a rough
  outing length, and a link out.
- **Bilingual** (FR / EN) and **2D-only**, locked to the data extent.
- **Free to run and free to host** — see below.

## Cost: $0

Every piece is free and open:

| Piece | Source |
|---|---|
| Transit schedules | STM / exo / REM **GTFS** (open data) |
| Walking network | **OpenStreetMap** (via Overpass) |
| Basemap tiles | **OpenFreeMap** (no API key, no quota) |
| Routing engine | custom **Python, pure standard library** |
| Frontend | **MapLibre GL** (open source) |

The engine core has **no third-party dependencies**, and the API server uses only
**Starlette + uvicorn** (both pure-Python). That keeps the whole stack installable
on **PyPy**, so the live demo runs on a **free Hugging Face Space** — and the same
stdlib core could compile to WebAssembly (Pyodide) for a pure static site later.

## How it works

```
GTFS + OSM ──build──► compiled network ──► RAPTOR engine ──► /api/isochrone ─┐
 (offline scripts)     data/processed       engine/raptor.py   server/app.py │──► MapLibre (web/)
                                                               /api/fog ──────┘
```

1. **Ingestion** (`engine/gtfs.py`, run offline): parses every feed with the
   standard library, auto-picks the busiest weekday, and groups trips into
   *patterns* — the RAPTOR trick: a "route" is a set of trips sharing one ordered
   stop sequence, so finding the first catchable trip is a binary search and one
   sweep relaxes every downstream stop.

2. **Walking network** (`engine/walk.py`): an OSM pedestrian graph (~1.5 M nodes)
   gives real street access from the clicked point to nearby stops, and real
   footpath transfers between stops.

3. **Engine** (`engine/raptor.py`): exact-schedule **range-RAPTOR**. From the
   origin it walks to nearby stops, then scans in rounds (round *k* = reachable
   with ≤ *k* boardings), relaxing footpath transfers each round, until the time
   budget runs out. Every reached stop keeps a back-pointer to *how* it was
   reached, which is reconstructed into the drawable transit spine.

4. **Frontend** (`web/`): three stacked MapLibre maps — a colour basemap; a
   tiles-less overlay drawing a grey hex grid with CSS `mix-blend-mode: saturation`
   (this desaturates the **unreachable** area to black & white while keeping its
   street detail); and a top overlay for the colour spine.

**The budget slider is free.** Earliest-arrival times are budget-independent — a
bigger budget just reveals more of the same tree — so the server computes one
isochrone at the maximum budget and tags every stop, segment, and fog hex with
its travel time. The slider is then a pure client-side layer filter
(`travel ≤ budget`): sub-millisecond, exact, and it never touches the server. A
new request happens only when the origin, time, or modes change.

**The fog** is a multi-source flood from every reached stop across a precomputed
hex-adjacency graph; `/api/fog` **streams** reachable hexes as NDJSON so the
reveal opens from near to far. It rides the same cached RAPTOR run as the spine.

## Side quests

A small curated catalogue of ~150 places across the region — parks, eateries,
neighbourhoods, viewpoints, markets, landmarks — each with a short blurb, a
typical "dwell" time, and a link (official site / Wikipedia / Google Maps). On
each query the app maps every place to its fog hex to get its travel time, then
suggests a few that are **reachable but out toward the edge** of your budget, in
different neighbourhoods — so the side quest is somewhere you might not otherwise
go. The estimated outing length is `≈ 2 × travel + dwell`. It's all client-side:
a static list plus a hex lookup, no extra server cost.

## Performance & correctness

A fresh query computes in roughly **80–160 ms** over the full network; the budget
slider stays a sub-millisecond client-side filter regardless of budget. Repeated
clicks and the paired spine/fog requests share a single cached engine run.

Correctness is cross-checked against an **independent engine**: `engine/reference_csa.py`
implements the Connection Scan Algorithm — a deliberately different method (a
time-ordered sweep of elementary connections vs. RAPTOR's round-based route scan).
`scripts/validate.py` runs many random (origin, departure) queries through both
and compares earliest-arrival at every stop; they agree **exactly** (0 mismatches
over 300k+ labels). This caught two real engine bugs during development.

```bash
python scripts/validate.py 40 90   # 40 random queries at a 90-min budget
```

## Quick start

```bash
pip install -r requirements.txt

# 1) Transit timetable  →  data/processed/network.pkl
python scripts/download_gtfs.py
python scripts/build_network.py

# 2) Walking network  →  data/processed/walk_graph.pkl   (real street access + transfers)
python scripts/download_osm.py
python scripts/build_walk_graph.py     # run AFTER build_network (it also rewrites transfers)

# 3) Run
python -m uvicorn server.app:app --port 8077
# open http://127.0.0.1:8077  →  click the map
```

Step 2 is optional — without a walk graph the engine falls back to straight-line
access and geometric transfers. The side-quest catalogue (`web/side_quests.json`
and its thumbnails) is committed, so the panel works out of the box.

## Coverage

The full **ARTM regional network** (14 GTFS feeds in `config.py`): STM bus +
metro, REM light metro, and exo commuter trains plus its suburban bus sectors.
The OSM walk graph covers the island **plus the near north shore (Laval) and
south shore (Longueuil / Brossard / Boucherville)**; those areas get real
street-network fog, while far exurbs fall back to a cheap straight-line egress.
Widening coverage is just a bounding-box change in `config.py` and a re-build.

Transit geometry: buses follow GTFS `shapes.txt`, lightly corner-rounded so they
read smoothly; the metro traces real **OpenStreetMap tunnel geometry** (the GTFS
metro shapes were too coarse). Walk and transfer legs aren't drawn — only the
transit spine — to keep the map clean.

## Development

```bash
python -m unittest discover -s tests        # engine (RAPTOR == CSA) + geometry + assets
node tests/hexkey_contract.js               # JS hex projection matches the engine
```

GitHub Actions runs these on every push — engine correctness, a PyPy install
check (the runtime the demo deploys on), and the frontend/contract checks — and
**auto-deploys** the demo to the Hugging Face Space on a green push to `main`.
Knobs (walk speed, access/transfer radii, budgets, service date, coverage box)
live in `config.py`.

> Editing `web/app.js`? Bump the `?v=` query on its `<script>` tag in
> `web/index.html`, or the browser may serve a stale cached copy.

## Credits & data

- Transit schedules: **Société de transport de Montréal (STM)**, **exo**, and
  **REM** open GTFS feeds.
- Map data: **© OpenStreetMap contributors** (ODbL); basemap tiles by
  **OpenFreeMap**; rendering by **MapLibre GL**.
- Side-quest descriptions and photos: **OpenStreetMap** and **Wikidata /
  Wikimedia Commons** — each photo's author and licence are stored alongside it
  in `web/side_quests.json`.

Built for Montréal. 🚇
