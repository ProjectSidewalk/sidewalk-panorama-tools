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

## 4. Unblinding (2026-09-29)

**What was known.** Jon judged all 48 sheets. His verdicts were committed as a separate commit after the
rules above: 17 C, 11 B, 8 A, 3 `A=C`, 9 `none`, with 14 notes. Nobody who judged had opened the key.

**Then** the key was opened and `score_beta` run with the rules unchanged:

| | n | `none` | scored | low : high | two-sided p (Holm) | reading | mean score [95% CI] |
|---|---|---|---|---|---|---|---|
| post179 | 24 | 1 | 23 | 4 : 1 | 0.375 (0.75) | consistent with 1 | 0.95 [0.86, 1.03] |
| legacy+mid | 24 | 8 | 16 | 4 : 1 | 0.375 (0.75) | consistent with 1 | 0.91 [0.80, 1.00] |
| pooled | 48 | 9 | 39 | 8 : 2 | 0.109 | - | 0.93 [0.86, 0.99] |

- Score counts: post179 0.5 ×3, 0.75 ×1, 1.0 ×18, 1.5 ×1. legacy+mid 0.5 ×3, 0.75 ×1, 1.0 ×11, 1.25 ×1.
  The 1.0 T window takes 29 of the 39 scored sheets. Section 2.1's caveat applies to that share.
- **Rule 5:** both arms read consistent with 1, so the CropRunner correction defaults to **beta = 1** in both
  arms, and the CIs above are its quoted uncertainty. The pooled CI's upper end is just under 1, but by rule 4
  the mean is descriptive, and the pooled sign test (8 : 2, p = 0.11) is not significant.
  *Appended 2026-10-05: the default is **not** chosen. It is pending Jon's choice
  ([#197](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/197)), and this batch does not set it.
  See section 5.*
- **Rule 6:** the low/high split does not differ between arms (Fisher p = 1.0). legacy+mid has more `none`
  (8 of 24 against 1 of 24, Fisher p = 0.023). By the pre-set reading, legacy+mid's 7 stored wins in C are
  per-pano (XML) pose error rather than a smaller beta.
- **Post hoc, not part of the result:** six of legacy+mid's eight `none` sheets have |T| >= 7.7 deg, where the
  nearest window is furthest from the stored y. Nine notes name a second ring as nearly as good. Neither
  observation changes a scored answer.

## 5. Post hoc: corroboration, not a measurement (2026-10-05)

**What was known.** The PR's review ([#194 review](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/194#issuecomment-5969779431))
had found that the primary test could not have rejected at the n it got, and that section 4 and the PR title
presented that as a result. The RampNet per-pose-record slopes were posted on
[#191 on 2026-10-01](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/191#issuecomment-5922041299):
the same 3,518 pairs give beta about 0.88 under the XML pose and about 0.95 under the npz pose. Jon decided the
framing below. Sections 1-3 are left exactly as written; this entry is post hoc, and nothing in it is a
pre-set rule.

**The framing: corroboration.** This batch reports a direction and the middle window's share. It does not
measure beta, and its "consistent with 1" readings are not evidence that beta = 1.

- **The primary test had no power at this n.** Each arm has n = 5 discordant sheets (the low + high
  sheets of rule 2). With n = 5 the smallest two-sided exact p is 2/32 = 0.0625, and the smallest Holm-adjusted
  p over the two arms is 0.125. So neither arm could reject, whatever the split: a 5 : 0 split would also have
  read "consistent with 1". A per-arm rejection needed at least 7 discordant sheets all one way on its own
  (7 : 0 gives a Holm-adjusted 0.031). Holm steps down, so the bar is lower for the second arm: 6 : 0 (p = 0.031)
  rejects if the other arm rejected first, at p < 0.025. At n = 5 neither applies: even unadjusted, 0.0625
  exceeds 0.05. Rule 3's reading is what the pre-set rule says, and it carries no evidence. These
  numbers are `post_hoc_power` in `sealed/score_beta_jon.json`, and a test pins them.
- **What the batch does show.** Both arms lean low, 4 : 1 each (pooled 8 : 2, p = 0.109, not significant).
  The arm means are 0.95 [0.86, 1.03] for post179 and 0.91 [0.80, 1.00] for legacy+mid. Grouped by pose record
  (see the erratum below) they are 0.935 npz-posed (23 scored sheets) and 0.922 XML-posed (16). So the batch
  corroborates beta < 1 overall, but it does not resolve the per-pose-record split: the 0.013 gap between the
  two pose records is much smaller than the RampNet slopes' 0.95 against 0.88, and both slopes sit inside both
  arms' CIs. Both of section 2's biases (the middle-window pull and the bounded grid) push the means toward 1.
  The 1.0 T window took 29 of the 39 scored sheets, under section 2.1's caveat.
- **Rule 5's consequence is withdrawn.** Section 4 says the correction "defaults to beta = 1 in both arms".
  It does not: the default is pending Jon's choice, tracked in
  [#197](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/197). This batch is corroboration
  for that choice, not its source.
- **Rule 6's reading conflicts with the 2026-10-01 result.** The low/high Fisher test (4 : 1 against 4 : 1,
  p = 1.0) has no power to see an arm difference, so "the split does not differ" is not evidence that it is the
  same. The pre-set reading, that legacy+mid's 7 stored wins in C are XML pose error "rather than a smaller
  beta", is contradicted by the pose-record result: the same pairs read about 0.88 under the XML pose and
  about 0.95 under the npz pose, so the slope a correction should use depends on the pose record it corrects
  with. The `none` difference (p = 0.023) stands as a count. The reading is kept above as recorded and is not
  endorsed here.

**Provenance: what git attests, and what rests on Jon's word.** The preamble says this file was "written before
any verdict on this batch existed", and the verdict commit says "committed before the key is opened".

- Git attests **commit order, not commit time**. ffc7356 (design, rules, key hash) and 96362be (verdicts) have
  identical author and committer times, 2026-09-29 15:07:59 -0700, and d35fa44 (unblinding) follows at
  15:08:36. All three were made after judging ended. The salted hash was committed with the verdicts, so it
  does not bind the key to a time before judging either.
- The review read file mtimes in Jon's checkout: `score_beta` 14:34, the draw 14:35, the key and sheets 14:37,
  the last verdict write 15:01. Those support `score_beta` predating the sheets. Nothing in the repository dates
  sections 1-3 before the first verdict.
- So "the rules were fixed before judging" rests on Jon's word, not on the repository.
- **Recommendation for future batches:** commit and push DECISIONS.md, the scoring code and `key.sha256` before
  the first verdict (or post the hash on the issue), so that a pushed timestamp attests the order.

**The tooling, fixed with this entry.** Section 4's table was transcribed by hand: there was no command for it,
and `tilt_adjudicate.py score` died with `KeyError: 'b100'` on this folder. Now
`tilt_adjudicate.py score-beta --out <this folder> --judge jon` writes `sealed/score_beta_jon.json` (committed),
`score` refuses this folder and names `score-beta`, and a test asserts that every number in section 4 is that
file's. The README's step 5 named C's analysis (`tilt_error_study.py analyze`), and `sealed/README.md` was C's
text (the endpoint-C key, a machine pass, a key "committed early by mistake"), none of which applies here. Both
READMEs were rewritten for this design. The key, the salt and `key.sha256` are untouched.

**Erratum to section 1** (found by the [round-2 review](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/194#issuecomment-5998184562);
section 1 is left as written): "every legacy+mid label is XML-posed and every post179 label is npz-posed" is
false for two sheets. The key's `pose_source` gives post179 23 npz + 1 XML and legacy+mid 23 XML + 1 npz:

- `t6f72c3c9f9` (seattle-wa:290009) is post179 but XML-posed. Jon chose A, the 1.0 T window: score 1.0.
- `t7876d5c101` (la-piedad-old:1779) is legacy+mid (era `mid`, `scrape_era` modern) but npz-posed. Jon chose
  `A=C`, the 0.5 T and 1.0 T windows: score 0.75.

So era and pose source are separable here, barely, and rule 6's gloss ("legacy+mid here means XML-posed") holds
for 23 of 24 sheets, not all. The 10-01 slopes are defined per pose record, so the means grouped that way are
the comparison that matches them: npz 0.935 (24 sheets, 23 scored), XML 0.922 (24 sheets, 16 scored). Both
are `post_hoc_by_pose_record` in `sealed/score_beta_jon.json`, with the two sheets listed, and a test pins
them. No scored result in section 4 changes: the rules group by era arm, and so does the table.

**Errata to section 4's post-hoc bullet** (neither changes a scored result):

- "Six of legacy+mid's eight `none` sheets have |T| >= 7.7 deg": **five** do. One (`t87251dd38a`) has
  |T| = 7.680; six meet 7.68.
- "Nine notes name a second ring as nearly as good": **ten** single-answer sheets have such a note
  (`t4d8e4e6eac`, `t95151aad0f`, `ta9749d15f2`, `taa103d7bc2`, `tab1502c942`, `tc489134b83`, `tcdf1f35b0c`,
  `tdf734c39b0`, `tfb81323001`, `tfbc348b385`), and eleven with the `A=C` tie's note.
