"""Download the walkable OSM network for the island via the Overpass API.

Tiled over the bbox to keep each query small, cached to data/raw/osm_walk.json.
Free: Overpass is open infrastructure (be polite — this runs once).

Usage:  python scripts/download_osm.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import OSM_BBOX, OSM_EXCLUDE_HIGHWAY, OSM_RAW_FILE, OSM_TILES, OVERPASS_URL  # noqa: E402


def query_tile(s: float, w: float, n: float, e: float) -> list[dict]:
    excl = "|".join(sorted(OSM_EXCLUDE_HIGHWAY))
    q = (
        f"[out:json][timeout:180];"
        f'way["highway"]["highway"!~"^({excl})$"]'
        f'["foot"!~"^(no|private)$"]["access"!~"^(private|no)$"]'
        f"({s},{w},{n},{e});"
        f"out geom;"
    )
    data = urllib.parse.urlencode({"data": q}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=data, headers={"User-Agent": "mtl-isochrone/0.1"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=200) as resp:
                return json.load(resp).get("elements", [])
        except Exception as ex:  # noqa: BLE001 - transient Overpass load / rate limits
            wait = 20 * (attempt + 1)
            print(f"    retry in {wait}s ({ex})")
            time.sleep(wait)
    raise RuntimeError("Overpass query failed after retries")


def main() -> None:
    s0, w0, n0, e0 = OSM_BBOX
    dlat = (n0 - s0) / OSM_TILES
    dlon = (e0 - w0) / OSM_TILES
    ways: dict[int, dict] = {}
    print(f"Fetching walk network in {OSM_TILES}x{OSM_TILES} tiles...")
    for i in range(OSM_TILES):
        for j in range(OSM_TILES):
            s = s0 + i * dlat
            w = w0 + j * dlon
            els = query_tile(s, w, s + dlat, w + dlon)
            new = 0
            for el in els:
                if el.get("type") == "way" and el["id"] not in ways:
                    ways[el["id"]] = el
                    new += 1
            print(f"  tile ({i},{j}): +{new} ways  (total {len(ways)})")
            time.sleep(1)
    OSM_RAW_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OSM_RAW_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ways.values()), f)
    print(f"Wrote {OSM_RAW_FILE}  ({OSM_RAW_FILE.stat().st_size/1e6:.1f} MB, {len(ways)} ways)")


if __name__ == "__main__":
    main()
