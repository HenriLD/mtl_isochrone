"""Download the configured GTFS feeds into data/raw/.

Usage:  python scripts/download_gtfs.py
Re-running re-downloads (feeds update regularly). Free: these are open data.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DATA_RAW, GTFS_FEEDS  # noqa: E402


def download(feed: dict) -> Path:
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    dest = DATA_RAW / f"gtfs_{feed['id']}.zip"
    print(f"  {feed['id']}: {feed['url']}")
    req = urllib.request.Request(feed["url"], headers={"User-Agent": "mtl-isochrone/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length", 0))
        read = 0
        while chunk := resp.read(1 << 16):
            f.write(chunk)
            read += len(chunk)
            if total:
                pct = 100 * read / total
                print(f"\r    {read/1e6:6.1f} / {total/1e6:.1f} MB ({pct:4.1f}%)", end="")
        print()
    print(f"    -> {dest}  ({dest.stat().st_size/1e6:.1f} MB)")
    return dest


def main() -> None:
    print(f"Downloading {len(GTFS_FEEDS)} GTFS feed(s)...")
    for feed in GTFS_FEEDS:
        download(feed)
    print("Done.")


if __name__ == "__main__":
    main()
