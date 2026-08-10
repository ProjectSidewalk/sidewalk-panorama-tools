# Report 1 — Crop priors, and the pre-registration for the placement/sizing studies

**2026-08-09** · cropper work package (#54, #32, #48/#52 geometry) · **this document is the
binding pre-registration for Phase 3's Studies 1–3; it is committed before any Phase 2
annotation exists.** Changes after annotation begins are permitted only as dated, appended
amendments with reasons — the original text stays.

## 1 · What Phase 1 established (the priors)

Each line is a committed report with committed data and CI-pinned findings:

| Prior | Value | Source |
|---|---|---|
| Placement threshold consumers need | ≤ 0.5° in the pano frame | [consumer requirements](2026-08-09-cropper-consumer-requirements.md) |
| Margin convention consumers converge on | crop = 3–4.5× object half-extent | same |
| Stored `pano_x/pano_y` trustworthiness | click-time truth in every era; anchor on it | [era replay](2026-08-09-era-replay-study.md) |
| Per-label data-quality covariates | era ∈ {legacy, mid, post179}; bug-window flag (2023-03-29 → 2024-09-26); replay-mismatch flag | same |
| Between-user placement noise σ (per axis) | 0.30° core / 0.51° conservative (radius-dependent, no plateau) | [click noise](2026-08-09-click-noise.md) |
| Deployed sizing behaviour | 19.25% clamped at 1500 px; far clamp + edge truncation ≈ 0; 1.198× resolution inflation on the 90% of labels at 8192 px height | [clamp census](2026-08-09-clamp-census.md) |
| Label depression distribution (weights for evaluation) | p10/p50/p90/p99 = 7.8°/15.4°/27.3°/43.5° | same |
| Camera tilt prior (the #54 effect-size scale) | \|pitch\| p50/p90 = 0.63/2.60°, \|roll\| p50/p90 = 0.90/2.16° | [photometa census](data/2026-08-09-photometa-census.json) |
| Pano survival + metadata drift | 47.9% of labeled panos alive (33.2% legacy → 60.0% post-179); 0.0% of alive serve different dims than stored; depth present for 100.0% | same |

Working constraint carried from the era study: never re-derive geometry from current
`camera_heading` for old labels (it drifts); everything below anchors on stored pano pixels.

## 2 · Studies being registered

### Study 1 — placement error (#54)

**Question.** Is the stored label point biased relative to the object's canonical point, and is
any vertical bias explained by the projection's missing rig-tilt term?

**Design.** Gold annotations on a stratified label sample (§3), annotated per §4. Per label:
signed azimuth error Δaz = (stored − gold)·cos(el), signed elevation error Δel = stored − gold,
in degrees in the pano frame.

**Primary endpoints and decision rules** (α = 0.05 throughout; cluster-robust inference with
panos as clusters, since labels share panos):

1. **Mean bias per axis, overall and per depression band** (bands: <5°, 5–15°, 15–30°, >30°).
   Report point estimate + cluster-robust 95% CI. *Decision rule*: a band is "biased at consumer
   scale" if its CI excludes 0 **and** the point estimate exceeds 0.25° in magnitude (half the
   consumer threshold); "clean" if the CI lies entirely within ±0.25° (equivalence reading);
   otherwise underpowered/inconclusive — say so.
2. **Tilt regression.** Δel ~ β·T + γ_band + ε, where T = camera_pitch·cos(Δb) +
   camera_roll·sin(Δb), Δb = label bearing − camera heading (photometa values fetched fresh for
   study panos at study time, not rawLabels'), γ_band = depression-band fixed effects,
   cluster-robust by pano. The missing-tilt-term hypothesis predicts β = 1; gravity-aligned
   rendering predicts β = 0. *Decision rule*: CI ⊂ [0.7, 1.3] → tilt term missing, geometry fix
   warranted; CI ⊂ [−0.3, 0.3] → panos are gravity-aligned, no fix; anything else → partial/
   inconclusive, report the CI, no code change on this evidence alone.
3. **Within-pano contrast** (methodology-review requirement): the tilt effect is identified
   within pano where multiple study labels share a pano at different bearings — the pano fixed
   effect absorbs per-pano placement culture. Run 2 with pano fixed effects as a robustness
   column; the conclusion must survive it.

**Secondary**: quantile bias (p10/p90 of Δel per band; a mean-zero heavy tail still matters to
validator-ai's single-pixel depth read); bias vs era/window/replay-mismatch covariates
(descriptive only).

### Study 2 — crop sizing (#32)

**Question.** Which sizing rule keeps the object inside the consumer margin band?

**Candidates** (all consume only label geometry available at crop time):
- **A (deployed)**: `predict_crop_size` as shipped (pixel-linear distance, [50, 1500] clamp).
- **B (blend)**: crop side = k · angular-size(object class) at `predict_blend_distance`
  (depression), resolution-independent, k chosen per the 3–4.5× margin convention — exact
  functional form frozen in Phase 3's analysis code before evaluation, from tune split only.
- **C (fixed-angular)**: crop side = fixed angular width per label type (the null candidate any
  winner must beat).

**Endpoint.** Using gold *bounding boxes*: margin ratio = crop half-side / object half-extent,
computed per label per candidate. Primary metric: share of labels with ratio ∈ [3, 4.5]
(consumer band), reweighted to the corpus depression×type distribution (clamp-census weights).
*Decision rule*: adopt the candidate with the highest reweighted in-band share if its paired
advantage over A is significant (McNemar on in-band indicator, cluster-robust bootstrap by pano
as sensitivity); ties broken toward the simpler candidate.

**Tune/eval split by pano** (never by label): 50/50, split frozen at corpus assembly, eval
touched once per candidate.

### Study 3 — perspective vs equirectangular crops

**Question.** Does re-projecting to a rectilinear view centred on the label (undoing
equirect distortion) improve object legibility enough to justify the pipeline change?

**Design.** Paired A/B of equirect vs perspective renderings of the same labels (subset of the
Study 1 sample, ≥ 150 labels stratified by depression), blind-judged on a 3-point legibility
rubric by the clean-context annotator (§4), plus object-aspect distortion measured from gold
boxes in both projections. *Decision rule*: perspective is recommended only if it wins the
paired legibility comparison (sign test, α = 0.05) **and** does not reduce Study 2's in-band
share. Otherwise: document and stop (implementing it is not Phase 4 work either way).

## 3 · Corpus (assembled in Phase 2, spec frozen here)

From the six-city rawLabels fetch of 2026-08-09 (era study provenance):

* **Strata**: depression band (4) × era-quality stratum (post-fix / window / mid / legacy) ×
  label type (9 types; Occlusion excluded — no crop consumer). Cell target 6 labels, city-mixed;
  plus a forced oversample of **all** labels on served heights ∉ {8192} up to 60 (resolution
  sensitivity) and ≥ 30 labels with replay-mismatch flags. Target ≈ **650 labels across ≈ 550
  panos** (one pano never contributes > 3 labels).
* **Exclusions, pre-specified**: tutorial panos; `pano_y` outside [0, height]; dims-mismatch at
  acquisition time (stored vs acquired frame, the #77 preflight); labels with `disagree_count >
  agree_count` are *included* but flagged (consumer pipelines don't filter them — measure, don't
  sanitize).
* **Reweighting**: all corpus-level claims reweight strata back to the label population
  (clamp-census depression×type distribution); per-stratum claims stated as such.
* **Pixels, two sources** (the photometa census measured 47.9% pano survival, 33% for legacy —
  Google-only sourcing would gut the old strata and bias the corpus toward new imagery):
  panos alive at Google are downloaded via the production `DownloadRunner` path into an isolated
  store, over-drawing strata at ~1.7×/2.2×/3× (post-fix/mid/legacy) per the measured survival;
  panos dead at Google are pulled from the **production pano store** (the log-analyzer's SFTP
  path), which is now the only source of that imagery. Source is recorded per pano; panos absent
  from both are logged as unreachable. Store manifest (pano ids + source + hashes) committed with
  the Phase 2 commit.

## 4 · Annotation protocol (anti-anchoring, agreement-gated)

* **Rendering**: annotator sees a viewport crop centred at the stored point **plus a uniform
  random jitter of ±40–80 px per axis** (seeded, logged); the stored point, any crop box, and
  any prior annotation are **never rendered**. Zoom permitted; the tile is served from the
  isolated store, not Google.
* **Task**: per label — (a) mark the object's **canonical point** per the type rubric below;
  (b) drag a **tight bounding box**; (c) flag {object-absent, ambiguous, occluded}.
* **Rubric (canonical points)**: CurbRamp/NoCurbRamp — centre of the ramp (or would-be ramp)
  where it meets the gutter line; Obstacle — centroid of the obstruction at ground contact;
  SurfaceProblem — centroid of the defect; Crosswalk — centre of the marked area; Signal — the
  signal head centre; NoSidewalk — point on the roadway edge where sidewalk is absent. The
  rubric is binding; edge cases go to the flag, not to judgement drift.
* **Annotators**: Claude annotates the full corpus inside a **clean-context subagent** that
  receives only this protocol section and images (no study hypotheses, no stored coordinates);
  Jon annotates an independent stratified **n = 50** interleaved through the same tooling.
* **Agreement gate**: before any Study 1–3 analysis touches Claude's annotations, per-axis
  agreement on the overlap set is computed (mean |Δ| and ICC). Gate: mean |Δ| ≤ 0.5° per axis
  (the consumer threshold). Fail → rubric revised, both re-annotate the overlap, gate re-run —
  all documented as an amendment. Claude's annotations are used only after the gate passes.
* **Blindness audit**: the subagent transcript is retained; any leakage of stored coordinates
  into the annotation context voids that label's annotation.

## 5 · Power (from the measured noise floor)

Per-axis difference noise for stored-vs-gold: σ_diff ≈ √(σ_label² + σ_gold²). With the
conservative σ_label = 0.51° and gold σ_gold ≤ σ_core = 0.30° (deliberate, zoomed annotation):
σ_diff ≈ 0.59°. Two-sided α = 0.05, power 0.8:

| detectable mean bias δ | n per stratum |
|---|---|
| 0.50° (consumer threshold) | 11 |
| 0.25° (decision-rule scale) | 44 |
| 0.15° | 122 |

Depression-band cells at ≈ 160 labels/band (650/4) detect δ = 0.25° with power > 0.8 even after
a design effect of ~1.3 for pano clustering; the 6-label type×era×band cells support only the
pooled analyses, which is why per-cell claims are out of scope (§6). For the tilt regression,
with tilt-term spread sd ≈ 1.56° (photometa), n = 650 and σ_resid = 0.59°:
SE(β) ≈ 0.59/(1.56·√650) ≈ 0.015 — β = 1 and β = 0 sit ~65 SE apart; the tilt regression is power-unconstrained.

## 6 · Explicitly out of scope for Phase 3

Per-city bias league tables (not powered); any change to `camera_heading` handling; Mapillary
panos (GSV only in the study corpus); re-deriving label lat/lng (that is
label-latlng-estimation's #7); shifted-crop stratum (retired by the clamp census: exposure ≈ 0).

## 7 · Amendment log

*(empty at registration — 2026-08-09)*

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-fable-5). Registered before
Phase 2 so that when a hypothesis dies in Phase 3, it dies in public.*
