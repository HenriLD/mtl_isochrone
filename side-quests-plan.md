# Side Quests — plan (for next session)

A curated set of ~150 "side quest" places across the Montréal region. Drop an
origin and the app suggests a few worth-visiting spots **biased toward the far
edge of what's reachable** (discover new areas), each with an image, a blurb, a
vibe, a time estimate, and "minutes from here".

## Why it's cheap (the key insight)

Every isochrone query already labels the whole region with travel time (the fog
hexes carry per-hex `travel`; reached stops carry arrivals). So a place's
"minutes from here" is a **client-side lookup** — place → its hex → `travel` — with
no new server work and no per-place routing. The feature decomposes into:

- **"Discover neighbourhoods"** = a *curation-time* property → bake geographic
  diversity into the list (spread across boroughs, don't cluster downtown).
- **"Bias to the extremities"** = a *runtime ranking* → from the current origin,
  favour reachable places in the **outer band** of the budget. Same list, different
  far-flung suggestions per origin.

Net: static JSON + thumbnails + a small frontend panel + client-side ranking.
**~$0 added server cost.**

## Decisions locked (this session)

- **Curation = hybrid**: auto-harvest (OSM + Wikidata) → score → geo-diversify →
  editorial pass (Claude writes blurbs/vibes, prunes to ~150) → user review.
- **Images = thumbnail + store**: fetch once at curation time, resize ~400 px
  (~10–15 MB total), store via **Git LFS** (already set up) or the `/data` bucket,
  serve static from the Space. Per-image attribution; Commons (CC/PD) only.
- **Scope = whole reachable region**: island + Laval + south/north shore (matches
  the network extent and the discovery goal).

## Data model — `data/side_quests.json` (committed, ~150 KB)

```json
{
  "id": "mtl-001",
  "name": "Parc-nature du Bois-de-l'Île-Bizard",
  "type": "park",              // eatery | park | neighborhood | landmark | viewpoint | market | historic | art
  "lat": 45.49, "lon": -73.89, // a neighborhood quest uses a representative CENTRE point
  "neighborhood": "L'Île-Bizard",
  "avg_dwell_min": 120,        // typical time spent ON SITE — drives the "how long is this side quest" estimate
  "blurb": "Boardwalks over a marsh full of turtles and herons...",
  "website": "https://www.pamm.qc.ca/...",  // official site (OSM website/url/contact:website, else Wikidata P856); null if none
  "wikipedia": "https://fr.wikipedia.org/wiki/...",  // Wikidata sitelink, fr preferred; null if none
  // a Google Maps link is BUILT AT RUNTIME from name+coords (hours/reviews/directions) — not stored
  "image": "images/quests/mtl-001.jpg",   // local thumbnail (LFS) or null
  "attribution": "Photo: <author> / CC BY-SA 4.0 (Wikimedia Commons)",
  "source": "wikidata:Q123"               // or osm:way/123 / osm:relation/123
}
```
Only `lat/lon` are needed at runtime (client maps to hex → travel); everything
else is presentation.

### Links / "more info"

Each card offers a "more info" link, prioritised **official `website` → `wikipedia`**,
plus an **always-present Google Maps** link built client-side from name + coords
(`https://www.google.com/maps/search/?api=1&query=<name>%20<lat>,<lon>`) — the
reliable one for hours, reviews, photos and directions, and the most useful for
eateries (which often have a Maps presence but no website). So the JSON only
stores `website`/`wikipedia` (nullable); Maps needs no harvesting.

### Place types & dwell time

The three first-class types are **eateries, parks, and neighbourhoods**, plus
landmark / viewpoint / market / historic / art from the harvest. Each carries an
`avg_dwell_min` (typical on-site time) — set a per-type default in the harvest and
let the editorial pass tune outliers (a pocket park vs a nature reserve):

| type | what | default dwell |
|---|---|---:|
| `eatery` | a worth-a-trip café / restaurant / bar (named, non-chain) | ~60 min |
| `park` | green space, from pocket parks to nature reserves | ~45–150 min |
| `neighborhood` | "go wander X" — an area, not a point (centre + longer dwell) | ~120 min |
| `viewpoint` | a lookout / belvédère | ~25 min |
| `landmark` / `market` / `historic` / `art` | attraction / market / site / murals | ~45–90 min |

**Side-quest duration** is then computed at runtime: `≈ 2 × travel + avg_dwell_min`
(round-trip + time there). That's what lets a card say *"~45 min each way · ~2 h
there · ~3 h round trip"* and lets us offer "I have ~N hours" as a filter.

## Pipeline (next session)

1. **Harvest** — `scripts/harvest_quests.py` (cache raw responses like
   `metro_overpass.json` so re-runs are offline):
   - **Attractions/parks (Overpass)**: `tourism` in {attraction, viewpoint,
     museum, gallery, artwork, zoo, theme_park}, `leisure` in {park,
     nature_reserve, garden}, `natural` in {beach, peak}, `historic=*`,
     `amenity=marketplace`. Keep name + coords + tags → type park/viewpoint/etc.
   - **Eateries (Overpass)**: `amenity` in {restaurant, cafe, bar, pub,
     ice_cream} — **named, non-chain** (exclude a chain-name blocklist). OSM has
     no ratings, so harvest wide and let the editorial pass + known food lists do
     the "worth it" filter; this is the type that most needs human/Claude judgment.
   - **Neighbourhoods (Overpass + GeoJSON)**: `place` in {neighbourhood, suburb,
     quarter} and/or a Montréal boroughs/quartiers GeoJSON → each a "go wander X"
     quest with a centre point and a longer dwell.
   - **Wikidata SPARQL**: items with coordinates in the Montréal area, an image
     (`P18`), and an en/fr Wikipedia sitelink. Pull label, description, image,
     sitelink count (notability signal), official site (`P856`), and the fr/en
     Wikipedia URL — main image + notability + link source.
   - **Links from OSM**: `website` / `url` / `contact:website` tags → `website`
     (prefer OSM's, else Wikidata `P856`). The Google Maps link is constructed at
     runtime, not harvested.
2. **Merge + score** — match OSM↔Wikidata by proximity + name; score =
   notability (sitelinks) + tag quality + has-image.
3. **Geo-diversify** — bin by borough/neighbourhood (a boroughs GeoJSON, or a
   coarse grid), cap ~6–10 per bin, up-weight sparse outer areas → ~300
   candidates spread across the region.
4. **Editorial pass (Claude)** — write side-quest blurbs, assign `type` and tune
   `avg_dwell_min`, prune to ~150 (esp. the eateries, where "worth it" is
   editorial), ensure a mix of types and outer-neighbourhood coverage. User
   reviews / tweaks.
5. **Images** — `scripts/fetch_quest_images.py`: download the source image,
   thumbnail to ~400 px JPEG into `web/images/quests/` (LFS), record attribution;
   `null` where no free image.

## Runtime (frontend, `web/`)

- Load `side_quests.json` once.
- On each isochrone result: per quest, `travel` = fog travel of its hex (fallback:
  nearest reached stop + straight-line walk). Reachable iff `travel <= budget`.
- **Duration estimate** per quest: `≈ 2 × travel + avg_dwell_min` (round-trip +
  time there). Drives the card text and an optional "I have ~N hours" filter.
- **Suggestion ranking**: reachable AND in the outer band (≈ 0.6–1.0 × budget);
  score = w1·(travel/budget) [farther = better] + w2·novelty (distance from
  origin / different neighbourhood) + w3·quality. Aim for a spread of types and
  durations (a quick park *and* a far neighbourhood). Show top ~5 as cards (image,
  name, neighbourhood, type icon, "~45 min each way · ~3 h round trip", blurb,
  and a "more info" link — official site/Wikipedia + a Google Maps link) + an
  "Another" reshuffle.
- v2: click a card → highlight the route there (the spine already has geometry to
  the nearest stop).
- Hosting: ranking + cards are client-side; images static. $0.

## Open questions to settle next session

- **Bilingual blurbs?** The UI is FR/EN — write blurbs in both (FR primary) and
  pick by `lang`, or FR-only to start.
- Borough binning source (boroughs GeoJSON vs coarse grid).
- Card count / "Another" UX; whether to also list *all* reachable quests.
- Confirm Commons-only image licensing (skip non-free); store attribution shown in
  the card.

## Effort

Harvest+merge+diversify ~half session; editorial pass batched by Claude; images
automated; frontend panel + ranking ~half session. ≈ 1–2 sessions total.
