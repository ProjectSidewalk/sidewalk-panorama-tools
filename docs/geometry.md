# The installable geometry package — `sidewalk_panorama_tools`

One shared implementation of "render a perspective (gnomonic) view of a Project Sidewalk label from its
equirectangular panorama, with rig-tilt correction", installable with pip so other projects pin it instead of
copying it. It is numpy-only and has no I/O. Two projects use it:

* **sidewalk-validator-ai**, the training pipeline, which cuts its training views with it.
* **sidewalk-ai-api**, the production API, which renders the same view of a label at inference time.

Both get identical geometry: the same view centre, the same field of view and the same tilt correction. A
model trained on one framing and served another loses accuracy without raising an error anywhere.

## Install

```bash
pip install "sidewalk-panorama-tools @ git+https://github.com/ProjectSidewalk/sidewalk-panorama-tools@<tag>"
```

Pin a tag, never a branch. The distribution installs two things: the `sidewalk_panorama_tools` package and
`pano_pose`, a top-level module. Its only dependency is `numpy`, and it needs Python 3.9 or newer. None of the
runners (`DownloadRunner.py`, `CropRunner.py`, ...) is installed; they still run from a checkout.

## Usage

```python
import numpy as np
from PIL import Image
from sidewalk_panorama_tools.geometry import label_view, render_view, sampling_grid

pano = np.asarray(Image.open("pano.jpg").convert("RGB"))       # (H, W, 3), as stored (rig frame)
view, tilt = label_view(pano_x / pano_width, pano_y / pano_height,
                        fov_h_deg=90.0, size=512,
                        camera_pitch=label.camera_pitch)        # degrees, from the PS database; None if unknown
crop = render_view(pano, view, tilt)                            # (512, 512, 3) uint8
```

`label_view` validates its input: a finite `x` (it wraps modulo 1, because `pano_x` is periodic and real rows
carry `pano_x == pano_width` and a pixel past it), `0 <= y <= 1` (never wrapped), `0 < fov < 180`, and an
integer `size`. It returns a square view aimed at the label and the tilt matrix, which is None when there is
no pose. `render_view` samples bilinearly, wrapping x at the seam and clamping y at the poles.

**Anti-aliasing belongs to the package.** `render_view` box-reduces the pano by
`k = view.reduction_factor(W)` before sampling whenever k > 1 (`box_reduce`; pass `antialias=False` to turn
it off). The last `W % k` columns and `H % k` rows, which do not fill a block, are dropped, and the
reduced pano is sampled at the full-resolution coordinates divided by k, so the geometry is unchanged
outside that strip. For both current consumers, an 8192-wide pano and a 2048 px 90° view, k = 1: nothing is
reduced. A smaller output or a 16384-wide pano does reduce (a test pins both cases).

**For torch**, install the `[torch]` extra
(`pip install "sidewalk-panorama-tools[torch] @ git+https://github.com/ProjectSidewalk/sidewalk-panorama-tools@<tag>"`)
and use `sidewalk_panorama_tools.torch_geometry`. It imports torch lazily, so the package never needs it.

```python
import torch.nn.functional as F
from sidewalk_panorama_tools.torch_geometry import normalized_grid_torch, sampling_grid_torch, wrap_pad_torch

pano = ...                                   # (1, C, H, W) float tensor on the GPU, rig frame
k = view.reduction_factor(W)                 # the anti-aliasing render_view does
if k > 1:
    pano = F.avg_pool2d(pano, k)             # drops the trailing W % k, H % k, as box_reduce does
sx, sy = sampling_grid_torch(view, tilt, W, H, device=pano.device)    # full-resolution pixels, float32
grid = normalized_grid_torch(sx / k, sy / k, W // k, H // k)
crop = F.grid_sample(wrap_pad_torch(pano), grid, mode="bilinear", padding_mode="border", align_corners=False)
```

`sampling_grid_torch` has the same semantics as the numpy `sampling_grid`: pixel *i*'s centre is at `i + 0.5`,
x is wrapped into [0, W), and y is left for the sampler to clamp. The tilt stays the numpy 3×3 and is
converted on the way in. `normalized_grid_torch` is the wrap-padding convention: the pano is padded by one
wrapped column on each side (`wrap_pad_torch`), and `gx = 2 (sx + 1) / (W + 2) − 1`, `gy = 2 sy / H − 1`. So
sx = 0.5 lands on the first real column and sx = W − 0.5 on the last. Tests check that this recipe
reproduces `render_view`, with and without a box reduction. On an M1 Max the grid for a 2048² view takes
262 ms in numpy, 75 ms in torch on the CPU, and 19 ms on MPS. float32 agrees with numpy to under 1e-3 px in
y and in arc length. Near the poles the raw x error reaches about 1e-2 px, where a column covers almost none
of the sphere.

## The convention

Every convention here is `pano_pose.py`'s (its module docstring is the reference):

* A label's normalized point `(X, Y) = (pano_x / pano_width, pano_y / pano_height)` looks at
  `lon = (X - 0.5) * 360` (clockwise from the pano's centre column) and `lat = (0.5 - Y) * 180` (up
  positive). This is the inverse of Project Sidewalk's `calculatePanoXYFromPov`. It is resolution-independent,
  so a stored JPEG at a different resolution from the label's `pano_width` is sampled at the same point.
* Rays are unit vectors in the RFU frame (right, forward, up). A view is a roll-free pinhole camera, and
  `fov_h_deg` is always the horizontal angle.
* **Tilt.** The stored `pano_x`/`pano_y` are in the level frame, because Project Sidewalk's click mapping
  ignores the rig's pose, but the stored JPEG is in the rig frame
  ([the #54 study](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/blob/master/reports/2026-09-26-tilt-error-study.md); SidewalkWebpage#4784). `tilt_matrix` takes a
  level ray into the rig frame. It is `pano_pose.rig_from_gravity` at `beta` times the pose, so it uses the
  measured sign: **pitch > 0 means the nose points down**, and a level ray at bearing b sits at rig elevation
  `lat + beta * (camera_pitch * cos b + camera_roll * sin b)`. With `camera_pitch = 3` and the default beta 0.95, a
  level ray straight ahead is at +2.85°, and one behind is at −2.85°. RampNet's crop cutter measured the same
  relation independently against the HF viewport crops (its `VIEWER_TILT = "mm"`), and a test pins the
  agreement.
* **Beta** is the fraction of the rig tilt that leaked into the stored click. It scales the pose, as
  `pano_pose.corrected_pixel` does. On 3,518 pairs this repo measured 0.884 under the 2019 XML pose record and
  0.953 (SE 0.020) under streetlevel's modern pose record, the `.depth.npz` pose ([the cropper's tilt correction](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/blob/master/docs/cropper.md#the-tilt-correction-opt-in-191)). The package
  defaults to **0.95**, for the pose a consumer actually has: the label's `camera_pitch` database column. That
  column equals streetlevel's modern pose record exactly; sidewalk-auto-labeler#113 checked all 2,952 paired
  Gainesville labels, and Gainesville's own beta is 0.951. (PS stores no roll for GSV labels.) Beta 0, or no
  pose, means no correction.
* **Wrapping.** Angles wrap to (−180°, 180°] with `pano_pose.wrap_deg`; `geometry.wrap180` is the same
  function.

`tests/test_geometry.py` pins all of the above. That includes a synthetic rig-frame pano whose marker is drawn
at `corrected_pixel`'s point: a render with the tilt shows the marker at the centre, and an untilted render
does not. The tests also replay RampNet's committed 200-label validation sample.

## How it relates to CropRunner

`CropRunner.py` does not use this package. It cuts **equirect windows**, which are plain 3:2 rectangles of the
stored JPEG sized by its sizing rule ([Crop geometry](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/blob/master/docs/cropper.md#crop-geometry)), and with `--tilt-correction`
it moves the window's centre to the rig pixel (`pano_pose.corrected_pixel`, with beta set per pose record).
This package renders **gnomonic views**, which undo the equirect's 1/cos(latitude) horizontal stretch. It
corrects the tilt by rotating every ray rather than shifting a window, so at the same beta the centre
of a tilted `label_view` is the same rig pixel that `--tilt-correction` centres on. Both rest on the same `pano_pose`
definitions, so the sign and the frames cannot diverge.

## Provenance and releases

The view geometry is ported from RampNet `rampnet/crops.py` at commit
`4156847d12d3f4ccac6102f7770cd315d02791d0` and rebased onto `pano_pose`'s frame. The port drops RampNet's
four-way tilt-convention table (the sign is measured, so there is nothing to choose), its viewport view, its
equirect window, and its JPEG and hash helpers. `pano_pose.py` stays at the repo root because it is also
uploaded to makelab2 by itself; `sidewalk_panorama_tools.pose` re-exports it.

A geometry change is a release. Bump `version` in `pyproject.toml` and `__version__` together (a test checks
that they match), tag it, and say in the release what a consumer will see change when it re-pins.
