"""Fetch accurate Montréal metro line geometry from OpenStreetMap (Overpass).

The STM GTFS shapes for the metro are extremely coarse (≈1 vertex per station →
straight lines between stops), so the rendered lines look angular. OSM maps the
actual tunnel alignment as `route=subway` relations; we stitch each line's member
ways into one ordered polyline and save them by line ref.

Output: data/raw/metro_geometry.json  ->  { "1": [[lon,lat], ...], ... }
Run:    python scripts/fetch_metro_geometry.py
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw" / "metro_geometry.json"
RAW_CACHE = ROOT / "data" / "raw" / "metro_overpass.json"

# a few public mirrors — the main instance is often overloaded (504s)
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]
# Montréal metro is entirely STM; grab every subway route relation in the region.
QUERY = """
[out:json][timeout:120];
relation["route"="subway"](45.30,-74.10,45.80,-73.20);
out geom;
"""

WANT = {"1", "2", "4", "5"}     # green, orange, yellow, blue


def _m(a, b):  # approx metres between two [lon,lat] near Montréal
    return math.hypot((a[0] - b[0]) * 78000, (a[1] - b[1]) * 111000)


def _wlen(w):
    return sum(_m(w[k - 1], w[k]) for k in range(1, len(w)))


def _stitch(ways: list[list[list[float]]]) -> list[list[float]]:
    """Assemble member ways into the line's REVENUE polyline = the longest
    terminus-to-terminus path through the way network. Member order isn't
    geographic, and `route=subway` relations often include non-revenue garage /
    crossover spurs that loop back near the line (these mis-snap stops and bloat
    the geometry). Building the longest leaf-to-leaf path drops those spurs.

    Ways are joined at endpoints clustered within JOIN_M; we DFS the longest
    (by metres) simple path from every leaf cluster and keep the best."""
    JOIN_M = 60
    ways = [list(w) for w in ways if len(w) >= 2]
    if not ways:
        return []
    if len(ways) == 1:
        return ways[0]

    # cluster way endpoints into nodes (union endpoints within JOIN_M)
    eps = [(i, 0, w[0]) for i, w in enumerate(ways)] + [(i, 1, w[-1]) for i, w in enumerate(ways)]
    parent = list(range(len(eps)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(eps)):
        for b in range(a + 1, len(eps)):
            if _m(eps[a][2], eps[b][2]) <= JOIN_M:
                parent[find(a)] = find(b)

    # node id per (way, end); adjacency node -> list of (way_index, end)
    node_of = {}
    for k, (i, end, _) in enumerate(eps):
        node_of[(i, end)] = find(k)
    adj: dict[int, list[tuple[int, int]]] = {}
    for (i, end), node in node_of.items():
        adj.setdefault(node, []).append((i, end))

    lens = [_wlen(w) for w in ways]
    leaves = [n for n, lst in adj.items() if len(lst) == 1] or list(adj)

    best = {"len": -1.0, "path": []}

    def dfs(node, used, path, total):
        if total > best["len"]:
            best["len"], best["path"] = total, list(path)
        for (i, end) in adj.get(node, []):
            if i in used:
                continue
            other = node_of[(i, 1 - end)]
            used.add(i)
            path.append((i, end == 1))           # flip if we entered at the way's far end
            dfs(other, used, path, total + lens[i])
            path.pop()
            used.discard(i)

    for leaf in leaves:
        dfs(leaf, set(), [], 0.0)

    chain: list[list[float]] = []
    for (i, flip) in best["path"]:
        seg = list(reversed(ways[i])) if flip else list(ways[i])
        if chain and _m(chain[-1], seg[0]) < 1:
            seg = seg[1:]
        chain.extend(seg)
    return chain


def main() -> None:
    # Cache the raw Overpass response so the stitching can be re-run offline
    # (Overpass is flaky). Pass --refresh to force a new query.
    if RAW_CACHE.exists() and "--refresh" not in sys.argv:
        print(f"Using cached Overpass response {RAW_CACHE.name} (pass --refresh to re-query).")
        data = json.loads(RAW_CACHE.read_text(encoding="utf-8"))
    else:
        print("Querying Overpass for Montréal subway relations…")
        payload = ("data=" + QUERY).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "User-Agent": "mtl-isochrone/1.0 (metro geometry fetch)"}
        data = None
        for attempt in range(6):
            url = OVERPASS_MIRRORS[attempt % len(OVERPASS_MIRRORS)]
            try:
                req = urllib.request.Request(url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.load(resp)
                print(f"  ok via {url}")
                break
            except Exception as ex:  # noqa: BLE001 — transient Overpass load / rate limits
                print(f"  {url} failed ({ex}); retrying…")
                time.sleep(3)
        if data is None:
            sys.exit("All Overpass mirrors failed.")
        RAW_CACHE.write_text(json.dumps(data), encoding="utf-8")

    # group member-way geometries per line ref; keep the relation with the most ways
    by_ref: dict[str, list[list[list[float]]]] = {}
    for el in data.get("elements", []):
        if el.get("type") != "relation":
            continue
        tags = el.get("tags", {})
        ref = (tags.get("ref") or "").strip()
        if ref not in WANT:
            continue
        ways = []
        for m in el.get("members", []):
            if m.get("type") != "way" or "geometry" not in m:
                continue
            ways.append([[round(p["lon"], 6), round(p["lat"], 6)] for p in m["geometry"]])
        if not ways:
            continue
        # prefer the richest relation for this ref (one direction is enough)
        if ref not in by_ref or sum(len(w) for w in ways) > sum(len(w) for w in by_ref[ref]):
            by_ref[ref] = ways

    out: dict[str, list[list[float]]] = {}
    for ref, ways in by_ref.items():
        line = _stitch(ways)
        out[ref] = line
        max_gap = max((_m(line[i - 1], line[i]) for i in range(1, len(line))), default=0)
        flag = "  <-- CHECK: large gap" if max_gap > 250 else ""
        print(f"  line {ref}: {len(ways)} ways -> {len(line)} points, max gap {max_gap:.0f} m{flag}")

    missing = WANT - set(out)
    if missing:
        print(f"WARNING: no geometry for line(s): {sorted(missing)}", file=sys.stderr)
    if not out:
        sys.exit("No metro geometry fetched — aborting.")

    OUT.write_text(json.dumps(out), encoding="utf-8")
    print(f"Wrote {OUT}  ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
