"""Thin Starlette server around the RAPTOR engine.

Loads the compiled network once at startup, exposes the /api/* endpoints, and
serves the static MapLibre frontend. Deliberately stateless and dependency-light.

Why Starlette (not FastAPI): the only third-party pieces are Starlette + uvicorn,
both pure-Python, so the whole stack runs unchanged on **PyPy** (FastAPI drags in
pydantic-core, a Rust extension whose PyPy wheels are unreliable). The engine
itself is pure stdlib. This keeps the free Hugging Face Space image on
`pypy:3.10-slim` for the ~5-10x JIT speedup with no compiled dependencies.

Run:  python -m uvicorn server.app:app --port 8077      (CPython, local dev)
      pypy3 -m uvicorn server.app:app --host 0.0.0.0 --port 7860   (Space)
"""
from __future__ import annotations

import os
import pickle
import threading
import time
from collections import OrderedDict

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from config import MAX_BUDGET_MIN, NETWORK_FILE, ROOT, WALK_GRAPH_FILE
from engine.raptor import ROUTE_TYPE_MODE, SPINE_TYPES, compute_isochrone, prepare_network
from engine.walk import egress_hex_disc, egress_hex_graph


def _ensure_data() -> None:
    """Optional fallback: fetch the compiled pickles from a Hugging Face Dataset
    if they're not already on disk. Normally a no-op — on the Space the pickles
    live in the persistent-storage bucket at /data (auto-detected by config), and
    locally the build scripts have written them. Only used if neither is present
    AND these env vars are set:
        HF_DATA_REPO = "user/mtl-isochrone-data"   (a dataset repo)
        HF_TOKEN     = <read token>                (only if the dataset is private)"""
    if NETWORK_FILE.exists() and WALK_GRAPH_FILE.exists():
        return
    repo = os.environ.get("HF_DATA_REPO")
    if not repo:
        return
    import shutil

    from huggingface_hub import hf_hub_download
    token = os.environ.get("HF_TOKEN")
    NETWORK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for filename, dest in (("network.pkl", NETWORK_FILE), ("walk_graph.pkl", WALK_GRAPH_FILE)):
        print(f"Fetching {filename} from dataset {repo}...")
        cached = hf_hub_download(repo_id=repo, filename=filename, repo_type="dataset", token=token)
        shutil.copy(cached, dest)


_ensure_data()

import config as _config
print(_config.describe_data_resolution())
if not NETWORK_FILE.exists():
    raise SystemExit(
        f"network.pkl not found at {NETWORK_FILE}. If running on a HF Space, check "
        "that the Storage Bucket is mounted and that the pickles are inside it; set "
        "MTL_DATA_DIR to the directory that actually contains network.pkl (see the "
        "'contents of /data' listing above), then restart the Space.")

with open(NETWORK_FILE, "rb") as f:
    NET = pickle.load(f)
prepare_network(NET)            # build per-route bisect columns + hop-geometry cache up front
print(f"Loaded network: {NET.n_stops} stops, service date {NET.service_date}")

WALK = None
if WALK_GRAPH_FILE.exists():
    with open(WALK_GRAPH_FILE, "rb") as f:
        WALK = pickle.load(f)
    # Pickles built by build_walk_graph.py already ship CSR with `adj` dropped.
    # These calls are no-ops then; they only kick in (and free memory) for an
    # older pickle that still carries the tuple adjacency.
    WALK.build_csr()
    WALK.free_adj()
    print(f"Loaded walk graph: {WALK.n_nodes} nodes (street-network access enabled, CSR)")
else:
    print("No walk graph found — using straight-line access (run build_walk_graph.py)")

ALL_MODES = set(ROUTE_TYPE_MODE.values())


def _parse_time(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


# --- single-flight isochrone cache -------------------------------------------
# The spine and fog endpoints are fired in parallel for the same origin. Without
# coordination each would recompute RAPTOR. This memoizes the IsochroneResult by
# request key and makes the second caller wait for (and reuse) the first's
# computation, so RAPTOR runs once per (origin, time, modes). Also serves instant
# repeats (e.g. re-clicking the same point).
_iso_cache: OrderedDict = OrderedDict()
_iso_events: dict = {}
_iso_lock = threading.Lock()
_ISO_CACHE_MAX = 8


def get_iso(lat: float, lon: float, time_str: str, modes: str):
    allowed = {m.strip() for m in modes.split(",") if m.strip()} & ALL_MODES or ALL_MODES
    dep = _parse_time(time_str)
    key = (round(lat, 6), round(lon, 6), time_str, ",".join(sorted(allowed)))
    with _iso_lock:
        if key in _iso_cache:
            _iso_cache.move_to_end(key)
            return _iso_cache[key], dep
        ev = _iso_events.get(key)
        owner = ev is None
        if owner:
            _iso_events[key] = ev = threading.Event()
    if not owner:                              # another request is computing it
        ev.wait(timeout=30)
        with _iso_lock:
            if key in _iso_cache:
                return _iso_cache[key], dep
    result = compute_isochrone(NET, lat, lon, dep, MAX_BUDGET_MIN * 60,
                               allowed_modes=allowed, walk_graph=WALK)
    with _iso_lock:
        _iso_cache[key] = result
        _iso_cache.move_to_end(key)
        while len(_iso_cache) > _ISO_CACHE_MAX:
            _iso_cache.popitem(last=False)
        _iso_events.pop(key, None)
    ev.set()
    return result, dep


def _query(request, name: str, default=None):
    return request.query_params.get(name, default)


def _origin(request):
    """Parse required lat/lon plus optional time/modes; returns a 400 JSONResponse
    on bad input (else a 4-tuple)."""
    try:
        lat = float(request.query_params["lat"])
        lon = float(request.query_params["lon"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "lat and lon are required floats"}, status_code=400)
    time_str = _query(request, "time", "08:00")
    modes = _query(request, "modes", "metro,bus,rail,tram")
    return lat, lon, time_str, modes


def meta(request) -> JSONResponse:
    return JSONResponse({
        "service_date": NET.service_date,
        "feeds": NET.feeds,
        "n_stops": NET.n_stops,
        "modes": sorted(ALL_MODES),
        "max_budget_min": MAX_BUDGET_MIN,
        "center": [45.5152, -73.5616],
    })


def lines(request) -> JSONResponse:
    """The distinct rapid-transit "spine" lines (metro / REM / exo trains) with
    their official colours, for the legend. Buses are intentionally excluded —
    they're consolidated into one colour on the map. Deduped by route_id; the
    feed id (route_id prefix, e.g. 'stm', 'rem', 'exo_trains') tags the agency."""
    seen: dict = {}
    for r in NET.routes:
        if r.route_type not in SPINE_TYPES or r.route_id in seen:
            continue
        seen[r.route_id] = {
            "name": r.route_name,
            "color": r.route_color,
            "type": r.route_type,
            "feed": (r.route_id.split(":", 1)[0] if ":" in r.route_id else ""),
        }
    # group order: metro (1) -> REM (0) -> exo trains (2); then by name
    rank = {1: 0, 0: 1, 2: 2}
    out = sorted(seen.values(), key=lambda d: (rank.get(d["type"], 9), d["name"]))
    return JSONResponse({"lines": out})


def isochrone(request) -> JSONResponse:
    """Compute once at the max budget. Every stop/segment carries `travel`
    (seconds from departure), so the client filters any smaller budget locally."""
    parsed = _origin(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    lat, lon, time_str, modes = parsed
    result, dep = get_iso(lat, lon, time_str, modes)
    return JSONResponse({
        "origin": [lat, lon],
        "departure": time_str,
        "max_budget_min": MAX_BUDGET_MIN,
        "service_date": NET.service_date,
        "count": len(result.stops),
        "segments": result.segments,
    })


def fog(request) -> StreamingResponse:
    """Reachable walk-area hexes as NDJSON [travel, q, r], streamed in increasing
    travel order (near->far reveal). Only reachable cells are sent — the client
    paints an opaque grey hex grid over the whole bbox itself and reveals these
    cells via the budget filter, so the unreachable zone (incl. water) is fully
    greyscale without us streaming every grey cell."""
    parsed = _origin(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    lat, lon, time_str, modes = parsed

    def generate():
        if WALK is None:
            return
        result, dep = get_iso(lat, lon, time_str, modes)
        cutoff = dep + MAX_BUDGET_MIN * 60
        seen: set = set()
        on_graph = [(WALK.stop_hex[r.stop_index], r.arrival)
                    for r in result.stops if WALK.stop_hex[r.stop_index] is not None]
        off_graph = [(r.lon, r.lat, r.arrival)
                     for r in result.stops if WALK.stop_hex[r.stop_index] is None]

        buf: list[str] = []
        for gen in (egress_hex_graph(WALK, on_graph, cutoff, dep, seen=seen),
                    egress_hex_disc(off_graph, cutoff, dep, seen=seen)):
            for travel, (q, r) in gen:
                buf.append(f"[{travel},{q},{r}]")
                if len(buf) >= 512:
                    yield "\n".join(buf) + "\n"
                    buf = []
        if buf:
            yield "\n".join(buf) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


def _warm_up() -> None:
    """Prime the hop-geometry cache and, on PyPy, force the JIT to compile and
    stabilise the hot paths BEFORE any user clicks — otherwise the first few real
    queries pay interpreter + compilation cost and spike (we measured a ~1.3 s
    outlier cold). We run several representative queries: different regions
    (downtown / on-graph suburb / off-graph), and a mode-filtered one (exercises
    the route_allowed branch), repeated so the JIT sees enough iterations."""
    cases = [
        (45.5017, -73.5673, ALL_MODES),          # downtown, all modes
        (45.5017, -73.5673, {"metro", "rail"}),  # mode filter branch
        (45.46, -73.62, ALL_MODES),              # west end
        (45.55, -73.55, ALL_MODES),              # east / on-graph
        (45.62, -73.50, ALL_MODES),              # far north (more off-graph)
    ]
    t = time.perf_counter()
    try:
        for _ in range(2):                       # two passes so the JIT settles
            for lat, lon, modes in cases:
                compute_isochrone(NET, lat, lon, _parse_time("08:00"),
                                  MAX_BUDGET_MIN * 60, allowed_modes=modes, walk_graph=WALK)
        print(f"Warm-up: {2 * len(cases)} queries in {(time.perf_counter() - t) * 1000:.0f} ms")
    except Exception as e:                       # never let warm-up block serving
        print(f"Warm-up skipped: {e}")


_warm_up()

routes = [
    Route("/api/meta", meta),
    Route("/api/lines", lines),
    Route("/api/isochrone", isochrone),
    Route("/api/fog", fog),
    # static frontend (mounted last so /api/* wins)
    Mount("/", app=StaticFiles(directory=str(ROOT / "web"), html=True), name="web"),
]

# GZip the JSON responses (the ~1 MB spine compresses ~5-8x); the fog NDJSON
# stream is compressed chunk-by-chunk, so the near->far reveal is preserved.
app = Starlette(routes=routes, middleware=[Middleware(GZipMiddleware, minimum_size=500)])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
