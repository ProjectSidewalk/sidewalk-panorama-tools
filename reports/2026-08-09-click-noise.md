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

Pedestrian signals — small, compact, unambiguous targets — have the tightest azimuth by 2×;
crosswalks — elongated along the road — the loosest. The estimator is measuring object geometry's
effect on placement, i.e. the right phenomenon. Depression bands move σ_el only mildly
(0.44°/0.48°/0.55° for <5°/5–15°/>15° below horizon; the far-field band has only 75 pairs — far
objects rarely get duplicate-labeled). Restricting to validated-correct labels (≥ 2 votes,
agree > disagree; 3,813 pairs) leaves σ essentially unchanged — the estimate is not carried by
labels validators would have culled.

## What this means downstream

* **The 0.5° placement threshold** (consumer-requirements survey) **equals ≈ 1σ of between-user
  noise** at the conservative radius, ≈ 1.7σ of the core. A systematic placement bias at
  threshold scale is therefore detectable with modest samples, but single labels can't be judged
  individually at that scale — only distributions can.
* **Power for the pre-registration** (per-axis, mean-bias test, α = .05 two-sided, 80% power):
  with σ = 0.5°, detecting a δ = 0.25° mean bias needs **n ≈ 32** per stratum; δ = 0.5° needs
  **n ≈ 8**. The planned ~50-per-stratum gold set is comfortably powered for threshold-scale
  placement bias; exact arithmetic goes in the pre-registration.
* **This σ is between-user**, so it includes per-user systematic convention differences (base vs
  centre of a ramp). It is an upper bound on within-user click jitter and exactly the right
  number for "how well does one stored label localize its object."
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
