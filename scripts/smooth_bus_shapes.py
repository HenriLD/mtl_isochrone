"""One-time migration: corner-round the bus shapes in an existing network.pkl.

build_network.py already does this for fresh builds; this applies the same change
to the current pickle in place — without a full rebuild — so the walk-graph
transfers and applied OSM metro geometry are preserved. It re-reads the DENSE
shapes from the GTFS zips (the pickle only kept the simplified ones, which can't
be smoothed without cutting corners), smooths the bus shapes, and re-projects the
bus stops onto them. Idempotent (re-parses from source each run). Rendering only;
travel times are unaffected.

Usage:  python scripts/smooth_bus_shapes.py
"""
from __future__ import annotations

import csv
import io
import pickle
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DATA_RAW, GTFS_FEEDS, NETWORK_FILE  # noqa: E402
from engine.gtfs import smooth_shape  # noqa: E402
from engine.model import project_stops_to_shape  # noqa: E402


def _dense_shapes() -> dict[str, list]:
    """Namespaced shape_id -> dense [lon,lat] polyline, straight from the GTFS."""
    out: dict[str, list] = {}
    for feed in GTFS_FEEDS:
        fid = feed["id"]
        path = DATA_RAW / f"gtfs_{fid}.zip"
        if not path.exists():
            continue
        with zipfile.ZipFile(path) as zf:
            if "shapes.txt" not in zf.namelist():
                continue
            local: dict[str, list] = defaultdict(list)
            with zf.open("shapes.txt") as raw:
                for r in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")):
                    local[r["shape_id"]].append(
                        (int(r["shape_pt_sequence"]), float(r["shape_pt_lon"]), float(r["shape_pt_lat"])))
            for sid, pts in local.items():
                pts.sort()
                out[f"{fid}:{sid}"] = [[round(lon, 5), round(lat, 5)] for (_, lon, lat) in pts]
    return out


def main() -> None:
    net = pickle.load(open(NETWORK_FILE, "rb"))
    dense = _dense_shapes()

    bus_shape_ids = {r.shape_id for r in net.routes
                     if r.route_type not in (0, 1, 2) and r.shape_id and r.shape_id in net.shapes}
    before = sum(len(net.shapes[s]) for s in bus_shape_ids)

    smoothed = 0
    for sid in bus_shape_ids:                       # smooth(dense) replaces the simplified shape
        d = dense.get(sid)
        if d and len(d) >= 3:
            net.shapes[sid] = smooth_shape(d)
            smoothed += 1
    after = sum(len(net.shapes[s]) for s in bus_shape_ids)

    reprojected = 0                                 # re-project bus stops onto the smoothed shapes
    for r in net.routes:
        if r.route_type in (0, 1, 2) or not r.shape_id or r.shape_id not in net.shapes:
            continue
        shape = net.shapes[r.shape_id]
        if len(shape) >= 2:
            stop_lonlat = [(net.stop_lon[s], net.stop_lat[s]) for s in r.stops]
            r.stop_shape_idx = project_stops_to_shape(stop_lonlat, shape)
            reprojected += 1

    net._bus_smoothed = True
    with open(NETWORK_FILE, "wb") as f:
        pickle.dump(net, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"Smoothed {smoothed}/{len(bus_shape_ids)} bus shapes, re-projected {reprojected} routes.")
    print(f"Bus shape vertices: {before} -> {after} ({after / max(before, 1):.2f}x)")
    print(f"Wrote {NETWORK_FILE} ({NETWORK_FILE.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
