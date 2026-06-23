"""Curate the harvested candidate pool down to ~150 side quests.

Scores candidates (Wikidata notability + image + website), then greedily selects
with type quotas and per-neighbourhood caps so the result is spread across the
region (the "discover neighbourhoods" bias). Builds bilingual blurbs grounded in
the Wikidata description where available (templated factual fallback otherwise —
no invented facts), assigns a per-type dwell time, and writes web/side_quests.json.
Images are downloaded later by fetch_quest_images.py.

Run:  python scripts/curate_quests.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# A POI named after a person often carries the PERSON's wikidata tag in OSM, so its
# description/image/wikipedia are the person's bio/portrait, not the place. Detect
# those (occupation words EN/FR, or birth–death years) and drop the enrichment so
# the place falls back to a correct templated blurb. "sculpture/statue/monument/
# memorial" are intentionally NOT here — those are legit artworks.
_PERSON = re.compile(
    r"\b(1[5-9]\d\d|20\d\d)\s*[–\-]\s*(1[5-9]\d\d|20\d\d)\b"
    r"|\b(activist|politician|writer|author|novelist|painter|poet|friar|missionary|"
    r"priest|saint|bishop|singer|actor|actress|composer|general|soldier|explorer|"
    r"founder|businessman|philanthropist|scientist|physician|lawyer|nun|monk|mayor|"
    r"premier|minister|journalist|architect|engineer|economist|historian|"
    r"militant|femme politique|homme politique|écrivain|écrivaine|peintre|poète|"
    r"prêtre|missionnaire|religieu|frère|évêque|chanteu|acteur|actrice|explorateur|"
    r"fondateu|médecin|avocat|journaliste)\b", re.IGNORECASE)


def _looks_like_person(c) -> bool:
    d = (c.get("desc_en") or "") + " | " + (c.get("desc_fr") or "")
    return bool(d.strip(" |")) and bool(_PERSON.search(d))

ROOT = Path(__file__).resolve().parent.parent
CAND = ROOT / "data" / "processed" / "quest_candidates.json"
OUT = ROOT / "web" / "side_quests.json"

# region area centroids -> nearest gives each POI a neighbourhood (display + binning)
AREAS = {
    "Ville-Marie": (45.508, -73.564), "Le Plateau-Mont-Royal": (45.527, -73.585),
    "Mile End": (45.523, -73.601), "Rosemont–La Petite-Patrie": (45.545, -73.585),
    "Villeray": (45.545, -73.620), "Ahuntsic-Cartierville": (45.555, -73.660),
    "Hochelaga-Maisonneuve": (45.545, -73.545), "Mercier": (45.585, -73.520),
    "Anjou": (45.605, -73.555), "Saint-Léonard": (45.585, -73.595),
    "Montréal-Nord": (45.600, -73.635), "Rivière-des-Prairies": (45.640, -73.510),
    "Pointe-aux-Trembles": (45.660, -73.500), "Le Sud-Ouest": (45.480, -73.585),
    "Verdun / Île-des-Sœurs": (45.458, -73.570), "LaSalle": (45.430, -73.620),
    "Lachine": (45.435, -73.675), "Côte-des-Neiges–NDG": (45.475, -73.625),
    "Outremont": (45.515, -73.610), "Westmount": (45.485, -73.600),
    "Côte-Saint-Luc": (45.470, -73.665), "Saint-Laurent": (45.500, -73.700),
    "Pierrefonds-Roxboro": (45.490, -73.860), "Dollard-des-Ormeaux": (45.495, -73.810),
    "Pointe-Claire": (45.450, -73.815), "Dorval": (45.450, -73.745),
    "Beaconsfield / Kirkland": (45.435, -73.860), "L'Île-Bizard": (45.495, -73.890),
    "Laval (Chomedey)": (45.545, -73.750), "Laval-des-Rapides": (45.555, -73.710),
    "Sainte-Rose (Laval)": (45.610, -73.785), "Sainte-Dorothée (Laval)": (45.530, -73.810),
    "Vimont (Laval)": (45.620, -73.720), "Vieux-Longueuil": (45.535, -73.510),
    "Greenfield Park": (45.495, -73.470), "Saint-Hubert": (45.490, -73.420),
    "Brossard": (45.460, -73.465), "Saint-Lambert": (45.500, -73.510),
    "Boucherville": (45.595, -73.435), "Saint-Bruno": (45.535, -73.350),
    "Châteauguay": (45.380, -73.750),
}

DWELL = {"eatery": 75, "park": 75, "neighborhood": 120, "viewpoint": 25,
         "market": 50, "landmark": 75, "historic": 45, "art": 75}
QUOTA = {"park": 40, "eatery": 25, "neighborhood": 18, "landmark": 22,
         "art": 15, "historic": 14, "viewpoint": 8, "market": 8}
PER_AREA = 6            # max quests per neighbourhood (spread out)
PER_AREA_TYPE = 2       # max per (neighbourhood, type)


def nearest_area(lat, lon):
    best, bd = None, 1e9
    for name, (alat, alon) in AREAS.items():
        d = (lat - alat) ** 2 + (lon - alon) ** 2
        if d < bd:
            best, bd = name, d
    return best


def score(c):
    s = c.get("sitelinks", 0) * 2.0
    if c.get("image"):
        s += 5
    if c.get("website"):
        s += 2
    if c.get("desc_fr") or c.get("desc_en"):
        s += 2
    # parks/eateries without any notability are weak; keep but de-rank
    if c["type"] in ("park", "eatery") and not c.get("wikidata") and not c.get("website"):
        s -= 3
    return s


# experiential types read better with an inviting template than with the dry
# Wikidata category ("urban park in Montreal"); informational types keep the desc.
_EXPERIENTIAL = {"park", "neighborhood", "eatery", "viewpoint", "market"}


def blurb(c, area):
    df, de = c.get("desc_fr"), c.get("desc_en")
    cap = lambda s: s[:1].upper() + s[1:] if s else s
    if (df or de) and c["type"] not in _EXPERIENTIAL:
        return cap(df or de), cap(de or df)
    t, cui = c["type"], (c.get("cuisine") or "").split(";")[0].replace("_", " ")
    name = c["name"]
    T = {
        "eatery": (f"{cui.capitalize()}, {area}." if cui else f"Une table de quartier, {area}.",
                   f"{cui.capitalize()} in {area}." if cui else f"A neighbourhood table in {area}."),
        "park": (f"Un espace vert où souffler un après-midi, dans {area}.",
                 f"Green space to slow down for an afternoon, in {area}."),
        "neighborhood": (f"Flânez dans {name} : ruelles, cafés et trouvailles au hasard.",
                         f"Go wander {name}: lanes, cafés and happy accidents."),
        "viewpoint": (f"Un point de vue sur la ville, à {area}.",
                      f"A lookout over the city, in {area}."),
        "market": (f"Un marché à arpenter à {area}.", f"A market to browse in {area}."),
        "landmark": (f"Un arrêt qui vaut le détour à {area}.",
                     f"A stop worth the detour in {area}."),
        "historic": (f"Un morceau d'histoire à {area}.", f"A slice of history in {area}."),
        "art": (f"Pour les yeux : art et galeries à {area}.",
                f"For the eyes: art around {area}."),
    }
    return T.get(t, (f"À découvrir à {area}.", f"Worth discovering in {area}."))


def main():
    cands = json.loads(CAND.read_text(encoding="utf-8"))

    # drop person-bio enrichment (place named after someone -> OSM points at the
    # person): blank desc/image/wikipedia and the wikidata link so it's treated as
    # an unenriched OSM place with a correct templated blurb.
    persons = 0
    for c in cands:
        if _looks_like_person(c):
            c["desc_en"] = c["desc_fr"] = c["image"] = c["wikipedia"] = None
            c["wikidata"] = None
            persons += 1
    print(f"Neutralised {persons} person-named entries")

    # dedupe: by wikidata id (keep best), then by (name, ~coords)
    best_by_qid: dict[str, dict] = {}
    rest = []
    for c in cands:
        q = c.get("wikidata")
        if q:
            if q not in best_by_qid or score(c) > score(best_by_qid[q]):
                best_by_qid[q] = c
        else:
            rest.append(c)
    seen = set()
    pool = list(best_by_qid.values())
    for c in rest:
        key = (c["name"].lower(), round(c["lat"], 3), round(c["lon"], 3))
        if key in seen:
            continue
        seen.add(key)
        pool.append(c)

    for c in pool:
        c["_area"] = nearest_area(c["lat"], c["lon"])
        c["_score"] = score(c)
    pool.sort(key=lambda c: -c["_score"])

    chosen, type_n, area_n, area_type_n = [], {}, {}, {}
    for c in pool:
        t, a = c["type"], c["_area"]
        if type_n.get(t, 0) >= QUOTA.get(t, 0):
            continue
        if area_n.get(a, 0) >= PER_AREA:
            continue
        if area_type_n.get((a, t), 0) >= PER_AREA_TYPE:
            continue
        chosen.append(c)
        type_n[t] = type_n.get(t, 0) + 1
        area_n[a] = area_n.get(a, 0) + 1
        area_type_n[(a, t)] = area_type_n.get((a, t), 0) + 1

    out = []
    for i, c in enumerate(sorted(chosen, key=lambda c: (c["_area"], c["type"]))):
        bf, be = blurb(c, c["_area"])
        out.append({
            "id": f"sq-{i:03d}",
            "name": c["name"],
            "type": c["type"],
            "lat": c["lat"], "lon": c["lon"],
            "neighborhood": c["_area"],
            "avg_dwell_min": DWELL.get(c["type"], 60),
            "blurb_fr": bf, "blurb_en": be,
            "website": c.get("website"),
            "wikipedia": c.get("wikipedia"),
            "image": c.get("image"),         # remote Commons url; localised by fetch_quest_images.py
            "attribution": None,
            "source": (f"wikidata:{c['wikidata']}" if c.get("wikidata") else f"osm:{c['osm']}"),
        })

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Selected {len(out)} quests -> {OUT}")
    print("By type:", dict(sorted(type_n.items(), key=lambda x: -x[1])))
    print("Neighbourhoods covered:", len(area_n), "/", len(AREAS))
    print("With image:", sum(1 for o in out if o["image"]),
          "| with website:", sum(1 for o in out if o["website"]),
          "| with wikipedia:", sum(1 for o in out if o["wikipedia"]))


if __name__ == "__main__":
    main()
