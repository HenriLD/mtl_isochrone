"""Build the pedestrian walk graph and recompute transit transfers along it.

Inputs:  data/raw/osm_walk.json (from download_osm.py), data/processed/network.pkl
Outputs: data/processed/walk_graph.pkl, and updated transfers in network.pkl

Usage:  python scripts/build_walk_graph.py
"""
from __future__ import annotations

import json
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (  # noqa: E402
    MAX_TRANSFER_WALK_SECONDS, NETWORK_FILE, OSM_RAW_FILE, WALK_GRAPH_FILE,
    WALK_SNAP_MAX_M, WALK_SPEED_MPS,
)
from engine.model import haversine_m  # noqa: E402
from engine.walk import WalkGraph, _Scratch, build_grid, build_hex_graph, dijkstra  # noqa: E402


def build_graph(ways: list[dict]) -> WalkGraph:
    osm_to_idx: dict[int, int] = {}
    lon: list[float] = []
    lat: list[float] = []
    adj_d: list[dict[int, int]] = []   # neighbor -> seconds (dedup, keep min)

    def node(osm_id: int, glon: float, glat: float) -> int:
        idx = osm_to_idx.get(osm_id)
        if idx is None:
            idx = len(lon)
            osm_to_idx[osm_id] = idx
            lon.append(glon)
            lat.append(glat)
            adj_d.append({})
        return idx

    for w in ways:
        ids = w.get("nodes")
        geom = w.get("geometry")
        if not ids or not geom or len(ids) != len(geom):
            continue
        prev_i = None
        prev_g = None
        for oid, g in zip(ids, geom):
            i = node(oid, g["lon"], g["lat"])
            if prev_i is not None and i != prev_i:
                d = haversine_m(prev_g["lat"], prev_g["lon"], g["lat"], g["lon"])
                sec = max(1, int(d / WALK_SPEED_MPS))
                if sec < adj_d[prev_i].get(i, 10 ** 9):
                    adj_d[prev_i][i] = sec
                    adj_d[i][prev_i] = sec
            prev_i, prev_g = i, g

    wg = WalkGraph(node_lon=lon, node_lat=lat,
                   adj=[list(d.items()) for d in adj_d])
    build_grid(wg)
    edges = sum(len(a) for a in wg.adj) // 2
    print(f"  walk graph: {wg.n_nodes} nodes, {edges} edges")
    return wg


def snap_stops(wg: WalkGraph, net, node_hex: list) -> None:
    """Snap each stop to a walk-graph node. Stops farther than WALK_SNAP_MAX_M
    (off-island suburbs the island graph doesn't cover) are marked off-graph
    (stop_node = -1) and fall back to geometric transfers/access. Also records
    each on-graph stop's hex (for fog egress)."""
    wg.stop_node = [-1] * net.n_stops
    wg.stop_hex = [None] * net.n_stops
    wg.node_stops = {}
    off = 0
    for s in range(net.n_stops):
        nd = wg.nearest_node(net.stop_lon[s], net.stop_lat[s])
        if nd is None:
            off += 1
            continue
        d = haversine_m(net.stop_lat[s], net.stop_lon[s], wg.node_lat[nd], wg.node_lon[nd])
        if d > WALK_SNAP_MAX_M:
            off += 1                    # off-graph (suburb) — leave stop_node = -1
            continue
        wg.stop_node[s] = nd
        wg.stop_hex[s] = node_hex[nd]
        wg.node_stops.setdefault(nd, []).append(s)
    print(f"  snapped {net.n_stops} stops; {off} are off-graph (suburbs, geometric fallback)")


def compute_transfers(wg: WalkGraph, net) -> None:
    """Walk-graph transfers for on-graph (island) stops; keep the geometric
    transfers already on net.transfers for off-graph (suburban) stops."""
    geometric = net.transfers
    scratch = _Scratch(wg.n_nodes)
    transfers: list[list[tuple[int, float]]] = [[] for _ in range(net.n_stops)]
    t0 = time.time()
    for s in range(net.n_stops):
        src = wg.stop_node[s]
        if src < 0:
            transfers[s] = list(geometric[s])   # suburb: keep straight-line transfers
            continue
        dijkstra(wg, src, MAX_TRANSFER_WALK_SECONDS, scratch)
        best: dict[int, int] = {}
        for nd in scratch.dirty:
            stops_here = wg.node_stops.get(nd)
            if not stops_here:
                continue
            dsec = scratch.dist[nd]
            for t in stops_here:
                if t != s and dsec < best.get(t, 10 ** 9):
                    best[t] = dsec
        transfers[s] = [(t, float(d)) for t, d in best.items()]
        if s % 4000 == 0:
            print(f"    transfers {s}/{net.n_stops} ({time.time()-t0:.0f}s)")
    net.transfers = transfers
    total = sum(len(t) for t in transfers)
    print(f"  transfers: {total} edges ({total/max(net.n_stops,1):.1f} avg/stop)")


def main() -> None:
    t0 = time.time()
    print("Loading OSM ways...")
    ways = json.load(open(OSM_RAW_FILE, encoding="utf-8"))
    print(f"  {len(ways)} ways")
    wg = build_graph(ways)
    print("  building hex graph...")
    node_hex = build_hex_graph(wg)
    print(f"  hex graph: {len(wg.hex_adj)} hexes")

    net = pickle.load(open(NETWORK_FILE, "rb"))
    snap_stops(wg, net, node_hex)
    compute_transfers(wg, net)

    # the snapping maps live on the graph; don't double-store on both pickles
    with open(WALK_GRAPH_FILE, "wb") as f:
        pickle.dump(wg, f, protocol=pickle.HIGHEST_PROTOCOL)
    with open(NETWORK_FILE, "wb") as f:
        pickle.dump(net, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"Wrote {WALK_GRAPH_FILE} ({WALK_GRAPH_FILE.stat().st_size/1e6:.1f} MB) "
          f"and updated {NETWORK_FILE.name} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
