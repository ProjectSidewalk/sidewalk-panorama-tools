# Clamp census: 19% of labels get the same fixed crop, and 90% get a resolution-inflated one

**2026-08-09** · Phase 1 desk study for the cropper work package (#32) · extends the
[2026-08-07 pano_y histogram](2026-08-07-cbk-tile-resolution.md)

> **Reproduce offline:** `pytest tests/test_clamp_census.py` (replica pinned against the real
> CropRunner source via ast, plus findings pinned against the committed JSON). **From source:**
> ```bash
> python reports/scripts/fetch_rawlabels.py
> python reports/scripts/clamp_census.py reports/scripts/.cache/rawlabels \
>     --fetched <today> --write reports/data/<date>-clamp-census.json
> ```

## The question

Before #32 proposes a replacement sizing formula, quantify how the deployed
`predict_crop_size` actually behaves on production geometry: how often its [50, 1500] px clamps
saturate (a clamped crop stops responding to distance), how often the wanted crop runs off the
frame, and how much its resolution dependence — the distance term is linear in *pixels* from the
horizon row, fit in the 13312×6656 era — matters on today's dims mix.

Corpus: the six-city rawLabels fetch of 2026-08-09, 436,348 labels after dropping rows with
missing/out-of-frame geometry (the loader and its provenance are shared with the
[era replay study](2026-08-09-era-replay-study.md)).

## Numbers

**Saturation** (committed JSON has the full type/city breakdowns):

| | share | count |
|---|---|---|
| crop = 1500 px (near clamp) | **19.25%** overall — 6.8% (Signal) to 25.9% (Crosswalk); 7.8% (Newberg) to 31.7% (Amsterdam) | 83,977 |
| crop = 50 px (far clamp) | **0%** — structurally unreachable | 0 |
| wanted crop off bottom edge | **0%** | **0** |
| wanted crop off top edge | **0%** | **0** |

A fifth of all labels — a quarter of crosswalks — get an identical 1500 px crop regardless of
their distance: every depression steeper than the clamp onset collapses to one framing. The far
clamp and edge truncation, by contrast, are non-issues: **the #77 y-shift machinery is safety
plumbing, not a hot path** (measured exposure exactly zero), so the placement study does not need a
shifted-crop stratum.

Those three zeros are structural, not a lucky sample, and `clamp_onset_depression_deg` /
`bottom_truncation_onset_depression_deg` say why — the onsets sit far outside the geometry labels
actually occupy (corpus depression p99 = **43.5°**):

| pano height | 1500 px clamp onset | 50 px clamp onset | bottom-truncation onset |
|---|---|---|---|
| 8192 (90.0% of labels) | 22.2° | −81.0° (above horizon) | 73.5° |
| 6656 (9.7%) | 27.4° | −99.7° (above horizon) | 69.7° |
| 3328 | 54.7° | −199.4° | 53.4° |
| 1664 (0.2%) | never (109.5°) | −398.8° | 62.8° |

The far clamp would need a label sighted *above* the horizon by 81°, which no click can produce —
hence "structurally unreachable" rather than "rare". Bottom truncation needs 53–74°, a ~30° margin
over the corpus p99.

**Reconciled against the #77 crop-geometry census.** That census
([2026-08-10](2026-08-10-crop-geometry-review.md)) reports **2** labels needing a vertical shift
where this one reports **0**, which looks like a contradiction and is not. It runs over 436,350
labels — this census's 436,348 plus the two corrupt negative-`pano_y` rows that
[`load_city`](scripts/clamp_census.py) drops — and those two rows *are* its entire vertical-shift
population (Seattle `231546` at `pano_y = -720` and `233419` at `-355`, the same pair the
[era replay study](2026-08-09-era-replay-study.md) named). Read together the statement is stronger
than either alone: **among sound labels the vertical-shift exposure is exactly zero, and every label
that would have needed one is a row #77 now rejects outright.** Pinned in
`tests/test_clamp_census.py::TestCrossCensusReconciliation`.

> **Correction, 2026-08-10 (pre-merge review).** The truncation row previously read "0.000% (9
> labels corpus-wide)" and the footer below quoted the same 9. The committed JSON has
> `truncated_bottom_pct: 0.0` exactly, and zero in every `by_city` and `by_label_type` cell — 9
> labels would be 0.00206%. The 9 came from an earlier run that did not survive into the committed
> artifact, so the report has been corrected to the data. The conclusion (retire the shifted-crop
> stratum) is unchanged and is now backed by the analytic onsets above rather than by a count.
> `test_edge_truncation_is_a_non_issue_in_production` previously asserted `< 0.05`, which passed for
> both numbers; it now asserts exact zero.

**Resolution dependence** (crop size at the label's served height vs the same depression in the
6656 frame the formula was fit in):

| served pano height | share of labels | crop ratio vs fitted frame (p50) |
|---|---|---|
| 8192 | **90.0%** | **1.198** |
| 6656 | 9.7% | 1.000 |
| 1664 | 0.2% | 0.591 |

Ninety percent of the corpus gets crops ~20% larger than the formula's fitted behaviour, purely
because Google now serves taller panos; the rare low-res pano gets crops 41% *smaller* for
identical geometry. Any consumer comparing crops across resolutions inherits a 2× systematic.

**Read those ratios as a floor, not as the size of the effect.** They are computed on *deployed*
crop sizes, so the [50, 1500] clamp is inside them, and the clamp is precisely what absorbs the
resolution dependence once it engages — two clamped labels have a ratio of exactly 1.0 no matter how
differently the formula would have sized them. At the corpus depression quantiles:

| corpus depression | crop @8192 | crop @6656 | deployed ratio | ratio the formula *wanted* |
|---|---|---|---|---|
| p10 = 7.8° | 364 px | 336 px | 1.085 | 1.085 |
| p50 = 15.4° | 624 px | 493 px | 1.266 | 1.266 |
| p90 = 27.3° | 1500 px (clamped) | 1490 px | **1.006** | **7.10** |
| p99 = 43.5° | 1500 px (clamped) | 1500 px (clamped) | 1.000 | undefined — see below |

So the census's committed `crop_ratio_vs_6656_p50 = 1.198` is the honest *deployed* number, while the
underlying formula is far more resolution-dependent than 20% in the top decile. Three of its
boundaries all scale as 1/height, which is the single mechanism behind every row above:

| boundary (from the deployed constants) | h = 8192 | h = 6656 |
|---|---|---|
| 1500 px clamp engages | 22.2° | 27.4° |
| modelled distance reaches **0 m** | **28.6°** | 35.1° |
| the formula was fit here | — | ✔ |

Past 28.6° of depression the deployed model puts a label at *zero metres* on an 8192-tall pano — a
tenth of the corpus, since p90 = 27.3° — while the same geometry on the frame it was fit in still
returns 3.7 m. That is the cleanest single statement of why #32's replacement must consume
depression rather than pixels.

**Geometry priors** the sizing study needs: label depression p10/p50/p90/p99 =
7.8°/15.4°/27.3°/43.5° — the corpus straddles the 11.25° cotangent-blend boundary, so both
regimes of the #32 candidate-B distance model carry real mass. Deployed-vs-blend distance
disagree by 0.69 m (p50) / 2.98 m (p90) per label.

## What this hands #32

* Replacement candidates must be **resolution-independent** (consume depression, not pixels) — the
  census shows the pixel-linear form is a ≥20% systematic on 90% of labels, and that 20% is only the
  deployed floor: past 28.6° of depression the model's distance term reaches zero metres on an
  8192-tall pano and the clamp takes over from geometry entirely.
* **Follow-up for the next census run:** `resolution_dependence()` reports the deployed (clamped)
  ratio only. Adding the pre-clamp ratio and a per-height `clamp_1500_pct` would let the report quote
  the effect and its masking from the committed JSON rather than from analytic onsets computed
  alongside it. Not backfilled here — rawLabels is a moving target and re-fetching would re-date the
  whole 2026-08-09 set.
* The near clamp is where sizing information dies today; 19% of labels are the immediate
  beneficiaries of a formula that keeps responding at close range.
* The depression quantiles above define the evaluation grid the study should weight by — not a
  uniform sweep.

## Where everything lives

| Artifact | Path |
|---|---|
| Census numbers (committed) | `reports/data/2026-08-09-clamp-census.json` |
| Analysis | `reports/scripts/clamp_census.py` |
| Replica-fidelity + findings pins | `tests/test_clamp_census.py` |

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-fable-5); corrected 2026-08-10
(claude-opus-5[1m]). The bottom-edge truncation rate was expected to justify a shifted-crop study
stratum; measuring it at zero in 436k retired that stratum instead — and working out the onset
analytically afterwards showed the zero was never going to be anything else.*
