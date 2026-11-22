# Pilot record for #186

What the planner (Fable 5.2) and the plan reviewer (Opus 5.5) ran on 2026-11-22 while the plan was being written
and reviewed, before the criteria in `reports/plans/2026-11-22-issue-186-crop-sizing-consolidation-plan.md` were
committed. It is kept because those criteria disclose what it showed, and a disclosure is only checkable if the
run is.

The scripts were scratch files with absolute local paths; they are committed rewritten to take the RampNet
checkout as an argument (`--rampnet <RampNet>`) and to import this repo's modules relative to their own location.
The computation is unchanged. All read only.

| Script | Who | What it shows | Output |
|---|---|---|---|
| `probe_tilt.py` | planner | The modelled tilt shift on the GSV gold at beta 0.95, and the share of windows that move by more than 10% between g.y and r.y | `probe_tilt.out.txt`, the planner's run; the rewritten script reproduces it byte for byte |
| `probe_tagger.py` | planner | Tagger mAP by depression band from rounded per-label probabilities, with an AP that ignores ties (PR-B's input; its full-test mAPs miss the committed ones) | `probe_tagger.out.txt`, the planner's run; reproduced byte for byte |
| `probe_ap_repro.py` | planner | Full-test mAP from logits (tie-grouped AP) against per-label probabilities, both tie rules | `probe_ap_repro.out.txt`, the planner's run; reproduced byte for byte |
| `probe_ap.py` | reviewer | The same question, independently, with pandas | `probe_ap.out.txt`, re-run by the engineer with the rewritten script |
| `probe_sign.py` | reviewer | `pano_pose` puts the gravity horizon T(b) above the rig raster's middle row | `probe_sign.out.txt`, re-run by the engineer |
| `probe_strata.py` | reviewer | The depressions at which v2 and v3 reach 37.5 deg and 70 deg windows (PR-B's strata) | `probe_strata.out.txt`, re-run by the engineer |

`v3-rerun-8d8d8ee.json` is the planner's re-run of `reports/scripts/crop_sizing_v3.py` against RampNet `8d8d8ee`
(without `--figure`). Every block equals `reports/data/2026-09-26-crop-sizing-v3.json` except `meta` (its
`rampnet_commit` and `generated_by`) and the figure block and the `populations` entry that lists it.
