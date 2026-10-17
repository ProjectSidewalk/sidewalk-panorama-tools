"""Tests for sidewalk_panorama_tools.geometry, the installable gnomonic label view (docs/geometry.md), and for
the package around it (sidewalk_panorama_tools.pose, the version).

CPU, numpy only, network-free: every render is of a small synthetic equirect with a known pattern. Ported from
RampNet tests/test_crops.py at commit 4156847d12d3f4ccac6102f7770cd315d02791d0 where the geometry still
applies (marker at centre, round trips, the seam, resolution independence, FOV scaling, reduction_factor,
behind-camera NaN), plus what is new here: the tilt matrix against pano_pose's measured sign and beta, a
rig-frame synthetic pano built with pano_pose.corrected_pixel, sampling_grid against render_view, label_view's
validation, and a replay of RampNet's committed 200-row validation sample.
"""

import csv
import hashlib
import math
import os
import re

import numpy as np
import pytest

import pano_pose
import sidewalk_panorama_tools
from sidewalk_panorama_tools import geometry as g
from sidewalk_panorama_tools import pose

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# RampNet docs/data/crop_cutter/validation_sample.csv at the ported commit, byte for byte (.gitattributes keeps
# it from being normalised). 200 HF-validated CurbRamp labels; no user ids.
SAMPLE = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'rampnet_crop_cutter', 'validation_sample.csv')
SAMPLE_SHA256 = 'b4acbb4b6aa45e8d4eb5daaaec3dd089abaf7eb36624432de32737dc200e0b35'


# ----------------------------------------------------------------------------- helpers

def marker_pano(W, H, points, radius=3, color=(255, 0, 0)):
    """Mid-grey pano with a coloured square (wrapping in x) at each (px, py)."""
    img = np.full((H, W, 3), 128, np.uint8)
    for px, py in points:
        ys = np.arange(int(py) - radius, int(py) + radius + 1)
        ys = ys[(ys >= 0) & (ys < H)]
        xs = np.arange(int(px) - radius, int(px) + radius + 1) % W
        img[np.ix_(ys, xs)] = color
    return img


def red_centroid(arr):
    m = (arr[..., 0] > 200) & (arr[..., 1] < 60) & (arr[..., 2] < 60)
    assert m.any(), 'marker not found in crop'
    ys, xs = np.nonzero(m)
    return xs.mean() + 0.5, ys.mean() + 0.5


def rig_elevation(bearing, camera_pitch=None, camera_roll=None, beta=None):
    """Rig-frame elevation of a LEVEL ray at `bearing` on the horizon, through tilt_matrix (its default beta
    when `beta` is None)."""
    T = g.tilt_matrix(camera_pitch, camera_roll) if beta is None else g.tilt_matrix(camera_pitch, camera_roll, beta)
    return float(g.rays_to_lonlat(g.lonlat_to_ray(bearing, 0.0)[None, :], T)[1][0])


def rampnet_mm_matrix(camera_pitch, camera_roll):
    """RampNet crops.tilt_matrix(pitch, roll, "mm"), transcribed - deliberately NOT via pano_pose - in RampNet's
    own frame (+x right, +y up, +z forward), applied there as `d @ R` to a level ray."""
    p, r = math.radians(-camera_pitch), math.radians(-camera_roll)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, math.cos(p), math.sin(p)], [0.0, -math.sin(p), math.cos(p)]])
    rz = np.array([[math.cos(r), -math.sin(r), 0.0], [math.sin(r), math.cos(r), 0.0], [0.0, 0.0, 1.0]])
    return rz @ rx


def rampnet_image_lonlat(lon, lat, camera_pitch, camera_roll):
    lo, la = math.radians(lon), math.radians(lat)
    d = np.array([math.cos(la) * math.sin(lo), math.sin(la), math.cos(la) * math.cos(lo)]) @ \
        rampnet_mm_matrix(camera_pitch, camera_roll)
    return math.degrees(math.atan2(d[0], d[2])), math.degrees(math.asin(max(-1.0, min(1.0, d[1]))))


# ----------------------------------------------------------------------------- the package

def test_the_version_matches_pyproject():
    """A release is a two-file edit (docs/geometry.md): pyproject.toml's version and __version__ move together."""
    with open(os.path.join(REPO_ROOT, 'pyproject.toml'), encoding='utf-8') as f:
        declared = re.search(r'^version = "([^"]+)"$', f.read(), re.MULTILINE).group(1)
    assert sidewalk_panorama_tools.__version__ == declared
    assert re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', declared), declared


def test_pose_is_pano_pose_not_a_copy():
    """One definition: every name the package exports IS pano_pose's object, and every public pano_pose name
    is exported, so a function added there cannot go missing here."""
    for name in pose.__all__:
        assert getattr(pose, name) is getattr(pano_pose, name), name
    # Modules are imports, and so is a function or class whose __module__ is another module's. Every other
    # public value is pano_pose's own and must be exported: its functions and classes (__module__ 'pano_pose'),
    # and every constant whatever its type - a str has no __module__, but an np.float64 or a Path reports
    # its class's ('numpy', 'pathlib'), so constants are not filtered on __module__ at all.
    def defined_here(value):
        if isinstance(value, type(os)):
            return False
        if callable(value):
            return getattr(value, '__module__', None) in (None, 'pano_pose')
        return True
    public = {name for name, value in vars(pano_pose).items() if not name.startswith('_') and defined_here(value)}
    assert public == set(pose.__all__)


# ----------------------------------------------------------------------------- views

def test_view_size_and_vertical_fov():
    v = g.centered_view(100, 50, 400, 200, 60, width=300, aspect=1.5)
    assert (v.width, v.height) == (300, 200)
    assert math.tan(math.radians(v.fov_v_deg) / 2) == pytest.approx(math.tan(math.radians(30)) / 1.5)
    for bad in (0, 180, -5):
        with pytest.raises(ValueError):
            g.centered_view(100, 50, 400, 200, bad)
    with pytest.raises(ValueError):
        g.centered_view(100, 50, 400, 200, 60, width=1)


def test_wrap180_and_the_pixel_mapping_is_pano_poses():
    assert g.wrap180 is pose.wrap_deg                     # one wrap helper, closed at +180
    assert g.wrap180(180.0) == 180.0 and g.wrap180(-180.0) == 180.0 and g.wrap180(-190.0) == 170.0
    lon, lat = g.pano_px_to_lonlat(13740, 4754, 16384, 8192)
    assert (lon, lat) == pytest.approx((121.904296875, -14.4580078125))
    assert (lon, lat) == pose.bearing_elevation_from_pixel(13740, 4754, 16384, 8192)
    assert g.lonlat_to_pano_px(lon, lat, 16384, 8192) == pytest.approx((13740, 4754))
    # resolution independence of the mapping itself: the same normalized point at half the size
    assert g.pano_px_to_lonlat(6870, 2377, 8192, 4096) == pytest.approx((lon, lat))


def test_rays_to_lonlat_is_pano_pose_bearing_elevation():
    rng = np.random.default_rng(3)
    v = rng.normal(size=(50, 3))
    v /= np.linalg.norm(v, axis=-1, keepdims=True)
    lon, lat = g.rays_to_lonlat(v)
    b, el = pose.bearing_elevation(v)
    assert np.allclose(g.wrap180(lon - b), 0.0, atol=1e-9) and np.allclose(lat, el, atol=1e-9)
    assert np.allclose(g.lonlat_to_ray(lon, lat), v, atol=1e-12)


@pytest.mark.parametrize('px,py', [
    (1000, 600),     # ordinary, below the horizon
    (0, 620),        # on the seam
    (2047, 640),     # last column
    (2048, 640),     # pano_x == pano_width: the same place as column 0 (real rows do this)
    (5, 700),        # a few columns right of the seam: the view must wrap to the right edge
    (1500, 950),     # 76 deg below the horizon
])
def test_marker_lands_at_centre(px, py):
    W, H = 2048, 1024
    pano = marker_pano(W, H, [(px % W, py)])
    view = g.centered_view(px, py, W, H, fov_h_deg=10, width=120, aspect=1.5)
    cx, cy = red_centroid(g.render_view(pano, view))
    # the marker is 7 source px; its centroid is at the source pixel centre (px + 0.5), which is half a source
    # pixel off the stored point - about 1.5 output px at this magnification
    assert abs(cx - view.width / 2) < 2.5 and abs(cy - view.height / 2) < 2.5


def test_seam_crop_has_no_synthetic_black_and_uses_both_edges():
    W, H = 1024, 512
    x = np.arange(W)
    pano = np.zeros((H, W, 3), np.uint8)
    pano[:, :, 0] = np.where(x < W // 2, 200, 40)[None, :]      # left half: bright red channel
    pano[:, :, 1] = 100
    pano[:, :, 2] = np.where(x < W // 2, 40, 200)[None, :]      # right half: bright blue
    crop = g.render_view(pano, g.centered_view(0, 300, W, H, 40, width=200))
    assert crop.min() > 0
    left, right = crop[:, :90], crop[:, 110:]
    assert left[..., 2].mean() > 150 and right[..., 0].mean() > 150  # the right edge left of centre, and v.v.


def test_resolution_independence():
    """Same label, pano stored at 2x resolution: identical framing."""
    yy, xx = np.mgrid[0:256, 0:512]
    small = np.stack([128 + 100 * np.sin(xx / 9.0), 128 + 100 * np.cos(yy / 7.0),
                      128 + 100 * np.sin((xx + yy) / 13.0)], -1).astype(np.uint8)
    yy, xx = np.mgrid[0:512, 0:1024] / 2.0
    big = np.stack([128 + 100 * np.sin(xx / 9.0), 128 + 100 * np.cos(yy / 7.0),
                    128 + 100 * np.sin((xx + yy) / 13.0)], -1).astype(np.uint8)
    v = g.centered_view(300, 170, 512, 256, 30, width=90)
    a = g.render_view(small, v).astype(int)
    b = g.render_view(big, v).astype(int)
    assert np.abs(a - b).mean() < 3  # the same smooth texture sampled at two resolutions


@pytest.mark.parametrize('tilt', [None, (2.0, -1.5, 1.0), (2.0, -1.5, 0.9), (-3.0, 0.0, 0.88)])
def test_pixel_sphere_pixel_round_trip(tilt):
    view = g.View(yaw_deg=-170.0, pitch_deg=-25.0, fov_h_deg=70.0, width=64, height=40)
    T = None if tilt is None else g.tilt_matrix(*tilt)
    lon, lat = g.rays_to_lonlat(g.view_rays(view), T)
    x, y = g.project_lonlat(lon, lat, view, T)
    gx, gy = np.meshgrid(np.arange(64) + 0.5, np.arange(40) + 0.5)
    assert np.allclose(x, gx, atol=1e-7) and np.allclose(y, gy, atol=1e-7)


def test_label_projects_to_exact_centre():
    v = g.centered_view(13740, 4754, 16384, 8192, 60)
    lon, lat = g.pano_px_to_lonlat(13740, 4754, 16384, 8192)
    assert g.project_lonlat(lon, lat, v) == pytest.approx((720.0, 480.0), abs=1e-6)


def test_behind_camera_is_nan():
    v = g.View(0.0, 0.0, 60.0, 30, 20)
    x, y = g.project_lonlat(180.0, 0.0, v)
    assert math.isnan(x) and math.isnan(y)


def test_straight_down_view_is_well_defined():
    """At the nadir the view's 'right' falls back to the horizontal right of its yaw; nothing is NaN."""
    view, _ = g.label_view(0.3, 1.0, fov_h_deg=60.0, size=16)
    fwd, right, up = g._basis(view.yaw_deg, view.pitch_deg)
    assert np.allclose(fwd, [0, 0, -1], atol=1e-12)
    assert np.allclose(right, pose.direction_rfu(view.yaw_deg + 90.0, 0.0), atol=1e-12)
    assert np.allclose(np.cross(right, fwd), up)
    sx, sy = g.sampling_grid(view, None, 64, 32)
    assert np.isfinite(sx).all() and np.isfinite(sy).all()


@pytest.mark.parametrize('fov', [20.0, 45.0, 90.0])
def test_fov_scaling(fov):
    W, H = 4096, 2048
    lpx, lpy = 1000.0, 1300.0
    lon, lat = g.pano_px_to_lonlat(lpx, lpy, W, H)
    off = fov / 5.0   # a marker off to the right by a fifth of the FOV, same elevation
    mx, my = g.lonlat_to_pano_px(lon + off, lat, W, H)
    view = g.centered_view(lpx, lpy, W, H, fov, width=300)
    x, y = g.project_lonlat(lon + off, lat, view)
    d = g.lonlat_to_ray(lon + off, lat)
    fwd, right, up = g._basis(view.yaw_deg, view.pitch_deg)
    assert x - 150 == pytest.approx(view.focal_px * (d @ right) / (d @ fwd), abs=1e-6)
    # geometry, not anti-aliasing: at fov 90 the box reduction would dilute the marker below red_centroid's cut
    cx, cy = red_centroid(g.render_view(marker_pano(W, H, [(mx, my)], radius=4), view, antialias=False))
    assert abs(cx - x) < 1.5 + 4 * view.focal_px * math.pi / 180 * 360 / W  # within the marker size
    assert view.focal_px == pytest.approx(150 / math.tan(math.radians(fov / 2)))


def test_reduction_factor():
    assert g.reduction_factor(16384, g.View(0, 0, 90.0, 1440, 960)) == 3
    assert g.reduction_factor(16384, g.View(0, 0, 20.0, 1440, 960)) == 1
    assert g.reduction_factor(16384, g.View(0, 0, 90.0, 224, 149)) == 23
    assert g.View(0, 0, 90.0, 224, 149).reduction_factor(16384) == 23


def test_the_current_consumers_are_not_reduced():
    """The module docstring's claim: an 8192-wide pano and a 2048 px 90 deg label_view give k = 1, so the
    anti-aliasing changes nothing for sidewalk-validator-ai or sidewalk-ai-api."""
    view, _ = g.label_view(0.3, 0.6)
    assert (view.width, view.fov_h_deg) == (2048, 90.0)
    assert view.reduction_factor(8192) == 1
    assert view.reduction_factor(16384) == 2     # a 16384 pano would be: the reduction is not a no-op


def test_box_reduce_means_blocks_and_drops_the_trailing_remainder():
    img = np.arange(7 * 5, dtype=np.float64).reshape(7, 5)        # 7 rows, 5 cols; k = 2 drops row 6, col 4
    out = g.box_reduce(img, 2)
    assert out.shape == (3, 2) and out.dtype == np.float64
    assert out[0, 0] == pytest.approx((0 + 1 + 5 + 6) / 4) and out[2, 1] == pytest.approx((22 + 23 + 27 + 28) / 4)
    rgb = np.zeros((4, 4, 3), np.uint8)
    rgb[0, 0] = (255, 1, 2)
    red = g.box_reduce(rgb, 2)
    assert red.dtype == np.uint8 and red.shape == (2, 2, 3) and red[0, 0].tolist() == [64, 0, 0]  # rint(63.75)
    assert g.box_reduce(rgb, 1) is rgb
    for bad in (0, -1):
        with pytest.raises(ValueError):
            g.box_reduce(rgb, bad)
    with pytest.raises(ValueError):
        g.box_reduce(rgb, 5)


def test_render_view_antialiases_with_the_views_k():
    """render_view = sample the k-reduced pano at the full-resolution grid rescaled by (W//k)/W; antialias=False samples the
    full pano. On a one-pixel checkerboard the anti-aliased render is the flat mean, the other is noise."""
    W, H = 1024, 512
    yy, xx = np.mgrid[0:H, 0:W]
    pano = np.where((xx + yy) % 2 == 0, 255, 0).astype(np.uint8)[..., None].repeat(3, -1)
    view = g.View(30.0, -20.0, 90.0, 40, 40)
    k = view.reduction_factor(W)
    assert k > 1
    sx, sy = g.sampling_grid(view, None, W, H)
    assert np.array_equal(g.render_view(pano, view), g.sample_grid(pano, sx, sy, k))
    assert np.array_equal(g.render_view(pano, view, antialias=False), g.sample_bilinear(pano, sx, sy))
    assert np.abs(g.render_view(pano, view).astype(int) - 128).max() <= 1
    assert g.render_view(pano, view, antialias=False).astype(float).std() > 20


def test_the_reduced_seam_is_continuous_when_k_does_not_divide_w():
    """W = 8192, k = 11 drops 8192 % 11 = 8 columns (more than k / 2). The reduced pano is sampled at
    sx * (W // k) / W, so its wrap period is its own width: approaching the seam from either side gives the
    same value, and the far edge (sx = W - 0.5) is a blend of the reduced pano's last and first columns."""
    W, H, k = 8192, 44, 11
    assert W % k > k / 2
    rng = np.random.default_rng(21)
    pano = rng.uniform(0, 255, (H, W))
    sy = np.full(4, 20.0)
    left, right, edge, start = g.sample_grid(pano, np.array([W - 1e-9, 1e-9, W - 0.5, 0.0]), sy, k)
    assert left == pytest.approx(right, abs=1e-6)
    # sx = W - 0.5 and sx = 0 sit between the reduced pano's last and first columns (rows 1-2 at sy = 20),
    # never on a column the wrap skipped or read twice
    reduced = g.box_reduce(pano, k)
    around = reduced[1:3][:, [-1, 0]]
    assert around.min() <= edge <= around.max() and around.min() <= start <= around.max()
    # and an interior point is the ordinary bilinear sample of the reduced pano at the rescaled coordinate
    Wr, Hr = W // k, H // k
    mid = g.sample_grid(pano, np.array([4000.3]), np.array([17.7]), k)[0]
    assert mid == pytest.approx(g.sample_bilinear(reduced, np.array([4000.3 * Wr / W]),
                                                  np.array([17.7 * Hr / H]))[0], abs=1e-9)
    with pytest.raises(ValueError):
        g.sample_grid(pano, sy, sy, 0)


def test_an_integer_pano_is_rounded_once():
    """render_view reduces in float and rounds at the end: the uint8 render is exactly the rounded float
    render of the same values (box_reduce's own uint8 output would round twice)."""
    rng = np.random.default_rng(22)
    pano = rng.integers(0, 256, (512, 1024, 3), dtype=np.uint8)
    view = g.View(10.0, -30.0, 90.0, 64, 64)
    k = view.reduction_factor(1024)
    assert k > 1
    once = g.render_view(pano, view)
    assert once.dtype == np.uint8
    assert np.array_equal(once, np.clip(np.rint(g.render_view(pano.astype(np.float32), view)), 0, 255))


def test_pose_refuses_a_stray_pano_pose(monkeypatch):
    """pano_pose installs under a generic name; a different pano_pose first on sys.path must fail loudly."""
    import importlib
    import sys
    import types
    assert pose.PANO_POSE_FINGERPRINT == pose.EXPECTED_PANO_POSE_FINGERPRINT == pano_pose.PANO_POSE_FINGERPRINT
    monkeypatch.setitem(sys.modules, 'pano_pose', types.ModuleType('pano_pose'))
    monkeypatch.delitem(sys.modules, 'sidewalk_panorama_tools.pose')
    with pytest.raises(ImportError, match='shadowing'):
        importlib.import_module('sidewalk_panorama_tools.pose')
    monkeypatch.undo()
    assert importlib.import_module('sidewalk_panorama_tools.pose') is pose


def test_render_view_antialias_keeps_the_label_at_centre():
    """Dividing the grid by k keeps the geometry: a block-aligned marker still lands at the centre."""
    W, H = 4096, 2048
    px, py = 1000.0, 1300.0
    pano = marker_pano(W, H, [(px, py)], radius=8)
    view = g.centered_view(px, py, W, H, 90.0, width=120)
    assert view.reduction_factor(W) > 1
    crop = g.render_view(pano, view)
    m = crop[..., 0].astype(int) - crop[..., 1].astype(int)
    ys, xs = np.nonzero(m > 40)
    assert abs(xs.mean() + 0.5 - 60) < 1.5 and abs(ys.mean() + 0.5 - 40) < 1.5


def test_sample_bilinear_refuses_non_finite_coordinates():
    img = np.zeros((4, 8, 3), np.uint8)
    for sx, sy in ((np.nan, 1.0), (1.0, np.inf), (-np.inf, 1.0)):
        with pytest.raises(ValueError):
            g.sample_bilinear(img, np.array([sx]), np.array([sy]))


def test_sample_bilinear_rounds_every_integer_dtype_and_keeps_float64_precision():
    row = np.array([[0, 3]], dtype=np.uint16)
    assert g.sample_bilinear(row, np.array([1.0]), np.array([0.5])).tolist() == [2]        # rint(1.5) = 2
    neg = np.array([[-5, -2]], dtype=np.int16)
    assert g.sample_bilinear(neg, np.array([0.75]), np.array([0.5])).tolist() == [-4]      # rint(-4.25)
    big = np.array([[2 ** 31 - 2, 2 ** 31 - 1]], dtype=np.int32)
    out = g.sample_bilinear(big, np.array([1.0]), np.array([0.5]))
    assert out.dtype == np.int32 and out.tolist() == [2 ** 31 - 2]   # rint(2**31 - 1.5): float64 math, no overflow
    fine = np.array([[1.0, 1.0 + 1e-12]], dtype=np.float64)
    got = g.sample_bilinear(fine, np.array([1.0]), np.array([0.5]))[0]
    assert got.dtype == np.float64 and got == pytest.approx(1.0 + 0.5e-12, abs=1e-15)   # float32 would lose it


def test_sample_bilinear_keeps_shape_and_dtype():
    img = np.arange(12, dtype=np.float32).reshape(3, 4)
    out = g.sample_bilinear(img, np.array([[0.5, 1.0]]), np.array([[0.5, 0.5]]))
    assert out.dtype == np.float32 and out.shape == (1, 2)
    assert out.tolist() == [[0.0, 0.5]]
    # x wraps: 3.75 is a quarter of the way from the last column's centre (value 3) to column 0's (value 0)
    assert g.sample_bilinear(img, np.array([3.75]), np.array([0.5]))[0] == pytest.approx(0.75 * 3 + 0.25 * 0)


# ----------------------------------------------------------------------------- tilt

class TestTiltMatrixIsPanoPosesSign:
    """tilt_matrix is pano_pose.rig_from_gravity at beta times the pose, so its sign is the MEASURED one
    (pano_pose: pitch > 0 = nose down, roll > 0 = left side up; a level ray at bearing b sits at rig
    elevation +T(b)). Signed values, so a flipped convention fails."""

    def test_the_pinned_numbers(self):
        """At the default beta, 0.95 (the label's camera_pitch is streetlevel's modern pose record)."""
        assert g.DEFAULT_BETA == 0.95
        assert rig_elevation(0.0, 3.0) == pytest.approx(2.85, abs=0.01)
        assert rig_elevation(180.0, 3.0) == pytest.approx(-2.85, abs=0.01)
        assert rig_elevation(90.0, None, 3.0) == pytest.approx(2.85, abs=0.01)
        assert rig_elevation(-90.0, None, 3.0) == pytest.approx(-2.85, abs=0.01)
        assert rig_elevation(90.0, 3.0) == pytest.approx(0.0, abs=1e-9)   # pitch is inert at the side
        # and an explicit beta is honoured
        assert rig_elevation(0.0, 3.0, beta=0.9) == pytest.approx(2.7, abs=0.01)
        assert rig_elevation(90.0, None, 3.0, beta=0.9) == pytest.approx(2.7, abs=0.01)

    def test_first_order_is_beta_times_pano_poses_T(self):
        for b in (-150.0, -40.0, 0.0, 33.0, 121.9):
            for beta in (0.88, 0.9, 0.95, 1.0):
                T = pose.tilt_term_deg(b, 1.0, 0.5)
                assert rig_elevation(b, 1.0, 0.5, beta) == pytest.approx(beta * T, abs=0.01)

    def test_it_is_rig_from_gravity_at_the_scaled_pose_and_moves_a_point_to_corrected_pixel(self):
        assert np.array_equal(g.tilt_matrix(2.0, -1.0, 0.9), pose.rig_from_gravity(1.8, -0.9))
        W, H = 16384, 8192
        for px, py in ((13740, 4754), (0, 5000), (8000, 3000)):
            for beta in (0.88, 0.95):
                T = g.tilt_matrix(2.5, -1.2, beta)
                lon, lat = g.pano_px_to_lonlat(px, py, W, H)
                ilon, ilat = g.rays_to_lonlat(g.lonlat_to_ray(lon, lat), T)
                got = g.lonlat_to_pano_px(ilon, ilat, W, H)
                want = pose.corrected_pixel(px, py, W, H, 2.5, -1.2, beta)
                assert (got[0] - want[0] + W / 2) % W - W / 2 == pytest.approx(0.0, abs=1e-6)
                assert got[1] == pytest.approx(want[1], abs=1e-6)

    def test_it_agrees_with_rampnets_viewer_tilt(self):
        """RampNet's VIEWER_TILT = "mm" (image_lat = level_lat + camera_pitch cos phi + camera_roll sin phi) is
        the same relation as pano_pose's +T(b) at beta 1. RampNet's HF-crop validation confirms the PITCH sign
        independently; its roll sign is untested (its own docstring: no GSV row carries a roll), so the roll
        sign here rests on #54 endpoint F1, and this test only shows the two formulas agree. With one axis (every GSV label: PS stores no roll) the two rotations are identical. With both,
        they differ only in composition order (pano_pose pitches then rolls about the pitched forward axis;
        RampNet's Rz @ Rx the other way round), which is a rotation about the vertical of about
        pitch * roll (radians): elevation agrees to 0.01 deg, and the direction to within that bound."""
        rng = np.random.default_rng(54)
        for pitch, roll in ((3.0, 0.0), (0.0, 3.0), (-2.0, 0.0), (2.0, -1.5), (-2.5, 2.5)):
            T = g.tilt_matrix(pitch, roll, beta=1.0)
            bound = math.degrees(abs(math.radians(pitch) * math.radians(roll))) * 1.05
            for lon, lat in zip(rng.uniform(-180, 180, 40), rng.uniform(-60, 60, 40)):
                ours = g.lonlat_to_ray(*g.rays_to_lonlat(g.lonlat_to_ray(lon, lat), T))
                tlon, tlat = rampnet_image_lonlat(lon, lat, pitch, roll)
                theirs = g.lonlat_to_ray(tlon, tlat)
                angle = math.degrees(math.atan2(np.linalg.norm(np.cross(ours, theirs)), float(ours @ theirs)))
                if pitch == 0.0 or roll == 0.0:
                    assert angle < 1e-9
                else:
                    assert angle <= bound
                assert float(g.rays_to_lonlat(ours)[1]) == pytest.approx(tlat, abs=0.01)
        # and RampNet's own pinned values, through ours
        assert rig_elevation(0.0, 3.0, beta=1.0) == pytest.approx(3.0, abs=1e-9)
        assert rig_elevation(180.0, 3.0, beta=1.0) == pytest.approx(-3.0, abs=1e-9)
        assert rig_elevation(90.0, 0.0, 2.0, beta=1.0) == pytest.approx(2.0, abs=1e-9)
        assert rig_elevation(-90.0, 0.0, 2.0, beta=1.0) == pytest.approx(-2.0, abs=1e-9)

    @pytest.mark.parametrize('pitch,roll,beta', [(None, None, 0.9), (0, 0, 0.9), (0.0, None, 0.9),
                                                 (float('nan'), None, 0.9), (None, float('nan'), 0.9),
                                                 (3.0, 1.0, 0.0)])
    def test_no_pose_or_beta_zero_is_no_tilt(self, pitch, roll, beta):
        assert g.tilt_matrix(pitch, roll, beta) is None

    def test_one_unknown_axis_is_zero_not_no_tilt(self):
        assert np.array_equal(g.tilt_matrix(3.0, float('nan')), g.tilt_matrix(3.0, 0.0))
        assert np.array_equal(g.tilt_matrix(None, 2.0), g.tilt_matrix(0.0, 2.0))

    @pytest.mark.parametrize('pitch,roll,beta', [(float('inf'), None, 0.9), (1.0, float('-inf'), 0.9),
                                                 (1.0, None, float('nan')), (1.0, None, float('inf'))])
    def test_non_finite_is_refused(self, pitch, roll, beta):
        with pytest.raises(ValueError):
            g.tilt_matrix(pitch, roll, beta)


def test_a_rig_frame_pano_is_centred_only_by_the_tilted_render():
    """The synthetic pano is built the way a stored GSV pano is (rig frame): the label is stored at (px, py) in
    the level frame, and its marker is drawn at the rig pixel pano_pose.corrected_pixel gives for
    camera_pitch 3 and beta 0.9 - the pixel CropRunner's --tilt-correction would centre on. A centred view
    rendered with tilt_matrix(3, beta=0.9) shows the marker at the centre; rendered untilted, it does not, and
    the marker is where label_pixel says the click lands."""
    W, H = 4096, 2048
    px, py, pitch, beta = 1400.0, 1250.0, 3.0, 0.9      # bearing -56.95 deg, 20 deg below the horizon
    mx, my = pose.corrected_pixel(px, py, W, H, pitch, 0.0, beta)
    pano = marker_pano(W, H, [(mx, my)], radius=2)
    view = g.centered_view(px, py, W, H, 20, width=240)
    T = g.tilt_matrix(pitch, None, beta)
    cx, cy = red_centroid(g.render_view(pano, view, T))
    assert abs(cx - 120) < 2 and abs(cy - 80) < 2
    assert g.label_pixel(px, py, W, H, view, T, T) == pytest.approx((120.0, 80.0), abs=1e-6)
    ex, ey = g.label_pixel(px, py, W, H, view, None, T)
    assert math.hypot(ex - 120, ey - 80) > 12       # 0.9 * 3 cos(57) = 1.47 deg, about 17 px at this focal
    cx, cy = red_centroid(g.render_view(pano, view))
    assert math.hypot(cx - 120, cy - 80) > 12
    assert abs(cx - ex) < 2 and abs(cy - ey) < 2


def test_label_pixel_docstring_example():
    v = g.centered_view(13740, 4754, 16384, 8192, 30)
    T = g.tilt_matrix(3, 0, beta=1.0)
    assert g.label_pixel(13740, 4754, 16384, 8192, v, None, T) == pytest.approx((688.6, 553.7), abs=0.1)
    assert g.label_pixel(13740, 4754, 16384, 8192, v, T, T) == pytest.approx((720.0, 480.0), abs=1e-6)


# ----------------------------------------------------------------------------- sampling_grid

@pytest.mark.parametrize('tilt', [None, (3.0, -1.0, 0.9)])
def test_sampling_grid_reproduces_render_view_exactly(tilt):
    rng = np.random.default_rng(7)
    pano = rng.integers(0, 256, (128, 256, 3), dtype=np.uint8)
    view, T = g.label_view(0.02, 0.6, fov_h_deg=50.0, size=48,
                           camera_pitch=None if tilt is None else tilt[0],
                           camera_roll=None if tilt is None else tilt[1])
    sx, sy = g.sampling_grid(view, T, 256, 128)
    assert sx.shape == sy.shape == (48, 48) and sx.dtype == np.float64
    assert sx.min() >= 0.0 and sx.max() < 256.0
    assert np.array_equal(g.sample_bilinear(pano, sx, sy), g.render_view(pano, view, T))
    # the seam is in this view (x_norm 0.02), so the grid must cover both edges
    assert sx.min() < 10 and sx.max() > 246


# ----------------------------------------------------------------------------- label_view

def test_label_view_defaults():
    view, tilt = g.label_view(0.5, 0.5)
    assert view == g.View(0.0, 0.0, 90.0, 2048, 2048) and tilt is None
    view, tilt = g.label_view(0.75, 0.5 + 20 / 180, fov_h_deg=60.0, size=512, camera_pitch=3.0)
    assert (view.yaw_deg, view.pitch_deg) == pytest.approx((90.0, -20.0))
    assert (view.width, view.height, view.fov_h_deg) == (512, 512, 60.0)
    assert np.array_equal(tilt, g.tilt_matrix(3.0, None, 0.95))
    assert np.array_equal(g.label_view(0.5, 0.5, camera_pitch=3.0, beta=0.95)[1], g.tilt_matrix(3.0, None, 0.95))


@pytest.mark.parametrize('kwargs', [dict(x_norm=float('-inf'), y_norm=0.5),
                                    dict(x_norm=0.5, y_norm=-1e-9), dict(x_norm=0.5, y_norm=1.5),
                                    dict(x_norm=float('nan'), y_norm=0.5), dict(x_norm=0.5, y_norm=float('inf')),
                                    dict(x_norm=0.5, y_norm=0.5, fov_h_deg=0.0),
                                    dict(x_norm=0.5, y_norm=0.5, fov_h_deg=180.0),
                                    dict(x_norm=0.5, y_norm=0.5, fov_h_deg=-30.0),
                                    dict(x_norm=0.5, y_norm=0.5, size=1),
                                    dict(x_norm=0.5, y_norm=0.5, size=64.5)])
def test_label_view_refuses_bad_input(kwargs):
    with pytest.raises(ValueError):
        g.label_view(**kwargs)


def test_label_view_wraps_x_modulo_one():
    """pano_x is periodic: real rows carry pano_x == pano_width and a pixel past it."""
    assert g.label_view(1.0, 0.5)[0] == g.label_view(0.0, 0.5)[0]
    assert g.label_view(1.0 + 1 / 16384, 0.6)[0].yaw_deg == pytest.approx(g.label_view(1 / 16384, 0.6)[0].yaw_deg)
    assert g.label_view(-0.25, 0.5)[0].yaw_deg == pytest.approx(90.0)
    assert g.label_view(1.25, 0.5)[0].yaw_deg == pytest.approx(-90.0)


@pytest.mark.parametrize('x,y', [(0.0, 0.5), (1.0, 0.5), (0.5, 0.0), (0.5, 1.0)])
def test_label_view_accepts_the_closed_range(x, y):
    view, _ = g.label_view(x, y, size=8)
    assert math.isfinite(view.yaw_deg) and math.isfinite(view.pitch_deg)


# ----------------------------------------------------------------------------- RampNet's committed sample

def _sample_rows():
    with open(SAMPLE, 'rb') as f:
        assert hashlib.sha256(f.read()).hexdigest() == SAMPLE_SHA256
    with open(SAMPLE, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def test_the_committed_sample_replays_into_centred_views():
    """Every real row of RampNet's 200-label validation sample (its test_committed_sample_replays_into_its_
    viewport reads the same file): the stored point is at the exact centre of a centred view and of a
    label_view built from the normalized point; and, for every row with a camera_pitch, the rig pixel
    pano_pose.corrected_pixel gives at the default beta - an independent route to where the content sits -
    projects to the exact centre of the TILTED label_view, and not to the centre of the untilted one."""
    rows = _sample_rows()
    assert len(rows) == 200
    tilted = 0
    for r in rows:
        px, py = float(r['pano_x']), float(r['pano_y'])
        w, h = float(r['pano_width']), float(r['pano_height'])
        view = g.centered_view(px, py, w, h, 60.0)
        assert g.label_pixel(px, py, w, h, view) == pytest.approx((720.0, 480.0), abs=1e-6)
        lview, T = g.label_view(px / w, py / h, fov_h_deg=90.0, size=2048,
                                camera_pitch=float(r['camera_pitch']) if r['camera_pitch'] else None,
                                camera_roll=float(r['camera_roll']) if r['camera_roll'] else None)
        assert g.label_pixel(px, py, w, h, lview) == pytest.approx((1024.0, 1024.0), abs=1e-6)
        if T is None:
            continue
        rx, ry = pose.corrected_pixel(px, py, w, h, float(r['camera_pitch']),
                                      float(r['camera_roll']) if r['camera_roll'] else 0.0, g.DEFAULT_BETA)
        rlon, rlat = g.pano_px_to_lonlat(rx, ry, w, h)
        assert g.project_lonlat(rlon, rlat, lview, T) == pytest.approx((1024.0, 1024.0), abs=1e-6)
        moved = math.hypot(*np.subtract(g.project_lonlat(rlon, rlat, lview), (1024.0, 1024.0)))
        tilted += moved > 1.0
    assert tilted > 150   # most rows' tilt moves the content by more than an output pixel
