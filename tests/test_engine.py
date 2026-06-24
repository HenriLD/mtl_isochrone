"""Engine correctness — the crown jewel. RAPTOR vs the independent CSA oracle on a
synthetic network, plus hand-computed known-answers and edge cases."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.raptor import compute_isochrone  # noqa: E402
from engine.reference_csa import build_connections, csa_isochrone  # noqa: E402
from tests.fixtures import DEP, ORIGIN, tiny_net  # noqa: E402


def _raptor(net, budget, modes=None):
    res = compute_isochrone(net, ORIGIN[0], ORIGIN[1], DEP, budget, allowed_modes=modes, walk_graph=None)
    return {r.stop_index: r.arrival for r in res.stops}


def _csa(net, conns, budget, modes=None):
    return csa_isochrone(net, conns, ORIGIN[0], ORIGIN[1], DEP, budget, allowed_modes=modes, walk_graph=None)


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.net = tiny_net()
        self.conns = build_connections(self.net)

    def test_known_answers(self):
        # A(access) -> ride R1 to B,C -> transfer C->D -> ride R2 to E
        self.assertEqual(_raptor(self.net, 5400),
                         {0: 28800, 1: 29100, 2: 29400, 3: 29550, 4: 30000})

    def test_raptor_equals_csa(self):
        for budget in (600, 1200, 1800, 3600, 5400):
            self.assertEqual(_raptor(self.net, budget), _csa(self.net, self.conns, budget),
                             msg=f"divergence at budget {budget}")

    def test_raptor_equals_csa_metro_only(self):
        modes = {"metro"}
        self.assertEqual(_raptor(self.net, 5400, modes),
                         _csa(self.net, self.conns, 5400, modes))

    def test_mode_filter_excludes_bus_leg(self):
        # metro-only: E (only via the R2 bus) is unreachable; D (via footpath) still is
        reached = _raptor(self.net, 5400, {"metro"})
        self.assertIn(3, reached)
        self.assertNotIn(4, reached)

    def test_zero_budget_only_origin(self):
        self.assertEqual(set(_raptor(self.net, 0)), {0})

    def test_departure_after_all_trips(self):
        # depart 08:31, after R1's last trip leaves A (08:20): nothing boardable
        res = compute_isochrone(self.net, ORIGIN[0], ORIGIN[1], 30660, 3600, walk_graph=None)
        self.assertEqual({r.stop_index for r in res.stops}, {0})

    def test_transfer_propagates(self):
        # D is reachable ONLY via the C->D footpath after riding R1 — guards the
        # footpath-seeding path that historically had bugs.
        self.assertEqual(_raptor(self.net, 5400)[3], 29550)


if __name__ == "__main__":
    unittest.main()
