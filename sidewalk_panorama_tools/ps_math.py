"""Project Sidewalk's front-end click and POV math, once (#222, step 6).

The projection the Project Sidewalk front end runs when it stores a label's pano_x/pano_y:
``calculatePovIfCentered`` (UtilitiesPanomarker.js, verified against SidewalkWebpage tag v7.19.10) followed by
``calculatePanoXYFromPov``, plus the inverse and the distance estimator the studies and the cropper's sizing
rule v3 use. Python ports of these lived in three places (``reports/scripts/pov_replay.py`` here,
``ps_placement.py`` in sidewalk-auto-labeler and ``rampnet/crops.py``); this module is the one definition,
and ``reports/scripts/pov_replay.py`` re-exports it.

Frames, the same in every function (docs/geometry.md, "ps_math"):

* ``pov_heading`` is degrees clockwise from north, in (-180, 180]; ``pov_pitch`` is degrees above the
  horizon (positive up). ``camera_heading`` is the photographer's heading, the bearing of the pano's centre
  column.
* The pano raster is heading-centred: its centre column looks along ``camera_heading``, so column zero sits at
  bearing ``camera_heading - 180``. ``pano_y`` grows downward, and the horizon is row ``pano_height / 2``.
* Depression is positive below the horizon: ``depression = -pov_pitch``.
* No rig pitch or roll anywhere: the front end ignores the pose, which is the #54 question
  (:mod:`sidewalk_panorama_tools.pose`, :func:`sidewalk_panorama_tools.geometry.label_view`).

Provenance: ported from label-latlng-estimation@main ``python/pov_inversion.py`` (math verbatim, vectorized
numpy, degrees in and out) and ``python/distance_refit.py::_predict_blend``, with the final calibration from
``data/modern-truth-summary.json`` -> ``final_coefficients``. The fidelity was proved there
(``reports/2026-08-06-pov-inversion.md``): pano_y replays exactly for every row with a replay target and
post-2021 pano_x replays exactly in all six non-DC cities. The era vocabulary (2021-01-01, evolution 179) is
in ``reports/scripts/pov_replay.py``'s docstring.

Usage::

    from sidewalk_panorama_tools.ps_math import pov_if_centered, pano_xy_from_pov, pov_from_pano_xy

    h, p = pov_if_centered(canvas_x, canvas_y, heading, pitch, zoom, canvas_width, canvas_height)
    x, y = pano_xy_from_pov(h, p, camera_heading, pano_width, pano_height)     # integer pixels
    h2, p2 = pov_from_pano_xy(x, y, camera_heading, pano_width, pano_height)   # back, within a pixel
"""

import numpy as np

__all__ = ['BLEND_CAMERA_HEIGHT_M', 'BLEND_DEG', 'CANVAS_H', 'CANVAS_W', 'DIST_CAP_M', 'depression_from_pano_y',
           'exact_depression_deg', 'get_3d_fov', 'pano_xy_from_pov', 'pov_from_pano_xy', 'pov_if_centered',
           'predict_blend_distance']


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


def pov_from_pano_xy(pano_x, pano_y, camera_heading, pano_width, pano_height):
    """The inverse of :func:`pano_xy_from_pov`: a pano pixel -> the (pov_heading, pov_pitch) looking at it.

    Continuous, with no rounding: ``pov_heading = camera_heading - 180 + pano_x * 360 / pano_width`` wrapped
    into (-180, 180], and ``pov_pitch = (pano_height / 2 - pano_y) * 180 / pano_height``. The forward map
    rounds to integer pixels, so ``pano_xy_from_pov(*pov_from_pano_xy(x, y, ...))`` returns integer ``(x, y)``
    exactly (``x`` modulo the width), while ``pov_from_pano_xy(*pano_xy_from_pov(h, p, ...))`` is back within
    half a pixel of ``(h, p)`` - one pixel at most, counting the seam - which is 360 / pano_width degrees.

    Not the front end's own ``calculatePovFromPanoXY``, which ignores ``camera_heading`` and measures pitch
    from the top row: that one returns a pano-relative angle, not the POV a click stored.

    Example: the centre pixel of a 16384 x 8192 pano, ``pov_from_pano_xy(8192, 4096, 100.0, 16384, 8192)``,
    looks along the camera, ``(100.0, 0.0)``.
    """
    pano_width = np.asarray(pano_width, float)
    pano_height = np.asarray(pano_height, float)
    heading = np.asarray(camera_heading, float) - 180.0 + np.asarray(pano_x, float) * 360.0 / pano_width
    heading = 180.0 - np.mod(180.0 - heading, 360.0)
    pitch = (pano_height / 2 - np.asarray(pano_y, float)) * 180.0 / pano_height
    return heading, pitch
