# Criteria: consolidating the crop-sizing evidence, PR-A of #186

Written 2026-11-22, before any new measurement, and committed on its own ahead of the code that measures.
This is the dated record of what the comparison is and what was already known when it was fixed; it is
the record [#186](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/186)'s plan (section
2.4) asks for, and it does not claim more blindness than there was. The plan itself is the comment of
2026-11-22 on that issue (planner Fable 5.2, plan reviewer Opus 5.5). This file restates the plan's
criteria for PR-A and adds the operational definitions the engineer (Opus 5.5) fixed before running
anything; the second kind is marked **[operational]**. Every criterion is **provisional** until Jon
confirms it (the plan's J3).

PR-A is report-only. No change to `CropRunner.py`, a default, a constant, a flag or `crop_rule.json`.

## What was known before these criteria were written

**Public before this file:** every point estimate for v2, v3 and option A on the 658-ramp gold, pooled and
per city (`reports/data/2026-09-26-crop-sizing-v3.json`), and every v1/v2 figure in
`reports/data/2026-08-19-crop-sizing-v2.json`. The planner re-ran the v3 study against RampNet `8d8d8ee` and
got the committed blocks back (pilot record `pilot/v3-rerun-8d8d8ee.json`, committed with the extracts).
Only intervals, the out-of-sample rows, the per-provider widths and the tilt arm are new.

**Seen by the planner in a pilot** (`probe_tilt.py`, committed with its output under
`reports/data/2026-11-22-crop-sizing-consolidation/pilot/`): the modelled tilt shift on the GSV gold at
beta 0.95, |dy| p50 0.79 deg / p90 2.64 deg in Sao Paulo (119 ramps) and 1.24 deg / 2.59 deg in Paterson
(109), and the share of GSV windows whose size moves by more than 10% between the gravity-frame y and the
rig-frame y: 21% (v2) and 28% (v3) in Sao Paulo, 22% and 30% in Paterson.

**Also seen before this file, by the planner and the reviewer, and not part of PR-A:** the tagger's
full-test mAPs (control 0.354457, fov25 0.349421, fov50 0.319403, fov90 0.280352), and the planner's
fov25/fov50/fov90 mAP in two depression bands (pilot `probe_tagger.out.txt`). They bear on PR-B's
criterion, which the plan already discloses as written after its point estimates were seen.

**Seen by the engineer before this file:** the plan, the review, the planner's answers, the pilot outputs
above (tagger bands included), and the committed v2 and v3 artifacts. The engineer computed nothing new.

**Not seen by anyone:** any R², dispersion or containment under a tilt configuration, any bootstrap
interval, any leave-one-city-out figure, any per-provider width.

## The criteria (the plan's section 2.4, restated)

1. Rules are compared at matched pooled fill p50. Clearing and containment never rank rules.
2. On the four-city gold, "X is better than Y on metric M" means the 95% pano-clustered interval of the
   paired pooled difference excludes 0 **and** the sign agrees in at least three of four cities.
   Otherwise: "not distinguishable on this gold". In the tilt arm the second clause is "both GSV cities
   agree in sign".
3. D7 (v3 against option A) is read on `fill_log_sd`, p90/p10 and R² under criterion 2. The point
   estimates are already published and split the metrics; the new content is whether any interval
   excludes 0.
4. D2 (5.8 m against 6.0 m) is a choice of criterion, not a data question: the two are 3.4% apart and
   differ only in which target fill they match. The bootstrap is reported to show that, and the report
   says so up front.
5. D6: the sign is already known (Annapolis clears 44.3% under v3 against 75.2% pooled). The test is per
   provider first: does a Mapillary constant differ from a GSV constant under criterion 2's interval rule?
   "Per city" is supported only if Annapolis also differs from Richmond, and the report calls that n = 1
   per cell. "The pooled constant misses the held-out city" means the upper end of that city's 95%
   interval on clearing is below 45%.
6. Tilt sizing, three-way, on `fill_log_sd` and R² over the 228 GSV ramps: **g.y better**, **r.y better**,
   or **not distinguishable at the stated minimum detectable effect**. Pre-stated reading of the third:
   the gold cannot tell, and the recommendation on #197's prerequisite is to keep sizing at the stored y
   on physical grounds (the cotangent wants the gravity depression, and the stored y is the gravity y to
   within beta). If g.y against g-.y (the negated-pose comparator) is also not distinguishable, the report
   says the gold cannot see tilt at all.
7. A recommendation is "high confidence" only if it rests on a criterion-2 result that also holds out of
   sample; "low" if it rests on quotable-only evidence. Because v3-depth is unscored on the GSV gold, D1's
   candidate set is incomplete and no "flip to v3" recommendation is rated above low.

## Operational definitions, fixed before measurement [operational]

### Population, identity, strata

- The gold is the 658 `status == "boxed"` items of the four RampNet `boxes.json` files at `8d8d8ee`,
  keyed by `(city, pano_id, key)`; no Project Sidewalk `label_id` is involved. Each window is centred on
  the item's `point` and cut through `CropRunner.compute_crop_box`.
- Strata: pooled; each city; each provider (`gsv` = Sao Paulo + Paterson, 228 ramps on 70 panos;
  `mapillary` = Richmond + Annapolis, 430 ramps). Provider comes from the record's `source` and must agree
  with the id-shape rule the v3 study used.
- Rule configurations: v2 at x2.5 (shipped), x2.0, x3.0; v3 at 5.8 m (shipped) and 6.0 m; option A at its
  matched scale (re-derived on v3's grid 1.5-3.5, expected x2.2) and at x2.5; v1-norm (square) at x1.0 and
  x3.5; geo-v1.5 (square) at camera 2.5 m and 1.7 m. v2 at a scale k is `CropRunner.predict_crop_size`
  times k through `elevation_px_to_deg`, clamped 8-90 deg; at k = 2.5 it must equal
  `CropRunner.crop_window_fov_deg(..., 'v2')`, and a test checks that.
- Square rules follow RampNet `scripts/analysis/crop_window_eval.py` at `8d8d8ee` (side functions and the
  square `crop_window`). Their reproduction check is RampNet's own detection-mode population (`det:` items
  centred on their point) and its interpolated quantiles, compared with the copied `crop_window_eval_*.json`.

### Metrics

- Discriminating: `fill_log_sd` (sample sd of log fill), `fill_p90_over_p10`, R² of log window degrees
  against log apron degrees, the isotonic ceiling (rule-independent, per stratum). "Better" means lower
  `fill_log_sd`, lower p90/p10, higher R².
- Checks: share with fill <= 0.49 (clearing), positional containment (`crop_sizing_v2.box_inside_window`).
- Descriptive: fill p10/p50/p90, window degrees p10/p50/p90, stored width p50, vertical fill p50 (apron
  box height over window height).
- Percentiles are nearest rank, as in the v2 and v3 studies.

### Bootstrap

- Pano-clustered within city: each replicate draws, for each city separately, as many panos as the city
  has, with replacement, and takes every ramp of each drawn pano as many times as the pano was drawn.
  B = 2000, Python `random.Random(186)`, the same replicates for every rule and configuration so every
  difference is paired. The interval is the nearest-rank 2.5th and 97.5th percentile of the B replicate
  values. A replicate in which a metric is undefined is dropped from that metric's interval and the
  count is recorded.
- Paired pooled differences under criterion 2: v3 5.8 m minus v2 x2.5; option A at its matched scale
  minus v2 x2.5; v3 5.8 m minus option A at its matched scale (D7). Each on `fill_log_sd`,
  `fill_p90_over_p10` and R²; clearing and containment differences are reported beside them and rank
  nothing. City sign agreement uses each city's point estimate of the same difference; a difference of
  exactly 0 counts as not agreeing.

### Out of sample (question 4)

- Leave one city out, for each city H: a constant is chosen on the other three cities and applied to H.
  v3 width and option A scale, each under both criteria: (a) pooled fill p50 of the three nearest v2 x2.5's
  pooled fill p50 on the same three; (b) pooled fill p50 of the three nearest 0.36. v2's scale under (b)
  only, since (a) is x2.5 by definition; v2 at x2.5 is also reported on H, labelled not cross-validated.
  Width grid 3.0-12.0 m in 0.1 m steps; scale grids 1.0-6.0 in 0.05 steps; first grid value on a tie.
- On H, at the chosen constant (held fixed): clearing and fill p50, each with a 95% pano-clustered
  interval from resampling H's panos. "Misses the 45% floor" = the interval's upper end is below 0.45.
  "In the band" = the point estimate of fill p50 lies in [0.28, 0.44].
- The bootstrap distribution of the pooled selected v3 width under both criteria (on the 4.0-8.0 m grid
  the v3 study used, and on the wide grid): its 2.5th, 50th and 97.5th percentiles and the share of
  replicates selecting 5.8 and 6.0.
- "Holds out of sample", for criterion 7: a criterion-2 ranking holds out of sample when, in each of the
  four folds, the held-out city's paired difference (at the constants chosen on the other three under
  criterion (a)) has the pooled difference's sign.

### D6 (question 5)

- The per-provider constant is the v3 width whose fill p50 on that provider's ramps is nearest 0.36
  (criterion (b), an absolute target), on the 3.0-12.0 m grid. A Mapillary width "differs" from a GSV width
  when the 95% interval of the paired bootstrap difference (Mapillary minus GSV) excludes 0. Annapolis minus
  Richmond is tested the same way, and only read if the provider difference holds. The width matched to
  v2's fill p50 within each provider is reported beside it as descriptive: it measures v3's shape against
  v2's, not what the provider needs.

### The tilt arm (GSV ramps only)

- The record pose: `camera_pitch`, and `camera_roll` wrapped with `pano_pose.wrap_deg`. The modelled stored
  PS point is g = `pano_pose.gravity_pixel_from_rig_pixel(r, beta * pitch, beta * roll)`; the negated-pose
  comparator g- uses `-beta * pitch, -beta * roll`. Primary beta 0.95; 1.0 and 0.88 are sensitivity rows.
- Configurations: S-rig (centre r, sized at r.y), S-grav (centre r, sized at g.y), Off (centre g, sized
  at g.y), S-neg (centre r, sized at g-.y), S-grav-noise (centre r, sized at g.y plus Gaussian noise of
  0.53 deg of elevation, `random.Random(1861)`).
- Criterion 6 is evaluated separately for v2 at x2.5 and v3 at 5.8 m. For each metric (`fill_log_sd`, R²)
  the difference is S-grav minus S-rig: "g" when the 95% interval excludes 0 in the better direction and
  both GSV cities' point differences agree in sign, "r" for the mirror case, else "none". Per rule: **g.y
  better** if at least one metric says "g" and neither says "r"; **r.y better** in the mirror case;
  otherwise **not distinguishable**. The same function, unchanged, scores the negated-pose control
  (S-grav against S-neg) and every power simulation.
- Power, computed and committed before any real apron width is read under a tilt configuration: the 228
  ramps' real points, poses and pano sizes; synthetic apron width = window width at g_m.y (pose scaled by
  m times beta) times 0.371 times exp(sigma_city z), z standard normal, with sigma_city the city's
  committed `fill_log_sd` for that rule in the v3 artifact (Paterson v2 0.327, v3 0.296; Sao Paulo v2
  0.335, v3 0.312). Apron height is not used. 200 simulations per multiplier, 500 bootstrap replicates
  each (pano-clustered within city, as above), `random.Random(1860)`; multipliers m in {1, 1.5, 2, 3, 4,
  6, 8}. Power is the share of simulations whose verdict is **g.y better**. The minimum detectable effect
  is the smallest m in the grid with power >= 0.80, or "beyond 8" if none.
- The v3 width selection re-run: pooled 658, GSV ramps sized at g.y (beta 0.95), Mapillary unchanged, v2's
  target fill p50 computed in the same configuration; v3's grid 4.0-8.0 m; both criteria.
- Off against S-grav: containment and size metrics only, labelled model-conditional (noiseless modelled
  click, full leak beta). It restates the tilt study's S3 and is not evidence that the flag helps real
  clicks.

### Confidence labels [operational]

"High" and "low" are criterion 7's. Anything that is neither (for example a criterion-2 result that does
not hold out of sample) is "moderate".

## What a later number cannot change

If a number seen after this commit makes a criterion look wrong, the criterion is not edited: the report
records it under its deviations and wrong turns, with what was seen and when.
