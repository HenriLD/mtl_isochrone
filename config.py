"""Central configuration: feeds, paths, and tunable knobs.

Adding a transit agency later (e.g. REM, exo) is just another entry in
``GTFS_FEEDS`` — ingestion merges them into one network.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_RAW = ROOT / "data" / "raw"


def _resolve_processed() -> Path:
    """Directory holding the compiled pickles (network.pkl / walk_graph.pkl).

    On a Hugging Face Space a Storage Bucket is mounted at a configurable path
    (default /data here), and files appear at <mount>/<their key in the bucket> —
    so if the pickles were synced under a prefix (e.g. data/processed/), they land
    at /data/data/processed/, not /data/. To be robust to however they were
    uploaded, we (1) try explicit/known roots directly, then (2) search a mounted
    root for network.pkl and use whatever directory actually contains it.

    Override the search with MTL_DATA_DIR (point it straight at the right dir,
    e.g. /data/data/processed) — no redeploy needed, just restart the Space."""
    local = ROOT / "data" / "processed"
    roots = []
    if os.environ.get("MTL_DATA_DIR"):
        roots.append(Path(os.environ["MTL_DATA_DIR"]))
    roots.append(Path("/data"))            # HF bucket default mount in this app
    roots.append(local)
    # 1) direct hit: <root>/network.pkl
    for d in roots:
        if (d / "network.pkl").exists():
            return d
    # 2) the file may sit under a prefix inside a mounted bucket — find it
    for d in roots:
        if d.is_dir():
            try:
                hit = next(d.rglob("network.pkl"), None)
            except OSError:
                hit = None
            if hit is not None:
                return hit.parent
    return local            # first build writes here; server fails loudly if empty


def describe_data_resolution() -> str:
    """Human-readable diagnostic of where we looked and what we found — logged at
    startup so a mount/prefix mismatch is obvious in the Space runtime logs."""
    lines = [f"DATA_PROCESSED resolved to: {DATA_PROCESSED}",
             f"network.pkl present: {NETWORK_FILE.exists()}"]
    for probe in (os.environ.get("MTL_DATA_DIR"), "/data"):
        if probe and Path(probe).is_dir():
            try:
                entries = sorted(p.name + ("/" if p.is_dir() else "") for p in Path(probe).iterdir())
            except OSError as e:
                entries = [f"<unreadable: {e}>"]
            lines.append(f"contents of {probe}: {entries[:50]}")
        elif probe:
            lines.append(f"{probe} is not a directory (not mounted?)")
    return "\n".join(lines)


DATA_PROCESSED = _resolve_processed()
NETWORK_FILE = DATA_PROCESSED / "network.pkl"
WALK_GRAPH_FILE = DATA_PROCESSED / "walk_graph.pkl"
OSM_RAW_FILE = DATA_RAW / "osm_walk.json"

# Each feed: a stable id (used for filenames + stop-id namespacing) and a URL.
# Phase 1 ships STM (bus + metro). REM/exo slot in here once a stable feed URL
# is pinned; ingestion namespaces stop ids per-feed so they never collide.
_EXO_SECTORS = {
    "exo_trains": "trains",   # all commuter rail (exo1–exo6)
    "exo_citcrc": "citcrc", "exo_citla": "citla", "exo_citpi": "citpi",
    "exo_citrous": "citrous", "exo_citsv": "citsv", "exo_citso": "citso",
    "exo_citvr": "citvr", "exo_mrclasso": "mrclasso", "exo_mrclm": "mrclm",
    "exo_omitsju": "omitsju", "exo_lrrs": "lrrs",
}

GTFS_FEEDS = [
    {
        "id": "stm",
        "name": "Société de transport de Montréal",
        "url": "https://www.stm.info/sites/default/files/gtfs/gtfs_stm.zip",
    },
    {
        "id": "rem",
        "name": "Réseau express métropolitain",
        "url": "https://gtfs.gpmmom.ca/gtfs/gtfs.zip",
    },
] + [
    {"id": fid, "name": f"exo ({sector})", "url": f"https://exo.quebec/xdata/{sector}/google_transit.zip"}
    for fid, sector in _EXO_SECTORS.items()
]

# --- Access / transfer tuning (Phase 1: straight-line placeholder legs) ---
# These crude geometric legs stand in for the real OSM walk/bike graph that
# arrives in Phase 2. Kept here so they're easy to experiment with.
WALK_SPEED_MPS = 5_000 / 3600          # 5 km/h in metres/second
MAX_TRANSFER_RADIUS_M = 300            # stop -> stop footpath transfers
MAX_TRANSFER_SECONDS = MAX_TRANSFER_RADIUS_M / WALK_SPEED_MPS

# The first walk (origin -> transit network) may use the whole time budget, so a
# pin far from any station can still walk to one and ride. Capped here so the
# always-at-max-budget computation doesn't explore an unrealistic walk radius.
# The access Dijkstra's cost scales ~with explored area (radius^2), so this cap
# is also the main lever on per-query latency: 35 min (~2.9 km) covers anyone
# within a realistic walk of transit on the island + near suburbs while keeping
# the access scan ~40% cheaper than 45 min. (Origins needing a >35-min walk to
# *any* stop — vanishingly rare in the covered area — lose reach: a deliberate,
# small fidelity trade for responsiveness.)
MAX_ACCESS_WALK_MIN = 35

# --- OSM walk graph (Phase 2: real walking network) ---
# Bounding box (s, w, n, e) for the high-res walk graph: the island PLUS the
# near north shore (Laval) and south shore (Longueuil / Brossard / Boucherville).
# Far exurbs stay on the cheap straight-line "disc" fog fallback.
OSM_BBOX = (45.40, -74.05, 45.75, -73.38)
OSM_TILES = 4                          # NxN tiling of the bbox for Overpass queries
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# highway values we won't let pedestrians use
OSM_EXCLUDE_HIGHWAY = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "construction", "proposed", "raceway", "bus_guideway",
}
# Stop -> stop footpath transfers: cap on network walking time between stops.
MAX_TRANSFER_WALK_SECONDS = 300        # ~415 m at 5 km/h

# A stop (or clicked origin) farther than this from any walk-graph node is
# treated as "off-graph" — i.e. off-island suburbs the island walk graph doesn't
# cover. Those fall back to straight-line (geometric) access/transfers, a
# low-resolution stand-in instead of expanding the graph to the whole region.
WALK_SNAP_MAX_M = 400

# Isochrones are computed once at this max budget; the UI budget slider then
# filters client-side (earliest-arrival times are budget-independent).
MAX_BUDGET_MIN = 90

# Default service date to build the timetable for (a typical weekday).
# None => the build script picks the busiest service date in the feed.
SERVICE_DATE = None
