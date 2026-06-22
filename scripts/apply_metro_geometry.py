"""Substitute the coarse GTFS metro shapes with accurate OSM geometry.

Reads data/raw/metro_geometry.json (from fetch_metro_geometry.py), orients each
line to match the existing GTFS shape's direction, replaces the shape in the
compiled network, and re-projects each metro pattern's stops onto the detailed
polyline so the per-hop spine tracing follows the real tunnel alignment.

Run (after fetch_metro_geometry.py):  python scripts/apply_metro_geometry.py
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import NETWORK_FILE, ROOT  # noqa: E402
from engine.gtfs import _simplify  # noqa: E402
from engine.model import project_stops_to_shape  # noqa: E402

GEO_FILE = ROOT / "data" / "raw" / "metro_geometry.json"


def _sq(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _orient(detailed: list[list[float]], coarse: list[list[float]]) -> list[list[float]]:
    """Flip the detailed line if the GTFS shape runs the other way."""
    c0, cn, d0, dn = coarse[0], coarse[-1], detailed[0], detailed[-1]
    straight = _sq(c0, d0) + _sq(cn, dn)
    crossed = _sq(c0, dn) + _sq(cn, d0)
    return detailed if straight <= crossed else list(reversed(detailed))


def _nearest(det: list[list[float]], p) -> int:
    return min(range(len(det)), key=lambda i: _sq(det[i], p))


def _trim_to_termini(det: list[list[float]], first, last) -> list[list[float]]:
    """Clip the polyline to the span between the line's two TERMINUS stations,
    dropping non-revenue tails (e.g. the garage track past Angrignon that OSM
    includes — it loops back near the line and otherwise mis-snaps stops)."""
    i0, i1 = _nearest(det, first), _nearest(det, last)
    lo, hi = (i0, i1) if i0 <= i1 else (i1, i0)
    seg = det[lo:hi + 1]
    return list(reversed(seg)) if i0 > i1 else seg


def apply_metro_geometry(net, geo_file: Path = GEO_FILE) -> int:
    """Swap the coarse GTFS metro shapes in `net` for accurate OSM geometry.
    Mutates `net` in place; returns the number of patterns updated (0 if the
    geometry file is missing, so the build still works without it)."""
    if not geo_file.exists():
        print(f"(no {geo_file.name} — keeping coarse GTFS metro shapes; "
              f"run scripts/fetch_metro_geometry.py to improve them)")
        return 0
    geo = json.loads(geo_file.read_text(encoding="utf-8"))
    metro = [r for r in net.routes if r.route_type == 1]

    # one detailed polyline per shape_id, trimmed to that pattern's terminus stops
    rep: dict[str, object] = {}      # shape_id -> route with the most stops
    for r in metro:
        if geo.get(r.route_id) and r.shape_id and (
                r.shape_id not in rep or len(r.stops) > len(rep[r.shape_id].stops)):
            rep[r.shape_id] = r

    detailed_by_shape: dict[str, list[list[float]]] = {}
    for sid, r in rep.items():
        coarse = net.shapes.get(sid)
        if not coarse or len(coarse) < 2:
            continue
        det = _simplify([[round(p[0], 6), round(p[1], 6)] for p in geo[r.route_id]], tol_m=2.0)
        det = _orient(det, coarse)
        first = (net.stop_lon[r.stops[0]], net.stop_lat[r.stops[0]])
        last = (net.stop_lon[r.stops[-1]], net.stop_lat[r.stops[-1]])
        detailed_by_shape[sid] = _trim_to_termini(det, first, last)

    swapped = 0
    for r in metro:
        det = detailed_by_shape.get(r.shape_id)
        if not det:
            continue
        net.shapes[r.shape_id] = det
        stop_lonlat = [(net.stop_lon[s], net.stop_lat[s]) for s in r.stops]
        r.stop_shape_idx = project_stops_to_shape(stop_lonlat, det)
        swapped += 1
    print(f"Applied OSM metro geometry to {swapped} patterns ({sorted(detailed_by_shape)} shapes).")
    return swapped


def main() -> None:
    net = pickle.loads(NETWORK_FILE.read_bytes())
    apply_metro_geometry(net)
    NETWORK_FILE.write_bytes(pickle.dumps(net))
    print(f"Saved {NETWORK_FILE}")


if __name__ == "__main__":
    main()
