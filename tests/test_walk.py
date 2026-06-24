"""Walk graph: CSR build, Dial's bounded Dijkstra, and the hex-key projection
(which the side-quests panel mirrors in JS — see hexkey_cases.json)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.walk import HEX_M, INF, _Scratch, _hex_key, dijkstra  # noqa: E402
from tests.fixtures import tiny_walk  # noqa: E402

CASES = json.loads((Path(__file__).resolve().parent / "hexkey_cases.json").read_text())


class TestWalk(unittest.TestCase):
    def test_dijkstra_shortest_paths(self):
        wg = tiny_walk()
        sc = _Scratch(wg.n_nodes)
        dijkstra(wg, 0, 100, sc)               # builds CSR lazily
        self.assertEqual([sc.dist[i] for i in range(4)], [0, 10, 20, 30])  # 0-1-2-3 beats 0-1-3

    def test_dijkstra_is_bounded(self):
        wg = tiny_walk()
        sc = _Scratch(wg.n_nodes)
        dijkstra(wg, 0, 15, sc)                 # node 2 (dist 20) is beyond the bound
        self.assertEqual(sc.dist[1], 10)
        self.assertEqual(sc.dist[2], INF)

    def test_build_csr_matches_adj(self):
        wg = tiny_walk()
        wg.build_csr()
        for u in range(wg.n_nodes):
            csr = {(wg.csr_tgt[k], wg.csr_wt[k]) for k in range(wg.csr_off[u], wg.csr_off[u + 1])}
            self.assertEqual(csr, set(wg.adj[u]))

    def test_build_csr_idempotent_and_free_adj(self):
        wg = tiny_walk()
        wg.build_csr()
        off = list(wg.csr_off)
        wg.build_csr()                          # no-op
        self.assertEqual(list(wg.csr_off), off)
        wg.free_adj()
        self.assertEqual(wg.adj, [])
        sc = _Scratch(wg.n_nodes)               # still routable on CSR alone
        dijkstra(wg, 0, 100, sc)
        self.assertEqual(sc.dist[3], 30)

    def test_hex_key_deterministic_and_known(self):
        for c in CASES:
            got = _hex_key(c["lon"], c["lat"], HEX_M)
            self.assertEqual(list(got), c["key"])
            self.assertEqual(got, _hex_key(c["lon"], c["lat"], HEX_M))


if __name__ == "__main__":
    unittest.main()
