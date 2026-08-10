# Pre-merge review of the Phase 1 crop priors and the pre-registration

**2026-08-10** · review of [PR #79](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/79)
· subject: the five Phase 1 desk studies and
[Report 1's pre-registration](2026-08-09-crop-priors-prereg.md)

A pre-registration is only worth committing if someone has tried to break it first, because after
the merge every change costs a dated amendment. This is the record of that attempt: what was
checked, the six things that were wrong, the five things I got wrong while checking, and what was
deliberately left alone.

## Method

Everything was re-derived from the committed artifacts — no re-fetch. Specifically: the suite was
run (417 passed at the time), every headline number in every report was recomputed from
`reports/data/*.json`, the vectorised `predict_crop_size` replica was diffed against the real
`CropRunner.predict_crop_size`, the projection port was checked term-by-term against a hand
derivation, the corpus counts were reconciled across studies, and every function in
`reports/scripts/` was checked for whether any test actually calls it.

Two reconciliations came out exactly right and are worth stating, because they are the reason the
rest of the review could take the data at face value:

* **Corpus arithmetic closes.** 438,410 labels across six cities, minus 2,060 rows with missing pano
  dims, minus the 2 corrupt negative-`pano_y` rows = **436,348**, which is the n in both the clamp
  census and the click-noise study, independently computed.
* **The tilt prior reproduces.** Recomputing from the census's own 651 alive records gives
  \|pitch\| p50/p90/p99 = 0.635/2.595/6.849°, \|roll\| = 0.905/2.163/4.263°, and a tilt-term
  sd of 1.557° — all matching the published values.

## What was wrong

| # | Finding | Where it landed |
|---|---|---|
| 1 | **The sizing band was 2× wrong, twice over.** "object ≈ 10–15% of crop side" was restated as "margin ≈ 3–4.5× object *half*-extent" — that arithmetic is margin over the *full* extent — and the prereg then registered `[3, 4.5]` under a third definition (`crop half-side / object half-extent`), where the consumer range is `[6.7, 10]`. sidewalk-validator-ai, the consumer the convention was read off, sits at R = 7.9: **as registered, Study 2's primary endpoint excluded its own anchor.** | Consumer report §(ii) rewritten around a single quantity, `R = crop side ÷ object extent`, chosen because it is invariant to half-vs-full; prereg §1/§2 updated; `reports/scripts/margin_convention.py` + `tests/test_margin_convention.py` added (5/5 mutants killed), including a pin that the retired phrasing cannot come back |
| 2 | **The clamp report claimed truncation "0.000% (9 labels corpus-wide)"; the committed JSON says exactly 0** in every scope. 9 labels would be 0.00206%. The pin asserted `< 0.05`, which passed for both, so it could not adjudicate. | Report corrected to the data, pin tightened to `== 0` across overall/by-city/by-type, and the zero is now explained analytically rather than counted |
| 3 | **Endpoint 2's n is ~310, not 650.** `camera_roll` is empty in **100%** of rawLabels rows (436,348/436,348), so pitch/roll can only come from photometa — which answers only for the 47.9% of panos still alive, a rate that is itself era-graded. Labels on dead panos are in the corpus by design and contribute nothing to the tilt regression. | Prereg §5 now states the achievable n, the survival selection, per-coefficient SEs (0.028/0.034), and that the estimate must not be reweighted back to the label population |
| 4 | **The agreement gate was looser than the assumption it protects.** §5 assumes σ_gold ≤ 0.30°; §4 gated on mean \|Δ\| ≤ 0.5°, which admits σ_gold ≈ 0.44° (E\|Δ\| = 1.128·σ_gold for two annotators). | Gate tightened to **0.34°** = 1.128 × 0.30, with the derivation in the text; §5's power table now carries a second column pricing the 0.44° fallback (n for δ = 0.25° goes 44 → 57, still inside the ~160/band budget) |
| 5 | **The within-pano robustness column was required but not provisioned.** §2.3 said the conclusion "must survive" a pano-fixed-effects fit; §3's ≈650 labels over ≈550 panos averages 1.18 labels/pano, so the column could have been silently unrunnable. | §3 forces ≥ 80 panos contributing 2–3 labels at ≥ 60° bearing separation; §2.3 adds an explicit *not estimable* outcome with a floor of 60 panos |
| 6 | **`reports/data/2026-08-09-photometa-census.json` was not valid JSON** — 4,916 bare `NaN` tokens. Cause: `df.where(df.notna(), None)` coerces the None straight back to NaN on float columns. Python's `json` accepts `NaN` on both read *and* write, so the scripts wrote it, the tests read it back, everything was green, and the file was rejected by jq, `JSON.parse`, and most non-Python readers. | `json_records()` scrubs through object dtype; `write_json()` uses `allow_nan=False` and LF newlines; all four study writers now pass `allow_nan=False`; the committed file was repaired **through the production `--resummarize` path**, verified to change 4,916 lines and nothing else; `tests/test_committed_data_files.py` now strict-parses every artifact under `reports/data/` |

Below those, six should-fixes and a dozen nits, all applied: the aggregation layer was uniformly
untested while the estimators were well covered (see next section); the `resolution_dependence`
ratios are clamp-attenuated and needed the mechanism stated; the post-fix drift claim was argued
from a pre-179 decomposition; a "Report 1" link pointed at the wrong report; `pov_replay.wrap_deg`
and `replay_pano_xy` were dead code, the latter a second copy of a projection that already exists
NaN-safe in `era_replay_study.replay_frame`.

## The structural gap: tested estimators, untested assembly

The studies had good tests of every *estimator* — seam-aware clustering, injected-σ recovery,
outlier robustness, the ast-pinned crop-size replica — and no test at all of the functions that
assemble those estimators into the committed JSON:

| Previously called by no test | What it produces |
|---|---|
| `clamp_census.resolution_dependence` | the 1.198× resolution headline |
| `era_replay_study.monthly_series` | the figure that dates the client fix to one day |
| `era_replay_study.study_city`, `click_noise_study.study` | every committed summary |
| `click_noise_study.load_city` | the filter behind "2 corrupt negative-y rows" |
| `photometa_census.resummarize` | the path that regenerated the summary after the roll bug |

This matters more than an ordinary coverage hole because of how the `TestCommittedFindings` pattern
works: those classes read the JSON that this untested layer produced. A bug in assembly yields a
wrong artifact, and the pins then certify it. All five are now covered. The suite goes 441 → 500 collected
tests, and the new machinery carries mutation evidence: **5/5** (margin convention), **8/8** (clamp
census), **7/7** (photometa), **7/7** (click-noise), **7/7** (era replay) — 34/34 killed.

One of those mutants is the reason the click-noise sensitivity analysis is now pinned on pair
*counts* and not only on σ — see the next section.

## Wrong turns (mine, this time)

* **"`validated_only` being bit-identical to `overall` is a bug."** It is not. `d_el` is a difference
  of integer pano rows, so on the 8192-tall panos carrying 90% of the corpus it is quantised to
  0.0220°/px, and both medians land on the same 22-pixel atom. Real — but it took recomputing the
  atom to believe it, and a reader hitting `0.5067739007076677` twice deserves the explanation, so
  it is now in the report *and* asserted directly. The genuine defect underneath was narrower: with
  the two σ values identical, `rel=0.15` cannot distinguish the sensitivity analysis from having
  been run on the wrong frame. That mutant survived; the pair-count assertion kills it.
* **"The era study never addresses post-fix `pano_x` misses."** It does, in prose, and correctly. The
  real gap was one step in: the *dispositive* within-pano/across-pano decomposition is computed on
  pre-179 rows only, so extending it to post-fix is an inference. Softened in the report, and
  `study_city` now emits `drift_signature_post_fix`.
* **"`ok_y` requiring `pov_ok` will undercount y-replayability"** (a NaN viewer `heading` kills the y
  replay even though y needs no camera metadata). Measured before reporting: `replayable_x` equals
  `replayable_y` in every era of every city, because the only unreplayable rows are the ones missing
  pano dims. Latent, never fires, not reported as a defect.
* **A new test of mine asserted within-pano σ < 0.02°** on synthetic drift. It failed at 0.031° — and
  the test was wrong, not the code: `pano_x` is an integer, so one pixel at width 8192 is 0.0439°,
  and two labels sharing one true drift can round to neighbouring columns. The bound is now stated
  as one pixel in degrees, which is what "rounding-noise level" actually means.
* **My first mutation harness had no `finally`.** A non-matching pattern aborted the run and left
  `photometa_census.py` mutated; a later timeout killed a run and left `era_replay_study.py`
  mutated. Both were caught by diffing against git before continuing, but the honest lesson is that
  a mutation harness must restore in a `finally` and the source must be diffed afterwards regardless
  — otherwise a "reviewed" branch quietly ships a deliberate bug.

## Deliberately not done

* **No re-fetch of rawLabels or photometa.** Both are moving targets; a fresh pull would re-date
  every number across five reports to buy two new aggregate fields. So
  `drift_signature_post_fix` (era replay) and a per-height clamp rate (clamp census) exist in the
  code but not in the 2026-08-09 artifacts, and both are flagged in their reports as awaiting the
  next fetch. Where a claim could be settled *analytically* instead, it was: the clamp census's
  three zeros are now derived from the formula's own constants rather than counted.
* **The consumer-requirements citations were not verified.** RampNet, validator-ai, tagger-ai and
  sidewalk-ai-api are not local, so the file/line claims are taken on trust. The geometry derived
  *from* them was checked and is internally consistent (f = 1024 px, 22.756 px/deg, 2.24–2.64
  heatmap px per degree); two rounding wobbles were corrected.
* **The era study's in-window sub-mechanisms** (the devicePixelRatio-2 cohort, the zoom-desync slice,
  the frame-change slice) are not backed by numbers in the committed JSON. Left as written — they
  are attributions of a residual, not load-bearing for any conclusion — but they are the part of
  that report a future reader cannot re-check offline.

## What the sign problem actually is

Finding #6's fix deserves its own note, because it is the one place where review changed a
scientific commitment rather than a number.

The registered tilt regression was `Δel ~ β·T` with `T = pitch·cos Δb + roll·sin Δb`. Working the
small-angle geometry independently — rig up-vector `u_rig = u − p·f + r·s`, bearing measured
clockwise from the camera forward axis — gives `Δel = pitch·cos Δb − roll·sin Δb`: the same pitch
sign, the **opposite** roll sign. Which is right depends on Google's own pitch/roll convention,
which cannot be settled from the committed data.

That is not a cosmetic disagreement. Under the wrong sign the two terms partially cancel, β is
attenuated toward zero, and the registered decision rule fires "panos are gravity-aligned, no fix" —
a real hypothesis killed by an arithmetic convention, in a document whose whole purpose is to make
that call in advance and stick to it. The prereg now registers the two-coefficient form
`Δel ~ β_p·(pitch·cos Δb) + β_r·(roll·sin Δb)`, for which the null is `β_p = β_r = 0` under either
convention and the sign of β̂_r *reports* the convention instead of assuming it. It also now states
that Δb collapses to `(pano_x/pano_width)·360 − 180`, so the camera heading cancels — which is what
keeps the regression consistent with the era study's "never re-derive from `camera_heading`".

## Where everything lives

| Artifact | Path |
|---|---|
| Corrected pre-registration (revision list in §7) | `reports/2026-08-09-crop-priors-prereg.md` |
| The sizing quantity and its conversions | `reports/scripts/margin_convention.py` |
| Clamp/truncation onsets, analytic | `reports/scripts/clamp_census.py` |
| JSON scrub + strict writer | `reports/scripts/photometa_census.py` |
| Strict-JSON check over every committed artifact | `tests/test_committed_data_files.py` |
| Tests (441 → 500 collected; 34/34 new mutants killed) | `tests/test_*.py` |

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-opus-5[1m]). The most useful
single check was the cheapest one: listing which functions no test ever calls, and noticing that it
was exactly the layer that wrote the files the other tests trust.*
