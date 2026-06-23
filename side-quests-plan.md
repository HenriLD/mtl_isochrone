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
  "lat": 45.49, "lon": -73.89,
  "neighborhood": "L'Île-Bizard",
  "vibe": "nature",            // nature | views | food | history | art | wander
  "blurb": "Boardwalks over a marsh full of turtles and herons...",
  "time_est_min": 120,
  "image": "images/quests/mtl-001.jpg",   // local thumbnail (LFS) or null
  "attribution": "Photo: <author> / CC BY-SA 4.0 (Wikimedia Commons)",
  "source": "wikidata:Q123"               // or osm:way/123
}
```
Only `lat/lon` are needed at runtime (client maps to hex → travel); everything
else is presentation.

## Pipeline (next session)

1. **Harvest** — `scripts/harvest_quests.py` (cache raw responses like
   `metro_overpass.json` so re-runs are offline):
   - **Overpass** over the region bbox: `tourism` in {attraction, viewpoint,
     museum, gallery, artwork, zoo, theme_park}, `leisure` in {park,
     nature_reserve, garden}, `natural` in {beach, peak}, `historic=*`,
     `amenity=marketplace`. Keep name + coords + tags.
   - **Wikidata SPARQL**: items with coordinates in the Montréal area, an image
     (`P18`), and an en/fr Wikipedia sitelink. Pull label, description, image,
     sitelink count (notability signal).
2. **Merge + score** — match OSM↔Wikidata by proximity + name; score =
   notability (sitelinks) + tag quality + has-image.
3. **Geo-diversify** — bin by borough/neighbourhood (a boroughs GeoJSON, or a
   coarse grid), cap ~6–10 per bin, up-weight sparse outer areas → ~300
   candidates spread across the region.
4. **Editorial pass (Claude)** — write side-quest blurbs, assign `vibe`,
   `time_est_min`, prune to ~150, ensure outer-neighbourhood coverage. User
   reviews / tweaks.
5. **Images** — `scripts/fetch_quest_images.py`: download the source image,
   thumbnail to ~400 px JPEG into `web/images/quests/` (LFS), record attribution;
   `null` where no free image.

## Runtime (frontend, `web/`)

- Load `side_quests.json` once.
- On each isochrone result: per quest, `travel` = fog travel of its hex (fallback:
  nearest reached stop + straight-line walk). Reachable iff `travel <= budget`.
- **Suggestion ranking**: reachable AND in the outer band (≈ 0.6–1.0 × budget);
  score = w1·(travel/budget) [farther = better] + w2·novelty (distance from
  origin / different neighbourhood) + w3·quality. Show top ~5 as cards (image,
  name, neighbourhood, "~X min", blurb, vibe icon) + an "Another" reshuffle.
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
