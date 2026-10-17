---
paths:
  - "CropRunner.py"
  - "pano_pose.py"
  - "sidewalk_panorama_tools/*.py"
  - "tests/test_geometry.py"
  - "tests/test_torch_geometry.py"
  - "docs/geometry.md"
  - "pyproject.toml"
  - "tests/test_pano_pose.py"
  - "tests/test_crop_tilt.py"
  - "reports/scripts/tilt_*.py"
  - "reports/scripts/pov_replay.py"
  - "tests/test_tilt_*.py"
  - "tests/test_pov_replay.py"
  - "reports/data/*tilt*"
  - "reports/2026-09-26-tilt-error-study.md"
  - "reports/plans/*tilt*"
---
# Tilt: the rig frame, the #54 study, and the crop-time correction (#191)

Loaded by Claude Code when a file matching the globs above is read. The repo-wide guidance is `CLAUDE.md` at the repo root; read it first. When behaviour described here changes, this file, the relevant `docs/` page and (if a summary there moved) the root file all need the edit. The cropper as a whole is `.claude/rules/cropper.md`.

## The depth planes are in the rig frame, and the tiles are not levelled (#54, measured 2026-09-26)

- **One frame, one sign, in `pano_pose.py`** (`reports/scripts/tilt_geometry.py` re-exports it, #191). The depth artifact's axes are x = left,
  y = back, **z = down** (the ray formula in `docs/depth.md`, not "z up"). Its facade normals follow the stored
  pose with slopes 0.9995 / 1.0003, which fixes the meaning of streetlevel's sign: **`pitch > 0` = the forward axis
  points below the horizon, `roll > 0` = left side up**, so a gravity-horizontal direction sits at rig elevation
  `+T(b)`, `T(b) = pitch cos b + roll sin b`, `b = (pano_x / w) * 360 - 180`. The #54 plan had both the axis and
  the sign backwards; don't re-derive either, call the module.
- **The stored `pano_y` of a tilted GSV pano is NOT pixel-true: it is off towards the rig pixel
  (adjudicated 2026-09-29).** The stored tiles are not gravity-levelled in either scrape era (F2: every arm's
  raw lean slope excludes 0; the calibrated slopes are estimates, 0.634-1.098). Endpoint C, Jon's blind forced
  choice on 96 lead-labeller-vouched labels: the window shifted by T in the predicted direction beat its
  mirror **79 : 0** (p = 1.7e-24). post179 is confirmed (98% of single-window answers); legacy+mid is not
  (84%). **C gives the direction, not the size**: every shifted window moves by the full T. The #191 beta batch
  (`2026-09-29-tilt-beta-jm/`) is corroboration only: it leans below 1 in both arms but, at 5 discordant sheets
  per arm, its primary test could not reject (DECISIONS.md section 5). The per-pose-record RampNet slopes are
  about 0.88 (XML pose) and 0.95 (npz pose), and those are the shipped betas (Jon, #197, 2026-10-06). The correction is
  CropRunner's opt-in `--tilt-correction` (`.claude/rules/cropper.md`, item 2): both axes, via
  `pano_pose.corrected_pixel`, the pose from the pano's own `.xml`/`.npz`, beta from `TILT_BETA_BY_POSE_SOURCE`
  (keyed on the pose record: xml 0.88, npz 0.95 since #197; #193 shipped both at 1.0), recorded per crop in the
  provenance manifest (#196: `pose_source`, `tilt_beta`, `corrected_pano_x`, `corrected_pano_y`); a row is
  corrected only if its `tilt_beta` is nonzero too (`manifest_row_is_tilt_corrected`, #200), since beta 0 is the
  identity. The flag stays off (#197). `TestRegistration` pins beta 1.0 on purpose (it checks the full transform).
- **The C folders are sealed, and the redraw is the decision-bearing one.** `reports/data/2026-09-29-tilt-adjudication-jm/`
  and `2026-09-30-tilt-adjudication-jm-b2/` hold Jon's verdicts, his notes, and a `sealed/` key and selection;
  `DECISIONS.md` fixed the scoring before unblinding. The first draw (`2026-09-26-tilt-adjudication/`) is
  superseded (no validation filter); its machine-judge pass is kept, and Jon's 7 verdicts on it are a pilot
  (`pilot-*.jsonl`, deliberately outside the analysis's `verdicts_*` glob). `next`/`record` refuse to run while
  a key file sits outside `sealed/`. Never move a key beside its sheets, and never caption a figure with one.
  `tilt_jm_pool.py` rebuilds the redraw from committed data, and `tilt_adjudicate_ui.py` is the judging page.
  The beta folder is sealed the same way but built with `--design beta` (`draw` and `sheets`); score it with
  `tilt_adjudicate.py score-beta` (writes the committed `sealed/score_beta_jon.json`), never `score`.
  **This file deliberately does not load inside a judge folder**: `reports/data/*tilt*` matches the study's
  files, but `*` does not cross `/`, so a judge working in `reports/data/<batch>/` is never handed C's result
  above. Keep it that way when adding globs; the judge README (`tilt_adjudicate.JUDGE_README`) names this file
  as one not to open, and a test checks that list against every file that quotes the result (bar the
  report, the judge folders and `reports/plans/`, which are records).
- **The `.xml` files beside 2019-22 scrapes carry legacy tilt**, for dead panos too.
  `tilt_geometry.xml_tilt_to_pitch_roll` is the one conversion (fitted on 3,594 panos with both files, median
  residual about 0.1 deg). Pose a JPEG by the file of its own scrape era: an `.xml` JPEG is a 2019-22 stitch, and a 2026
  `.npz` may describe a re-render. PS's `camera_pitch` equals this pitch; PS stores **no** roll for GSV labels.

## The installable geometry package (`sidewalk_panorama_tools`, docs/geometry.md)

- **What it is.** `pyproject.toml` installs `sidewalk_panorama_tools` (the gnomonic label view,
  `geometry.label_view` / `render_view` / `sampling_grid`, ported from RampNet `rampnet/crops.py`) plus
  `pano_pose` as a top-level module; sidewalk-validator-ai and sidewalk-ai-api pin it by tag. Nothing else in
  the repo is installed, and the suite never imports from an install (conftest's `sys.path` insert).
- **One convention.** `pano_pose.py` stays the home of the frames and the sign, because it is uploaded to
  makelab2 on its own beside `tilt_geometry.py`; `sidewalk_panorama_tools/pose.py` only re-exports it
  (`tests/test_geometry.py` checks every name IS pano_pose's). `geometry.tilt_matrix` is
  `pano_pose.rig_from_gravity` at `beta` times the pose, so its sign is the measured F1 one; RampNet's
  `VIEWER_TILT = "mm"` is the same relation, and a test pins the agreement (identical for one axis, within
  the composition-order term `pitch * roll` for two). Do not reintroduce RampNet's four-way convention table.
- **Beta default 0.95**, because the label's DB `camera_pitch` equals streetlevel's modern pose record
  exactly (sidewalk-auto-labeler#113, all 2,952 paired Gainesville labels), and beta under that record is
  0.953 (SE 0.020) on 3,518 pairs (0.884 under the XML record; Gainesville alone 0.951). CropRunner's
  per-pose-record betas (xml 0.88, npz 0.95) are unchanged and are not read from here.
- **Anti-aliasing and wrapping are the package's.** `render_view` box-reduces by `view.reduction_factor(W)`
  (trailing remainder dropped, grid divided by k), and a torch consumer `avg_pool2d`s by the same k; k = 1 for
  both current consumers. `label_view` wraps `x_norm` modulo 1; `wrap180` is `pano_pose.wrap_deg`.
- **The torch twin** (`torch_geometry.py`, the `[torch]` extra) imports torch lazily and reuses
  `geometry._basis` and the numpy tilt matrix, so it adds no second definition of where a view looks. CI has no
  torch: its test skips and `.coveragerc` omits the module. Run `tests/test_torch_geometry.py` locally with
  torch installed after touching either module.
- **Changing it is a release.** Consumers pin a tag; bump `version` in `pyproject.toml` and `__version__`
  together (a test checks), and say in the PR what a consumer re-pinning will see change.

## Also easy to get wrong

- **There is small but real Y-axis error in label positions** on the pano — diagnosed as uncorrected per-pano camera tilt in the click→pano mapping (SidewalkWebpage#4784); #54 tracks measuring it at crop level here, with a correction to follow if confirmed.
