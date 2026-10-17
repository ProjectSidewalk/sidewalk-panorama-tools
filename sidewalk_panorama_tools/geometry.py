"""A perspective (gnomonic) view of a Project Sidewalk label from its equirectangular panorama, with
rig-tilt correction: the one implementation the training pipeline (sidewalk-validator-ai) and the production
API (sidewalk-ai-api) both pin. docs/geometry.md is the reader's account.

Ported from RampNet ``rampnet/crops.py`` at commit 4156847d12d3f4ccac6102f7770cd315d02791d0 (its crop
cutter, validated against the HF tag crops in RampNet ``docs/crop_cutter.md`` section 3), re-based onto this
repo's ``pano_pose`` frame and pose sign so there is one convention, not two. Dropped from the port: the
four-way ``TILT_CONVENTIONS`` table (the sign is measured, not a choice: see below), the labeler-viewport
view and its ``get_3d_fov``, the plain equirect window (CropRunner cuts those), and the JPEG/hash/black-
fraction helpers.

**Why gnomonic.** An equirect stretches the scene horizontally by 1/cos(latitude); a curb ramp 10-40 deg
below the horizon is exactly where that bites, and it varies across a crop. A gnomonic view is what a
camera, the Project Sidewalk viewer, and every crop a tag model has been trained on actually show.

Conventions (all pano_pose's; degrees in and out):

* **Pano point.** Normalized ``X = pano_x / pano_width``, ``Y = pano_y / pano_height``;
  ``lon = (X - 0.5) * 360`` (pano_pose's bearing: clockwise from the pano's centre column, which looks
  along ``camera_heading``) and ``lat = (0.5 - Y) * 180`` (pano_pose's elevation, up positive). The same
  mapping as Project Sidewalk's ``calculatePanoXYFromPov``. Resolution-independent: a stored JPEG at another
  resolution than the label's ``pano_width`` is sampled at the same normalized point.
* **Pixels** are continuous with pixel *i*'s centre at ``i + 0.5``; only x wraps (column 0 and column
  ``pano_width`` are the same place; the poles are not).
* **Rays** are unit vectors in pano_pose's RFU frame (right, forward, up), via ``pano_pose.direction_rfu``.
  A :class:`View` is a roll-free pinhole camera aimed at (``yaw_deg``, ``pitch_deg``) = (lon, lat) with
  focal length ``(width / 2) / tan(fov_h / 2)`` output pixels; output pixel (0, 0) is the top-left corner,
  so the view's centre is ``(width / 2, height / 2)``.
* **Tilt.** The stored ``pano_x``/``pano_y`` (and the labeler's heading/pitch) are in the **level frame**:
  Project Sidewalk's click mapping ignores the rig's ``camera_pitch``/``camera_roll``, while the stored
  equirect is in the **rig frame** (the known y-error, SidewalkWebpage#4784; this repo's #54/#191). A tilt
  matrix ``R`` (:func:`tilt_matrix`) takes a level-frame ray into the rig frame, ``v_rig = R @ v_level``;
  it is ``pano_pose.rig_from_gravity`` at the pose scaled by ``beta``. The sign is pano_pose's *measured*
  one: **pitch > 0 = nose down, roll > 0 = left side up**, so a level ray at bearing b sits at rig
  elevation, to first order, ``lat + beta * (camera_pitch * cos b + camera_roll * sin b)`` = ``lat +
  beta * T(b)``. With ``camera_pitch = 3`` and beta 0.9 a level ray straight ahead (b = 0) is at rig
  elevation +2.7 deg and one behind (b = 180) at -2.7; with ``camera_roll = 3`` one at b = 90 is at +2.7.
  tests/test_geometry.py pins those numbers. This is the same physical relation as RampNet's
  ``VIEWER_TILT = "mm"`` (``image_lat = level_lat + camera_pitch cos phi + camera_roll sin phi``), measured
  there independently against the HF viewport crops; a test pins the agreement.
* **Beta** is the fraction of the rig tilt that leaked into the stored click (it scales the pose, as
  ``pano_pose.corrected_pixel`` does). Measured per pose record in this repo (#191, #197): about 0.88 with
  a pano's ``.xml`` pose and 0.95 with its ``.depth.npz`` pose, which is what CropRunner's
  ``--tilt-correction`` uses. The default here, 0.9, is for the pose a consumer actually has: the label's
  ``camera_pitch`` column from the Project Sidewalk database (which equals the xml pitch; PS stores no roll
  for GSV labels). Beta 0, or no pose, means no correction (:func:`tilt_matrix` returns None).

Rendering with the tilt puts the stored point at the exact centre of a :func:`centered_view` of the rig-frame
pano; rendering without it centres the raw pixel at those coordinates, which is off the click by the tilt.
:func:`label_pixel` says where the click lands for any render tilt.

Pure numpy, no I/O. Python 3.9-compatible (no ``match``, no ``X | None``), like pano_pose. Usage::

    import numpy as np
    from PIL import Image
    from sidewalk_panorama_tools.geometry import label_view, reduction_factor, render_view
    pano = np.asarray(Image.open("pano.jpg").convert("RGB"))        # (H, W, 3) uint8, rig frame
    view, tilt = label_view(pano_x / pano_width, pano_y / pano_height, fov_h_deg=90.0, size=512,
                            camera_pitch=1.8)                       # the label's DB camera_pitch
    crop = render_view(pano, view, tilt)                            # (512, 512, 3) uint8

For torch, build the same geometry with :func:`sampling_grid` and hand it to ``grid_sample``
(docs/geometry.md has the normalization).
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from . import pose

#: Production and HF crops are 3:2 and 1440 px wide (the 720x480 labeling canvas at 2x).
DEFAULT_ASPECT = 1.5
DEFAULT_WIDTH = 1440

#: Beta for the label's ``camera_pitch`` database column (module docstring).
DEFAULT_BETA = 0.9


@dataclass(frozen=True)
class View:
    """A roll-free perspective camera on the pano sphere.

    ``yaw_deg``/``pitch_deg`` aim the optical axis (lon/lat, degrees, level frame); ``fov_h_deg`` is the
    angle across ``width`` pixels; the vertical angle follows from square pixels,
    ``tan(fov_v / 2) = tan(fov_h / 2) * height / width``.
    """
    yaw_deg: float
    pitch_deg: float
    fov_h_deg: float
    width: int
    height: int

    @property
    def fov_v_deg(self) -> float:
        t = math.tan(math.radians(self.fov_h_deg) / 2.0) * self.height / self.width
        return math.degrees(2.0 * math.atan(t))

    @property
    def focal_px(self) -> float:
        """Focal length in output pixels."""
        return 0.5 * self.width / math.tan(math.radians(self.fov_h_deg) / 2.0)


def wrap180(deg):
    """Wrap an angle to [-180, 180). (pano_pose.wrap_deg wraps to (-180, 180]; both name the same
    direction, and nothing here depends on which end 180 lands.)"""
    return (np.asarray(deg, dtype=float) + 180.0) % 360.0 - 180.0


def pano_px_to_lonlat(pano_x, pano_y, pano_width, pano_height):
    """Stored pano pixel -> (lon, lat) in degrees: pano_pose.bearing_elevation_from_pixel.
    Resolution-independent; lon wraps."""
    return pose.bearing_elevation_from_pixel(pano_x, pano_y, pano_width, pano_height)


def lonlat_to_pano_px(lon, lat, pano_width, pano_height):
    """Inverse of :func:`pano_px_to_lonlat` (continuous, x in [0, pano_width)):
    pano_pose.pixel_from_bearing_elevation."""
    return pose.pixel_from_bearing_elevation(lon, lat, pano_width, pano_height)


def _size(width, aspect) -> Tuple[int, int]:
    width = int(width)
    if width < 2:
        raise ValueError(f"width must be >= 2, got {width}")
    return width, max(1, int(round(width / float(aspect))))


def _check_fov(fov_h_deg):
    if not 0.0 < float(fov_h_deg) < 180.0:
        raise ValueError(f"fov_h_deg must be in (0, 180), got {fov_h_deg}")


def centered_view(pano_x, pano_y, pano_width, pano_height, fov_h_deg,
                  width=DEFAULT_WIDTH, aspect=DEFAULT_ASPECT) -> View:
    """A view looking straight at the label's stored pano point (level frame)."""
    _check_fov(fov_h_deg)
    lon, lat = pano_px_to_lonlat(pano_x, pano_y, pano_width, pano_height)
    w, h = _size(width, aspect)
    return View(float(lon), float(lat), float(fov_h_deg), w, h)


def _pose_angle(value) -> float:
    """A pose angle in degrees, with None and NaN (a NULL database column) meaning unknown, i.e. 0."""
    if value is None:
        return 0.0
    value = float(value)
    return 0.0 if math.isnan(value) else value


def tilt_matrix(camera_pitch, camera_roll=None, beta=DEFAULT_BETA) -> Optional[np.ndarray]:
    """The 3x3 rotation taking a level-frame RFU ray into the rig (stored image) frame,
    ``v_rig = R @ v_level``, or None for no tilt.

    ``R = pano_pose.rig_from_gravity(beta * camera_pitch, beta * camera_roll)``: pano_pose's measured sign
    (pitch > 0 = nose down, roll > 0 = left side up), with beta scaling the pose exactly as
    ``pano_pose.corrected_pixel`` does. To first order a level ray at bearing b lands at rig elevation
    ``lat + beta * (camera_pitch * cos b + camera_roll * sin b)``. Pinned: ``camera_pitch = 3``, beta 0.9
    puts a level ray at b = 0 at rig elevation +2.7 deg and at b = 180 at -2.7; ``camera_roll = 3`` puts
    one at b = 90 at +2.7.

    ``None`` or NaN for an angle means unknown and is taken as 0 (PS stores no roll for GSV labels).
    Returns None when both angles are 0/unknown, or beta is 0: the identity, so a caller can skip the
    rotation. Raises ValueError for a non-finite angle other than NaN, or a non-finite beta.
    """
    beta = float(beta)
    if not math.isfinite(beta):
        raise ValueError(f"beta must be finite, got {beta}")
    p, r = _pose_angle(camera_pitch), _pose_angle(camera_roll)
    if not (math.isfinite(p) and math.isfinite(r)):
        raise ValueError(f"camera_pitch/camera_roll must be finite, got {camera_pitch!r}, {camera_roll!r}")
    if beta == 0.0 or (p == 0.0 and r == 0.0):
        return None
    return np.asarray(pose.rig_from_gravity(beta * p, beta * r), dtype=np.float64)


def label_view(x_norm, y_norm, fov_h_deg=90.0, size=2048, camera_pitch=None, camera_roll=None,
               beta=DEFAULT_BETA) -> Tuple[View, Optional[np.ndarray]]:
    """The one call both consumers make: a square ``size`` x ``size`` view aimed at a label's normalized
    pano point, and the tilt matrix to render it with.

    ``x_norm = pano_x / pano_width``, ``y_norm = pano_y / pano_height``, both in [0, 1]; the view looks at
    ``lon = (x_norm - 0.5) * 360``, ``lat = (0.5 - y_norm) * 180``. ``camera_pitch``/``camera_roll`` are the
    pano's pose in degrees (the label's database columns; None for unknown) and ``beta`` the fraction of it
    applied (:func:`tilt_matrix`). -> ``(View, tilt or None)``; render with ``render_view(pano, view, tilt)``,
    or build a torch grid from ``sampling_grid(view, tilt, W, H)``.

    Example: a label at the pano's centre column, 20 deg below the horizon, on a pano with camera_pitch 3::

        view, tilt = label_view(0.5, 0.5 + 20 / 180, fov_h_deg=90, size=512, camera_pitch=3)
        # view == View(yaw_deg=0.0, pitch_deg=-20.0, fov_h_deg=90.0, width=512, height=512)
    """
    for name, value in (("x_norm", x_norm), ("y_norm", y_norm)):
        value = float(value)
        if not (math.isfinite(value) and 0.0 <= value <= 1.0):
            raise ValueError(f"{name} must be in [0, 1], got {value}")
    _check_fov(fov_h_deg)
    if int(size) != size:
        raise ValueError(f"size must be an integer, got {size!r}")
    view = centered_view(float(x_norm), float(y_norm), 1.0, 1.0, fov_h_deg, width=int(size), aspect=1.0)
    return view, tilt_matrix(camera_pitch, camera_roll, beta)


def _basis(yaw_deg: float, pitch_deg: float):
    """(forward, right, up) of a roll-free view, RFU unit vectors."""
    fwd = pose.direction_rfu(yaw_deg, pitch_deg)
    right = np.cross(fwd, [0.0, 0.0, 1.0])
    n = np.linalg.norm(right)
    if n < 1e-12:  # looking straight up or down: any horizontal right vector is valid
        yaw = math.radians(yaw_deg)
        right = np.array([math.cos(yaw), -math.sin(yaw), 0.0])
    else:
        right = right / n
    up = np.cross(right, fwd)
    return fwd, right, up


def view_rays(view: View) -> np.ndarray:
    """Unit ray (level frame, RFU) through every output pixel centre, shape (H, W, 3)."""
    fwd, right, up = _basis(view.yaw_deg, view.pitch_deg)
    f = view.focal_px
    a = (np.arange(view.width, dtype=np.float64) + 0.5 - view.width / 2.0) / f
    b = (view.height / 2.0 - (np.arange(view.height, dtype=np.float64) + 0.5)) / f
    d = fwd[None, None, :] + a[None, :, None] * right[None, None, :] + b[:, None, None] * up[None, None, :]
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


def rays_to_lonlat(d: np.ndarray, tilt: Optional[np.ndarray] = None):
    """Level-frame unit rays (..., 3) -> rig-frame (lon, lat) in degrees.

    pano_pose.bearing_elevation for unit vectors, written out because it runs over every output pixel
    (tests/test_geometry.py checks the two agree)."""
    d = np.asarray(d, dtype=np.float64)
    if tilt is not None:
        d = d @ tilt.T  # row-vector form of tilt @ d
    lon = np.degrees(np.arctan2(d[..., 0], d[..., 1]))
    lat = np.degrees(np.arcsin(np.clip(d[..., 2], -1.0, 1.0)))
    return lon, lat


def lonlat_to_ray(lon, lat) -> np.ndarray:
    """(lon, lat) in degrees -> RFU unit rays (..., 3): pano_pose.direction_rfu."""
    return pose.direction_rfu(lon, lat)


def project_lonlat(lon, lat, view: View, tilt: Optional[np.ndarray] = None):
    """Rig-frame (lon, lat) -> output pixel (x, y), continuous; NaN if behind the camera.

    Inverse of :func:`view_rays` + :func:`rays_to_lonlat` under the same ``tilt``."""
    d = lonlat_to_ray(lon, lat)
    if tilt is not None:
        d = d @ tilt  # row-vector form of tilt.T @ d: back to the level frame
    fwd, right, up = _basis(view.yaw_deg, view.pitch_deg)
    zc = d @ fwd
    with np.errstate(divide="ignore", invalid="ignore"):
        x = view.width / 2.0 + view.focal_px * (d @ right) / zc
        y = view.height / 2.0 - view.focal_px * (d @ up) / zc
    bad = zc <= 1e-9
    x = np.where(bad, np.nan, x)
    y = np.where(bad, np.nan, y)
    return (float(x), float(y)) if np.ndim(x) == 0 else (x, y)


def label_pixel(pano_x, pano_y, pano_width, pano_height, view: View,
                render_tilt: Optional[np.ndarray] = None, true_tilt: Optional[np.ndarray] = None):
    """Where the *clicked* point lands in a view rendered with ``render_tilt``, in output pixels.

    The stored ``pano_x``/``pano_y`` are the click in the level frame; its content sits in the rig-frame
    pano at that point rotated by ``true_tilt`` (the pano's :func:`tilt_matrix`, or None where no tilt is
    known), which is pano_pose.corrected_pixel's point. When ``render_tilt`` is the same rotation the click
    is where the level-frame view puts it (the centre of a :func:`centered_view`); otherwise it is off by the
    difference, which is what an untilted render of a tilted pano carries.

    Example: seattle-wa:9's stored point (bearing 121.9 deg) on a pano with ``camera_pitch = 3``, beta 1:
    a 30 deg 1440 px centred view rendered with no tilt shows the click about 74 px below and 31 px left of
    centre; rendered with the tilt, at the centre::

        v = centered_view(13740, 4754, 16384, 8192, 30)
        T = tilt_matrix(3, 0, beta=1.0)
        label_pixel(13740, 4754, 16384, 8192, v, None, T)   # ~(688.6, 553.7)
        label_pixel(13740, 4754, 16384, 8192, v, T, T)      # (720.0, 480.0)
    """
    lon, lat = pano_px_to_lonlat(pano_x, pano_y, pano_width, pano_height)
    ilon, ilat = rays_to_lonlat(lonlat_to_ray(lon, lat), true_tilt)
    return project_lonlat(ilon, ilat, view, render_tilt)


def sample_bilinear(img: np.ndarray, sx: np.ndarray, sy: np.ndarray) -> np.ndarray:
    """Bilinear sample of ``img`` (H, W[, C]) at continuous pixel coords (centre = i + 0.5).

    x wraps (the seam), y clamps to the edge rows (the poles do not wrap). uint8 input is rounded and
    clipped back to uint8; any other dtype is returned in its own dtype.
    """
    H, W = img.shape[:2]
    fx = np.asarray(sx, dtype=np.float64) - 0.5
    fy = np.clip(np.asarray(sy, dtype=np.float64) - 0.5, 0.0, H - 1.0)
    x0 = np.floor(fx)
    y0 = np.floor(fy)
    wx = (fx - x0)[..., None].astype(np.float32)
    wy = (fy - y0)[..., None].astype(np.float32)
    x0 = x0.astype(np.int64) % W
    x1 = (x0 + 1) % W
    y0 = y0.astype(np.int64)
    y1 = np.minimum(y0 + 1, H - 1)
    src = img if img.ndim == 3 else img[..., None]
    top = src[y0, x0].astype(np.float32) * (1 - wx) + src[y0, x1].astype(np.float32) * wx
    bot = src[y1, x0].astype(np.float32) * (1 - wx) + src[y1, x1].astype(np.float32) * wx
    out = top * (1 - wy) + bot * wy
    out = np.clip(np.rint(out), 0, 255).astype(img.dtype) if img.dtype == np.uint8 else out.astype(img.dtype)
    return out if img.ndim == 3 else out[..., 0]


def reduction_factor(pano_width: int, view: View) -> int:
    """Largest integer box-reduction of the pano that keeps it at least as sharp as the view.

    Compares the pano's angular resolution (``pano_width / 360`` px/deg) with the view's at its centre
    (``focal_px * pi / 180`` px/deg). Reducing by this factor before bilinear sampling is the anti-aliasing:
    without it a 90 deg 1440 px view of a 16384 px pano samples one source pixel in three.
    """
    src_ppd = pano_width / 360.0
    out_ppd = view.focal_px * math.pi / 180.0
    return max(1, int(math.floor(src_ppd / out_ppd)))


def sampling_grid(view: View, tilt: Optional[np.ndarray], pano_width, pano_height):
    """The continuous source pixel of every output pixel: ``(sx, sy)``, each (view.height, view.width)
    float64, pixel *i*'s centre at ``i + 0.5``, x in [0, pano_width). Exactly what :func:`render_view`
    samples, so a torch consumer can build a ``grid_sample`` grid from identical geometry
    (docs/geometry.md: ``gx = 2 * sx / W - 1``, ``gy = 2 * sy / H - 1`` with ``align_corners=False``; x does
    not wrap in ``grid_sample``, so pad the pano by a column on each side or wrap it yourself).
    """
    lon, lat = rays_to_lonlat(view_rays(view), tilt)
    return lonlat_to_pano_px(lon, lat, pano_width, pano_height)


def render_view(pano: np.ndarray, view: View, tilt: Optional[np.ndarray] = None) -> np.ndarray:
    """Render ``view`` from an equirect pano array (H, W[, C]), rig frame. The caller handles
    anti-aliasing (:func:`reduction_factor`); this samples whatever resolution it is given."""
    H, W = pano.shape[:2]
    sx, sy = sampling_grid(view, tilt, W, H)
    return sample_bilinear(pano, sx, sy)
