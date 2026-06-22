"""Compiled network: the in-memory layout RAPTOR scans.

The key idea (RAPTOR's speed trick): group trips into *patterns* — sets of
trips that visit the exact same ordered sequence of stops. Each pattern is a
"route" here. Within a route, trips are sorted by departure time, so boarding
the earliest catchable trip is a binary search, and a single linear sweep down
the stop sequence relaxes every downstream arrival.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Route:
    """A unique stop-pattern and all trips that run it (sorted by departure)."""
    stops: list[int]                       # ordered internal stop indices
    arr: list[list[int]]                   # arr[trip][pos] -> arrival sec
    dep: list[list[int]]                   # dep[trip][pos] -> departure sec
    route_id: str = ""                     # source GTFS route_id (for label)
    route_type: int = 3                    # GTFS route_type (1=metro, 3=bus, 2=rail)
    route_color: str = ""                  # GTFS route_color hex (no '#'), e.g. "00B300"
    route_name: str = ""                   # short name for labels, e.g. "1" or "165"
    shape_id: str = ""                     # GTFS shape this pattern follows (key into Network.shapes)
    stop_shape_idx: list[float] = field(default_factory=list)  # per stop: float pos (seg+t) on the shape

    # Transposed departure columns: dep_cols[pos] = packed array of every trip's
    # departure at that stop position, ascending (trips are sorted, no overtaking).
    # Lets the "earliest boardable trip" lookup be a C-level bisect over a flat
    # array instead of a Python binary search over dep[mid][pos]. Built lazily.
    dep_cols: object = None

    @property
    def n_trips(self) -> int:
        return len(self.dep)


@dataclass
class Network:
    # --- stops (index == internal stop id) ---
    stop_ids: list[str] = field(default_factory=list)
    stop_name: list[str] = field(default_factory=list)
    stop_lat: list[float] = field(default_factory=list)
    stop_lon: list[float] = field(default_factory=list)
    stop_index: dict[str, int] = field(default_factory=dict)

    # --- routes / patterns ---
    routes: list[Route] = field(default_factory=list)
    # per stop: list of (route_idx, position-of-stop-within-route)
    stop_routes: list[list[tuple[int, int]]] = field(default_factory=list)
    # per stop: list of (to_stop_idx, walk_seconds) footpath transfers
    transfers: list[list[tuple[int, float]]] = field(default_factory=list)
    # transfer leg geometry (street path), keyed by (from_stop, to_stop). Filled
    # once the OSM walk graph exists; absent => draw a straight line.
    transfer_geom: dict = field(default_factory=dict)

    # shape_id -> polyline [[lon, lat], ...] for tracing transit legs along the route
    shapes: dict = field(default_factory=dict)

    # metadata
    service_date: str = ""
    feeds: list[str] = field(default_factory=list)

    @property
    def n_stops(self) -> int:
        return len(self.stop_ids)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_gtfs_time(t: str) -> int:
    """'HH:MM:SS' (hours may exceed 24) -> seconds since midnight."""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def project_stops_to_shape(stop_lonlat: list[tuple[float, float]],
                           shape: list[list[float]]) -> list[float]:
    """For each stop, its float position `segment_index + t` (t in [0,1)) of the
    NEAREST POINT on the shape polyline, scanning forward (monotonic) and made
    strictly increasing.

    No shape_dist_traveled in the feed, so we map each stop to the closest point
    on the polyline. Nearest *point on a segment* (not nearest vertex): a stop in
    the middle of a long, sparsely-sampled express segment maps onto that segment
    instead of jumping to a far vertex — the bug that crammed the remaining stops
    and collapsed legs into straight cross-town cuts. Strict increase keeps every
    leg's slice non-empty.
    """
    n = len(shape)
    out: list[float] = []
    if n < 2:
        return [0.0] * len(stop_lonlat)
    seg_start = 0
    for lon, lat in stop_lonlat:
        best_d2, best_pos, best_seg = float("inf"), float(seg_start), seg_start
        for s in range(seg_start, n - 1):
            ax, ay = shape[s]
            dx, dy = shape[s + 1][0] - ax, shape[s + 1][1] - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else ((lon - ax) * dx + (lat - ay) * dy) / seg2
            t = 0.0 if t < 0 else 1.0 if t > 1 else t
            px, py = ax + t * dx, ay + t * dy
            d2 = (lon - px) ** 2 + (lat - py) ** 2
            if d2 < best_d2:
                best_d2, best_pos, best_seg = d2, s + t, s
        if out and best_pos <= out[-1]:
            best_pos = min(out[-1] + 1e-3, n - 1.0)
        out.append(best_pos)
        seg_start = best_seg  # monotonic progress along the shape
    return out


def seconds_to_hhmmss(sec: int) -> str:
    sec %= 24 * 3600
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
