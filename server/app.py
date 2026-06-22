"""Thin FastAPI dev server around the RAPTOR engine.

Loads the compiled network once at startup, exposes a single /api/isochrone
endpoint, and serves the static MapLibre frontend. Deliberately stateless and
dependency-light so it can later move to a free backend tier unchanged.

Run:  python -m uvicorn server.app:app --reload   (from the repo root)
"""
from __future__ import annotations

import pickle
import threading
from collections import OrderedDict

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import MAX_BUDGET_MIN, NETWORK_FILE, ROOT, WALK_GRAPH_FILE
from engine.raptor import ROUTE_TYPE_MODE, SPINE_TYPES, compute_isochrone
from engine.walk import egress_hex_disc, egress_hex_graph

app = FastAPI(title="Montreal Isochrone")

with open(NETWORK_FILE, "rb") as f:
    NET = pickle.load(f)
print(f"Loaded network: {NET.n_stops} stops, service date {NET.service_date}")

WALK = None
if WALK_GRAPH_FILE.exists():
    with open(WALK_GRAPH_FILE, "rb") as f:
        WALK = pickle.load(f)
    print(f"Loaded walk graph: {WALK.n_nodes} nodes (street-network access enabled)")
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


def get_iso(lat: float, lon: float, time: str, modes: str):
    allowed = {m.strip() for m in modes.split(",") if m.strip()} & ALL_MODES or ALL_MODES
    dep = _parse_time(time)
    key = (round(lat, 6), round(lon, 6), time, ",".join(sorted(allowed)))
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


@app.get("/api/meta")
def meta() -> dict:
    return {
        "service_date": NET.service_date,
        "feeds": NET.feeds,
        "n_stops": NET.n_stops,
        "modes": sorted(ALL_MODES),
        "max_budget_min": MAX_BUDGET_MIN,
        "center": [45.5152, -73.5616],
    }


@app.get("/api/lines")
def lines() -> JSONResponse:
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


@app.get("/api/isochrone")
def isochrone(
    lat: float = Query(...),
    lon: float = Query(...),
    time: str = Query("08:00"),
    modes: str = Query("metro,bus,rail,tram"),
) -> JSONResponse:
    """Compute once at the max budget. Every stop/segment carries `travel`
    (seconds from departure), so the client filters any smaller budget locally."""
    result, dep = get_iso(lat, lon, time, modes)
    return JSONResponse({
        "origin": [lat, lon],
        "departure": time,
        "max_budget_min": MAX_BUDGET_MIN,
        "service_date": NET.service_date,
        "count": len(result.stops),
        "segments": result.segments,
    })


@app.get("/api/fog")
def fog(
    lat: float = Query(...),
    lon: float = Query(...),
    time: str = Query("08:00"),
    modes: str = Query("metro,bus,rail,tram"),
) -> StreamingResponse:
    """Reachable walk-area hexes as NDJSON [travel, q, r], streamed in increasing
    travel order (near->far reveal). Only reachable cells are sent — the client
    paints an opaque grey hex grid over the whole bbox itself and reveals these
    cells via the budget filter, so the unreachable zone (incl. water) is fully
    greyscale without us streaming every grey cell."""
    def generate():
        if WALK is None:
            return
        result, dep = get_iso(lat, lon, time, modes)
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


# static frontend (mounted last so /api/* wins)
app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True), name="web")
