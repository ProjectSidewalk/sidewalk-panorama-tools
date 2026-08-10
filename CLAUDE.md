# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Python tooling that works with data from [Project Sidewalk](https://github.com/ProjectSidewalk/SidewalkWebpage) to (1) download Google Street View / Mapillary panoramas and their GSV depth maps, (2) crop sidewalk-accessibility labels out of those panoramas for ML/CV use, and (3) monitor the nightly scrape across all cities. `DownloadRunner.py` is actively maintained; `CropRunner.py` still works but is being replaced (bugs may linger).

## Common Commands

Build and run the downloader via Docker (the supported path):

```bash
docker build --no-cache --pull -t projectsidewalk/scraper:v6 .

# Basic: download to a tmp dir inside the container
docker run --cap-add SYS_ADMIN --device=/dev/fuse --security-opt apparmor:unconfined \
  projectsidewalk/scraper:v6 <sidewalk-server-fqdn>

# The entrypoint (DownloadRunnerDockerEntrypoint.sh) also supports:
#   <fqdn> <user@host:/remote/path> <port>     # sshfs-mount remote dest
#   ... --all-panos                            # include panos with no labels (images only)
#   ... --skip-depth                           # skip the depth phase
#   ... --max-runtime MINUTES                  # stop starting work after MINUTES
#   ... --min-depth-runtime MINUTES            # reserve the tail of --max-runtime for depth
#   ... --max-depth-requests N                 # cap depth metadata requests
```

Run scripts directly (outside Docker, Linux recommended):

```bash
pip3 install -r requirements.txt

# Downloader
python3 DownloadRunner.py <fqdn> <storage-dir> [-c <csv>] [--all-panos] [--skip-depth] \
    [--max-runtime MINUTES] [--min-depth-runtime MINUTES] [--max-depth-requests N]

# Cropper
python3 CropRunner.py (-d <fqdn> | -f <metadata.csv|.json>) [-s <pano-dir>] [-o <crop-dir>] [--mark-label]

# Log analyzer (needs PS_SFTP_HOST + PS_SFTP_BASE; see README's "Log analyzer")
python3 log_analyzer/analyze.py [--no-download] [--city <city_id>] [--stale-days N]

# One-off migrator for pre-v2 depth artifacts
python3 migrate_depth_artifacts.py <storage-dir> [--dry-run]
```

Tests:

```bash
pip3 install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests
```

CI (`.github/workflows/tests.yml`) runs the suite on Ubuntu 22.04 / Python 3.10, matching the Docker image. There is no linter configured.

## Architecture

`DownloadRunner.py` and `CropRunner.py` both follow the #52.1 shape: side-effect-free import, `build_parser()` / `configure_logging()` / `run()` / `main(argv=None)` seams, and a `__main__` guard — tests drive them in-process. Per-source download logic lives in the `downloaders/` package.

**DownloadRunner.py** — orchestrates a nightly run: fetch the pano list, download images, download depth, write one `log.csv` row.
1. Fetches the pano list from `/adminapi/panos` (or a CSV via `-c`), using a `requests` session with retries and explicit timeouts. Drops empty ids and `'tutorial'`. `filter_supported_sources()` keeps `gsv` and (when `MAPILLARY_ACCESS_TOKEN` is set) `mapillary`; filtered-out panos are deliberately **not** written to `pano_id_log.csv` so a later run can still pick them up.
2. `select_image_panos()` applies `--all-panos`. **This gates images only** — the depth phase always gets the full corpus, since depth is wanted for unlabelled panos too and costs the same either way.
3. Runs the image phase, then the depth phase, then appends one `log.csv` row in a `finally`. Both phases share `--max-runtime`; `--min-depth-runtime` carves a reservation out of the image phase's share so an image backlog can't starve the depth backfill (only taken when `count_unresolved_depth()` shows work pending).
4. Budgets use `time.monotonic()`, never the wall clock, so an NTP step or DST transition can't stretch or shrink them.
5. `SIGTERM` is translated into `sys.exit(143)` so `docker stop` runs `finally` blocks instead of discarding the evidence row. Logging goes to `scrape.log` **on the pano store**, not the CWD — in Docker the CWD is `/app` inside the container and would vanish on exit.

**downloaders/** — per-source download logic; `download_pano()` dispatches on `pano_info['source']`.
- `gsv.py` stitches 512×512 tiles from Google's undocumented `cbk?output=tile` endpoint into one equirectangular JPEG. Determines a working zoom level (5 preferred, falling back to 3; a fully-black tile at both means no imagery), fans the tiles out concurrently via `aiohttp` with `backoff` retries, pastes them into a blank canvas sized per the server's width/height, and upscales zoom-3 panos with LANCZOS.
- `gsv.py` also owns the **depth phase** (`download_depth_maps`), which fetches depth via the `streetlevel` library's photometa call — one metadata request per unresolved pano.
- `mapillary.py` resolves `thumb_original_url` via the Graph API and downloads it. Requires `MAPILLARY_ACCESS_TOKEN`.

**CropRunner.py** — extracts per-label crops from downloaded panos.
1. Loads label metadata from `/adminapi/labels/cvMetadata` (`-d`), or a `.csv`/`.json` file (`-f`, case-insensitive extension). Both intakes dedupe on `label_id`; the CSV intake dtype-pins `pano_id` to `str` and guards required columns (the #46 bug class).
2. Groups labels by pano so each `<pano-dir>/<pano_id[:2]>/<pano_id>.jpg` is decoded once, then cuts a square window per label via `compute_crop_box()`: integer `CropBox(left, top, size, shifted)` centered at `(pano_x, pano_y)`, **x wraps at the equirectangular seam, y clamps by shifting** — no crop ever contains synthetic black (#47). Crop size comes from `predict_crop_size()` — an experimentally-fit formula mapping pano-y to distance to crop size, clamped to `[50, 1500]` (replacement tracked in #32/#54).
3. Two preflights skip a label rather than emit a quietly wrong crop: a metadata/image **dims mismatch** (`dims_mismatch`) and a `pano_y` **outside the image** (`out_of_frame`).
4. Writes atomically to `<crop-dir>/<label_type_id>/<label_id>.jpg`; existing crops are the resume marker (`skipped_existing`) and are **never re-cut**, so a store cropped before #47 keeps its black-padded crops. Failures are per-label: counted, logged to `crop.log` (rotating, in the crop dir), and retried next run. `--mark-label` (default off) draws a dot at the label position in the crop — debugging only, since marked crops are poison as training data.

**log_analyzer/analyze.py** — ops monitoring for the nightly scrape; shares no code with the runners.
1. Reads `log_analyzer/cities.csv`, pulls each city's `log.csv` off the pano store with `sftp -b -` (the store's restricted SFTP subsystem doesn't speak the SCP wire protocol), and caches it in the gitignored `log_analyzer/logs/`. Connection settings come from `PS_SFTP_*` env vars or matching flags — host and base path are required with no defaults, since a wrong default would silently analyze the wrong store.
2. Parses the 18 positional columns by position (`LOG_COLUMNS`), tolerating a present-or-absent header: `write_log_csv_row` never writes one, and production files get theirs by hand at city setup. Blank fields stay `NaN` — a crashed run must never read as a quiet one — so every check guards against NaN rather than coercing to `int`.
3. Prints per-city issues at CRITICAL/WARNING/INFO and exits `1` if anything is CRITICAL, so cron's mail-on-failure alerts. Thresholds are module constants near the top.

**migrate_depth_artifacts.py** — offline, idempotent one-off that rewrites pre-v2 (x-mirrored, unversioned) depth artifacts into v2 column order. The scraper never revisits an existing artifact, so a store scraped before #58 keeps mirrored artifacts forever without this.

## Storage layout

Everything lives under the storage root, with two-char pano-id prefix sharding:

| Path | What |
|------|------|
| `<pano_id[:2]>/<pano_id>.jpg` | Stitched panorama |
| `<pano_id[:2]>/<pano_id>.depth.npz` | Depth artifact (see below) |
| `pano_id_log.csv` | Per-pano image ledger: `pano_id,downloaded` |
| `depth_log.csv` | Per-pano depth ledger: `pano_id,saved\|unavailable` |
| `log.csv` | One 18-column row per run |
| `scrape.log` | Rotating run log (10 MB × 3) |

**`pano_id_log.csv` gates the image phase** — ids already in it are skipped on later runs. Note the caveat in the README: a network failure is logged as a failure and won't be retried.

**`depth_log.csv` gates the depth phase**, but only for permanent outcomes: `saved` and `unavailable` are ledgered and never retried; transient errors (including storage failures) are counted but **not** ledgered, so they retry next run. The artifact on disk is ground truth — deleting the ledger just makes the next run re-stat artifacts and re-request unresolved panos.

## Config

`config.py` holds `thread_count` (image-phase tile fan-out, default 8), a rotating `headers_list` (randomly picked per request), `proxies` (set to the `http://`/`https://` sentinels to disable), and `depth_min_request_interval` (seconds between depth metadata requests; 0 disables — leave it there unless a canary run shows Google pushing back, since the backfill is a multi-month job).

## Things that are easy to get wrong

- **`log.csv` is positional and headerless.** 18 comma-separated fields, blank-padded. Fields 2–6 are an XML-metadata stub kept at fixed values purely so column positions never shift (that endpoint died in 2022). Blank ≠ 0: blank means the phase never finished. The full table is in README's "Ops notes"; `LOG_CSV_FIELD_COUNT` and `log_analyzer/analyze.py`'s `LOG_COLUMNS` must move together, and a test asserts they do.
- **The depth failure count is not an alert signal.** It includes `unavailable`, a permanent and expected outcome, so early backfill runs show large, entirely normal failure numbers. The success/failure/unavailable split goes to stdout and `scrape.log`.
- **Depth artifacts are un-mirrored on write.** `streetlevel`'s decoder x-mirrors the payload relative to the pano JPEG; `_write_depth_artifact` flips it back (#58), so a consumer can index the stored array with `pano_x`/`pano_y` scaled by width/height, no correction needed. `tests/test_streetlevel_api.py` pins the decode's end-to-end column order so a streetlevel change fails CI rather than silently re-mirroring new artifacts.
- **Depth is not a measurement of the scene.** It's Google's plane-based model: vehicles, people, and vegetation are absent, `-1` means "no plane" (sky *or* anything unmodeled), and curb ramps sit ~0.15 m above the modeled road surface. See README's "What the depth product is (and isn't)" before building anything on it.
- **Labels may have `disagree_count > agree_count`;** the cropper does **not** filter these by default. For stricter filtering, intersect `label_id` with `/v2/access/attributesWithLabels`.
- **There is small but real Y-axis error in label positions** on the pano — diagnosed as uncorrected per-pano camera tilt in the click→pano mapping (SidewalkWebpage#4784); #54 tracks measuring it at crop level here, with a correction to follow if confirmed.
- **The cropper's dims preflight checks the store, not the label's frame.** `pano_width`/`pano_height` is a per-pano value joined from current pano metadata, not a click-time snapshot — measured over 438,410 labels / 172,790 panos, *no* pano carries two frames (`reports/2026-08-10-crop-geometry-review.md`). So it catches a stale image on disk, and cannot catch a label whose `pano_x`/`pano_y` went stale under a re-served pano. Don't let a study assume otherwise: that separation needs the POV replay (#54).
- **`pano_x` is never bounds-checked and must not be.** Column 0 and column `pano_width` are the same place in the world, so the seam modulo reads any finite x correctly; production rows storing `pano_x == pano_width` exist and crop fine. `pano_y` *is* checked, because the poles are not adjacent and a clamp yields clean imagery of the wrong place.
- **`bulk_extract_crops`' counts have one non-disjoint key.** `success + skipped_existing + missing_pano + dims_mismatch + out_of_frame + errors == total`; `shifted_vertically` annotates a success instead of being its own bucket. Adding a bucket without adding it to that sum is how the invariant went stale before — a test asserts the sum from the dict, not from the docstring.

## Label Type IDs

Used in both APIs and as the crop output subdirectory name. Note 8 is intentionally skipped.

| id | type |
|----|------|
| 1 | Curb Ramp |
| 2 | Missing Curb Ramp |
| 3 | Obstacle in a Path |
| 4 | Surface Problem |
| 5 | Other |
| 6 | Can't see the sidewalk |
| 7 | No Sidewalk |
| 9 | Crosswalk |
| 10 | Pedestrian Signal |

See README.md for the full field glossary for `/adminapi/panos` and `/adminapi/labels/cvMetadata`.

## Other directories

- `tests/` — pytest suite (network-free; `streetlevel` is stubbed). Covers the depth phase, the `log.csv` contract, the log analyzer, the depth migrator, and the Docker entrypoint's flag forwarding. `test_streetlevel_api.py` imports the real `streetlevel` to pin API details and skips itself if it isn't installed.
- `log_analyzer/` — the log analyzer plus `cities.csv`; `log_analyzer/logs/` is a gitignored local cache.
- `flag_panos/` — one-off web tool (HTML/JS) from the 2022 depth-endpoint outage. Not wired into the Python scripts; keep unless asked.
- `samples/` — reference CSV/JSON/XML and a sample pano+crop used for manual testing and as examples for the `-c`/`-f` flags.
