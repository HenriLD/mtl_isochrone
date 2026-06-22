"""Range-RAPTOR isochrone engine (exact-schedule, single departure time).

Given an origin, a departure time, and a time budget, label every reachable
stop with its earliest arrival time. Round k == reachable with <= k boardings.

Pure Python, no third-party deps — so it runs locally now and ports to a free
backend tier or to the browser (Pyodide/WASM) later without changes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from config import MAX_ACCESS_WALK_MIN, WALK_SNAP_MAX_M, WALK_SPEED_MPS
from engine.model import Network, haversine_m
from engine.walk import WalkGraph, _Scratch, dijkstra

INF = 10 ** 12
MAX_ROUNDS = 8  # max boardings considered (rounds converge well before this)

# metres-per-degree near Montréal, for cheap planar leg lengths in segment tracing
_MX, _MY = 78000.0, 111000.0

# GTFS route_type -> mode key used by the UI toggles. REM is published as
# type 0 (light rail) and exo trains as type 2 — both ride under the "rail"
# toggle. Extended/unknown types (e.g. exo's 1501 buses) fall back to "bus".
ROUTE_TYPE_MODE = {0: "rail", 1: "metro", 2: "rail", 3: "bus"}

# rapid-transit types drawn as the thick "spine"; everything else is a bus feeder
SPINE_TYPES = {0, 1, 2}


@dataclass
class ReachedStop:
    stop_index: int
    stop_id: str
    name: str
    lat: float
    lon: float
    arrival: int       # seconds since midnight
    remaining: int     # budget seconds left on arrival (>= 0)


@dataclass
class IsochroneResult:
    stops: list["ReachedStop"]
    # transit spine as compact arrays [travel, code, color, coords]:
    # code 0=bus, 1=rail/spine; color = GTFS hex (no '#').
    segments: list[list]


def _earliest_trip(route, p: int, t: int) -> int | None:
    """Earliest trip index whose departure at position p is >= t.

    Assumes trips don't overtake (departures nondecreasing by trip index at a
    fixed position) — true for STM scheduled service. Binary search.
    """
    dep = route.dep
    lo, hi = 0, len(dep)
    while lo < hi:
        mid = (lo + hi) // 2
        if dep[mid][p] >= t:
            hi = mid
        else:
            lo = mid + 1
    return lo if lo < len(dep) else None


def _access_straight(net: Network, lat: float, lon: float, radius_m: float):
    """Fallback access legs: straight-line origin -> nearby stops (no walk graph)."""
    out = []
    for i in range(net.n_stops):
        d = haversine_m(lat, lon, net.stop_lat[i], net.stop_lon[i])
        if d <= radius_m:
            out.append((i, d / WALK_SPEED_MPS))
    return out


def _access_walk(wg: WalkGraph, lon: float, lat: float, max_sec: float):
    """Access legs along the real walk network: origin -> stops within max_sec.
    Returns [] if the origin is off-graph (a suburb) so the caller can fall back
    to straight-line access."""
    src = wg.nearest_node(lon, lat)
    if src is None:
        return []
    if haversine_m(lat, lon, wg.node_lat[src], wg.node_lon[src]) > WALK_SNAP_MAX_M:
        return []
    scratch = _Scratch(wg.n_nodes)
    dijkstra(wg, src, int(max_sec), scratch)
    # time to walk from the exact origin onto the snapped node
    base = haversine_m(lat, lon, wg.node_lat[src], wg.node_lon[src]) / WALK_SPEED_MPS
    reached: dict[int, float] = {}
    for nd in scratch.dirty:
        stops_here = wg.node_stops.get(nd)
        if not stops_here:
            continue
        t = scratch.dist[nd] + base
        if t <= max_sec:
            for s in stops_here:
                if t < reached.get(s, INF):
                    reached[s] = t
    return list(reached.items())


def compute_isochrone(
    net: Network,
    origin_lat: float,
    origin_lon: float,
    departure_sec: int,
    budget_sec: int,
    allowed_modes: set[str] | None = None,
    walk_graph: WalkGraph | None = None,
) -> "IsochroneResult":
    """Run RAPTOR and return every stop reachable within the budget.

    If ``walk_graph`` is given, origin access legs route along the real street
    network; otherwise they fall back to straight-line distance.
    """
    n = net.n_stops
    cutoff = departure_sec + budget_sec
    best = [INF] * n          # earliest arrival overall (the answer)
    prev = [INF] * n          # arrival after previous round (for boarding)
    # back-pointer for the journey tree (how each stop was reached):
    #   ("access",) | ("transit", ridx, board_pos, alight_pos) | ("transfer", from_stop)
    parent: list[tuple | None] = [None] * n

    # allowed_modes filters which routes can be boarded (mode toggles)
    def route_allowed(route) -> bool:
        if allowed_modes is None:
            return True
        return ROUTE_TYPE_MODE.get(route.route_type, "bus") in allowed_modes

    # --- access legs: origin -> stops on foot ---
    # The first walk (to the transit network) may use the WHOLE time budget, so a
    # pin far from any station can still walk to one and ride with the remaining
    # budget. (Transfers *between* transit legs stay capped at
    # MAX_TRANSFER_WALK_SECONDS — you wouldn't walk 20 min to change buses.)
    access_sec = min(budget_sec, MAX_ACCESS_WALK_MIN * 60)
    access_legs = []
    if walk_graph is not None:
        access_legs = _access_walk(walk_graph, origin_lon, origin_lat, access_sec)
    if not access_legs:   # no walk graph, or off-graph (suburban) origin
        access_legs = _access_straight(net, origin_lat, origin_lon, access_sec * WALK_SPEED_MPS)

    marked: set[int] = set()
    for s, walk_sec in access_legs:
        t = departure_sec + int(walk_sec)
        if t < best[s] and t <= cutoff:
            best[s] = t
            prev[s] = t
            parent[s] = ("access",)
            marked.add(s)
    # NB: no footpath relaxation from access stops — walk-graph access already
    # finds every stop reachable on foot from the origin, so an extra transfer
    # hop would just chain walking. Footpaths apply only between transit legs.

    for _ in range(MAX_ROUNDS):
        if not marked:
            break

        # collect the earliest boardable position per route from marked stops
        queue: dict[int, int] = {}
        for s in marked:
            for ridx, pos in net.stop_routes[s]:
                if pos < queue.get(ridx, INF):
                    queue[ridx] = pos
        marked = set()
        # earliest *transit* arrival at each stop this round — footpaths are
        # seeded from these (every transit arrival, not only improving ones), so
        # a transit leg that isn't a stop's best label can still feed a transfer.
        round_transit: dict[int, int] = {}

        # --- scan each route once ---
        for ridx, pos0 in queue.items():
            route = net.routes[ridx]
            if not route_allowed(route):
                continue
            trip_idx: int | None = None
            trip_arr = None
            board_pos = pos0
            for p in range(pos0, len(route.stops)):
                stop = route.stops[p]
                # if aboard a trip, record/try-to-improve this stop's arrival
                if trip_arr is not None:
                    a = trip_arr[p]
                    if a <= cutoff:
                        if a < round_transit.get(stop, INF):
                            round_transit[stop] = a
                        if a < best[stop]:
                            best[stop] = a
                            parent[stop] = ("transit", ridx, board_pos, p)
                            marked.add(stop)
                # can we catch an earlier trip here, using last round's arrival?
                if prev[stop] < INF and (trip_idx is None or prev[stop] <= route.dep[trip_idx][p]):
                    et = _earliest_trip(route, p, prev[stop])
                    if et is not None and (trip_idx is None or et < trip_idx):
                        trip_idx = et
                        trip_arr = route.arr[et]
                        board_pos = p

        # --- relax single-hop footpaths from this round's transit arrivals ---
        # Seeding from round_transit (a snapshot of transit arrivals) means walk
        # legs never chain into further walks, and a non-best transit arrival can
        # still feed a transfer. Footpath targets board next round but, unless
        # they're themselves transit-reached, never seed a further footpath.
        for s, tarr in round_transit.items():
            for t2, walk_sec in net.transfers[s]:
                cand = tarr + int(walk_sec)
                if cand <= cutoff and cand < best[t2]:
                    best[t2] = cand
                    parent[t2] = ("transfer", s)
                    marked.add(t2)

        prev = best[:]  # this round's labels feed next round's boarding

    # --- collect reached stops ---
    results: list[ReachedStop] = []
    for i in range(n):
        if best[i] <= cutoff:
            results.append(ReachedStop(
                stop_index=i, stop_id=net.stop_ids[i], name=net.stop_name[i],
                lat=net.stop_lat[i], lon=net.stop_lon[i],
                arrival=best[i], remaining=cutoff - best[i],
            ))

    segments = _reconstruct_segments(net, parent, best, departure_sec)
    return IsochroneResult(stops=results, segments=segments)


def _reconstruct_segments(net: Network, parent, best, departure_sec: int) -> list[list]:
    """Turn the back-pointer tree into drawable transit legs.

    Only transit legs are emitted (the spine) — walk/transfer legs are not drawn.
    Legs are per stop-to-stop hop, deduped by (route, hop), so a shared trunk
    (e.g. the green line downtown) is drawn exactly once.
    """
    def coord(s: int):
        return [round(net.stop_lon[s], 5), round(net.stop_lat[s], 5)]

    transit_hops: dict[tuple[int, int], None] = {}   # (route_idx, hop_index)
    for s in range(net.n_stops):
        p = parent[s]
        if p is not None and p[0] == "transit":
            _, ridx, bp, ap = p
            for i in range(bp, ap):
                transit_hops[(ridx, i)] = None

    # 'travel' on each segment = seconds from departure to *complete* that leg,
    # i.e. arrival at its far end. The frontend filters segments by the budget
    # slider with this, so a single max-budget result serves every budget.
    def travel(stop: int) -> int:
        return int(best[stop] - departure_sec)

    def hop_dist(coords) -> float:
        return sum(math.hypot((coords[k][0] - coords[k - 1][0]) * _MX,
                              (coords[k][1] - coords[k - 1][1]) * _MY)
                   for k in range(1, len(coords)))

    segments: list[list] = []
    for (ridx, i) in transit_hops:
        route = net.routes[ridx]
        a, b = route.stops[i], route.stops[i + 1]
        ca, cb = coord(a), coord(b)
        # trace the leg along the route's real shape geometry when available,
        # else fall back to a straight stop-to-stop line
        if route.shape_id and route.stop_shape_idx:
            shape = net.shapes[route.shape_id]
            pa, pb = route.stop_shape_idx[i], route.stop_shape_idx[i + 1]
            mid = [shape[k] for k in range(math.floor(pa) + 1, math.ceil(pb)) if pa < k < pb]
            coords = [ca] + mid + [cb]
            # guard against a mis-projected shape slicing in a big detour/loop:
            # if the traced path is far longer than the straight hop, draw straight.
            straight = math.hypot((cb[0] - ca[0]) * _MX, (cb[1] - ca[1]) * _MY)
            if mid and hop_dist(coords) > 1.6 * straight + 250:
                coords = [ca, cb]
        else:
            coords = [ca, cb]
        code = 1 if route.route_type in SPINE_TYPES else 0
        segments.append([travel(b), code, route.route_color, coords])
    return segments
