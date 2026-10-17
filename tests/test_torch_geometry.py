"""Tests for sidewalk_panorama_tools.torch_geometry, the optional torch sampling grid (docs/geometry.md).

torch is an optional extra of the package, so this whole module skips cleanly without it. CI has it:
requirements.txt pulls torch in through streetlevel -> pyequilib, so the gated job runs these tests and
measures torch_geometry.py, and the `package` job reruns them on the CPU-only torch build. With torch it checks the
torch grid against the numpy one over seven label points - both sides of the seam, the centre, +-72 deg and
+-86 deg elevation - with and without tilt, in float32 and float64, and checks the wrap-padded grid_sample
normalization against the padded tensor's columns and against geometry.render_view.
"""

import math

import numpy as np
import pytest

from sidewalk_panorama_tools import geometry as g

torch = pytest.importorskip('torch')
F = pytest.importorskip('torch.nn.functional')

from sidewalk_panorama_tools import torch_geometry as tg  # noqa: E402

W, H = 8192, 4096   # the production pano width the two consumers use

# (x_norm, y_norm): seam both sides, centre, +-72 deg and +-86 deg elevation
POINTS = [(0.001, 0.55), (0.999, 0.55), (0.5, 0.5),
          (0.3, 0.5 - 72 / 180), (0.7, 0.5 + 72 / 180),
          (0.15, 0.5 - 86 / 180), (0.85, 0.5 + 86 / 180)]

# float64 is the numpy computation in another library: 1e-4 px on both axes. float32 cannot reach that: its
# spacing near x = 8192 is about 5e-4 px, and towards the poles a longitude error is magnified in x by
# 1/cos(lat), where a whole column is a sliver of the sphere. So float32 is held to 2e-3 px in y and in
# x times cos(lat) - the error as arc length on the sphere, in pano pixels. Measured: 1.0-1.05e-3 px of arc at
# ordinary latitudes (#221 review; 9.3e-4 px over these points, 2026-10-17), and a raw x error of 0.03-0.08 px
# within 0.05 deg of the pole.
TOLERANCE_PX = {'float64': 1e-4, 'float32': 2e-3}


def _x_error(a, b):
    """Pixel distance in x, the seam taken into account (x is periodic)."""
    return np.abs((a - b + W / 2) % W - W / 2)


@pytest.mark.parametrize('dtype', ['float64', 'float32'])
@pytest.mark.parametrize('pitch,roll', [(None, None), (3.0, None), (2.0, -1.5)])
@pytest.mark.parametrize('point', POINTS, ids=[f'x{x:.3f}_y{y:.3f}' for x, y in POINTS])
def test_the_torch_grid_matches_numpys(point, pitch, roll, dtype):
    view, tilt = g.label_view(*point, fov_h_deg=90.0, size=96, camera_pitch=pitch, camera_roll=roll)
    sx, sy = g.sampling_grid(view, tilt, W, H)
    tx, ty = tg.sampling_grid_torch(view, tilt, W, H, dtype=getattr(torch, dtype))
    assert tx.shape == ty.shape == (96, 96) and tx.dtype == getattr(torch, dtype)
    tx, ty = tx.double().numpy(), ty.double().numpy()
    assert tx.min() >= 0.0 and tx.max() < W
    x_error = _x_error(tx, sx)
    if dtype == 'float32':
        x_error = x_error * np.cos((0.5 - sy / H) * np.pi)
    assert x_error.max() < TOLERANCE_PX[dtype]
    assert np.abs(ty - sy).max() < TOLERANCE_PX[dtype]


def test_float32_is_the_default():
    view, tilt = g.label_view(0.5, 0.5, size=4)
    assert tg.sampling_grid_torch(view, tilt, W, H)[0].dtype == torch.float32


def test_the_normalized_grid_lands_on_the_padded_columns():
    """sx = 0.5 is column 0's centre, which is column 1 of the padded tensor; sx = W - 0.5 is column W."""
    w, h = 16, 8
    pano = torch.arange(w, dtype=torch.float64).repeat(h, 1)[None, None]       # value = column index
    padded = tg.wrap_pad_torch(pano)
    assert padded.shape[-1] == w + 2
    assert padded[0, 0, 0, 0] == w - 1 and padded[0, 0, 0, -1] == 0           # the wrap columns
    sx = torch.tensor([[0.5, w - 0.5, 0.0, 3.25]], dtype=torch.float64)
    sy = torch.full_like(sx, 4.5)
    grid = tg.normalized_grid_torch(sx, sy, w, h)
    assert grid.shape == (1, 1, 4, 2)
    gx = grid[0, 0, :, 0]
    # align_corners=False: padded column i's centre is at gx = 2 (i + 0.5) / (w + 2) - 1
    assert gx[0].item() == pytest.approx(2 * 1.5 / (w + 2) - 1)
    assert gx[1].item() == pytest.approx(2 * (w + 0.5) / (w + 2) - 1)
    out = F.grid_sample(padded, grid, mode='bilinear', padding_mode='border', align_corners=False)[0, 0, 0]
    # column 0, column w - 1, the seam (half way between column w - 1 and column 0), and a quarter past 2
    assert out.tolist() == pytest.approx([0.0, w - 1, (w - 1) / 2, 2.75])


@pytest.mark.parametrize('pitch', [None, 3.0])
def test_grid_sample_on_the_padded_pano_reproduces_render_view(pitch):
    """The whole recipe of the module docstring, at k = 1, against the numpy render, across the seam."""
    w, h = 512, 256
    rng = np.random.default_rng(11)
    pano = rng.uniform(0, 255, (h, w, 3))
    view, tilt = g.label_view(0.995, 0.6, fov_h_deg=40.0, size=48, camera_pitch=pitch)
    assert view.reduction_factor(w) == 1
    want = g.render_view(pano, view, tilt)
    t = torch.as_tensor(pano).permute(2, 0, 1)[None]
    sx, sy = tg.sampling_grid_torch(view, tilt, w, h, dtype=torch.float64)
    got = F.grid_sample(tg.wrap_pad_torch(t), tg.normalized_grid_torch(sx, sy, w, h), mode='bilinear',
                        padding_mode='border', align_corners=False)[0].permute(1, 2, 0).numpy()
    assert np.abs(got - want).max() < 1e-6


def test_the_recipe_with_a_box_reduction_matches_render_view():
    """k > 1: avg_pool2d by k, the grid divided by k and normalized against the reduced size, is render_view."""
    w, h = 1024, 512
    rng = np.random.default_rng(12)
    pano = rng.uniform(0, 255, (h, w, 3))
    view, tilt = g.label_view(0.4, 0.62, fov_h_deg=90.0, size=40, camera_pitch=2.0)
    k = view.reduction_factor(w)
    assert k > 1
    want = g.render_view(pano, view, tilt)
    t = F.avg_pool2d(torch.as_tensor(pano).permute(2, 0, 1)[None], k)
    sx, sy = tg.sampling_grid_torch(view, tilt, w, h, dtype=torch.float64)
    got = F.grid_sample(tg.wrap_pad_torch(t), tg.normalized_grid_torch(sx, sy, w, h, k=k),
                        mode='bilinear', padding_mode='border', align_corners=False)[0].permute(1, 2, 0).numpy()
    assert np.abs(got - want).max() < 1e-6


def test_a_tilt_matrix_is_used_not_ignored():
    view, tilt = g.label_view(0.5, 0.5, size=9, camera_pitch=3.0)
    flat = tg.sampling_grid_torch(view, None, W, H, dtype=torch.float64)[1]
    tilted = tg.sampling_grid_torch(view, tilt, W, H, dtype=torch.float64)[1]
    # 0.95 * 3 deg of nose-down pitch puts the level view's centre (pixel 4 of 9) 2.85 deg higher in the rig
    shift = (flat - tilted)[4, 4].item()
    assert shift == pytest.approx(2.85 / 180 * H, rel=0.01) and not math.isnan(shift)


def _torch_sample(pano, sx, sy, k, dtype=torch.float64):
    """The docs recipe on an (H, W[, C]) numpy pano at full-resolution coordinates sx, sy (any shape)."""
    h, w = pano.shape[:2]
    t = torch.as_tensor(pano, dtype=dtype)
    t = (t[..., None] if t.ndim == 2 else t).permute(2, 0, 1)[None]
    if k > 1:
        t = F.avg_pool2d(t, k)
    sx_t = torch.as_tensor(np.atleast_2d(sx), dtype=dtype)
    sy_t = torch.as_tensor(np.atleast_2d(sy), dtype=dtype)
    out = F.grid_sample(tg.wrap_pad_torch(t), tg.normalized_grid_torch(sx_t, sy_t, w, h, k=k), mode='bilinear',
                        padding_mode='border', align_corners=False)[0]
    return out.permute(1, 2, 0)


def test_numpy_and_torch_agree_at_the_seam_when_k_does_not_divide_w():
    """W = 8192, k = 11 (8 dropped columns, more than k / 2): sampled directly at W - 0.5 and across the seam,
    numpy's sample_grid and the torch recipe agree, and both are continuous across sx = W == 0."""
    W, H, k = 8192, 44, 11
    rng = np.random.default_rng(23)
    pano = rng.uniform(0, 255, (H, W))
    sx = np.array([W - 0.5, W - 1e-9, 1e-9, 0.5, W - 30.0, 4000.3])
    sy = np.array([20.0, 20.0, 20.0, 20.0, 3.0, 40.0])
    want = g.sample_grid(pano, sx, sy, k)
    got = _torch_sample(pano, sx, sy, k).numpy()[0, :, 0]
    assert np.abs(got - want).max() < 1e-9
    assert got[1] == pytest.approx(got[2], abs=1e-6)


def test_a_uint8_pano_at_k2_matches_numpy_to_one_lsb():
    """Single rounding on both sides: numpy reduces in float and rounds once; the torch recipe ends with
    .round().clamp(0, 255).to(torch.uint8). Run in float32 (as a GPU would), they differ by at most one LSB
    and only where float32 lands within rounding of a half - not the 16.6% the double-rounded numpy path
    disagreed on before the #221 review."""
    rng = np.random.default_rng(24)
    pano = rng.integers(0, 256, (512, 1024, 3), dtype=np.uint8)
    view, tilt = g.label_view(0.3, 0.6, fov_h_deg=90.0, size=128, camera_pitch=2.0)
    k = view.reduction_factor(1024)
    assert k == 2
    want = g.render_view(pano, view, tilt).astype(int)
    sx, sy = g.sampling_grid(view, tilt, 1024, 512)
    got = _torch_sample(pano, sx, sy, k, dtype=torch.float32).round().clamp(0, 255).to(torch.uint8).numpy()
    diff = np.abs(got.astype(int) - want)
    assert diff.max() <= 1
    assert (diff > 0).mean() < 0.01, (diff > 0).mean()


def test_the_normalized_grid_refuses_a_bad_k():
    with pytest.raises(ValueError):
        tg.normalized_grid_torch(torch.zeros(1, 1), torch.zeros(1, 1), 16, 8, k=0)


@pytest.mark.parametrize('dtype', ['float64', 'float32'])
def test_sampling_grid_torch_x_is_in_zero_to_w(dtype):
    """x is wrapped into [0, W) like the numpy grid's - including a view straddling the seam and one at the
    nadir, where every bearing appears - so normalized_grid_torch never sees a coordinate past the pad."""
    for point in ((0.0, 0.6), (0.9999, 0.5), (0.4, 1.0)):
        view, tilt = g.label_view(*point, fov_h_deg=120.0, size=64, camera_pitch=3.0)
        sx, _ = tg.sampling_grid_torch(view, tilt, W, H, dtype=getattr(torch, dtype))
        assert sx.min().item() >= 0.0 and sx.max().item() < W
