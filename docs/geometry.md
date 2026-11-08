# The installable geometry package — `sidewalk_panorama_tools`

One shared implementation of "render a perspective (gnomonic) view of a Project Sidewalk label from its
equirectangular panorama, with rig-tilt correction", installable with pip so other projects pin it instead of
copying it. The geometry is numpy-only and has no I/O; since 1.1.0 the package also carries
[the pano store's layout](#the-pano-store--sidewalk_panorama_toolsstore), the one module that touches files, and
since 1.2.0 [the GSV tile fetcher](#fetching-a-gsv-panorama--sidewalk_panorama_toolsgsv_tiles), the one that
touches the network (behind the `[gsv]` extra). Two projects use the geometry:

* **sidewalk-validator-ai**, the training pipeline, which cuts its training views with it.
* **sidewalk-ai-api**, the production API, which renders the same view of a label at inference time.

Both get identical geometry: the same view centre, the same field of view and the same tilt correction. A
model trained on one framing and served another loses accuracy without raising an error anywhere.

## Install

```bash
pip install "sidewalk-panorama-tools @ git+https://github.com/ProjectSidewalk/sidewalk-panorama-tools@<tag>"
```

Pin a tag, never a branch. The distribution installs two things: the `sidewalk_panorama_tools` package and
`pano_pose`, a top-level module. It depends on `numpy` and, since 1.1.0, `pillow` (`store.read_pano` decodes a
stored pano; the geometry modules never import it), and it needs Python 3.9 or newer. None of the
runners (`DownloadRunner.py`, `CropRunner.py`, ...) is installed; they still run from a checkout. The tile
fetcher needs the `[gsv]` extra ([below](#fetching-a-gsv-panorama--sidewalk_panorama_toolsgsv_tiles)), as the GPU
grid needs `[torch]`; every other module works on the base install.

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
no pose. A bad input raises `ValueError`; a server should answer it with a 400, not a 500. `render_view`
samples bilinearly, wrapping x at the seam and clamping y at the poles. It is plain numpy on the CPU: about
1.1 s and 554 MB peak for an 8192 pano into a 2048² view, about 0.3 s of it the grid (#221 review). The
torch path below is the fast one. The two consumers must move to a new tag together, because a model
trained on one release's framing and served another's fails without an error.

**Anti-aliasing belongs to the package.** `render_view` box-reduces the pano by
`k = view.reduction_factor(W)` before sampling whenever k > 1 (`box_reduce`; pass `antialias=False` to turn
it off). The last `W % k` columns and `H % k` rows, which do not fill a block, are dropped. The reduced
pano is sampled at `sx · (W // k) / W` and `sy · (H // k) / H` (`sample_grid`), so its wrap period is its own
width and the seam is continuous even when k does not divide W. The dropped columns' angle is spread across
the width, and nothing moves by more than a reduced pixel. The reduction is done in float and rounded once, at
the end. For both current consumers, an 8192-wide pano and a 2048 px 90° view, k = 1: nothing is reduced. A
smaller output or a 16384-wide pano does reduce (a test pins both cases); at 8192, a 512 px view gives k = 5
and a 224 px view k = 11. k is judged at the equator, comparing the pano's W / 360 px per degree with the
view centre's. Towards the poles the pano's horizontal density rises by 1/cos(lat), so a steep view stays
oversampled horizontally (the #221 review measured 1.8× at −45°).

**For torch**, install the `[torch]` extra
(`pip install "sidewalk-panorama-tools[torch] @ git+https://github.com/ProjectSidewalk/sidewalk-panorama-tools@<tag>"`)
and use `sidewalk_panorama_tools.torch_geometry`. It imports torch lazily, so the package never needs it.

```python
import torch
import torch.nn.functional as F
from sidewalk_panorama_tools.torch_geometry import normalized_grid_torch, sampling_grid_torch, wrap_pad_torch

pano = ...                                   # (1, C, H, W) float tensor on the GPU, rig frame
k = view.reduction_factor(W)                 # the anti-aliasing render_view does
if k > 1:
    pano = F.avg_pool2d(pano, k)             # drops the trailing W % k, H % k, as box_reduce does
sx, sy = sampling_grid_torch(view, tilt, W, H, device=pano.device)    # full-resolution pixels, float32
grid = normalized_grid_torch(sx, sy, W, H, k=k)                       # rescales to the pooled size
crop = F.grid_sample(wrap_pad_torch(pano), grid, mode="bilinear", padding_mode="border", align_corners=False)
crop = crop.round().clamp(0, 255).to(torch.uint8)                     # pano in 0-255: round once, here
```

The last line matters for a uint8 pano. `(x * 255).byte()` truncates instead of rounding, a −0.5 LSB bias.

`sampling_grid_torch` has the same semantics as the numpy `sampling_grid`: pixel *i*'s centre is at `i + 0.5`,
x is wrapped into [0, W), and y is left for the sampler to clamp. The tilt stays the numpy 3×3 and is
converted on the way in. `normalized_grid_torch` is the wrap-padding convention: the pano (pooled, if k > 1)
is padded by one wrapped column on each side (`wrap_pad_torch`). With `Wr = W // k` and `Hr = H // k`, the
coordinates are rescaled exactly as numpy's `sample_grid` rescales them, `sxr = sx · Wr / W` and
`syr = sy · Hr / H`. Then `gx = 2 (sxr + 1) / (Wr + 2) − 1` and `gy = 2 syr / Hr − 1`. So sxr = 0.5 lands on
the first real column and sxr = Wr − 0.5 on the last. Tests check that this recipe reproduces `render_view`,
with and without a box reduction. They also check it at the seam with W = 8192, k = 11, and against numpy's
uint8 output to one LSB at k = 2. On an M1 Max the grid for a 2048² view takes 262 ms in numpy, 75 ms in
torch on the CPU, and 19 ms on MPS. In float32 the error against numpy is 1.0–1.05e-3 px of arc at
ordinary latitudes, and the test allows 2e-3 px in y and in arc length. Within 0.05° of the pole the raw x
error is 0.03–0.08 px, because there a column covers almost none of the sphere.

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
  level ray straight ahead is at +2.85°, and one behind is at −2.85°. RampNet's crop cutter (its `VIEWER_TILT = "mm"`,
  the same formula) independently confirms the pitch sign against the HF viewport crops. Its roll sign is
  untested: no GSV row there carries a roll, as its own docstring says. So the roll sign rests on this repo's
  #54 endpoint F1. A test pins the agreement of the two formulas.
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

## The pano store — `sidewalk_panorama_tools.store`

The layout every consumer of the panorama store reads, defined once. sidewalk-ai-api, RampNet and the
auto-labeler scripts each hardcoded it (#222); they pin this module instead. One directory per city, and inside
it one shard per pano, named for the id's first two characters whatever the source (base64url GSV ids,
all-digit Mapillary ids, UUID Panoramax ids):

```
<store>/<city>/<id[:2]>/<id>.jpg          the panorama, as stitched (rig frame)
<store>/<city>/<id[:2]>/<id>.depth.npz    its GSV depth artifact (docs/depth.md)
<store>/<city>/<id[:2]>/<id>.w8192.jpg    a display copy (#115): not a panorama, never cut from
```

```python
from sidewalk_panorama_tools.store import (pano_path, depth_path, walk_store_panos, jpeg_dimensions,
                                           read_pano, PanoDimsMismatch)

path = pano_path("/mnt/panostore", pano_id, city="seattle-wa")
try:
    pano = read_pano(path, expected_width=label["pano_width"], expected_height=label["pano_height"])
except PanoDimsMismatch as e:        # e.expected, e.stored: (width, height)
    ...                              # skip the label: its pano_x/pano_y are in another frame
for pano_file in walk_store_panos("/mnt/panostore/seattle-wa"):
    width, height = jpeg_dimensions(pano_file)      # header only; None for an unreadable file
```

* `shard`, `pano_path`, `depth_path`, `sidecar_path(pano_file, max_width)` build paths; pass `city=` with the
  store root, or leave it out with a city's own directory. The pano id must be a `str` of at least two
  characters, or `ValueError`: an all-digit Mapillary id is text too, so read it as `str`, never as a number.
  `is_downscaled_sidecar(name)` says whether a file is a display copy.
* **`walk_store_panos(city_dir)` is the one definition of "this file is a panorama".** Only
  `<id[:2]>/<id>.jpg` in its own shard counts. A walker that forgets the display-copy rule does not fail, it
  invents pano ids like `<id>.w8192`, so never list `*.jpg` by hand.
* `jpeg_dimensions(path)` reads `(width, height)` from the JPEG header without decoding: a 16384×8192 pano
  costs a few reads instead of 384 MB.
* **`read_pano(path, expected_width=None, expected_height=None)`** checks the header against the label's
  `pano_width`/`pano_height` and raises `PanoDimsMismatch` **before decoding** when they differ, then
  returns the loaded `PIL.Image`. It is the cropper's dims preflight (`docs/cropper.md`): a stored file whose
  frame is not the pano's, most often one stitched at the 13312×6656 the app hardcoded until 2022-03, would put
  every label in the wrong place. The check is `jpeg_dimensions`, so a refusal never touches Pillow, and the
  expected values are read as CropRunner reads them (`int(float(x))`, so `"64.0"` is 64; a value that is not a
  positive number raises `ValueError`, never `PanoDimsMismatch`). A header over `MAX_PANO_PIXELS`
  (16384 × 8192) is refused. **Call `raise_decompression_bomb_ceiling()` once at startup** if you read 134 MP
  panos: otherwise Pillow warns on each. `read_pano` installs no per-call warnings filter, because
  `warnings.catch_warnings` rewrites process-wide state and is not thread-safe in a server. A file the file
  system will not read raises the file system's own `OSError`, errno included (`FileNotFoundError`,
  `PermissionError`, EIO, a stale NFS handle); a file that reads but is not a JPEG header raises `OSError`
  naming the path. `jpeg_dimensions` keeps returning `None` for both, because its callers are sweeps.

`CropRunner.py` makes the same comparison but does not call `read_pano`: it opens a pano once for all its
labels, checks each label's dims against the one header, and decodes lazily only when a crop is about to be
written, which a one-shot read cannot express. `downloaders/common.py` re-exports the walker, the sidecar
predicate, the header reader and the ceiling from this module, so the scraper and the consumers share them.

## The front end's click math — `sidewalk_panorama_tools.ps_math`

The projection the Project Sidewalk front end runs when it stores a label's `pano_x`/`pano_y`, ported once.
sidewalk-auto-labeler (`ps_placement.py`) and RampNet (`rampnet/crops.py`) each carried a port; they pin this.
It is numpy, vectorised, degrees in and out, and has no I/O. `reports/scripts/pov_replay.py`, where it was
written for the desk studies, now re-exports it.

| Function | What it is |
|---|---|
| `get_3d_fov(zoom)` | `get3dFov`: the viewer's field of view at a zoom (89.75° at 1, 53° at 2, 27.68° at 3) |
| `pov_if_centered(canvas_x, canvas_y, heading, pitch, zoom, canvas_width=720, canvas_height=480)` | `calculatePovIfCentered`: the POV a click would have at the canvas centre |
| `exact_depression_deg(...)` | the same click's depression below the horizon, `-pov_pitch` |
| `pano_xy_from_pov(pov_heading, pov_pitch, camera_heading, pano_width, pano_height)` | `calculatePanoXYFromPov` with evolution 179's round-then-wrap: integer pano pixels |
| `pov_from_pano_xy(pano_x, pano_y, camera_heading, pano_width, pano_height)` | its inverse, continuous. The forward map rounds, so pixel → POV → pixel is exact and POV → pixel → POV is back within one pixel (360 / `pano_width` degrees) |
| `depression_from_pano_y(pano_y, pano_height)` | the depression a stored row implies |
| `predict_blend_distance(depression_deg)` | label-latlng-estimation #3's distance: a cotangent on the camera height above `BLEND_DEG` (11.25°) of depression, a linear tail below it, capped at `DIST_CAP_M` (50 m) |

The frames, the same in every function:

* `pov_heading` is degrees **clockwise from north**, in (−180°, 180°]; `pov_pitch` is degrees, **positive up**.
* The pano raster is **heading-centred**: its centre column looks along `camera_heading` (the photographer's
  heading), so column zero is at bearing `camera_heading − 180`, and the horizon is row `pano_height / 2`.
* **Depression is positive down**: `depression = −pov_pitch`.
* There is no rig pitch or roll: the front end ignores the pose. That is #54, and what `geometry.label_view`'s
  tilt corrects.

`tests/test_ps_math.py` pins `pov_if_centered` and the unrounded `calculatePanoXYFromPov` against a line-by-line
transcription of `UtilitiesPanomarker.js` at SidewalkWebpage tag `v7.19.10`, and the round trips on a grid that
crosses the seam and both hemispheres. `viewport_view` (RampNet's on-screen view of a label) is not here; it
stays in RampNet.

## Validation votes — `sidewalk_panorama_tools.labels`

The arithmetic four consumer repos each re-derived when deciding which labels count as validated: human-only or
all votes, majority or thresholds, and what a tie means. Every policy is a required argument, so a caller
states its rule rather than inheriting one; the module makes no decision about which rule is right.

```python
from sidewalk_panorama_tools.labels import label_uid, vote_counts, majority, is_validated

props = feature["properties"]                                # one /v3/api/rawLabels feature
counts = vote_counts(props["validations"])                   # VoteCounts(agree, disagree, unsure), humans only
verdict = majority(counts, tie="none")                       # Verdict.AGREE / DISAGREE / UNSURE / NONE
keep = is_validated(counts, min_votes=2, min_agree_fraction=0.75)
key = label_uid("seattle-wa", props["label_id"])             # "seattle-wa:12327"
```

* **`label_uid(city, label_id)`** is `"<city>:<label_id>"`. `label_id` is a per-city serial, so a label
  reference without its city is incomplete, and a join keyed on `label_id` alone silently merges labels from
  different cities. The uid and `pano_id` are the only safe cross-city keys. A `label_id` below 1 (a fill
  value: every city's serial starts at 1), one that is not an integer, and a non-finite float are refused
  with `ValueError`, and so is a `city` that is not a non-empty `str` without a colon.
* **`vote_counts(validations, *, include_ai=False)`** tallies the API's `validations` array. Each element is
  `{"validation": "Agree" | "Disagree" | "Unsure", "validator_type": "Human" | "AI", "user_id": ...}` (read from
  a live Seattle response on 2026-11-05). The AI validator is excluded by default and is identified by
  `validator_type` alone, never by an account. A value outside those raises, so a new validator type is never
  silently counted as human.
* **`majority(counts, *, tie)`** compares agree with disagree; unsure abstains. `tie` is required: `"agree"`,
  `"disagree"`, `"unsure"` or `"none"`. No decisive vote at all is a tie too.
* **`is_validated(counts, *, min_votes, min_agree_fraction)`** passes a label with at least `min_votes` decisive
  votes (at least 1; any integer type, numpy's included) whose agree share `agree / decisive` is at or above the
  fraction, compared as that quotient so a share exactly on the threshold passes. The fraction is a number in
  [0, 1] (any real type, numpy's included); a `str` (`"0.5"` too), a `bool` (numpy's too) or `None` raises `ValueError`, as
  `min_votes` refuses `"2"` and `True`.

**What the API's own fields mean**, measured on that response: `agree_count`, `disagree_count` and
`unsure_count` **include the AI validator's votes** (they equal `vote_counts(..., include_ai=True)`, measured
on all 85 labels of that response), and `correct` is `majority(vote_counts(..., include_ai=True), tie="none")`:
true, false, or null on a tie. A consumer that wants human votes only must tally the array; the counts columns
cannot give it. `tests/test_labels.py` pins both on the anonymised 14 of those 85 that the committed fixture
keeps; the full response is not in the repo, so the 85-label measurement is a record, not a test.

## Fetching a GSV panorama — `sidewalk_panorama_tools.gsv_tiles`

The tile fetcher the nightly scraper runs, defined once (#222 step 2): the fan-out over Google's undocumented
`cbk?output=tile` endpoint, the two-tile zoom probe, the frame check, the stitch, and the classification of
Google's refusals. sidewalk-validator-ai, sidewalk-ai-api and RampNet each carry a copy; this module is the one to pin
instead. It needs the `[gsv]` extra (`requests`, `urllib3`, `aiohttp`, `backoff`), and importing it without the
extra raises an `ImportError` that names it:

```bash
pip install "sidewalk-panorama-tools[gsv] @ git+https://github.com/ProjectSidewalk/sidewalk-panorama-tools@<tag>"
```

```python
from sidewalk_panorama_tools import gsv_tiles

zoom = gsv_tiles.probe_zoom(pano_id)                  # 5, 3, or None
if zoom is None:
    ...                                               # no imagery at either zoom: the pano is gone
elif not gsv_tiles.frame_covers_pano(pano_id, width, height, zoom):
    ...                                               # Google serves it LARGER than (width, height): refuse
else:
    stitched = gsv_tiles.fetch_pano_image(pano_id, width, height, zoom)
    image = stitched.image                            # a PIL image at exactly (width, height)
```

The composition, and what each answer means:

1. **`probe_zoom(pano_id)`** asks for tile (0, 0) at zoom 3 and zoom 5 (two requests). `None` means both came
   back exactly black on a 200, which is how cbk answers a retired pano: the pano is gone. Any status but 200
   raises `requests.HTTPError` instead, so a 403 with a black body is never read as "gone".
2. **`frame_covers_pano(pano_id, width, height, zoom)`** asks three tiles at the edges of the grid that
   `(width, height)` implies. `False` means a tile past the grid has imagery: Google serves the pano larger, and
   fetching would return its top-left corner at your exact dimensions, with nothing to show it. It raises
   `FrameDisagreementError` when the grid's own last column is black: Google serves it smaller, and the stitch
   would be a third black. **Ask it only after the probe found imagery**: cbk answers a retired pano with black
   tiles everywhere, which this check reads as "smaller".
3. **`fetch_pano_image(pano_id, width, height, zoom)`** fans out every tile of the grid (`CONCURRENCY` in
   flight), stitches them, and returns `StitchedPano(image, undersized_tiles, upscaled)`. `upscaled` is true when
   only a lower zoom covered the frame and the image was LANCZOS-upscaled to `(width, height)`; it holds less
   imagery than its size says. A failed tile, or a stitch more than half exactly black, raises: transient, try
   another day.

`width` and `height` are yours to supply - the pano's frame as the Project Sidewalk app records it. The probe
cannot tell a 16384-wide pano from a 13312-wide one, and this module does not ask photometa (the scraper's
`downloaders/gsv.py` does, through `streetlevel`, which stays out of the package). `choose_zoom(sizes, width,
height)` picks the zoom from photometa's level sizes if you have them.

**Push-back.** A tile that answers 429 or 403, or lands on Google's interstitial at any status, raises
`TilePushbackError`, which the tile retry policy never retries, and the pano's remaining tiles are abandoned at
the first one. `pushback_reason(exc)` reads every shape of refusal (`'HTTP 429'`, `'HTTP 403'`,
`'interstitial'`) and returns `None` for anything that is merely weather: a timeout, a reset, a 5xx storm. A
non-`None` answer means Google is refusing this host. Stop for the night. Retrying a refusal is how a soft rate
limit is turned into a block, and before the scraper learned that, one refused 16384-wide pano cost 5,120
requests.

**What it does not do.** It never writes a file, keeps no ledger, has no breaker and no block latch, and makes
no display copy: those are the nightly scraper's policy, in `downloaders/gsv.py`, which composes these same
functions with them. **Never write what you fetch over a stored pano.** The store is an archive, not a cache
(`docs/ops.md`): about half of the labelled panos are already retired at Google and about a quarter of the rest
come back re-rendered, so the stored file is usually the only copy of that picture.

**One URL builder.** `tile_url(pano_id, zoom, x, y)` builds every tile request from `CBK_BASE_URL`, so a
parameter added there reaches the fan-out, the probe and the frame check at once. Do not add viewer parameters
without measuring them: `fover`, copied from the Street View viewer, halved the polar rows of zoom 5 (#73).

**Settings.** `HEADERS` (one picked at random per request), `PROXIES` and `CONCURRENCY` (8) are read at call
time. Change them with `configure(headers=..., proxies=..., concurrency=...)`; the scraper configures them from
its `config.py` at import, whose `headers_list` the package default `DEFAULT_HEADERS` copies (a test holds the
two equal). Logging goes to the root logger, with the scraper's `IMAGEDOWNLOAD:` prefix. `sidewalk_panorama_tools`
itself never imports this module, so the base install stays free of the network stack.

## Provenance and releases

The view geometry is ported from RampNet `rampnet/crops.py` at commit
`4156847d12d3f4ccac6102f7770cd315d02791d0` and rebased onto `pano_pose`'s frame. The port drops RampNet's
four-way tilt-convention table (the sign is measured, so there is nothing to choose), its viewport view, its
equirect window, and its JPEG and hash helpers. `pano_pose.py` stays at the repo root because it is also
uploaded to makelab2 by itself; `sidewalk_panorama_tools.pose` re-exports it.

A geometry change is a release. Bump `version` in `pyproject.toml` and `__version__` together (a test checks
that they match), tag it, and say in the release what a consumer will see change when it re-pins.

**Releases.**

* **1.2.0** (2026-11-06, #222 step 2): adds `gsv_tiles`, the GSV tile fetcher the nightly scraper runs
  (moved from `downloaders/gsv.py`, which re-exports it), behind the new `[gsv]` extra (`requests`, `urllib3`,
  `aiohttp`, `backoff`), and `store.black_fraction` (moved from `downloaders/common.py`, which re-exports it).
  The base install's dependencies are unchanged, and nothing in `geometry`, `pose`, `torch_geometry`,
  `ps_math` or `labels` changed, so a consumer re-pinning sees the same views byte for byte.
* **1.1.0** (2026-11-05, #222 steps 4-6): adds `store` (the pano store's layout, moved here from
  `downloaders/common.py`, which re-exports it), `ps_math` (the front end's click math, moved here from
  `reports/scripts/pov_replay.py`, with the new inverse `pov_from_pano_xy`), `labels` (vote arithmetic with explicit policies), and `pillow` as a
  dependency. Its refusals (#231): `store.shard` and the path builders raise `ValueError` for a pano id that
  is not a `str` of at least two characters; `read_pano` raises the file system's own `OSError`;
  `labels.label_uid` refuses a non-finite id and a non-`str` city; `is_validated` refuses a fraction that is
  not a number (`str`, `bool` and `numpy.bool_` included) with `ValueError`.
  Nothing in `geometry`, `pose`
  or `torch_geometry` changed, so a consumer re-pinning sees the same views byte for byte.
* **1.0.0**: the gnomonic label view, `pose`, and the torch twin.
