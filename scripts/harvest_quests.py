"""Harvest side-quest POI candidates for the Montréal region.

Spatial source = OpenStreetMap (Overpass); enrichment (description / image /
notability / official site) = Wikidata, via the `wikidata` tags OSM objects
already carry. Output: data/processed/quest_candidates.json — a deduped candidate
pool with everything the curation step needs. Raw responses are cached so re-runs
are offline.

Run:  python scripts/harvest_quests.py
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RAW = ROOT / "data" / "raw"
PROC = ROOT / "data" / "processed"
OVERPASS_CACHE = RAW / "quests_overpass.json"
WIKIDATA_CACHE = RAW / "quests_wikidata.json"
OUT = PROC / "quest_candidates.json"

# region: island + Laval + south/north shore (matches the reachable extent)
BBOX = (45.35, -74.05, 45.72, -73.28)        # s, w, n, e

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]
UA = "mtl-isochrone-sidequests/1.0 (henrildu20@gmail.com)"

# OSM tag -> our coarse type. Order matters (first match wins in _classify).
_TOUR = "attraction|viewpoint|museum|gallery|artwork|zoo|theme_park|aquarium"
_LEIS = "park|nature_reserve|garden"

QUERY = f"""
[out:json][timeout:180];
(
  nwr["tourism"~"^({_TOUR})$"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
  nwr["leisure"~"^({_LEIS})$"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
  nwr["natural"~"^(beach|peak)$"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
  nwr["historic"]["name"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
  nwr["amenity"="marketplace"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
  nwr["amenity"~"^(restaurant|cafe|bar)$"]["website"]["name"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
  node["place"~"^(neighbourhood|suburb|quarter|borough)$"]["name"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
);
out tags center;
"""

# chain blocklist for eateries (case-insensitive substring) — keep the list short;
# the curation pass prunes the rest
CHAINS = {"tim hortons", "mcdonald", "starbucks", "subway", "a&w", "burger king",
          "kfc", "pizza hut", "domino", "second cup", "presse caf", "valentine",
          "st-hubert", "st hubert", "scores", "normandin", "harvey", "dairy queen",
          "wendy", "popeyes", "five guys", "couche-tard", "boustan", "basha",
          "thai express", "sushi shop", "pita pit", "ben & jerry", "cora"}


def _http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 180) -> str:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_overpass() -> dict:
    if OVERPASS_CACHE.exists():
        print(f"Using cached Overpass: {OVERPASS_CACHE.name}")
        return json.loads(OVERPASS_CACHE.read_text(encoding="utf-8"))
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    for url in OVERPASS_MIRRORS:
        try:
            print(f"Overpass: {url} ...")
            txt = _http(url, data=body, timeout=240)
            js = json.loads(txt)
            OVERPASS_CACHE.write_text(json.dumps(js), encoding="utf-8")
            print(f"  {len(js.get('elements', []))} elements")
            return js
        except Exception as e:
            print(f"  failed: {e}")
            time.sleep(2)
    raise SystemExit("All Overpass mirrors failed.")


def _classify(tags: dict) -> str | None:
    t = tags.get("tourism"); l = tags.get("leisure"); n = tags.get("natural")
    a = tags.get("amenity"); h = tags.get("historic"); p = tags.get("place")
    if a in ("restaurant", "cafe", "bar"):
        return "eatery"
    if l in ("park", "nature_reserve", "garden") or n == "beach":
        return "park"
    if p in ("neighbourhood", "suburb", "quarter", "borough"):
        return "neighborhood"
    if t == "viewpoint" or n == "peak":
        return "viewpoint"
    if a == "marketplace":
        return "market"
    if t in ("museum", "gallery", "artwork", "zoo", "theme_park", "aquarium"):
        return "art" if t in ("gallery", "artwork") else "landmark"
    if h:
        return "historic"
    if t == "attraction":
        return "landmark"
    return None


def _website(tags: dict) -> str | None:
    for k in ("website", "contact:website", "url"):
        v = tags.get(k)
        if v and v.startswith("http"):
            return v.split(";")[0].strip()
    return None


def parse_elements(js: dict) -> list[dict]:
    out = []
    for el in js.get("elements", []):
        tags = el.get("tags") or {}
        name = tags.get("name:fr") or tags.get("name") or tags.get("name:en")
        if not name:
            continue
        typ = _classify(tags)
        if typ is None:
            continue
        if typ == "eatery" and any(c in name.lower() for c in CHAINS):
            continue
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        out.append({
            "name": name,
            "name_en": tags.get("name:en"),
            "type": typ,
            "lat": round(lat, 6), "lon": round(lon, 6),
            "website": _website(tags),
            "wikidata": tags.get("wikidata"),
            "osm": f"{el['type']}/{el['id']}",
            "cuisine": tags.get("cuisine"),
        })
    return out


def fetch_wikidata(qids: list[str]) -> dict:
    cache = json.loads(WIKIDATA_CACHE.read_text(encoding="utf-8")) if WIKIDATA_CACHE.exists() else {}
    todo = [q for q in qids if q not in cache]
    print(f"Wikidata: {len(qids)} ids, {len(todo)} to fetch")
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        params = urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(batch), "format": "json",
            "props": "descriptions|claims|sitelinks", "languages": "en|fr",
        })
        try:
            js = json.loads(_http(f"https://www.wikidata.org/w/api.php?{params}", timeout=60))
        except Exception as e:
            print(f"  batch {i} failed: {e}"); time.sleep(3); continue
        for qid, ent in (js.get("entities") or {}).items():
            desc = ent.get("descriptions") or {}
            claims = ent.get("claims") or {}
            sitelinks = ent.get("sitelinks") or {}

            def claim(pid):
                try:
                    return claims[pid][0]["mainsnak"]["datavalue"]["value"]
                except Exception:
                    return None
            image = claim("P18")
            img_url = None
            if isinstance(image, str):
                fn = image.replace(" ", "_")
                img_url = "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(fn) + "?width=600"
            wiki = None
            for site in ("frwiki", "enwiki"):
                if site in sitelinks:
                    title = sitelinks[site]["title"].replace(" ", "_")
                    lang = "fr" if site == "frwiki" else "en"
                    wiki = f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(title)
                    break
            p856 = claim("P856")
            cache[qid] = {
                "desc_en": (desc.get("en") or {}).get("value"),
                "desc_fr": (desc.get("fr") or {}).get("value"),
                "image": img_url,
                "wikipedia": wiki,
                "sitelinks": len(sitelinks),
                "p856": p856 if isinstance(p856, str) else None,
            }
        print(f"  fetched {i + len(batch)}/{len(todo)}")
        time.sleep(1)
    WIKIDATA_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return cache


def main() -> None:
    PROC.mkdir(parents=True, exist_ok=True)
    cands = parse_elements(fetch_overpass())
    print(f"Parsed {len(cands)} candidates with name+type+coords")

    qids = sorted({c["wikidata"] for c in cands if c["wikidata"]})
    wd = fetch_wikidata(qids)
    for c in cands:
        e = wd.get(c["wikidata"] or "", {})
        c["desc_en"] = e.get("desc_en")
        c["desc_fr"] = e.get("desc_fr")
        c["image"] = e.get("image")
        c["wikipedia"] = e.get("wikipedia")
        c["sitelinks"] = e.get("sitelinks", 0)
        if not c["website"]:
            c["website"] = e.get("p856")

    by_type: dict[str, int] = {}
    for c in cands:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    print("By type:", dict(sorted(by_type.items(), key=lambda x: -x[1])))
    print("With image:", sum(1 for c in cands if c.get("image")),
          "| with wikidata:", sum(1 for c in cands if c["wikidata"]),
          "| with website:", sum(1 for c in cands if c["website"]))
    OUT.write_text(json.dumps(cands, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"Wrote {OUT} ({len(cands)} candidates)")


if __name__ == "__main__":
    main()
