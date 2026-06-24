"""Tiny hand-built fixtures so CI can exercise the engine without the real
(git-ignored, ~90 MB) pickles. Geometry is laid out so the far stops are NOT
walk-reachable from the origin — forcing transit, which keeps known-answers clean.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.model import Network, Route  # noqa: E402
from engine.walk import WalkGraph  # noqa: E402

# stops: A near origin; B,C,D,E placed >3.75 km away so straight-line access can't
# reach them (only transit can). Transit times come from the schedule, not geometry.
_LAT = [45.50, 45.55, 45.60, 45.60, 45.62]
_LON = [-73.57, -73.57, -73.57, -73.568, -73.568]
ORIGIN = (45.50, -73.57)            # at stop A
DEP = 28800                         # 08:00


def tiny_net() -> Network:
    net = Network()
    net.stop_ids = ["A", "B", "C", "D", "E"]
    net.stop_name = list(net.stop_ids)
    net.stop_lat = list(_LAT)
    net.stop_lon = list(_LON)
    net.stop_index = {s: i for i, s in enumerate(net.stop_ids)}
    # R1 metro A->B->C, three trips at 08:00 / 08:10 / 08:20 (5 min between stops)
    r1 = Route(
        stops=[0, 1, 2],
        arr=[[28800, 29100, 29400], [29400, 29700, 30000], [30000, 30300, 30600]],
        dep=[[28800, 29100, 29400], [29400, 29700, 30000], [30000, 30300, 30600]],
        route_id="m1", route_type=1, route_color="00B300", route_name="1")
    # R2 bus D->E, trips at 08:15 / 08:25
    r2 = Route(
        stops=[3, 4],
        arr=[[29700, 30000], [30300, 30600]],
        dep=[[29700, 30000], [30300, 30600]],
        route_id="b1", route_type=3, route_color="", route_name="100")
    net.routes = [r1, r2]
    net.stop_routes = [[(0, 0)], [(0, 1)], [(0, 2)], [(1, 0)], [(1, 1)]]
    net.transfers = [[], [], [(3, 150.0)], [(2, 150.0)], []]   # C <-> D footpath, 150 s
    net.shapes = {}
    net.service_date = "20260101"
    net.feeds = ["test"]
    return net


def tiny_walk() -> WalkGraph:
    """4-node graph: 0-1-2-3 chain plus a 1-3 shortcut, for CSR / Dijkstra tests."""
    wg = WalkGraph()
    wg.node_lon = [-73.570, -73.569, -73.568, -73.567]
    wg.node_lat = [45.500, 45.501, 45.502, 45.503]
    wg.adj = [
        [(1, 10)],
        [(0, 10), (2, 10), (3, 25)],
        [(1, 10), (3, 10)],
        [(2, 10), (1, 25)],
    ]
    return wg
