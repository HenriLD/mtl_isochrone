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

# Montreal Multi-Modal Isochrone Map

Pick a starting point on a map of Montreal, set a departure time and a time
budget, and see everywhere reachable by transit within that budget — computed
by a custom range-RAPTOR engine over real STM schedules.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the engineering/performance
orientation, and [`montreal-isochrone-plan.md`](montreal-isochrone-plan.md) for
the full multi-phase plan.

The map reads as an **expanding zone**: everywhere reachable shows the basemap in
full colour, everywhere else is the same map in **black & white**, with a
**transit spine** (metro/REM/exo in official line colours, bus feeders in one
rose) cutting through. The full ARTM regional network is loaded (STM + REM + exo).

## Cost: $0

Everything here is free and runs on your machine:

| Piece | Source | Cost |
|---|---|---|
| Transit schedules | STM GTFS (open data) | free |
| Basemap tiles | OpenFreeMap (no API key, no quota) | free |
| Routing engine | custom Python, pure stdlib core | free |
| Frontend | MapLibre GL (open source) | free |

It's built to stay cheap when deployed: the engine has **no third-party
dependencies** (only the dev API server uses FastAPI), so it can later move to
a free backend tier — or compile to WASM (Pyodide) for a pure static site —
without changes.

## Quick start

```bash
pip install -r requirements.txt

# 1. Transit timetable
python scripts/download_gtfs.py     # ~60 MB STM feed -> data/raw/
python scripts/build_network.py     # compile -> data/processed/network.pkl (~15s)

# 2. Walking network (Phase 2) — real street access + transfers
python scripts/download_osm.py      # walkable OSM via Overpass -> data/raw/osm_walk.json
python scripts/build_walk_graph.py  # graph + transfers -> data/processed/walk_graph.pkl
                                    # (also overwrites network transfers; run AFTER build_network)

python -m uvicorn server.app:app --port 8077
# open http://127.0.0.1:8077  ->  click the map
```

The server runs without step 2 — it just falls back to straight-line access and
geometric transfers until the walk graph exists.

## How it works

```
GTFS zip ──ingest──► Network (RAPTOR layout) ──pickle──► engine ──► /api/isochrone ──► MapLibre
  scripts/download    engine/gtfs.py            data/processed   engine/raptor.py  server/app.py   web/
```

1. **Ingestion** (`engine/gtfs.py`): parses the feed with the standard library,
   auto-selects the busiest weekday service date, and groups trips into
   *patterns* (RAPTOR "routes" — sets of trips sharing one ordered stop
   sequence). Builds geometric footpath transfers via a grid spatial index.
2. **Engine** (`engine/raptor.py`): exact-schedule range-RAPTOR. From the
   clicked origin it adds walk *access legs* to nearby stops, then runs
   round-based scanning (round *k* = reachable with ≤ *k* boardings), relaxing
   footpath transfers each round, until the time budget is exhausted. ~20–40 ms
   per query over the full STM network.
3. **API** (`server/app.py`): loads the network once, exposes
   `/api/isochrone?lat&lon&time&budget&modes`, serves the frontend.
4. **Frontend** (`web/`): click to set origin; mode toggles, departure-time and
   budget sliders. The reveal uses **three stacked MapLibre maps** (see
   [`ARCHITECTURE.md`](ARCHITECTURE.md#rendering-architecture-frontend-perf-is-here)):
   a colour Liberty basemap; a tiles-less overlay that draws a grey hex grid with
   CSS `mix-blend-mode: saturation` to desaturate the **unreachable** area to
   black & white while keeping its detail; and a top overlay for the colour
   transit spine. 2D-only; the view is locked to the data extent.

   - **Fog (reachable area)**: a multi-source Dijkstra egresses from every
     reached stop across the OSM walk graph into tessellating **hexagons**
     (`engine/walk.py:egress_hex_graph`), tagged by travel time. The `/api/fog`
     endpoint **streams** the reachable hexes as NDJSON; the client filters the
     hex grid by `travel` so reachable cells stay colour and the rest desaturate.
     Fired right after the spine request (sequential — see the GIL note in
     `ARCHITECTURE.md`), reusing the spine's cached RAPTOR run.

### Instant budget slider (compute once, filter locally)

Earliest-arrival times are **budget-independent**: a bigger budget only reveals
more of the same tree. So the server computes a single isochrone at the max
budget (`MAX_BUDGET_MIN`, default 90) and tags every stop and segment with its
`travel` time (seconds from departure). The budget slider is then a pure
client-side MapLibre layer filter (`travel <= budget`) — it updates in well
under a millisecond, follows the knob exactly (drag *or* scroll), and never hits
the server. A new request happens only when the origin, departure time, or modes
change (~0.5 s at max budget).

### "How you get there" — the journey tree

The engine doesn't just say *where* you can go, it records *how*: every reached
stop keeps a back-pointer to the leg that reached it (which route/trip + board
stop, or which footpath transfer). `_reconstruct_segments` turns that tree into
drawable edges, deduping the shared trunk so the green line downtown is drawn
once as a thick spine with bus/walk legs branching off. Transit legs carry the
**official GTFS route color** and type; the frontend styles them (metro/REM
thick in line colors, bus thinner, walk dashed). The engine stays
presentation-agnostic — it only emits geometry + route metadata.

This is designed in from the start so Phase 2/3 walk and bike legs extend the
same rendering for free.

## Performance

Profile-driven. The fog egress *used* to be ~95% of the cost and was optimized
away (hex-graph egress, **2924 ms → ~70 ms**); the hot path is now
`compute_isochrone` — the origin access-walk Dijkstra over the 1.5 M-node OSM
graph and the RAPTOR round scan. Full numbers, profile, the already-applied
optimizations (compute-once/filter-locally, single-flight cache, sequential-not-
parallel/GIL, compact wire formats), and ranked candidate targets are in
**[`ARCHITECTURE.md` → Performance](ARCHITECTURE.md#performance)**.

The budget slider stays a sub-millisecond client-side filter regardless.

> Editing `web/app.js`? Bump the `?v=` query on its `<script>` tag in
> `index.html`, or the browser may serve a stale cached copy.

## Correctness

`engine/reference_csa.py` is an independent **Connection Scan Algorithm** — a
deliberately different method (time-ordered connection sweep vs. RAPTOR's
round-based route scan). `scripts/validate.py` runs many random
(origin, departure) queries through both engines and compares earliest-arrival
labels at every stop; they agree **exactly** (0 mismatches over 300k+ labels at
a 90-min budget). This cross-check caught two real RAPTOR bugs during
development — chained walk→walk transfers, and order-dependent footpath seeding
that missed valid transit→walk legs — both fixed. Run it with:

```bash
python scripts/validate.py 40 90   # 40 samples, 90-min budget
```

## Design decisions (this build)

- **Exact-schedule**, single departure time — the honest "where can I get
  leaving at 08:00" answer, using real `stop_times`. The trip-scan is
  structured so a frequency-based mode can slot in later.
- **Run local now, architect for cheap hosting later** — clean split between
  offline ingestion, a portable engine, a thin API, and a static frontend.
- **Phase-1 placeholder legs**: access/transfer use straight-line distance at a
  walking speed. Phase 2 replaces these with a real OSM walk graph.

## Coverage

The full **ARTM regional network**: STM (bus + metro), **REM** (light metro),
and **exo** (commuter trains + 11 suburban bus sectors) — all 14 feeds in
`config.GTFS_FEEDS`. Note REM is GTFS `route_type 0` and exo buses are `1501`;
the engine maps types `{0,1,2}` to the rapid-transit "spine" and everything else
to buses (`ROUTE_TYPE_MODE` / `SPINE_TYPES`).

The OSM walk graph covers the island **plus the near north shore (Laval) and
south shore (Longueuil / Brossard / Boucherville)** — `OSM_BBOX` in `config.py`.
Those areas get real street-network hex fog. Far exurbs (beyond the bbox) fall
back to straight-line access/transfers and a cheap egress-disc fog
(`egress_hex_disc`). Widening coverage is just `OSM_BBOX` → re-download →
`build_walk_graph` (the fog egress stays cheap regardless of graph size).

Walk/transfer legs are not drawn (they cluttered the map); the spine shows
transit legs only.

## Limitations / next (per the plan)
- **Fog is a ~140 m egress grid, not a crisp contour** — a true street-network
  egress, but a hex grid rather than a sharp isochrone boundary. Crisp
  time-banded polygons are Phase 4.
- **Transit geometry**: buses trace simplified GTFS `shapes.txt`; metro traces
  **OSM tunnel geometry** (GTFS metro shapes were ~1 pt/station — replaced, see
  `ARCHITECTURE.md`). Walk legs aren't drawn (they cluttered the map).

## Tuning

Knobs live in `config.py`: walk speed, access/transfer radii, and `SERVICE_DATE`
(leave `None` to auto-pick the busiest weekday in the feed).
