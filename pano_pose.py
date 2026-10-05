"""The pano's pose and the one definition of every frame the #54 tilt study measured: bearings,
elevations, pixels, the depth artifact's plane frame, and the rig attitude (pitch, roll) that relates a
gravity-levelled direction to the camera rig's own frame. Production code, used by CropRunner's opt-in tilt
correction (#191); reports/scripts/tilt_geometry.py re-exports the geometry from here, so the study and the
cropper share one definition rather than a copy.

**Runs on makelab2 as well as here** (tilt_geometry.py and this file are uploaded side by side for the pose
scan and the tile-frame estimator): Python 3.9 / numpy 1.23, so no `match`, no `X | None` annotations, no
3.10+ stdlib. tests/test_tilt_pose_scan.py parses this file with `feature_version=(3, 9)`. Nothing at module
scope imports the repo; resolve_pano_pose imports downloaders.gsv only when it is called without the suffix.

Conventions (every geometry function here, degrees in and out, scalars or numpy arrays):

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

What is added here for the cropper, below the geometry: corrected_pixel (the correction scaled by beta),
the two pose readers and the scrape-era rule that picks between them (resolve_pano_pose), and label_era
(the SidewalkWebpage v7.12.2 boundary the study's per-era beta is keyed on). docs/cropper.md, "The tilt
correction (opt-in, #191)", is the operator's account.
"""

import collections
import datetime
import math
import os
import xml.etree.ElementTree as ET

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
    CropRunner applies at beta = 1 under --tilt-correction (#191; corrected_pixel scales it). To first
    order y moves by -T(b)*h/180."""
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


# ---------------------------------------------------------------------------
# The cropper's side (#191): beta, the pose readers, and the label's era.


def corrected_pixel(x, y, pano_width, pano_height, pitch_deg, roll_deg, beta):
    """The crop centre for a label stored at (x, y), with a fraction `beta` of the rig transform applied.

    Beta scales the POSE, not the pixel move: rig_pixel_from_gravity_pixel at (beta * pitch, beta * roll).
    Beta 0 is exactly the identity, beta 1 is the full transform, and to first order y moves by
    -beta * T(b) * h / 180. Interpolating between the stored and the rig pixel agrees to first order and
    differs at second; scaling the pose is what "a fraction of the tilt leaked into the click mapping"
    means physically. -> (x, y) as floats, x in [0, pano_width).
    """
    if beta == 0:
        # Exact, not merely to rounding: a zero rotation through the trigonometry returns the point to
        # ~1e-12 px, but "beta 0 is the identity" is a promise the marker's 0.0 leans on.
        return float(x) % pano_width, float(y)
    cx, cy = rig_pixel_from_gravity_pixel(x, y, pano_width, pano_height, beta * pitch_deg, beta * roll_deg)
    return float(cx), float(cy)


PanoPose = collections.namedtuple('PanoPose', 'pitch_deg roll_deg source')

POSE_SOURCE_XML = 'xml'
POSE_SOURCE_NPZ = 'npz'

# The 2019-22 XML endpoint's file beside the JPEG (the endpoint died in 2022).
XML_SUFFIX = '.xml'

_XML_TILT_KEYS = ('pano_yaw_deg', 'tilt_yaw_deg', 'tilt_pitch_deg')


def pose_from_xml(path):
    """(PanoPose, None) from a 2019-22 <id>.xml's <projection_properties pano_yaw_deg tilt_yaw_deg
    tilt_pitch_deg/>, through xml_tilt_to_pitch_roll; or (None, reason) when the file cannot be read or
    any of the three is missing, blank, unparseable or not finite. Never guessed."""
    try:
        root = ET.parse(path).getroot()
    # Broad, like the npz reader's: ET.parse raises LookupError, not ParseError, for a declaration naming an
    # encoding Python does not know, and a narrow except let that end the whole crop run (#193 review).
    except Exception as e:
        return None, 'xml unreadable (%s: %s)' % (type(e).__name__, e)
    proj = root.find('.//projection_properties')
    if proj is None:
        return None, 'xml has no projection_properties'
    values = []
    for key in _XML_TILT_KEYS:
        raw = proj.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float('nan')
        if not math.isfinite(value):
            return None, 'xml %s is %r' % (key, raw)
        values.append(value)
    pitch, roll = xml_tilt_to_pitch_roll(*values)
    return PanoPose(float(pitch), float(roll), POSE_SOURCE_XML), None


def pose_from_depth_artifact(path):
    """(PanoPose, None) from a .depth.npz's scalar `pitch` and `roll` (radians, streetlevel's sign; gsv
    stores pitch already as 90 - raw), converted to degrees and wrapped; or (None, reason) when the file
    cannot be read or either is missing, non-scalar or not finite (gsv writes NaN when Google omitted it).
    Reads only those two members - an npz is a zip, and the rasters are never touched."""
    try:
        with np.load(path) as art:
            missing = [key for key in ('pitch', 'roll') if key not in art.files]
            if missing:
                return None, 'npz has no %s' % ' or '.join(missing)
            pitch = float(art['pitch'])
            roll = float(art['roll'])
    except Exception as e:  # a corrupt zip, a truncated member, a non-scalar value: all "no pose"
        return None, 'npz unreadable (%s: %s)' % (type(e).__name__, e)
    if not (math.isfinite(pitch) and math.isfinite(roll)):
        return None, 'npz pitch/roll not finite (%r, %r)' % (pitch, roll)
    return PanoPose(wrap_deg(math.degrees(pitch)), wrap_deg(math.degrees(roll)), POSE_SOURCE_NPZ), None


def resolve_pano_pose(pano_jpg_path, depth_suffix=None):
    """The pose that matches this JPEG's scrape era: (PanoPose, None) or (None, reason).

    A pano with an <id>.xml beside it is a 2019-22 stitch, so the xml decides - even when a newer
    .depth.npz also exists, which may describe a since-re-rendered pano - and an incomplete xml is no pose,
    NEVER a fall-through to the npz. Otherwise the <id>.depth.npz (docs/depth.md) if present; otherwise no
    pose. The rule of reports/scripts/tilt_adjudicate.attach_pose, per pano.

    `depth_suffix` is downloaders.gsv.DEPTH_ARTIFACT_SUFFIX. CropRunner passes it from its own module-scope
    import, so a crop run never imports anything per pano (#193 review); left None, it is imported here.
    """
    if depth_suffix is None:
        # Imported here, not at module scope: this file is uploaded to makelab2 without the repo, and the
        # geometry above must import there. The suffix is gsv's, never restated.
        from downloaders.gsv import DEPTH_ARTIFACT_SUFFIX as depth_suffix
    stem = os.path.splitext(pano_jpg_path)[0]
    xml_path = stem + XML_SUFFIX
    if os.path.exists(xml_path):
        return pose_from_xml(xml_path)
    npz_path = stem + depth_suffix
    if os.path.exists(npz_path):
        return pose_from_depth_artifact(npz_path)
    return None, 'no %s and no %s beside the pano' % (XML_SUFFIX, depth_suffix)


# SidewalkWebpage v7.12.2: from here the front end wrote pano_x/pano_y live with the exact projection
# (reports/scripts/rawlabels.py's EVO179, which tests/test_pano_pose.py pins this against).
EVO179_UTC = '2023-03-29T00:00:00+00:00'
_EVO179 = datetime.datetime.fromisoformat(EVO179_UTC)

ERA_POST179 = 'post179'
ERA_LEGACY_MID = 'legacy+mid'
ERA_UNKNOWN = 'unknown'
ERAS = (ERA_LEGACY_MID, ERA_POST179, ERA_UNKNOWN)

_DECIMAL_CHARS = frozenset('0123456789.')


def _parse_time_created(value):
    """An aware UTC datetime, or None. Epoch milliseconds (int, float or digit string: what rawLabels
    exports) or ISO 8601 (a trailing Z included; naive means UTC)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        millis = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if all(c in _DECIMAL_CHARS for c in text):
            try:
                millis = float(text)
            except ValueError:
                return None
        else:
            if text[-1] in 'Zz':
                text = text[:-1] + '+00:00'
            try:
                stamp = datetime.datetime.fromisoformat(text)
            except ValueError:
                return None
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=datetime.timezone.utc)
            return stamp.astimezone(datetime.timezone.utc)
    else:
        return None
    if not math.isfinite(millis):
        return None
    try:
        return datetime.datetime.fromtimestamp(millis / 1000.0, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def label_era(time_created):
    """'post179' at or after EVO179_UTC, 'legacy+mid' before it, 'unknown' for anything unreadable
    (blank, None, NaN, garbage) - the keys of CropRunner.TILT_BETA_BY_ERA."""
    stamp = _parse_time_created(time_created)
    if stamp is None:
        return ERA_UNKNOWN
    return ERA_POST179 if stamp >= _EVO179 else ERA_LEGACY_MID
