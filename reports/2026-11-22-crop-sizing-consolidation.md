# Consolidating the crop-sizing evidence: every rule on one gold, out of sample, and with the tilt term

2026-11-22. Part of [#186](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/186) (PR-A of its
plan; PR-B, the re-analysis of RampNet's tagger experiment, is the next PR). **Report-only: nothing in
`CropRunner.py`, no default, constant, flag or `crop_rule.json` key changes.** v2 stays the default, 5.8 m
stays, the flag keeps its name.

- Criteria, committed before any measurement: [`plans/2026-11-22-issue-186-crop-sizing-consolidation-plan.md`](plans/2026-11-22-issue-186-crop-sizing-consolidation-plan.md),
  commit `1ee6229`. The tilt arm's power simulation was committed next, on its own (`3370c1f`), and the
  study ran after it (`20d466e`).
- Artifact: [`data/2026-11-22-crop-sizing-consolidation.json`](data/2026-11-22-crop-sizing-consolidation.json);
  inputs and per-ramp rows: [`data/2026-11-22-crop-sizing-consolidation/`](data/2026-11-22-crop-sizing-consolidation/);
  script: [`scripts/crop_sizing_consolidation.py`](scripts/crop_sizing_consolidation.py); tests:
  `tests/test_crop_sizing_consolidation_report.py`.

## Outcome

- **v3 and option A both beat v2** on all three discriminating metrics under criterion 2 (interval excludes 0,
  three of four cities agree), and the sign holds in all four leave-one-city-out folds.
- **v3 against option A (D7):** v3 tracks the apron better on R² (all four cities, interval excludes 0);
  fill dispersion and p90/p10 are not distinguishable. The published "A is less dispersed" was a point
  estimate inside its own interval.
- **D2 (5.8 m against 6.0 m) is a choice of criterion**, as pre-stated: the two criteria's bootstrap
  ranges overlap and neither width is rejected by the other's.
- **D6:** a per-provider width is supported (Mapillary needs a wider v3 context than GSV to reach the band
  centre, and Annapolis than Richmond), but **no held-out city misses the 45% floor** by the pre-stated rule,
  Annapolis included.
- **Tilt (#197's prerequisite), the unexpected result:** sizing at the rig-frame y tracks the hand-drawn
  apron **better** than sizing at the modelled gravity-frame y, for v2 under criterion 2 (**r.y better**),
  and in the same direction but not distinguishable for v3. The negated-pose control is not distinguishable
  from S-grav, which is what r.y being the right coordinate predicts. This contradicts the plan's physical
  prior (the gravity depression is what the cotangent wants). Section 7 discusses it.

## 1. The question

Five decisions from PR #157 are parked on #186: D1 (flip the default to v3), D2 (5.8 m or 6.0 m), D3 (the flag
name), D6 (a per-city width for Annapolis) and D7 (v3 or option A). Both shipped constants were chosen and
scored on the same 658-apron gold, so every published comparison was in sample and without an interval, and
neither rule had been scored with the window sized where a tilt-corrected label actually sits. The issue
asks for the evidence from this repo, RampNet and the auto-labeler to be read as one body before anything
flips.

## 2. What was known before the criteria

The criteria file records it; in brief:

- **Public:** every v2, v3 and option A point estimate on this gold (the v2 and v3 artifacts). The planner
  re-ran the v3 study against RampNet `8d8d8ee` and got every block back (`pilot/v3-rerun-8d8d8ee.json`).
- **Seen in the planner's pilot** (`pilot/probe_tilt.py`): the modelled tilt shift, |dy| p50 0.79° / p90
  2.64° in São Paulo and 1.24° / 2.59° in Paterson, and the share of windows moving by more than 10% between
  g.y and r.y, 21% (v2) and 28% (v3) in São Paulo, 22% and 30% in Paterson.
- **Seen, and PR-B's:** the tagger's full-test mAPs and the planner's two-band split (`pilot/`).
- **Seen by nobody:** any interval, any out-of-sample figure, any per-provider width, any metric under a tilt
  configuration. Those are this report's new content.

## 3. Evidence inventory

Status: **R** re-derivable offline from this repo today, **Q** quotable only, **M** raw input missing.

| # | Experiment | Objective it measures | Status here |
|---|---|---|---|
| E1 | Extent gold: RampNet `benchmark/<city>/boxes.json` + `records.jsonl` ([RampNet](https://github.com/ProjectSidewalk/RampNet)), 705 items, 658 boxed | One annotator's whole-apron boxes (box rule v2) | **R**, now copied: `gold.csv` |
| E2 | [Sizing v2](2026-08-19-crop-sizing-v2.md) | v1 against v2: fill, clearing, containment | **R**, reproduced below |
| E3 | [Sizing v3](2026-09-26-crop-sizing-v3.md), option A | v2, v3, option A: dispersion, R², the isotonic ceiling | **R**, reproduced below |
| E4 | RampNet [crop-window evaluation](https://github.com/ProjectSidewalk/RampNet/blob/main/docs/crop_window_eval.md), rounds 2-3 | Square v1-raw, v1-norm, geo-v1.5: containment, context ratio, a rule's scale-free spread | **R**: the four summaries are copied and reproduced below |
| E5 | The same, round 1 (`manual_labels`) | Near-point marks, not extents | **Q**; not gold |
| E6 | RampNet [tagger FOV experiment](https://github.com/ProjectSidewalk/RampNet/blob/main/docs/context_fov_86.md) | mAP of a tagger trained and tested at fixed FOVs | **R** from RampNet's logits; PR-B |
| E7 | Three blind human rounds, sidewalk-auto-labeler `PLAN-crop-clarity.md` §5a | One judge's framing: the 0.49 threshold, the 0.28-0.44 band, ×2.5 | **Q**; the plan is now snapshotted here, the raw verdicts are **M** |
| E8 | [Consumer requirements](2026-08-09-cropper-consumer-requirements.md) | A survey of consumers' constants (the 10-15% band) | **Q** |
| E9 | [Clamp census](2026-08-09-clamp-census.md) | Depression percentiles of production labels by type | **R** (percentiles) |
| E10 | [Tilt study](2026-09-26-tilt-error-study.md) S3, S4 | Tilt shift against the window; PS labels on gold panos | **Q** |
| E11 | RampNet [crop cutter](https://github.com/ProjectSidewalk/RampNet/blob/main/docs/crop_cutter.md) | A gnomonic cutter against tagger crops | **Q** |
| E12 | v3-depth (#180, PR #211) | A depth-backed distance | **M** on the GSV gold (no depth artifact for a gold pano); equals v3 on Mapillary by construction (the `no_artifact` fallback) |
| E13 | Camera heights | Four different quantities (section 6.7) | **Q** |
| E14 | RampNet pano-tilt validation | Image-measured zenith against the record pose | **Q**, copied: `pano_tilt_validation_summary.json` |

E14 is what makes the tilt arm's model well posed: the zenith measured in the JPEG follows the record pose with
slopes 0.9708 (pitch) and 0.9782 (roll), MAE 0.2305°, on 434 accepted panos; Paterson 0.9724 / 0.9677 and São
Paulo 0.9932 / 1.005. So the gold's GSV JPEGs are rig-frame stitches the record poses describe, in
`pano_pose`'s sign.

## 4. Method

**Inputs.** `extract` reads RampNet `8d8d8ee` and sidewalk-auto-labeler `2c46e7f` with `git show` (committed
blobs only) and writes `gold.csv` (705 rows, `status` kept, no attribution fields), RampNet's four
`crop_window_eval_<city>.json`, the pano-tilt validation summary and its rows for the 70 GSV gold panos, and a
snapshot of `PLAN-crop-clarity.md` with one absolute local path redacted; `provenance.json` pins each by
sha256. A `RAMPNET_ROOT`/`AUTO_LABELER_ROOT`-gated test rebuilds every file byte for byte. The gold is keyed
by `(city, pano_id, key)`; it carries no Project Sidewalk `label_id`.

**Rules.** Every 3:2 window is `CropRunner.compute_crop_box` over CropRunner's own sizing primitives, centred on
the gold point. v2 at ×2.5 (shipped), ×2.0, ×3.0; v3 at 5.8 m (shipped) and 6.0 m; option A at ×2.2 and ×2.5;
v1-norm (square) at ×1.0 and ×3.5; geo-v1.5 (square) at camera 2.5 m and 1.7 m.

**Metrics.** Discriminating: fill log-sd, fill p90/p10, R² of log window degrees on log apron degrees, and the
isotonic ceiling. Checks: clearing (fill at most 0.49) and positional containment. Percentiles are nearest
rank.

**Intervals.** Panos resampled within each city, 2000 replicates, the same replicates for every rule and
configuration, so every difference is paired; nearest-rank 2.5th and 97.5th percentiles.

**Out of sample, D6 and the tilt arm** follow the criteria file's operational definitions exactly: a constant
chosen on three cities and applied to the fourth; the per-provider v3 width at the band centre; and, on the
228 GSV ramps, the modelled stored point g = `gravity_pixel_from_rig_pixel(r, 0.95 × pose)`, with five
configurations: S-rig (centre r, sized at r.y), S-grav (centre r, sized at g.y), Off (centre g, sized at g.y),
S-neg (sized at the negated-pose point) and S-grav-noise (g.y plus 0.53° of click noise). Beta 1.0 and 0.88
are sensitivity rows.

The whole study runs in about 33 s from the committed extract, so CI regenerates every block, intervals
included, and compares it with the artifact.

## 5. Results

### 5.1 The new scorer is the old scorer

From `gold.csv` alone: the v2 artifact's eleven listed keys and the v3 artifact's thirteen, for both rules, in
every city and pooled, reproduce to a relative 1e-12 (R² and the isotonic ceilings to 1e-9); so do option A's matched scale (×2.2) and both selected widths (5.8 m matched to v2's pooled fill
p50 of 0.371, 6.0 m at the band centre). RampNet's square rules reproduce its committed detection-mode
summaries and its v1-norm ×3.5 sweep row in all four cities, 96 values in all. From the copied sweeps, the
smallest v1-norm scale containing at least 98.5% of detection-mode aprons is ×2.0 in São Paulo, ×2.5 in
Paterson, ×3.5 in Richmond and ×4.0 in Annapolis, RampNet's round 3; its round-2 Richmond figures,
geo-v1.5 containment 0.996 and an apron aspect of 3.29:1, are in the same files.

### 5.2 Every rule on the gold (pooled, 658 ramps)

| rule | fill p50 | fill log-sd [95%] | fill p90/p10 | R² [95%] | clearing [95%] | containment | window p50 | vertical fill p50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2 ×2.5 (shipped) | 0.371 | 0.428 [0.395, 0.459] | 2.97 | 0.570 [0.518, 0.622] | 74.8% [70.3%, 79.0%] | 0.944 | 24.9° | 0.175 |
| v2 ×2.0 | 0.462 | 0.428 [0.396, 0.459] | 2.97 | 0.565 [0.513, 0.616] | 57.6% [52.6%, 62.3%] | 0.886 | 19.9° | 0.218 |
| v2 ×3.0 | 0.310 | 0.430 [0.397, 0.461] | 2.97 | 0.579 [0.528, 0.631] | 84.2% [80.3%, 87.8%] | 0.973 | 29.8° | 0.147 |
| v3 5.8 m (shipped) | 0.370 | 0.406 [0.374, 0.435] | 2.78 | 0.612 [0.557, 0.665] | 75.2% [70.9%, 79.6%] | 0.944 | 24.6° | 0.170 |
| v3 6.0 m | 0.359 | 0.406 [0.375, 0.436] | 2.78 | 0.611 [0.557, 0.664] | 76.7% [72.4%, 80.8%] | 0.950 | 25.5° | 0.165 |
| option A ×2.2 | 0.368 | 0.402 [0.371, 0.431] | 2.73 | 0.604 [0.549, 0.657] | 77.4% [73.1%, 81.3%] | 0.944 | 23.8° | 0.170 |
| option A ×2.5 | 0.325 | 0.403 [0.373, 0.433] | 2.73 | 0.600 [0.546, 0.653] | 82.8% [78.8%, 86.4%] | 0.965 | 27.0° | 0.151 |
| v1-norm ×1.0, square | 0.923 | 0.428 [0.396, 0.459] | 2.97 | 0.565 [0.513, 0.616] | 4.3% [2.4%, 6.3%] | 0.368 | 9.9° | 0.291 |
| v1-norm ×3.5, square | 0.264 | 0.428 [0.396, 0.459] | 2.97 | 0.565 [0.513, 0.616] | 90.7% [87.8%, 93.6%] | 0.991 | 34.8° | 0.083 |
| geo-v1.5 2.5 m, square | 0.209 | 0.413 [0.381, 0.441] | 2.85 | 0.600 [0.545, 0.659] | 96.8% [95.2%, 98.2%] | 0.994 | 47.7° | 0.063 |
| geo-v1.5 1.7 m, square | 0.145 | 0.436 [0.402, 0.466] | 3.08 | 0.564 [0.508, 0.623] | 98.5% [97.4%, 99.4%] | 1.000 | 70.2° | 0.044 |

Within a family, the dispersion metrics barely move with the constant (v2 ×2.0, ×2.5 and ×3.0 all have a fill
p90/p10 of 2.97), while clearing and containment move a lot: the reason criterion 1 compares at matched fill
p50 and lets neither check rank rules. v1-norm is v2's base, so its fill dispersion is v2's; geo-v1.5 is v3's
flat-ground form, near v3 at RampNet's 2.5 m and more dispersed at 1.7 m.

By stratum, for the three 3:2 candidates at matched fill (v2 ×2.5 / v3 5.8 m / option A ×2.2):

| stratum | n | fill log-sd v2 / v3 / A | R² v2 / v3 / A | clearing v2 / v3 / A | isotonic ceiling [95%] |
|---|---:|---:|---:|---:|---:|
| **pooled** | 658 | 0.428 / 0.406 / 0.402 | 0.570 / 0.612 / 0.604 | 74.8% / 75.2% / 77.4% | 0.647 [0.604, 0.711] |
| Richmond | 299 | 0.421 / 0.402 / 0.400 | 0.568 / 0.595 / 0.587 | 74.2% / 74.6% / 75.9% | 0.653 [0.597, 0.735] |
| São Paulo | 119 | 0.335 / 0.312 / 0.295 | 0.743 / 0.790 / 0.782 | 95.0% / 95.0% / 95.8% | 0.838 [0.765, 0.924] |
| Paterson | 109 | 0.327 / 0.296 / 0.279 | 0.747 / 0.798 / 0.792 | 92.7% / 92.7% / 95.4% | 0.841 [0.783, 0.913] |
| Annapolis | 131 | 0.445 / 0.427 / 0.429 | 0.501 / 0.530 / 0.522 | 42.7% / 44.3% / 48.9% | 0.585 [0.477, 0.741] |
| GSV | 228 | 0.330 / 0.304 / 0.287 | 0.747 / 0.796 / 0.789 | 93.9% / 93.9% / 95.6% | 0.833 [0.780, 0.896] |
| Mapillary | 430 | 0.447 / 0.427 / 0.424 | 0.535 / 0.566 / 0.558 | 64.7% / 65.3% / 67.7% | 0.611 [0.555, 0.691] |

### 5.3 Criterion 2: the paired comparisons

| comparison | metric | pooled difference [95%] | Richmond / São Paulo / Paterson / Annapolis | criterion 2 | sign holds in all four held-out folds |
|---|---|---:|---:|---|---|
| option A − v2 | fill log-sd | −0.0259 [−0.0401, −0.0121] | −0.021 / −0.040 / −0.048 / −0.016 | option A better | yes |
| option A − v2 | fill p90/p10 | −0.2427 [−0.4535, −0.0255] | −0.226 / −0.102 / −0.078 / −0.071 | option A better | yes |
| option A − v2 | R² | +0.0335 [+0.0151, +0.0509] | +0.019 / +0.040 / +0.045 / +0.021 | option A better | yes |
| v3 − option A | fill log-sd | +0.0034 [−0.0046, +0.0115] | +0.002 / +0.017 / +0.017 / −0.002 | not distinguishable | no |
| v3 − option A | fill p90/p10 | +0.0454 [−0.0866, +0.1855] | +0.006 / +0.027 / −0.121 / −0.141 | not distinguishable | no |
| v3 − option A | R² | +0.0080 [+0.0029, +0.0136] | +0.008 / +0.008 / +0.006 / +0.008 | v3 better | yes |
| v3 − v2 | fill log-sd | −0.0225 [−0.0302, −0.0150] | −0.019 / −0.022 / −0.031 / −0.018 | v3 better | yes |
| v3 − v2 | fill p90/p10 | −0.1972 [−0.3395, −0.0300] | −0.221 / −0.075 / −0.199 / −0.212 | v3 better | yes |
| v3 − v2 | R² | +0.0414 [+0.0246, +0.0585] | +0.027 / +0.048 / +0.051 / +0.028 | v3 better | yes |

The held-out folds re-select v3's width and option A's scale on the other three cities (criterion (a)) and read
the held-out city's difference; because the discriminating metrics are scale-invariant apart from the clamps,
this is close to "all four cities agree", which is what it should be.

### 5.4 Out of sample (question 4)

| held out | rule, criterion | constant | clearing [95%] | misses the 45% floor | fill p50 [95%] | p50 in 0.28-0.44 |
|---|---|---:|---:|---|---:|---|
| Richmond | v2 ×2.5 (not cross-validated) | ×2.5 | 74.2% [67.0%, 80.9%] | no | 0.378 [0.350, 0.407] | yes |
| Richmond | v2, band centre | ×2.55 | 74.9% [67.6%, 81.5%] | no | 0.371 [0.344, 0.399] | yes |
| Richmond | v3, match v2 | 5.6 m | 71.6% [63.7%, 78.5%] | no | 0.407 [0.370, 0.429] | yes |
| Richmond | v3, band centre | 5.8 m | 74.6% [66.8%, 81.2%] | no | 0.394 [0.358, 0.415] | yes |
| Richmond | option A, match v2 | ×2.1 | 70.6% [63.1%, 77.7%] | no | 0.414 [0.376, 0.436] | yes |
| Richmond | option A, band centre | ×2.15 | 73.6% [66.4%, 80.3%] | no | 0.405 [0.367, 0.426] | yes |
| São Paulo | v2 ×2.5 (not cross-validated) | ×2.5 | 95.0% [89.2%, 99.2%] | no | 0.322 [0.301, 0.368] | yes |
| São Paulo | v2, band centre | ×2.65 | 95.0% [89.2%, 99.2%] | no | 0.308 [0.284, 0.347] | yes |
| São Paulo | v3, match v2 | 6.1 m | 95.0% [88.5%, 100.0%] | no | 0.316 [0.294, 0.337] | yes |
| São Paulo | v3, band centre | 6.4 m | 96.6% [92.2%, 100.0%] | no | 0.303 [0.280, 0.323] | yes |
| São Paulo | option A, match v2 | ×2.3 | 96.6% [92.2%, 100.0%] | no | 0.298 [0.286, 0.319] | yes |
| São Paulo | option A, band centre | ×2.4 | 96.6% [92.2%, 100.0%] | no | 0.286 [0.278, 0.305] | yes |
| Paterson | v2 ×2.5 (not cross-validated) | ×2.5 | 92.7% [84.4%, 97.8%] | no | 0.315 [0.284, 0.356] | yes |
| Paterson | v2, band centre | ×2.65 | 94.5% [87.7%, 98.4%] | no | 0.297 [0.273, 0.336] | yes |
| Paterson | v3, match v2 | 6.0 m | 95.4% [89.4%, 99.2%] | no | 0.319 [0.287, 0.343] | yes |
| Paterson | v3, band centre | 6.4 m | 96.3% [90.6%, 100.0%] | no | 0.300 [0.269, 0.326] | yes |
| Paterson | option A, match v2 | ×2.25 | 95.4% [89.4%, 99.2%] | no | 0.315 [0.294, 0.342] | yes |
| Paterson | option A, band centre | ×2.4 | 95.4% [89.4%, 99.2%] | no | 0.296 [0.275, 0.321] | yes |
| Annapolis | v2 ×2.5 (not cross-validated) | ×2.5 | 42.7% [32.3%, 52.6%] | no | 0.519 [0.468, 0.588] | no |
| Annapolis | v2, band centre | ×2.45 | 41.2% [30.8%, 51.1%] | no | 0.529 [0.478, 0.600] | no |
| Annapolis | v3, match v2 | 5.9 m | 45.8% [35.3%, 55.6%] | no | 0.505 [0.458, 0.567] | no |
| Annapolis | v3, band centre | 5.7 m | 42.7% [32.8%, 52.4%] | no | 0.523 [0.473, 0.584] | no |
| Annapolis | option A, match v2 | ×2.2 | 48.9% [38.6%, 58.3%] | no | 0.506 [0.458, 0.554] | no |
| Annapolis | option A, band centre | ×2.15 | 45.0% [33.6%, 55.2%] | no | 0.517 [0.468, 0.567] | no |

Annapolis held out is the only fold whose fill p50 leaves the 0.28-0.44 band, under every rule, but its
clearing interval reaches past 45% under every rule, so by the pre-stated definition no constant chosen on
the other three cities "misses" it. Its 131 ramps on 42 panos are too few to say that the pooled constant
fails it. ×2.5 itself is not cross-validated: the judge chose it on ramps from these four cities and the
verdicts are missing (E7), so question 4 stays open for ×2.5.

### 5.5 D2: 5.8 m against 6.0 m

| criterion | selected | replicates 2.5% / 50% / 97.5% | replicates selecting 5.8 m | selecting 6.0 m |
|---|---:|---:|---:|---:|
| match v2's fill p50 | 5.8 m | 5.6 / 5.8 / 6.1 m | 28.9% | 10.7% |
| band centre (fill p50 nearest 0.36) | 6.0 m | 5.7 / 6.0 / 6.5 m | 16.8% | 14.4% |

Each width sits inside the other criterion's replicate range. The wide 3.0-12.0 m grid gives the same
distribution, so the v3 study's 4.0-8.0 m grid did not truncate it.

### 5.6 D6: does a provider or a city need its own width? (question 5)

| group | band-centre width [95%] | v2-matched width [95%] |
|---|---:|---:|
| GSV | 5.3 m [5.1, 5.6] | 6.0 m [5.6, 6.2] |
| Mapillary | 6.8 m [6.4, 7.3] | 6.0 m [5.7, 6.2] |
| Richmond | 6.4 m [5.8, 6.7] | 6.1 m [5.8, 6.3] |
| São Paulo | 5.3 m [4.9, 5.7] | 6.0 m [5.4, 6.2] |
| Paterson | 5.3 m [4.8, 5.7] | 6.1 m [5.6, 6.5] |
| Annapolis | 8.5 m [7.7, 10.0] | 5.7 m [5.4, 6.0] |
| Mapillary − GSV | +1.5 m [+1.0, +2.0] | +0.0 m [−0.3, +0.4] |
| Annapolis − Richmond | +2.1 m [+1.2, +3.7] | −0.4 m [−0.7, +0.1] |

Under the pre-stated D6 criterion (the band-centre width), the Mapillary width differs from the GSV width, and
Annapolis from Richmond, so a per-city width is supported, with one city per cell on each side. The v2-matched
widths do not differ: v3's shape relative to v2's is the same across providers, and it is the ramps' angular
size, not the rule's shape, that separates them (Annapolis's apron aspect is also the widest, below). The
Annapolis band-centre width's interval runs to 10.0 m: v3 needs a far wider context there to reach fill 0.36.

### 5.7 The tilt arm (#197's prerequisite), on the 228 GSV ramps

**Power first** (`power_first.json`, committed before any real apron was scored under a tilt configuration):
the share of 200 simulations in which criterion 6 reads **g.y better** when g.y is the truth.

| rule | ×1 | ×1.5 | ×2 | ×3 | ×4 | ×6 | ×8 | minimum detectable multiplier |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2 | 0.550 | 0.855 | 0.965 | 0.995 | 1.000 | 1.000 | 1.000 | ×1.5 |
| v3 | 0.610 | 0.925 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | ×1.5 |

So at the modelled tilt the test had about even odds of seeing a real g.y advantage, and better than 80% at
1.5 times it. The configurations:

| rule | configuration | fill p50 | fill log-sd | fill p90/p10 | R² | clearing | containment | window p50 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| v2 | S-rig | 0.319 | 0.330 | 2.04 | 0.747 | 93.9% | 0.987 | 28.0° |
| v2 | S-grav | 0.320 | 0.344 | 2.05 | 0.720 | 94.3% | 0.987 | 26.4° |
| v2 | Off | 0.320 | 0.344 | 2.05 | 0.720 | 94.3% | 0.982 | 26.4° |
| v2 | S-neg | 0.317 | 0.339 | 2.16 | 0.720 | 92.5% | 0.987 | 26.9° |
| v2 | S-grav-noise | 0.323 | 0.343 | 2.10 | 0.720 | 95.2% | 0.982 | 26.5° |
| v3 | S-rig | 0.331 | 0.304 | 1.92 | 0.796 | 93.9% | 0.982 | 30.2° |
| v3 | S-grav | 0.327 | 0.316 | 1.90 | 0.775 | 93.9% | 0.982 | 27.4° |
| v3 | Off | 0.327 | 0.316 | 1.90 | 0.775 | 93.9% | 0.982 | 27.4° |
| v3 | S-neg | 0.334 | 0.323 | 2.11 | 0.748 | 92.1% | 0.987 | 28.2° |
| v3 | S-grav-noise | 0.330 | 0.316 | 1.92 | 0.774 | 93.9% | 0.982 | 27.6° |

| rule | difference | metric | pooled [95%] | Paterson | São Paulo | criterion 2 |
|---|---|---|---:|---:|---:|---|
| v2 | S-grav − S-rig | fill log-sd | +0.0132 [+0.0015, +0.0246] | +0.006 | +0.019 | S-rig better |
| v2 | S-grav − S-rig | R² | −0.0272 [−0.0488, −0.0039] | −0.018 | −0.035 | S-rig better |
| v2 | S-grav − S-neg | fill log-sd | +0.0045 [−0.0205, +0.0268] | −0.008 | +0.015 | not distinguishable |
| v2 | S-grav − S-neg | R² | −0.0008 [−0.0408, +0.0474] | +0.017 | −0.017 | not distinguishable |
| v2 | S-neg − S-rig | fill log-sd | +0.0087 [−0.0034, +0.0227] | +0.014 | +0.004 | not distinguishable |
| v2 | S-neg − S-rig | R² | −0.0264 [−0.0557, −0.0042] | −0.035 | −0.018 | S-rig better |
| v2 | S-grav-noise − S-rig | fill log-sd | +0.0130 [+0.0011, +0.0253] | +0.008 | +0.017 | S-rig better |
| v2 | S-grav-noise − S-rig | R² | −0.0273 [−0.0521, −0.0027] | −0.023 | −0.032 | S-rig better |
| v2 | S-grav − S-rig at beta 0.88 | fill log-sd | +0.0114 [+0.0006, +0.0221] | +0.005 | +0.017 | S-rig better |
| v2 | S-grav − S-rig at beta 0.88 | R² | −0.0232 [−0.0432, −0.0013] | −0.015 | −0.031 | S-rig better |
| v2 | S-grav − S-rig at beta 1.00 | fill log-sd | +0.0144 [+0.0021, +0.0264] | +0.007 | +0.021 | S-rig better |
| v2 | S-grav − S-rig at beta 1.00 | R² | −0.0301 [−0.0529, −0.0055] | −0.021 | −0.039 | S-rig better |
| v3 | S-grav − S-rig | fill log-sd | +0.0117 [−0.0012, +0.0237] | +0.008 | +0.015 | not distinguishable |
| v3 | S-grav − S-rig | R² | −0.0211 [−0.0441, +0.0040] | −0.015 | −0.027 | not distinguishable |
| v3 | S-grav − S-neg | fill log-sd | −0.0075 [−0.0360, +0.0186] | −0.017 | +0.001 | not distinguishable |
| v3 | S-grav − S-neg | R² | +0.0263 [−0.0162, +0.0763] | +0.044 | +0.011 | not distinguishable |
| v3 | S-neg − S-rig | fill log-sd | +0.0192 [+0.0039, +0.0369] | +0.025 | +0.014 | S-rig better |
| v3 | S-neg − S-rig | R² | −0.0474 [−0.0800, −0.0217] | −0.059 | −0.038 | S-rig better |
| v3 | S-grav-noise − S-rig | fill log-sd | +0.0120 [−0.0011, +0.0235] | +0.010 | +0.014 | not distinguishable |
| v3 | S-grav-noise − S-rig | R² | −0.0222 [−0.0461, +0.0041] | −0.021 | −0.023 | not distinguishable |
| v3 | S-grav − S-rig at beta 0.88 | fill log-sd | +0.0098 [−0.0022, +0.0210] | +0.006 | +0.013 | not distinguishable |
| v3 | S-grav − S-rig at beta 0.88 | R² | −0.0171 [−0.0384, +0.0059] | −0.012 | −0.022 | not distinguishable |
| v3 | S-grav − S-rig at beta 1.00 | fill log-sd | +0.0132 [−0.0003, +0.0256] | +0.009 | +0.016 | not distinguishable |
| v3 | S-grav − S-rig at beta 1.00 | R² | −0.0242 [−0.0482, +0.0024] | −0.018 | −0.030 | not distinguishable |

Criterion 6 reads **r.y better** for v2 and **not distinguishable** for v3; the negated-pose control (S-grav
against S-neg) is **not distinguishable** for both. The pattern is consistent across every row: g.y and g-.y,
which sit symmetrically on either side of r.y, are each worse than r.y by about the same amount, so the
depression that best predicts the apron's angular width on this gold is the rig-frame one. The isotonic
ceiling says the same without any rule: the best monotone function of depression explains R² 0.833 of the
GSV aprons' log width at r.y and 0.807 at g.y. For v2 the result holds at beta 0.88 and 1.0 and with click
noise; for v3 every one of those rows has the same sign and none is distinguishable.

Smaller things this arm settles:

- **Off against S-grav is model-conditional** (a noiseless modelled click and the full leak beta): it changes
  containment by −0.0044 [−0.0146, 0.0000] under v2 and by 0.000 under v3, so at these window sizes the
  centre shift rarely pushes an apron out. It restates the tilt study's S3 on another sample and is not
  evidence about real clicks, which rests on that study's endpoint C.
- Sizing at g.y moves 21.5% of v2 windows and 28.9% of v3 windows by more than 10%.
- Re-selecting the v3 width with the GSV ramps sized at g.y gives 5.8 m matched and 6.0 m at the band
  centre, the same as at r.y.
- The 430 Mapillary ramps have no pose; production files them as `no_pose` under `--tilt-correction`.

### 5.8 Shape (question 6)

The aprons are wide: apron width over height p50 is 3.333 pooled (Richmond 3.448, São Paulo 3.211, Paterson
3.055, Annapolis 3.618). A 3:2 window at the same width as a square one is two thirds as tall, and neither
shape comes close to the apron's own aspect: vertical fill p50 is 0.175 under v2 ×2.5 and 0.083 under v1-norm
×3.5, the square scale RampNet needed to contain 99.5% of Richmond's detection-mode aprons. The judge found framing barely moved with aspect (E7). No model
experiment varied shape, and none here measures gnomonic against equirect on pixels; question 6 is answered
for shape and for the tilt term, and stays open for projection.

### 5.9 Label types (question 8)

Each rule's window at the depression percentiles of every label type in the clamp census (production labels,
not gold). It shows where types differ; it validates no rule for them.

| label type | n | depression p10 / p50 / p90 / p99 | v2 window | v3 5.8 m window | option A ×2.2 window |
|---|---:|---:|---:|---:|---:|
| Crosswalk | 14,697 | 7.9° / 15.8° / 32.4° / 53.5° | 22.7° / 34.1° / 90.0° / 90.0° | 21.3° / 38.5° / 76.2° / 90.0° | 19.9° / 41.7° / 89.2° / 89.2° |
| CurbRamp | 149,837 | 7.8° / 14.8° / 28.0° / 44.7° | 22.7° / 32.2° / 90.0° / 90.0° | 21.3° / 36.2° / 66.7° / 90.0° | 19.9° / 38.5° / 88.6° / 89.2° |
| NoCurbRamp | 61,277 | 9.3° / 16.3° / 28.4° / 43.2° | 24.2° / 35.2° / 90.0° / 90.0° | 23.5° / 39.7° / 67.6° / 90.0° | 22.5° / 43.4° / 89.2° / 89.2° |
| NoSidewalk | 81,998 | 7.1° / 14.9° / 26.2° / 41.0° | 21.9° / 32.4° / 85.4° / 90.0° | 20.2° / 36.4° / 62.7° / 90.0° | 18.7° / 38.8° / 80.7° / 89.2° |
| Obstacle | 53,904 | 6.4° / 14.2° / 25.8° / 38.7° | 21.3° / 31.2° / 80.9° / 90.0° | 19.4° / 34.9° / 61.7° / 89.6° | 17.8° / 36.7° / 79.0° / 89.2° |
| Occlusion | 2,648 | 4.9° / 12.4° / 21.6° / 30.4° | 20.1° / 28.2° / 52.4° / 90.0° | 17.7° / 30.5° / 52.3° / 72.1° | 15.9° / 30.9° / 62.5° / 89.2° |
| Other | 1,666 | 6.9° / 16.3° / 31.3° / 52.1° | 21.8° / 35.3° / 90.0° / 90.0° | 20.0° / 39.9° / 74.0° / 90.0° | 18.4° / 43.5° / 89.2° / 89.2° |
| Signal | 3,544 | 3.3° / 9.0° / 19.8° / 36.9° | 18.9° / 23.9° / 44.9° / 90.0° | 16.3° / 23.1° / 48.0° / 85.9° | 14.4° / 22.0° / 55.6° / 89.2° |
| SurfaceProblem | 66,777 | 10.3° / 17.3° / 27.2° / 42.1° | 25.4° / 37.6° / 90.0° / 90.0° | 25.6° / 42.1° / 65.0° / 90.0° | 24.9° / 46.8° / 85.3° / 89.2° |

### 5.10 Camera heights (question 7)

| height | value | where | what it is |
|---|---:|---|---|
| geo-v1.5 GEO_CAM_H_M | 2.5 m | RampNet scripts/analysis/crop_window_eval.py | the flat-ground rule's one free parameter; it absorbs every error of the flat-ground model |
| geo-v1.5 at 1.7 m | 1.7 m | RampNet docs/crop_window_eval.md, round 2 | the height at which geo-v1.5 reconciles with the Richmond gold: a fitted value, not a measurement |
| V3_CAMERA_HEIGHT_M | 2.34122 m | sidewalk_panorama_tools.ps_math.BLEND_CAMERA_HEIGHT_M | the lle #3 cotangent blend's calibration constant, fitted with the blend |
| auto-labeler issue 40 | 2.21 m | sidewalk-auto-labeler issue 40 | a measured median GSV camera height |

The identification limit is exact: v3's blend distance is proportional to the camera height at every
depression, so its window depends on W/h alone. Scaling W with h reproduces every v3 window to 2.8e-14°.
So on this gold a per-city context width and a per-provider camera height are one degree of freedom, and the
four heights cannot be reconciled by fitting; they are four different quantities.

## 6. Answers to the issue's questions

1. **Who is the default for?** Open. No experiment in any of the three repos scores a human consumer and a model
   consumer on the same crops. The store already records the rule per crop, so a rule per consumer is
   possible without a format change; whether it is wanted is a product decision.
2. **Model accuracy against the rule.** Open in PR-A, quoted from RampNet: at fixed FOVs, mAP control 0.354457,
   fov25 0.349421, fov50 0.319403, fov90 0.280352 (reproduced from RampNet's logits in the pilot). PR-B
   re-reads those arms by the rules' own windows; no offline analysis can say what a model trained on v2-,
   v3- or option A-sized crops would score.
3. **A second judge.** Open: none was available, and the first judge's raw verdicts are missing. The snapshot
   of `PLAN-crop-clarity.md` (now in this repo) is the only record: 132 and 112 forced choices, 144 absolute
   judgements, "too tight" above fill 0.49 (95% CI 0.46-0.54), 68% clearing at ×2.5, and framing quality
   74% at 1:1, 3:2 and 2:1 alike.
4. **Out of sample.** Answered for clearing and the band (section 5.4); open for ×2.5 itself.
5. **Annapolis.** Section 5.6: its ramps subtend the largest angles, the per-provider and per-city band-centre
   widths differ, and the v2-matched widths do not, so it is the ramps (and possibly the Mapillary imagery,
   which this gold cannot separate from the city), not the rule's shape. Option A clears it best (48.9% held
   out), but no rule's held-out interval falls below the floor.
6. **Geometry.** Shape: section 5.8. Tilt: section 5.7, and it is the surprise of this report. Projection:
   open.
7. **One camera height.** Section 5.10: not reconcilable on this gold, by construction.
8. **Other label types.** Section 5.9 shows where their windows would land. All gold is curb ramps; a default
   for other types rests on no measurement here, and the evidence it would need is extent gold for them.

## 7. Decisions

| Decision | Recommendation | For | Against | Confidence |
|---|---|---|---|---|
| D1, flip to v3 | Do not flip yet | v3 beats v2 on all three discriminating metrics, out of sample in sign | v3-depth is unscored on the GSV gold (the candidate set is incomplete), option A beats v2 just as clearly, no model metric (PR-B), human metrics in sample | low (criterion 7 caps it) |
| D2, 5.8 m or 6.0 m | Keep 5.8 m unless the band centre is preferred as a criterion | Criterion-only: each width is inside the other's replicate range | None from data | not a data question |
| D3, the flag name | No change | The code as it stands (`--sizing-rule {v2,v3,v3-depth}`, `--tilt-correction`) | No experiment bears on it | not a data question |
| D6, per-city width | Supported for a provider split; do not ship until the floor question is settled | Mapillary − GSV and Annapolis − Richmond band-centre widths differ | No held-out city misses the floor; n = 1 city per cell | moderate |
| D7, v3 or option A | v3, narrowly | v3 tracks the apron better (R², all four cities, holds out of sample) | Dispersion not distinguishable; option A clears more (77.4% against 75.2% pooled) | high on R² alone; the rest not distinguishable |
| #197, size at stored or corrected y | Sizing at the corrected (rig) y is what this gold prefers for v2 | Criterion 6: r.y better for v2, same sign for v3; the control and the isotonic ceiling agree | The gold points are pixel-true detections, not PS clicks; the click is modelled; the reason is unexplained | moderate; Jon's call |

**The #197 sizing note.** The plan pre-stated a reading only for "not distinguishable" (keep sizing at the stored
y on physical grounds). The outcome is the opposite of the physical prior: on this gold the rig-frame depression
predicts the apron better than the modelled gravity-frame depression. One untested explanation is that the
road, the sidewalk and the car tilt together (a street's grade and cross-slope tilt the rig, and the ground
under a nearby ramp shares that slope), so distance follows the rig frame more closely than gravity. If that
is right, `--tilt-correction` should size at the corrected y as well as centre on it. It is not adopted here:
production keeps sizing at the stored y (J7) until Jon decides, and a test on real PS clicks (the tilt study's
S4 had 14 pairs where its design needed 30) would settle it.

**Sequencing for #84.** Nothing here asks for a re-cut. If D1, D7, D6 or the #197 sizing change is taken, it
should ride #84's single pass with the tilt correction and the metadata re-export, after v3-depth has been
scored on the GSV gold, so that the rule is chosen once.

## 8. Deviations and wrong turns

- **The plan's physical prior for the tilt arm was wrong on this gold.** The criteria did not pre-state what
  "r.y better" would mean for #197; the reading above is written after the result and is labelled so.
- **The planner's pilot and this report quote the tilt shift differently.** The pilot used interpolated
  percentiles (`np.percentile`); this study uses nearest rank, so São Paulo's |dy| p90 reads 2.582° in the
  artifact against the pilot's 2.64°, and the share of windows moving by more than 10% is pooled over both
  cities and measured on cut integer widths (21.5% and 28.9%) where the pilot reported each city's angles.
- **The `PLAN-crop-clarity.md` snapshot is not verbatim.** The plan asked for a verbatim copy; the file holds an
  absolute local path, which committed files here must not, so that one path is replaced by `<RampNet>`. The
  provenance records both hashes and the substitution, and a gated test rebuilds the snapshot from the source.
- **The run was first 69 s**, over the plan's 60 s line for regenerating intervals in CI. Caching window
  widths (they depend only on rule, constant, sizing y and pano size) brought it to about 33 s with every
  number unchanged, so CI regenerates every block.
- **The first draft of a reproduction test compared depressions after a round trip through pixels**, which
  differs in the last bit; the test now reads the depression off y as the v3 study does.

## 9. Open questions and deferred follow-ups

| Follow-up | Unblocked by |
|---|---|
| v3-depth on the GSV gold | Fetching depth artifacts for the 70 GSV gold panos (network; São Paulo's are not on the pano store) |
| Why the rig-frame y sizes better | A per-pano check of ground slope against the rig pose, or the depth planes of those panos |
| A tilt arm on real PS clicks | More PS labels on gold panos |
| Tagger trained on v2, v3 and option A crops, three seeds; fov90 at native resolution and masked | GPU time |
| A second judge, and whether 0.49 and the ×2.0 / ×3.0 split reproduce | A person |
| The raw blind-round exports | Jon, if they still exist |
| Extent gold for a fifth city and for other label types | An annotator |
| Whether a tilt-corrected run should treat detection-submitted labels differently (their points are pixel-true) | Jon |

## 10. Reproduce, and the tests that pin it

```bash
# From the committed extract only (about 33 s):
python reports/scripts/crop_sizing_consolidation.py study \
    --write reports/data/2026-11-22-crop-sizing-consolidation.json \
    --per-ramp reports/data/2026-11-22-crop-sizing-consolidation/per_ramp.csv.gz
python reports/scripts/crop_sizing_consolidation.py tables      # every table above
# The extract itself, from read-only checkouts:
python reports/scripts/crop_sizing_consolidation.py extract --rampnet <RampNet> --labeler <sidewalk-auto-labeler>
```

`tests/test_crop_sizing_consolidation_report.py`: the study's logic on synthetic ramps (the tilt model against
T(b), the round trip, roll wrapping, the paired bootstrap on panos with unequal ramp counts, criteria 2 and 6,
the power step without apron fields); the extract and its hashes; the reproduction of the v2, v3 and RampNet
figures; the artifact regenerated from the committed inputs, intervals included; every figure's population;
and every table and number above against the artifact. Code change: none outside `reports/` and this
report's test.
