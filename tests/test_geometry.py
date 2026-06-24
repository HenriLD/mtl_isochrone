"""Geometry / build helpers: bus-shape smoothing drift bound, simplification,
stop projection monotonicity, GTFS time parsing."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.gtfs import _simplify, smooth_shape  # noqa: E402
from engine.model import parse_gtfs_time, project_stops_to_shape  # noqa: E402

_SX = 111320 * math.cos(math.radians(45.5))
_SY = 110540


def _pt_seg_m(p, a, b):
    px, py = (p[0] - a[0]) * _SX, (p[1] - a[1]) * _SY
    dx, dy = (b[0] - a[0]) * _SX, (b[1] - a[1]) * _SY
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, (px * dx + py * dy) / seg2))
    return math.hypot(px - t * dx, py - t * dy)


def _drift_m(orig, smoothed):                 # max distance from an original vertex to the smoothed line
    return max(min(_pt_seg_m(p, smoothed[k], smoothed[k + 1]) for k in range(len(smoothed) - 1))
               for p in orig)


def _zigzag():                                # long (~280 m) segments with 90° corners
    pts = []
    for i in range(9):
        lon = -73.57 + i * 0.0026
        lat = 45.50 + (0.0018 if i % 2 else 0.0)
        pts.append([round(lon, 5), round(lat, 5)])
    return pts


class TestGeometry(unittest.TestCase):
    def test_smooth_drift_is_capped(self):
        z = _zigzag()
        sm = smooth_shape(z, iters=2, cap_m=7.0, tol_m=3.0)
        # capped corner-cutting keeps the line on the road; uncapped would drift ~50 m
        self.assertLess(_drift_m(z, sm), 12.0)

    def test_smooth_preserves_endpoints(self):
        z = _zigzag()
        sm = smooth_shape(z)
        self.assertAlmostEqual(sm[0][0], z[0][0], places=5)
        self.assertAlmostEqual(sm[0][1], z[0][1], places=5)
        self.assertAlmostEqual(sm[-1][0], z[-1][0], places=5)
        self.assertAlmostEqual(sm[-1][1], z[-1][1], places=5)

    def test_simplify_drops_collinear(self):
        line = [[-73.57, 45.50], [-73.56, 45.51], [-73.55, 45.52], [-73.54, 45.53]]
        self.assertEqual(len(_simplify(line, tol_m=2.0)), 2)

    def test_project_stops_strictly_increasing(self):
        shape = [[-73.57, 45.50], [-73.56, 45.50], [-73.55, 45.50], [-73.54, 45.50]]
        stops = [(-73.57, 45.501), (-73.558, 45.501), (-73.545, 45.501)]
        idx = project_stops_to_shape(stops, shape)
        self.assertEqual(idx, sorted(idx))
        self.assertTrue(all(idx[i] < idx[i + 1] for i in range(len(idx) - 1)))

    def test_parse_gtfs_time(self):
        self.assertEqual(parse_gtfs_time("00:00:00"), 0)
        self.assertEqual(parse_gtfs_time("08:00:00"), 28800)
        self.assertEqual(parse_gtfs_time("25:10:05"), 90605)   # GTFS hours may exceed 24


if __name__ == "__main__":
    unittest.main()
