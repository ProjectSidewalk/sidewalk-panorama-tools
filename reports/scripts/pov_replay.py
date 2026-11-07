"""Exact front-end click->pano replay math, ported from label-latlng-estimation for the cropper studies.

The definitions moved to the installable package, sidewalk_panorama_tools/ps_math.py (#222 step 6), because
consumers outside this repo (sidewalk-auto-labeler, RampNet) carried their own ports, and production code never
imports from reports/ (.claude/rules/desk-studies.md). This module re-exports them under the names every study
and test already uses; the history below is the port's provenance and still describes the code.

This is the projection the Project Sidewalk front end actually runs when it stores a label's
pano_x/pano_y: `calculatePovIfCentered` (UtilitiesPanomarker.js, verified against SidewalkWebpage tag
v7.19.10) followed by `calculatePanoXYFromPov`. The sibling repo proved the fidelity three ways
(label-latlng-estimation, reports/2026-08-06-pov-inversion.md): pano_y replays exactly for every row
with a replay target, post-2021 pano_x replays exactly (integer equality) in all six non-DC cities,
and the pre-2021 misses carry the per-pano-constant signature of camera_heading metadata drift, not
projection error.

Why it lives here: the #54 placement study needs, per label, the deterministic replay of the stored
coordinate (era identification + a projection-error covariate with zero annotation cost), and the #32
sizing study needs the exact depression angle plus the refit distance blend. Everything below is a
pure function of cvMetadata columns; no network.

Ported from label-latlng-estimation@main python/pov_inversion.py (math verbatim, vectorized numpy,
degrees in/out) and python/distance_refit.py::_predict_blend, with the final calibration from
data/modern-truth-summary.json -> final_coefficients. Field-name adaptations for cvMetadata only:
canvas_width/canvas_height come from the row (the sibling repo hardcodes 720x480, which is what every
recovered row carried), and camera_heading is cvMetadata's photographer heading.

Era vocabulary (the sibling repo's two boundaries; keep them distinct):
- 2021-01-01 UTC: legacy depth-era client (parseInt-truncated POV inputs; the +0.72 deg Math.ceil
  depth-lookup bearing bias lives in stored LAT/LNG targets of that era, never in pano_x/pano_y).
- Evolution 179, 2023-03-29 UTC (SidewalkWebpage v7.12.2): every stored pano_x/pano_y recomputed with
  exactly this math, and the front end switched its distance input from the fixed 13312x6656 frame to
  real pano pixels. Post-179 rows replay bit-for-bit given the stored camera metadata.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sidewalk_panorama_tools.ps_math import (  # noqa: E402,F401  (re-exports; defined in ps_math.py)
    BLEND_CAMERA_HEIGHT_M,
    BLEND_DEG,
    CANVAS_H,
    CANVAS_W,
    DIST_CAP_M,
    depression_from_pano_y,
    exact_depression_deg,
    get_3d_fov,
    pano_xy_from_pov,
    pov_from_pano_xy,
    pov_if_centered,
    predict_blend_distance,
)

# There is deliberately no frame-level `replay_pano_xy(df)` helper here. It existed, was never
# called, and duplicated era_replay_study.replay_frame() minus that function's NaN masking - two
# implementations of one projection that had to be kept in step, with the untested one being the
# more permissive. Use era_replay_study.replay_frame(); it returns the residuals and replayability
# masks as well. (Removed 2026-08-10 in review, along with an unused wrap_deg.)
