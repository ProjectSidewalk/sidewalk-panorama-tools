# Photometa census: the tilt prior, pano survival, and metadata drift, sampled live

**2026-08-09** · Phase 1 desk study for the cropper work package (#54, #32) · the last input to
the [pre-registration](2026-08-09-crop-priors-prereg.md)

> **Reproduce offline:** `pytest tests/test_photometa_census.py` (sampling determinism, field
> extraction, accounting) — the census itself is a live measurement against Google's photometa
> endpoint and will drift as panos die and metadata refreshes:
> ```bash
> python reports/scripts/fetch_rawlabels.py
> python reports/scripts/photometa_census.py reports/scripts/.cache/rawlabels \
>     --fetched <rawlabels-date> --per-stratum 85 --interval 0.25 \
>     --write reports/data/<date>-photometa-census.json
> ```
> The committed JSON embeds every per-pano record (sample manifest included), so the exact
> sample can be re-fetched later to measure decay.

## The question

Three quantities no desk source carries, needed before Phase 2 downloads and the Phase 3
analyses: (1) the **camera tilt distribution** — the effect-size scale for #54's tilt
regression; (2) the **alive-rate** of labeled panos by label era — how much of any study
corpus will simply be gone; (3) **dims drift** — how often Google now serves a labeled pano at
different dimensions than the label's stored frame (the #77 preflight's hit rate), plus depth
coverage for validator-ai-adjacent uses.

## Method

One streetlevel `find_panorama_by_id(download_depth=True)` per pano — the identical call path
production's depth phase uses — over a seed-fixed stratified sample: 85 panos per (city ×
era-of-earliest-label) stratum, 16 populated strata, **1,360 panos**, paced at 0.25 s.
Tilt values are Google's own camera pose (radians on the wire, degrees here).

## Numbers

**651 of 1,360 sampled panos are still served by Google (47.9%), and survival is strongly
era-graded:**

| era of earliest label | alive |
|---|---|
| legacy (< 2021) | **33.2%** |
| mid (2021 → evo 179) | 45.5% |
| post-179 | 60.0% |

(3 request errors, counted separately.) Among the alive panos:

* **Dims drift: 0.0%** — every alive pano serves exactly the dimensions rawLabels stores for it
  (542 at 16384×8192, 109 at 13312×6656, none smaller). The server's `gsv_data` tracks Google
  faithfully *for panos that still exist*; the frame-changed rows the
  [era replay study](2026-08-09-era-replay-study.md) caught are a historical population whose
  panos have since died or been reconciled. The #77 dims preflight guards a real but now-rare
  case.
* **Depth: 100.0%** of alive panos return a depth map.
* **Camera tilt** (wrapped to (−180°, 180°] — see wrong turn below):

| | p50 | p90 | p99 |
|---|---|---|---|
| \|pitch\| | 0.63° | 2.60° | 6.85° |
| \|roll\| | 0.90° | 2.16° | 4.26° |

The tilt term the projection ignores, T = pitch·cos(Δb) + roll·sin(Δb), has spread
**sd ≈ 1.56°** across random bearings — 3–5× the click-noise σ.

## What this hands the studies

* **The #54 effect-size scale**: if the missing tilt term leaks fully into pano_y (β = 1),
  a tenth of panos carry ≥ 2° of placement error — an order of magnitude over the consumer
  threshold. With T's sd of 1.56° and n ≈ 650, SE(β) ≈ 0.015: β = 1 vs β = 0 are ~65 SE apart;
  power is not a constraint for the tilt regression.
* **Phase 2 must over-draw its strata**: at 33–60% survival, hitting cell targets from Google
  alone needs ~1.7–3× draws — and labels on dead panos (52%!) can only be studied from the
  production pano store, which is now the sole source of that imagery. The corpus spec in the
  pre-registration sources dead-pano pixels from the store for exactly this reason.
* **Depth availability is not a constraint** for validator-ai-adjacent uses on alive panos.

## Wrong turns

* The first summary reported \|roll\| p90 = **359.6°** — Google serves roll in [0, 360), so
  359.6 is a −0.4° tilt, and the summary was taking magnitudes without wrapping. Fixed
  (`_wrap_deg`), regression-pinned, and the committed JSON's summary was regenerated offline
  from its own embedded records (`--resummarize`), no refetch.

## Where everything lives

| Artifact | Path |
|---|---|
| Census + per-pano records (committed) | `reports/data/2026-08-09-photometa-census.json` |
| Analysis | `reports/scripts/photometa_census.py` |
| Tests | `tests/test_photometa_census.py` |

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-fable-5).*
