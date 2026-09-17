# What did Google's re-render actually change? Geometry, tone and sharpness on all 19 pairs

**2026-09-06** · Follow-up measurement for
([#114](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/114)), from the
[`fover` pilot](2026-09-05-fover-refetch-pilot.md)
([#73](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/73))

> **Reproduce:** `pytest tests/test_rerender_probe.py tests/test_rerender_probe_report.py` offline. The
> table and the figure below regenerate from the committed artifacts with
> ```bash
> python reports/scripts/rerender_reduce.py \
>     --probe reports/data/2026-09-06-rerender-probe.json \
>     --photometa reports/data/2026-09-06-rerender-photometa.json \
>     --figure reports/figures/2026-09-06-rerender-dy-vs-heading.png
> ```
> and the pose-drift block at the end, from the two committed censuses, with
> ```bash
> python reports/scripts/photometa_census.py --resummarize reports/data/2026-09-06-photometa-census.json
> ```
> The measurement itself needs both copies of each panorama and was run on makelab2, where the pilot
> store sits beside the production Seattle store:
> ```bash
> python reports/scripts/rerender_probe.py --old-store <production seattle dir> \
>     --new-store <pilot copy> --ledger <pilot copy>/refetch_log.csv \
>     --pilot-json reports/data/2026-09-05-fover-refetch-pilot.json \
>     --write reports/data/2026-09-06-rerender-probe.json
> ```

## The question, and why it mattered

The `fover` pilot re-fetched 200 Seattle panoramas against a copy of the store and found that of the 78
Google still served, **19 (24.4%) came back as a different picture**: same pano id, same 16384×8192 frame,
pixels differing from the stored file at the horizon band by far more than two encodes of one picture
differ. That was a by-product of the pilot, not its question, so nothing was known about *what* had
changed — and one thing about it mattered a great deal.

**If a re-render moves features, every stored `pano_x`/`pano_y` on those panoramas is wrong for the live
imagery.** Labels were placed on the rendering we scraped. A re-stitch with a re-estimated camera pose
would shift the referent under the coordinate. That is a different problem from a re-grade, which changes
pixels a model sees and leaves every coordinate correct.

Both versions of all 78 panoramas were on makelab2 — the stored frame in the production Seattle store, the
re-fetched one in the pilot copy — so this was measured rather than argued.

## Method

Per panorama, per band. The **horizon band** is the control: CBK served those tile rows at full size in
both eras, so it isolates a change in the picture from the polar-resolution question #73 was about. The
**bottom band** is the polar rows below it.

* **Displacement** — phase correlation on 16 windows of 1024×1024 px spread across each band (8 headings ×
  2 rows; the bottom band is 2048 px tall on a 13312×6656 frame, so there it is one row of 8), with
  parabolic sub-pixel refinement. A yaw change reads as a constant horizontal shift; a pitch/roll
  re-estimate cannot translate an equirectangular frame, so it reads as a vertical shift running as one
  sinusoid across heading. Every window also records its MAE **before and after** applying the estimated
  shift, so a spurious correlation peak cannot pass as a displacement. The per-window shifts are then
  reduced two ways, and the table shows both: the probe's **classifier**, whose thresholds were fixed before
  the run (`shifted` = at least half the locked windows moved by a pixel, agreeing to ±1 px, with a
  sub-pixel median of at least 1 px; `warped` = moved but not agreeing), and a three-component **fit** over
  the locked windows, `dy ≈ c + a·cos(φ) + b·sin(φ)` — heading offset (mean horizontal shift), vertical
  offset (`c`), tilt amplitude (`hypot(a, b)`) and the residual RMS the model leaves. Whether a panorama
  **moved** is the classifier's own first gate; the fit says what the movement looked like.
* **Tone** — least squares `new ≈ gain × old + offset` on luma, plus the residual after it. A re-grade is a
  change the fit removes; a re-render is one it does not.
* **Sharpness** — Laplacian variance, re-fetched over stored, **divided by gain²**. A pure contrast gain
  scales that ratio with no change in detail; the synthetic battery caught that confound before the run.
* **Photometa** — capture date, served size and current pose for all 78.

The 59 panoramas the pilot classed as same-rendering run through identical code and are the null.

## What came out

**The null is clean, which is what licenses everything below.** Across all 59 same-rendering panoramas the
horizon band differs by at most **0.0742** luma, every one of the 16 windows locks, the largest measured
shift is **0 px**, and the tone fit is the identity. The 19 start at **3.3823** — a **45.6× gap** with
nothing in between. Whatever the instrument says about the 19, it is not the instrument.

| pano | captured | classifier (pre-run) | moved | horizon MAE | heading offset px | vertical offset px | tilt amplitude px | fit residual px | tone gain / offset | sharpness × | bottom-band MAE |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `sJUrMnpJlG-xe1F4C4UPcQ` | 2018-10 | uniform shift | yes | 24.4 | -2.4 (-0.053°) | +0.9 (+0.019°) | 0.4 (0.008°) | 0.28 | 1.08 / +1 | 1.6 | 38.6 |
| `F3xUV6btmwM9LnPtPaYogA` | 2019-07 | windows disagree | yes | 18.4 | +3.0 (+0.067°) | -0.1 (-0.003°) | 7.2 (0.158°) | 0.21 | 0.92 / +8 | 1.8 | 9.7 |
| `5LKY_y72gH6MFAsG_xdglg` | 2019-06 | windows disagree | yes | 18.1 | +3.8 (+0.083°) | +0.1 (+0.002°) | 5.2 (0.115°) | 0.24 | 0.88 / +18 | 2.2 | 15.1 |
| `vqBgfAg5m8d4prZhuClfLA` | 2018-09 | uniform shift | yes | 16.5 | +1.2 (+0.025°) | +0.1 (+0.002°) | 0.5 (0.011°) | 0.17 | 1.03 / +4 | 1.6 | 12.9 |
| `st1BSHwdSV1XF1QiISncQA` | 2019-06 | windows disagree | yes | 15.2 | +2.0 (+0.045°) | +0.3 (+0.006°) | 1.7 (0.038°) | 0.16 | 0.91 / +11 | 2.0 | 8.8 |
| `QF6akwbkzbXmp-YX2OIVlQ` | 2022-09 | uniform shift | yes | 12.3 | +1.3 (+0.029°) | +0.4 (+0.009°) | 0.8 (0.017°) | 0.11 | 0.93 / +7 | 2.0 | 5.4 |
| `s9WoG61hak-5LI8FWmVq4w` | 2019-06 | windows disagree | yes | 11.6 | +1.6 (+0.036°) | +0.8 (+0.017°) | 2.8 (0.062°) | 0.46 | 0.95 / +6 | 1.9 | 6.4 |
| `koyB692YVKl5MYVMrRKqlQ` | 2023-01 | uniform shift | yes | 11.2 | +1.9 (+0.042°) | +1.0 (+0.021°) | 0.9 (0.019°) | 0.26 | 0.94 / +9 | 1.9 | 6.7 |
| `b17px-qNlqgD1nN3Kcx6HQ` | 2023-01 | uniform shift | yes | 9.9 | +1.5 (+0.033°) | +1.4 (+0.032°) | 0.6 (0.014°) | 0.28 | 0.96 / +6 | 1.7 | 8.4 |
| `HWvXpp4MJSQsOKUpEUhhFA` | 2022-09 | sharpened, no shift seen | no | 9.2 | +0.0 (+0.000°) | +0.0 (+0.000°) | 0.0 (0.000°) | 0.03 | 0.90 / +15 | 2.2 | 7.6 |
| `J1GLvc1i62EnweUZVJaQbA` | 2022-09 | uniform shift | yes | 8.6 | +1.4 (+0.030°) | +0.5 (+0.010°) | 0.7 (0.016°) | 0.11 | 0.97 / +5 | 1.8 | 6.4 |
| `1hmsxmHRB-ieRY2WNN1n1g` | 2015-08 | windows disagree | yes | 8.3 | -0.2 (-0.006°) | -1.4 (-0.037°) | 0.7 (0.018°) | 0.71 | 0.95 / +5 | 1.0 | 6.2 |
| `G8HiMBy3EqB3yAxR2Tgyzw` | 2019-04 | windows disagree | yes | 8.1 | +1.1 (+0.025°) | -0.9 (-0.020°) | 0.3 (0.007°) | 0.12 | 0.97 / +4 | 1.7 | 6.9 |
| `TQEXMTPPTyPaAQkWyRm1-w` | 2019-05 | no shift seen | yes | 7.9 | +0.3 (+0.006°) | +0.3 (+0.008°) | 1.2 (0.027°) | 0.49 | 0.97 / +5 | 1.0 | 10.9 |
| `WfLuTvukUoZGf-ldOjry-Q` | 2011-07 | windows disagree | yes, no consistent model | 7.8 | +0.4 (+0.010°) | -0.7 (-0.019°) | 0.7 (0.019°) | 1.07 | 0.95 / +6 | 1.6 | 14.3 |
| `Qd6QYq567lGBJmPHsi5yVA` | 2011-09 | no shift seen | yes | 7.3 | +0.4 (+0.010°) | +0.0 (-0.001°) | 1.2 (0.031°) | 0.50 | 0.95 / +7 | 1.1 | 10.8 |
| `Mb9A91CdjWBbez-kweqpxQ` | 2025-09 | no shift seen | no | 5.5 | +0.0 (+0.000°) | +0.0 (+0.000°) | 0.0 (0.000°) | 0.02 | 0.97 / +3 | 1.1 | 48.9 |
| `CpwhhT8VYk-RCGpSqa6s3w` | 2025-09 | no shift seen | no | 4.5 | +0.0 (+0.000°) | +0.0 (+0.000°) | 0.0 (0.000°) | 0.01 | 0.99 / +1 | 1.0 | 29.1 |
| `2MtE0TuWnZWqVJk7mIzwEg` | 2025-09 | no shift seen | no | 3.4 | +0.0 (-0.001°) | +0.0 (+0.000°) | 0.0 (0.000°) | 0.01 | 0.99 / +1 | 1.0 | 12.6 |

"Heading offset" is the mean horizontal shift over locked windows; "vertical offset" is the fitted constant;
"tilt amplitude" is the amplitude of the sinusoid fitted to vertical shift against heading; each is in px and
in degrees at that frame's scale (heading over the width, the vertical two over the height). "Fit residual" is
the RMS the three-component model leaves behind. "Moved" is the probe's own gate — at least half the locked
windows shifted by a whole pixel — and reads "no consistent model" when they did but no fitted component
reaches 1 px. "Sharpness ×" is the Laplacian-variance ratio net of gain, horizon band.

![Vertical shift against heading](figures/2026-09-06-rerender-dy-vs-heading.png)

### Reading it

1. **15 of 19 moved; 14 of them with a camera pose the fit can name.** On the four that did not, no locked
   window shifted by as much as a pixel (largest **0.9 px**) and every fitted component is under **0.03 px**;
   on the 15 that did, at least one window shifted by **1.8 px** or more. The fit puts a heading offset of
   at least 1 px on **11** of them, up to **3.8 px (0.083°)**; a vertical offset of at least 1 px on **2**, up
   to **1.4 px (0.037°)**; and a tilt sinusoid of at least 1 px on **six**, **1.2 to 7.2 px (0.027° to
   0.158°)**, which is what a corrected pitch/roll does to an equirectangular frame. The fifteenth,
   `WfLuTvukUoZGf-ldOjry-Q` (2011), moved on **5 of 9** locked windows by up to **3.8 px** and the fit
   explains none of it — residual **1.07 px**, larger than any component. Removing each window's measured
   shift roughly halves the per-window MAE across the 15 — median **11.4 → 6.2** luma — so the displacement
   is real, and what remains is the next item.

   The classifier fixed before the run flags **13**: it agrees with the fit on **12** of the 14, misses
   `TQEXMTPPTyPaAQkWyRm1-w` and `Qd6QYq567lGBJmPHsi5yVA` — pure tilts of **1.2 px** on **8 of 15** and
   **10 of 14** windows, which its `shifted` test cannot see because a sinusoid's median is zero and its
   `warped` test cannot see because ±1 px agreement swallows the amplitude — and flags `WfLuTvuk` on window
   disagreement alone. The first draft of this table printed 0 for those two and 0.7 px for `1hmsxmHR`,
   whose largest component is a **-1.4 px** vertical offset the fit had absorbed into its constant and
   dropped; every component is now shown for every row.
2. **13 were sharpened and re-graded** (Laplacian ratio ≥ 1.2× net of gain): high-frequency energy up **1.6
   to 2.2×**, and across those 13 a contrast gain of **0.88 to 1.08** with an offset of **+1 to +18** luma.
   Flatter and brighter with more edge energy. That is the opposite of denoising, and it is global, so it is
   not localised glare repair either. **12** of them are among the 15 that moved; `HWvXpp4MJSQsOKUpEUhhFA`
   was sharpened without moving, and `1hmsxmHR`, `TQEXMTPP` and `Qd6QYq56` moved without being sharpened.
3. **Four show no displacement at all.** Three are **2025-09** captures whose horizon barely moved (MAE 3.4
   to 5.5) but whose bottom band changed enormously — MAE 12.6 to 48.9, with mean signed differences of
   **-28.5** and **+20.4** luma on two of them. That is the nadir patch over the car being replaced, on
   imagery published within the last year. The fourth is the one sharpened without moving.
4. **Not an age effect.** Capture years run **2011 to 2025 in both groups**. Every one of the 78 is still
   served at its stored frame size.

So: a reprocessing pass whose components are a pose refinement, a sharpen-and-tone step, and a nadir-patch
update, applied to different subsets. "Quality improvement" is a fair description of the motive.

## What this changes

* **Label geometry survives a swap.** The largest displacement here is **0.158°** of tilt, **0.083°** of
  heading and **0.037°** of vertical offset. The [click-noise study](2026-08-09-click-noise.md) puts label
  placement sigma at **0.57°** azimuth and **0.51°** elevation, so a re-render moves a feature by at most
  **a third of one sigma** of the noise already on every label. Nothing in this sample would move a label
  off its referent.
* **But it is a different rendering.** Sharpness up 2× and contrast re-graded is a distribution shift for
  any crop consumer trained on the stored pixels — and the pilot's per-tile finding still stands: nothing
  Google now serves has detail the stored file lacks. So "harmless" for coordinates, **not "same" for
  pixels**. The store keeps the picture the labels were placed on; a swap trades that for a sharper one.
* **The store is an archive, and that is now written down.** See
  [ops → The store is an archive, not a cache](../docs/ops.md#the-store-is-an-archive-not-a-cache). A
  re-render at *identical dimensions* passes all four of `refetch_panos.py`'s swap gates — `dims_changed`
  catches only the ones that changed frame — so the repair path could today overwrite without proof the
  replacement is better. The gate that would close it is a horizon-band MAE against the stored file, which
  costs **no extra requests** (the fresh frame is already decoded at the point of the swap) and separates
  these two populations by 45.6×. It is not being built, because no writer runs; the rule is what stops the
  next one shipping without it.
* **For the [#43](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/43) decay
  measurement:** "alive" means the id is served, and here a quarter of alive ids serve a rendering that has
  been re-posed by a few pixels and re-graded. The stored rendering for those is ours alone.

## What it does not say

* **One city, n = 19 re-rendered**, drawn from panoramas with a label in the polar band. Whether 24.4% is
  Seattle-specific or age-specific is unanswered here — but see below, because it need not stay that way.
* **Displacement is measured on the horizon band only.** The bottom band is too smooth for phase
  correlation to lock reliably (**0 to 4** of a band's **8 or 16** windows), so the nadir finding rests on
  MAE and signed difference, not on shift.
* **The re-pose model is a heading offset, a vertical offset and a single sinusoid.** Its residual is in the
  table: **0.11 to 0.71 px** on the 14 it describes, so nothing larger is hiding in those — but a more
  complex warp would not be distinguished from noise, and on `WfLuTvuk` the residual exceeds every
  component, which is why that row says "no consistent model" rather than naming one.
* Thresholds (lock peak ≥ 0.10, "moved" at half the locked windows, "shifted" at ≥ 1 px sub-pixel median,
  "sharpened" at ≥ 1.2× net of gain) were **fixed before the run** against a synthetic battery of known
  shifts, re-grades and sharpening. The 1 px line the fit's components are read against reuses the
  classifier's own threshold, but that reuse was decided after the run, when the fit was added.

## How we would ever notice this again

Detecting a re-render in **pixels** requires the pixels, which is a full tile fan-out per panorama, and the
download path deliberately never re-fetches a panorama it already has. So there is no cheap image-side
tripwire and there should not be one.

There is a cheaper channel. **Photometa carries `heading`/`pitch`/`roll`, and 15 of these 19 moved** — so
pose drift is a re-render proxy at one metadata request rather than 512 tile requests.
`photometa_census.py --refetch` already re-asks a fixed 1,360-panorama manifest, already records pose on both
sides, and already joins them one-to-one; it simply never compared the pose. It does now (`pose_drift`),
which makes the standing decay census a re-render tracker for free, across every city in the sample rather
than Seattle alone.

**Run against the two existing censuses, it immediately finds the effect.** Of the **649** panoramas alive in
both, **21 (3.24%)** had their reported pose move by at least 0.05° on some axis in the 28 days between them,
and **95** moved by any amount at all. Maximum movement was **0.1065°** of pitch and **0.2918°** of roll —
which is squarely the "`camera_pitch` changing by .1 degrees or something" raised on
[#114](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/114), now measured rather than
recalled.

The structural detail is what makes it a signal rather than noise: **the 95 panoramas that moved on pitch and
the 95 that moved on roll are the same 95.** Jitter that touched each axis on its own would not move exactly
the same set; a pose re-estimation is one operation touching both, which is exactly what a re-stitch does.
(So is a re-serialisation of the whole rotation, which the joint movement cannot rule out — the magnitudes
are what argue against that, and the threshold below is the honest statement of how far they go.) No
panorama alive in both changed its served dimensions, so none of this is visible to any existing check.

Its limits are worth stating plainly:

* **It catches at most 15 of 19.** The four no-displacement panoramas — the pure re-grade and the 2025 nadir
  patches — are invisible to pose and stay undetectable by anything short of the pixels. It is a lower bound
  on re-rendering, never a count of it.
* **The 0.05° threshold is a reporting choice, not a validated detector**, and it cannot be validated: that
  would need each panorama's pose as of the night we scraped it, and nothing recorded it. The count of
  panoramas whose pose moved *at all* is reported beside it for that reason.
* **Pose drift is not the same measurement as the pixel displacement above.** These are Google's reported
  camera angles between two censuses; the table measures where the pixels actually went between scrape and
  now. They should agree in magnitude and here they do, but nothing forces them to.
* **Heading joins the census only from now on.** `extract_record` did not carry it before this change, so the
  two existing censuses can only be compared on pitch and roll — the axis a pure yaw re-estimate moves is the
  one the existing baseline is blind to.

## Where the data lives

* `reports/data/2026-09-06-rerender-probe.json` — thresholds, the two group summaries, and all 78 per-pano
  records including every window's shift, peak and before/after MAE.
* `reports/data/2026-09-06-rerender-photometa.json` — capture date, served size and current pose for all 78.
* `reports/data/2026-09-06-photometa-census.json` — the re-fetch census, whose `decay.pose_drift` block is
  the source of every number in the section above (regenerated offline by `--resummarize`).
* `reports/data/2026-09-05-fover-refetch-pilot.json` — the pilot that produced the pairs and the
  `RERENDERED_HORIZON_MAE = 3.0` split.
* `reports/scripts/rerender_probe.py` — the instrument, with its synthetic self-tests;
  `rerender_photometa.py`; `rerender_reduce.py` — the table and the figure above.

## The tests that pin it

`tests/test_rerender_probe.py` drives the measurement functions on **synthetic** pairs with known answers —
a known shift, a known re-grade, a known sharpening, the gain²-confound case that a naive sharpness ratio
gets wrong, and a uniform vertical offset that must come out as its own component rather than as a tilt or
as nothing. `tests/test_rerender_probe_report.py` re-derives every cell of the table above and every count
in the prose from the committed artifacts and asserts it appears in this markdown, per the repo's rule that
a report table is the one place a plausible number has no compiler.
