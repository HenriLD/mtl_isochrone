"""Cross-check the RAPTOR engine against an independent CSA implementation.

Runs many random (origin, departure-time) queries through both engines over the
same network and compares earliest-arrival labels at every stop. They should
match exactly; any difference is a bug.

Usage:  python scripts/validate.py [n_samples] [budget_min]
"""
from __future__ import annotations

import pickle
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import NETWORK_FILE, WALK_GRAPH_FILE  # noqa: E402
from engine.raptor import compute_isochrone  # noqa: E402
from engine.reference_csa import build_connections, csa_isochrone  # noqa: E402


def main() -> None:
    n_samples = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    budget_min = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    budget = budget_min * 60

    net = pickle.load(open(NETWORK_FILE, "rb"))
    walk = pickle.load(open(WALK_GRAPH_FILE, "rb")) if WALK_GRAPH_FILE.exists() else None
    print(f"Network: {net.n_stops} stops | walk graph: {'yes' if walk else 'no'} | "
          f"{n_samples} samples @ {budget_min} min")
    print("Building CSA connections...")
    conns = build_connections(net)
    print(f"  {len(conns[0])} connections")

    rng = random.Random(42)
    total_stops = total_mismatch = raptor_worse = raptor_better = 0
    max_abs = 0
    examples = []

    for i in range(n_samples):
        s = rng.randrange(net.n_stops)            # random origin at an existing stop
        lat, lon = net.stop_lat[s], net.stop_lon[s]
        dep = rng.randrange(6 * 3600, 22 * 3600)  # random departure 06:00–22:00

        rap = {r.stop_index: r.arrival for r in
               compute_isochrone(net, lat, lon, dep, budget, walk_graph=walk).stops}
        ref = csa_isochrone(net, conns, lat, lon, dep, budget, walk_graph=walk)

        keys = set(rap) | set(ref)
        for k in keys:
            a = rap.get(k, INF)
            b = ref.get(k, INF)
            total_stops += 1
            if a != b:
                total_mismatch += 1
                d = abs(a - b)
                max_abs = max(max_abs, d)
                if a > b:
                    raptor_worse += 1   # RAPTOR slower/missed -> potential gap
                else:
                    raptor_better += 1  # RAPTOR faster than optimal -> would be a real bug
                if len(examples) < 8:
                    examples.append((i, k, a, b))
        print(f"  sample {i+1}/{n_samples}: origin '{net.stop_name[s][:24]}' "
              f"dep {dep//3600:02d}:{dep%3600//60:02d} | "
              f"RAPTOR {len(rap)} vs CSA {len(ref)} reached")

    print("\n=== results ===")
    print(f"stop-labels compared: {total_stops}")
    print(f"mismatches:           {total_mismatch}  "
          f"({100*total_mismatch/max(total_stops,1):.4f}%)")
    print(f"  RAPTOR later than CSA (missed/slower): {raptor_worse}")
    print(f"  RAPTOR earlier than CSA (UNSOUND bug): {raptor_better}")
    print(f"max abs arrival diff: {max_abs} s")
    if examples:
        print("examples (sample, stop, raptor_arr, csa_arr):")
        for ex in examples:
            print(f"  {ex}")
    ok = raptor_better == 0 and total_mismatch == 0
    print("\nRESULT:", "PASS — engines agree exactly" if ok else
          ("PASS (sound; RAPTOR never beats optimal)" if raptor_better == 0 else "FAIL — RAPTOR unsound"))


INF = 10 ** 12
if __name__ == "__main__":
    main()
