# sidewalk-panorama-tools

[![Tests](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/actions/workflows/tests.yml/badge.svg)](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/actions/workflows/tests.yml)

Python tools that turn [Project Sidewalk](https://github.com/ProjectSidewalk/SidewalkWebpage)'s
crowdsourced accessibility labels into computer-vision-ready datasets: they download the underlying
street-level panoramas (Google Street View and Mapillary) plus GSV depth maps, then crop out the
labeled features (curb ramps, obstacles, surface problems, …) for ML training.

```mermaid
flowchart LR
    A[Project Sidewalk server] -- pano list --> B[DownloadRunner.py]
    B -- "panos + depth maps" --> C[(storage dir)]
    A -- label metadata --> D[CropRunner.py]
    C --> D
    D --> E[crops, by label type]
```

| Script | Purpose |
|--------|---------|
| [`DownloadRunner.py`](DownloadRunner.py) | Downloads every panorama a Project Sidewalk city has labels on, plus GSV depth maps. Resumable; designed to run nightly. |
| [`CropRunner.py`](CropRunner.py) | Cuts a crop around each label using the label's pixel position on the pano. |

The downloader is actively used in production; the cropper is functional but less exercised
(a new version is in the works), so cropper bugs may go unnoticed longer.

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Downloader](#downloader)
- [Depth maps](#depth-maps)
- [Cropper](#cropper)
- [Running with Docker](#running-with-docker)
- [Data reference](#data-reference)
- [Development](#development)
- [Project history](#project-history)

## Requirements

- **Python 3.10+** and the packages in `requirements.txt`. Everything is pure Python; Linux, macOS,
  and Windows all work. One caveat: [streetlevel](https://github.com/sk-zk/streetlevel) (used for
  depth maps) depends on `pyfrpc`, whose prebuilt wheel coverage is spotty — if pip falls back to
  building it from source you'll need a C compiler.
- **~2 GB RAM.** Stitched panoramas are large (up to 16384×8192), and processing them can crash
  very-low-memory machines.
- Docker is **optional** — it's how we deploy the nightly scraper, not a prerequisite.
  See [Running with Docker](#running-with-docker).

## Quick start

```bash
git clone https://github.com/ProjectSidewalk/sidewalk-panorama-tools.git
cd sidewalk-panorama-tools
pip3 install -r requirements.txt

# Download panos + depth maps for a city
python3 DownloadRunner.py sidewalk-columbus.cs.washington.edu ./panos

# Crop the labeled features out of them
python3 CropRunner.py -d sidewalk-columbus.cs.washington.edu -s ./panos -o ./crops
```

The first argument is a Project Sidewalk server domain. Visit any deployment (e.g.
<https://sidewalk-columbus.cs.washington.edu>) and use the city dropdown to see the public cities
you can pull from.

Heads-up before your first big run: a full city is tens to hundreds of GB, the downloader
[never retries a pano it has logged as failed](#what-gets-written), and the cropper
[draws a dot on every crop by default](#the-mark_label-flag).

## Downloader

```
python3 DownloadRunner.py <sidewalk-server-fqdn> <storage-path> [options]
```

| Argument / flag | Meaning |
|-----------------|---------|
| `<sidewalk-server-fqdn>` | Project Sidewalk server to fetch the pano list from, e.g. `sidewalk-columbus.cs.washington.edu`. |
| `<storage-path>` | Directory to store the panos (created if missing). |
| `-c <csv>` | Read pano metadata from a CSV instead of the server's `/adminapi/panos`. Must contain the same columns (notably `pano_id` and `source`). |
| `--all-panos` | Also download *images* for panos users visited but never labeled. Does not affect depth, which always covers every GSV pano. |
| `--skip-depth` | Skip the [depth-map phase](#depth-maps) (on by default). |
| `--max-runtime MINUTES` | Stop starting new downloads/requests after this much wall time (our nightly cron uses this). The image and depth phases share this one budget. |
| `--max-depth-requests N` | Stop the depth phase after N metadata requests (throttle for the initial backfill). |

### Configuration (`config.py`)

| Setting | Meaning |
|---------|---------|
| `thread_count` | Parallel connections for tile downloads. I/O-bound, so higher is generally faster — test what your machine and network tolerate. |
| `proxies` | Proxy for all requests. Leave the placeholder values as-is for no proxy. |
| `headers_list` | Pool of real browser headers; each tile request picks one at random. Edit or extend freely. |
| `depth_min_request_interval` | Floor (seconds) between depth-map requests. See [being a good citizen](#being-a-good-citizen-of-googles-servers). |

### Imagery sources

Each pano is dispatched to a source-specific module in `downloaders/` based on the `source` field
from `/adminapi/panos`. Panos with an unsupported source are skipped with a warning (and not marked
as processed, so a later run with support can pick them up).

- **Google Street View (`gsv`)** — no configuration needed; stitches 512×512 tiles from the
  undocumented CBK endpoint.
- **Mapillary (`mapillary`)** — downloads the original-resolution equirectangular image via the
  [Graph API v4](https://www.mapillary.com/developer/api-documentation). Requires a client token:
  1. Create one at <https://www.mapillary.com/dashboard/developers> (default read scopes suffice).
  2. Export it as `MAPILLARY_ACCESS_TOKEN` before running:
     ```bash
     export MAPILLARY_ACCESS_TOKEN='MLY|...'
     python3 DownloadRunner.py <sidewalk-fqdn> <storage-dir>
     ```

### What gets written

```
<storage-path>/
├── xB/                      # panos sharded by the first 2 chars of pano_id
│   ├── xBcD….jpg            # stitched equirectangular panorama
│   └── xBcD….depth.npz      # GSV depth map, where available
├── pano_id_log.csv          # image ledger: pano_id,downloaded (1|0)
├── depth_log.csv            # depth ledger: pano_id,saved|unavailable
└── log.csv                  # one 18-column row per run (positional; parsed by our log-analyzer tooling)
```

A debug log (`scrape.log`) is written to the *current working directory*, not the storage path.

**Resume semantics:** any pano with a row in `pano_id_log.csv` — success *or* failure — is never
re-attempted, which keeps nightly runs fast but means transient network failures stick. To retry
failed panos, delete their `downloaded = 0` rows (or the whole file: panos whose `.jpg` already
exists are re-registered as skipped without re-downloading).

## Depth maps

`DownloadRunner.py` downloads a depth map for every GSV pano by default, using the
[streetlevel](https://github.com/sk-zk/streetlevel) library to fetch and decode Google's photometa
response (pass `--skip-depth` to turn this off). Not every pano has depth data — third-party and
some older panos don't — so the phase saves it where available and records the outcome either way.

The artifact is stored next to the pano's `.jpg` and loads with numpy:

```python
import numpy as np
d = np.load("xB/xBcD….depth.npz")
d["depth"]    # float32 (height, width) array, typically 256x512; meters from camera; -1 = sky/infinitely far
d["heading"]  # camera heading in radians (NaN if Google omitted it); likewise d["pitch"], d["roll"]
```

Note that Google's depth appears to be synthesized from elevation data and building footprints
rather than measured directly, so treat it as approximate near fine structures.

`depth_log.csv` is an append-only ledger (`pano_id,status`) of resolved outcomes: `saved` (artifact
written) or `unavailable` (pano gone from Google, or no depth payload). Ledgered panos are never
re-requested; transient network failures are *not* ledgered, so they retry on the next run. The
artifacts on disk are the ground truth — deleting the ledger is safe and just makes the next run
re-check everything (existing artifacts are re-registered without re-downloading).

The depth phase runs after the image phase and shares its `--max-runtime` budget, so on a fresh
city images download first and depth backfills incrementally across daily runs. Use
`--max-depth-requests` to cap the phase's request volume during backfill.

### Being a good citizen of Google's servers

The phase is serial — one metadata request in flight at a time, unlike the image phase's
`thread_count` fan-out — and on top of that:

- **Requests stop when Google pushes back.** The photometa endpoint doesn't answer scraping
  pressure with an HTTP 429; it serves (or redirects to) a captcha/consent interstitial carrying a
  200, which would otherwise look identical to one pano having a bad payload. A response hook spots
  those, and the phase stops for the run rather than spending the rest of its budget on a wall.
  Exhausting the retry policy against 429/5xx is treated the same way.
- **A circuit breaker** stops the phase after 25 consecutive transient failures, with escalating
  back-off (30 s / 2 min / 5 min) before it gives up. Nothing is concluded from a block — every
  unresolved pano simply retries next run — but the run prints a loud warning, so check for a rate
  limit before the next one.
- **`depth_min_request_interval`** in `config.py` sets a floor (with jitter) on the gap between
  depth requests. It defaults to `0.0`. Leave it there unless a canary run shows Google pushing
  back: the backfill is inherently a multi-month job, so pacing costs real weeks. **The throttle is
  per-process** — if several cities scrape concurrently from one box, the rate Google sees is this
  multiplied by however many runs overlap.

### Ops notes

- **Depth ignores `--all-panos`.** The image phase only downloads labeled panos unless you pass
  that flag, but depth always covers every GSV pano the server knows about — including ones nobody
  has labeled, and ones whose image download failed or was never attempted. It costs one metadata
  request per pano either way, and the goal is depth for the whole corpus. As a result the depth
  phase's pano count is normally larger than the image phase's; both are printed at startup.
- Unresolved panos are shuffled each run. Iteration order is otherwise stable, so a cluster of
  panos that fail every time would monopolize `--max-depth-requests` run after run and the backfill
  would never reach anything behind it.
- **The depth failure count in `log.csv` is not an alert signal.** It includes `unavailable` — a
  permanent, expected, non-actionable outcome — so the first backfill runs will show large failure
  numbers that are entirely normal. The success/failure/unavailable split is printed to stdout and
  `scrape.log`; `log.csv` keeps its 18-column positional shape, so there was no room for a separate
  column.
- Storage or ledger write failures (a full or unmounted store) are treated as transient per-pano
  failures and retried next run. They must never escape the phase: `DownloadRunner.py` writes the
  depth and total-duration columns *after* it returns, so a crash here would leave a 12-field
  `log.csv` line where the analyzer expects 18.

## Cropper

`CropRunner.py` cuts a crop around each Project Sidewalk label using the label's pixel position on
the downloaded pano. Crop size is estimated from the label's vertical position (a proxy for
distance from the camera).

<img src="samples/sample_crop.jpg" width="300" alt="Example crop of a curb showing a missing curb ramp">

```
python3 CropRunner.py (-d <sidewalk-server-fqdn> | -f <metadata-file>) [-s <pano-dir>] [-o <crop-dir>]
```

| Flag | Meaning |
|------|---------|
| `-d <fqdn>` | Fetch label metadata from the server's `/adminapi/labels/cvMetadata`. Mutually exclusive with `-f`; one is required. |
| `-f <file>` | Read label metadata from a local `.csv` or `.json` file instead (see `samples/`). |
| `-s <dir>` | Directory of panos downloaded by `DownloadRunner.py`. Default: `/tmp/download_dest/`. |
| `-o <dir>` | Output directory. Crops are written as `<crop-dir>/<label_type_id>/<label_id>.jpg`. Default: `/crops/`. |

### The `MARK_LABEL` flag

By default `CropRunner.py` **draws a gray dot at the label point on every crop** (`MARK_LABEL = True`
at the top of the script). That's useful for eyeballing label placement, but you almost certainly
want it **off** for ML training data — edit the constant before a real run.

### Filtering low-quality labels

You will likely want to filter out labels where `disagree_count > agree_count` — these counts come
from human validations by other Project Sidewalk users. The code does not do this by default.

A stricter option: query the city's `/v2/access/attributesWithLabels` API endpoint and keep only
labels whose `label_id` appears there too. That filter also removes labels from users we suspect
are providing low-quality data, at the cost of less data — the right trade-off depends on whether
your project needs more data or more accurate data.

### Known limitations

- **Small y-position errors.** We've noticed some error in the y-position of labels on the pano —
  either a bug in the GSV API or metadata Google isn't providing. The errors are relatively small
  and in the y-direction; a cropper that corrects for them is in development, but this version
  works well in the meantime.
- Crops near the pano's left/right edge don't wrap around the 360° seam; the out-of-bounds region
  is padded with black.
- Cropping is single-core; parallelizing it would speed up jobs with tens of thousands of labels.

## Running with Docker

Docker is **not required** to use these tools ([quick start](#quick-start) covers plain-Python
usage). We use it to *deploy* the downloader as a nightly cron job, where it buys three things: a
pinned Ubuntu 22.04 / Python 3.10 baseline, a guaranteed build environment for streetlevel's
`pyfrpc` dependency, and an `sshfs` mount that streams downloads straight to a remote storage
server — that last one is why the container needs FUSE privileges.

Build the image from the repo root:

```bash
docker build -t projectsidewalk/scraper:v6 .
```

**Local storage** — bind-mount a host directory over the container's download path (no special
privileges needed):

```bash
docker run -v "$PWD/panos:/tmp/download_dest" projectsidewalk/scraper:v6 <sidewalk-server-fqdn> [options]
```

(Without the `-v` mount, downloads land inside the container at `/tmp/download_dest` and you'd have
to fish them out with `docker cp`.)

**Remote storage over sshfs** — pass an sshfs remote and port after the server FQDN, and grant the
FUSE-related flags (needed *only* in this mode):

```bash
docker run --cap-add SYS_ADMIN --device=/dev/fuse --security-opt apparmor:unconfined \
  projectsidewalk/scraper:v6 <sidewalk-server-fqdn> user@storage-host:/remote/path <ssh-port> [options]
```

The mount authenticates with an SSH private key at `/app/id_rsa` — place it at the repo root as
`id_rsa` (it's `.gitignore`d) before building. Note that this bakes the key into the image, so
don't push such an image to a registry.

To download Mapillary panos, pass the token through: `docker run -e MAPILLARY_ACCESS_TOKEN ...`.

All `DownloadRunner.py` options (`--all-panos`, `--skip-depth`, `--max-runtime`,
`--max-depth-requests`) are accepted after the positional arguments in either mode.

## Data reference

Definitions of the fields returned by the two Project Sidewalk API endpoints these tools consume.

### Downloader: `/adminapi/panos`

| Attribute | Definition |
|-----------|------------|
| pano_id | A unique ID, provided by Google, for the panoramic image |
| width | The width of the pano image in pixels |
| height | The height of the pano image in pixels |
| lat | The latitude of the camera when the image was taken |
| lng | The longitude of the camera when the image was taken |
| camera_heading | The heading (in degrees) of the center of the image with respect to true north |
| camera_pitch | The pitch (in degrees) of the camera with respect to horizontal |
| source | The source of the imagery (gsv, mapillary, etc) |

### Cropper: `/adminapi/labels/cvMetadata`

You won't need most of this data in your work, but it's all here for reference. Everything through
`notsure_count` might be useful, then there are a few that are duplicates from the API described
above, then everything starting with `canvas_width` probably won't matter for you.

| Attribute | Definition |
|-----------|------------|
| label_id | A unique ID for each label (within a given city), provided by Project Sidewalk |
| gsv_panorama_id | A unique ID, provided by Google, for the panoramic image [same as /adminapi/panos] |
| source | The source of the imagery (gsv, mapillary, etc) [same as /adminapi/panos] |
| label_type_id | An integer ID denoting the type of label placed, defined in the chart below |
| pano_x | The x-pixel location of the label on the pano, where top-left is (0,0) |
| pano_y | The y-pixel location of the label on the pano, where top-left is (0,0) |
| agree_count | The number of "agree" validations provided by Project Sidewalk users |
| disagree_count | The number of "disagree" validations provided by Project Sidewalk users |
| notsure_count | The number of "not sure" validations provided by Project Sidewalk users |
| pano_width | The width of the pano image in pixels [same as /adminapi/panos] |
| pano_height | The height of the pano image in pixels [same as /adminapi/panos] |
| camera_heading | The heading (in degrees) of the center of the image with respect to true north [same as /adminapi/panos] |
| camera_pitch | The pitch (in degrees) of the camera with respect to horizontal [same as /adminapi/panos] |
| canvas_width | The width of the canvas where the user placed a label in Project Sidewalk |
| canvas_height | The height of the canvas where the user placed a label in Project Sidewalk |
| canvas_x | The x-pixel location where the user clicked on the canvas to place the label, where top-left is (0,0) |
| canvas_y | The y-pixel location where the user clicked on the canvas to place the label, where top-left is (0,0) |
| heading | The heading (in degrees) of the center of the canvas with respect to true north when the label was placed |
| pitch | The pitch (in degrees) of the center of the canvas with respect to _the camera's pitch_ when the label was placed |
| zoom | The zoom level in the GSV interface when the user placed the label |

### Label types

The numbers in the `label_type_id` column correspond to these label types (yes, 8 was skipped! :shrug:):

| label_type_id | label type |
|---------------|------------|
| 1 | Curb Ramp |
| 2 | Missing Curb Ramp |
| 3 | Obstacle in a Path |
| 4 | Surface Problem |
| 5 | Other |
| 6 | Can't see the sidewalk |
| 7 | No Sidewalk |
| 9 | Crosswalk |
| 10 | Pedestrian Signal |

## Development

A `pytest` suite covers the depth phase (ledger semantics, error taxonomy, artifact format, budget
flags), the positional `log.csv` contract that our log-analyzer tooling parses, and the Docker
entrypoint's flag forwarding. The tests are network-free (streetlevel is mocked) and need only the
packages in `requirements.txt` plus `requirements-dev.txt`:

```bash
pip3 install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests
```

One module (`test_streetlevel_api.py`) does import the real streetlevel, to pin the handful of API
details `downloaders/gsv.py` depends on — the mocked suite can't catch drift there, since the stub
accepts any arguments. It skips itself if streetlevel isn't installed (its `pyfrpc` dependency
needs a C compiler). Tests that assert POSIX file modes or run the bash entrypoint skip on Windows;
CI runs everything on Ubuntu 22.04.

The `flag_panos/` directory holds a one-time-use internal web tool ([details](flag_panos/README.md))
and isn't part of the pipeline.

## Project history

- **Apr 2023** ([#26](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/26)): removed
  old code related to our 2014 Tohme paper and to depth-map-based cropping, after Google removed
  access to their depth data API.
- **Aug 2026** ([#39](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/39)):
  removed the rest of the legacy depth pipeline — the XML metadata downloader (the `cbk?output=xml`
  endpoint it relied on died in 2022) and the `decode_depthmap` binary. Depth maps now come from
  the streetlevel library instead — see [Depth maps](#depth-maps).

If any of this code ever needs to be revived, it exists in the git history via the PRs linked above.
