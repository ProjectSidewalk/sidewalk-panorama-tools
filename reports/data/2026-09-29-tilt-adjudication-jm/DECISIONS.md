# Endpoint C redraw: decision log (2026-09-29)

Written before the key of this folder, or of batch 2, was opened by anyone: the judge's answers were tallied
from `verdicts_jon.jsonl` and `tasks.json` only. Each entry records what changed and what was known at the time.

## 1. Redraw from a vouched-for pool (2026-09-29)

**What was known.** The first draw (`../2026-09-26-tilt-adjudication/`) had no validation filter. Of its 2,994
eligible labels, 403 were voted incorrect by the crowd, 405 were never validated, and 96 carried a Disagree
from a lead labeller. Seven sheets into judging it, the judge (Jon) reported labels he would have voted down.

**Change.** C is redrawn from `tilt_jm_pool.py`'s pool: labels made by `jonfroehlich` or `mikey`, or
validated Agree by either, with no Disagree from either. That is 70,422 labels on 43,563 panos across the GSV
deployments (`../2026-09-29-tilt-jm-pool.csv.gz`, built from the 2026-09-12 rawLabels sweep). They were posed by
a targeted store scan (`../2026-09-29-tilt-pose-jm.csv.gz`, 45,687 panos, makelab2). Other changes:

- 6 per type per era arm, one label per pano, at most 4 per city per arm, seed `20260929`, |T| >= 4 deg
  (`draw.json`).
- The 7 verdicts Jon recorded on the first draw are kept as
  `../2026-09-26-tilt-adjudication/pilot-2026-09-29-superseded_jon.jsonl`. They are a pilot and are never
  pooled with this set.
- The judging page names the feature, and asks for the ring **closest to where the judge would have
  marked it** (the best of three), rather than one that sits exactly on it. Five of this batch's 48 verdicts
  were recorded under the older "sits on" wording.

## 2. The pre-set rule cannot be met; replaced before unblinding (2026-09-29)

**What was known.** Jon's 48 verdicts on this batch: 36 decisive (A 9, B 17, C 10) and 12 `none`. By his account,
most of the `none` answers were two rings equally close. The plan's rule needed one window to reach at least
90% of *all* n, with `none` in the denominator. With 12 of 48 `none`, no window can exceed 75%, so the rule
reads "split" whatever the key says. More sheets of the same design would not change that, because ties are a
property of the design: the tilt shift is small beside a large or linear referent.

**Changes, fixed now, blind:**

1. **Ties are answerable.** `A=B`, `A=C` and `B=C` join the choices (`tilt_adjudicate.TIE_CHOICES`).
   `none` now means that all three rings are clearly off. The judge may re-answer this batch's 12 `none`
   sheets as ties while still blind.
2. **Primary contrast: leak against antileak**, a one-sided exact sign test over every sheet whose choice
   holds exactly one of the two (`score`'s `pairwise`). A `stored=leak` tie counts for leak, a
   `stored=antileak` tie counts for antileak, and a `leak=antileak` tie counts for neither. This is the
   contrast #158 already named as the one Jon's verdict is read on, since only it is blind to a judge who
   knows the hypothesis.
3. **Secondary: a window is "confirmed"** when it takes at least 90% of the single-window answers. Ties and
   `none` are reported as their own buckets beside it and never dropped. The plan's all-n share is still
   reported, labelled as the original rule.
4. **More data at a larger shift: batch 2.** 48 more sheets at |T| >= 6 deg, where the pool still fills 6 per
   type in both arms (post179 has 9-15 panos per type at 6 deg, and 4-6 at 7 deg). The draw uses seed `20260930`
   and excludes batch 1's panos (`../2026-09-30-tilt-adjudication-jm-b2/`). Results are reported per batch and
   pooled.
5. **Comments.** The judge may attach a free-text note to any sheet (`record --comment`). Notes are kept with
   the verdict and committed.

## 3. Unblinding (2026-09-29)

**What was known.** Jon finished both batches: he went back over batch 1 and gave a lean wherever one ring
was even slightly more plausible, then judged all of batch 2. His verdicts and notes were committed as
`2a814f1` before either key was opened. He recorded no ties. 13 sheets carry a note. Five of his `none`
answers name two rings as equally plausible in their note ("Both B and C clearly mark the trash can"), which
are ties recorded as `none`. Several of his picks note a weak lean ("Slight lean towards C").

**Then:** the two keys were opened and the batches scored by rules 2.2 and 2.3 above, unchanged, so those
five sheets count as `none`. Reading them as the ties their notes name is **post hoc**: it was decided after
the notes were read and after unblinding. It is reported only as a sensitivity (`NOTE_NAMED_TIES` in
`tilt_error_study.py`) and never replaces the scored result. The result, and every number quoted from it, is
in the study artifact (`c_redraw`) and the report's C section. The first draw's machine-judge verdicts stay
in its `sealed/` folder, reported as a superseded first draw.
