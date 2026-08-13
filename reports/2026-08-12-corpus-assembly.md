# Phase 2a — assembling the crop-priors corpus, and why the frame changed

**2026-08-12** · cropper work package (#54, #32) · Phase 2a of the
[crop-priors pre-registration](2026-08-09-crop-priors-prereg.md), whose §3 froze the corpus spec and
whose [amendment 2](2026-08-09-crop-priors-prereg.md#amendment-2--2026-08-12--the-sampling-frame-a-mapillary-arm-and-the-corpusmeasurable-split)
records the changes this work prompted.

Artifacts: [`corpus_sample.py`](scripts/corpus_sample.py),
[GSV corpus](data/2026-08-12-crop-corpus-gsv.csv.gz) +
[manifest](data/2026-08-12-crop-corpus-gsv.json),
[Mapillary corpus](data/2026-08-12-crop-corpus-mapillary.csv.gz) +
[manifest](data/2026-08-12-crop-corpus-mapillary.json). No annotation exists yet; nothing here
depends on any.

## 1 · What was drawn

| | GSV arm | Mapillary arm |
|---|---|---|
| deployments in frame | 49 | 1 (richmond) |
| frame labels | 1,376,851 | 267 |
| corpus-eligible | 1,348,743 | 264 |
| measurable (Study 1) | 922,936 | 181 |
| frame panos | 556,775 | 89 |
| **drawn labels** | **763** | **97** |
| drawn panos | 661 | 44 |
| drawn measurable | 584 | — |
| cities represented | 35 | 1 |
| strata cells filled | 120 of 120 | 18 |
| tune / eval | 388 / 375 | 45 / 52 |
| disputed labels kept | 86 | — |

Every one of the GSV arm's 120 cells reaches §3's target of 6: **112 at exactly 6**, 8 above it where a
forced stratum contributed extra, **none below**. The reweighting is fully supported — all **32**
population cells carry drawn labels, so **0.00%** of the population sits in a cell the draw cannot
speak for.

**The two studies read different populations, and the per-band counts are where that bites.** The
corpus averages **190.8** labels per depression band (180–205), but Study 1 reads only the measurable
subset, which averages **146.0** (137–155). §5's power table assumed ≈160/band, so Study 1's bands land
slightly *below* it rather than above — still comfortably powered (§5 needs 44 per stratum for
δ = 0.25°, ~57 after its design effect for pano clustering, against a thinnest band of 137), but the
corpus figure is the flattering one and quoting it at a Study 1 power claim overstates every band by
30%. The first draft of amendment 2(d) did exactly that; both figures are now in the manifest under
names that say which filter they are under.

Forced strata, achieved against required: within-pano contrast **81/80** panos, resolution oversample
**122/60** labels, replay-mismatch **48/30**. The Mapillary arm reports two shortfalls honestly:
**40/80** contrast panos and **0/30** mismatches, the latter because every Richmond record replays
exactly. 40 is also below §2.3's 60-pano estimability gate, so that arm's within-pano column is *not
estimable* — stated now rather than discovered after annotation.

## 2 · The frame question, and the measurement that settled it

§3 drew from six deployments. Asked why — with 49 GSV deployments available — the honest answer needed
a measurement, not a rationale. Two separable questions came out of it.

**Does a wider frame reach strata the six cannot?** No. The six cities occupy **120 of 120**
(band × era-quality × type) cells that exist across all 49 GSV deployments; widening adds **zero** new
cells and removes none. Since the draw is ~700 labels sized by power rather than by frame size, breadth
buys almost nothing in coverage. This is also the property that *licenses* reweighting a narrow draw to
a wide population: no cell carrying population weight lacks support in the six.

**Does the six-city population match the one the cropper serves?** No, and this is the real defect.
The six are **31.98%** of the corpus-eligible population (431,276 of 1,348,743) and differ from it by
**43.16 percentage points** of total variation on era-quality:

| era-quality | six-city | 49 GSV | ratio |
|---|---|---|---|
| post_fix (≥ 2024-09-26) | 9.98% | 46.34% | **4.64×** |
| window | 10.74% | 17.54% | 1.63× |
| mid | 45.54% | 21.76% | 0.48× |
| legacy | 33.75% | 14.36% | 0.43× |

The six cities were selected in Phase 1 *to span* the era boundaries, which necessarily loaded them
with old data. Nearly half of Project Sidewalk's labels are now post-fix — written by the current,
self-consistent client — and that is the stratum the old frame represents worst. Reweighting on
six-city weights would put 79% of the mass on legacy+mid where the population carries 31%.

Label type moves too (11.06 pp), and not uniformly: PedestrianSignal **3.00×**, Crosswalk **2.56×**,
NoCurbRamp **0.57×**, against CurbRamp 0.88× and SurfaceProblem 0.97×. Depression band is the stable
one at 3.35 pp.

So the draw widened and the weights widened with it. Amendment 2(a) records it, along with three
deployments kept out of the frame because they are not populations: **validation-study** (10,809
labels, a research deployment), **la-piedad-old** (4,391, superseded by la-piedad and would
double-count it), and **winterthur-infra3d** (0 labels).

### Richmond

The same question covered Mapillary, and here the pre-registration was simply out of date: §6 excluded
Mapillary before the [Mapillary census](2026-08-11-mapillary-census.md) existed. `camera_roll` is
present for **0 of 1,376,851** GSV rows and **267 of 267** Richmond rows, so Richmond is the only place
in the project where rig roll is *measured* rather than fetched from photometa — which answers only for
the 47.9% of panos still alive at Google, and selects on era in doing so. Richmond's arm is unselected,
and its rig is more tilted. It is registered as a separate arm (amendment 2(b)), reported beside the
GSV arm and never pooled: two rigs in one estimate is exactly the hazard the separate cache trees
exist to prevent.

`zurich-infra3d` is a **third rig** (4,791 labels, infra3d) and stays out of scope. Worth recording
because it is the sole source of the 8032-px served height the GSV frame lacks entirely, so §3's
resolution stratum can look under-provisioned for what is really a scope boundary.

## 3 · Two defects found while assembling, both silent

Neither would have failed anything. Both are the reason this phase produced a report rather than just
a file.

**Label identity was `label_id`, which is not unique.** Label ids restart at 1 in every deployment:
across seattle-wa, columbus-oh and oradell-nj alone, **90,369 of 316,735** rows share a `label_id`
with a different city (`seattle-wa 9` and `oradell-nj 9` are different labels on different panos). The
draw keyed its selection dicts on the bare integer, so one city's label displaced another's. The
visible cost was a corpus of **449 labels instead of 763** — 314 lost — with **50 of 98** strata cells
short while the frame held thousands of candidates for every one of them, and 22 cells missing
entirely. Nothing raised: the corpus was simply smaller and thinner than the spec it claimed to
implement, which is the kind of defect that only surfaces once the annotation is paid for. Identity is
now `(city, label_id)`, carried as `label_uid`.

The hazard is narrower than it first looked. `pano_id` does **not** collide across deployments (0 cases
over the same frames), and the committed studies key on `pano_id` or work per city —
`off_target_markers_examples` merges on `label_id` *inside* its per-city loop, and
`click_noise_study.matched_pairs` already resets its index with a comment naming the concatenated
multi-city frame as the case that would break. No committed artifact is affected.

**The resolution stratum overshot to 96/60.** The forced-stratum loops recounted their progress by
rescanning every selected row per candidate, and the rescan disagreed with the selection path about
which rows counted. Replaced by running tallies updated at the point of selection. The first test
written for this could not tell the two apart, because every row that phase draws is non-standard by
construction; the discriminating fixture needed an earlier stratum to contribute standard-height rows
first.

A third, smaller one caught before it shipped: `frame_comparison` compared the Mapillary arm against
six *GSV* reference cities, and against a disjoint reference every total-variation distance comes out at
exactly **50.00 pp** — a clean-looking number meaning nothing. It now reports `applicable: false` with
a reason.

## 4 · How the draw is checked

`tests/test_corpus_sample.py` — 60 tests, and a **32-mutant battery, 32 killed**. The mutants that
mattered were the ones that started out surviving:

* **`CELL_TARGET` 6 → 5 survived**, because the test asserting "every cell reaches the target" read the
  constant. Registered quantities now have one assertion that does not go through the constant.
* **The bearing separation losing its wrap survived**, because no fixture had a pair straddling the
  seam. Two labels at Δb −170 and +170 are 20° apart in the world; a plain difference calls them 340°
  apart and admits a pano carrying no within-pano contrast at all.
* **An in-frame `pano_y` guard could not be killed either way** — and the reason is worth keeping: it
  is implied twice over, by `exact_y` (the replay maps an arcsin-bounded pitch onto [0, H]) and, first,
  by the band guard (y outside [0, H] is depression outside ±90, hence no band). The guard is gone and
  both implications are pinned, including the case that discriminates them: a label at `pano_y` 0
  replays *exactly* and is rejected only by the band.

`contrast_panos_available` is a vectorized replica of the canonical §2.3 predicate, kept because the
canonical one takes minutes over 556,775 panos, and pinned against it on random and near-threshold
bearings — the same arrangement `clamp_census.predict_crop_size` has with CropRunner's original. It
raises above 90° separation, where the span argument stops holding.

## 5 · What this does not settle

* **Pixels.** Nothing has been fetched. The store-coverage figures §3 relies on (99.2% of dead panos,
  97.8% in legacy) were measured on the six cities; for the 43 newly-in-frame deployments store
  coverage is **unmeasured**. The corpus does not freeze until a probe covers the drawn panos, and §3's
  dims-mismatch exclusion cannot be applied at all until then — it compares against the store's own
  JPEG.
* **§3's "preferentially alive at Google" preference** for the contrast stratum is not implemented:
  aliveness needs a photometa request per pano, and the draw is offline. The stratum is drawn without
  the preference and the manifest says so; a Phase 2b re-draw can supply a known-alive pano set.
* **The Mapillary arm's size.** 97 labels of 264 eligible, because the cell targets are calibrated for
  a frame five thousand times larger. Since the arm exists to identify endpoint 2 and the whole
  deployment is 264 labels, annotating more of it is cheap — a decision for Phase 2b, not a defect
  here.
* **Nothing about placement or sizing.** No annotation exists. Every endpoint in §2 is untouched.

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-opus-5[1m]).*
