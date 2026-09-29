"""One definition of every frame the #54 tilt study uses: bearings, elevations, pixels, the depth
artifact's plane frame, and the rig attitude (pitch, roll) that relates a gravity-levelled direction
to the camera rig's own frame.

**Runs on makelab2 as well as here** (the pose scan and the tile-frame estimator import it on the
store host): Python 3.9 / numpy 1.23, so no `match`, no `X | None` annotations, no 3.10+ stdlib.
tests/test_tilt_pose_scan.py parses this file with `feature_version=(3, 9)`.

Conventions (every function here, degrees in and out, scalars or numpy arrays):

* **Bearing b**: degrees clockwise from the pano's forward (heading) direction, in (-180, 180].
  **Elevation el**: degrees above the horizon.
* **Pixels** of a heading-centred equirectangular raster (the stored JPEG; pov_replay's convention),
  continuous: x = ((b + 180)/360)*w mod w, y = (0.5 - el/180)*h; inverse b = (x/w)*360 - 180,
  el = 90 - (y/h)*180. An integer column c of a raster (e.g. the 512x256 depth raster) has its
  centre at x = c + 0.5, as docs/depth.md's (c + 0.5)/w says.
* **Internal frame RFU**: (right, forward, up), right-handed. direction_rfu(b, el) =
  (cos el sin b, cos el cos b, sin el).
* **Depth artifact frame**: defined by the ray formula of docs/depth.md and gsv._write_depth_artifact,
  v(r, c) = (sin t cos f, sin t sin f, cos t), t = (h-r-0.5)/h*pi, f = (w-c-0.5)/w*2pi + pi/2.
  Worked through: column w/2 (forward) is (0, -1, 0), column w/4 (90 deg left) is (1, 0, 0), and the
  bottom row is (0, 0, +1). So the artifact's axes are **x = +left, y = +backward, z = +DOWN**, i.e.
  artifact = -RFU. (The #54 plan said z = +up; the ray formula says down, and the test that reproduces
  the formula is what decides it. Plane normals are sign-ambiguous, so nothing that reads a normal as
  an unoriented line is affected - but an elevation read off n_z with the wrong sign would be.)
* **Pose**: pitch_deg, roll_deg in streetlevel's sign (pitch = 90 - raw, roll = raw, converted to
  degrees and wrapped to (-180, 180] by wrap_deg). **Measured meaning** (endpoint F1 of
  reports/2026-09-26-tilt-error-study.md - facade normals of the store's depth artifacts against their
  own pose, slopes -1.00 under the opposite hypothesis the #54 plan started from): **pitch > 0 = the
  forward axis points below the horizon (nose down); roll > 0 = the left side up, right side down.**
  So a gravity-horizontal direction at bearing b has rig elevation +(pitch cos b + roll sin b) = +T(b),
  and a point stored in gravity-levelled pixels sits at y - T(b)*h/180 in a rig-frame raster. This is
  evidence, not convention: tests/test_tilt_error_study.py re-measures it against the committed scan.
"""

import numpy as np


def wrap_deg(a):
    """Wrap degrees to (-180, 180]. photometa serves roll in [0, 360), so 359.6 means -0.4."""
    a = np.asarray(a, dtype=float)
    out = -((-a + 180.0) % 360.0) + 180.0
    return float(out) if out.ndim == 0 else out


def direction_rfu(bearing_deg, elevation_deg):
    """Unit vectors (..., 3) in RFU for bearings/elevations in degrees."""
    b = np.radians(np.asarray(bearing_deg, dtype=float))
    e = np.radians(np.asarray(elevation_deg, dtype=float))
    b, e = np.broadcast_arrays(b, e)
    return np.stack([np.cos(e) * np.sin(b), np.cos(e) * np.cos(b), np.sin(e)], axis=-1)


def bearing_elevation(v):
    """(bearing_deg, elevation_deg) of RFU vectors (..., 3); the inverse of direction_rfu.
    A zero vector gives (nan, nan)."""
    v = np.asarray(v, dtype=float)
    norm = np.linalg.norm(v, axis=-1)
    with np.errstate(invalid='ignore', divide='ignore'):
        el = np.degrees(np.arcsin(np.clip(v[..., 2] / norm, -1.0, 1.0)))
    b = np.degrees(np.arctan2(v[..., 0], v[..., 1]))
    b = np.where(norm > 0, wrap_deg(b), np.nan)
    el = np.where(norm > 0, el, np.nan)
    if b.ndim == 0:
        return float(b), float(el)
    return b, el


def _rx(a_deg):
    a = np.radians(np.asarray(a_deg, dtype=float))
    c, s, o, z = np.cos(a), np.sin(a), np.ones_like(a), np.zeros_like(a)
    return np.stack([np.stack([o, z, z], -1), np.stack([z, c, -s], -1), np.stack([z, s, c], -1)], -2)


def _ry(a_deg):
    a = np.radians(np.asarray(a_deg, dtype=float))
    c, s, o, z = np.cos(a), np.sin(a), np.ones_like(a), np.zeros_like(a)
    return np.stack([np.stack([c, z, s], -1), np.stack([z, o, z], -1), np.stack([-s, z, c], -1)], -2)


def rig_from_gravity(pitch_deg, roll_deg):
    """3x3 R (or (..., 3, 3) for array poses) with v_rig = R @ v_gravity, both in RFU.

    The rig's axes are the gravity axes rotated by pitch about the right axis (forward DROPS for
    pitch > 0), then by roll about the pitched forward axis (the right side drops, the left side
    rises, for roll > 0) - the measured meaning of streetlevel's sign (module docstring); intrinsic
    pitch-then-roll, the order of sidewalk-auto-labeler's
    geo._world_ray. Yaw is not needed: bearings are already relative to forward. The order matters only
    at second order (< 0.01 deg for |tilt| <= 3 deg) but is pinned by a test so it cannot drift.
    """
    pitch_deg, roll_deg = np.broadcast_arrays(np.asarray(pitch_deg, dtype=float),
                                              np.asarray(roll_deg, dtype=float))
    axes = _rx(-pitch_deg) @ _ry(roll_deg)   # columns: rig axes in gravity coordinates
    return np.swapaxes(axes, -1, -2)


def _rotate(bearing_deg, elevation_deg, R):
    v = direction_rfu(bearing_deg, elevation_deg)
    return bearing_elevation(np.einsum('...ij,...j->...i', R, v))


def gravity_to_rig(bearing_deg, elevation_deg, pitch_deg, roll_deg):
    """Exact: where a gravity-frame direction appears in the rig frame. -> (bearing_rig, el_rig)."""
    return _rotate(bearing_deg, elevation_deg, rig_from_gravity(pitch_deg, roll_deg))


def rig_to_gravity(bearing_deg, elevation_deg, pitch_deg, roll_deg):
    """Exact inverse of gravity_to_rig."""
    return _rotate(bearing_deg, elevation_deg, np.swapaxes(rig_from_gravity(pitch_deg, roll_deg), -1, -2))


def tilt_term_deg(bearing_deg, pitch_deg, roll_deg):
    """First-order T(b) = pitch cos b + roll sin b: how far the gravity horizon at bearing b sits
    ABOVE the rig horizon (el_rig ~ el_gravity + T), in the measured sign."""
    b = np.radians(np.asarray(bearing_deg, dtype=float))
    return pitch_deg * np.cos(b) + roll_deg * np.sin(b)


def vertical_lean_deg(bearing_deg, pitch_deg, roll_deg):
    """First-order r cos b - p sin b = dT/d(b in radians) = d(el_rig of the gravity horizon)/db: the
    angle, in degrees, by which the gravity horizon (and every gravity-vertical edge crossing it) is
    rotated in a rig-frame equirectangular image at bearing b. Positive = the horizon rises to the
    right (+x), i.e. vertical edges rotated counter-clockwise as the image is viewed."""
    b = np.radians(np.asarray(bearing_deg, dtype=float))
    return roll_deg * np.cos(b) - pitch_deg * np.sin(b)


def pixel_from_bearing_elevation(bearing_deg, elevation_deg, pano_width, pano_height):
    """Continuous (x, y) on a heading-centred equirectangular raster."""
    b = np.asarray(bearing_deg, dtype=float)
    el = np.asarray(elevation_deg, dtype=float)
    x = ((b + 180.0) / 360.0 * pano_width) % pano_width
    y = (0.5 - el / 180.0) * pano_height
    return x, y


def bearing_elevation_from_pixel(x, y, pano_width, pano_height):
    """Inverse of pixel_from_bearing_elevation; bearing wrapped to (-180, 180]."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    return wrap_deg(x / pano_width * 360.0 - 180.0), 90.0 - y / pano_height * 180.0


def rig_pixel_from_gravity_pixel(x, y, pano_width, pano_height, pitch_deg, roll_deg):
    """Where a point stored in gravity-levelled pixels sits in a rig-frame raster: the correction
    CropRunner WOULD apply if endpoint C found the leak. To first order y moves by -T(b)*h/180."""
    b, el = bearing_elevation_from_pixel(x, y, pano_width, pano_height)
    return pixel_from_bearing_elevation(*gravity_to_rig(b, el, pitch_deg, roll_deg), pano_width, pano_height)


def gravity_pixel_from_rig_pixel(x, y, pano_width, pano_height, pitch_deg, roll_deg):
    """Inverse of rig_pixel_from_gravity_pixel."""
    b, el = bearing_elevation_from_pixel(x, y, pano_width, pano_height)
    return pixel_from_bearing_elevation(*rig_to_gravity(b, el, pitch_deg, roll_deg), pano_width, pano_height)


def artifact_normal_bearing_elevation(n):
    """(bearing, elevation) of vectors given in the depth artifact's frame (x left, y back, z down).
    A plane normal is an unoriented line, so (b, el) and (b + 180, -el) describe the same plane; the
    study's regressions are invariant under that flip because T(b + 180) = -T(b)."""
    n = np.asarray(n, dtype=float)
    return bearing_elevation(-n)


def xml_tilt_to_pitch_roll(pano_yaw_deg, tilt_yaw_deg, tilt_pitch_deg):
    """The 2019 XML endpoint's <projection_properties pano_yaw_deg tilt_yaw_deg tilt_pitch_deg/> ->
    (pitch_deg, roll_deg) in streetlevel's sign.

    tilt_pitch_deg is the total tilt magnitude and tilt_yaw_deg - pano_yaw_deg its direction, with
    pitch = -m cos(dir), roll = -m sin(dir). Fitted, not assumed: reports/scripts/tilt_error_study.py
    scores all eight sign/axis alternatives over every Seattle pano carrying both an .xml and a
    .depth.npz, and tests/test_tilt_geometry.py pins this function against that committed overlap.
    """
    d = np.radians(np.asarray(tilt_yaw_deg, dtype=float) - np.asarray(pano_yaw_deg, dtype=float))
    m = np.asarray(tilt_pitch_deg, dtype=float)
    return -m * np.cos(d), -m * np.sin(d)


def pitch_roll_to_xml_tilt(pano_yaw_deg, pitch_deg, roll_deg):
    """Inverse of xml_tilt_to_pitch_roll (for synthetic tests): -> (pano_yaw, tilt_yaw, tilt_pitch)."""
    p = np.asarray(pitch_deg, dtype=float)
    r = np.asarray(roll_deg, dtype=float)
    m = np.hypot(p, r)
    d = np.degrees(np.arctan2(-r, -p))
    return np.asarray(pano_yaw_deg, dtype=float), np.asarray(pano_yaw_deg, dtype=float) + d, m
