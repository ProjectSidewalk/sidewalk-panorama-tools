# Cropper — `CropRunner.py`

Cuts one image per Project Sidewalk label out of the downloaded panoramas: square, centered on the label,
sized by an estimated camera-to-label distance, written to `<crop-dir>/<label_type_id>/<label_id>.jpg`.

`CropRunner.py` still works but is being replaced, so bugs may linger longer here than in the downloader.
Consumer requirements and the open geometry questions are tracked in
[#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54) and
[#32](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/32).

## Usage

```bash
python3 CropRunner.py (-d <fqdn> | -f <metadata-file>) [-s <pano-dir>] [-o <crop-dir>] [--mark-label]
```

| Flag | What it does |
|---|---|
| `-d <fqdn>` | Fetch label metadata from a Project Sidewalk server's `/adminapi/labels/cvMetadata`. Mutually exclusive with `-f`; one is required. |
| `-f <file>` | Read label metadata from a `.csv` or `.json` file (extension is matched case-insensitively). See `samples/`. |
| `-s <dir>` | Directory holding the panos downloaded by `DownloadRunner.py`. Default `/tmp/download_dest/`. |
| `-o <dir>` | Where crops are written. `crop.log` goes here too. Default `/crops/`. |
| `--mark-label` | Draw a dot at the label position **inside the crop**. Debugging aid, off by default — see the warning below. |

Example:

```bash
python3 CropRunner.py -d sidewalk-columbus.cs.washington.edu \
  -s /sidewalk/columbus/panos/ -o /sidewalk/columbus/crops/
```

Both intakes dedupe on `label_id`, and the CSV intake dtype-pins `pano_id` to `str` and checks the required
columns up front — so a header typo is one error naming the file, not a `KeyError` 200k labels in. Labels are
grouped by pano so each pano JPEG is decoded exactly once for all of its labels.

## Crop geometry

Crop size comes from `predict_crop_size()`, an experimentally fit formula mapping pano-y to distance to crop
size, clamped to `[50, 1500]` px. The window itself comes from `compute_crop_box()`, an integer
`CropBox(left, top, size, shifted)`:

* **x wraps at the equirectangular seam.** The left and right image edges are the same place in the world, so
  a window overlapping the seam is assembled from both edges. In a six-city census of 438,410 labels, 1.52%
  of crops cross the seam; before this was fixed, every one of them carried a black bar
  ([#47](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/47)).
* **y clamps by shifting.** A window that would run past the top or bottom is moved inside the image instead
  of being padded. No crop ever contains synthetic black.

A shifted crop still contains its label, but not at the center. Those are counted separately in the run
summary (`shifted_vertically`) and logged with their offset, so a consumer that assumes centering can see how
many it got. In that same census only two labels needed a shift, and both turned out to be corrupt rows.

Details and measurements: [reports/2026-08-10-crop-geometry-review.md](../reports/2026-08-10-crop-geometry-review.md)
and [reports/2026-08-09-clamp-census.md](../reports/2026-08-09-clamp-census.md).

## The two preflights

Two checks reject a label rather than emit a quietly wrong crop.

**Pano dimensions (`dims_mismatch`).** If the label metadata's pano dimensions disagree with the image on
disk, the label is skipped with a warning. This is a **store-integrity** check: the metadata describes the
pano as it is served *now*, the image was stitched to whatever the API reported when it was downloaded, so a
disagreement means the store is stale (or, on the Mapillary path, that `thumb_original_url` served a
different size than was recorded). It does **not** detect a label whose `pano_x`/`pano_y` went stale under a
pano re-served at a new resolution: those dimensions are a per-pano value that gets refreshed along with the
pano, so such a row looks perfectly consistent. Separating those needs the click→pano replay
([#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54)). Measured over 438,410 labels /
172,790 panos, no pano carries two frames.

Dimensions are read from `pano_width`/`pano_height`, or from `width`/`height` for the older CSV export shape
(`samples/metadata-seattle.csv`). Those latter names are generic — if you supply your own CSV where
`width`/`height` mean something else, a canvas or a bounding box, **every row is skipped as a dimension
mismatch**. The skip is loud and counted, so this surfaces as a number rather than as bad crops.

**Label position (`out_of_frame`).** A `pano_y` outside the image is skipped, because there is no way to
recover it: the poles are not adjacent, so clamping produces clean imagery of a place the label is not in.
`pano_x` is deliberately **not** checked — column 0 and column `pano_width` are the same place in the world,
so the seam wrap reads any finite x correctly, and production rows storing `pano_x == pano_width` exactly do
exist and crop fine.

## Outcomes, exit code, and re-runs

Nothing in the crop loop is fatal. Every label lands in exactly one bucket and the counts reconcile on every
path, including re-runs:

```
success + skipped_existing + missing_pano + dims_mismatch + out_of_frame + errors == total
```

(`shifted_vertically` annotates a success, so it is deliberately not in that sum.)

The run writes a rotating `crop.log` into the crop directory, prints a per-outcome summary, and **exits 1 if
any label errored** — a corrupt pano, a malformed metadata row, a failed write — so a cron wrapper can alert.
Errors are retried on the next run.

The skip outcomes are **not** errors and do not affect the exit code: `missing_pano` (the pano store is
scraped independently and legitimately lags the label list) and the two preflight rejections. Those are
metadata the run declined to trust, not work it got wrong.

**Re-running does not regenerate existing crops.** A crop already on disk is the resume marker and is never
re-cut. A store cropped before the seam fix keeps its black-padded crops, and one cropped before crop sizes
became deterministic holds a mix of (for example) 503- and 504-px crops for the same predicted size. There is
no `--force`: delete the crops you want re-cut.

## Before you train on these crops

**Crops produced before `--mark-label` existed all carry a burned-in dark-red (128, 0, 0) dot at the label
position.** Marking used to be a `MARK_LABEL = True` constant at the top of the file, on for every run. That
dot sits directly over the feature of interest and is exactly what a model will learn instead of the feature.
Re-crop rather than reuse ([#48](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/48)).

**You will likely want to filter out labels where `disagree_count > agree_count`.** These come from human
validations by other Project Sidewalk users; the cropper does **not** filter them by default. A stricter
option is to query `/v2/access/attributesWithLabels` for the city and keep only labels whose `label_id`
appears there too — a more aggressive filter that also removes labels from users we suspect of low-quality
data on some heuristics. The tradeoff is the usual one: more data vs. more accurate data.

**There is small but real error in the y-position of labels on the pano** (first observed Apr 2023). The
candidate root cause is diagnosed — the click→pano mapping corrects for camera heading but not for per-pano
camera tilt, [SidewalkWebpage#4784](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4784) — and
[#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54) tracks measuring the effect at
crop level here, with a correction to follow if the measurement confirms it. A separate render-side effect is
measured in
[reports/2026-08-10-off-target-markers-validate.md](../reports/2026-08-10-off-target-markers-validate.md).
(An earlier note here referred to an "alternative cropper" in development; that effort was abandoned and #54
supersedes it.)

**`label_id` is unique per city, not globally.** Project Sidewalk runs one database schema per city, so crops
from two cities can collide on filename. Key on `(city, label_id)` when you combine them.

## Related

* [API fields](api-fields.md) — what every column of `/adminapi/labels/cvMetadata` means, and the label type IDs.
* [Reports](../reports/README.md) — the crop-geometry, clamp, and click-noise studies behind the numbers above.
