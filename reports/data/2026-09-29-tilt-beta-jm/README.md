# Adjudication sheets - read this, and nothing else in this folder, first

1. From the repository root: `python reports/scripts/tilt_adjudicate.py next --out reports/data/2026-09-29-tilt-beta-jm --judge <you>`
   (`<you>` is lowercase letters, digits, `-` or `_`; the decision-bearing judge is `jon`)
2. Open the sheet it prints. Which yellow ring sits on the labelled feature? Answer A, B, C or none.
3. `python reports/scripts/tilt_adjudicate.py record --out reports/data/2026-09-29-tilt-beta-jm --judge <you> <token> A|B|C|none`
4. Repeat until `next` prints "all judged". A second `record` for a token replaces the first.
5. Commit `verdicts_<you>.jsonl` (it stays in this folder), then score it from the repository root:
   `python reports/scripts/tilt_adjudicate.py score-beta --out reports/data/2026-09-29-tilt-beta-jm --judge <you>`, which writes
   `sealed/score_beta_<you>.json`. Only then open `sealed/`, DECISIONS.md, the report, or the study JSON.

Do not open `sealed/`, the report's results, `reports/data/2026-09-26-tilt-error-study.json`,
`reports/README.md`, `docs/cropper.md` or `.claude/rules/tilt.md` before step 5: they hold the key
or another judge's answers. `next` and `record` never read the key, and they
refuse to run while a key file sits in this folder outside `sealed/`.

This folder holds one judge's completed verdicts: `jon`, all 48 sheets, judged 2026-09-29 and scored in
`sealed/score_beta_jon.json`. Adding a second judge needs a new dated entry in `DECISIONS.md` first, written
by whoever sets that judge up (not by the judge), saying how the new verdicts are scored and read beside these.
