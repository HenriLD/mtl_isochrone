"""Independent reference engine: Connection Scan Algorithm (CSA).

A deliberately *different* algorithm from RAPTOR — it sweeps a single
time-ordered list of elementary connections instead of scanning routes in
rounds. Over the same network it must yield identical earliest-arrival labels,
so it's used by scripts/validate.py to cross-check RAPTOR for correctness.

Not used in production; clarity over speed.
"""
from __future__ import annotations

from config import MAX_ACCESS_WALK_MIN, WALK_SPEED_MPS
from engine.model import Network
from engine.raptor import ROUTE_TYPE_MODE, _access_straight, _access_walk

INF = 10 ** 12


def build_connections(net: Network):
    """Flatten every trip into elementary connections, sorted by departure.

    Returns parallel arrays (dep_time, dep_stop, arr_time, arr_stop, mode).
    Static for a network, so build once and reuse across queries.
    """
    dep_t, dep_s, arr_t, arr_s, mode = [], [], [], [], []
    for route in net.routes:
        m = ROUTE_TYPE_MODE.get(route.route_type, "bus")
        stops = route.stops
        for ti in range(route.n_trips):
            dep = route.dep[ti]
            arr = route.arr[ti]
            for i in range(len(stops) - 1):
                dep_t.append(dep[i])
                dep_s.append(stops[i])
                arr_t.append(arr[i + 1])
                arr_s.append(stops[i + 1])
                mode.append(m)
    order = sorted(range(len(dep_t)), key=lambda k: dep_t[k])
    return (
        [dep_t[k] for k in order],
        [dep_s[k] for k in order],
        [arr_t[k] for k in order],
        [arr_s[k] for k in order],
        [mode[k] for k in order],
    )


def _bisect_dep(dep_t, value):
    lo, hi = 0, len(dep_t)
    while lo < hi:
        mid = (lo + hi) // 2
        if dep_t[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return lo


def csa_isochrone(net, connections, origin_lat, origin_lon, departure_sec, budget_sec,
                  allowed_modes=None, walk_graph=None):
    """Earliest-arrival labels via Connection Scan. Returns {stop_index: arrival}."""
    dep_t, dep_s, arr_t, arr_s, c_mode = connections
    n = net.n_stops
    cutoff = departure_sec + budget_sec
    arr = [INF] * n

    # access (identical to RAPTOR): the first walk may use the whole budget
    access_sec = min(budget_sec, MAX_ACCESS_WALK_MIN * 60)
    access = []
    if walk_graph is not None:
        access = _access_walk(walk_graph, origin_lon, origin_lat, access_sec)
    if not access:
        access = _access_straight(net, origin_lat, origin_lon, access_sec * WALK_SPEED_MPS)
    for s, walk_sec in access:
        t = departure_sec + int(walk_sec)
        if t < arr[s] and t <= cutoff:
            arr[s] = t
    # (no footpaths from access stops — walk access is already complete; transfers
    #  are single-hop and applied only after a transit connection, matching RAPTOR)

    # sweep connections in departure-time order
    for k in range(_bisect_dep(dep_t, departure_sec), len(dep_t)):
        dt = dep_t[k]
        if dt > cutoff:
            break
        if allowed_modes is not None and c_mode[k] not in allowed_modes:
            continue
        ds = dep_s[k]
        if arr[ds] <= dt:                       # already at the departure stop in time
            na = arr_t[k]
            if na <= cutoff:
                a_stop = arr_s[k]
                if na < arr[a_stop]:
                    arr[a_stop] = na
                # seed single-hop footpaths from this transit arrival (na) even
                # if na isn't a_stop's best label — matches RAPTOR's round_transit
                for t2, w in net.transfers[a_stop]:
                    cand = na + int(w)
                    if cand <= cutoff and cand < arr[t2]:
                        arr[t2] = cand

    return {s: arr[s] for s in range(n) if arr[s] <= cutoff}
