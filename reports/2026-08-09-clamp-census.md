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

| | share |
|---|---|
| crop = 1500 px (near clamp) | **19.25%** overall — 6.8% (Signal) to 25.9% (Crosswalk); 7.8% (Newberg) to 31.7% (Amsterdam) |
| crop = 50 px (far clamp) | **0.000%** — unreachable on real geometry |
| wanted crop off bottom edge | **0.000%** (9 labels corpus-wide) |
| wanted crop off top edge | 0.000% |

A fifth of all labels — a quarter of crosswalks — get an identical 1500 px crop regardless of
their distance: every depression steeper than the clamp onset collapses to one framing. The far
clamp and edge truncation, by contrast, are non-issues: **the #77 y-shift machinery is safety
plumbing, not a hot path** (measured exposure ≈ 0), so the placement study does not need a
shifted-crop stratum.

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

**Geometry priors** the sizing study needs: label depression p10/p50/p90/p99 =
7.8°/15.4°/27.3°/43.5° — the corpus straddles the 11.25° cotangent-blend boundary, so both
regimes of the #32 candidate-B distance model carry real mass. Deployed-vs-blend distance
disagree by 0.69 m (p50) / 2.98 m (p90) per label.

## What this hands #32

* Replacement candidates must be **resolution-independent** (consume depression, not pixels) —
  the census shows the pixel-linear form is now a 20% systematic on 90% of labels.
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

*Written with [Claude Code](https://claude.com/claude-code) (claude-fable-5). The bottom-edge
truncation rate was expected to justify a shifted-crop study stratum; measuring it at 9 labels in
436k retired that stratum instead.*
