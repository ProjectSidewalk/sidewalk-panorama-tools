"""Tests for sidewalk_panorama_tools.torch_geometry, the optional torch sampling grid (docs/geometry.md).

torch is an optional extra and CI does not install it, so this whole module skips cleanly without it (and
.coveragerc omits torch_geometry.py from the gate for the same reason). With torch installed it checks the
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
# 1/cos(lat) (14x at 86 deg), where a whole column is a sliver of the sphere. So float32 is held to 1e-3 px in
# y and in x times cos(lat) - the error as arc length on the sphere, in pano pixels (measured 2026-10-17:
# 3.8e-4 px in y, 9.3e-4 px of arc, against a raw x error of up to 9.5e-3 px at 86 deg).
TOLERANCE_PX = {'float64': 1e-4, 'float32': 1e-3}


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
    got = F.grid_sample(tg.wrap_pad_torch(t), tg.normalized_grid_torch(sx / k, sy / k, w // k, h // k),
                        mode='bilinear', padding_mode='border', align_corners=False)[0].permute(1, 2, 0).numpy()
    assert np.abs(got - want).max() < 1e-6


def test_a_tilt_matrix_is_used_not_ignored():
    view, tilt = g.label_view(0.5, 0.5, size=9, camera_pitch=3.0)
    flat = tg.sampling_grid_torch(view, None, W, H, dtype=torch.float64)[1]
    tilted = tg.sampling_grid_torch(view, tilt, W, H, dtype=torch.float64)[1]
    # 0.95 * 3 deg of nose-down pitch puts the level view's centre (pixel 4 of 9) 2.85 deg higher in the rig
    shift = (flat - tilted)[4, 4].item()
    assert shift == pytest.approx(2.85 / 180 * H, rel=0.01) and not math.isnan(shift)
