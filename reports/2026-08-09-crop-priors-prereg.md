# Report 1 — Crop priors, and the pre-registration for the placement/sizing studies

**2026-08-09**, revised 2026-08-10 after pre-merge review (§7) · cropper work package
(#54, #32, #48/#52 geometry) · **this document is the binding pre-registration for Phase 3's
Studies 1–3. Registration is its merge; it is written and reviewed before any Phase 2 annotation
exists.** Once merged, changes are permitted only as dated, appended amendments with reasons — the
original text stays. Revisions made before merge are listed in §7 and are not amendments.

## 1 · What Phase 1 established (the priors)

Each line is a committed report with committed data and CI-pinned findings:

| Prior | Value | Source |
|---|---|---|
| Placement threshold consumers need | ≤ 0.5° in the pano frame | [consumer requirements](2026-08-09-cropper-consumer-requirements.md) |
| Sizing convention consumers converge on | crop ratio R ∈ [6.7, 10], R = crop side ÷ object extent (object 10–15% of crop side) | same |
| Stored `pano_x/pano_y` trustworthiness | click-time truth in every era; anchor on it | [era replay](2026-08-09-era-replay-study.md) |
| Per-label data-quality covariates | era ∈ {legacy, mid, post179}; bug-window flag (2023-03-29 → 2024-09-26); replay-mismatch flag | same |
| Between-user placement noise σ (per axis) | 0.30° core / 0.51° conservative (radius-dependent, no plateau) | [click noise](2026-08-09-click-noise.md) |
| Deployed sizing behaviour | 19.25% clamped at 1500 px; far clamp + edge truncation ≈ 0; 1.198× resolution inflation on the 90% of labels at 8192 px height | [clamp census](2026-08-09-clamp-census.md) |
| Label depression distribution (weights for evaluation) | p10/p50/p90/p99 = 7.8°/15.4°/27.3°/43.5° | same |
| Camera tilt prior (the #54 effect-size scale) | \|pitch\| p50/p90 = 0.63/2.60°, \|roll\| p50/p90 = 0.90/2.16° | [photometa census](data/2026-08-09-photometa-census.json) |
| Pano survival + metadata drift | 47.9% of labeled panos alive (33.2% legacy → 60.0% post-179); 0.0% of alive serve different dims than stored¹; depth present for 100.0% | same |
| Backup-store coverage (the Phase 2 pixel source) | 99.2% of dead-at-Google panos are on the makelab2 store (97.8% even for legacy); store JPEG disagrees with `gsv_data`'s frame for 4.6% | [store coverage](2026-08-10-store-coverage.md) |

¹ That 0.0% compares **`gsv_data` against Google**, not our stored JPEG against either. Store panos
hold whatever Google served at scrape time, so a store image can legitimately differ from both. The
[store-coverage study](2026-08-10-store-coverage.md) measured that comparison directly and found
the store's JPEG disagrees with `gsv_data`'s frame for **4.6%** of panos (61 of 62 in the direction
`gsv_data` 16384×8192 → store 13312×6656). So #77's dims preflight is **not** retired by this row —
it has a real hit rate — and §3's dims-mismatch exclusion is what applies to the study corpus.

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
2. **Tilt regression**, registered in **two-coefficient, sign-robust** form:

   > Δel ~ β_p·(pitch·cos Δb) + β_r·(roll·sin Δb) + γ_band + ε

   γ_band = depression-band fixed effects; cluster-robust by pano.

   *Why two coefficients and not one.* The one-coefficient form Δel ~ β·T with
   T = pitch·cos Δb + roll·sin Δb hard-codes a **relative sign** between the pitch and roll terms
   that this project has not established. Working the small-angle geometry through — rig up-vector
   u_rig = u − p·f + r·s, bearing measured clockwise from the camera forward axis — gives
   Δel = pitch·cos Δb **−** roll·sin Δb, i.e. the same pitch sign and the opposite roll sign.
   Whether that or the `+` form is right depends on Google's own pitch/roll convention, which we
   cannot settle offline. Under the wrong sign the two terms partially cancel, β is attenuated
   toward 0, and the single-β decision rule below would have concluded "gravity-aligned, no fix" —
   killing a live hypothesis on an arithmetic convention. Splitting the coefficient removes the
   dependence: the null is β_p = β_r = 0 under *either* convention, and the sign of β̂_r *reports*
   the convention rather than assuming it.

   *Inputs.* `pitch`/`roll` are photometa values fetched fresh for study panos at study time (never
   rawLabels', which carries `camera_pitch` but has `camera_roll` empty in 100% of rows — see §5).
   **Δb is computed from stored pixels alone**: label bearing − camera heading collapses to
   `(pano_x / pano_width)·360 − 180`, because the pano raster is heading-centred, so the camera
   heading cancels. This is what keeps §1's "never re-derive geometry from `camera_heading`"
   constraint intact; do not reintroduce a heading term.

   *Decision rule*: both CIs ⊂ ±[0.7, 1.3] with |β̂_p| and |β̂_r| of comparable magnitude → tilt
   term missing, geometry fix warranted; both CIs ⊂ [−0.3, 0.3] → panos are gravity-aligned, no
   fix; anything else → partial/inconclusive, report both CIs, no code change on this evidence
   alone. Note that at the SEs in §5 the inconclusive region is a *substantive* zone (a genuine
   partial effect), not an underpowered one — no additional sample resolves it, so do not read
   "inconclusive" as "needs more data".
3. **Within-pano contrast** (methodology-review requirement): the tilt effect is identified
   within pano where multiple study labels share a pano at different bearings — the pano fixed
   effect absorbs per-pano placement culture. Run 2 with pano fixed effects as a robustness
   column; the conclusion must survive it.

   *Provisioning.* This column is only estimable if enough panos carry ≥ 2 study labels at
   *separated bearings* — at ≈650 labels over ≈550 panos the corpus would otherwise average 1.18
   labels/pano and the required robustness check could silently be unrunnable. §3 therefore forces
   the stratum. If the assembled corpus still delivers fewer than **60 panos** contributing ≥ 2
   labels with pairwise |ΔΔb| ≥ 60°, this column is reported as *not estimable* (with the achieved
   count) rather than reported underpowered, and endpoint 2's conclusion stands on the pooled fit
   with the limitation stated.

**Secondary**: quantile bias (p10/p90 of Δel per band; a mean-zero heavy tail still matters to
validator-ai's single-pixel depth read); bias vs era/window/replay-mismatch covariates
(descriptive only).

### Study 2 — crop sizing (#32)

**Question.** Which sizing rule keeps the object inside the consumer margin band?

**Candidates** (all consume only label geometry available at crop time):
- **A (deployed)**: `predict_crop_size` as shipped (pixel-linear distance, [50, 1500] clamp).
- **B (blend)**: crop side = R · angular-size(object class) at `predict_blend_distance`
  (depression), resolution-independent, R chosen inside the [6.7, 10] convention — exact
  functional form frozen in Phase 3's analysis code before evaluation, from tune split only.
- **C (fixed-angular)**: crop side = fixed angular width per label type (the null candidate any
  winner must beat).

**Endpoint.** Using gold *bounding boxes*: **crop ratio R = crop side / object extent** (identically
crop half-side / object half-extent — a ratio of like quantities, so half-vs-full cannot shift it),
computed per label per candidate, with object extent taken as the box's larger side. Primary metric:
share of labels with **R ∈ [6.7, 10]** (the consumer band — see the correction note in the
[consumer requirements](2026-08-09-cropper-consumer-requirements.md) §(ii)), reweighted to the corpus
depression×type distribution (clamp-census weights).
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
  label type (**8** — the corpus carries 9, Occlusion excluded as it has no crop consumer). Cell
  target 6 labels, city-mixed; plus a forced oversample of **all** labels on served heights ∉ {8192}
  up to 60 (resolution sensitivity) and ≥ 30 labels with replay-mismatch flags. Target ≈ **650
  labels across ≈ 550 panos** (one pano never contributes > 3 labels).
* **Forced multi-label-pano stratum** (provisions §2.3, which is otherwise not estimable): **≥ 80
  panos** must contribute exactly 2–3 study labels with pairwise bearing separation |ΔΔb| ≥ 60°,
  drawn preferentially from panos alive at Google (only those carry the photometa pitch/roll that
  endpoint 2 consumes). These panos count toward the 650, not on top of it.
* **Exclusions, pre-specified**: tutorial panos; `pano_y` outside [0, height]; **dims mismatch
  between the acquired image and the label's recorded frame** — compared against the *store's own
  JPEG*, not against `gsv_data`, and measured at **≈ 4.6%** of panos by the
  [store-coverage study](2026-08-10-store-coverage.md), so this is a material filter and not a
  formality (this is the #77 preflight; the photometa census's 0.0% figure compared a different
  pair — see §1 note ¹); **`pano_y` replay mismatch**, which is the separate and sharper check that
  the recorded frame is the *click-time* frame rather than a later refresh (the store-vs-recorded
  test above cannot see that case) — exact for 99.43% of the corpus, so this drops ~0.6%,
  concentrated in the bug window; labels with `disagree_count > agree_count` are *included* but
  flagged (consumer pipelines don't filter them — measure, don't sanitize).
* **Reweighting**: all corpus-level claims reweight strata back to the label population
  (clamp-census depression×type distribution); per-stratum claims stated as such.
* **Pixels: one source, the lab backup store** —
  `/m-makeabilitylab/makeabilitylab/sidewalk_panos/Panoramas` on makelab2, keyed
  `<city>/<pano_id[:2]>/<pano_id>.jpg`. The [store-coverage study](2026-08-10-store-coverage.md)
  measured it at **99.2% of the panos Google has dropped** and 99.6% of the census sample overall,
  holding at **97.8% even in the legacy era** where Google survival is 33.2%. Every stratum takes
  the same path, live or dead: no download, no per-era sourcing split.
  * **Over-draw: a flat ~5% allowance**, not the era-graded ~1.7×/2.2×/3× this spec previously
    carried. Those factors priced *Google* survival, which is no longer the binding constraint;
    against a ~99% source the era grading disappears (97.8% → 100.0% across eras).
  * Google is used only where the store misses. Source is recorded per pano; panos absent from
    both are logged **unreachable** (six such in the census sample, ids committed). Store manifest
    (pano ids + source + hashes) committed with the Phase 2 commit.
  * **This changes imagery only.** Endpoint 2 still needs photometa `pitch`/`roll`, which exists
    only for panos alive at Google, so its n and its survival selection are exactly as stated in
    §5 — unchanged by store coverage.

## 4 · Annotation protocol (anti-anchoring, agreement-gated)

* **Rendering**: annotator sees a viewport crop centred at the stored point **plus a uniform
  random jitter of ±40–80 px per axis** (seeded, logged); the stored point, any crop box, and
  any prior annotation are **never rendered**. Zoom permitted; the tile is served from the
  Phase 2 corpus store (§3), never fetched live from Google — so an annotation can never be made
  against different pixels than the analysis reads.
* **Task**: per label — (a) mark the object's **canonical point** per the type rubric below;
  (b) drag a **tight bounding box**; (c) flag {object-absent, ambiguous, occluded}.
* **Rubric (canonical points)**: CurbRamp/NoCurbRamp — centre of the ramp (or would-be ramp)
  where it meets the gutter line; Obstacle — centroid of the obstruction at ground contact;
  SurfaceProblem — centroid of the defect; Crosswalk — centre of the marked area; Signal — the
  signal head centre; NoSidewalk — point on the roadway edge where sidewalk is absent. The
  rubric is binding; edge cases go to the flag, not to judgement drift.
* **Annotators**: Claude annotates the full corpus inside a **clean-context subagent** that
  receives only this protocol section and images (no study hypotheses, no stored coordinates);
  Jon annotates an independent stratified **n = 50** interleaved through the same tooling. Jon is
  *not* blind to the hypotheses — he wrote them. His 50 are therefore a rubric-agreement instrument
  only; they are never used as gold for any reported estimate, and no endpoint is computed on the
  overlap set alone.
* **Agreement gate**: before any Study 1–3 analysis touches Claude's annotations, per-axis
  agreement on the overlap set is computed (mean |Δ| and ICC). Gate: **mean |Δ| ≤ 0.34° per axis**.
  Fail → rubric revised, both re-annotate the overlap, gate re-run — all documented as an amendment.
  Claude's annotations are used only after the gate passes.

  *Where 0.34° comes from, and why not 0.5°.* The gate exists to protect §5's assumption
  σ_gold ≤ 0.30°, so it has to be set in those units. For two annotators each at σ_gold, the mean
  absolute difference is E|Δ| = √(2/π)·√2·σ_gold = 1.128·σ_gold, so a gate at the 0.5° *consumer*
  threshold would admit σ_gold up to 0.44° — half again the value the power table is computed at.
  0.34° = 1.128 × 0.30° makes the gate enforce the assumption it is named for. (§5 also states the
  cost of failing over to 0.44°, so a marginal miss is a known quantity rather than a surprise.)
* **Intra-annotator repeat**: 25 labels drawn from Claude's corpus are re-annotated in a later
  batch by a fresh clean-context subagent, unaware they are repeats. Reported as test–retest
  |Δ| alongside the inter-annotator gate; this is a *reported* diagnostic, not a gate, since a
  single-annotator repeat cannot detect a shared rubric misreading.
* **Blindness audit**: the subagent transcript is retained; any leakage of stored coordinates
  into the annotation context voids that label's annotation.

## 5 · Power (from the measured noise floor)

Per-axis difference noise for stored-vs-gold: σ_diff = √(σ_label² + σ_gold²), with the conservative
σ_label = 0.51° (click-noise study, radius 1.5°). Two-sided α = 0.05, power 0.8, n per stratum:

| detectable mean bias δ | σ_gold = 0.30° (what §4's gate enforces; σ_diff = 0.592°) | σ_gold = 0.44° (what a 0.5° gate would have admitted; σ_diff = 0.674°) |
|---|---|---|
| 0.50° (consumer threshold) | 11 | 15 |
| 0.25° (decision-rule scale) | 44 | 57 |
| 0.15° | 123 | 159 |

The second column is the cost of the gate slipping, priced in advance: even there the
depression-band cells at ≈ 160 labels/band (650/4) detect δ = 0.25° with power > 0.8 after a design
effect of ~1.3 for pano clustering. The 6-label type×era×band cells support only the pooled
analyses, which is why per-cell claims are out of scope (§6).

**Tilt regression (endpoint 2), at its achievable n — not 650.** `camera_roll` is empty in **100% of
rawLabels rows** (436,348/436,348 across all six cities), so pitch/roll can only come from photometa,
and photometa only answers for panos still served by Google — **47.9%** of labeled panos, era-graded
33.2% (legacy) → 60.0% (post-179). Labels on dead panos are in the corpus by design (§3 sources their
pixels from the production store) but contribute nothing to this endpoint. So:

* **Effective n ≈ 310** as a floor (650 × 0.479), higher in practice because §3's forced
  multi-label-pano stratum draws preferentially from alive panos.
* This subsample is **selected on pano survival**, which correlates with era — endpoint 2's estimate
  is therefore conditioned on a corpus tilted toward post-179. Report the realised era mix beside
  the coefficients; do not reweight it back to the label population (reweighting a
  survival-selected subsample would import the assumption that tilt behaves identically on dead
  panos, which is untestable here).
* Component spreads from the photometa census: sd(pitch·cos Δb) = 1.20°, sd(roll·sin Δb) = 1.00°
  (combined tilt-term sd 1.56°). With σ_resid = 0.59° and n = 310:
  **SE(β_p) ≈ 0.028, SE(β_r) ≈ 0.034** — β = 1 and β = 0 sit 30–36 SE apart. At n = 650 it would be
  43–52 SE. Either way the regression is power-unconstrained; the loss from survival is immaterial to
  the decision, and it is stated here so the estimand is not mistaken for the full corpus.

## 6 · Explicitly out of scope for Phase 3

Per-city bias league tables (not powered); any change to `camera_heading` handling; Mapillary
panos (GSV only in the study corpus); re-deriving label lat/lng (that is
label-latlng-estimation's #7); shifted-crop stratum (retired by the clamp census: exposure is
exactly zero across 436,348 labels, and analytically the wanted crop cannot reach the bottom edge
below 53° of depression against a corpus p99 of 43.5°).

## 7 · Amendment log

*Empty. Registration is the merge of this document, so nothing above is an amendment yet.*

**Pre-registration revisions (before registration, so not amendments).**

* **2026-08-10 — pre-merge review** ([review report](2026-08-10-crop-priors-review.md)). Six changes,
  all made while the document was still unregistered and no Phase 2 annotation existed:
  1. §1, §2 Study 2 — the sizing convention was carried over from the consumer survey as
     "3–4.5× object half-extent" and registered as Study 2's acceptance band. It was
     margin-over-*full*-extent mislabelled, then re-read under a third definition; the correct band
     under the endpoint's own definition is **R ∈ [6.7, 10]**. As registered, the validator — the
     consumer the convention was derived from, at R = 7.9 — scored outside its own band.
  2. §2.2 — the tilt regression was single-coefficient on T = pitch·cos Δb + roll·sin Δb, which
     hard-codes an unverified relative sign; under the opposite convention β attenuates to 0 and the
     decision rule would have concluded "gravity-aligned, no fix". Now two-coefficient and
     sign-robust, with Δb's collapse to stored pixels made explicit.
  3. §2.3, §3 — the within-pano robustness column was required but not provisioned by the corpus
     spec; added a forced multi-label-pano stratum and a not-estimable fallback.
  4. §4, §5 — the agreement gate (0.5°) was looser than the σ_gold ≤ 0.30° the power table assumes;
     tightened to 0.34° and the fallback priced in the table. Intra-annotator repeat added.
  5. §5 — endpoint 2's n is ~310, not 650: `camera_roll` is absent from rawLabels entirely and
     photometa only answers for the 47.9% of panos still alive, which selects on era.
  6. §1, §6 — dims-drift and shifted-crop footnotes corrected against what was actually measured.
* **2026-08-10 — §3 corpus sourcing, after the
  [store-coverage study](2026-08-10-store-coverage.md).** Prompted by Mikey confirming the EC2
  backup panos are synced to makelab2, which made the store measurable for the first time. §3 had
  sized its sourcing plan against Google survival because that was the constraint Phase 1 had
  measured; the store turns out to hold **99.2%** of the dead panos (97.8% for legacy), so it is
  now the single pixel source for every stratum, the era-graded ~1.7×/2.2×/3× over-draw is replaced
  by a flat ~5% allowance, and the `PS_SFTP_*` credential dependency is withdrawn. The same probe
  measured the store's JPEG against `gsv_data`'s frame at **4.6% disagreement**, which turns §3's
  dims-mismatch exclusion from a formality into a material filter and corrects the reading of §1's
  0.0%-dims-drift row. Endpoint 2 is untouched: photometa pitch/roll still exists only for panos
  alive at Google.

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-fable-5); revised 2026-08-10
after review (claude-opus-5[1m]). Registered before Phase 2 so that when a hypothesis dies in
Phase 3, it dies in public.*
