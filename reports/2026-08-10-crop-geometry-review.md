# Crop geometry: what the seam fix reaches, and what the preflights beside it can see

**2026-08-10** · Review evidence for [PR #77](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/77) (#47) · fed back into that PR

> **Reproduce from committed bytes:**
> ```bash
> pytest tests/test_crop_geometry_census.py tests/test_crop_runner.py
> ```
> **Reproduce the census from the source** (rawLabels is a moving target — labels accrue and
> `gsv_data` refreshes — so expect drifted counts):
> ```bash
> # one CSV per city into a directory of your choosing
> curl -o seattle-wa.csv 'https://sidewalk-sea.cs.washington.edu/v3/api/rawLabels?filetype=csv'
> python reports/scripts/crop_geometry_census.py <that-dir> \
>     --fetched <date> --write reports/data/<date>-crop-geometry-census.json
> ```

## The question

PR #77 fixes a real geometry bug — `Image.crop()` zero-fills out-of-range boxes, so a label near
the equirectangular seam got a black bar instead of the imagery that is genuinely there — and adds
two behaviours around it: a vertical clamp, and a preflight comparing the label row's pano
dimensions against the image on disk. All three were argued from reading the code. Three questions
had no data behind them:

1. **What does the dims preflight actually catch?** That depends entirely on whether
   `pano_width`/`pano_height` is a click-time snapshot stored with the *label*, or a value joined
   from the *pano*. The PR's comment, its description, and the README all assumed the former.
2. **How many labels does the seam wrap rescue?** #47 reasoned from the x-*range* — about 9% of
   columns on a 16384-wide pano. Labels are not uniform in x, so that is an upper bound on
   something, but not the number of affected crops.
3. **When does the vertical clamp fire, and on what?** It trades centring for the absence of
   padding. Whether that is a routine tradeoff or a rare corrupt-data path changes what it should do.

## Method

`reports/scripts/crop_geometry_census.py` over `/v3/api/rawLabels?filetype=csv` for six cities,
fetched 2026-08-09 — **438,410 labels on 172,790 panos** (seattle-wa 261,958 · cdmx 74,263 ·
columbus-oh 41,186 · amsterdam 30,061 · newberg-or 17,351 · oradell-nj 13,591). 2,060 rows carry no
frame at all (third-party photospheres) and are set aside, leaving 436,350 for the geometry counts.

The census replicates `predict_crop_size` and `compute_crop_box` vectorized, because it has to run
over 438k rows. Both replicas are pinned against the real `CropRunner` functions in
`tests/test_crop_geometry_census.py`, including the banker's rounding — `np.round` and Python's
`round` are both half-to-even, and the seam/shift rates would drift by a pixel at odd crop sizes if
they weren't. A geometry change now fails CI rather than silently invalidating the census.

Unlike the sizing census, this one deliberately does **not** filter out-of-range `pano_y`. Those
rows are the finding.

## Numbers

| Quantity | Count | Share |
|---|---|---|
| Labels with a usable frame | 436,350 | — |
| Crop window crosses the seam (black-padded pre-#77) | **6,634** | **1.52%** |
| Crop window needs a vertical shift | **2** | 0.0005% |
| Crop size capped by the pano's own dimensions | 0 | 0% |
| `pano_y` outside its frame — unrecoverable | 2 | 0.0005% |
| `pano_x` outside its frame — benign, wraps correctly | 2 | 0.0005% |

**Panos carrying more than one `(pano_width, pano_height)`: 0 of 172,790** — including **0 of the
196** whose own labels are more than four years apart, which is exactly where a pano re-served at a
new resolution would have to show up. Each dimension bucket spans the full corpus time range
(16384×8192: 2019-03-04 → 2026-07-31; 13312×6656: 2019-01-30 → 2026-07-15), so the buckets are a
property of the pano, not of the era it was labelled in.

Where real labels sit vertically, as a fraction of pano height:

| p0 | p1 | p50 | p99 | p100 |
|---|---|---|---|---|
| 0.341 | 0.517 | 0.585 | 0.742 | 0.877 |

For a 16384×8192 pano the clamp zone is `y < 25` or `y > 7442` — that is, below 0.003 or above
0.908. Real labels are nowhere near it, which is why the shift count is 2 rather than a percentage.

The four out-of-frame rows, verbatim (also in the committed JSON):

| city | label_id | pano_x | pano_y | frame | axis | recoverable |
|---|---|---|---|---|---|---|
| cdmx | 65875 | 16384 | 5010 | 16384×8192 | x | yes |
| cdmx | 66643 | 16384 | 6446 | 16384×8192 | x | yes |
| seattle-wa | 231546 | 845 | **−720** | 13312×6656 | y | no |
| seattle-wa | 233419 | 12327 | **−355** | 16384×8192 | y | no |

## What this settled

**1. The dims preflight guards the store, not the label's frame.** Since the dimensions are a
per-pano join, a pano re-served at a new resolution gets its dimensions refreshed in place while
`pano_x`/`pano_y` do not move — and `DownloadRunner` stitches to those same refreshed dimensions, so
the metadata and the image on disk agree and the preflight passes. The stale-coordinate row it was
advertised to catch is precisely the row it cannot see. What it *does* catch is genuine and worth
keeping: a store holding an image downloaded before a metadata refresh, and the Mapillary path,
where `thumb_original_url` may serve a size other than the one recorded.

This matters beyond wording. PR #77 described the preflight as "load-bearing for the upcoming
#54/#32 measurement studies, where a frame mismatch would corrupt every residual." It will not
exclude those rows; #54 needs the POV replay flag for that.

**2. The seam fix is the load-bearing half of the PR** — 6,634 labels, every one of which had been
getting a black bar through the middle of its training crop.

**3. The vertical clamp, as shipped, was a corrupt-data path wearing a recovery's clothes.** Both
rows that trigger it are the known-corrupt negative-`pano_y` rows. Pre-fix they produced obviously
black crops; the clamp turned them into clean imagery of a place the label is not in, and
`--mark-label` cannot even reveal it — the dot lands at row −720 of a 50-px crop and is clipped
away. That is a quieter failure than the one being fixed.

## What changed in the code

| Change | Commit |
|---|---|
| The census, its data, and its tests | `6b3514c` |
| RED: y-bounds, the shift report, the full counts invariant | `8f7dca5` |
| GREEN: reject out-of-frame `pano_y`, report the shift, reconcile every outcome | `3b09721` |

* `compute_crop_box` returns `CropBox(left, top, size, shifted)`. The caller no longer re-derives
  `round(pano_y - size / 2)` to decide whether to announce a shift, so the announcement cannot drift
  away from the geometry producing it.
* `pano_y` outside `[0, pano_height)` is a counted `out_of_frame` skip.
* `pano_x` is deliberately **not** checked (see the wrong turn below).
* A legitimately near-polar label is de-centred rather than rejected, and says so: counted as
  `shifted_vertically` and logged with its offset from centre. It annotates a success rather than
  forming its own bucket, so the reconciliation still sums to `total`.
* The reconciliation docstring, wrong since `dims_mismatch` was added, now names all six disjoint
  outcomes — and a test asserts the sum from the counts dict rather than from the prose.
* The dims-preflight comment, README and CLAUDE.md now say what the check actually guards.

Mutation coverage went from 11/13 to **19/19 killed**, including the two that survived the review:
the size cap's `pano_width` term, and truncation in place of rounding for `left`.

## Wrong turns

* **"Two labels are out of bounds."** The first pass used `pano_x > pano_width`, when a pixel column
  index is out of range at `>= pano_width`. That missed the two CDMX rows storing exactly
  `pano_x == 16384`. A bound written from intuition rather than from what the index means.
* **…"so the bounds check should reject out-of-range x too."** Wrong, and the review had already
  said it before the data came back. Column 0 and column `pano_width` are the *same place in the
  world*: `compute_crop_box(16384, …)` returns byte-identical output to `compute_crop_box(0, …)`,
  still centred. An x-bounds check would have thrown away two perfectly good labels in the name of
  data hygiene. The asymmetry between the axes is the whole point — y has no wrap to appeal to, x
  needs none.
* **A differential test that agreed with a bug.** `extract_crop` was checked against an independent
  `np.roll`-based reference over 7,722 (pano shape × position × crop size) combinations, 0
  mismatches. That result is real but narrower than it looks: the reference computed its window with
  the same `min(size, width, height)` cap, so when the cap's `pano_width` term was removed as a
  mutation, reference and mutant agreed and the differential test passed. The mutant is a genuine
  defect — on a 200×600 image it produces a window whose darkest pixel is 0, i.e. #47's black,
  restored. It was caught by a property the reference does not share ("no synthetic black"), not by
  the comparison. A reference implementation that inherits your assumption cannot test that
  assumption.
* **Assuming the census could reuse the desk-study loaders.** `rawlabels.py`, `pov_replay.py` and
  friends live on an unmerged branch; this census is self-contained instead, which is better anyway
  — a PR's evidence should run on that PR's branch.
* **The `ast`-extraction trick for pinning `CropRunner` functions stopped working.** The older
  `reports/scripts` tests lift a function body out of the source rather than importing the module,
  because pre-#52.1 importing `CropRunner` ran the whole flow. A lifted body cannot see the
  module-level `CropBox`, so the pin broke the moment the return type changed. The new tests import
  `CropRunner` directly, which is safe now and pinned by `test_import_is_side_effect_free`.

## Open questions

* **The 6,634 already-poisoned crops.** A crop on disk is the resume marker and is never re-cut, so
  a store cropped before #47 keeps every one of its black-padded crops, and mixes 503- and 504-px
  crops for the same predicted size. This is documented in the README, but there is no `--force` and
  no migrator (contrast `migrate_depth_artifacts.py`). Whether to add one depends on whether any
  consumer is holding a pre-fix crop set.
* **How many stale-coordinate rows are actually out there.** The frame-change population is real —
  the era replay study found rows whose stored coordinates invert to a 6656-tall frame while the pano
  now serves 8192 — but nothing here bounds its size, because dims alone cannot identify it. That is
  #54's job.
* **The two corrupt Seattle rows are now skipped, not repaired.** Whether their `pano_y` is
  recoverable from the canvas/POV record is an upstream question.

## Where everything lives

| Artifact | Path |
|---|---|
| Census summary + the four out-of-frame rows (committed) | `reports/data/2026-08-10-crop-geometry-census.json` |
| Analysis | `reports/scripts/crop_geometry_census.py` |
| Census tests + committed-findings pins | `tests/test_crop_geometry_census.py` |
| Geometry, bounds and counts tests | `tests/test_crop_runner.py` |

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-opus-5[1m]).*
