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
| 2026-08-12 | [Phase 2a corpus assembly](2026-08-12-corpus-assembly.md) | Draws the gold-standard corpus: **763** GSV labels over 661 panos from 35 cities, all **120 of 120** strata cells at target, plus a separate **97**-label Mapillary arm. Widens the frame from 6 deployments to all **49** GSV ones after measuring that the six are **31.98%** of the population and misdescribe it by **43.16 pp** on era-quality (post-fix is 9.98% of them and **46.34%** of the population); the six already occupy 120/120 cells, so the wider reweighting was licensed either way. Registers Richmond as its own arm - the only place in the project where rig roll is measured (**267/267** rows vs **0** of 1,376,851 GSV). Two silent defects found: `label_id` is not unique across deployments (90,369 of 316,735 rows collide) and cost 314 labels of the draw, and a rescan-based stratum tally overshot 96/60. Amends the pre-registration section 7. No pixels and no annotation yet. |
| 2026-08-13 | [What the cropper is for, and how far this study should go](2026-08-13-cropper-scope.md) | **Scope decision, not a measurement.** The crop is context, not the object (consumers put the object at **10-15%** of the crop side), so crop size is nearly type-independent and only large referents drive it — and the binding consumer for both centering and sizing is RampNet, which is curb ramps. So: fit an excellent **curb-ramp** cropper on a distance-stratified draw and ship it for all types with the caveat stated. Settles the box question (whole object, CV convention — the *point* carries impedance, and keeping it out of the box is what leaves competing size rules testable), rejects a ground-contact span for destroying future detector value, and places the depth + segmentation + intersection pipeline in `sidewalk-auto-labeler`/RampNet 3.0, noting it would largely obsolete a heuristic cropper. Also records the recovered Tohme gold (**2,862** boxes / 741 panos) as a sizing asset that `predict_crop_size` was *fit on*, and that overlaps `sidewalk_dc` by **1 pano**. **Amended 2026-08-19 (#88):** the size half happened in RampNet rather than here and the shipped fix was one normalisation plus a scale constant, so §3/§5 are superseded for curb ramps — and §7's "every consumer is an ML pipeline" holds only until AI-submitted labels ship, since a server-side CropService makes a formula cut the **human-facing** Gallery crop for labels that have no browser canvas capture. The centering half, and this PR's corpus and tooling, are unaffected. |
| 2026-08-19 | [Crop sizing rule v2](2026-08-19-crop-sizing-v2.md) | The first measurement of what a crop should contain, against **658** hand-drawn whole-apron extents in four cities and two providers. `predict_crop_size` fed native pixels into constants fit on 6656-px panos, so the window's ANGLE swung **1.86-4.09x** on pano height alone - and the wrong way, with the largest panoramas getting the tightest crops. Only **10.9%** of v1 crops clear the measured "too tight" threshold (fill 0.49, from a blind absolute-judgement round) and **more than half** of aprons are not inside their own crop (containment **0.471**, measured positionally rather than by size). Rule v2 - normalised, x2.5, clamped 8-90 deg, cut 3:2, stored at min(window, 1440) - reaches **74.8%** clearing and **0.944** containment, and takes stored width from **348** to **873** px at the median. Annapolis is the reported exception at 43%: one global constant under-sizes the city whose ramps subtend the largest angle (**14.93°** against Sao Paulo's 9.67°). Two corrections it made to itself: containment was a size comparison that passed for an apron outside its own crop, and the **4.14x** upsample belongs to ImageController's write path rather than to this cropper, which has never resized - v2 reduces that to **1.65x** rather than removing it. |
| 2026-09-05 | [The `fover` re-fetch pilot](2026-09-05-fover-refetch-pilot.md) | **Do not run the full pass; nothing to recover.** 200 Seattle panos re-fetched against a copy of the store: Google still serves **39.8%**, the pass is clean (0 undersized, 0 frame_grew, 0 failures), but the pre-specified MAE metric reads **-0.279** against its own noise floor and the re-fetched polar band has **0.591×** the high-frequency energy of the stored one (0 of 78 sharper, horizon control 1.000). The committed tile pair explains it: the 512-px polar body CBK serves without `fover` is within **0.232** luma of a bilinear upscale of the 256-px body it served with it — `fover` skipped a server-side upscale, it never cost resolution. Also: Google has **re-rendered 24.4%** of the surviving panos since scrape (same id and frame, different pixels). Closes the #73 action; `refetch_panos.py` stays as a general repair tool. |
| 2026-09-06 | [How fast are we actually losing depth?](2026-09-06-photometa-decay.md) | **The urgency premise for the depth rollout was wrong.** The 2026-08-09 census's own 1,360-pano manifest, re-asked 28 days later from the production IP: **2** of **651** living panoramas died (**0.31%**), **9** previously-dead ones came back, and the living population went **651 -> 658**. So the 47.9% -> 39.8% "slide" was a population difference between the census and the fover pilot's Seattle work-list, not a month of decay - and another month of `--skip-depth` costs ~0.3% of the depth still available, not eight points of it. Both deaths were re-probed and confirmed, because an errored request is recorded `found=False` and would otherwise book a timeout as a retirement. Replicates depth coverage at **100.0%** of alive panos, dims drift at **0.0%**, and the #54 tilt prior to two decimals. Also the rollout's canary: 1,360 paced requests, zero push-back. Adds `--refetch` to `photometa_census.py`. |
| 2026-09-06 | [What did Google's re-render actually change?](2026-09-06-rerender-probe.md) | **Label geometry survives a re-render; the pixels do not.** All 19 pairs the [fover pilot](2026-09-05-fover-refetch-pilot.md) found re-rendered, measured per band against the 59 same-rendering panos as the null — which is clean: horizon MAE **≤ 0.0742** luma, every window locked, **0 px** measured shift, against a floor of **3.3823** on the 19, a **45.6×** gap. **15 of 19** moved, 14 with a camera pose the fit can name (heading ≤ **3.8 px / 0.083°**, vertical offset ≤ **1.4 px / 0.037°**, tilt ≤ **7.2 px / 0.158°**; removing each window's shift takes per-window MAE **11.4 → 6.2**) — the pre-run classifier flags **13**, missing two pure ~1.2 px tilts its median test cannot see. **13** were sharpened **1.6–2.2×** net of gain and re-graded, and three 2025-09 captures had the nadir patch over the car replaced (bottom-band MAE to **48.9**); **four** did not move at all. The largest displacement is **a third of one sigma** of [click noise](2026-08-09-click-noise.md), so no label moves off its referent — but sharpness up 2× is a distribution shift for any crop consumer, so the store keeps the rendering the labels were placed on. Not an age effect (both groups span 2011–2025). Motivates the [archive rule](../docs/ops.md#the-store-is-an-archive-not-a-cache) and a `pose_drift` tracker in the standing decay census. |
| 2026-09-08 | [What the Panoramax catalog actually serves](2026-09-08-panoramax-api.md) | **Pre-integration measurement for the third imagery source (#110), ten days before Bayonne opens.** Three findings the downloader is built on, two of which contradict the issue that asked for it. (1) Absence is a **404** at `/api/pictures/<id>` (`{"status": 404, "message": "Feature not found"}`) and a **200** with **0** features at `/api/search?ids=`, so only the first endpoint can ever found a permanent `downloaded=0` row (#41, #99). (2) A city's bbox is not a fleet: of **1000** pictures listed in Bayonne, **677** are 360 panoramas (`etalab-2.0`, `sig_bayonne`, on `panoramax.ign.fr`) and **323** are flat **92** deg GoPro photographs (`CC-BY-SA-4.0`, `Patchanka`, on `panoramax.openstreetmap.fr`) - hence a projection guard, which the first implementation of got wrong by reading `field_of_view` off the item's top level instead of `properties` and never fired. (3) The item *does* carry the frame's dimensions, in two independent places that agree **677**/**0**/**0**, confirmed against six downloaded JPEG headers (**6** of **6**) - so the cropper's `dims_mismatch` preflight will not skip every Bayonne label. The frames are **5760**x**2880** (**547**) and **5376**x**2688** (**130**), not the 12288-wide professional-rig imagery #110 and SidewalkWebpage#5185 assumed. |
| 2026-09-09 | [The depth backfill's first three nights](2026-09-09-depth-backfill-first-nights.md) | **A fleet average hid a 463-night tail.** Three nights of the production backfill: zero push-back, a median **585.5** requests per `max-runtime` run (the pacer never leaves its ramp), **76,251** of **1,433,938** panos resolved, **13** cities already complete and **477 of 690** window minutes used. The "47 nights" that sized the slots was a fleet average; the fleet finishes when chicago-il does, **463** nights out at the measured rate, **140** with the pacer at its floor, **12** with the window shared among the cities that still have work. Drove #124 (progress in `log.csv` and the analyzer), #125 (the pacer keeps earned speed across runs) and the queue's extra passes. |
| 2026-09-26 | [Swapping the distance estimator: sizing rule v3](2026-09-26-crop-sizing-v3.md) | **Opt-in, default unchanged.** The 2013 linear distance reaches 0 m at **35.15°** of depression and is not distance-shaped; the lle #3 cotangent blend explains more of the aprons' angular width in every city (pooled R² **0.502 → 0.612**, the 2013 line's on the 650 ramps where it is above 0 m, log-log slope **−1.10**, geometry says −1). Rule v3 - the angle a **5.8 m** span subtends at the blend distance, fitted to v2's median fill - lowers fill dispersion (log-sd **0.428 → 0.406**) and raises the window-vs-apron R² (**0.570 → 0.612**, against a monotone ceiling of **0.647**) in every city, but moves **39.8%** of the 658 gold ramps' windows by more than 10%, so it ships behind `--sizing-rule v3` until a re-cut path exists. The minimal alternative (blend distance into v2's power law, option A) is less dispersed pooled and in three of four cities, clears more in all four, and tracks the apron less well in every city: a real trade, left for the default-flip decision. Under v3 Annapolis stays under the 45% floor (**44.3%**); option A clears it (48.9%). |
| 2026-09-26 | [Is the stored `pano_y` in the frame of the stored pixels? The tilt study](2026-09-26-tilt-error-study.md) | **The depth planes are in the rig frame, the tiles are not levelled, and the stored `pano_y` is off towards the rig pixel (#54).** F1: facade normals of 29,824 Seattle depth-artifact planes track the stored pose with slopes 0.9995 / 1.0003, which also fixes the pose sign (streetlevel `pitch > 0` = nose down). F2: every arm's raw vertical-edge lean slope excludes 0, so no arm is gravity-levelled; the calibrated slopes (0.634-1.098) are estimates and their shortfall below 1 is open. C, Jon's blind adjudication of 96 labels vouched for by the lead labellers: the window shifted by the tilt beat its mirror 79 : 0 (p = 1.7e-24). post179 is confirmed at 98% of single-window answers; legacy+mid is at 84%. C gives the direction, not the size (beta); the first draw, with no validation filter, is superseded. Also: the 2019-22 XML files carry legacy tilt (convention fitted on 3,594 panos), PS stores no roll, and a leak at the |T| p90 is 37.8% of a far label's window height. |

## Desk-study conventions — six that keep being rediscovered

Moved here from CLAUDE.md on 2026-09-29. These bit four scripts at once in the 2026-08-11 review; see `reports/2026-08-11-mapillary-census.md`.

- **Undefined is not zero, and `main()` must not format-spec it.** Analysis functions correctly return
  `None` for a percentage with a zero denominator, a sample sd of one value, a correlation against a
  constant series. `f"{None:.1f}"` then raises `TypeError` at the summary print — after all the compute
  and before `--write`. Print through `studyfmt.fmt` and build artifact values with `studyfmt.num`;
  there is one definition of each and no script may grow a local copy (a test asserts that). Writes use
  `allow_nan=False`, so a NaN reaching the dict aborts the run on its last line.
- **`rawlabels` pins `pano_id` to `str`, and must.** Mapillary image ids are all-numeric, so pandas
  infers `int64` for any Mapillary city — the #46 bug class the two runners already pin against. A
  merge between an int-keyed and a str-keyed frame matches nothing and reports zero coverage rather
  than failing.
- **Mapillary cities live in a separate cache directory.** Every study globs `*.csv` over a directory,
  so a Mapillary city dropped into `.cache/rawlabels/` silently joins the six-city GSV corpus and moves
  every committed artifact. `fetch_rawlabels.py` writes them to `.cache/rawlabels-mapillary/`.
- **Committed-artifact tests do not test code.** Pinning a finding against `reports/data/*.json` proves
  nothing about the function that produced it: the artifact was generated *by* the current code, so a
  revert stays green. Every finding needs a synthetic code-level test beside its corpus pin. Three
  mutation batteries in a row surfaced survivors of exactly this shape.
- **Every number in a report's prose is transcribed from a committed artifact, and a test says so.**
  Two counts hand-typed into `reports/2026-08-11-mapillary-census.md` §6 were wrong by 2× and 6×, and
  nothing about the surrounding sentences looked different for it — a report table is the one place in
  this repo where a plausible number has no compiler and no test. So the script computes the whole
  table, and `TestReportMatchesTheArtifact` asserts each value appears in the markdown. The same round
  found a filtered count (81,667 eligible) quoted as a raw one (82,769): **state which filter a count is
  under, or don't quote it.** Corollary for exclusion rules: **size the rule against the corpus it will
  be applied to, not only the one it was derived from.** `NoSidewalk` was left as an open question after
  a rule was derived on 267 Richmond labels that contain none of it; it is 82,769 labels and the largest
  arm in the six-city corpus the rule actually governs.
- **Two figures in one artifact must each name the frame they were computed on.**
  `click_noise_study.study()` put a matched sigma (referent-filtered, 335,712 labels) and every clustered
  sigma (all 436,348) in one dict and printed them in one column, and nothing recorded that they were
  100,636 labels apart — so a docstring, a test docstring and a report all quoted one against the other
  as a single comparison. The fix is not prose: `populations` names each side, lists the keys it covers,
  and a test asserts every emitted figure is claimed by exactly one side, so the *next* figure added
  cannot land unclaimed. Where the comparison is the point, compute the like-for-like cell too
  (`comparable_only`) rather than leaving a reader to assume the difference is the estimator — here it
  mostly was, but that was a measurement, not a given.

## The gold-standard annotation tool (`corpus_sample.py` → `annotation_tiles.py` → `annotation_subset.py` → `annotate_server.py`)

Four scripts feed each other, and the seams between them are load-bearing:

- **`corpus_sample.py`** draws the study corpus from a rawLabels frame. **Label identity is
  `(city, label_id)`, carried as `label_uid`** — `label_id` restarts at 1 in every deployment, and keying
  on it silently cost 314 labels of a 763-label draw. `pano_id` does *not* collide across cities, which is
  why the per-pano cap and the pano-wise tune/eval split are sound on it.
- **`annotation_tiles.py`** cuts one tile per label and emits **two** files. `tasks.json` is
  annotator-facing and carries no stored coordinate, no jitter, no tile origin and **no seed** — each of
  those recovers the answer, since the tile origin is `stored + jitter - size/2`. `geometry.json` carries
  all of them and is what the analysis uses to map a tile-space annotation back to pano coordinates.
  Tiles are cut at **60°** and the view opens at the whole cut: a tile of angular width F can only
  measure a displacement up to F/2, so a tight tile converts gross errors into `object-absent` and
  deletes the largest errors from the distribution being estimated.
- **Everything angular on that instrument must be angular, including the jitter.** It was `±40–80 px`
  until 2026-08-19, which is 1.5–2.9% of the tile on the 8192-height panos 641 of the 763 drawn labels
  sit on and 7.2–14.4% on the 1664s — so the one device whose job is to keep the stored point off the
  tile centre varied ~5× with resolution and was weakest on 84% of the corpus. It is now
  `JITTER_MIN_FRAC`/`JITTER_MAX_FRAC` of the tile (§4's numbers at the 20° cut §4 was written for),
  which also means changing `CUT_FOV_DEG` can no longer dilute it.
- **`measurable` has exactly one definition, `rawlabels.study_measurable`,** and **no script may read
  the corpus CSV's `measurable` column** — that is a snapshot of the rule at draw time and says 584
  where the live rule says 368. `annotation_subset.py --measurable-only` and
  `annotation_tiles.py --measurable-only` both call it; the latter used to read the column, which is
  the failure the former was written to prevent, one script upstream.
- **Protocol fields come from code, pixel fields come from the rendered file.** `FLAGS`, `FLAG_HELP`,
  `BOX_RULE` and `initial_view_fraction` are properties of the instrument, so
  `annotate_server.tasks_payload` sends them from `annotation_tiles` on every request and
  `annotation_subset.write_subset` refreshes them in the copies it writes. `cut_fov_deg` is the one
  exception: it describes the pixels, so it comes from whatever produced them. Taking the flag *list*
  from the file while taking its *help text* from code is the specific half-measure that shipped a
  queue offering three flags to a server that accepted four — the annotator had no key to press.
- **`annotation_subset.py`** narrows an already-rendered tile set, and is where the *current* referent
  rule is applied. Both its filters fail silently: a queue drawn from the wrong population or missing a
  flag looks perfectly well-formed.
- **`annotate_server.py`** serves tiles to `annotate.html` on loopback and writes one JSON per label per
  annotator. It refuses `geometry.json` **by name** — it sits beside the file that is served, and the
  natural static-file handler would publish the answer key at a guessable URL.

Amendment 1(e) forbids porting the webpage's render path into any of this: Study 1 compares stored
`pano_x`/`pano_y` against gold *in pano coordinates*, so a mapping sharing the projection under test would
make the study measure zero by construction. The tile transform is verified by round-trip against
directly-indexed pixels, never against another implementation.

**The corpus is 8 types; Study 1's measurable set is 4.** The referent rule (2026-08-13) excludes
Occlusion, Crosswalk, NoSidewalk, **Signal** and **Other** by type, plus eleven `(label_type, tag)`
pairs — leaving CurbRamp, NoCurbRamp, Obstacle and SurfaceProblem, 368 of the 763-label corpus. It is a
**placement-measurability** rule, not a corpus rule: the excluded types have real crop consumers and
Study 2 still sizes crops for them. What changed on 2026-08-13 is that they are no longer *annotated* —
if a referent has no located centre it has no tight extent either, so a gold box on one is as arbitrary
as a gold point. The rule is keyed on **pairs, not tags**: `height difference` is excluded under
SurfaceProblem (a run of pavement) and kept under Obstacle (a discrete step). Tags are optional, so the
rule is leaky by construction — 14% of Obstacle labels carry none — which is what the `no-extent` flag
is for, and that flag is **reported as its own bucket, never dropped from a denominator**.

The prereg's §7 is a **decision log**, not an amendment log — plain dated entries recording what changed
and *what was known at the time*, since the ordering (a filter fixed before any gold existed) is the only
part that cannot be reconstructed later. Old references resolve as Amendment 1/2/3 = 2026-08-11/12/13.
Note that changing the referent rule invalidates published artifacts computed under the old one: the
Mapillary census is deliberately **not** regenerated, and `TestTheCommittedRuleIsCurrentOrSuperseded`
fails if the live rule diverges from a committed artifact's recorded rule without the report saying so.
