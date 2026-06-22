# Architecture & Performance Orientation

Orientation for working on the engine — especially the **performance pass**. For
the product overview and setup, see [`README.md`](README.md). For the original
multi-phase plan, see [`montreal-isochrone-plan.md`](montreal-isochrone-plan.md).

The headline for a perf engineer: **the fog used to be 95% of the cost and was
optimized away; the hot path is now `compute_isochrone` — specifically the
origin access-walk Dijkstra over the 1.5 M-node OSM graph, and the RAPTOR round
scan.** Numbers and the profile are in [Performance](#performance) below.

---

## Data flow

```
            OFFLINE (scripts/, run once)                         ONLINE (per request)
 ┌───────────────────────────────────────────┐      ┌──────────────────────────────────────┐
 GTFS zips ─► engine/gtfs.py ─► network.pkl ──┼──┐   │  click ─► /api/isochrone ─► spine     │
   (14 feeds, download_gtfs)   (build_network) │  └─► │           /api/fog       ─► hex fog   │
 OSM (Overpass) ─► engine/walk.py ─► walk_graph.pkl   │  server/app.py ─► engine ─► MapLibre │
   (download_osm)  (build_walk_graph)          │      │                                web/   │
 OSM subway ─► fetch_metro_geometry ─► metro_geometry.json (applied into network.pkl at build)│
 └───────────────────────────────────────────┘      └──────────────────────────────────────┘
```

The engine core (`engine/`) has **no third-party dependencies** (pure stdlib) so
it can move to a free backend tier or compile to WASM later. Only the dev server
(`server/app.py`) uses FastAPI.

## Repo layout

| Path | What |
|---|---|
| `config.py` | All knobs: feeds, `OSM_BBOX`, budgets, walk speed, file paths. |
| `engine/gtfs.py` | GTFS ingestion → `Network` (stops, patterns, shapes, transfers). |
| `engine/model.py` | `Network`/`Route` dataclasses; `project_stops_to_shape` (stop→shape mapping). |
| `engine/raptor.py` | **The engine.** Range-RAPTOR isochrone + access walk + segment reconstruction. |
| `engine/walk.py` | OSM walk graph, bounded Dijkstra, **hex-graph egress** (the fog). |
| `engine/reference_csa.py` | Independent Connection-Scan engine — correctness oracle. |
| `server/app.py` | FastAPI: `/api/isochrone`, `/api/fog`, `/api/lines`, `/api/meta`; single-flight cache. |
| `web/app.js` | MapLibre frontend: 3 stacked maps, budget filter, fog stream, legend. |
| `web/index.html` | Panel/legend UI + CSS (incl. the desaturation blend). |
| `scripts/` | `download_*`, `build_*`, `fetch_metro_geometry`, `apply_metro_geometry`, `validate`. |

## Request lifecycle

A click fires **two** requests for the same origin; both go through `get_iso`
(single-flight cache), so RAPTOR runs **once**:

1. `GET /api/isochrone` → `compute_isochrone` → `result.segments` (the transit
   **spine**, compact `[travel, code, color, coords]`). Rendered immediately.
2. `GET /api/fog` → reuses the cached `result` → `egress_hex_graph` +
   `egress_hex_disc` → NDJSON stream of reachable hexes `[travel, q, r]`.

The client fetches spine **then** fog (sequential — see [GIL note](#what-was-already-optimized)).

**The budget slider never hits the server.** Earliest-arrival times are
budget-independent, so the server computes once at `MAX_BUDGET_MIN` (90) and tags
every stop/segment/hex with its `travel` time; the slider is a pure MapLibre
layer filter (`travel <= budget`), **< 0.3 ms**. A new request happens only on
origin / departure-time / mode change.

---

## Performance

### Current cost (downtown origin, 90-min max budget, warm)

| Stage | Time | Notes |
|---|---:|---|
| `compute_isochrone` total | **~365 ms** | dominates a fresh query |
| └ access-walk Dijkstra (`_access_walk`→`walk.py:dijkstra`) | ~165 ms | bounded explore over **1.5 M** OSM nodes |
| └ RAPTOR round scan (`compute_isochrone` body + `_earliest_trip`) | ~135 ms | 8 rounds × per-route trip search |
| └ `_reconstruct_segments` (geometry slicing) | ~60 ms | includes per-hop detour check |
| `egress_hex_graph` (fog, separate req, cached RAPTOR) | ~44 ms | Dijkstra over 25 k **hexes**, not 1.5 M nodes |
| `egress_hex_disc` (off-graph suburban stops) | ~27 ms | straight-line disc fog |
| budget slider (client) | < 0.3 ms | MapLibre `setFilter` |

`profile`: `tottime` leaders are `compute_isochrone`, `walk.py:dijkstra`,
`_heapq.heappop`, `raptor.py:_earliest_trip`. Reproduce:

```bash
python -c "import pickle,cProfile,pstats; from config import *; \
import engine.raptor as R; net=pickle.loads(NETWORK_FILE.read_bytes()); \
W=pickle.loads(WALK_GRAPH_FILE.read_bytes()); \
R.compute_isochrone(net,45.5017,-73.5673,28800,5400,walk_graph=W); \
cProfile.run('[R.compute_isochrone(net,45.5017,-73.5673,28800,5400,walk_graph=W) for _ in range(5)]','p'); \
pstats.Stats('p').sort_stats('tottime').print_stats(15)"
```

### What was already optimized

- **Hex-graph egress** — the fog Dijkstra runs over a precomputed ~25 k-hex
  adjacency graph (`build_hex_graph`), not the 1.5 M raw walk nodes: **2924 ms →
  ~70 ms**. This is why the fog is no longer the bottleneck.
- **Compute-once / filter-locally** — single max-budget result + client-side
  `travel` filter makes the budget slider free.
- **Single-flight RAPTOR cache** (`get_iso`, `threading.Event`) — spine + fog
  share one RAPTOR run; identical repeat clicks are instant.
- **Sequential, not parallel (GIL)** — racing spine+fog as parallel requests was
  *slower* (~900 ms; pure-Python CPU work contends on the GIL) than sequential
  (~230 ms; fog hits the shared cache). Don't "just add threads."
- **Compact wire formats** — fog `[travel,q,r]` (client rebuilds the hex);
  segments `[travel,code,color,coords]`, 5-decimal, Douglas-Peucker-simplified.

### Candidate targets for the perf pass (rough priority)

1. **Access-walk Dijkstra (~165 ms, biggest single item).** It explores a
   45-min walk radius (`MAX_ACCESS_WALK_MIN`) over 1.5 M nodes from a cold origin
   every query. Ideas: run access over the **hex graph** like the fog does
   (huge node reduction); A*/goal-bounding toward stops; multi-source from
   snapped origin segment; cache per-origin; or precompute stop→stop-area access.
   Beware: access correctness feeds RAPTOR — validate after.
2. **RAPTOR round scan (~135 ms).** `_earliest_trip` (per-route binary/linear
   search for the first boardable trip) is called ~200 k×. Ideas: tighten the
   per-round route queue (only scan routes touched by improved stops — partially
   done), precompute per-route departure arrays for binary search, prune routes
   outside the budget envelope, `array`/struct-of-arrays layout for cache locality.
3. **`_reconstruct_segments` (~60 ms).** Now does a per-hop detour-length check
   (`hop_dist`). Could be skipped when shapes are known-good, or vectorized.
   Only needed for the spine (not for the fog), and only at the final budget.
4. **Pyodide/WASM or PyPy** — the core is stdlib-only; CPython interpreter
   overhead (heap ops, dict gets) is a large share. A different runtime could be
   the cheapest global win.
5. **Caching at the API layer** — `get_iso` already memoizes the last 8 results;
   a spatial cache keyed by snapped origin could serve near-repeats.

> Correctness gate: any engine change must keep `python scripts/validate.py`
> at **0 mismatches** (it cross-checks RAPTOR vs the independent CSA engine).
> The geometry work (below) only affects rendering, not travel times.

---

## Rendering architecture (frontend perf is here)

Three **stacked MapLibre maps**, camera-synced every frame (`web/app.js`):

1. `#map` — colour Liberty basemap (the only interactive one; owns pan/zoom and
   `maxBounds`/`minZoom` locking the view to the data extent).
2. `#maskmap` — empty-style (no tiles) map drawing **only the grey hex grid**,
   with the container set to CSS `mix-blend-mode: saturation`. This desaturates
   the colour map *behind it* wherever a hex is painted → the unreachable area is
   real map detail in **black & white**; reachable cells (filtered out) stay
   colour. The whole reveal is a `setFilter` on `travel`.
3. `#spinemap` — empty-style map drawing **only the transit spine**, above the
   mask so the coloured lines are never desaturated.

The hex grid is ~80 k cells over `OSM_BBOX`; geometry is built once, only the
`travel` property changes per query. Budget changes are pure `setFilter` (instant).

## Transit line geometry

- **Buses**: GTFS `shapes.txt`, Douglas-Peucker simplified to 2 m
  (`gtfs.py:_simplify`). Stops are mapped onto the shape by
  `project_stops_to_shape` — **nearest point on segment** (not nearest vertex),
  strictly increasing, so sparse express segments don't mis-snap. A per-hop
  detour guard in `_reconstruct_segments` falls back to a straight leg if the
  sliced shape is > 1.6× the straight distance (kills loops from broken shapes).
- **Metro**: STM's GTFS metro shapes are garbage-coarse (~1 vertex/station →
  straight hops). Replaced with **OSM tunnel geometry**: `fetch_metro_geometry.py`
  (Overpass, cached) stitches each line's ways into the longest terminus-to-
  terminus path; `apply_metro_geometry.py` trims it to the terminus stations
  (drops non-revenue garage/tail track) and re-projects the stops. Wired into
  `build_network.py`. See `MEMORY`/git history for the garage-spur saga.

## Correctness

`engine/reference_csa.py` (Connection Scan) is an independent oracle;
`scripts/validate.py N BUDGET` runs random (origin, departure) queries through
both engines and compares earliest-arrival at every stop — **0 mismatches over
300 k+ labels**. This caught two real RAPTOR bugs during development.

```bash
python scripts/validate.py 40 90   # 40 samples, 90-min budget
```

## Build / run (recap)

```bash
pip install -r requirements.txt
python scripts/download_gtfs.py && python scripts/build_network.py
python scripts/download_osm.py   && python scripts/build_walk_graph.py
python -m uvicorn server.app:app --port 8077   # http://127.0.0.1:8077
```

`network.pkl` (~14 MB) and `walk_graph.pkl` (~73 MB) are git-ignored — regenerate
with the scripts above. The metro OSM geometry is cached in
`data/raw/metro_geometry.json` (committed) so the build applies it without
re-hitting Overpass.
