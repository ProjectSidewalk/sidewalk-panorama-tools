# Adjudication sheets - read this, and nothing else in this folder, first

1. From the repository root: `python reports/scripts/tilt_adjudicate.py next --out reports/data/2026-09-26-tilt-adjudication --judge <you>`
2. Open the sheet it prints. Which yellow ring sits on the labelled feature? Answer A, B, C or none.
3. `python reports/scripts/tilt_adjudicate.py record --out reports/data/2026-09-26-tilt-adjudication --judge <you> <token> A|B|C|none`
4. Repeat until `next` prints "all judged". A second `record` for a token replaces the first.
5. Commit `verdicts_<you>.jsonl`; only then open `sealed/`, the report, or the study JSON.

Do not open `sealed/`, the report's results, or `reports/data/2026-09-26-tilt-error-study.json` before
step 5: they hold the key or another judge's answers. `next` and `record` never read the key, and they
refuse to run while a key file sits in this folder outside `sealed/`.
