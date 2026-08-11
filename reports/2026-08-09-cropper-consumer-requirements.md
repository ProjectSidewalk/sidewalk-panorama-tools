# Consumer requirements for a canonical Project Sidewalk label cropper

**2026-08-09** · Issues [#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54),
[#32](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/32),
[RampNet#113](https://github.com/ProjectSidewalk/RampNet/issues/113),
[Planning#6](https://github.com/ProjectSidewalk/Planning/issues/6)

This is a **requirements survey, not a measurement report**: it characterizes every downstream
consumer that would use a canonical cropper from this repo, and derives the numeric tolerances that
the cropper work package's studies (#54 placement, #32 sizing) will use as pre-registered decision
thresholds. Method: the consumers' code was read at the pinned commits below; every claim cites a
file/line or an issue quote. Nothing here was measured on imagery — the derived numbers are geometry
computed from the consumers' own constants.

| Repo | Commit read | Files |
|---|---|---|
| ProjectSidewalk/RampNet | `dc7450ea82` | `stage_one/crop_model/ps_model/data/download_data.py`, `ps_model/model/train.py`, issue #113 |
| ProjectSidewalk/sidewalk-validator-ai | `eb9bc65e0e` | `data_generation.py`, `fine_tune_classifier.py`, `README.md` |
| ProjectSidewalk/sidewalk-tagger-ai | `3b7405cd32` | `crop.py`, `download_and_process_test_dataset.sh`, `README.md`, `REPRODUCE_RESULTS.md` |
| ProjectSidewalk/sidewalk-ai-api | `94ee55a2d4` | `main.py`, `sidewalk_ai_api/panorama.py`, `tagger.py`, `validator.py`, `depthanything.py` |
| ProjectSidewalk/SidewalkWebpage | `ef42db3d50` | `docs/ai-subsystems.md` |

A shared fact worth stating once: `equirectangular_to_perspective` exists as three byte-equivalent
copies — `RampNet/.../download_data.py:125-171`, `sidewalk-validator-ai/data_generation.py:244-290`,
`sidewalk-ai-api/sidewalk_ai_api/panorama.py:22-80` — and all three repos also carry near-identical
private tile-stitch `fetch_panorama` implementations that resize the stitch to exactly **8192×4096**
(`download_data.py:123`, `data_generation.py:242`, `panorama.py:108,242-243`). Every number below is
derived against that 8192×4096 frame and the shared 90°-FOV/2048-px render (focal length
f = (2048/2)/tan(45°) = **1024 px**; on-axis scale **17.87 px/deg**, growing as sec²ψ off-axis —
21.1 px/deg at ψ = 23°).

---

## 1. RampNet 2.0 (crop-model training data)

### Projection and geometry

Perspective (gnomonic), fixed-pitch strips — **not** label-centered:

1. Stitch pano at tile zoom 4, resize to 8192×4096 (`download_data.py:19,123`).
2. Yaw from the label's normalized `Panorama X`, **snapped to 30°**:
   `nearest_theta = round(theta / 30) * 30` (`download_data.py:271-272`).
3. Render 90°-FOV, 2048×2048, at **fixed pitch −30°** (`download_data.py:273`).
4. Project the label point into the render (`download_data.py:274`), keep the **middle third**:
   columns `[682:1365)` → **683×2048 strip** (`:275`), shift the point by 2048/3 (`:276`), encode
   the point in the filename (`:277`). Other labels on the same pano are kept if they project into
   the strip (`:278-290`).
5. Training (`train.py`): filename keypoints ×0.5 (`:65-66`), image resized to 1024×352 (`:21,103-104`),
   Gaussian heatmap target on a **256×88 grid with σ = 12** heatmap px (`:23-33,110,117`),
   multi-keypoint max (`:88-90`), MSE loss (`:136`).

### Crop size

Constant by construction: every strip is 683×2048 render px ≈ 36.9° wide × 90° tall centered on
pitch −30°. Size never depends on the label.

### How centering error hurts

The defining property, in the issue's own words (RampNet#113):

> "RampNet never centers on the label — it renders a fixed strip and records where the label lands
> inside it. The object therefore always stays in frame; what moves instead is **the supervision
> target, off the object it is supposed to mark.** Nothing about the crop looks wrong."

Containment is guaranteed by construction: the 30° yaw snap bounds horizontal offset at ±15°, inside
the ±18.4° strip half-width, and the −30°-pitch render covers depressions from −15.5° to −75.5°. So
mis-centering **only** corrupts supervision, never framing.

### Numeric tolerance

Conversion chain (verified against #113's table): 1° of pano-frame error = 22.76 px on the 8192×4096
stitch = 17.9–21.1 render px = **2.24–2.64 heatmap px = 0.19–0.22 σ** (σ = 12). Hence:

- **1 σ of target displacement ≈ 4.6–5.4° ≈ 104–122 px of pano-y on the 4096-tall stitch** (the
  range is the on-axis/off-axis span of the line above; quote the range, not a midpoint).
- The measured defect, 1–3° of rig tilt, puts the target "0.2–0.6 σ off the object" (#113) —
  sub-σ: it degrades supervision rather than destroying it.
- A useful internal yardstick: the pipeline already carries a deterministic systematic of the same
  order. The 683-px strip is resized to width 352 but keypoint x is scaled by exactly 0.5
  (`train.py:65` vs `:21`); 683×0.5 = 341.5 ≠ 352, a scale mismatch reaching 10.5 input px =
  2.6 heatmap px ≈ **0.22 σ at the strip's right edge**. RampNet has been training through a ~0.2 σ
  x-bias without visible failure — corroborating "degrade rather than destroy", and setting the
  noise floor a canonical cropper should stay well under.
- **Derived requirement: label-point placement accurate to ≤ 0.5° in the pano frame (11.4 px
  pano-y at 4096-tall, 0.28% of pano height) keeps displacement ≤ 0.11 σ.**

### What a switch requires

Per #113's scope ("consume the canonical cropper … keep what is genuinely RampNet-specific (the
fixed-pitch strip convention) as a thin layer over that cropper's projection"):

1. Canonical equirectangular fetch at a deterministic, documented resolution with the
   **gravity/tilt frame stated**, not assumed.
2. A perspective-render primitive with free `(fov, yaw, pitch, height, width)`.
3. A **point-projection primitive guaranteed consistent with the image projection** — RampNet
   currently maintains both halves separately, which is exactly where a frame mismatch becomes a
   supervision error.
4. Tilt (pitch/roll) metadata cached alongside the pano (the #39 depth precedent — our v3 depth
   artifacts already do this).
5. Accuracy contract: residual `pano_y` error ≤ 0.5° (0.1 σ), against a current defect of 1–3°.

---

## 2. sidewalk-validator-ai (training data for the production validator models)

### Projection and geometry

Perspective, label-centered horizontally, horizon-pitched (`data_generation.py:382-425`): render
90°-FOV 2048×2048 at `theta = label_x_norm*360 − 180` (continuous), **`phi = 0`** — the label sits
on the center column at its natural depression. The label point is projected into the render
(`:393-403`), a square crop is centered there and clamped to render bounds (`:414-417`), resized so
the max side is 640 (`:419-424`), saved as webp. Class labels from agreement data: incorrect =
`disagree − agree > 1`, correct = `agree − disagree > 2`, mixed at 55% correct (`:87-101`).

### Crop size

Monocular metric depth at **the single pixel under the projected label point**:
`crop_size_half = int(1/depth[center_y, center_x] * 6100)` (`:410-412`), DepthAnythingV2-metric
vitl/vkitti, `max_depth = 80`. Since f = 1024 px, the crop frames a **constant physical footprint of
2·6100/1024 ≈ 11.9 m** at the label's depth — a 1.5 m curb ramp is always ≈ 12.6% of the crop side.

### How centering error hurts

1. **Depth is read at one wrong pixel** — the catastrophic, asymmetric mode. An upward error that
   lifts the point past the local horizon lands on background/sky: at the 80 m cap the crop is
   152 px framing a spot that isn't the object. A foreground occluder (pole at 2 m) explodes the
   crop to most of the render. Downward/lateral errors usually land on pavement at similar depth
   and are benign.
2. **Object off-center / out of frame** — secondary; the slack is large (see table).
3. **Border clamping** makes edge crops non-square, changing content scale after the resize.

### Numeric tolerance

Camera height ≈ 2.5 m, object = 1.5 m curb ramp, render f = 1024:

| d | crop (px, pre-resize) | object/crop side | hard **upward** budget = depression atan(2.5/d) | in render px | containment slack |
|---|---|---|---|---|---|
| 10 m | 1220 | 12.6% | 14.0° | ~256 px | 533 px ≈ 29.8° |
| 20 m | 610 | 12.6% | 7.1° | ~128 px | 267 px ≈ 14.9° |
| 40 m | 305 | 12.6% | 3.6° | ~64 px | 133 px ≈ 7.5° |
| 60 m | 203 | 12.6% | 2.4° | ~43 px | 89 px ≈ 5.0° |

- **Depth-sample-on-object condition:** error < object half-extent = 6.3% of the crop side
  (distance-invariant ratio). Beyond that, mild until the hard bound at the depression angle —
  which shrinks with distance, so **distant labels are the fragile ones**: at 40–60 m the whole
  budget is 2.4–3.6°, and the 1–3° tilt defect can already push a far label's depth read past the
  horizon.
- Containment is not binding (~15° at 20 m).

### What a switch requires

The same three primitives as RampNet, with the validator keeping its DA2 sizing rule on top. Plus:
**the canonical cropper must own the render-pitch convention.** Training renders at `phi = 0` with
the label below center; production inference (`sidewalk-ai-api/main.py:60`) renders the label
**dead-center**. Both are label-centered but the perspective foreshortening differs (~6% local
vertical scale at 10 m, plus ground-plane rotation) — a train/serve divergence a single blessed
"label-centered perspective crop" call would eliminate.

---

## 3. sidewalk-tagger-ai (training/eval data for the production tagger models)

### Projection and geometry

Flat, axis-aligned pixel crop — no projection at all (`crop.py:5-38`): read
`filename, normalized_x, normalized_y` from CSV, denormalize by the source image's own dimensions,
crop a **640×640 box centered on the point, clamped at borders**, overwrite in place. Source images
are the pre-cropped GSV regions in the HuggingFace dataset zips; `docs/ai-subsystems.md:53-56`:
"DINOv2 / CLIP multi-label classifiers over 640×640 label-centered crops, 33 tag classes." Models
consume at 256×256 (`sidewalk-ai-api/tagger.py:56-58`).

### Crop size

Fixed **640 px**, independent of distance, object size, and pano resolution — angular width floats
with the source pano: 14.1° on a 16384-wide pano, 28.1° on 8192. A 1.5 m ramp is ~61% of the crop
side at 10 m on a 16384 pano, ~15% at 20 m on 8192. The tagger is the one consumer with **no sizing
normalization whatsoever** (the anti-pattern the #32 study exists to retire).

### How centering error hurts

Label-centered, so error moves the object toward the edge and eventually out; tagging is
fine-grained (33 attribute classes), so salient details must stay visible. Border clamping silently
produces sub-640 crops near pano edges. No supervision point inside the crop — mis-centering
degrades the *input*, not the target.

### Numeric tolerance

Containment slack = 320 px − object half-extent (source-image px):

| pano width | d | object half | slack (px) | slack (deg) | as fraction of crop side |
|---|---|---|---|---|---|
| 16384 | 10 m | 196 px | 124 px | 2.7° | 19% |
| 16384 | 20 m | 98 px | 222 px | 4.9° | 35% |
| 8192 | 20 m | 49 px | 271 px | 11.9° | 42% |

Hard containment fails at ~2.7–4.9° on high-res panos; keeping the offset within ~10% of the crop
side (64 px) keeps the object comfortably central at all distances above.

### What a switch requires

A "flat equirect crop, fixed pixel size, centered, clamped" primitive — essentially `CropRunner.py`
minus its distance-based sizing. The more important requirement is **consistency of source**: the
deployed tagger checkpoints were trained on flat 640-px crops, yet at inference
`sidewalk-ai-api/main.py:61-92` feeds them the validator-style depth-aware perspective crop. The
tagger is already running with a train/serve crop-geometry mismatch; a canonical cropper must be
called by both dataset generation and `sidewalk-ai-api`, and the tagger retrained on whichever
geometry is blessed.

---

## 4. The de-facto fourth consumer: sidewalk-ai-api (production inference)

Re-implements the crop at serving time for both model families: fetch/cache pano (resized 8192×4096),
compute `theta, phi` so the label is exactly centered (`panorama.py:82-91`), render 90°-FOV 2048×2048
(`main.py:61`), DA2 depth at crop center — **vitb rather than the training-side vitl**
(`depthanything.py:15`) — `crop_size_half = 6100/depth`, clamp, resize max-side 640 (`main.py:71-81`).
It also demonstrates the cache contract a canonical cropper should serve:
`SCRAPES_DIR/<city>/<pano_id[:2]>/<pano_id>.jpg` (`main.py:52-54`) — exactly this repo's storage
layout. Divergences it must reconcile: pitch convention (label-centered vs `phi=0`), DA2 encoder
(vitb vs vitl), tagger crop geometry (flat vs perspective).

---

## Summary table

| | RampNet 2.0 (crop model) | sidewalk-validator-ai | sidewalk-tagger-ai | sidewalk-ai-api (serving) |
|---|---|---|---|---|
| Projection | Perspective, fixed strips (90° FOV, pitch −30°, yaw snapped 30°) | Perspective (90° FOV, `phi=0`, yaw = label lon) | Flat equirect pixel crop | Perspective (90° FOV, label dead-center) |
| Geometry | 2048² render → middle-third 683×2048 strip | 2048² render → square crop at label point | 640×640 box at label point, clamped | 2048² render → square crop at center |
| Size rule | Constant (strip) | 11.9 m physical footprint via DA2 depth at label pixel | Constant 640 px | Same as validator |
| Label-centered? | **No** — label projected into fixed strip | Yes (horizontal exact; vertical via crop) | Yes | Yes (render and crop) |
| Mis-centering failure | Supervision target moves off object; crop looks fine | Depth read at wrong pixel (size collapses/explodes), then off-center | Object drifts to edge / out | Same as validator |
| Hard tolerance | 1° = 0.19–0.22 σ; 1 σ ≈ 4.6–5.4° ≈ 104–122 px pano-y (4096-tall) | Upward < atan(2.5/d): 7.1° @20 m, 3.6° @40 m; depth-on-object < 6.3% of crop side | Containment < 2.7–4.9° (16384 pano, 10–20 m) | As validator |
| Cost of the 1–3° `pano_y` defect | 0.2–0.6 σ target displacement (#113's subject) | Minor near; up to ~full depth budget at 40–60 m | Negligible | As validator |

## Decision thresholds for the work package

**(i) Acceptable mis-centering.** The binding consumer is RampNet's supervision, not containment:

- **Target: label-point placement error ≤ 0.5° in the pano frame** (11.4 px pano-y on a 4096-tall
  stitch; 0.28% of pano height). Keeps RampNet's heatmap-target displacement ≤ 0.11 σ — below its
  existing ~0.2 σ internal noise floor — and an order of magnitude inside every other consumer's
  budget.
- Consumer ceilings for reference, as fraction of crop side: RampNet ~1% (0.2 σ ↔ ~1°); validator
  hard-asymmetric ~6% upward (degrading to 2.4–7° beyond 20 m); tagger ~10% practical, 19–42% hard.
- The status quo (1–3° tilt residual) violates the RampNet target by 2–6× and consumes most of the
  validator's far-field budget; it satisfies the tagger.

**(ii) Required containment margin.** Not binding once (i) is met — ≤ 0.5° centering error plus ~1°
margin per side guarantees containment for every consumer. What actually sets crop size is
**context**, and the consumers converge on **object ≈ 10–15% of crop side**: validator 12.6% by
construction (≈ 5.2 m margin per side for a 1.5 m ramp in an 11.9 m footprint); RampNet strip, a ramp
at 20 m ≈ 11% of strip width; tagger at typical range 15–31%.

Stated as a multiple, that is **crop ratio R = 6.7–10×**, where

> **R = crop side ÷ object extent = crop half-side ÷ object half-extent.**

Use R and nothing else. It is a ratio of like quantities, so it is invariant to whether both terms are
full extents or both are half-extents — which is exactly the confusion that has to be designed out
here. The margin-based restatements are *not* interchangeable with it:

| quantity | formula | at object = 10–15% of crop side |
|---|---|---|
| **crop ratio R** (use this) | `1/φ` | **6.7 – 10** |
| margin per side ÷ object half-extent | `R − 1` | 5.7 – 9.0 |
| margin per side ÷ object full extent | `(R − 1)/2` | 2.8 – 4.5 |

(φ = object extent ÷ crop side. Anchor check: the validator's `R = (11.9/2)/(1.5/2) = 7.9`, inside
6.7–10.) A canonical API should expose size as R (or as a physical footprint in metres) rather than
absolute pixels — the tagger's fixed 640 px, whose object fraction swings 15–61% (R = 1.6–6.7), is the
anti-pattern.

> **Correction, 2026-08-10 (pre-merge review).** This paragraph originally read "margin per side ≈
> 3–4.5× the object's half-extent" as an *i.e.* on the 10–15% range. That is the bottom row of the
> table above — margin over the object's **full** extent — mislabelled as half-extent, and the
> pre-registration then carried `[3, 4.5]` into Study 2's primary endpoint under a third definition
> (`crop half-side / object half-extent`), where the correct band is `[6.7, 10]`. The validator, the
> consumer that anchors the convention, scored *outside* the registered band. Corrected here and in
> the pre-registration before registration; the arithmetic is now pinned in
> `tests/test_margin_convention.py`.

**Cross-cutting requirements**: one fetch-and-stitch implementation at a declared resolution and
declared gravity frame; image-projection and point-projection guaranteed to share one camera model;
tilt metadata cached beside the pano; a deterministic version tag so consumers can pin dataset
provenance; and adoption by `sidewalk-ai-api` so train and serve geometry can never drift apart
again — the tagger's existing flat-vs-perspective serving skew being the cautionary example already
in production.

## How the studies consume this

- The **#54 placement study** pre-registers its "material mis-centering" threshold from (i): the
  correction ships if the confirmed residual exceeds 0.5° in the pano frame for a material fraction
  of labels (exact fraction set in the pre-registration alongside the power analysis).
- The **#32 sizing study** scores candidates at the sizing policy drawn from (ii) — object 10–15% of
  crop side, i.e. **crop ratio R ∈ [6.7, 10]** — stratified by distance band, with the validator's
  asymmetric far-field depth budget as the reason the placement fix and the sizing rework must be
  evaluated jointly.
- The **flat-vs-perspective design study** starts from the fact that three of four consumers already
  render perspective crops from the same three copied functions — the question it answers is
  implementation cost and migration, not whether perspective is wanted.

## Open questions

- Whether RampNet 2.0's strip convention changes (issue #113 leaves it open); the ≤ 0.5° target is
  convention-independent.
- The tagger retraining decision (flat vs perspective) is downstream of the design study, not of
  this survey.
- `sidewalk-ai-api`'s vitb/vitl and pitch-convention divergences are its own issues to file; noted
  here because a canonical cropper is the natural fix for both.
