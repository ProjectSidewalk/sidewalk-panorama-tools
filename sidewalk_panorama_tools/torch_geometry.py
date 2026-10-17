"""Optional torch versions of the sampling grid, so a consumer can build it on the GPU (docs/geometry.md).

The numpy :func:`sidewalk_panorama_tools.geometry.sampling_grid` takes about 240 ms for a 2048 x 2048 view on
an M1 Max (measured by the sidewalk-ai-api agent), which a nightly sweep of tens of thousands of requests
feels. :func:`sampling_grid_torch` computes the same grid with torch on ``device``; the geometry (view basis,
tilt matrix, pixel convention) is still the numpy module's, so there is one definition of where a view looks.

torch is **not** a dependency of the package: it is imported inside the functions, so this module imports
without it. Install it with the ``[torch]`` extra (``pip install "sidewalk-panorama-tools[torch] @ git+..."``)
or bring your own.

Recipe (the one :func:`normalized_grid_torch` is written for)::

    import torch, torch.nn.functional as F
    from sidewalk_panorama_tools.geometry import label_view
    from sidewalk_panorama_tools.torch_geometry import normalized_grid_torch, sampling_grid_torch, wrap_pad_torch

    view, tilt = label_view(x_norm, y_norm, fov_h_deg=90.0, size=2048, camera_pitch=camera_pitch)
    pano = ...                                            # (1, C, H, W) float tensor on `device`, rig frame
    k = view.reduction_factor(W)
    if k > 1:                                             # the anti-aliasing render_view does in numpy
        pano = F.avg_pool2d(pano, k)                      # drops the trailing W % k, H % k, as box_reduce does
    sx, sy = sampling_grid_torch(view, tilt, W, H, device=pano.device)
    grid = normalized_grid_torch(sx, sy, W, H, k=k)       # full-resolution sx, sy; k rescales to the pooled size
    crop = F.grid_sample(wrap_pad_torch(pano), grid, mode='bilinear', padding_mode='border', align_corners=False)
    crop = crop.round().clamp(0, 255).to(torch.uint8)     # for a uint8 pano: round once, here (not (x*255).byte())

Precision: float64 matches the numpy grid to well under 1e-4 px. float32 is limited by its own resolution
(an x near 8192 has a float32 spacing of about 5e-4 px): at an 8192-wide pano the error is 1.0-1.05e-3 px of
arc at ordinary latitudes, while the raw x error grows towards the poles (0.03-0.08 px within 0.05 deg of
the pole, where a column is a sliver of the sphere; #221 review). tests/test_torch_geometry.py holds float32
to 2e-3 px in y and in arc length. MPS has no float64.

Speed (M1 Max, 2048 x 2048 view, 8192 pano, measured 2026-10-17): numpy sampling_grid 262 ms, this on the
CPU 75 ms, on MPS 19 ms.
"""

from typing import Optional, Tuple

import numpy as np

from . import geometry


def _torch():
    import torch  # deferred: torch is an optional extra, never a hard dependency
    return torch


def sampling_grid_torch(view: 'geometry.View', tilt: Optional[np.ndarray], pano_width, pano_height,
                        device=None, dtype=None) -> Tuple['object', 'object']:
    """Torch twin of :func:`geometry.sampling_grid`: ``(sx, sy)``, each a (view.height, view.width) tensor of
    ``dtype`` (float32 when None) on ``device``, in the full-resolution pano's continuous pixels (pixel
    *i*'s centre at ``i + 0.5``; x wrapped into [0, pano_width), y in [0, pano_height] unclamped, exactly as
    the numpy grid; the sampler clamps y). ``tilt`` is the numpy 3x3 from :func:`geometry.tilt_matrix`
    (or None), converted on the way in.
    """
    torch = _torch()
    dtype = torch.float32 if dtype is None else dtype
    fwd, right, up = (torch.as_tensor(v, dtype=dtype, device=device) for v in
                      geometry._basis(view.yaw_deg, view.pitch_deg))
    f = view.focal_px
    a = (torch.arange(view.width, dtype=dtype, device=device) + 0.5 - view.width / 2.0) / f
    b = (view.height / 2.0 - (torch.arange(view.height, dtype=dtype, device=device) + 0.5)) / f
    d = fwd + a[None, :, None] * right + b[:, None, None] * up              # (H, W, 3)
    d = d / torch.linalg.vector_norm(d, dim=-1, keepdim=True)
    if tilt is not None:
        d = d @ torch.as_tensor(np.asarray(tilt, dtype=np.float64), dtype=dtype, device=device).T
    lon = torch.rad2deg(torch.atan2(d[..., 0], d[..., 1]))
    # atan2(z, |xy|) rather than numpy's asin(z): the same angle for a unit ray, but asin loses float32
    # precision towards the poles, where a curb ramp's steep views sit
    lat = torch.rad2deg(torch.atan2(d[..., 2], torch.hypot(d[..., 0], d[..., 1])))
    # pano_pose.pixel_from_bearing_elevation, written out
    sx = torch.remainder((lon + 180.0) / 360.0 * pano_width, pano_width)
    sy = (0.5 - lat / 180.0) * pano_height
    return sx, sy


def normalized_grid_torch(sx, sy, pano_width, pano_height, k=1):
    """The ``grid_sample`` grid, (1, H, W, 2), for ``align_corners=False`` on a pano padded by one wrapped
    column on each side (:func:`wrap_pad_torch`).

    ``sx``, ``sy`` are full-resolution coordinates (:func:`sampling_grid_torch`) of a ``pano_width`` x
    ``pano_height`` pano; ``k`` is the box reduction applied to it (``avg_pool2d(pano, k)``, k = 1 for none).
    With ``Wr = pano_width // k``, ``Hr = pano_height // k`` the coordinates are rescaled to
    ``sxr = sx * Wr / pano_width``, ``syr = sy * Hr / pano_height`` - the wrap period is the pooled width, as in
    numpy's :func:`geometry.sample_grid` - and then ``gx = 2 (sxr + 1) / (Wr + 2) - 1``, ``gy = 2 syr / Hr - 1``.
    So sxr = 0.5 (the centre of column 0) lands on the centre of the first real column of the padded tensor,
    and sxr = Wr - 0.5 on the last; a sample between them reads the padding, which is the other edge.
    """
    torch = _torch()
    k = int(k)
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    wr, hr = pano_width // k, pano_height // k
    sxr = sx * (wr / pano_width)
    syr = sy * (hr / pano_height)
    gx = 2.0 * (sxr + 1.0) / (wr + 2.0) - 1.0
    gy = 2.0 * syr / hr - 1.0
    return torch.stack([gx, gy], dim=-1)[None]


def wrap_pad_torch(pano):
    """Pad an (N, C, H, W) pano by one wrapped column on each side: the layout
    :func:`normalized_grid_torch` assumes."""
    torch = _torch()
    return torch.cat([pano[..., -1:], pano, pano[..., :1]], dim=-1)
