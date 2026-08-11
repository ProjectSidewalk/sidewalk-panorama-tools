# The covariate that separates a capture-side projection error from tilt and from placement

**2026-08-11** · prompted by [SidewalkWebpage#4842](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4842)
after the [record-staleness study](2026-08-10-record-staleness-validate.md) cleared its two example
labels · amends the [crop-priors pre-registration](2026-08-09-crop-priors-prereg.md) §7 before any
Phase 2 annotation exists

> Links to the record-staleness report resolve once **PR #80** merges; it is the immediate
> predecessor of this study and is still open at the time of writing.

> **Reproduce offline from committed bytes:**
> ```bash
> pytest tests/test_offaxis_covariate.py            # machinery + the findings, pinned
> ```
> **Reproduce from the source** (rawLabels is a moving target; the #4842 repair migration will
> rewrite record fields when it lands):
> ```bash
> python reports/scripts/fetch_rawlabels.py         # -> reports/scripts/.cache/rawlabels/*.csv
> python reports/scripts/offaxis_covariate.py reports/scripts/.cache/rawlabels \
>     --fetched <date> --write reports/data/<date>-offaxis-covariate.json
> ```

## Summary

#4842 ("labels do not always look on target") lost its leading explanation when the record-staleness
study found both of its example labels replay `exact` — their stored records reproduce their own
`pano_x`/`pano_y` at 0 px. What survives splits three ways, and the three live in **two different
frames**:

| | mechanism | visible in Validate | visible in stored `pano_y` |
|---|---|---|---|
| **i** | the click really was low — human placement and/or rig tilt (`pano_y` carries no tilt term) | yes | **yes** |
| **ii** | the render lost an offset — e.g. the 5 px vertical fudge Validate's projection dropped in the Jan 2026 pano-code consolidation (`865b5b8a8`) | yes | **no** |
| **iii** | capture-side projection error — the client's canvas→pano math is off | **no** (Validate renders from the same record, so it draws the marker exactly where the user clicked) | **yes** |

The #54 placement study reads stored `pano_x`/`pano_y` against gold annotations, so **it sees (i) and
(iii) and is structurally blind to (ii)**. Row (iii) is the one that matters most to this repo and the
one #4842 can never surface: it mis-places every crop we cut while Validate looks perfectly on target.

This study supplies what separates (i) from (iii) at zero annotation cost — the click's angular offset
from the viewport centre — and establishes the two facts that make it usable. Over **433,866 eligible
labels of 438,410** in six cities: **95.08% of the covariate's variation survives the
pre-registration's depression-band fixed effects** (sd 8.34° → 7.93° within band, correlation with
depression 0.319), holding at 93.5–96.4% in every era. It is therefore identified against the strata
Study 1 already fits, and no new forced stratum is needed. The Explore viewport's **pitch floor of
−35° is hard** (minimum observed pitch is exactly −35.0000) and carries **10.18% of eligible labels,
rising to 49.2% of the >30° depression band**. Both #4842 examples sit at that floor, clicked
**15.75°** and **23.47° above** the viewport centre — chicago-il 30652 beyond the 5th percentile of
every band, teaneck-nj 14955 beyond it in the >30° band only.

## The question

The pre-registration's Study 1 estimates mean placement bias per depression band and regresses
elevation error on rig-tilt terms. Suppose it reports a band-graded downward bias. Three mechanisms
produce that, and as registered the study cannot tell them apart:

* rig tilt — bearing-driven, and the tilt regression already tests it;
* a canvas↔pano projection constant — grows with the click's distance from the canvas centre and
  vanishes at the centre;
* human placement behaviour — roughly constant in both.

The middle one had no instrument. It also has a property that makes it easy to overlook: it is
invisible in Validate by construction, so the interface where the problem was noticed is the one
interface that cannot show it.

Two things had to be true for an off-axis covariate to be worth registering, and neither was known:

1. **Is it identified?** Study 1 carries depression-band fixed effects. A covariate collinear with
   depression is absorbed by them and estimates nothing.
2. **Is it robust to the record bug — and to its repair?** The covariate is computed from the stored
   viewport record, which is exactly what the 2023-03-29 → 2024-09-25 staleness bug corrupted and
   what #4842's repair migration is about to rewrite.

## Method

**Corpus.** The six-city rawLabels cache the era study fetched 2026-08-09 (438,410 labels: seattle-wa,
cdmx, columbus-oh, amsterdam, newberg-or, oradell-nj) — the same provenance the pre-registration's §3
corpus is drawn from, so the identification claim is about the population Study 1 will actually sample.

**The covariate.** `offaxis_covariate.offaxis_offsets` runs the verbatim production projection
(`pov_replay.pov_if_centered`) to get the click's own POV, and reports its offset from the viewport
axis: `vertical = pitch − pov_pitch` (positive = clicked below the centre) and the great-circle
`radial` separation. Vertical is what the elevation endpoint consumes; radial is reported because a
radially symmetric error (a wrong fov) shows in it while a purely vertical one (a lost pixel fudge)
does not.

**Eligibility: `exact_y`, not `exact`.** Rows are kept when the stored record's vertical half
reproduces stored `pano_y` exactly. This is deliberately *not* the record-staleness study's `exact`
class (both axes), because the covariate is **provably heading-free**: in `pov_if_centered`,
x² + y² collapses to A² + B² with A = f·cos p₀ − dv·sin p₀ and B = du·sgn, and the heading cancels
exactly — pinned to floating-point equality in `tests/test_offaxis_covariate.py`. So:

* the `x_only` staleness class — **58% of all record misses**, stale only in the viewport heading —
  is harmless here, and restricting on both axes would have discarded **13,485 eligible rows** to
  guard against a field the covariate cannot read;
* the restriction is **exactly invariant to #4842's repair migration**. That migration rotates
  heading for `x_only` rows and leaves pitch/zoom/canvas alone; every class whose repair touches
  canvas or zoom (`dpr2`, `zoom_desync`, `multi_field`, `xy_small`) fails `exact_y` and is excluded
  here anyway.

**A geometric property worth stating**, because it decides what the covariate *is*: on the canvas
vertical centerline the vertical offset equals **−atan(dv/f)** exactly, independent of viewport pitch
— a click there is a pure rotation about the camera's horizontal axis, so the viewport aim divides
out. The covariate is thus a canvas-frame quantity rather than a mixture of canvas position and where
the user happened to be looking, which also keeps it close to orthogonal to the pitch-floor covariate
registered beside it. Away from the centerline the sphere geometry couples the two; both behaviours
are pinned.

## Numbers

### 1. Identification against the pre-registered strata

The band means are what a band fixed effect removes, so the residual spread is what a coefficient
could be estimated from.

| | sd overall | sd within band | surviving | corr. with depression |
|---|---|---|---|---|
| pooled (n = 433,866) | 8.34° | 7.93° | **95.08%** | 0.319 |
| legacy (n = 146,435) | 8.60° | — | 96.4% | — |
| mid (n = 197,594) | 7.85° | — | 94.6% | — |
| post-179 (n = 89,837) | 8.86° | 8.28° | 93.46% | 0.384 |

The study corpus spans all three eras by design, so a covariate identified only post-179 would not
serve the strata the pre-registration fits. It is identified in all of them. The three era rows come
from `by_era_identification`; the artifact used to carry the post-179 row a second time under
`identification_post179`, byte-identical because it selected the same rows by the same predicate, and
that key is gone.

### 2. Spread and floor exposure per Study 1 band

| band | n | off-axis vertical p5 / p50 / p95 | sd | radial p95 | at pitch floor |
|---|---|---|---|---|---|
| <5° | 12,991 | −23.3 / −4.2 / 3.7 | 8.23 | 35.5° | 0.4% |
| 5–15° | 193,807 | −21.3 / −3.7 / 6.7 | 8.27 | 36.2° | 3.0% |
| 15–30° | 197,170 | −16.0 / −3.9 / 9.8 | 7.71 | 35.8° | 12.0% |
| >30° | 29,898 | −3.9 / 3.8 / 18.2 | 6.87 | 33.1° | **49.2%** |

Every band spans ~20–27° of off-axis offset, which is the within-stratum contrast the coefficient
needs. Floor exposure is the opposite: sharply graded, from 0.4% to nearly half, concentrated in the
band where deployed crop sizing and lat/lng estimates are already weakest. That grading is why the
floor is registered as its own covariate rather than folded into depression.

### 3. What a canvas-pixel error is worth, in the units consumers use

| zoom | fov | n | corpus share | 5 px | 20 px |
|---|---|---|---|---|---|
| 1 | 89.75° | 279,344 | 64.385% | **0.623°** | 2.493° |
| 2 | 53.00° | 91,548 | 21.101% | 0.368° | 1.472° |
| 3 | 27.68° | 62,694 | 14.450% | 0.192° | 0.769° |
| *off-ladder* | 29.4–88.3° | 280 | 0.065% | — | — |

The last row is the one an earlier draft left out. The viewer's zoom control has three stops, but
stored `zoom` is a float and 280 eligible rows carry a **fractional** value strictly between them (49
distinct values, e.g. 1.6818, 2.4689) from clients that interpolated it continuously. `get_3d_fov` is
continuous, so those rows have a well-defined fov — what they lack is a rung. Without the row the
three shares sum to 99.94% in a table that reads as a census, which is how a future re-fetch moving a
real share of the corpus off-ladder would go unnoticed; the artifact now carries `zoom.other`, and a
test asserts the four counts sum to the eligible total exactly.

Against the consumer survey's **0.5° placement threshold**, a 5-px canvas-frame error is
supra-threshold at zoom 1 — where two thirds of the corpus sits — and sub-threshold at zoom 3. That
monotonicity is itself a discriminating signature: a canvas-pixel-constant error scales with fov,
while rig tilt and placement behaviour do not.

### 4. The #4842 specimens

| | teaneck-nj 14955 | chicago-il 30652 |
|---|---|---|
| stored record | (298.25°, **−35°**, zoom 1) @ canvas (451, 142) | (320.5°, **−35°**, zoom 1) @ canvas (361, 83) |
| viewport at pitch floor | yes | yes |
| vertical off-axis | **−15.75°** (above centre) | **−23.47°** (above centre) |
| radial off-axis | 20.30° | 23.47° |
| beyond the p5 of band | >30° only | **all four** |

Neither specimen is in this study's six-city corpus, so both rows are computed from the records above
rather than joined — the records, the computed offsets, and the band membership are committed in
`reports/data/2026-08-11-offaxis-covariate.json` under `specimens` and pinned in
`tests/test_offaxis_covariate.py`, so this table reproduces from the repo like every other number here.

Both are at the floor and both are clicked far off-axis: chicago-il 30652 sits beyond the 5th
percentile of *every* band, and teaneck-nj 14955 beyond the 5th percentile of the >30° band. (An
earlier draft of this report claimed both were beyond p5 of every band. They are not — teaneck's
−15.75° is inside p5 for the <5°, 5–15° and 15–30° bands, whose p5 runs −23.3 / −21.3 / −16.0°.)
This is not evidence for mechanism (iii); it is the reason the covariate is worth having. The labels
that made the issue visible are drawn from the covariate's tail, not its middle, which is exactly
where the three mechanisms diverge most.

## What this changes

An appended amendment to the pre-registration (§7, dated 2026-08-11), made before any Phase 2
annotation exists:

* **Study 1's estimand boundary is stated.** It sees up to the stored pixel and no further, so a null
  result does not close #4842 — row (ii) needs a SidewalkWebpage-side check of the lost vertical fudge.
* **Off-axis vertical offset and the −35° pitch floor are registered as Study 1 secondaries**, on
  `exact_y` rows, with the mechanism-discriminator reading:

  | Δel signature | reading |
  |---|---|
  | tracks the tilt terms, flat in off-axis | rig tilt → the #54 geometry fix |
  | grows with off-axis, ≈ 0 at the canvas centre, scales with fov | capture-side projection error |
  | constant in both | human placement behaviour |

Nothing else moves: the corpus spec, pixel source, annotation protocol, and power table are untouched,
and stored `pano_x`/`pano_y` remains the anchor. The corpus must **not** be stratified on "looks off in
Validate" — that is selection on the outcome.

One constraint this hands the Phase 2 tooling: the annotation tile renderer must cut from the
equirectangular raster with its own tested transform, and Study 3's gnomonic re-projection must be
written fresh — **never ported from the webpage's render path**. Porting it would import the very bug
family under test into the gold standard, and Study 1 would measure zero by construction.

## Wrong turns

* **Restricting on `exact` because that is what the record-staleness study calls a clean record.**
  The class name is right for that study and wrong here. It would have dropped 13,485 rows to guard
  against a stale *heading*, which the covariate provably never reads. The heading-independence proof
  is what corrected it — and it only got written down because a test asked what the covariate varies
  with.
* **Measuring on post-fix labels only.** The first probe restricted to `time_created ≥ 2024-09-26` to
  dodge the record bug, and reported 92% surviving the band fixed effects with a 16.03% floor share
  and 53.1% in the >30° band. Those numbers are correct *for that population* and wrong for the study,
  whose corpus spans all three eras. The committed all-era figures are 95.08%, 10.18% and 49.2%. The
  `exact_y` restriction turned out to be the better way to dodge the bug — it keeps 98.96% of the
  corpus instead of 9.9%.
* **Predicting the floor cohort clicks *low* in the canvas.** The reasoning was that a user who cannot
  pitch further down must click near the bottom of the frame. Measured, the floor cohort clicks
  *higher* in frame than the free cohort in every band — at the floor the label's depression is closer
  to the viewport centre's own depression, so the click lands nearer the middle. The mechanism was
  backwards; the exposure numbers stand.
* **A mutation that was killed by a broken test.** The "demean by median instead of mean" mutant
  appeared to die, but the fixture gave both bands the same skew — and residual standard deviation is
  shift-invariant, so both estimators produce identical output and the test failed on clean and
  mutated code alike. It was a false kill, caught only because the test carried an explicit
  guard-the-guard assertion that the fixture discriminates the two estimators at all. The fixture now
  gives the bands different mean−median gaps.
* **A guard that could not be tested because it could not fire — and its twin, which survived the
  same argument.** `eligible` carried an `isfinite(depression)` check that survived mutation;
  `exact_y` already requires finite `pano_y` and a finite positive `pano_height`, which is exactly
  what makes depression finite. Removed rather than given a test that would have asserted nothing.
  The `isfinite(offaxis_v)` term two lines above it was left in, and it is unreachable by the *same*
  reasoning: `offaxis_v = pitch − pov_pitch`, and a non-finite pitch propagates through `cos p₀` into
  all three of x/y/z, so `pov_pitch` is non-finite too and `exact_y` is already false. Caught in
  review (see below); the file was documenting two contradictory standards. Both are gone now, and
  what is pinned instead is the observable consequence — a NaN-pitch row is ineligible — which is
  what would actually break if `exact_y` were ever weakened. The band guard beside them is not
  redundant in the same way and stays.
* **Reducing a fraction and reading the reduced numerator as the sample size.** The review flagged
  `by_label_type_pct` for publishing rates with no n, and read `Signal: 2.5316455696…%` as 2 labels of
  79. It is 86 of 3,397 — the same fraction reduced. The finding stood for a different reason: the
  same function runs per city, where `oradell-nj` `Other` is 0.0% from 12 labels and `newberg-or`
  `Crosswalk` is 23.8% from 42, printed beside a 12.07% drawn from 148,796. The n is now published;
  the pooled types are all in the thousands.

## Pre-merge review (2026-08-11)

Reviewed before merge, while the amendment was appended but PR #81 was still open. Fifteen findings,
all fixed here; the amendment's registered content is unchanged — none of the defects touched a
covariate definition, an eligibility rule, or a number the amendment leans on. The two that would have
stopped a re-run, and the two report/data mismatches, are the ones worth reading:

**Two crashes on the degenerate path, both at the ends of a run.** `main()` format-specced the `None`s
that `identification()` returns for fewer than two eligible rows — its own pinned contract — so one
thin city would abort a six-city run with a `TypeError` after every other city had been computed and
before `--write` was reached. And `corr_with_depression` was unguarded against zero variance, where
Pearson's *r* is undefined and pandas answers `NaN`: since `main()` writes with `allow_nan=False`, a
single constant subgroup killed the write on the last line. That path was **already live in the
suite** — `test_ineligible_rows_are_excluded_from_the_estimate` produced `corr=nan` and was the source
of two `invalid value encountered in divide` warnings in a green run; the test only asserted `n == 50`.
Both are now null-safe end to end, and `TestDegenerateInput` exercises the thin-group path deliberately
rather than only the corpus.

**Two numbers in shipped prose contradicted the data.** The module docstring justified the `exact_y`
restriction by saying the alternative "would discard 58% of the eligible rows" — 58% is the `x_only`
share of record *misses*, used correctly two lines above; the actual cost is **3.1%** (13,485 of
433,866), which is what this report and the amendment both say. It overstated the cost of reversing the
study's central methodological choice by 19×. And `deg_per_canvas_px` cited "70% of post-fix labels",
a population the Wrong Turns section above records abandoning. `TestDocstringFigures` now checks both
docstrings' figures against the committed artifact.

**The zoom table was not the census it read as.** See §3: three hardcoded rungs, no residual, 280
eligible rows at fractional zoom silently absent. This is the finding that changed a number rather
than a description.

**§4's specimen values reproduced from nothing.** Neither label is in the six-city corpus, so the two
off-axis figures existed only in the prose — against this repo's own bar that a fresh clone reproduce
every number in `reports/`. Their records now live in `SPECIMENS`, their offsets and band membership in
the artifact under `specimens`, and both are pinned. Computing the band membership is also what caught
the Summary claiming both specimens sat beyond the 5th percentile of every band when only one does.

The rest, briefly: `by_label_type` publishes its *n* (per city a rate could be 0.0% from 12 labels);
`_spread` reports a null sd for a one-row band instead of `0.0`; `identification_post179` is gone,
having been byte-identical to `by_era_identification['post179']`; `offaxis_offsets` reads the POV
columns `replay_frame` now publishes instead of re-running the gnomonic projection over all 438,410
rows a second time; the local `CANVAS_W/CANVAS_H` copy is gone, so eligibility and the covariate cannot
be computed against two different canvases; a vacuous `isfinite(offaxis_v)` term is removed (see Wrong
Turns); an empty `--csv-dir` now names the directory instead of dying in `pd.concat`; and the index row
in `reports/README.md` is back in date order.

**Enforcement.** 103 tests in `tests/test_offaxis_covariate.py` (up from 48) and a new
`tests/test_reports_index.py`; **18/18 mutants killed** on a battery that reverts each fix
individually. Two mutants survived the first pass and both were the tests' fault, not the fixes':
`floor_census`'s and `specimen_census`'s findings were pinned only against the committed artifact, so a
code revert left them green — synthetic code-level tests were added. A third survivor,
"`eligible` stops requiring a band", exposed a test that reimplemented the mask inline instead of
calling `prepare`; it now uses the reachable boundary (a click on the pano's top row gives depression
exactly −90°, which `pd.cut`'s open left edge rejects). `TestReportMatchesTheArtifact` compares this
report's §1, §3 and §4 tables against the committed JSON cell by cell, which is the check that would
have caught both mismatches above without a reviewer.

## Open questions

* **Row (ii) is untouched by anything here.** A pure render-side error is invisible to this covariate
  *and* to Study 1. The lost 5-px vertical fudge (`865b5b8a8`) needs a check in SidewalkWebpage; if
  fixing it puts #4842's examples on target, the blind spot never mattered.
* **The covariate cannot separate a capture-side projection error from a placement behaviour that is
  itself canvas-relative** — a user who systematically clicks slightly low on objects near the frame
  edge produces the same signature. Gold annotation breaks the tie only if the behaviour is
  object-relative rather than canvas-relative. Worth stating in the Study 1 write-up rather than
  discovering at interpretation time.
* **The pooled estimate mixes centerline and off-centerline rows**, where the covariate's relationship
  to viewport pitch differs. Whether the Study 1 fit should carry the centerline identity separately is
  a modelling choice left to Phase 3's analysis code, frozen before evaluation.
* **The repair migration will move the inputs.** Eligible rows are invariant to it by construction, but
  that is an argument, not a measurement — re-run this script against a post-migration fetch as an
  offline sensitivity check.

## Where everything lives

| Artifact | Path |
|---|---|
| Summary numbers (committed) | `reports/data/2026-08-11-offaxis-covariate.json` |
| Covariate + identification analysis | `reports/scripts/offaxis_covariate.py` |
| Machinery tests + findings pins | `tests/test_offaxis_covariate.py` (103 tests; 13/13 study mutants, 18/18 review mutants killed) |
| Report/index consistency | `tests/test_reports_index.py`, `TestReportMatchesTheArtifact` |
| Inherited machinery | `reports/scripts/pov_replay.py`, `era_replay_study.py`, `rawlabels.py` |
| The amendment this supports | `reports/2026-08-09-crop-priors-prereg.md` §7 |

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-opus-5[1m]). The useful move was
noticing that the interface where the problem was reported is the one interface that cannot show the
mechanism this repo most needs to rule out.*
