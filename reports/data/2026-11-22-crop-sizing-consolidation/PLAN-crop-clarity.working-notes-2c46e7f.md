# Plan: crop clarity — a measured acceptance test before Richmond ships

**Written:** 2026-08-14, from the sidewalk-auto-labeler session.
**Repos involved:** RampNet (eval data + annotation tool), SidewalkWebpage (CropService consumer),
sidewalk-auto-labeler (candidate sizing + possible bbox submission), sidewalk-panorama-tools
(reference only).
**Status:** plan recorded; nothing filed or built yet.

**Decision (Jon, 2026-08-14):** no new AI-submitted labels ship without more clarity around robust
and accurate cropping. This **adds a gate on top of** `PLAN-pano-hosting.md` — the architecture
there stands unchanged (server-side CropService, reconciliation job, isolated swappable sizing
rule), but its "ship sizing v1 now, don't wait for the study" gating no longer holds. The gate is
made concrete below so it is a measurement, not a feeling.

---

## 1. Why this is tractable in ~2 weeks, not months

Established by three repo surveys on 2026-08-14 (SidewalkWebpage read at `origin/develop`
`471304a`; RampNet at `main` `dc7450e` + unmerged YOLO branches; pano-tools at `master` + the
PR #87 worktree):

- **No gold-standard crop evaluation exists anywhere.** Every pano-tools report is a metadata
  census; the PR #87 annotation tool has produced no committed annotations; Tohme (2,862 boxes /
  741 panos, lab storage only) cannot score `predict_crop_size` — the formula was plausibly fit
  on it, and only 1 pano overlaps the modern DC database.
- ~~**But 3,919 human-drawn curb-ramp bounding boxes already exist**: RampNet
  `manual_labels/*.txt`.~~ **CORRECTED 2026-08-14 by measurement (RampNet#114,
  `docs/crop_window_eval.md` on the `analysis/crop-window-eval-114` branch): those w/h are NOT
  object extents.** Median box max-side is 12.8 px; box size tracks distance ~7× slower than
  a physical ramp's apparent size; only ~10% of boxes (52 all-big panos + mixed) are plausibly
  full-apron — the typical "box" is a near-point mark on the detectable-warning pad, visually
  confirmed. Center points are unaffected. Consequence: **no usable curb-ramp extent gold
  exists anywhere in the ecosystem**, on either provider.
- The 9-city benchmark has **~3,024 verified curb-ramp points** (1,833 TP verdicts + 1,191 missed
  marks) with native-res panos on disk for 7/9 cities. ~~`benchmark/bend/panos` and
  `benchmark/richmond/panos` are empty locally — re-fetch before annotating richmond.~~
  **CORRECTED 2026-08-14: richmond/panos has all 124 reviewed panos at their native
  resolution (each verified == its record's dims during the box-gallery render; heights are
  MIXED — mostly 11000×5500, some 12288×6144, 5760×2880, 4096×2048), and manual_gold/panos
  has all 1,000 — nothing blocks annotation.** (bend unverified, not needed for Phase 0.)
- **The annotation gap is therefore both providers** (revised 2026-08-14): tight whole-apron
  boxes on Richmond's 124 benchmark panos (~388 confirmed points, Mapillary) AND on a
  manual_gold subset (GSV — re-annotation with an explicit box rule; the existing near-point
  marks make good prompts to box around). Still hours-to-a-day of drawing, not weeks.
- Production context (verified in SW code): the only crop producer is the browser canvas
  screenshot (`Canvas.js saveCanvasScreenshot` → `POST /saveImage` → 1440×960 PNG), which is the
  user's **viewport**, label not centered, no computed window. CropRunner appears nowhere in
  production. Validate has zero crop dependency (`useCrops = false`); only the static-image
  surfaces (Gallery cards, landing grid, thumbnails, OG previews) need a crop, and only on
  non-GSV imagery. AI ingest writes `canvas_x/y = (360, 240)`, so a **centered** server-cut crop
  makes the composited marker correct by construction.

## 2. The acceptance test

For a crop generated from a detection point on a pano, scored against a gold box:

1. **Containment** — gold box fully inside the crop window (the visibly-broken failure mode);
2. **Context ratio** — object at ~10–15% of crop side (the consumer-survey band; note tagger-ai
   is a known outlier at 15–61%, so treat the band as a target, not a law). **Refined 2026-08-14
   (#114 Finding 3): that band is an ML-consumer number — padding a full-extent object model to
   it implies ~70° FOV windows. Set the band per consumer class: the human-facing Gallery card's
   right context level has never been specified anywhere and needs its own number** (the
   production canvas captures — whole 3:2 viewports — are the de-facto reference);
3. **Centering error** — offset of gold-box center from crop center;

each **stratified by distance band, pano resolution, and provider** (GSV vs Mapillary). That is
priority (1) — "accurate regardless of distance, resolution, provider" — phrased as measurement.

Eval sets: the 3,919 GSV gold boxes + the new Richmond Mapillary boxes.

## 3. Phase 0 — make quality measurable (~1 week, parallelizable)

- **Box annotation tool**: a **separate second-pass tool** in RampNet (e.g.
  `scripts/box_gallery.py`), sibling of `gt_gallery.py`, sharing its rendering helpers — NOT a
  mode inside the point tool. Design decision and rationale in §6.
- **Annotate Richmond**: re-fetch `benchmark/richmond/panos`, Jon draws tight boxes around the
  ~388 confirmed points. (Optionally a second Mapillary city later for robustness.)
- **Scorer**: point + pano → candidate crop window → containment / context ratio / centering vs
  gold, stratified. Pure Python over records + boxes; no GPU.

## 4. Phase 1 — score three candidates (~1 week)

| candidate | what | notes |
|---|---|---|
| sizing v1 | resolution-normalized `predict_crop_size` (SW#4865's planned default) | the baseline to beat; pixel-linear distance fit from ~2013, 19.25% clamp rate, 1.198× resolution inflation already measured |
| geometric v1.5 | depression angle → distance (`geo.py`), per-pano camera height where available (labeler#40), metric ramp footprint → pixels → pad to context ratio | ~a day to build; weak on Mapillary camera height (unknown, assume ~2.2 m) and on hills (São Paulo) |
| SAM2 point-prompt | RampNet#83 path 1: prompt SAM2 at the detection point on a native-res crop, tight box, pad to context ratio | ~1 GPU-day to evaluate; the only candidate robust to distance/resolution/provider by construction; failure modes (occlusion, snow, ambiguous extent) are what the eval measures |

**Explicitly NOT candidates / NOT gates:**

- **YOLO extent.** The YOLO comparison (RampNet#51) trains on **pseudo-boxes synthesized by the
  same flat-ground pitch heuristic** (`prepare_yolo_dataset.py`, 1.8 m ramp, fixed camera
  height) — its box supervision IS the heuristic, so its predicted w/h cannot validate extent.
  Separately: run the pre-registered benchmark F1@0.25 eval (implemented, never run for any arm)
  to settle the *detector* question — decoupled from this plan. RampNet#95's confound warning
  (240× training-budget asymmetry) applies to interpreting it.
- **The impedance-region pipeline (labeler#47).** Right long-term direction for
  Obstacle/SurfaceProblem, wrong Richmond gate: its stage-1 asset (harvested GSV depth, #41) does
  not exist for the four Mapillary cities where the crop gap actually bites, and camera-height QC
  is unresolved (#44). Its two cheap proposed measurements are worth running as research. Note
  its "every formula-crop consumer is an ML pipeline" claim goes stale the moment SW#4865's
  CropService ships — that makes a formula-cut crop the human-facing Gallery image for AI labels.
- **The pano-tools crowd-label study** (PR #87 corpus). Different question (crowd click noise,
  the #4784 y-error, all label types); keep decoupled. Merging #87 is fine — tooling and corpus
  are real — but its scope-decision narrative rests partly on unmeasured numbers (the "1–3° tilt
  vs ≤0.5° target" pillar traces to a requirements survey citing RampNet#113, which itself calls
  the effect sub-σ).

## 5. Phase 2 — ship Richmond with whichever passes

- **v1 or v1.5 passes** → implement as the isolated sizing function in SW's CropService
  (PLAN-pano-hosting PR-A2). No schema change.
- **SAM2 wins decisively** → use the **pre-seamed contingency** in PLAN-pano-hosting §5:
  labeler computes boxes offline over its archive (3,685 label-bearing Richmond panos is cheap),
  submits per-label bboxes (nullable columns + reader fields), CropService prefers padded-bbox
  over heuristic. The heuristic fallback must exist regardless (crowd labels, Vancouver's ~64.8k,
  any point-only submitter).
- Crops remain derived data under the reconciliation job either way — a later better rule
  regenerates everything retroactively.

**Parallel, unblocked, needed under every outcome:** the plumbing PRs — SW#4865 PR-A1 (keyed
`PUT /ai/panoImage/:panoId`) and labeler#46 (resumable pano upload). Start now; no sizing
decision changes their design.

## 5a. The sizing rule to ship (settled 2026-08-18, after three blind human rounds)

Phase 1 of §4 is answered. The three candidates are **equally accurate** — scale-free
p90/p10 spread 1.8–3.2× in every city — so the question was never "which rule" but "which
constant, cut how". Review page with all charts and worked examples: `crop_plan.html`
(session scratchpad).

**SHIP: `v1-norm`, in degrees, ×2.5, clamped 8°–90°, cut 3:2 by width, stored at
`min(window, 1440)` without upscaling.**

### The recommendation moved twice; both moves are the useful part

| draft | constant | why it was wrong |
|---|---|---|
| 1st | ×4.5 | maximised **containment** (whole apron inside the window) — monotone in window size, so it always prefers a bigger crop |
| 2nd | ×1.25 | maximised a **framing** metric whose acceptance band I assumed (fill 0.45–1.10) rather than measured |
| final | **×2.5** | the band was measured: blind judgement puts "too tight" above fill **0.49**, and nothing tested was ever "too wide" |

**Method lesson, now twice-earned: never choose a crop size with a criterion that improves
without bound as the crop grows** (2026-08-14 it mis-ranked *rules*; 2026-08-18 it
mis-ranked *constants*). And: do not assume the acceptance band — elicit it.

### The three blind rounds (Jon judging, rule labels hidden from the DOM until export)

1. **Forced choice, 132 comparisons, candidates ×1.0–×4.5.** Bradley-Terry: ×2.0 first
   (+0.91), then ×1.5, ×1.25, v1-raw, ×1.0 (−0.96), ×4.5 last. Same order in every distance
   band and every city. Fitted ideal fill 0.43. Self-consistency 6/7 on hidden repeats; no
   side bias (48% left, p = 0.77) — which matters because these votes were cast on the
   pre-counterbalancing build where v1-raw sat left 67% of the time.
2. **Forced choice, 112 comparisons, ×1.5–×4.0** (fresh ramp sample, counterbalanced).
   ×3.0 first, then ×2.5, ×4.0, ×2.0, ×1.5 far last. Fitted ideal fill 0.28.
   **The two rounds disagree**, and it is not the sample: refitting round 1 on only the
   comparisons inside round 2's fill range still gives 0.44. A forced choice grades against
   whatever else is on screen, so its optimum drifts with the option set.
3. **Absolute judgement, 144 crops** (too tight / just right / too wide), fills sampled
   log-uniformly 0.12–1.79. **Zero "too wide" verdicts**; "too tight" sets in above
   **fill 0.49** (95% CI 0.46–0.54), repeats agreed 92%. Because the lower edge was never
   observed, the ordered-logistic "target fill" is unidentified — *ignore the 0.02 the
   optimiser reports*; only the upper threshold is estimable. Sharpness confound tested and
   dismissed (adding log-upsample: LR 1.19, p > 0.05).

**Combining the instruments:** the absolute round sets a floor (median crop clears "too
tight" only from **×1.95**), the forced-choice rounds locate a preference peak at fill
0.28–0.44 (**×2–×3**). They overlap at **×2.5**, where 68% of crops clear the threshold
(×2.0 → 50%, ×3.0 → 79%, ×1 → 7%). Per-city floors: Paterson ×1.75, São Paulo ×1.75,
Richmond ×2.06, Annapolis ×2.68 — the same apparent-ramp-size ordering the containment
analysis found, via a completely different instrument.

### Two constraints from `ImageController`, independent of the constant

Both raised on SW#4865 (comment 5337240084):

- **Cut 3:2.** `resize()` scales to 1440×960 with `getScaledInstance` — no aspect
  preservation — so a square window is stretched 1.5× on write. 3:2 is already the shape of
  stored crops, share images and the label canvas (720×480, AI ingest writes 360/240).
  Measured, shape barely moves framing quality (74% at 1:1, 3:2, 2:1 alike); it moves
  vertical fill (0.26 / 0.39 / 0.52), and aprons are ~3:1.
- **Stop upscaling.** Windows narrower than 1440 source px are stretched to fill the file;
  at ×1 that is **89% of crops, median 4.0×** (Richmond 95%, Annapolis 98% above 2×). The
  ramp's real detail is fixed regardless of constant. Store `min(window, 1440)`.

### Acceptance run before ship

≥65% of crops clearing the measured threshold pooled, no city below 45%, no window over 90°;
~40 eyeballed crops from a city with no extent gold (bend or morgantown); one Gallery card
checked end to end, since ×2.5 is visibly wider than anything that surface has shown.

### Still open

A **second judge** (the instrument is built, ~7 min/round) would show whether fill 0.49 is
personal or general — every number in the human study is one person's eye. Per-city
calibration (~100 boxes ≈ a day) is worth it only where framing looks wrong, Annapolis-like
cities first. And **RampNet#83 extent-aware sizing** remains the only route past the y-only
ceiling (R² 0.43–0.74 vs an extent oracle's 0.72–0.95); CropService needs the heuristic
regardless, for crowd labels, Vancouver's ~64.8k, and any point-only submitter.

## 6. Box tool design: separate second-pass tool, not a mode (decided 2026-08-14)

Jon's requirement: point-only GT review must remain pursuable **distinctly** for new cities.
The answer is a sibling script that **consumes a finished `verdicts.json`** and writes a separate
sidecar — the point tool is untouched. Rationale:

1. **Sequencing.** Boxes are drawn around *adjudicated* ramps (TP verdicts + missed marks),
   which only exist after point review completes. It is naturally a second pass.
2. **Protocol integrity.** Nine cities of verdicts were produced under the current point
   protocol; future cities must be comparable. A drawing mode inside the verdict UI risks bias
   (extent ambiguity leaking into existence judgments; drawing friction discouraging
   confirmations) and protocol drift.
3. **Schema.** `verdicts.json` is sync-locked to `rampnet.validation.collect` ("keep the two in
   sync"). Boxes live in a separate `boxes.json` keyed by `(pano_id, det:<idx> | missed:<idx>)`,
   so validation code never changes and point-only cities stay first-class.
4. **Resolution rules differ, and would conflict in one tool.** Point review deliberately
   renders at model resolution (4096×2048) so the reviewer never sees more than the model — the
   recall-fairness rule from RampNet#26. Box drawing *wants* native resolution for tight edges,
   which is legitimate precisely because extent annotation is not a recall judgment. Two tools
   make each rule load-bearing; one tool with a toggle invites a session that views native-res
   then judges recall.
5. **Repo idiom.** RampNet already does one-script-per-task galleries (gt_gallery, miss_gallery,
   fp_gallery, make_tagger) sharing helpers.

Implementation notes: crop-centered view per ramp with the point pre-marked; drag-to-rect;
must handle the equirectangular seam (RampNet#43 — a ramp split across the left/right edge);
export normalized coords so boxes are resolution-independent.

**Blindness policy (decided 2026-08-14).** The drawing pass shows NO predicted crop window. The
annotator already sees the point, so a displayed window leaks exactly one thing — a size prior —
and size is the quantity being measured (a window at ~7–10× object side is a strong scale
anchor). This matches pano-tools PR#87's own standard ("blindness is by construction"). The
"how good/bad is the current algorithm" eyeball belongs in the **scorer's overlay gallery**
(gold box + predicted window + metrics per ramp), which:

- runs on the 3,919 manual_gold GSV boxes **immediately** — annotation already done and locked,
  zero bias risk, and it delivers the v1 baseline verdict before any Richmond box is drawn;
- doubles as the post-export review view for Richmond (at ~388 boxes, the full pass is an
  afternoon, so review follows quickly).

Fallback if mid-session feedback is wanted anyway: reveal only after the pano is submitted and
locked (one-way, no un-reveal-and-edit), record `revealed: true` + annotation order per pano so
a drift check (agreement with the heuristic vs annotation order) is possible afterward — and
accept the residual cross-pano learning risk this cannot remove (seeing the heuristic's habits
after every pano can shift later drawing). Default is fully blind.

## 7. Also on the ship checklist (not crop work)

- **SW#4764**: the daily AI validator excludes Mapillary panos AND AI-authored labels — Richmond
  AI labels get human-only QA. Decide deliberately before the release cut.
- **Vancouver backfix** needs nothing from the Mapillary work: once CropService + reconciliation
  exist, the option-B GSV live-fetch floor reaches its ~64,824 labels retroactively.

## Status log

**2026-08-14 (evening) — Phase 1's harness is built and the first numbers are in.**
RampNet#114 filed; branch `analysis/crop-window-eval-114` pushed (commit `ea7a618`):
`scripts/analysis/crop_window_eval.py` + 18 tests (514 pass) + `docs/crop_window_eval.md` +
committed summary JSON + an overlay gallery (fresh-GSV sample fetch, view-only bytes).
Findings, in order of importance:

1. **manual_gold's boxes are not extent gold** (see corrected §1 bullet) — reported on #114
   and cross-posted to RampNet#83, whose SAM2-eval premise it qualifies. Phase 0's annotation
   scope now includes a GSV set, not just Richmond.
2. **The v1 resolution defect is real and large**: at 2048-height panos, raw
   `predict_crop_size` windows are **1.97×** the normalized ones (median 294 vs 149 px) —
   direct confirmation the CropService port must normalize (it does, per SW#4865's spec).
3. **Containment of the marked point is a non-problem** for every rule (≥97.5%); the
   interesting acceptance criteria are context ratio and whole-apron containment, both of
   which wait on real extent gold.
4. **Detection coverage at 0.55**: 87.3% of gold ramps would get a crop; 190 operational
   detections match no gold ramp (would-be FP crops).

Next: the box-annotation tool (`box_gallery.py`, per §6) → Jon annotates Richmond + a
manual_gold subset → re-run the harness → pick the sizing rule.

**2026-08-14 (late evening) — the box tool is built and Richmond is ready to annotate.**
RampNet#115 opened (the #114 harness branch, unmerged) and RampNet#116 filed → built →
PR RampNet#117 (branch `tool/box-gallery-116`, commit `a3bf20a`): `scripts/box_gallery.py`
+ 13 tests (suite 514). Everything §6 specified is in: second-pass over `verdicts.json`,
separate `boxes.json` sidecar keyed `(pano_id, det:<i>|missed:<i>)`, native-res crops with
the seam stitched (#43), pano-normalized `{cx, cy, w, h}` exports, fully blind (no
predicted window), plus the post-#114 requirement: an explicit **BOX_RULE v1** in the UI,
versioned and embedded in every export. Extras that fell out of the design: prefill
reconciliation via stored prompt points (a revised verdicts.json that renumbers
`missed:<i>` can't re-attach a box to the wrong ramp), auto `edge_flag` when a box touches
a non-pano crop edge, `--fov` re-render that keeps all annotations, and
`--from-manual-labels --sample-panos N` for the GSV re-annotation arm. The real Richmond
gallery is rendered at `<RampNet>\benchmark\richmond\box_gallery\index.html`:
**310 adjudicated ramps on 92 panos** (TP dets + sure missed; unsure abstains — hence
fewer than the ~388 confirmed-point figure, which counted unsure marks), zero
skips, 3072 px crops, 221 MB. BOX_RULE v1's contested choices (flares IN, occlusion
inferred, corner aprons = one box per direction of travel) are one string constant — edit
+ bump the version before drawing if the convention should differ.

Next: Jon draws Richmond (~an afternoon at 310 boxes) → export over
`benchmark/richmond/boxes.json` → same for a manual_gold subset
(`--from-manual-labels --sample-panos 50`) → point the #114 harness at real gold →
pick the sizing rule (§4).

**2026-08-14 (night) — first real extent gold scored; the v1 verdict INVERTS.**
Jon annotated **112/310** richmond ramps (110 boxed + 2 can't; single session, tool
feedback folded back in: scannable rule bullets, legend/controls under the image,
shift-pan — RampNet#117 `20608f7`; BOX_RULE bumped to **v2** adding ruler-not-a-crop +
oblique-AABB bullets after two design discussions: context belongs to the crop rule and
is *measured* (new directional road-margin metric), and axis-aligned boxes are the
sufficient statistic for axis-aligned windows, so no rotated boxes, no polygons — tight
AABB with empty corners is correct; polygons deferred to box-prompted SAM2 if ever
needed). Quality check: per-band box medians run ~1.2–1.3× the 1.5 m flat-ground
nominal — true full-apron extents (manual_labels sat at 0.13×). Installed as
`benchmark/richmond/boxes.json`.

Harness `--bundle` mode built on the #114 branch (RampNet#115, commit `777cc27`,
22 tests, suite 523) + committed `analysis_out/crop_window_eval_richmond.json` + a
110-crop overlay gallery. **Finding 4 (INTERIM, n=110, det-prompted n=88): whole-apron
containment v1-raw 0.534, v1-norm 0.295, geo-v1.5 1.000** — both v1 variants produce
object-sized windows (ctx p50 0.8–0.96), failure grows with proximity (v1-raw → 0.00 at
<5 m). The resolution normalization is *correct and still loses* (richmond heights sit
below the 6656 calibration height): raw's defect was masking a formula that can't
contain a full apron. **Shipping SW#4865's port as planned = shipping the worst rule
for Richmond.** geo-v1.5 contains everything at ~2× the ML context band (per-consumer
question). Ranking is outside the CIs at n=110 — more richmond boxes refine strata but
won't flip it; the load-bearing next dataset is the **GSV arm** (manual_gold
re-annotation), where native heights sit *above* 6656 and raw over-sizes instead.
Posted as an interim comment on RampNet#114.

**2026-08-14 (later that night) — RICHMOND EXTENT GOLD COMPLETE; Finding 4 is final.**
Jon finished all **310/310 adjudicated ramps** (299 boxed + 11 can't-determine, all 92
panos, one day, rule v1→v2). Gold committed as `benchmark/richmond/boxes.json` on the
RampNet#117 branch (`72646fa`); final scoring committed on the RampNet#115 branch
(`511c7a7`); final comment on RampNet#114. **Det-prompted n=227: v1-raw 0.449
[0.39–0.51], v1-norm 0.260 [0.21–0.32], geo-v1.5 0.996 [0.98–1.00]** — stable
112→246→299, converged not sample-limited. geo's single miss = far-field placement
slack (231 px window vs 187 px box at ~28 m; det-prompt off-center) → if geo ships, add
a far-field padding floor. Per-band gold medians 1.2–1.3× the 1.5 m nominal confirm
true full-apron extents. §4's Phase-1 table is now two-thirds answered on the Mapillary
side: v1 fails, geo-v1.5 passes containment with context ~0.23 (vs 0.10–0.15 ML band —
per-consumer decision outstanding). **Remaining before the ship decision: the GSV arm
(`--from-manual-labels --sample-panos 50`, Jon to draw), per-consumer context targets
(§2 refinement), and optionally the SAM2 arm for framing consistency (geo's ctx p10–p90
spread is 0.14–0.40 at a fixed target).**

**2026-08-14 (deep PR review of RampNet#115/#117) — FINDING 4'S RANKING WAS A SCALE
ARTIFACT; the recommendation for SW#4865 inverts back.** Both PRs are now MERGED
(RampNet#117 → `1056907`, RampNet#115 → `bc35181`) with the fixes below.

Containment and context ratio are **both monotone in window size**, so the rule that is
4.5× bigger wins one and loses the other by construction — the ranking above measured
scale, not accuracy. Scoring predicted side against `required_side` (the side box+prompt
actually demand, a property of the gold rather than of any rule) gives a p90/p10 spread
of **v1-raw 3.24, v1-norm 3.02, geo-v1.5 3.08**: the three rules are equally accurate and
differ in one constant. The new rescale sweep (exact re-scoring, pano-dim cap included)
compares them at *matched* containment:

| rule | k for ≥99.5% containment | ctx p50 | side p50 |
|---|---:|---:|---:|
| v1-raw | ×3.0 | 0.291 | 1020 px |
| v1-norm | ×3.5 | 0.291 | 1063 px |
| geo-v1.5 | ×1.0 | 0.229 | 1458 px |

v1 reaches the same guarantee in a **~30 % smaller median window**. So: **ship SW#4865's
normalization and give it a scale constant (~3.5×)** — a one-line change, not a swap to
geometry. Upstream's own docstring settles that the normalization is the *faithful* port
(`old_pano_y` is documented as converting to the coordinate space the fit was made in;
that conversion is exactly what is missing). The finding that survives is the spread
itself: nothing is within ~1.75× of the right size at either tail at *any* scale — that,
not the constant, is the case for extent-aware sizing (RampNet#83).

Two corrections that came with it: geo-v1.5's 1.83× overshoot of its own 0.125 target was
the **camera height** — `--cam-height 1.7` lands it at 0.156, exactly what the gold's
1.2–1.3× apron measurement predicts (2.5 m is a GSV figure; Richmond is Mapillary), and
only the height × nominal-width *product* is identified from one city. And **aprons are
3.29:1** in equirect pixels while every candidate cuts a square, so containment is decided
by width alone and geo's vertical context ratio is 0.068 (~15 box-heights) — most of the
"enormous windows" objection is the square-window assumption, not the sizing rule.

**The GSV arm cannot do what §4 assumed.** Every `benchmark/manual_gold` record is
4096×2048, so `--from-manual-labels` scores at pano_h = 2048 — *below* the 6656 calibration
height, the same regime as Richmond, not the >6656 regime where the raw formula over-sizes
— and it annotates at 1024 px crops vs Richmond's 2750–3072. Its real value is a second
provider and a check on whether the ~3.5× constant transfers. (9 of Richmond's own 92 panos
are 4096×2048 too; both tools now record `crop_px_by_pano_dims` rather than leaving it
assumed.) **Method lesson to carry forward: never rank sizing rules on a metric that is
monotone in size.**

The review also closed two silent annotation-loss paths in the box tool (per-pano prefill
merge deleting other sessions' boxes on export; the stale-box guard never firing on the
revise-in-the-same-browser path) — see the merged PRs for detail.

Replicability audit (Jon's ask, same night): everything derivable is committed — item
list from verdicts+records, versioned rule embedded in exports, tool+scorer+constants,
gold (`boxes.json`, RampNet#117 branch), scoring JSON w/ per-box-CSV sha256 (#115
branch), and the three session snapshots + README under
`benchmark/richmond/box_annotation_log/` (`a78b54f`) making the convergence trajectory
regenerable. Pixels remain archive-anchored (makelab2 + imagery_manifest), as for the
whole benchmark. Known accepted gap: no `--annotator` flag (prefill/localStorage assume
one annotator) — declined as unlikely to be needed; if a second annotator ever happens,
render WITHOUT prefill (see the log README). Both PRs ready for review; branches are
independent and merge cleanly.

**2026-08-17 — the GSV arm closes, the constant transfers, and the residual is
extent (not orientation).** Paterson's box session ended at **109 boxed + 10
can't-determine over 30 panos** (RampNet commit `10160b6` on
`data/paterson-extent-gold`; verified a strict superset of the 61-item checkpoint).
The pano mix (28 × 16384×8192, one 13312×6656, one 3328×1664) is the first extent
gold scored *above* the 6656 calibration height — the thing `manual_gold` structurally
could not do.

**The n=58 reading is corrected: the ~3.5× constant does transfer.** At n=86
det-prompted, k for ≥99.5% containment is **v1-raw ×3.0 (richmond ×3.0), v1-norm ×3.0
(richmond ×3.5), geo-v1.5 ×1.0 (×1.0)**. So "ship SW#4865's normalization with a scale
constant of ~3.0–3.5×" now survives a second city and a second provider. Paterson's
scale-free spread is also tighter than Richmond's (1.94–2.44 vs 3.02–3.24).

Jon's question — a ramp is labeled from behind, from in front, and from nearly overhead
when the car is on top of it, so orientation should drive apparent size as much as
distance — was measured over both cities' gold (richmond 299 + paterson 109), using the
harness's own `required_side`/rule functions. Three results:

1. **Azimuth is nearly worthless as a predictor.** A full angular basis (cos/sin of az
   and 2az) on top of the best depression-only fit to log(required_side) moves R² by
   0.014–0.034 (richmond det 0.478 → 0.492; paterson 0.600 → 0.634). Where a ramp sits
   in the image does not tell you which way it faces. The cheap version of the idea —
   a directional term in the formula — is dead.
2. **The variance is real, and it lives in extent.** An oracle knowing the true
   cross-range extent lifts the same fit to **0.721 / 0.816** (resid sd 0.452 → 0.331,
   0.344 → 0.233); implied extent spans 1.58–4.64 m p10–p90. This is a *ceiling*:
   any y-only rule tops out at R² ≈ 0.48–0.60 / ~×1.8 p90:p50, extent-aware reaches
   ~0.72–0.82 / ~×1.4. It is the quantitative case for RampNet#83 and is not reachable
   by a better closed form.
3. **The orientation effect lands on shape, not size.** Pixel aspect median 2.85–3.45:1
   (p90 ~6:1); in metres the boxes imply ~2× more radial depth than cross-range width,
   and **W/D is exactly invariant to camera height** (both scale with H), so that
   survives labeler#40's per-pano-height problem. The implausibly deep implied footprint
   (~4.4 m median) points at the ramp's rise, the curb face, and **AABB inflation on
   oblique ramps** — BOX_RULE v2's axis-aligned choice, working as designed. Prompt
   placement is a secondary term: median 1.19–1.20× inflation of the required window.

Posted as a comment on RampNet#114.

**Annapolis is next, and it resolves a confound.** Paterson's y-only fit is much better
than Richmond's (0.600 vs 0.478) and provider/pose noise is confounded with pano
resolution. Annapolis is Mapillary at 8000×4000, so it separates them: looks like
richmond ⇒ provider, looks like paterson ⇒ resolution. That decides whether a
Mapillary-specific scale constant is needed. Gallery is rendered (294 items).

**New, filed 2026-08-17: RampNet#126 — Mapillary Vistas as a second supervision
source.** Two arms, deliberately kept out of this plan's scope: an off-the-shelf
Vistas-trained segmenter as a zero-training curb-ramp baseline (Vistas carries a curb-cut
class). **Corrected on Jon's catch the same day:** that arm is *not* "the first external
comparison" — `docs/model_comparison.md` already scores eight external models (2 Geminis,
2 Qwens, Molmo, OWLv2, Grounding DINO + RampNet) over all nine splits and manual_gold,
plus a ninth gemini-3.7-flash leg. What the roster lacks is a **class**: every challenger
there is zero-shot (prompted VLM or open-vocab detector), none supervised on curb ramps
from another dataset — and OWLv2/Grounding DINO's recall 0.85–0.97 at precision 0.03 is
precisely the "concept findable, discrimination absent" result that a supervised-transfer
arm would test. Second arm: a
route to non-ramp label types (crosswalks etc.) without a new annotation campaign. Its
third, secondary note is the one that touches this plan: street/sidewalk masks give the
curb line — hence orientation *measured* rather than inferred, which is exactly what
finding (1) above says cannot be recovered geometrically — and a metres-per-pixel
reference needing neither camera height nor flat ground, the two assumptions that break
on São Paulo's hills.

**2026-08-17 (later) — ANNAPOLIS GOLD LANDS AND RETRACTS THE MORNING'S CONSTANT CLAIM.**
131 boxed + 11 can't over 42 panos, all 8000×4000 (RampNet `bfcdf9f` on
`data/annapolis-extent-gold`). Three results:

1. **The provider/resolution confound resolves against resolution.** Annapolis is
   Mapillary at *higher* resolution than most of richmond, and behaves like richmond:
   depression-only R² 0.431 (richmond 0.478, paterson 0.600); spread 3.24/2.98/2.90
   (richmond 3.24/3.02/3.08, paterson 2.06/1.94/2.44). The "coarser drawing view ⇒
   noisier boxes" rival is ruled out too — median box width in the annotation view is
   336 px vs richmond 278 / paterson 398, so annapolis sits *between* on drawing
   precision while matching richmond on spread.
2. **RETRACTED: the scale constant does NOT transfer.** k for ≥99.5% containment,
   v1-norm: paterson ×3.0, richmond ×3.5, **annapolis ×4.5** (v1-raw 3.0/3.0/3.5). Two
   cities agreeing this morning was luck. **The constant is tracking apparent ramp size**:
   median angular box width 10.38° / 11.37° / **15.66°** against k_med 1.10 / 1.26 / 1.68
   — ratio 0.106 / 0.111 / 0.107, constant to 3% while its inputs vary 1.5×. That is
   nearly structural (similar depression distributions across the three cities), and that
   is the point: **the constant is a per-city physical quantity the formula cannot see.**
   Annapolis' ramps genuinely subtend 1.51× paterson's. So one global constant mis-sizes
   by up to ~1.5× per city; the options are per-city/per-design-standard calibration or
   extent-aware sizing (RampNet#83). v1-norm remains the faithful, scale-equivariant port
   — what changed is the *cost* of "and give it a constant".
3. **Calibration cost, bootstrapped:** ~100 boxes pins a city's constant to ≈±12%, which
   resolves paterson-vs-annapolis (51% apart) but NOT paterson-vs-richmond (9% apart).
   A day of annotation per city, saturating slowly.

Azimuth, honestly restated: on annapolis it is *not* negligible (R² 0.431 → 0.526,
+0.095, vs +0.014 richmond / +0.034 paterson) — but the sign disagrees between cities
(annapolis ahead-wider 0.68–0.86, paterson beside-wider 1.05–1.43, richmond flat
0.97–1.02), so it is city-specific population structure, not a law. Conclusion unchanged
(no directional term), phrasing corrected. Extent oracle on annapolis: **R² 0.949**.

Posted on RampNet#114. **NEXT: sao_paulo** — non-US design standard (NBR 9050) makes it
the sharpest test of whether apparent ramp size is design-driven; also a second GSV city
(does the tighter fit replicate?) and the hilly one. Morgantown is only worth doing if a
resolution question resurfaces, which annapolis has largely closed.

**2026-08-18 — SÃO PAULO GOLD LANDS: the constant spread widens to 2×, GSV-vs-Mapillary
replicates, hills cost nothing, and Annapolis' "constant to 3%" identity softens to ±10%.**
119 boxed + 15 can't over 42 panos (39 × 16384×8192 boxed, one 13312×6656), box rule v2 —
RampNet `cc3617f` + `85fbaf5` on `data/sao_paulo-extent-gold`. Fourth extent-gold city,
carrying three between-city questions at once. All four cities re-scored with the same
committed harness (richmond's regenerated JSON is byte-identical to the committed one, so
nothing in the last 58 upstream commits moved these numbers).

| city | provider | pano h | n_box (det) | spread raw/norm/geo | R² dep → +az | oracle | k ≥98.5% / ≥99.5% (v1-norm) |
|---|---|---:|---:|---|---|---:|---:|
| paterson | GSV | 6656–8192 | 109 (86) | 2.06 / 1.94 / 2.44 | 0.600 → 0.634 | 0.816 | ×2.5 / ×3.0 |
| richmond | Mapillary | 2048–6144 | 299 (223) | 3.24 / 3.02 / 3.08 | 0.478 → 0.492 | 0.721 | ×3.5 / ×3.5 |
| annapolis | Mapillary | 4000 | 131 (94) | 3.24 / 2.98 / 2.90 | 0.431 → 0.526 | 0.949 | ×4.0 / ×4.5 |
| **sao_paulo** | **GSV** | **6656–8192** | **119 (75)** | **2.07 / 2.11 / 1.81** | **0.738 → 0.761** | **0.887** | **×2.0 / ×2.0** |

*(First export had one box drawn around the wrong ramp — `fto2w3ZBO7XYUyIPzctfxw det:0`,
23.4° from its own prompt, gallery item 122/281. Jon re-drew it the same evening; the
table above is the corrected gold. Both snapshots and the correction are documented in
`benchmark/sao_paulo/box_annotation_log/`.)*

1. **A non-US design standard moves the constant, downward.** São Paulo needs the
   *smallest* k of the four; the four-city spread is **2×** (×2.0–×4.5), not the 1.5×
   reported after Annapolis. Its ramps are the smallest: apparent width 10.1° at a matched
   12 m range, vs 11.1° / 12.8° / 15.7° — implied physical width 2.1 / 2.3 / 2.7 / 3.3 m.
2. **"k ÷ apparent ramp width is constant to 3%" softens to ±10%, and the mechanism is
   different from what Annapolis suggested.** São Paulo lands at 0.089 vs 0.106 / 0.111 /
   0.107. The four cities' viewing geometry is *matched* (median depression 10.4–12.0°,
   median range 11.8–13.6 m), so the difference is not distance. Apparent size still
   **orders** the constant; what it cannot do is predict it, because the ratio also
   absorbs how tightly a city's ramps obey the range law — São Paulo's log-log
   size-vs-range slope is −0.90 with r = −0.91 (the geometric ideal being −1.00) against
   Annapolis' −0.83 / r = −0.62. Calibration means running the sweep on a city's boxes,
   not measuring its ramps.
3. **Provider, not resolution, orders the y-only fit — replicated.** Two GSV cities at
   R² 0.600 / 0.738 and spread ~2.0; two Mapillary cities at 0.431 / 0.478 and spread
   ~3.0. Annapolis already excluded pano resolution; São Paulo excludes annotation zoom
   from the other side (its 440 px median drawing-view box is the *finest* of the four —
   1 px jitter = 0.23% of a box — and zoom does not order the spreads: Annapolis at 336 px
   sits between Paterson 398 and Richmond 278 while matching Richmond). The live
   hypothesis is Mapillary's per-image SfM pose/height variation corrupting the y → range
   map that every candidate rule depends on — and the tightness statistic in (2) is the
   same effect measured a second way.
4. **Hills cost nothing measurable.** The hilly city has the tightest scale-free spread
   *and* the tightest size-vs-range relation of the four, and geo-v1.5's over-sizing
   constant (k_med 0.21) matches the others (0.22 / 0.28 / 0.33). The flat-ground
   assumption survives its pre-registered stress test.
5. **Azimuth: conclusion unchanged.** +0.023 R² here (paterson +0.034, richmond +0.014,
   annapolis +0.095), and São Paulo's within-band direction (0.83–0.88 beside/ahead) sides
   with Annapolis against Paterson (1.11–1.16). Opposite signs across cities ⇒
   city-specific population structure, not a law. Extent oracle 0.887 (0.72–0.95 across
   four cities) — the same hard ceiling on any y-only rule.

**The ship recommendation is now concrete, and a global constant is affordable.** v1-norm
at a single **k = 4.5** contains **100%** of the gold in all four cities. The price of one
global constant is over-context, not broken crops: median ramp occupies **0.19–0.29** of
the crop side, against the 0.10–0.15 ML band. At that calibration v1-norm and geo-v1.5 are
near-interchangeable (geo at ×1.0: ctx p50 0.17–0.26, containment 0.996–1.000), which
again says the rules differ in one constant, not in accuracy — so **ship SW#4865's
normalization with a constant; do not swap to geometry.** Per-city calibration buys
~1.5–2× tighter framing and costs a day of annotation per city; extent-aware sizing
(RampNet#83) is the only route past the R² ≈ 0.72–0.95 oracle ceiling.

Posted on RampNet#114 ([comment](https://github.com/ProjectSidewalk/RampNet/issues/114#issuecomment-5336071816),
[correction](https://github.com/ProjectSidewalk/RampNet/issues/114#issuecomment-5336145786));
gold branch pushed.

## Claim provenance

Everything in §1 was verified 2026-08-14 by three Explore-agent surveys with file:line cites
(SidewalkWebpage crop path; RampNet YOLO/GT/extent; pano-tools cropper/study state), plus direct
reads of labeler#47, pano-tools#87, SW#4865/#46 and RampNet#113/#83/#40/#41/#44. The
10–15% context band and the R = 6.7–10 figure come from pano-tools'
`2026-08-09-cropper-consumer-requirements.md`, which is a requirements survey, not a measurement.

---

*Prepared with Claude Code (claude-fable-5).*
