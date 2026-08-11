# Mapillary: the machinery transfers, the tilt is larger, and the corpus is 267 labels

**2026-08-11** · prompted by asking whether the ground-truth study should cover Mapillary imagery ·
measures the pre-registration's §6 exclusion (*"Mapillary panos (GSV only in the study corpus)"*)
against Richmond, the first launched Mapillary deployment

> **Reproduce offline from committed bytes:**
> ```bash
> pytest tests/test_mapillary_census.py tests/test_rawlabels_mapillary.py
> ```
> **Reproduce from the source** (rawLabels is a moving target and Richmond is actively being labelled;
> these numbers are the 2026-08-11 fetch):
> ```bash
> python reports/scripts/fetch_rawlabels.py    # -> reports/scripts/.cache/rawlabels-mapillary/*.csv
> python reports/scripts/mapillary_census.py reports/scripts/.cache/rawlabels-mapillary \
>     --fetched <date> --gsv-dir reports/scripts/.cache/rawlabels \
>     --write reports/data/<date>-mapillary-census.json
> ```

## Summary

The pre-registration put Mapillary out of scope in §6. That was registered when no Mapillary city had
launched and nothing about it had been measured. Measured now, on Richmond:

**The machinery transfers essentially completely.** All **267** labels replay **exactly on both axes** —
`exact_x` 267/267, `exact_y` 267/267, max |dx| = |dy| = **0 px**. So SidewalkWebpage runs the same
`calculatePovIfCentered` → `calculatePanoXYFromPov` path for Mapillary as for GSV, on the same 720×480
canvas, with the same three zoom stops. That single measurement settles two separate worries at once:
`exact_y` is a meaningful eligibility rule here, and `get_3d_fov`'s ladder applies — an exact replay is
impossible under a different fov model, because fov sets the focal length.

**The tilt is materially larger, and `camera_roll` comes free.** `camera_roll` is populated for 100% of
Richmond rows and for **0% of all 438,410 GSV rows across six cities**. §5 prices endpoint 2 at n ≈ 310
rather than 650 precisely because roll must come from photometa, which answers only for the 47.9% of
panos still alive at Google — a survival-selected subsample §5 explicitly refuses to reweight. Richmond
has no such selection, and its rig is more tilted: |pitch| p90 **6.35°** and |roll| p90 **5.02°**
against the photometa prior's 2.6° and 2.2°.

**So endpoint 2 is already better identified on 267 Richmond labels than on the whole GSV corpus** —
SE(β_p) **0.019**, SE(β_r) **0.015**, against §5's 0.028 and 0.034 — because the regressors vary 1.6×
and 2.5× more, not because there is more data.

**What is short is a pano count, not a label count.** §2.3's within-pano contrast needs ≥ 60 panos
carrying ≥ 2 labels at ≥ 60° pairwise bearing separation. Richmond now has **43**, up from 15 before
today's labelling. And the cross-user block failed: two labellers shared a *route* and overlapped on
**7** panos, yielding **6** matched pairs where a placement-noise σ needs ~150.

**A referent-quality rule came out of this**, distinct from anything in the study spec: 86 of 267 labels
have no *point* referent to measure a displacement against — 62 `Crosswalk` (correctly placed anywhere
along the crossing), 3 `Occlusion`, 21 `SurfaceProblem` tagged `brick/cobblestone`. That leaves **181
comparable**. See §6; it applies to the GSV corpus too, where `NoSidewalk` is the open case.

## Method

**Corpus.** `/v3/api/rawLabels?filetype=csv` from `sidewalk-richmond.cs.washington.edu`, fetched
2026-08-11: 267 labels, 89 panos, 2 labellers, 2026-03-25 → 2026-08-11. Cached to
`.cache/rawlabels-mapillary/`, deliberately a **different directory** from the six GSV cities — every
study script globs `*.csv` over a directory, so mixing them would silently redefine "the six cities".

**Source identification.** rawLabels carries **no `source` column** (`/adminapi/panos` and cvMetadata
do; this endpoint does not), so imagery is identified by `pano_id` shape: Mapillary ids are all-numeric,
GSV ids are 22-char base64, Google user photospheres are longer `CAoS…` ids. All 267 Richmond labels
are Mapillary. For contrast, the six-city GSV corpus is 438,291 GSV ids plus **119 Google user
photospheres** and zero Mapillary.

**Two loader bugs this surfaced**, both invisible on GSV and both fixed:

* **`pano_id` was not dtype-pinned.** Mapillary image ids are all-numeric, so pandas infers `int64` for
  any Mapillary city — the #46 bug class that `DownloadRunner` and `CropRunner` both pin against with a
  comment naming Mapillary. Every `pano_id[:2]` store path breaks, and worse, a merge between an
  int-keyed and a str-keyed frame matches nothing and reports zero coverage rather than failing.
* **`tags` was not loaded**, and it is the only field that says whether a label's stored point
  identifies a located thing.

## Numbers

### 1. Does the replay transfer?

| | Richmond (Mapillary) |
|---|---|
| labels | 267 |
| `replayable_x` / `exact_x` | 267 / **267 (100.0%)** |
| `replayable_y` / `exact_y` | 267 / **267 (100.0%)** |
| max abs dx, dy | **0 px**, **0 px** |
| canvas frames | 720×480 only |
| zoom | 96 at 1, 127 at 2, 38 at 3, **3 fractional** |
| pano frames | 4096×2048, 5760×2880, 11000×5500 |

The three fractional-zoom labels are the same off-ladder tail the off-axis study found in the GSV
corpus (280 of 433,866): clients that interpolated zoom continuously. `get_3d_fov` is continuous, so
those rows have a well-defined fov — they just have no rung.

Pano dimensions vary far more than GSV's small set of frames, including a non-power-of-two
11000×5500, which makes #77's dims preflight matter more here.

### 2. Endpoint 2's design inputs

Not an estimate of β — that needs gold annotation, which does not exist. This is the design side, where
SE(β) = σ_resid / (sd · √n) with σ_resid = 0.59° from §5.

| | Richmond (n = 267) | GSV corpus (n ≈ 310) |
|---|---|---|
| `camera_roll` available | **100%** | **0%** in rawLabels; photometa only, 47.9% of panos |
| survival selection | none | yes, era-graded 33.2% → 60.0% |
| sd(pitch · cos Δb) | **1.94°** | 1.20° |
| sd(roll · sin Δb) | **2.48°** | 1.00° |
| SE(β_p) | **0.019** | 0.028 |
| SE(β_r) | **0.015** | 0.034 |
| §2.2 decision rule (needs SE < 0.153) | reachable, 8× margin | reachable |

Per-pano rig tilt over 89 panos: `camera_pitch` −17.64° to +5.01° (sd 3.32°, |·| p90 6.35°),
`camera_roll` −6.37° to +11.98° (sd 2.80°, |·| p90 5.02°).

**More Richmond labels would not buy endpoint 2 power.** It already clears its band with 8× margin, and
§5 says the same of the GSV fit: *"no additional sample resolves it, so do not read 'inconclusive' as
'needs more data'."* What Richmond adds is an **unselected** replicate of a conclusion currently
conditioned on pano survival.

### 3. What is actually short: §2.3's within-pano stratum

| | |
|---|---|
| panos | 89 |
| panos with ≥ 2 labels | 57 |
| **panos with ≥ 2 labels ≥ 60° apart** | **43** |
| required | 60 |
| shortfall | **17 panos** |

This is a *pano* count. Three labels on one pano at separated bearings are worth far more here than
three labels on three panos, because the pano fixed effect is what absorbs per-pano placement culture.

### 4. The cross-user block, and why it failed

| | count |
|---|---|
| labellers | 2 |
| comparable panos labelled, per labeller | 38 and 40 |
| **panos shared by both** | **7** |
| pairs found by the clustering estimator (r = 1.5°) | **2** |
| pairs found by 1:1 matching | **6** |

They shared a *route*, not a pano list. With multi-perspective labelling each picked different
perspectives of the same ramps, so pano-level overlap collapsed — and a cross-user comparison needs the
same pano, because that is the only frame where both clicks are directly comparable without gold.

The gap between 2 and 6 is a tooling finding: the clustering estimator requires both clicks inside a
radius, while a one-to-one assignment only requires them on the same pano and label type.
`click_noise_study.py` now has that matched mode behind `--pano-list`. It is **opt-in on purpose** —
run on the six-city corpus, where co-location is incidental, it returns σ_el **0.967°** against the
clustered estimate's 0.507°, because a corner's four curb ramps get paired across users almost
arbitrarily. A plausible σ from force-paired objects must not land in an artifact by default.

Six pairs give σ_az 1.89°, σ_el 0.309°. Not a finding — 6 pairs — but the σ_el is suggestively close
to the GSV core estimate of 0.299°.

### 5. Multi-perspective labelling

101 `CurbRamp` labels resolve to **39 physical objects** within 8 m; **25** are seen from ≥ 2 panos, and
12 were labelled by both people.

Opposite consequences by endpoint. For **endpoint 2** it is a gain: the same object across panos varies
both the rig tilt and Δb with identity held fixed. For **endpoint 1** it is a caution — those labels are
not independent, and the pre-registration clusters by *pano*, which does not absorb one object appearing
in several panos. Effective n is nearer 39 than 101, and the analysis needs an object-level cluster too.

### 6. Referent quality: a new exclusion principle

267 labels → **181 comparable**, **86** excluded:

| arm | rule | n |
|---|---|---|
| by type | `Crosswalk` — an extended linear feature | 62 |
| by type | `Occlusion` — marks the view, not a thing in it | 3 |
| by (type, tag) | `SurfaceProblem` + `brick/cobblestone` | 21 |

Every other exclusion in the study spec is about **record** quality — does the record replay, do the
dims agree. This one is about **referent** quality: if a label is correctly placed *anywhere* within some
extent, there is no particular spot it was aiming at, so a stored-vs-gold displacement has nothing to be
a displacement *from*, and keeping it puts an arbitrary, unbounded offset into the noise floor.

Three ways a label can lack a point referent, and all three occur here:

* **The type is an extended feature.** A `Crosswalk` label is correctly placed anywhere along the
  crosswalk, so two annotators who both place it correctly can be metres apart along its length. This is
  the largest arm — 62 of 267 labels, 23% of the corpus — and it is the one this census initially got
  wrong, keeping crosswalks on the reasoning that "a crosswalk is a located object whatever its surface".
  It is a located *object*; it is not a located *point*.
* **The type marks the view.** `Occlusion` ("Can't see the sidewalk"). §3 already excludes it as having
  no crop consumer.
* **A tag makes the extent arbitrary.** `SurfaceProblem` + `brick/cobblestone`: the whole sidewalk is
  brick, so any point on it qualifies.

**This is about placement-measurability, not crop-corpus membership.** Crosswalk (label type 9) has real
crop consumers and stays in the crop corpus; what it cannot be is the subject of a displacement
measurement. A test pins that distinction so the set is not wired into crop selection by mistake.

The rule stays narrow and enumerated rather than a heuristic over tag text, and a test pins its
membership so widening it is a decision rather than a drift. Two candidates are deliberately left out:
`SurfaceProblem` + `{bumpy, uneven/slanted}`, and — the open one — **`NoSidewalk`**, which may belong
with `Crosswalk` by exactly the same extended-feature argument (it marks a *stretch* of missing
sidewalk). Richmond has no `NoSidewalk` labels, so nothing here turns on it, but the six-city GSV corpus
has 81,667 and a placement study reading them should settle it first.

### 7. Where Richmond sits against §2.1's strata

| band | n | | |
|---|---|---|---|
| <5° | 8 | at pitch floor | 5 |
| 5–15° | 150 | above the horizon | 0 |
| 15–30° | 101 | eligible (`exact_y`) | 267 of 267 |
| >30° | 8 | | |

The tails are thin — 8 labels in each of `<5°` and `>30°` against the ~44 §5 needs to detect
δ = 0.25° per stratum.

## What does not transfer

* **No depth.** The depth phase goes through streetlevel's photometa, which is GSV-only, so Study 3's
  distance input is absent for Mapillary.
* **The gravity-alignment assumption is weaker.** `depression_from_pano_y` is exact for a
  gravity-aligned equirectangular pano, which is why stored `pano_y` carries no tilt term for GSV.
  RampNet measures the consequence on *this same Richmond imagery*: flat-ground
  `camera_height/tan(depression)` agrees with DA3 metric depth at Spearman **0.95 on Bend (GSV) vs 0.81
  on Richmond (Mapillary)**, and depth rescues 4 Richmond ramps that geometry placed above the horizon.
  Across RampNet's benchmark, 5 of 1,066 Mapillary GT ramps (0.5%) sit above the horizon against **0 of
  994 GSV**; Budapest's consumer rig reaches 12 of 300 (4%). None of Richmond's 267 Project Sidewalk
  labels are above the horizon, but 267 is far too few to see a 0.5% rate — that is consistent with the
  RampNet finding, not evidence against it.
* **The labeller pool.** Richmond has 2 labellers because it has not been opened broadly, so nothing
  here can speak to population placement bias (endpoint 1). Endpoint 2 is robust to that: β is
  identified off Δb, an angle the interface never shows a labeller, and §2.3's pano fixed effect absorbs
  per-pano placement culture. A constant personal offset lands in the intercept.

## Wrong turns

* **Reading `camera_roll`'s range as a p90.** The first pass, on 93 labels, saw roll reaching +11.98°
  and reported it as "≈5× the GSV prior". That was a tail, not the typical case: at p90 on 93 labels the
  two were comparable (2.70° vs 2.2°). Only the full 89-pano route made the difference real — |roll| p90
  5.02°, sd 2.80°. Both the overclaim and its correction came from the same field; the fix was to quote
  a quantile and its n rather than an extremum.
* **Believing "same route" meant "same panos".** The crossed block was designed as a route both
  labellers would cover. With multi-perspective labelling that produces almost no pano-level overlap: 9
  of 42 and 43. The measurement needs an explicit pano list, which is now what `--pano-list` takes.
* **Reducing a fraction and reading the numerator as the sample size.** In the off-axis review,
  `Signal: 2.5316455696…%` was read as 2 labels of 79. It is 86 of 3,397 — the same fraction reduced.
* **A census histogram that silently dropped labels.** This script's first version built its zoom
  histogram as `{f'{z:g}': n for z, n in value_counts().items()}`. Richmond carries
  `2.999999999999998` beside `3.0` and two spellings of `1.9925`, so distinct floats formatted to the
  same key and the later entry replaced the earlier: 267 labels reported as 264. Caught by a test
  asserting the histogram sums to the label count — the same reconciliation habit the off-axis review
  had just installed, applied one level down. Every histogram here now rounds first and sums on
  collision.
* **Assuming the fov ladder could not transfer.** The three "GSV-specific instruments" were argued from
  reading the code. Two of the three were wrong, and one measurement — the replay — refuted both. The
  general lesson is the cheaper one: a 30-second replay check settled what an hour of reading could not.
* **Keeping `Crosswalk` in the comparable set.** The referent rule was first written as
  `Occlusion` + one (type, tag) pair, and it explicitly *kept* `Crosswalk` + `brick/cobblestone` on the
  reasoning that "a crosswalk is a located object whatever its surface is made of". That confuses a
  located object with a located point: a crosswalk label is correctly placed anywhere along the crossing,
  so it has no point for a displacement to be measured from — the same property as the region tag, but
  inherent to the type. Caught by Jon from the labelling semantics, not by any test, and it is the
  largest arm of the rule at 62 of 267 labels. The code-level lesson: an exclusion rule about referents
  has to be derived from what a label *means*, and the tests can only pin a decision once it is made.

## Open questions

* **Is the pitch-floor covariate meaningful here?** 5 of 267 labels sit at −35°. The floor is a property
  of the Explore viewport, so it should apply identically, but the corpus is too thin to confirm the
  sharp banding the GSV census found (0.4% → 49.2% across depression bands).
* **The multi-perspective labels are a gold-free tilt probe, unexploited.** One physical ramp has one
  true position, and each pano's stored `pano_y` is perturbed by that pano's own
  pitch·cos Δb − roll·sin Δb — so disagreement across perspectives isolates the tilt term with identity
  held fixed, potentially giving a preliminary endpoint-2 read before any Phase 2 gold exists. It needs
  each **pano's** own lat/lng (rawLabels' `latitude`/`longitude` is the *label's* estimated position, not
  the camera's), so a Mapillary Graph API fetch, and it inherits the distance-estimate error. Worth
  scoping.
* **Whether any of this should amend §6.** Not decided here. The census says the instruments transfer
  and that a Mapillary tilt stratum would be unselected where the GSV one is not; it also says the
  corpus is 267 labels, 17 panos short of §2.3's gate, with no depth. That is an input to an amendment
  decision, not the decision.

## Where everything lives

| Artifact | Path |
|---|---|
| Census numbers (committed) | `reports/data/2026-08-11-mapillary-census.json` |
| Census script | `reports/scripts/mapillary_census.py` |
| Mapillary rawLabels fixture (10 real rows) | `tests/fixtures/rawlabels_richmond_head.csv` |
| Loader + referent rule | `reports/scripts/rawlabels.py` (`has_located_referent`, `parse_tags`) |
| Matched-pair mode | `reports/scripts/click_noise_study.py` (`matched_pairs`, `--pano-list`) |
| Tests | `tests/test_mapillary_census.py`, `tests/test_rawlabels_mapillary.py`, `tests/test_click_noise_matched.py` |
| The exclusion this measures | `reports/2026-08-09-crop-priors-prereg.md` §6 |

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-opus-5[1m]). The useful move was
running the replay instead of continuing to read the projection code: it refuted two of my own three
objections in one measurement.*
