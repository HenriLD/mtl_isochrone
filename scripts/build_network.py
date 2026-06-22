"""Compile the downloaded GTFS feed(s) into a serialized RAPTOR network.

Usage:  python scripts/build_network.py
Output: data/processed/network.pkl  (consumed by the API server / engine)
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DATA_PROCESSED, NETWORK_FILE, SERVICE_DATE  # noqa: E402
from engine.gtfs import build_network  # noqa: E402
from scripts.apply_metro_geometry import apply_metro_geometry  # noqa: E402


def main() -> None:
    t0 = time.time()
    net = build_network(target_date=SERVICE_DATE)
    # swap the coarse GTFS metro shapes for accurate OSM geometry (no-op if the
    # geometry file is missing — fetch it with scripts/fetch_metro_geometry.py)
    apply_metro_geometry(net)
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    with open(NETWORK_FILE, "wb") as f:
        pickle.dump(net, f, protocol=pickle.HIGHEST_PROTOCOL)
    size_mb = NETWORK_FILE.stat().st_size / 1e6
    print(f"Wrote {NETWORK_FILE}  ({size_mb:.1f} MB)  in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
