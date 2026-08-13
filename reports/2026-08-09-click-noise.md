# Placement noise between independent users: σ ≈ 0.3–0.6° per axis, priced for free

**2026-08-09** · Phase 1 desk study for the cropper work package (#54, #32) · prices the
pre-registration's power calculation

> **Reproduce offline:** `pytest tests/test_click_noise_study.py` (machinery, 5/5 mutants killed,
> plus the findings pinned against the committed JSON). **From source:**
> ```bash
> python reports/scripts/fetch_rawlabels.py
> python reports/scripts/click_noise_study.py reports/scripts/.cache/rawlabels \
>     --fetched <today> --write reports/data/<date>-click-noise-summary.json
> ```

## The question

The #54 placement study will compare stored label positions against gold annotations. Any bias it
finds must be judged against the noise two honest annotators already exhibit on the same object —
without that floor, "labels are off by X" is uninterpretable. Production data prices this floor
for free: when two users independently label the same physical object on the same pano, their
angular spread *is* end-to-end placement noise at identical viewpoint and imagery.

## Method

Same six-city rawLabels corpus as the [era replay study](2026-08-09-era-replay-study.md)
(fetched 2026-08-09; 438k labels; stored `pano_x/pano_y` anchors, which that study established as
click-time truth). Within each (pano, label type): cluster labels by angular proximity (azimuth
seam-wrapped, cos(elevation)-scaled), drop same-user repeats, take every cross-user pair. Robust
per-axis σ from pair differences: σ = 1.4826·median|Δ|/√2 (a pair difference of iid noise is
N(0, 2σ²); the median keeps the misplacement tail from inflating a click-noise estimate).
Full estimator properties are pinned in `tests/test_click_noise_study.py`, including recovery of a
known injected σ and bounded sensitivity to a planted 5% outlier mass.

## Numbers

At the primary radius 1.5°: **13,359 pairs across 9,916 clusters** — σ_az **0.573°**, σ_el
**0.507°**. But the radius sweep is the finding that matters:

| clustering radius | σ_el |
|---|---|
| 0.75° | **0.299°** |
| 1.0° | 0.392° |
| 1.5° | 0.507° |
| 2.0° | 0.599° |

No plateau. The pair population is a **mixture**: a tight click-noise core (~0.3°/axis) plus
genuinely distinct nearby objects (a corner's two curb ramps, segments of one crosswalk) that leak
in as the radius grows. So the honest statement is a range — **core ≈ 0.3°, conservative ≈ 0.5° at
1.5°** — and any consumer of one number must carry its radius with it.

Face-validity checks, by type (radius 1.5°):

| type | pairs | σ_az | σ_el |
|---|---|---|---|
| Signal | 127 | **0.203°** | 0.507° |
| Obstacle | 847 | 0.450° | 0.599° |
| CurbRamp | 9,314 | 0.571° | 0.484° |
| SurfaceProblem | 674 | 0.548° | 0.507° |
| NoCurbRamp | 1,945 | 0.660° | 0.530° |
| Crosswalk | 277 | **0.616°** | 0.438° |
| NoSidewalk | 173 | **0.706°** | 0.425° |

Pedestrian signals — small, compact, unambiguous targets — have the tightest azimuth by 2×. The
loosest three (NoSidewalk 0.706°, NoCurbRamp 0.660°, Crosswalk 0.616°) are exactly the classes with
no compact object to centre on: two mark an *absence* along a stretch of street, and the third is
elongated along the road. σ_el does not order the same way, which is the point — azimuth spread
tracks how extended the target is horizontally. The estimator is measuring object geometry's effect
on placement, i.e. the right phenomenon. Depression bands move σ_el only mildly
(0.44°/0.48°/0.55° for <5°/5–15°/>15° below horizon; the far-field band has only 75 pairs — far
objects rarely get duplicate-labeled; the three bands account for all 13,359 pairs, i.e. no cluster
in this corpus sits above the horizon). Restricting to validated-correct labels (≥ 2 votes,
agree > disagree; 3,813 pairs) leaves σ_el **bit-identical** at 0.5068° and σ_az within 1%
— the estimate is not carried by labels validators would have culled.

That bit-identity is real rather than a copy-paste, and worth spelling out because it looks like
one. `d_el` is a difference of integer pano rows, so on the 8192-tall panos that carry 90% of the
corpus it is quantised to 180/8192 = **0.0220° per pixel**. Both medians land on the same atom —
exactly **22 px** — so the robust σ, which is a scaled median, is identical to the last bit. The
practical consequence is that σ_el is only resolvable to about ±0.022°, well inside the spread the
radius sweep already shows. `tests/test_click_noise_study.py` asserts the pixel-atom property
directly, and pins the validated-only *pair counts* as well, since two equal σ values on their own
cannot distinguish this from the sensitivity analysis having been run on the wrong frame.

### The floor the placement study will actually use is the referent-filtered one

#54 measures displacement against gold, so it can only run on labels that have a located referent to
be displaced *from*. `rawlabels.has_located_referent` drops Crosswalk, NoSidewalk, Occlusion and
brick-tagged SurfaceProblems — **100,636 of 436,348 labels, 23.1%**. Restricting the estimator to the
remaining **335,712** (`comparable_only` in the committed JSON) gives **σ_az 0.570°, σ_el 0.507°** over
12,904 pairs, against 0.573° / 0.507° over all labels. A 23% cut in labels moves the floor by 0.003° in
azimuth and not at all in elevation: the excluded arms are large in labels and small in *pairs* — 455
of 13,359, of which Crosswalk contributes 277 and NoSidewalk 173 — even though they are the two
loosest rows in the table above.

It matters for reading the artifact rather than for the number. Matched mode (`--pano-list`) is
computed on the referent-filtered frame and every clustered figure on the unfiltered one, so the JSON
now carries a `populations` block naming which figure sits on which frame, and the CLI prints each σ
with its label count. Comparing a σ from one against a σ from the other is a comparison across
corpora, and the two used to be printed in one column with nothing recording the difference.

## What this means downstream

* **The 0.5° placement threshold** (consumer-requirements survey) **equals ≈ 1σ of between-user
  noise** at the conservative radius, ≈ 1.7σ of the core. A systematic placement bias at
  threshold scale is therefore detectable with modest samples, but single labels can't be judged
  individually at that scale — only distributions can.
* **Power for the pre-registration** (per-axis, mean-bias test, α = .05 two-sided, 80% power):
  with σ = 0.5°, detecting a δ = 0.25° mean bias needs **n ≈ 32** per stratum; δ = 0.5° needs
  **n ≈ 8**. The [pre-registration](2026-08-09-crop-priors-prereg.md) §5 is the binding version and
  works from σ_diff (label ⊕ gold), which lands at n = 44 for δ = 0.25° against ≈ 160 labels per
  depression band — comfortably powered for threshold-scale bias.
* **This σ is between-user**, so it includes per-user systematic convention differences (base vs
  centre of a ramp). It is an upper bound on within-user click jitter and exactly the right
  number for "how well does one stored label localize its object."
* **Pairs are not independent observations.** A cluster with *k* distinct users contributes
  *k*(*k*−1)/2 pairs, so an object that attracted many labels — typically an ambiguous one — carries
  quadratically more weight in the median. Mild here (13,359 pairs over 9,916 clusters, 1.35 each),
  and σ is a median rather than a mean, so the effect is small and in the conservative direction.
  But it means **n_pairs is not an effective sample size**: no confidence interval is quoted on σ
  for that reason, and none should be derived from 13,359. A one-pair-per-cluster sensitivity would
  settle the residual, and is cheap to add on the next run.
* **Caveat for Phase 2 sampling**: duplicate-labeled objects are not a random sample — they favor
  well-audited streets and conspicuous objects. Fine for a noise floor; do not reuse the cluster
  population as a study corpus without reweighting (the methodology review's population-reweighting
  step already covers this).

## Where everything lives

| Artifact | Path |
|---|---|
| Summary numbers (committed) | `reports/data/2026-08-09-click-noise-summary.json` |
| Analysis | `reports/scripts/click_noise_study.py` (loader shared: `rawlabels.py`) |
| Machinery tests + findings pins | `tests/test_click_noise_study.py` |

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-fable-5). The radius sweep was
added after the single-radius σ looked suspiciously tidy; the absence of a plateau is the finding.*
