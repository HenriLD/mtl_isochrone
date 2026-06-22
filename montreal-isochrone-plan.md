# Montreal Multi-Modal Isochrone Map — Project Plan

## What we're building

An interactive web app where you pick a starting point on a map of Montreal, choose which transport modes to combine (bus, metro, REM, walking, biking), set a time budget (e.g. 30 min) and a departure time, and see a colored isochrone showing everywhere reachable within that budget. Bike routing is bike-lane-aware, so the reachable area realistically follows the protected-lane network.

---

## Locked decisions

- **Routing engine**: custom, from scratch (not OpenTripPlanner), using modern transit-routing techniques rather than naive time-expanded Dijkstra.
- **Core algorithm**: range-RAPTOR / isochrone variant — label every stop with earliest arrival time within the budget, rather than point-to-point search.
- **Format**: interactive web app — map, point picker, mode toggles, time-of-day slider, live isochrone rendering.
- **Modes**: bus + metro + REM (transit), plus walking and biking (continuous-time), combinable at will.
- **Scope (geographic)**: Island of Montreal core (STM + REM). Off-island (STL/RTL) is a possible later extension, not v1.

---

## Open decisions (resolve before or during build)

1. **Exact-schedule vs. frequency-based**: do isochrones reflect a *specific* departure time (e.g. "leaving 8:05am Tuesday," precise but jagged) or *typical* frequency-based reachability (smoother, cheaper, less precise)? Recommendation: start exact-schedule, it's the honest answer to "where can I go."
2. **Bike model**: full BIXI dock integration (walk → grab a bike → ride → dock → walk) vs. simpler "assume you have a bike with you" (bike from anywhere). Recommendation: start with own-bike model; BIXI docks are a great v2.
3. **Max bike-leg cap**: do we cap biking per leg (e.g. ≤15 min) to avoid degenerate "just bike the whole way" paths, or let it run free? Worth a config knob.
4. **Isochrone geometry**: discrete colored stop markers (simple, ships fast) vs. smooth polygons via alpha shapes / concave hull (prettier, more geometry work). Recommendation: markers first, polygons as a fast-follow.

---

## Architecture

Five components, roughly in dependency order:

### 1. GTFS ingestion → RAPTOR data structures
Parse STM + REM GTFS static feeds into the in-memory structures RAPTOR needs:
- `stops` (id, lat, lon)
- `routes` → ordered list of `trips`
- `stop_times` grouped and sorted by route, then by stop sequence
- `transfers` (seeded here, computed in step 2)

The key RAPTOR data layout is "for each route, the trips sorted by departure, and the stops in sequence order" — this is what makes round-based scanning fast. Worth getting this representation right early.

### 2. OSM walk/bike graph → transfer matrix
- Pull an OSM extract for the island.
- Build a routable graph for walking (~5 km/h constant) and biking (two speeds: faster on Montreal bike-path network from the city open-data geojson, slower on regular cycleable streets).
- Precompute shortest walk/bike times between nearby stops (spatial radius pruning — no transfers beyond a sane distance) → a sparse transfer/transfer-time matrix consumed by RAPTOR.
- Optimization: contraction hierarchies (or at minimum a k-d tree spatial index + bounded Dijkstra) so this is fast and cacheable. Precompute once, cache to disk.

### 3. Range-RAPTOR isochrone engine
- Input: origin (snapped to nearby stops via walk/bike access legs), departure time, time budget, enabled modes.
- Output: earliest-arrival-time label for every reachable stop.
- Round k = reachable with ≤ k transfers; iterate until budget exhausted.
- Walk/bike legs enter as access legs (origin → first stops), transfer legs (stop → stop, from the step-2 matrix), and egress legs (last stop → final reachable area).

### 4. Geometry / isochrone layer
- Turn labeled stops + their reachable radius (remaining-time × mode speed around each stop) into a reachability surface.
- v1: colored markers / buffered circles per stop, colored by arrival time.
- v2: alpha shape / concave hull → smooth time-banded polygons.

### 5. Frontend
- Map (MapLibre/Leaflet) with click-to-set origin.
- Mode toggles (bus / metro / REM / walk / bike).
- Time budget control + departure-time slider.
- Render isochrone as colored bands; ideally a legend and travel-time gradient.

---

## Modern optimizations to fold in (priority order)

1. **Transfer pruning by geography** (sparse transfer matrix) — biggest bang for the buck, do it from day one.
2. **Spatial index (k-d tree / grid)** for stop lookup and frontier expansion.
3. **Contraction hierarchies** on the OSM graph — makes walk/bike legs near-instant at query time. Precompute + cache.
4. **Range-RAPTOR profile expansion** rather than re-running per departure time.
5. (Optional) **Frequency-based mode** as a cheaper alternative path for "typical" results.

---

## Suggested build phases (each one demoable)

- **Phase 1**: GTFS ingestion + range-RAPTOR for **transit only** (bus + metro + REM). Output: colored stop markers on a map. *This is the milestone where the idea becomes real and visible.*
- **Phase 2**: Add walking — access/egress/transfer legs via OSM walk graph. Reachable area grows and smooths.
- **Phase 3**: Add biking — bike-lane-aware speeds, the distinctive feature. Add mode toggles in the UI.
- **Phase 4**: Smooth isochrone polygons (alpha shapes) replacing markers.
- **Phase 5**: Polish — departure-time slider, BIXI docks, off-island agencies, performance tuning.

---

## Tech stack suggestions (open to preference)

- **Backend / engine**: Python (fast to write, great GTFS/OSM libs, fine for a project at city scale) or a typed/compiled language if you want query latency to be very low. RAPTOR itself is simple enough to port later if Python becomes the bottleneck.
- **OSM tooling**: a standard OSM extract + a graph lib for the walk/bike network; the city bike-path geojson layered on top for lane awareness.
- **Frontend**: MapLibre GL or Leaflet; a thin API between the engine and the map.

---

## Data sources

- **STM GTFS static + GTFS-RT**: schedule, stops, routes, shapes.
- **REM GTFS**: now that the line is operational.
- **Montreal open data portal**: bike-path network geojson (the lane-awareness layer).
- **OpenStreetMap**: walk + cycleable street network for the island.
- **BIXI GBFS** (v2): live dock/station data if BIXI integration is added.

---

## Note for the Claude Code session

Hand this whole doc in as context, then start with **Phase 1 only** — ask it to set up the repo, the GTFS ingestion pipeline, and a transit-only range-RAPTOR producing labeled stops, with a minimal map frontend to view results. Resist building all phases at once; each phase has a clean demoable output and real complexity. Confirm the open decisions above (especially exact-schedule vs. frequency, and the bike model) before Phase 3.
