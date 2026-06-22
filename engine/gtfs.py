"""GTFS ingestion: zip(s) -> compiled RAPTOR Network.

Pure standard library (zipfile + csv). Handles multiple feeds by namespacing
stop ids per feed (``feedid:stopid``) so they never collide.
"""
from __future__ import annotations

import csv
import io
import math
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from config import DATA_RAW, GTFS_FEEDS, MAX_TRANSFER_RADIUS_M, WALK_SPEED_MPS
from engine.model import Network, Route, haversine_m, parse_gtfs_time, project_stops_to_shape

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_SHX = 111320 * math.cos(math.radians(45.5))   # metres per degree lon (Montreal)
_SHY = 110540


def _simplify(pts: list, tol_m: float = 8.0) -> list:
    """Douglas-Peucker on a [lon,lat] polyline (perp distance in metres). Drops
    near-collinear shape vertices to shrink the spine payload without changing
    how the line looks at city/neighbourhood zoom."""
    if len(pts) <= 2:
        return pts

    def pdist(p, a, b):
        px, py = p[0] * _SHX, p[1] * _SHY
        ax, ay = a[0] * _SHX, a[1] * _SHY
        bx, by = b[0] * _SHX, b[1] * _SHY
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - ax - t * dx, py - ay - t * dy)

    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        dmax, idx = tol_m, -1
        for k in range(i + 1, j):
            d = pdist(pts[k], pts[i], pts[j])
            if d > dmax:
                dmax, idx = d, k
        if idx != -1:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [pts[k] for k in range(len(pts)) if keep[k]]


def _open_csv(zf: zipfile.ZipFile, name: str):
    """Yield dict rows from a GTFS table, or nothing if the table is absent."""
    if name not in zf.namelist():
        return
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


def _service_dates(zf: zipfile.ZipFile) -> dict[str, set[str]]:
    """Map service_id -> set of active YYYYMMDD dates (calendar + exceptions)."""
    active: dict[str, set[str]] = defaultdict(set)
    for row in _open_csv(zf, "calendar.txt"):
        sid = row["service_id"]
        start = date(int(row["start_date"][:4]), int(row["start_date"][4:6]), int(row["start_date"][6:8]))
        end = date(int(row["end_date"][:4]), int(row["end_date"][4:6]), int(row["end_date"][6:8]))
        d = start
        while d <= end:
            if row[_WEEKDAYS[d.weekday()]] == "1":
                active[sid].add(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
    for row in _open_csv(zf, "calendar_dates.txt"):
        sid, ymd, ex = row["service_id"], row["date"], row["exception_type"]
        if ex == "1":
            active[sid].add(ymd)
        elif ex == "2":
            active[sid].discard(ymd)
    return active


def _pick_service_date(zf: zipfile.ZipFile, service_dates: dict[str, set[str]]) -> str:
    """Choose the in-feed date with the most scheduled trips (prefer weekdays)."""
    trips_per_service: dict[str, int] = defaultdict(int)
    for row in _open_csv(zf, "trips.txt"):
        trips_per_service[row["service_id"]] += 1

    trips_on_date: dict[str, int] = defaultdict(int)
    for sid, dates in service_dates.items():
        n = trips_per_service.get(sid, 0)
        for ymd in dates:
            trips_on_date[ymd] += n

    def score(ymd: str) -> tuple[int, int]:
        d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
        return (trips_on_date[ymd], 1 if d.weekday() < 5 else 0)

    return max(trips_on_date, key=score)


def _load_feed(feed: dict, target_date: str | None, acc: dict) -> None:
    """Parse one feed's stops + active trips into the shared accumulators."""
    fid = feed["id"]
    path = DATA_RAW / f"gtfs_{fid}.zip"
    with zipfile.ZipFile(path) as zf:
        service_dates = _service_dates(zf)
        chosen = target_date or _pick_service_date(zf, service_dates)
        acc["service_date"] = chosen
        active_services = {sid for sid, ds in service_dates.items() if chosen in ds}
        print(f"  [{fid}] service date {chosen}: {len(active_services)} active services")

        route_meta = {
            r["route_id"]: {
                "type": int(r.get("route_type") or 3),
                "color": (r.get("route_color") or "").strip(),
                "name": (r.get("route_short_name") or r.get("route_long_name") or "").strip(),
            }
            for r in _open_csv(zf, "routes.txt")
        }

        # shapes: polyline geometry per shape_id (namespaced per feed)
        shapes_local: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        for r in _open_csv(zf, "shapes.txt"):
            shapes_local[r["shape_id"]].append(
                (int(r["shape_pt_sequence"]), float(r["shape_pt_lon"]), float(r["shape_pt_lat"]))
            )
        for sid, pts in shapes_local.items():
            pts.sort()
            # 5 decimals (~1 m) + light Douglas-Peucker. Keep the tolerance small:
            # at 8 m it crushed winding bus routes to a handful of points and made
            # legs cut straight across blocks. 2 m preserves the street path.
            coords = [[round(lon, 5), round(lat, 5)] for (_, lon, lat) in pts]
            acc["shapes"][f"{fid}:{sid}"] = _simplify(coords, tol_m=2.0)

        # stops: namespace ids, register in the shared index, return local->global map
        local_to_global: dict[str, int] = {}
        for r in _open_csv(zf, "stops.txt"):
            if not r.get("stop_lat") or not r.get("stop_lon"):
                continue
            gid = f"{fid}:{r['stop_id']}"
            idx = acc["stop_index"].get(gid)
            if idx is None:
                idx = len(acc["stop_ids"])
                acc["stop_index"][gid] = idx
                acc["stop_ids"].append(gid)
                acc["stop_name"].append(r.get("stop_name", ""))
                acc["stop_lat"].append(float(r["stop_lat"]))
                acc["stop_lon"].append(float(r["stop_lon"]))
            local_to_global[r["stop_id"]] = idx

        # trips active on the chosen date -> route_id (and shape_id)
        trip_route: dict[str, str] = {}
        trip_shape: dict[str, str] = {}
        for r in _open_csv(zf, "trips.txt"):
            if r["service_id"] in active_services:
                trip_route[r["trip_id"]] = r["route_id"]
                sid = r.get("shape_id") or ""
                trip_shape[r["trip_id"]] = f"{fid}:{sid}" if sid else ""

        # stop_times grouped per trip; rows = (seq, global_stop_idx, arr, dep)
        per_trip: dict[str, list[tuple[int, int, int, int]]] = defaultdict(list)
        for r in _open_csv(zf, "stop_times.txt"):
            tid = r["trip_id"]
            if tid not in trip_route:
                continue
            sid = local_to_global.get(r["stop_id"])
            if sid is None:
                continue
            dep = r["departure_time"] or r["arrival_time"]
            arr = r["arrival_time"] or r["departure_time"]
            if not dep:
                continue
            per_trip[tid].append((int(r["stop_sequence"]), sid, parse_gtfs_time(arr), parse_gtfs_time(dep)))

        # group trips into patterns (identical ordered stop sequence)
        for tid, rows in per_trip.items():
            rows.sort()
            stop_seq = tuple(sid for (_, sid, _, _) in rows)
            if len(stop_seq) < 2:
                continue
            key = (trip_route[tid], stop_seq)
            pat = acc["patterns"].get(key)
            if pat is None:
                meta = route_meta.get(trip_route[tid], {"type": 3, "color": "", "name": ""})
                pat = {
                    "stops": list(stop_seq),
                    "route_id": trip_route[tid],
                    "route_type": meta["type"],
                    "route_color": meta["color"],
                    "route_name": meta["name"],
                    "shape_id": trip_shape.get(tid, ""),
                    "trips": [],
                }
                acc["patterns"][key] = pat
            pat["trips"].append(([a for (_, _, a, _) in rows], [d for (_, _, _, d) in rows]))


def _build_transfers(net: Network) -> None:
    """Geometric footpath transfers between stops within MAX_TRANSFER_RADIUS_M.

    Uses a uniform grid index so this is ~O(n) instead of O(n^2). This is the
    Phase-1 placeholder for the OSM walk graph arriving in Phase 2.
    """
    n = net.n_stops
    transfers: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    # grid cell ~ the transfer radius, in degrees (latitude-based approximation)
    cell_deg = MAX_TRANSFER_RADIUS_M / 111_320.0
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i in range(n):
        grid[(int(net.stop_lat[i] / cell_deg), int(net.stop_lon[i] / cell_deg))].append(i)

    for i in range(n):
        ci = int(net.stop_lat[i] / cell_deg)
        cj = int(net.stop_lon[i] / cell_deg)
        seen: set[int] = set()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for j in grid.get((ci + di, cj + dj), ()):
                    if j == i or j in seen:
                        continue
                    seen.add(j)
                    dist = haversine_m(net.stop_lat[i], net.stop_lon[i], net.stop_lat[j], net.stop_lon[j])
                    if dist <= MAX_TRANSFER_RADIUS_M:
                        transfers[i].append((j, dist / WALK_SPEED_MPS))
    net.transfers = transfers
    total = sum(len(t) for t in transfers)
    print(f"  transfers: {total} edges ({total / max(n,1):.1f} avg/stop)")


def build_network(target_date: str | None = None) -> Network:
    """Ingest all configured feeds into a single compiled Network."""
    acc = {
        "stop_index": {}, "stop_ids": [], "stop_name": [], "stop_lat": [], "stop_lon": [],
        "patterns": {}, "shapes": {}, "service_date": "",
    }
    print(f"Ingesting {len(GTFS_FEEDS)} feed(s)...")
    for feed in GTFS_FEEDS:
        _load_feed(feed, target_date, acc)

    net = Network(
        stop_ids=acc["stop_ids"], stop_name=acc["stop_name"],
        stop_lat=acc["stop_lat"], stop_lon=acc["stop_lon"], stop_index=acc["stop_index"],
        service_date=acc["service_date"], feeds=[f["id"] for f in GTFS_FEEDS],
    )

    # materialise routes, sorted by departure at first stop
    net.stop_routes = [[] for _ in range(net.n_stops)]
    used_shapes: set[str] = set()
    for pat in acc["patterns"].values():
        trips = sorted(pat["trips"], key=lambda ad: ad[1][0])  # by departure at stop 0
        route = Route(
            stops=pat["stops"],
            arr=[t[0] for t in trips],
            dep=[t[1] for t in trips],
            route_id=pat["route_id"],
            route_type=pat["route_type"],
            route_color=pat["route_color"],
            route_name=pat["route_name"],
        )
        # snap each stop to a vertex on the route's shape, for tracing legs
        shape = acc["shapes"].get(pat["shape_id"])
        if shape and len(shape) >= 2:
            stop_lonlat = [(net.stop_lon[s], net.stop_lat[s]) for s in route.stops]
            route.shape_id = pat["shape_id"]
            route.stop_shape_idx = project_stops_to_shape(stop_lonlat, shape)
            used_shapes.add(pat["shape_id"])
        ridx = len(net.routes)
        net.routes.append(route)
        for pos, sidx in enumerate(route.stops):
            net.stop_routes[sidx].append((ridx, pos))

    net.shapes = {sid: acc["shapes"][sid] for sid in used_shapes}
    print(f"  stops: {net.n_stops}  routes(patterns): {len(net.routes)}  "
          f"trips: {sum(r.n_trips for r in net.routes)}  shapes: {len(net.shapes)}")
    _build_transfers(net)
    return net
