# Architecture & Performance Orientation

Orientation for working on the engine — especially the **performance pass**. For
the product overview and setup, see [`README.md`](README.md). For the original
multi-phase plan, see [`montreal-isochrone-plan.md`](montreal-isochrone-plan.md).

The headline for a perf engineer: the fog was optimized away (hex-graph egress);
then a **second perf pass** halved the remaining hot path —
`compute_isochrone` went **~324 ms → ~160 ms, bit-identical** (CSR + Dial's
Dijkstra, bisect departure columns, memoised hop geometry, pooled scratch). The
engine is **deployed on a free Hugging Face Space running PyPy**. Numbers, the
profile, and what each change did are in [Performance](#performance); the hosting
shape is in [Deployment](#deployment). What's left is structural, not code: a
~115 ms fixed HF-proxy tax per request, and the 45-min access-walk exploration.

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
it runs on PyPy and could compile to WASM later. The server (`server/app.py`)
uses only **Starlette + uvicorn** — both pure-Python, no compiled wheels — so the
whole stack installs on `pypy:3.10-slim` (FastAPI was dropped because its
pydantic-core is a Rust extension with unreliable PyPy wheels). See
[Deployment](#deployment).

## Repo layout

| Path | What |
|---|---|
| `config.py` | All knobs: feeds, `OSM_BBOX`, budgets, walk speed. `_resolve_processed()` finds the pickles: `MTL_DATA_DIR` → `/data` bucket → in-repo. |
| `engine/gtfs.py` | GTFS ingestion → `Network` (stops, patterns, shapes, transfers). |
| `engine/model.py` | `Network`/`Route` dataclasses; `Route.dep_cols` (bisect columns); `project_stops_to_shape`. |
| `engine/raptor.py` | **The engine.** Range-RAPTOR + access walk + segment reconstruction; `prepare_network`, pooled scratch. |
| `engine/walk.py` | OSM walk graph: **CSR adjacency + Dial's Dijkstra**, hex-graph egress (the fog). |
| `engine/reference_csa.py` | Independent Connection-Scan engine — correctness oracle. |
| `server/app.py` | Starlette: `/api/isochrone`, `/api/fog`, `/api/lines`, `/api/meta`; single-flight cache, gzip, warm-up, `/data` fetch. |
| `web/app.js` | MapLibre frontend: 3 stacked maps, feature-state reveal, bilingual i18n, budget filter, fog stream. |
| `web/index.html` | Panel/legend UI + CSS (desaturation blend, FR/EN toggle, `.panning` fast-path). |
| `Dockerfile` | `pypy:3.10-slim` image for the HF Space (port 7860); `.dockerignore` keeps data out of the image. |
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

`compute_isochrone` ≈ **324 ms → ~157 ms** (CPython, this machine) after the
pass; ~100 ms on the Space's warm PyPy. Per stage, before → after:

| Stage | Before | After | What changed |
|---|---:|---:|---|
| access-walk Dijkstra | ~165 ms | **~75 ms** | CSR adjacency + Dial's bucket queue + pooled scratch |
| RAPTOR round scan | ~135 ms | **~70 ms** | bisect over transposed `dep_cols`, hoisted lookups |
| `_reconstruct_segments` | ~60 ms | **~12 ms** | per-hop geometry memoised in `net.hop_geom` |
| `egress_hex_graph` / `_disc` (fog) | ~44 / ~27 ms | unchanged | already hex-graph; separate cached request |
| budget slider (client) | < 0.3 ms | unchanged | MapLibre filter / paint expr |

Every change is **exact** — `scripts/validate.py` stays at **0 mismatches** and
the segment output is byte-identical to pre-pass (verified by SHA-256 over
sampled origins). Reproduce the timing/profile:

```bash
python -c "import pickle,time; from config import *; import engine.raptor as R; \
net=pickle.loads(NETWORK_FILE.read_bytes()); W=pickle.loads(WALK_GRAPH_FILE.read_bytes()); \
R.compute_isochrone(net,45.5017,-73.5673,28800,5400,walk_graph=W); \
t=time.perf_counter(); [R.compute_isochrone(net,45.5017,-73.5673,28800,5400,walk_graph=W) for _ in range(5)]; \
print('avg ms', (time.perf_counter()-t)/5*1000)"
```

### The perf pass (each change, why it's exact)

1. **CSR walk graph** (`engine/walk.py`). `adj` (1.5 M lists of `(nbr,sec)`
   tuples) is flattened into three packed `array('i')` buffers
   (`csr_off/tgt/wt`, built by `build_csr`, idempotent). Dijkstra iterates an
   index range — no tuple unpack. Frees the tuple `adj` at runtime: **396 MB →
   37 MB** adjacency. Baked into the pickle by `build_walk_graph.py`.
2. **Dial's algorithm** replaces the binary heap in `dijkstra`. Walk-seconds are
   bounded positive integers, so a bucket queue gives O(1) insert/extract and
   drops ~571 k `heappush`/`heappop` per access query — same shortest paths.
3. **Pooled Dijkstra scratch** (`raptor._scratch_for`, thread-local). A fresh
   `_Scratch` allocated two ~1.5 M-element lists per query (~24 MB of garbage →
   PyPy GC pauses / latency spikes); now reused (reset via its dirty-list).
4. **Bisect departure columns** (`Route.dep_cols`, built in `prepare_network`).
   `_earliest_trip` is a C-level `bisect_left` over a transposed per-position
   departure array instead of a hand-rolled binary search over `dep[mid][p]`.
5. **Memoised hop geometry** (`net.hop_geom`). The shape-slice + detour check per
   drawn spine hop is static, so it runs once per unique hop, not every query.

### What was already optimized (still true)

- **Hex-graph egress** — fog Dijkstra over a ~25 k-hex graph, not 1.5 M nodes:
  **2924 ms → ~70 ms**. Why the fog isn't the bottleneck.
- **Compute-once / filter-locally** — one max-budget result + client `travel`
  filter makes the budget slider free.
- **Single-flight RAPTOR cache** (`get_iso`, `threading.Event`) — spine + fog
  share one RAPTOR run; repeat clicks are instant.
- **Sequential, not parallel (GIL)** — racing spine+fog in parallel was *slower*;
  the client fetches spine then fog. (PyPy still has a GIL — don't "add threads".)
- **Compact wire formats** — fog `[travel,q,r]`; segments
  `[travel,code,color,coords]`, 5-decimal, Douglas-Peucker-simplified. Both
  **gzipped** by the server (spine ~655 KB → ~147 KB).

### What's left (structural, not code)

- **~115 ms fixed HF-proxy tax** per request on the free tier — unremovable
  without a paid tier / self-hosting. A warm fresh click lands ~250–300 ms live.
- **45-min access exploration** (`MAX_ACCESS_WALK_MIN`). Shrinking it is the next
  ~1.5–2× but it's a *fidelity* trade (rare far-from-transit origins lose reach),
  so it was deliberately **not** done. A Rust/PyO3 kernel is the other big lever.

> Correctness gate: any engine change must keep `python scripts/validate.py` at
> **0 mismatches** (RAPTOR vs the independent CSA oracle, which uses the *same*
> access legs — so it validates transit propagation). The pass above is exact;
> geometry work only affects rendering.

---

## Rendering architecture (frontend perf is here)

Three **stacked MapLibre maps**, camera-synced every frame (`web/app.js`):

1. `#map` — colour Liberty basemap (the only interactive one; owns pan/zoom and
   `maxBounds`/`minZoom` locking the view to the data extent).
2. `#maskmap` — empty-style (no tiles) map drawing **only the grey hex grid**,
   with the container set to CSS `mix-blend-mode: saturation`. This desaturates
   the colour map *behind it* wherever a hex is painted → the unreachable area is
   real map detail in **black & white**; reachable cells stay colour.
3. `#spinemap` — empty-style map drawing **only the transit spine**, above the
   mask so the coloured lines are never desaturated.

The hex grid is ~80 k cells over `OSM_BBOX`; geometry is built **once**.

**Reveal = feature-state, not `setData`** (perf): the ~80 k hexes are uploaded to
the GPU one time. As the fog streams, each reached cell's `travel` is set via
`setFeatureState` (cheap, no geometry re-upload — the old code re-`setData`'d all
80 k every 200 ms, the main click-time stutter). `clearReach` =
`removeFeatureState`. The budget cutoff is a `fill-opacity` expression over the
`travel` feature-state, updated with `setPaintProperty` — so the slider stays
instant.

**Panning fast-path**: while the camera moves (`movestart`→`moveend` toggles
`body.panning`) the glass panels' `backdrop-filter` blur is dropped and the
`symbol-placement: line` spine arrows are hidden — both are re-computed every
frame otherwise. All three maps use `renderWorldCopies: false`, and the heavy
mask map's pixel ratio is capped (its resolution barely matters under the blend).

**Bilingual UI** (`web/`): French by default (Québec) with an FR/EN toggle.
Static text uses `data-i18n` keys; dynamic strings (status, legend) read the
active language at render time; choice persists in `localStorage`.

## Transit line geometry

- **Buses**: GTFS `shapes.txt`, Douglas-Peucker simplified to 2 m
  (`gtfs.py:_simplify`). Stops are mapped onto the shape by
  `project_stops_to_shape` — **nearest point on segment** (not nearest vertex),
  strictly increasing, so sparse express segments don't mis-snap. A per-hop
  detour guard in `_trace_hop` (memoised by `_reconstruct_segments`) falls back to
  a straight leg if the sliced shape is > 1.6× the straight distance (kills loops).
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

---

## Deployment

Deployed on a **free Hugging Face Docker Space** (`HenriLD/mtl_isochrone`, 2 vCPU
/ 16 GB) running **PyPy** for the JIT speedup on the pure-Python engine — RAM is
free there, so PyPy's heavier footprint doesn't matter and latency is the goal.

- **`Dockerfile`**: `pypy:3.10-slim`, `pip install -r requirements.txt`
  (`starlette` + base `uvicorn` only — pure-Python, no C/Rust toolchain), serves
  on port **7860** (HF default; set in the README YAML `app_port`).
- **Data via the `/data` bucket**: the pickles are not in the image
  (`.dockerignore`). HF Storage Buckets mount at a path, and files appear at
  `<mount>/<key>`; `config._resolve_processed()` therefore *searches* the mount
  for `network.pkl` and uses whatever dir holds it (`MTL_DATA_DIR` overrides).
  `server._ensure_data()` is an optional HF-dataset download fallback.
- **Server** (`server/app.py`): sync `def` endpoints (CPU work runs in
  Starlette's threadpool, **not** on the event loop), `GZipMiddleware`, and a
  multi-query **warm-up** at startup so PyPy's JIT is compiled before first click.
- **Memory**: steady ~440 MB. The CSR-baked pickle keeps the startup peak ≈ steady
  (~440 MB) instead of ~990 MB — fits a 512 MB tier; trivial on 16 GB.

**Live latency**: warm fresh click ≈ **250–300 ms, no spikes** — ~115 ms fixed
HF-proxy tax + ~100 ms PyPy compute + gzip transfer. (The earlier ~1.4 s outliers
were the pre-fix per-query GC churn + event-loop blocking; see the perf pass.)

Push to the Space's git remote; HF builds the `Dockerfile`. The README's first
bytes must be the HF YAML metadata block (`sdk: docker`, `app_port: 7860`).
