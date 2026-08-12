# Reports

Write-ups of data-driven investigations: what was measured, how, what came out, and what changed as a
result. One file per investigation, named `YYYY-MM-DD-topic.md`, with:

* `figures/` — plots and diagrams, same date prefix.
* `data/` — the computed results a report cites, small enough to commit and read in a diff.
* `scripts/` — the analysis that produced them, runnable from the repo root. Bulk inputs (multi-MB API
  dumps) are cached under `scripts/.cache/` and gitignored, never committed; the script re-fetches them.

These exist because conclusions about external behaviour — Google's endpoints, imagery quirks, thresholds
calibrated against real data — get re-argued months later when nobody can reproduce the original
measurement. An issue thread is not replicable and does not fail CI when it stops being true.

**Where artifacts live:** everything a report rests on goes in **GitHub or the `projectsidewalk`
Hugging Face org — never personal cloud storage** (Drive/Dropbox/shared links). Those feel accessible
in the moment but don't survive people moving on, and experiments have had to be re-run because of it.
Data too large to commit here goes to a HF dataset, referenced by exact revision from the report; the
repo keeps the manifest and the script that regenerates or re-downloads it.

## What a report should carry

* **The question**, and why it mattered at the time.
* **Method** — exactly how the measurement was taken, and enough detail to repeat it. Say what was swept
  versus sampled; the difference has already burned us once.
* **The numbers**, in tables. Not "roughly 60%" — the counts.
* **Where the data lives.** Every claim should point at committed fixtures or a committed measurement file
  (see `tests/fixtures/tiles/`), not at a paragraph.
* **The tests that pin it**, so the finding fails CI if it stops holding.
* **What changed in the code**, with the commit.
* **Wrong turns.** Explicitly. The reasoning that produced a wrong answer is the part most likely to be
  repeated, and it is the part that never survives into a commit message.
* **Open questions**, so a reader knows what is settled and what isn't.

## Relationship to tests and fixtures

A report is the narrative; the tests and fixtures are the enforcement. Neither replaces the other:

* Raw measurements go in the repo as data (e.g. `tests/fixtures/tiles/fover_band_map.json`), with the metric
  and its rationale written into the file.
* Real captured bytes go in as fixtures with a `manifest.json` recording provenance.
* Assertions over that data go in the normal suite; live re-checks against the external service go behind an
  opt-in env var so CI stays network-free.
* The report links all three together and explains *why* they are shaped that way.

## Index

| Date | Report | Outcome |
|---|---|---|
| 2026-08-07 | [CBK tile resolution and the `fover` parameter](2026-08-07-cbk-tile-resolution.md) | Dropped `fover`; recovered full zoom-5 resolution. Recommends against re-downloading the store. Issues #73, #74; PR #68. |
| 2026-08-09 | [Cropper consumer requirements](2026-08-09-cropper-consumer-requirements.md) | Requirements survey of RampNet 2.0 / validator-ai / tagger-ai / sidewalk-ai-api; sets the pre-registered thresholds for the #54/#32 studies (placement ≤ 0.5°, crop ratio R = 6.7–10× object extent). |
| 2026-08-09 | [Era replay study](2026-08-09-era-replay-study.md) | 438k labels, 6 cities: stored pano_x/y is click-time truth in every era; found + bounded an 18-month client record bug (evo 179 → 7.20.7); pano_x-only drift signature extended to 3 new cities; hands #54/#32 their era/window covariates. |
| 2026-08-09 | [Click-noise floor](2026-08-09-click-noise.md) | 13k co-located duplicate pairs price between-user placement noise: core σ ≈ 0.3°/axis, 0.5° conservative — the floor the #54 study and the pre-registration's power calc budget against. |
| 2026-08-09 | [Clamp census](2026-08-09-clamp-census.md) | Deployed crop sizing on 436k labels: 19% hit the 1500px clamp, edge truncation is exactly zero (and structurally unreachable), and the pixel-linear distance term inflates crops ≥1.2× on the 90% of labels served at 8192px height — the case for a resolution-independent #32 formula. |
| 2026-08-09 | [Photometa census](2026-08-09-photometa-census.md) | 1,360 labeled panos sampled live: 47.9% still served (33% legacy → 60% post-179), 0% dims drift among survivors, depth 100%, and the #54 tilt prior — \|pitch\| p90 2.6°, \|roll\| p90 2.2°, tilt-term sd 1.56°. |
| 2026-08-09 | [Crop priors + pre-registration](2026-08-09-crop-priors-prereg.md) | **Report 1.** Synthesizes the Phase 1 desk studies into binding pre-registered endpoints, decision rules, corpus spec, annotation protocol, and power for Studies 1–3. Registration is the merge; amendments append-only after that (§7 lists the pre-merge revisions). |
| 2026-08-10 | [Crop geometry: what the seam fix reaches, and what the preflights beside it can see](2026-08-10-crop-geometry-review.md) | Seam wrap reaches 1.52% of labels; pano dims are a per-pano join, so the dims preflight guards the store and not the label's frame; out-of-frame `pano_y` now rejected instead of clamped. Issue #47; PR #77. |
| 2026-08-10 | [Backup-store coverage](2026-08-10-store-coverage.md) | The makelab2 pano store (15 TB, 54 cities) holds **99.2%** of the panos Google has dropped, 97.8% even for legacy — so the store, not Google survival, is the Phase 2 pixel source, and the era-graded over-draw is retired. Also the first measurement of our own JPEG against `gsv_data`'s frame: **4.6% disagree**, giving #77's dims preflight a real hit rate. |
| 2026-08-10 | [Pre-merge review of the crop priors](2026-08-10-crop-priors-review.md) | Review of PR #79 before registering Report 1: six defects fixed (a 2× error in Study 2's acceptance band, a report/data mismatch, an unstated tilt-regression n, a gate looser than its own assumption, an unprovisioned robustness column, and 4,916 bare `NaN` tokens in a committed artifact), plus the untested aggregation layer that produced every committed number. |
| 2026-08-10 | [Off-target markers on screen](2026-08-10-off-target-markers-validate.md) | 626k labels, 8 cities (+teaneck, +chicago): 17.40% of in-window records don't reproduce their own pano_x/y — and the record is what Validate renders, so 4–17% of in-window labels sit ≥ 4 px off on screen, dropping to ≤ 0.28% after the one-day 7.20.7 cliff (2024-09-25 in three cities). Decomposes the x misses (58% pure heading staleness; miss groups sharing a stored POV share one dx in 83–93% of cases), repairs **100.00% of all 19,472 misses** from pano_x/y (committed per-label CSVs), and shows 20 legend-annotated before/after examples on real pano imagery. SidewalkWebpage#4842's two example labels are both `exact` — not this bug. |
| 2026-08-11 | [The off-axis covariate](2026-08-11-offaxis-covariate.md) | #4842's example labels replay `exact`, so the residual visual offset splits three ways across two frames — and Study 1 is blind to the render-side one. Registers the click's off-axis offset (**95.08%** of its variation survives the pre-registered band fixed effects) and the hard **−35°** pitch floor (10.18% of labels, 49.2% of the >30° band) as the covariates that tell a capture-side projection error from rig tilt and from placement behaviour. Amends the pre-registration §7. |
| 2026-08-11 | [Mapillary census](2026-08-11-mapillary-census.md) | Richmond, the first Mapillary city: all **267** labels replay **exactly** on both axes, so the GSV projection and fov ladder transfer and `exact_y` is meaningful there. `camera_roll` is served for 100% of Mapillary rows vs **0%** of 438k GSV rows, and the rig is more tilted — so endpoint 2 is better identified here (SE 0.019/0.015) than on the whole GSV corpus, with no survival selection. Short by **17 panos** on §2.3 and by ~144 cross-user pairs. Also yields a **referent-quality** exclusion (Crosswalk, Occlusion, brick/cobblestone SurfaceProblems — 86 of 267 labels have no point to measure a displacement from), and fixes two Mapillary-only loader bugs plus a None-format crash in three merged scripts. |
