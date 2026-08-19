# sidewalk-panorama-tools

## About
This repository contains a set of Python scripts, intended to be used with data from [Project Sidewalk](https://github.com/ProjectSidewalk/SidewalkWebpage). The purpose of these scripts are to create crops of sidewalk accessibility issues/features usable for ML and computer vision applications from Google Streetview Panoramas via crowd-sourced label data from Project Sidewalk. 

The scripts are intended to be run inside a Docker container running Ubuntu 22.04 64-bit. However, one should be able to run these scripts on most Linux distros without the need for Docker, assuming the Python packages listed in `requirements.txt` can be installed. Additional effort would be required to use the downloader on a Mac or Windows machine without Docker.

There are two main scripts of note: [DownloadRunner.py](DownloadRunner.py) and [CropRunner.py](CropRunner.py). Both should be fully functional, but only the downloader is actively in use (a new version is in the works), so we may not notice bugs with the cropper as quickly. More details on both below!

**Note:** At least 2GB RAM is recommended, as these scripts may crash on very low memory systems due to the size of the images processed.

## Downloader
1. [Install  Docker Desktop](https://www.docker.com/get-started).
1. Run `git clone https://github.com/ProjectSidewalk/sidewalk-panorama-tools.git` in the directory where you want to put the code.
1. Create the Docker image
    ```
    docker build --no-cache --pull -t projectsidewalk/scraper:v6 <path-to-pano-tools-repo>
    ```
1. You can then run the downloader using the following command:
    ```
    docker run --cap-add SYS_ADMIN --device=/dev/fuse --security-opt apparmor:unconfined projectsidewalk/scraper:v6 <project-sidewalk-url>
    ```
    Where the `<project-sidewalk-url>` looks like `sidewalk-columbus.cs.washington.edu` if you want data from Columbus. If you visit that URL, you will see a dropdown menu with a list of publicly deployed cities that you can pull data from.
1. Right now the data is stored in a temporary directory in the Docker container. You could set up a shared volume for it, but for now you can just copy the data over using `docker cp <container-id>:/tmp/download_dest/ <local-storage-location>`, where `<local-storage-location>` is the place on your local machine where you want to save the files. You can find the `<container-id>` using `docker ps -a`.

Optional flags (accepted both by `DownloadRunner.py` and, appended after the positional args, by the Docker entrypoint):
* `--all-panos` — download *images* for panos that users visited but never labeled. This does not affect depth, which always covers every pano (see [Depth Maps](#depth-maps)).
* `--skip-depth` — skip the GSV depth-map phase (on by default; see [Depth Maps](#depth-maps)).
* `--max-runtime MINUTES` — stop starting new downloads/requests after this much wall time (the daily cron uses this; it exists to keep a run inside its daily slot, see [#38](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/38)).
* `--min-depth-runtime MINUTES` — reserve the last MINUTES of `--max-runtime` for the depth phase when there is unresolved depth work, so an image backlog can't starve the depth backfill (see [Depth Maps](#depth-maps) for the exact semantics). Default 0 (no reservation); **the recommended production crontab value is `--min-depth-runtime 60`**. Ignored without `--max-runtime` or with `--skip-depth`. Beware: if the reservation meets or exceeds `--max-runtime`, the run downloads **no images** (it prints a loud `WARNING`).
* `--max-depth-requests N` — stop the depth phase after N metadata requests (useful to throttle the initial backfill).

For the nightly production crontab, append `--max-runtime` sized to the cron slot plus the recommended depth reservation, e.g.:
```
docker run --cap-add SYS_ADMIN --device=/dev/fuse --security-opt apparmor:unconfined \
  projectsidewalk/scraper:v6 <project-sidewalk-url> <user@host:/remote/path> <port> \
  --max-runtime 360 --min-depth-runtime 60
```

Additional settings can be configured for `DownloadRunner.py` in the configuration file `config.py`. 
* `thread_count` - the number of threads you wish to run in parallel. As this uses asyncio and is an I/O task, the higher the count the faster the operation, but you will need to test what the upper limit is for your own device and network connection.
* `proxies` - if you wish to use a proxy when downloading, update this dictionary with the relevant details, otherwise leave as is and no proxy will be used. 
* `headers` - this is a list of real headers that is used when making requests. You can add to this list, edit it, or leave as is. 

### Imagery sources

The downloader dispatches each pano to a source-specific module based on the `source` field from `/adminapi/panos`. Per-source modules live in the `downloaders/` package.

* **Google Street View (`gsv`)** — no configuration needed; stitches tiles from the undocumented CBK endpoint.
* **Mapillary (`mapillary`)** — downloads the original-resolution equirectangular image via the [Graph API v4](https://www.mapillary.com/developer/api-documentation). Requires a client token.
  1. Create a token at <https://www.mapillary.com/dashboard/developers> (the default read scopes are sufficient).
  2. Export it as `MAPILLARY_ACCESS_TOKEN` before running. Examples:
     ```bash
     # Local
     export MAPILLARY_ACCESS_TOKEN='MLY|...'
     python3 DownloadRunner.py <sidewalk-fqdn> <storage-dir>

     # Docker (pass the variable through to the container)
     docker run -e MAPILLARY_ACCESS_TOKEN --cap-add SYS_ADMIN --device=/dev/fuse \
       --security-opt apparmor:unconfined \
       projectsidewalk/scraper:v6 <sidewalk-fqdn>
     ```

Panos with any other `source` value are skipped with a warning.

## Cropper

`CropRunner.py` creates crops of the accessibility features from the downloaded GSV panoramas images via label data from Project Sidewalk, provided by their API.

Usage:
```python
python CropRunner.py [-h] (-d D | -f F) [-s S] [-o O] [--mark-label]
```
* To fetch label metadata from webserver or a file, use respectively (mutually exclusive, required):
  * ``-d <project-sidewalk-url>``
  * ``-f <path-to-label-metadata-file>`` (`.csv` or `.json`)
* ``-s <path-to-panoramas-dir>`` (optional). Specify if using a different directory containing panoramas. Panoramas are used to crop the labels.
* ``-o <path-of-crop-dir>`` (optional). Specify if want to set a different directory for crops to be stored. `crop.log` is written here too.
* ``--mark-label`` (optional, debugging aid). Draws a dot at the label position in every crop. Off by default — the crops are ML training data, and a synthetic marker painted over the feature of interest is exactly what a model would learn instead of the feature.

Crops are **3:2**, centered on the label, sized by **crop sizing rule v2** and written to `<crop-dir>/<label_type_id>/<label_id>.jpg` at `min(window, 1440)` px wide — never upscaled. The window is an *angle*: the camera-to-label distance formula is evaluated in the 6656-px pano height its constants were fit on, scaled back to the pano's own pixels, scaled up ×2.5 and clamped to 8°–90°. Before v2 it was fed native pixels, so the same ramp asked for a window 1.86–4.09× different depending only on the panorama's resolution, and the largest panoramas got the tightest crops — see [the rule v2 report](reports/2026-08-19-crop-sizing-v2.md). Crop windows wrap across the equirectangular seam (the left and right image edges are the same place in the world) and shift to stay inside the image at the top and bottom, so a crop never contains synthetic black padding. In a six-city census of 438,410 labels, 1.52% cross the seam; before this was fixed every one of those crops carried a black bar.

A shifted crop still contains its label, but not at the center. Those are counted separately in the run summary (`shifted_vertically`) and logged with their offset, so a consumer that assumes centering can see how many it got. In the same census only two labels needed a shift, and both were corrupt rows (see below).

Two preflights reject a label rather than produce a quietly wrong crop:

* **Pano dimensions.** If the label metadata carries pano dimensions that disagree with the image on disk, the label is skipped with a warning. This is a **store-integrity** check: the metadata describes the pano as it is served now, the image was stitched to whatever the API reported when it was downloaded, so a disagreement means the store is stale (or, on the Mapillary path, that `thumb_original_url` served a different size than was recorded). It does **not** detect a label whose `pano_x`/`pano_y` went stale under a pano that was re-served at a new resolution — those dimensions are a per-pano value and get refreshed along with the pano, so such a row looks perfectly consistent. Separating those needs the click→pano replay, tracked in [#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54).
* **Label position.** A `pano_y` outside the image is skipped, because there is no way to recover it: the poles are not adjacent, so clamping produces clean imagery of a place the label is not in. `pano_x` is deliberately *not* checked — column 0 and column `pano_width` are the same place in the world, so any value is read correctly by the seam wrap, and labels storing `pano_x == pano_width` exactly do exist and crop fine.

The metadata dimensions are read from `pano_width`/`pano_height`, or from `width`/`height` for the older CSV export shape (`samples/metadata-seattle.csv`). Those latter names are generic, so if you supply your own CSV where `width`/`height` mean something else — a canvas or bounding box — every row will be skipped as a dimension mismatch. The skip is loud and counted, so this shows up as a number rather than as bad crops.

**Re-running does not regenerate existing crops.** A crop already on disk is the resume marker and is never re-cut, so a store cropped before the seam fix keeps its black-padded crops, and one cropped before crop sizes became deterministic holds a mix of (for example) 503- and 504-px crops for the same predicted size. Delete the crops you want re-cut.

As an example:
```python
python CropRunner.py -d sidewalk-columbus.cs.washington.edu -s /sidewalk/columbus/panos/ -o /sidewalk/columbus/crops/
```

The run writes a rotating `crop.log` into the crop directory, prints a per-outcome summary, and **exits 1 if any label errored** (a corrupt pano, a malformed metadata row, a failed write) so a cron wrapper can alert. The three skip outcomes are *not* errors and do not affect the exit code: a label waiting on a pano that hasn't been downloaded yet (the pano store is scraped independently and legitimately lags the label list), and the two preflight rejections above. Those are metadata the run declined to trust, not work it got wrong. Errors are retried on the next run.

**Note** Crops produced before the `--mark-label` flag existed all carry a burned-in dark-red dot at the label position: marking used to be a `MARK_LABEL = True` constant at the top of the file and was on for every run. If you are reusing an older crop set for training, that dot sits directly over the feature of interest and is exactly what a model will learn instead of the feature. Re-crop rather than reuse.

**Note** You will likely want to filter out labels where `disagree_count > agree_count`. These are based on human-provided validations from other Project Sidewalk users. This is not written in the code by default. There is also an option for a filter that is even more strict. This of course has the tradeoff of using less data, so this depends on the the needs of your project: more data vs more accurate data. To do this, you would query the `/v2/access/attributesWithLabels` API endpoint for the city you're looking at. Then you would only include labels where the `label_id` is also present in the attributesWithLabels API. This is a more aggressive filter that removes labels from some users that we suspect are providing low quality data based on some heuristics.

**Note** We have noticed some error in the y-position of labels on the panorama (first observed Apr 2023). The candidate root cause is now diagnosed — the click→pano mapping corrects for camera heading but not for per-pano camera tilt, see [SidewalkWebpage#4784](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4784) — and [#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54) tracks measuring the effect at crop level in this repo, with a correction to follow if the measurement confirms it. (An earlier note here referred to an "alternative cropper" in development; that effort was abandoned and #54 supersedes it.)

## Log analyzer

`log_analyzer/analyze.py` monitors the nightly scrape across every city. It pulls each city's `log.csv` off the pano store over SFTP and flags the ones that look broken. Nothing is downloaded by the scraper itself — this is an ops tool you run from a workstation or a cron box, not something the Docker image needs.

It needs only `pandas` (already in `requirements.txt`) plus the `sftp` client binary (`openssh-client`).

Connection settings are read from the environment, or the matching flag. Host and base path are required and have no defaults — a wrong default would silently analyze the wrong store:

| Variable | Flag | |
|---|---|---|
| `PS_SFTP_HOST` | `--host` | **required** — host, or an `~/.ssh/config` `Host` alias |
| `PS_SFTP_BASE` | `--base` | **required** — remote directory holding the per-city folders |
| `PS_SFTP_USER` | `--user` | optional — omit when the ssh config supplies it |
| `PS_SFTP_PORT` | `--port` | optional — omit for 22 |
| `PS_SFTP_KEY`  | `--key`  | optional — omit to let ssh choose (ssh config / agent) |

```bash
export PS_SFTP_HOST=... PS_SFTP_BASE=... PS_SFTP_USER=... PS_SFTP_PORT=... PS_SFTP_KEY=~/.ssh/...

python3 log_analyzer/analyze.py                    # download all city logs, then analyze
python3 log_analyzer/analyze.py --no-download      # re-analyze the local cache
python3 log_analyzer/analyze.py --city seattle-wa  # one city
python3 log_analyzer/analyze.py --stale-days 5     # custom staleness threshold
```

Setting up an `~/.ssh/config` `Host` alias is the tidiest option: with the user, port, and key declared there, only `PS_SFTP_HOST` and `PS_SFTP_BASE` are needed.

Exit status is `1` when any city has a CRITICAL issue, so cron's mail-on-failure does the alerting. Downloaded logs are cached in `log_analyzer/logs/` (gitignored).

`log_analyzer/cities.csv` maps `city_id` → display name; each `city_id` must match that city's folder name on the pano store exactly. Add a row when a new city is deployed.

**Checks:**

| Level | Condition |
|-------|-----------|
| 🔴 CRITICAL | Log download failed, or the file is missing/empty/unparseable |
| 🔴 CRITICAL | Last log entry is more than `--stale-days` days old (default 3) |
| 🟡 WARNING | `image_fail` growing by ≥20/day (7-day average) — new panos failing |
| 🟡 WARNING | Zero new images for 30 consecutive days, after a period that had some (regression) |
| 🟡 WARNING | A recent run took >3× the historical median runtime |
| 🟡 WARNING | ≥3 of the last 7 runs ended early (blank columns) |
| 🔵 INFO | Multiple runs logged on the same calendar day |

A healthy mature city looks like: `image_success` small or zero most days, stable `image_fail`, `image_skip` ≈ `image_total`. The column layout the analyzer parses is the 18-field table under [Ops notes](#ops-notes); blank fields are read as missing data, never as `0`, so a run that crashed can't be mistaken for a quiet one. `log.csv` is written without a header — the header row in production files is added by hand during city setup — so the analyzer accepts files with or without one.

The analyzer uses `sftp -b -` (batch mode via stdin) rather than `scp` because the store runs a restricted SFTP subsystem that doesn't speak the SCP wire protocol; newer `scp` clients default to SFTP-over-SSH and fail with `mtime.sec not present`.

## Definitions of variables found in APIs

### Downloader: /adminapi/panos
| Attribute      | Definition                                                                     |
|----------------|--------------------------------------------------------------------------------|
| pano_id        | A unique ID, provided by Google, for the panoramic image                       |
| width          | The width of the pano image in pixels                                          |
| height         | The height of the pano image in pixels                                         |
| lat            | The latitude of the camera when the image was taken                            |
| lng            | The longitude of the camera when the image was taken                           |
| camera_heading | The heading (in degrees) of the center of the image with respect to true north |
| camera_pitch   | The pitch (in degrees) of the camera with respect to horizontal                |
| source         | The source of the imagery (gsv, mapillary, etc)                                |


### Cropper: /adminapi/labels/cvMetadata
You won't need most of this data in your work, but it's all here for reference. Everything through `notsure_count` might be useful, then there are a few that are duplicates from the API described above, then everything starting with `canvas_width` probably won't matter for you.

| Attribute       | Definition |
|-----------------| ------------- |
| label_id        | A unique ID for each label (within a given city), provided by Project Sidewalk |
| gsv_panorama_id | A unique ID, provided by Google, for the panoramic image [same as /adminapi/panos] |
| source          | The source of the imagery (gsv, mapillary, etc) [same as /adminapi/panos] |
| label_type_id   | An integer ID denoting the type of label placed, defined in the chart below |
| pano_x          | The x-pixel location of the label on the pano, where top-left is (0,0) |
| pano_y          | The y-pixel location of the label on the pano, where top-left is (0,0) |
| agree_count     | The number of "agree" validations provided by Project Sidewalk users |
| disagree_count  | The number of "disagree" validations provided by Project Sidewalk users |
| notsure_count   | The number of "not sure" validations provided by Project Sidewalk users |
| pano_width      | The width of the pano image in pixels [same as /adminapi/panos] |
| pano_height     | The height of the pano image in pixels [same as /adminapi/panos] |
| camera_heading  | The heading (in degrees) of the center of the image with respect to true north [same as /adminapi/panos] |
| camera_pitch    | The pitch (in degrees) of the camera with respect to horizontal [same as /adminapi/panos] |
| canvas_width    | The width of the canvas where the user placed a label in Project Sidewalk |
| canvas_height   | The height of the canvas where the user placed a label in Project Sidewalk |
| canvas_x        | The x-pixel location where the user clicked on the canvas to place the label, where top-left is (0,0) |
| canvas_y        | The y-pixel location where the user clicked on the canvas to place the label, where top-left is (0,0) |
| heading         | The heading (in degrees) of the center of the canvas with respect to true north when the label was placed |
| pitch           | The pitch (in degrees) of the center of the canvas with respect to _the camera's pitch_ when the label was placed |
| zoom            | The zoom level in the GSV interface when the user placed the label |


Note that the numbers in the `label_type_id` column correspond to these label types (yes, 8 was skipped! :shrug:):

| label_type_id | label type |
| ------------- | ------------- |
| 1 | Curb Ramp |
| 2 | Missing Curb Ramp |
| 3 | Obstacle in a Path |
| 4 | Surface Problem |
| 5 | Other |
| 6 | Can't see the sidewalk |
| 7 | No Sidewalk |
| 9 | Crosswalk |
| 10 | Pedestrian Signal |

## Suggested Improvements

* `CropRunner.py` - implement multi core usage when creating crops. Currently runs on a single core, most modern machines
  have more than one core so would give a speed up for cropping 10's of thousands of images and objects.

## Depth Maps
`DownloadRunner.py` downloads a depth map for every GSV pano by default, using the [streetlevel](https://github.com/sk-zk/streetlevel) library to fetch Google's photometa response (pass `--skip-depth` to turn this off). The depth payload itself is decoded in-repo: every streetlevel release through 0.12.11 misreads a header byte, which makes its parser crash on the ~1% of panos whose zenith is a modeled surface (tunnels, overpass soffits) — see [sk-zk/streetlevel#45](https://github.com/sk-zk/streetlevel/pull/45); once that fix ships, the bypass can be revisited. Not every pano has depth data — third-party and some older panos don't — so the phase saves it where available and records the outcome either way.

Artifacts and bookkeeping, relative to the storage root:

* `<first-2-chars-of-pano-id>/<pano_id>.depth.npz` — the depth map, stored next to the pano's `.jpg`. Load with numpy:
  ```python
  import numpy as np
  d = np.load("aB/aBcDeF....depth.npz")
  d["depth"]           # float32 (height, width) array, typically 256x512; distance from camera in meters; -1 = no plane (sky, or unmodeled)
  d["plane_indices"]   # uint8 (height, width): per-pixel index into the plane list below; 0 = no plane (exactly where depth is -1, checked on write)
  d["planes_n"]        # float32 (P, 3): plane normals, verbatim from Google's payload (pano-local frame; see below)
  d["planes_d"]        # float32 (P,): plane offsets; a plane is {p : p·n = d}, so its perpendicular camera distance is |d| / ||n||
  d["heading"]         # camera heading in radians (NaN if Google omitted it); likewise d["pitch"], d["roll"]
  d["format_version"]  # 3; version 2 lacked the three plane fields; absent means pre-mirror-fix (see below)
  ```

  The array shares the JPEG's orientation: column 0 of `d["depth"]` is the leftmost column of the pano image. (streetlevel's decoder delivers the payload x-mirrored relative to the imagery; we flip it back on write, and contract tests pin the decoder's end-to-end output orientation — both the ray-direction formula and the write order — so an upstream change fails CI instead of silently re-mirroring new artifacts; see [#58](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/58). **An artifact with no `format_version` field predates that fix and is horizontally flipped — see the migration note below.**) To sample the depth under a label position stored in the database:

  ```python
  col = int(pano_x / pano_width * d["depth"].shape[1]) % d["depth"].shape[1]
  row = min(int(pano_y / pano_height * d["depth"].shape[0]), d["depth"].shape[0] - 1)
  meters = d["depth"][row, col]
  ```

  The plane fields ([#56](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/56)) are the raw material for per-pano **camera height** and **ground tilt**: Google's depth is plane-based, and `depth` is derived from the planes via `depth[r, c] == |planes_d[i] / (v(r, c) · planes_n[i])|` for `i = plane_indices[r, c] > 0`, where `v(r, c)` is the unit ray at `θ = (h−r−0.5)/h·π`, `φ = (w−c−0.5)/w·2π + π/2`. That identity is CI-tested and is the operational definition of the normals' frame — `plane_indices` shares `depth`'s row/column order, and the normals are untouched by the mirror fix below. The writer also refuses to emit an artifact whose `plane_indices == 0` mask doesn't match its `depth == -1` mask, so the correspondence above holds by construction rather than by assumption.

  `downloaders/gsv.py` ships the reference derivations, `ground_plane_from_artifact(d)` and `camera_height_from_artifact(d)` (its `|d| / ||n||`, sign-insensitive). The ground plane is picked as the near-horizontal plane that most of the pano's *below-horizon* pixels land on — rows from `(h+1)//2` on, which are exactly those with `θ < π/2` (plain `h//2` for the even heights every real raster has). Both halves of that rule matter: ranking on verticality alone lets a few pixels of an overpass soffit or tunnel ceiling — flatter than any real cambered road — outrank tens of thousands of pixels of actual road, and the returned "camera height" then silently becomes the height of the ceiling. When no plane below the horizon qualifies, the helpers return `None` (or your `default`) rather than a confident wrong answer:

  ```python
  from downloaders.gsv import camera_height_from_artifact
  height_m = camera_height_from_artifact(d, default=2.5)  # per-pano camera height above the modeled ground
  ```

  (Truncation, not `round()`: each depth pixel covers a *range* of pano columns, and flooring picks the pixel containing the position; rounding would pick the pixel whose edge is nearest — a systematic half-pixel shift.) The payload is angular (~0.7°/pixel; the horizon at θ = π/2 falls midway between the two middle rows, not on a single row), so this scaling works for any pano resolution. Note the frame caveat: `pano_x` and the pano raster are both *heading-centred* (column 0 sits at compass bearing `pano_yaw − 180°`, the vehicle's forward direction at image centre), but the legacy pre-evolution-179 `sv_image_x` is *north-referenced* (`sv_image_x / 13312 × 360` is a true compass bearing). Mixing the legacy value with the raster or this array displaces a label by up to half a panorama — and by nothing at all on a pano that happens to face south, so a one-example sanity check can pass on the wrong convention.
* `depth_log.csv` — an append-only ledger (`pano_id,status`) of resolved outcomes: `saved` (artifact written) or `unavailable` (pano gone from Google, or no depth payload). Ledgered panos are never re-requested; transient network failures are *not* ledgered, so they retry on the next run. The artifacts on disk are the ground truth — deleting the ledger is safe and just makes the next run re-check everything (existing artifacts are re-registered without re-downloading).

**Migrating a pre-v2 store.** Any store scraped before the [#58](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/58) fix holds x-mirrored artifacts, and the scraper will never correct them on its own — existing artifacts are never re-fetched or rewritten. `migrate_depth_artifacts.py` detects and fixes them offline: it scans a storage root, flips every artifact whose `format_version` is missing or below 2, and stamps it, leaving v2 artifacts byte-for-byte untouched (idempotent, so re-running on a healthy store is a no-op):

```bash
python3 migrate_depth_artifacts.py /path/to/storage --dry-run   # count pre-v2 artifacts, change nothing
python3 migrate_depth_artifacts.py /path/to/storage             # rewrite them in place
```

There is **no offline migration from v2 to v3**: the plane fields v3 adds were never stored by the v2 writer, so they can only come from a re-fetch. A v2 artifact (only pre-[#56](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/56) dev/test runs produced any — no production store ever ran the depth phase) reaches v3 by deleting the artifact *and* its `depth_log.csv` row, which makes the next run re-request it. The plane fields cost roughly 10–30 KB per pano on top of v2's 50–200 KB.

The depth phase runs after the image phase, and the two share one `--max-runtime` budget — that flag bounds the whole run to its daily cron slot, and the slot doesn't care which phase spends the clock. Because images run first, a big image backlog (a mapathon influx — which is also exactly when many new panos want depth) could starve the backfill night after night. `--min-depth-runtime` (default 0, i.e. off; the production crontab should pass 60) counters that by reserving the tail of the budget for depth whenever `depth_log.csv` shows unresolved work: the image phase then stops *starting* new panos at `max-runtime − min-depth-runtime`. Three consequences worth knowing:

* **It is a reservation, not a hard floor on depth wall time.** A pano already downloading when the image share runs out finishes anyway (eating into the reserved slice), and depth still ends at `--max-runtime` — on light nights images finish early and depth also gets the slack.
* **It only applies while depth has work.** Once every GSV pano is resolved in `depth_log.csv`, no time is reserved and the image phase keeps the whole budget.
* **A reservation at or above `--max-runtime` zeroes the image phase.** The run then downloads **no images** and prints `WARNING: --min-depth-runtime (X) >= --max-runtime (Y); NO images will be downloaded this run`, so a misconfigured crontab shows up in cron mail instead of looking like ordinary budget exhaustion.

Use `--max-depth-requests` to cap the phase's request volume during backfill.

### Being a good citizen of Google's servers

The phase is serial — one metadata request in flight at a time, unlike the image phase's `thread_count` fan-out — and on top of that:

* **Requests stop when Google pushes back.** The photometa endpoint doesn't answer scraping pressure with an HTTP 429; it serves (or redirects to) a captcha/consent interstitial carrying a 200, which would otherwise look identical to one pano having a bad payload. A response hook spots those, and the phase stops for the run rather than spending the rest of its budget on a wall. Exhausting the retry policy against 429/5xx is treated the same way.
* **A circuit breaker** stops the phase after 25 consecutive transient failures, with escalating back-off (30 s / 2 min / 5 min) before it gives up. Nothing is concluded from a trip — every unresolved pano simply retries next run — but the run prints a loud warning breaking the failure streak down by cause (e.g. `24 storage, 1 network`) and naming the last error. Storage failures (a full or unmounted store) count toward the breaker too but skip the back-off — waiting cannot un-fill a disk — so read that breakdown before assuming a Google rate limit: `[Errno 28] No space left on device` points at the store, not the network. A run that stops on its `--max-runtime` or `--max-depth-requests` budget (or finishes its list) after failures prints a warning with the last error too, so a store that fills mid-run can't hide behind a budget stop.
* **`depth_min_request_interval`** in `config.py` sets a floor (with jitter) on the gap between depth requests. It defaults to `0.0`. Leave it there unless a canary run shows Google pushing back: the backfill is inherently a multi-month job, so pacing costs real weeks. **The throttle is per-process** — if several cities scrape concurrently from one box, the rate Google sees is this multiplied by however many runs overlap.

### Ops notes

* **Depth ignores `--all-panos`.** The image phase only downloads labeled panos unless you pass that flag, but depth always covers every GSV pano the server knows about — including ones nobody has labeled, and ones whose image download failed or was never attempted. It costs one metadata request per pano either way, and the goal is depth for the whole corpus. As a result the depth phase's pano count is normally larger than the image phase's; both are printed at startup.
* Unresolved panos are shuffled each run. Iteration order is otherwise stable, so a cluster of panos that fail every time would monopolise `--max-depth-requests` run after run and the backfill would never reach anything behind it.
* **The depth failure count in `log.csv` is not an alert signal.** It includes `unavailable` — a permanent, expected, non-actionable outcome — so the first backfill runs will show large failure numbers that are entirely normal. The success/failure/unavailable split is printed to stdout and `scrape.log` (which lives next to `log.csv` under the storage root); `log.csv` keeps its 18-column positional shape, so there was no room for a separate column.
* Storage or ledger write failures (a full or unmounted store) are treated as transient per-pano failures and retried next run — the phase deliberately never lets them escape. Even if a run does crash between phases, `log.csv` still gets a single full-width row: fields are accumulated in memory and written once in a `finally`, with completed phases' counts kept and never-finished phases left blank (not fake zeros).
* **`pano_id_log.csv` — the image phase's resume ledger** (`pano_id,downloaded`, mirroring `depth_log.csv`'s semantics). A row means the pano is *resolved* and is never re-attempted: `1` = image on disk (or a prior success), `0` = the source has nothing for this pano (no imagery at any zoom, unknowable dimensions) — a permanent verdict. Transient failures — network blips, a failed tile, a full store — leave **no row** and retry automatically on the next run. Deleting `0` rows (or the whole file) remains the manual force-retry lever; existing `.jpg`s are simply re-registered as skipped.
* **Unattempted panos are shuffled each run**, for the same reason depth shuffles. Because a transient failure leaves no ledger row, it keeps its place in the server's ordering, so a stable iteration order would re-attempt the same failing head block first every night and spend `--max-runtime` before reaching new work. Shuffling also means a source-clustered `/adminapi/panos` response can't starve whichever source sorts last.
* **Images are written through a `.part` file and renamed into place.** An existing `.jpg` *is* the resume marker, so a download killed mid-write would otherwise leave a truncated file that every later run reports as a completed success. A stray `*.jpg.part` on the store is debris from a killed run and is safe to delete; the next run rewrites it.
* **The `log.csv` columns.** Each run appends one row of 18 positional comma-separated fields (no header), parsed by the [log analyzer](#log-analyzer). Durations are whole minutes (rounded). Fields 2–6 describe the XML metadata phase, a stub since Google killed that endpoint in 2022 — kept so the column positions never shift:

  | # | field | notes |
  |---|-------|-------|
  | 1 | run start timestamp | `str(datetime.now())`, e.g. `2026-08-06 01:00:00.123456` |
  | 2 | metadata successes | always `0` (stub) |
  | 3 | metadata failures | always `0` (stub) |
  | 4 | metadata skipped | count of image-eligible panos (stub) |
  | 5 | metadata total processed | count of image-eligible panos (stub) |
  | 6 | metadata phase duration | effectively `0` (stub) |
  | 7 | image successes | |
  | 8 | image fallback successes | downloaded, but at a fallback resolution |
  | 9 | image failures | includes prior runs' permanent failures, seeded from `pano_id_log.csv`; a transient failure is not ledgered, so it is counted again if it fails again next run |
  | 10 | image skipped | includes panos already downloaded on previous runs, seeded likewise |
  | 11 | image total processed | sum of fields 7–10 |
  | 12 | image phase duration | |
  | 13 | depth successes | |
  | 14 | depth failures | includes permanent `unavailable` outcomes — see the bullet above |
  | 15 | depth skipped | panos already resolved in `depth_log.csv` |
  | 16 | depth total processed | sum of fields 13–15 |
  | 17 | depth phase duration | |
  | 18 | total run duration | |

* **Blank fields mark a crashed or stopped run.** A run that crashes (or is `docker stop`ped) still appends a full 18-field row: every phase that completed keeps its real counts, and every field from the first unfinished phase onward is blank — visibly missing data, never a fabricated `0`. A row that is only a timestamp means the run died before scraping started (most likely the pano-list fetch against the webserver failed). Blanks are new as of #49 — historical rows are all-integer — so readers must treat them as missing data (`pandas.read_csv` surfaces them as `NaN`, turning those columns `float64`) rather than feeding them to `int()`.

### What the depth product is (and isn't)

The depth map is Google's plane-based encoding decoded to a per-pixel distance grid — and it is **not a measurement of the scene**. Analysis of 409 payloads ([label-latlng-estimation#9](https://github.com/ProjectSidewalk/label-latlng-estimation/issues/9)) shows a constructed model of terrain plus extruded building footprints: >99% of pixels lie on near-flat or near-vertical surfaces, with none of the intermediate slopes (car roofs, tree canopy, pitched roofs) a real reconstruction would have. Consequences for anything built on it:

* **Vehicles, people, and vegetation are absent.** A ray aimed at a parked car passes through it and returns the ground behind — a distance *overestimate* that can't be detected from the depth alone, only from imagery.
* **Under a label, depth is close to plain trigonometry** — ~91% of ground pixels fall within 1 m of `camera_height / tan(depression)`. The payload's added value is terrain relief and rays that hit a facade.
* **Curb ramps sit ~0.15 m above the modeled road surface**, so rays overshoot them by roughly 0.5 m at typical label distances. That's a bias, not noise.
* **`-1` means "no plane"** — sky *and* anything unmodeled. It is not "very far away".
* **Building geometry drifts between captures** (facades from re-captures of the same street differ by a couple of meters), so don't treat facade distances as survey-grade.

## Tests
A `pytest` suite covers the depth phase (ledger semantics, error taxonomy, artifact format, budget flags), the positional `log.csv` contract shared by the writer and the [log analyzer](#log-analyzer), and the Docker entrypoint's flag forwarding. The tests are network-free (streetlevel is mocked) and need only the packages in `requirements.txt` plus `pytest`:

```bash
pip3 install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests
```

One module (`test_streetlevel_api.py`) does import the real streetlevel, to pin the handful of API details `downloaders/gsv.py` depends on — the mocked suite can't catch drift there, since the stub accepts any arguments. It skips itself if streetlevel isn't installed (its `pyfrpc` dependency needs a C compiler). Tests that assert POSIX file modes or run the bash entrypoint skip on Windows; CI runs everything on Ubuntu 22.04.

## Old Code We've Removed
In PR [#26](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/26), we removed some old code. Some was related to our Tohme paper from 2014, some had to do with using depth maps for cropping images. Given that no one seems to be using the Tohme code (those on our team don't even know how it works) and Google has removed access to their depth data API, we removed this code in Apr 2023. We are hoping that this will simplify the repository, making it easier to make use of our newer work, while making it easier to maintain the code that's actually being used.

In [#39](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/39) (Aug 2026), we removed the rest of the legacy depth pipeline: the XML metadata downloader (the `cbk?output=xml` endpoint it relied on died in 2022) and the `decode_depthmap` binary. Depth maps are downloaded via the streetlevel library instead — see [Depth Maps](#depth-maps).

If any of this code ever needs to be revived, it exists in the git history, and can be found in the PRs linked above!
