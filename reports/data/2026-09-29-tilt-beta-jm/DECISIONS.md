# Beta batch (#191): decision log (2026-09-29)

Written before any verdict on this batch existed and before its key was opened by anyone who judges it.
The rules below are implemented as `tilt_adjudicate.score_beta` (committed with this file), and the key's
salted hash (`key.sha256`) was committed with it. Each entry records what changed and what was known at
the time.

## 1. Design (2026-09-29)

**What was known.** Endpoint C (`../2026-09-29-tilt-adjudication-jm/`, `../2026-09-30-tilt-adjudication-jm-b2/`)
is unblinded: the leak window beat its mirror 79 : 0. post179 is confirmed at 98% of single-window answers,
legacy+mid at 84%, and the stored window won 7 of legacy+mid's 48 sheets. The judge (Jon) knows these results.
C cannot give the size of the shift (beta), because every shifted window moved by the full T.

**The batch.**

- **Windows at 0.5 T, 1.0 T and 1.5 T**, all in the predicted (leak) direction: centred at
  `pano_y - k * T(b) * h / 180` for k = 0.5, 1.0, 1.5 (`tilt_adjudicate.BETA_OFFSETS`, key names `b050`,
  `b100`, `b150`). The rest is as in C: the production v2 window, one width for all three (computed at the
  stored y), a ring at each window's centre, a random A/B/C order per sheet, and the same choices (A, B, C,
  the three ties, `none`) on the same page.
- **Pool and exclusions.** The same vouched-for pool and pose scan, with every pano of both C batches
  excluded (96 panos).
- **Draw.** `tilt_jm_pool.py draw --design beta --seed beta20260929 --min-abs-t 5 --cell-cap 2`: 6 per type
  per era arm (48 sheets), one label per pano, at most 2 per city per (arm, type) and then a fill pass, in
  md5(seed:label_uid) order (`draw.json`).
- **Why |T| >= 5 deg.** Adjacent windows are 0.5 T apart, half C's spacing. At |T| >= 4 deg that is a median
  11-13% of the window height, which would have made most sheets ties. At 6 deg, post179 has only 3-9 panos
  left per type. At 5 deg it has 12-24, and adjacent rings are at least 2.5 deg apart.
- **What the draw gave.** 48 labels over 14 cities. Seattle has 15 of them. Every legacy+mid label is
  XML-posed (2019-22 stitches) and every post179 label is npz-posed, so in this batch era and pose source
  cannot be separated.

## 2. Two limits of the design, stated before judging

1. **The 1.0 T window is always in the middle.** The feature's height in the three panels is monotone in k,
   so a judge can see which panel is the middle one, just as the stored window was always the middle one in C.
   A judge who knows beta = 1 is predicted, and who is drawn to the middle, pushes answers toward 1.0.
   The **0.5 T vs 1.5 T** contrast is blind even to that judge: telling the two extremes apart needs the sign
   of T, which nothing on the sheet shows. It is therefore the primary contrast. The share of the middle window,
   and the mean score, inherit the caveat.
2. **The grid is bounded.** No window sits at the stored y (k = 0) or beyond 1.5 T. A sheet whose true shift
   is near 0, from a small beta or a bad pose, can only read as 0.5 or `none`. The score is bounded to
   [0.5, 1.5] and is pulled toward the grid's centre.

## 3. Scoring rules, fixed now, blind

Per era arm (legacy+mid, post179) and pooled. `none` and ties are never dropped from n.

1. **Per-sheet score.** A single window scores its k. A tie scores the mean of its two k: `b050=b100` = 0.75,
   `b100=b150` = 1.25, and `b050=b150` = 1.0. `none` has no score and is reported as its own bucket.
2. **Primary: 0.5 T against 1.5 T.** This is a two-sided exact sign test over every sheet whose choice holds
   exactly one of `b050` and `b150` (`score`'s `pairwise`). `b050` and `b050=b100` count low, `b150` and
   `b100=b150` count high, and `b100`, `b050=b150` and `none` count for neither. It is run per arm, with a
   Holm adjustment over the two arms, at alpha = 0.05. Under beta = 1 the two extremes are equally far from
   the feature, so the null is beta = 1, which is F1's geometric value.
3. **Reading, per arm.** If the Holm-adjusted p >= 0.05, beta is **consistent with 1**. Otherwise beta is
   **below 1** (low > high) or **above 1** (high > low).
4. **Estimate.** The mean per-sheet score, with a 95% percentile bootstrap CI over sheets (10,000 resamples,
   seed 20260929). The distribution of scores (0.5 / 0.75 / 1.0 / 1.25 / 1.5) is reported beside it. Because
   of section 2, the estimate is descriptive. Rule 3's reading is the result.
5. **What each reading means for the CropRunner correction (#191 step 2).** If an arm reads consistent with 1,
   its correction defaults to beta = 1, and the CI is quoted as the uncertainty. If an arm reads below or above
   1, this batch does not set the default. A follow-up batch on a grid centred on that arm's estimate, 0.25 T
   apart, sizes it first. Either way, the correction lands with beta as a parameter, off by default.
6. **legacy+mid's 7 stored wins in C.** Two pre-set arm comparisons, each a two-sided Fisher exact test:
   (a) the low/high split, legacy+mid against post179; and (b) the `none` share, legacy+mid against post179.
   If (a) does not differ and legacy+mid has more `none` answers, that reads as per-pano pose error (a wrong
   pose leaves no ring near the feature) rather than a smaller beta. If (a) differs, with legacy+mid lower,
   that reads as a smaller beta in legacy+mid. Because of section 1, "legacy+mid" here means XML-posed 2019-22
   stitches, and "pose error" means XML pose error. Any other combination of results is reported as it stands.
7. **Notes.** The judge may attach a note to any sheet. Notes are committed with the verdicts. Reading a note
   as a different answer (for example, a `none` whose note names a tie) is post hoc. It may be reported as a
   sensitivity, and never replaces the scored result.
