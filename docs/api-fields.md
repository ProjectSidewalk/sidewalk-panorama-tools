# API field glossary

The two Project Sidewalk endpoints this repo reads, field by field. Both are served by every deployed city at
`https://<city-fqdn>/...`.

## `/adminapi/panos` — the downloader's pano list

| Attribute | Definition |
|---|---|
| `pano_id` | A unique ID, provided by the imagery source, for the panoramic image |
| `width` | The width of the pano image in pixels |
| `height` | The height of the pano image in pixels |
| `lat` | The latitude of the camera when the image was taken |
| `lng` | The longitude of the camera when the image was taken |
| `camera_heading` | The heading (in degrees) of the center of the image with respect to true north |
| `camera_pitch` | The pitch (in degrees) of the camera with respect to horizontal |
| `source` | The source of the imagery (`gsv`, `mapillary`, …) |

The downloader drops empty ids and the literal id `tutorial`, then keeps `gsv` and — when
`MAPILLARY_ACCESS_TOKEN` is set — `mapillary`. See [Downloader → Imagery sources](downloader.md#imagery-sources).

## `/adminapi/labels/cvMetadata` — the cropper's label list

You won't need most of this in your work, but it's all here for reference. Everything through `notsure_count`
might be useful; then there are a few duplicates from the endpoint above; then everything from `canvas_width`
on probably doesn't matter for you.

| Attribute | Definition |
|---|---|
| `label_id` | A unique ID for each label **within a given city**, provided by Project Sidewalk |
| `gsv_panorama_id` | A unique ID, provided by the imagery source, for the panoramic image [same as `/adminapi/panos`] |
| `source` | The source of the imagery (`gsv`, `mapillary`, …) [same as `/adminapi/panos`] |
| `label_type_id` | An integer ID denoting the type of label placed — see the table below |
| `pano_x` | The x-pixel location of the label on the pano, where top-left is (0,0) |
| `pano_y` | The y-pixel location of the label on the pano, where top-left is (0,0) |
| `agree_count` | The number of "agree" validations provided by Project Sidewalk users |
| `disagree_count` | The number of "disagree" validations provided by Project Sidewalk users |
| `notsure_count` | The number of "not sure" validations provided by Project Sidewalk users |
| `pano_width` | The width of the pano image in pixels [same as `/adminapi/panos`] |
| `pano_height` | The height of the pano image in pixels [same as `/adminapi/panos`] |
| `camera_heading` | The heading (in degrees) of the center of the image with respect to true north [same as `/adminapi/panos`] |
| `camera_pitch` | The pitch (in degrees) of the camera with respect to horizontal [same as `/adminapi/panos`] |
| `canvas_width` | The width of the canvas where the user placed a label in Project Sidewalk |
| `canvas_height` | The height of the canvas where the user placed a label in Project Sidewalk |
| `canvas_x` | The x-pixel location where the user clicked on the canvas to place the label, where top-left is (0,0) |
| `canvas_y` | The y-pixel location where the user clicked on the canvas to place the label, where top-left is (0,0) |
| `heading` | The heading (in degrees) of the center of the canvas with respect to true north when the label was placed |
| `pitch` | The pitch (in degrees) of the center of the canvas with respect to *the camera's pitch* when the label was placed |
| `zoom` | The zoom level in the GSV interface when the user placed the label |

Three things to know before you join on any of this:

* **`label_id` is unique per city, not globally.** Project Sidewalk runs one database schema per city, each
  with its own serial. Key on `(city, label_id)`; a cross-city merge on `label_id` alone silently
  cross-joins. `pano_id` is the imagery source's and *is* safe as a cross-city key.
* **`pano_width`/`pano_height` are a per-pano join against current metadata**, not a click-time snapshot —
  which is exactly why the cropper's dims preflight guards the store rather than the label's frame. See
  [Cropper → The two preflights](cropper.md#the-two-preflights).
* **`pano_x`/`pano_y` are heading-centred**, unlike the legacy `sv_image_x`, which is north-referenced. Mixing
  the conventions displaces a label by up to half a panorama — and by nothing at all on a pano facing south,
  so a one-example check can pass on the wrong one. See [Depth maps → Sampling depth under a
  label](depth.md#sampling-depth-under-a-label).

## Label type IDs

Used in both APIs, and as the crop output subdirectory name. Yes, 8 was skipped. 🤷

| `label_type_id` | Label type |
|---|---|
| 1 | Curb Ramp |
| 2 | Missing Curb Ramp |
| 3 | Obstacle in a Path |
| 4 | Surface Problem |
| 5 | Other |
| 6 | Can't see the sidewalk |
| 7 | No Sidewalk |
| 9 | Crosswalk |
| 10 | Pedestrian Signal |
