"""Routable pedestrian graph built from OSM ways.

Pure standard library. Used for:
  - build time: precompute stop -> stop transfer walking times
  - query time: route from the clicked origin to nearby stops (access legs),
    and reconstruct street geometry for the walk legs actually used.

Bounded Dijkstra everywhere — we only ever explore a small local neighbourhood
(a few hundred metres for transfers, ~1 km for access), so it stays fast in
plain Python.
"""
from __future__ import annotations

import heapq
from array import array
from dataclasses import dataclass, field

from config import WALK_SPEED_MPS
from engine.model import haversine_m

INF = 10 ** 12


@dataclass
class WalkGraph:
    node_lon: list[float] = field(default_factory=list)
    node_lat: list[float] = field(default_factory=list)
    adj: list[list[tuple[int, int]]] = field(default_factory=list)  # (neighbor, seconds)
    cell_deg: float = 0.001                                         # ~111 m grid cells
    grid: dict[tuple[int, int], list[int]] = field(default_factory=dict)
    stop_node: list[int] = field(default_factory=list)              # transit stop -> node idx
    node_stops: dict[int, list[int]] = field(default_factory=dict)  # node idx -> stop idxs

    # precomputed hex-adjacency graph for fast fog egress (built once, see
    # build_hex_graph). hex key = (q, r); egress runs over ~15k hexes instead of
    # ~1.3M raw walk nodes.
    hex_adj: dict = field(default_factory=dict)        # hexkey -> [neighbor hexkey, ...]
    stop_hex: list = field(default_factory=list)       # transit stop -> hexkey (None if off-graph)

    # --- CSR (compressed sparse row) edge layout, built from `adj` ---
    # The query-time Dijkstra (access legs) is the hot path; iterating `adj` —
    # 1.5M lists of (neighbor, sec) tuples — burns most of its time on tuple
    # unpacking and list indirection. CSR flattens every edge into three packed
    # `array` buffers so the inner loop is plain integer indexing over a slice:
    #   csr_tgt[csr_off[u]:csr_off[u+1]]  are u's neighbours,
    #   csr_wt [csr_off[u]:csr_off[u+1]]  their walk-seconds.
    # ~2x faster than the tuple form and far smaller in memory, so `adj` can be
    # dropped at runtime (see WalkGraph.free_adj). Built lazily (build_csr) so
    # pre-CSR pickles keep working.
    csr_off: object = None     # array('i'), len n_nodes+1
    csr_tgt: object = None     # array('i'), len n_edges
    csr_wt: object = None      # array('i'), len n_edges

    @property
    def n_nodes(self) -> int:
        return len(self.node_lon)

    def build_csr(self) -> None:
        """Flatten `adj` into packed CSR arrays. Idempotent; safe on old pickles."""
        n = len(self.node_lon)
        adj = self.adj
        off = array('i', bytes(4 * (n + 1)))
        acc = 0
        for u in range(n):
            off[u] = acc
            acc += len(adj[u])
        off[n] = acc
        tgt = array('i', bytes(4 * acc))
        wt = array('i', bytes(4 * acc))
        k = 0
        for u in range(n):
            for v, w in adj[u]:
                tgt[k] = v
                wt[k] = w
                k += 1
        self.csr_off, self.csr_tgt, self.csr_wt = off, tgt, wt

    def free_adj(self) -> None:
        """Drop the bulky tuple adjacency once CSR exists (runtime memory win).
        Only the offline build scripts need `adj` (build_hex_graph); the server
        and validator route exclusively through CSR Dijkstra."""
        if self.csr_off is not None:
            self.adj = []

    def _cell(self, lon: float, lat: float) -> tuple[int, int]:
        return (int(lat / self.cell_deg), int(lon / self.cell_deg))

    def nearest_node(self, lon: float, lat: float, max_rings: int = 12) -> int | None:
        """Closest graph node to a coordinate, via expanding grid-ring search."""
        ci, cj = self._cell(lon, lat)
        best, best_d = None, INF
        for r in range(max_rings + 1):
            for i in range(ci - r, ci + r + 1):
                for j in range(cj - r, cj + r + 1):
                    if r > 0 and ci - r < i < ci + r and cj - r < j < cj + r:
                        continue  # interior already scanned in a smaller ring
                    for nd in self.grid.get((i, j), ()):
                        d = haversine_m(lat, lon, self.node_lat[nd], self.node_lon[nd])
                        if d < best_d:
                            best, best_d = nd, d
            if best is not None and r >= 1:
                return best
        return best


def build_grid(wg: WalkGraph) -> None:
    grid: dict[tuple[int, int], list[int]] = {}
    for nd in range(wg.n_nodes):
        grid.setdefault(wg._cell(wg.node_lon[nd], wg.node_lat[nd]), []).append(nd)
    wg.grid = grid


class _Scratch:
    """Reusable Dijkstra buffers so the 9k-stop transfer build doesn't realloc."""
    def __init__(self, n: int):
        self.dist = [INF] * n
        self.prev = [-1] * n
        self.dirty: list[int] = []

    def reset(self) -> None:
        for i in self.dirty:
            self.dist[i] = INF
            self.prev[i] = -1
        self.dirty.clear()


def dijkstra(wg: WalkGraph, source: int, max_seconds: int, scratch: _Scratch):
    """Bounded single-source shortest paths over the CSR walk graph, using
    **Dial's algorithm** (a bucket queue) instead of a binary heap.

    Edge weights are positive integers (walk-seconds) and every settled distance
    is <= ``max_seconds``, so we bucket frontier nodes by distance and sweep the
    bucket index upward. Each push/pop is O(1) array work — no log-n heap and no
    571k heappush/heappop calls per access query — while producing exactly the
    same shortest paths. Stale bucket entries (a node later improved) are skipped
    on pop via the usual ``d > dist[u]`` guard.

    Iterates each node's edge slice as packed CSR integer arrays (no tuple
    unpack). Builds CSR on first use if the graph predates it (old pickles)."""
    if wg.csr_off is None:
        wg.build_csr()
    scratch.reset()
    dist, prev, dirty = scratch.dist, scratch.prev, scratch.dirty
    off, tgt, wt = wg.csr_off, wg.csr_tgt, wg.csr_wt
    buckets: list[list[int]] = [[] for _ in range(max_seconds + 1)]
    dist[source] = 0
    dirty.append(source)
    buckets[0].append(source)
    d = 0
    while d <= max_seconds:
        bucket = buckets[d]
        if not bucket:
            d += 1
            continue
        u = bucket.pop()
        if d > dist[u]:                 # stale entry (u already settled shorter)
            continue
        base = off[u]
        for k in range(base, off[u + 1]):
            nd = d + wt[k]
            if nd <= max_seconds:
                v = tgt[k]
                if nd < dist[v]:
                    if dist[v] == INF:
                        dirty.append(v)
                    dist[v] = nd
                    prev[v] = u
                    buckets[nd].append(v)
    return scratch


import math

# planar hex grid (pointy-top) in a local equirectangular projection around
# Montreal. Good enough at city scale and dependency-free (H3-style look).
_LAT0 = 45.5
_SX = 111320 * math.cos(math.radians(_LAT0))   # metres per degree lon
_SY = 110540                                    # metres per degree lat
_SQRT3 = math.sqrt(3)
HEX_M = 140                                     # hex "size" (centre→corner), ~242 m across


def _hex_key(lon: float, lat: float, s: float) -> tuple[int, int]:
    mx, my = lon * _SX, lat * _SY
    q = (_SQRT3 / 3 * mx - 1 / 3 * my) / s
    r = (2 / 3 * my) / s
    x, z = q, r
    y = -x - z
    rx, ry, rz = round(x), round(y), round(z)
    dx, dy, dz = abs(rx - x), abs(ry - y), abs(rz - z)
    if dx > dy and dx > dz:
        rx = -ry - rz
    elif dy > dz:
        ry = -rx - rz
    else:
        rz = -rx - ry
    return (rx, rz)


def _hex_polygon(q: int, r: int, s: float) -> list[list[float]]:
    cx = s * _SQRT3 * (q + r / 2)
    cy = s * 1.5 * r
    pts = []
    for i in range(6):
        a = math.radians(60 * i - 30)
        px, py = cx + s * math.cos(a), cy + s * math.sin(a)
        pts.append([round(px / _SX, 6), round(py / _SY, 6)])
    pts.append(pts[0])
    return pts


def build_hex_graph(wg: WalkGraph, hex_m: float = HEX_M) -> list:
    """Precompute the hex-adjacency graph: two hexes are connected iff a walk
    edge joins nodes in them. Egress then runs over ~15k hexes instead of ~1.3M
    raw nodes — the single biggest fog speedup. Returns node->hexkey (transient,
    used by the build script to set stop_hex); stores wg.hex_adj.
    """
    lon, lat, adj = wg.node_lon, wg.node_lat, wg.adj
    node_hex = [_hex_key(lon[i], lat[i], hex_m) for i in range(wg.n_nodes)]
    conn: dict[tuple, set] = {}
    for u in range(wg.n_nodes):
        hu = node_hex[u]
        nbrs = conn.get(hu)
        if nbrs is None:
            nbrs = conn[hu] = set()
        for v, _w in adj[u]:
            hv = node_hex[v]
            if hv != hu:
                nbrs.add(hv)
    wg.hex_adj = {h: list(s) for h, s in conn.items()}
    return node_hex


def egress_hex_graph(wg: WalkGraph, seeds: list[tuple], cutoff: int,
                     departure_sec: int, hex_m: float = HEX_M, seen: set | None = None):
    """Fog egress as a Dijkstra over the precomputed hex graph. Each hex step is
    a uniform ~sqrt(3)·hex_m walk, so this is essentially a weighted hex flood,
    settling hexes earliest-first (streamed near -> far).

    seeds: (hexkey, arrival_sec). Yields (travel_seconds, hex_polygon).
    """
    step = int(_SQRT3 * hex_m / WALK_SPEED_MPS)     # ~174 s between hex centres
    if seen is None:
        seen = set()
    dist: dict[tuple, int] = {}
    heap = []
    for h, t in seeds:
        if h is not None and t < dist.get(h, INF):
            dist[h] = t
            heap.append((t, h))
    heapq.heapify(heap)
    hex_adj = wg.hex_adj
    pop, push = heapq.heappop, heapq.heappush
    while heap:
        d, h = pop(heap)
        if d > dist[h]:
            continue
        if h not in seen:
            seen.add(h)
            yield (d - departure_sec, h)                # (q, r) key; client builds the polygon
        nd = d + step
        if nd > cutoff:
            continue
        for nb in hex_adj.get(h, ()):
            if nd < dist.get(nb, INF):
                dist[nb] = nd
                push(heap, (nd, nb))


def egress_hex_disc(disc_stops: list[tuple[float, float, int]], cutoff: int,
                    departure_sec: int, hex_m: float = 140, seen: set | None = None,
                    max_egress_m: float = 700):
    """Low-resolution suburban fog: straight-line egress discs around off-graph
    (suburban) reached stops, binned to the same hex grid. A cheap stand-in for
    the street-network egress where the island walk graph doesn't reach.

    disc_stops: (lon, lat, arrival_sec). Yields (travel_seconds, hex_polygon).
    """
    if seen is None:
        seen = set()
    out: dict[tuple[int, int], int] = {}
    speed = WALK_SPEED_MPS
    budget = cutoff - departure_sec
    width = _SQRT3 * hex_m                       # hex centre spacing
    for lon0, lat0, arr in disc_stops:
        remaining = cutoff - arr
        if remaining <= 0:
            continue
        radius = min(max_egress_m, remaining * speed)
        mx0, my0 = lon0 * _SX, lat0 * _SY
        q0, r0 = _hex_key(lon0, lat0, hex_m)
        rings = int(radius / width) + 1
        for dq in range(-rings, rings + 1):       # enumerate hexes within range once
            for dr in range(max(-rings, -dq - rings), min(rings, -dq + rings) + 1):
                q, r = q0 + dq, r0 + dr
                cx = hex_m * _SQRT3 * (q + r / 2)
                cy = hex_m * 1.5 * r
                d = math.hypot(cx - mx0, cy - my0)
                if d > radius:
                    continue
                travel = int(arr - departure_sec + d / speed)
                if travel <= budget and travel < out.get((q, r), 1 << 30):
                    out[(q, r)] = travel
    for key, travel in out.items():
        if key in seen:
            continue
        seen.add(key)
        yield (travel, key)                              # (q, r) key; client builds the polygon


def path_coords(wg: WalkGraph, prev: list[int], source: int, target: int) -> list[list[float]]:
    """Reconstruct [[lon,lat],...] from source to target using a prev array."""
    if target != source and prev[target] < 0:
        return []
    nodes = []
    u = target
    while u != -1:
        nodes.append(u)
        if u == source:
            break
        u = prev[u]
    nodes.reverse()
    return [[wg.node_lon[n], wg.node_lat[n]] for n in nodes]
