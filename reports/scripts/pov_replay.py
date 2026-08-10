"""Exact front-end click->pano replay math, ported from label-latlng-estimation for the cropper studies.

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

import numpy as np

# The viewer canvas every recovered label row carried, and what evolution 179 hardcoded. cvMetadata
# serves per-row canvas_width/canvas_height; pass those when present rather than assuming.
CANVAS_W = 720.0
CANVAS_H = 480.0

# Final distance calibration (label-latlng-estimation #3, Stage 4 closure): horizon-saturating
# cotangent blend on the exact depression angle, one flat camera height, no label-type input.
# data/modern-truth-summary.json -> final_coefficients; held-out median 0.41 m vs 1.24 m deployed.
BLEND_CAMERA_HEIGHT_M = 2.341219672825709
BLEND_DEG = 11.25
DIST_CAP_M = 50.0


def get_3d_fov(zoom):
    """UtilitiesPanomarker's get3dFov: the GSV viewer's 3D field of view in degrees per zoom level.

    zoom 1 -> 89.75, zoom 2 -> 53, zoom 3 -> 27.68 (the JS's "determined experimentally" branch).
    """
    z = np.asarray(zoom, float)
    return np.where(z <= 2, 126.5 - z * 36.75, 195.93 / np.power(1.92, z))


def pov_if_centered(canvas_x, canvas_y, heading, pitch, zoom,
                    canvas_width=CANVAS_W, canvas_height=CANVAS_H):
    """Exact replica of calculatePovIfCentered: the (heading, pitch) a click would have at canvas
    center. Returns (pov_heading, pov_pitch) in degrees; pov_heading in (-180, 180] clockwise from
    north, pov_pitch positive above the horizon.

    Models the viewport as a rectilinear (gnomonic) camera aimed at (heading, pitch) with focal
    length f = (canvas_width/2)/tan(fov/2) px; canvas origin is top-left. The sgn(cos(pitch)) factor
    is the JS's beyond-vertical guard (never fires for real viewer pitch, kept for fidelity).
    """
    fov = np.radians(get_3d_fov(zoom))
    h0 = np.radians(np.asarray(heading, float))
    p0 = np.radians(np.asarray(pitch, float))
    canvas_width = np.asarray(canvas_width, float)
    canvas_height = np.asarray(canvas_height, float)
    f = 0.5 * canvas_width / np.tan(0.5 * fov)

    du = np.asarray(canvas_x, float) - canvas_width / 2
    dv = canvas_height / 2 - np.asarray(canvas_y, float)
    sg = np.where(np.cos(p0) >= 0, 1.0, -1.0)

    x = f * np.cos(p0) * np.sin(h0) + du * sg * np.cos(h0) - dv * np.sin(p0) * np.sin(h0)
    y = f * np.cos(p0) * np.cos(h0) - du * sg * np.sin(h0) - dv * np.sin(p0) * np.cos(h0)
    z = f * np.sin(p0) + dv * np.cos(p0)

    r = np.sqrt(x * x + y * y + z * z)
    return np.degrees(np.arctan2(x, y)), np.degrees(np.arcsin(z / r))


def exact_depression_deg(canvas_x, canvas_y, heading, pitch, zoom,
                         canvas_width=CANVAS_W, canvas_height=CANVAS_H):
    """The click's depression angle below the horizon (positive down): -pov_pitch."""
    _, pov_pitch = pov_if_centered(canvas_x, canvas_y, heading, pitch, zoom,
                                   canvas_width, canvas_height)
    return -pov_pitch


def pano_xy_from_pov(pov_heading, pov_pitch, camera_heading, pano_width, pano_height):
    """calculatePanoXYFromPov with evolution 179's round-then-wrap: POV -> integer pano pixels.

    The pano raster is heading-centred: its centre column looks along camera_heading, so column zero
    sits at bearing camera_heading - 180. The y mapping is linear in elevation - which is exactly
    correct for a gravity-aligned equirectangular image; what it lacks is any rig pitch/roll term,
    which is the #54 question.

    One tie-break differs from the JS by construction: np.round is half-to-even, Math.round is
    half-up, so a value landing exactly on .5 can differ by one pixel. The era replay study measures
    the practical consequence at zero - pano_y replays exactly for 100% of legacy/mid rows across
    438k labels - but "bit-for-bit" is a claim about that measurement, not about the tie rule.
    """
    pano_width = np.asarray(pano_width, float)
    pano_height = np.asarray(pano_height, float)
    heading_wrapped = (np.asarray(pov_heading, float) + 360) % 360
    heading_pixel_zero = ((np.asarray(camera_heading, float) + 180) % 360 + 360) % 360
    pano_x = (pano_width + np.round(pano_width * (heading_wrapped - heading_pixel_zero) / 360)) % pano_width
    pano_y = pano_height / 2 - np.round((pano_height / 2) * (np.asarray(pov_pitch, float) / 90))
    return pano_x.astype(int), pano_y.astype(int)


# There is deliberately no frame-level `replay_pano_xy(df)` helper here. It existed, was never
# called, and duplicated era_replay_study.replay_frame() minus that function's NaN masking - two
# implementations of one projection that had to be kept in step, with the untested one being the
# more permissive. Use era_replay_study.replay_frame(); it returns the residuals and replayability
# masks as well. (Removed 2026-08-10 in review, along with an unused wrap_deg.)


def depression_from_pano_y(pano_y, pano_height):
    """Depression below the horizon implied by a stored pano pixel: exact and resolution-independent
    for a gravity-aligned equirect pano. Positive down."""
    pano_y = np.asarray(pano_y, float)
    pano_height = np.asarray(pano_height, float)
    return (pano_y - pano_height / 2) * 180.0 / pano_height


def predict_blend_distance(depression_deg,
                           camera_height_m=BLEND_CAMERA_HEIGHT_M,
                           blend_deg=BLEND_DEG,
                           cap_m=DIST_CAP_M):
    """The refit distance estimator: cotangent above blend_deg of depression, matched-value/
    matched-slope linear tail below it, clamped [0, cap_m]. label-latlng-estimation's
    distance_refit._predict_blend with the shipped flat-height calibration.

    Above the horizon (depression <= 0) the tail returns its horizon value; the structural maximum
    with the shipped parameters is ~23.85 m, never a divergence.
    """
    dep = np.asarray(depression_deg, float)
    a_rad = np.radians(blend_deg)
    value_at_blend = camera_height_m / np.tan(a_rad)
    slope = -camera_height_m * (np.pi / 180.0) / np.sin(a_rad) ** 2
    cot = camera_height_m / np.tan(np.radians(np.maximum(dep, 1e-9)))
    tail = value_at_blend + slope * (np.maximum(dep, 0.0) - blend_deg)
    return np.clip(np.where(dep >= blend_deg, cot, tail), 0.0, cap_m)
