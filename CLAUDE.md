# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Python tooling that works with data from [Project Sidewalk](https://github.com/ProjectSidewalk/SidewalkWebpage) to (1) download Google Street View / Mapillary panoramas and their GSV depth maps, (2) crop sidewalk-accessibility labels out of those panoramas for ML/CV use, and (3) monitor the nightly scrape across all cities. `DownloadRunner.py` is actively maintained; `CropRunner.py` still works but is being replaced (bugs may linger).

## Common Commands

Everything runs from a virtualenv — **there is no Docker in this repo**. The image and its sshfs entrypoint were retired from the repo in Aug 2026 and from the production box on 2026-09-01 (`docs/history.md`); production is a crontab line per city calling `.venv/bin/python` directly, with the pano store mounted on the host.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip3 install -r requirements.txt

# Downloader (one city)
python3 DownloadRunner.py <fqdn> <storage-dir> [-c <csv>] [--all-panos] [--skip-depth] \
    [--max-runtime MINUTES] [--min-depth-runtime MINUTES] [--max-depth-requests N]

# Nightly queue (the whole fleet, one cron line). --dry-run prints the plan and takes no lock.
python3 scrape_queue.py --cities <manifest.csv> --store-root <dir> \
    [--max-runtime MINUTES] [--city-max-runtime MINUTES] [--only CITY_ID] [--no-rotate] [--dry-run] \
    -- [DownloadRunner args...]

# Cropper (exits 1 if any label errored; missing/untrusted panos alone are not an error)
python3 CropRunner.py (-d <fqdn> | -f <metadata.csv|.json>) -s <pano-dir> -o <crop-dir> [--mark-label]

# flag_panos JSON -> CSV, for one city (one-off tool; see flag_panos/README.md)
python3 flag_panos/json_to_csv.py --city <city> [--dir <dir>]

# Log analyzer (needs PS_SFTP_HOST + PS_SFTP_BASE; see docs/log-analyzer.md)
python3 log_analyzer/analyze.py [--no-download] [--city <city_id>] [--stale-days N]

# One-off migrator for pre-v2 depth artifacts
python3 migrate_depth_artifacts.py <storage-dir> [--dry-run]

# Regenerate the README's hero figure after a crop-geometry change
python3 assets/make_banner.py
```

Tests:

```bash
pip3 install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests
python3 -m pytest tests --cov --cov-report=term-missing   # what CI reports and gates on
```

CI (`.github/workflows/tests.yml`) runs the suite on Ubuntu 22.04 / Python 3.10, the production baseline. There is no linter configured.

## Documentation layout

The README is a front door only; the reference material lives in `docs/` and each page is linked from the README's documentation map. **When behaviour changes, the relevant `docs/` page and this file both need the edit.**

| Page | Covers |
|---|---|
| `docs/downloader.md` | install, options, runtime budgets, imagery sources, `config.py`, the nightly queue and its cron line |
| `docs/cropper.md` | crop geometry, preflights, outcome taxonomy, consumer warnings |
| `docs/depth.md` | artifact format, plane fields, ledger, migration, rate-limit behaviour, what depth is/isn't |
| `docs/ops.md` | storage layout, resume ledgers, the 18-column `log.csv`, crashed-run semantics |
| `docs/log-analyzer.md` | SFTP settings and the per-city checks |
| `docs/api-fields.md` | `/adminapi/panos` and `/adminapi/labels/cvMetadata` glossaries, label type IDs |
| `docs/testing.md` | what the suite covers |
| `docs/history.md` | removed code, and why |

`tests/test_docs.py` fails if a relative link or an anchor (cross-page **or** same-page) stops resolving, if a docs page is not linked from the README, or if a `docs/*.md` path cited in a Python comment **or in this file** goes missing — so the pointers above are checked, not decorative. Links are scanned over the joined page text, so one that hard-wraps across a newline is still checked.

**Coverage** is configured in `.coveragerc` (#57) and gated by its `fail_under`. Three things about it are load-bearing and easy to break by "simplifying":

- **The measured set is the production tree only** — the ten top-level/`downloaders/`/`log_analyzer/` modules. `reports/*` and `flag_panos/*` are omitted, the first because averaging a large body of frozen study tooling in would let the scraper's number move several points unnoticed, the second because its module scope writes files at import. `tests/test_coverage_config.py` asserts the resolved set exactly, so adding a module is a deliberate measure-or-omit decision.
- **`source` is written as `${SIDEWALK_COVERAGE_ROOT-.}`, not `.`** — coverage resolves a relative source against each *process's* CWD, and the runner tests spawn subprocesses with `cwd=tmp_path`. `tests/conftest.py`'s `pytest_configure` sets that variable (plus `COVERAGE_PROCESS_START` and `COVERAGE_FILE`) only when the parent is itself being measured. Break any of the three and `main()`, the argparse `type=` validators and the budget carve-out all read as dead: `DownloadRunner.py` drops from 97.6% to 87.9% with nothing failing.
- **`branch = True`** — the gap that motivated the gate was an `if` that only ever went one way (three of the log analyzer's six alert rules never fired while every line around them was green).

## Architecture

`DownloadRunner.py` (#52) and `CropRunner.py` (#48) both follow the same extracted #52.1 shape: `build_parser()` / `configure_logging()` / `run(...)` / `main(argv=None)` behind an `if __name__ == '__main__'` guard, so **importing either has no side effects** and tests can drive the real flow in-process. (`tests/test_log_analyzer.py` still lifts `LOG_CSV_FIELD_COUNT` out of `DownloadRunner.py` with `ast`, but that is now just avoiding the import cost, not a workaround for a module-scope `parse_args`.) Per-source download logic lives in the `downloaders/` package, which is also safe to import.

**DownloadRunner.py** — orchestrates a nightly run: fetch the pano list, download images, download depth, write one `log.csv` row.
1. Fetches the pano list from `/adminapi/panos` (or a CSV via `-c`), using a `requests` session with retries and explicit timeouts. Drops empty ids and `'tutorial'`. `filter_supported_sources()` keeps `gsv` and (when `MAPILLARY_ACCESS_TOKEN` is set) `mapillary`; filtered-out panos are deliberately **not** written to `pano_id_log.csv` so a later run can still pick them up.
2. `select_image_panos()` applies `--all-panos`. **This gates images only** — the depth phase always gets the full corpus, since depth is wanted for unlabelled panos too and costs the same either way.
3. Runs the image phase, then the depth phase, then appends one `log.csv` row in a `finally`. Both phases share `--max-runtime`; `--min-depth-runtime` carves a reservation out of the image phase's share so an image backlog can't starve the depth backfill (only taken when `count_unresolved_depth()` shows work pending).
4. Budgets use `time.monotonic()`, never the wall clock, so an NTP step or DST transition can't stretch or shrink them.
5. `SIGTERM` is translated into `sys.exit(143)` so a stop (`systemctl stop`, a cron timeout wrapper, an operator's kill) runs `finally` blocks instead of discarding the evidence row. Logging goes to `scrape.log` **on the pano store**, not the CWD — under cron the CWD is wherever the process happened to start, which is nowhere anyone looks.

**downloaders/** — per-source download logic; `download_pano()` dispatches on `pano_info['source']`.
- `gsv.py` stitches 512×512 tiles from Google's undocumented `cbk?output=tile` endpoint into one equirectangular JPEG. Determines a working zoom level (5 preferred, falling back to 3; a fully-black tile at both means no imagery), fans the tiles out concurrently via `aiohttp` with `backoff` retries, pastes them into a blank canvas sized per the server's width/height, and upscales zoom-3 panos with LANCZOS.
  - **`fallback_success` is "the stitch was upscaled", not "zoom == 3".** `download_single_pano` returns it when `_dims_at_zoom(w, h, zoom) != final_im_dimension`, which is exactly when `_stitch_tiles` had to LANCZOS the frame up to the reported dims. Those two rules disagree on the panos that matter: an old four-level pano (3328×1664) has max zoom 3, so zoom 3 *is* its native resolution and nothing was lost. Two tests hold that split apart and both kill a `zoom == 3` implementation. Nothing returned this verdict at all until #52, so `log.csv` column 8 was a constant `0` for every run before it — and `log_analyzer` sums that column into `daily_success`, so it was adding a permanent zero.
- `gsv.py` also owns the **depth phase** (`download_depth_maps`), which fetches depth via the `streetlevel` library's photometa call — one metadata request per unresolved pano.
- `mapillary.py` resolves `thumb_original_url` via the Graph API and downloads it. Requires `MAPILLARY_ACCESS_TOKEN`.

**CropRunner.py** — extracts per-label crops from downloaded panos.
1. Loads label metadata from `/adminapi/labels/cvMetadata` (`-d`), or a `.csv`/`.json` file (`-f`, case-insensitive extension). Both intakes dedupe on `label_id`; the CSV intake reads with `csv.DictReader`, not pandas (#72), so no field's type can depend on what the values happen to look like (the #46 bug class), and requires `REQUIRED_LABEL_COLUMNS` up front, so a header typo is one error naming the file rather than a `KeyError` 200k labels in.
2. Labels are **grouped by pano**, so each `<pano-dir>/<pano_id[:2]>/<pano_id>.jpg` is decoded exactly once for all its labels — a 13312×6656 pano is ~250 MB decoded and a 16384×8192 one 384 MB (`w × h × 3` bytes). Each label's window comes from `compute_crop_box()`: an integer `CropBox(left, top, width, height, shifted)` centered at `(pano_x, pano_y)`, **3:2**, where **x wraps at the equirectangular seam and y clamps by shifting**, so no crop ever contains synthetic black (#47). Width comes from `crop_window_width()` — **sizing rule v2**: `predict_crop_size()` normalised into the 6656-px frame its constants were fit on, ×`CROP_SIZE_SCALE`, clamped to `CROP_MIN_FOV_DEG`–`CROP_MAX_FOV_DEG` **as an angle** rather than as pixels. `downscale_for_storage()` then caps the cut window at `CROP_MAX_STORED_WIDTH` without ever upscaling it. Every constant is one measured number — see `reports/2026-08-19-crop-sizing-v2.md`; the residual (a y-only rule cannot know how large a ramp actually is) is RampNet #83.
   - **The rule version lives in the store, not in the run summary.** `write_rule_marker()` writes `<crop-dir>/crop_rule.json` (`CROP_RULE_VERSION` plus every constant) *before* cutting anything, and warns — never refuses — when it disagrees with what is there. A mixed store is the **ordinary** result of changing the rule, since existing crops are the resume marker and are never re-cut: run v2 over a v1 store and the new crops are 3:2 while the old stay square, with nothing on disk to say so. Stdout was where this used to go, which on a cron run is nowhere.
   - **`reports/scripts/crop_rule_v1.py` is the one frozen copy of the old rule**, used by the sizing study and by the two census tests whose reports were *measured* under v1. It is a record, not a rule anyone runs — the resolution defect in it is the subject of the v2 report and must not be "fixed".
3. Two preflights skip a label rather than emit a quietly wrong crop: a metadata/image **dims mismatch** (`dims_mismatch`) and a `pano_y` **outside the image** (`out_of_frame`).
4. Writes to `<crop-dir>/<label_type_id>/<label_id>.jpg` through `atomic_output_path`. Existing crops are the resume marker (`skipped_existing`) and are **never re-cut**, so a store cropped before #47 keeps its black-padded crops — delete them to pick the fix up. Rotating `crop.log` lands next to the crops, not the CWD.
5. `--mark-label` draws a dot at the label position **inside the crop**, never on the shared pano, and follows the label through both transforms. It is **off by default**: it was a `MARK_LABEL = True` module constant until #48, so every crop this tool produced before then has a (128, 0, 0) dot burned over the feature of interest.
6. **Nothing in the crop loop is fatal.** The counts reconcile on every path including re-runs — `success + skipped_existing + missing_pano + dims_mismatch + out_of_frame + errors == total` (`shifted_vertically` annotates a success, so it is deliberately *not* in that sum). A corrupt pano, malformed row, or failed write is one counted, logged error and retries next run. `main()` exits 1 if `errors` is nonzero — **not** on `missing_pano`/`dims_mismatch`/`out_of_frame`, which are metadata the run refused to trust rather than work it got wrong.

**scrape_queue.py** (#101) — the nightly driver: one cron line that walks a city manifest and starts each city as soon as the last one exits, replacing 53 hand-picked crontab slots. Same extracted shape as the runners (`build_parser()` / `configure_logging()` / `main(argv=None)` behind a `__main__` guard), so it imports inertly and tests drive `main()` in-process against a stand-in runner script.
1. **It replaced a ring, not a schedule.** Slots were staggered across the whole UTC day and had wrapped, so 32 of 53 cities ran 07:00–19:00 Pacific — the working day on the store and the app hosts, which are in Seattle regardless of where the city is. The cron line carries `CRON_TZ=America/Los_Angeles` so the fleet follows DST instead of drifting an hour against it twice a year.
2. **Two budgets that compose, and the composition is the point.** `--max-runtime` is the window and gates *starting* a city (never interrupts one, same rule as the image phase, #51); `--city-max-runtime` is passed through as the runner's own `--max-runtime` and hard-killed `--kill-grace` minutes later. The city gets `min(the two)`: the city cap alone lets the last city run past the window, and the remaining window alone lets the *first* city eat the whole night — the head-of-line problem serialising introduces.
3. **The kill is the backstop, not the mechanism.** SIGTERM first, because `DownloadRunner` translates it into `sys.exit(143)` and its `finally` still writes the `log.csv` row (#49); `SIGKILL` only if that is ignored.
4. **The lock is advisory (`flock`/`msvcrt.locking`), never an `O_EXCL` file**, and defaults to local disk rather than the store. The OS releases it when the holder dies — a lock that outlived a crash would silently stop the whole fleet, which is worse than the overlap it prevents — and the store is a network mount whose lock semantics are not guaranteed.
5. **A city the window never reached exits nonzero**, like a failure. cron's mail-on-failure is the only unattended alarm, and a fleet quietly completing 40 of 53 cities a night is exactly the silent failure #101 is about.
6. `--cities` has no default (deployment fact, same reasoning as `resolve_sftp`'s host/base), a `#`-prefixed row is skipped (replacing "comment the crontab line out"), and the manifest carries `fqdn` next to `city_id` because **there is no rule that derives one from the other** — `seattle-wa` is served by `sidewalk-sea`.

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

**`pano_id_log.csv` gates the image phase** — ids already in it are skipped on later runs. Note the caveat in `docs/ops.md`: a permanent verdict is ledgered and never retried, while a transient failure leaves no row.

**`depth_log.csv` gates the depth phase**, but only for permanent outcomes: `saved` and `unavailable` are ledgered and never retried; transient errors (including storage failures) are counted but **not** ledgered, so they retry next run. The artifact on disk is ground truth — deleting the ledger just makes the next run re-stat artifacts and re-request unresolved panos.

## Config

`config.py` holds `thread_count` (image-phase tile fan-out, default 8), a rotating `headers_list` (randomly picked per request), `proxies` (set to the `http://`/`https://` sentinels to disable), and `depth_min_request_interval` (seconds between depth metadata requests; 0 disables — leave it there unless a canary run shows Google pushing back, since the backfill is a multi-month job).

## Artifact storage (standing rule)

Every research and engineering artifact — datasets, annotations, measurement files, figures,
scripts, model outputs — lives in **GitHub (this repo or a sibling org repo) or the
`projectsidewalk` Hugging Face org, nothing else**. Personal cloud storage (Google Drive,
Dropbox, ad-hoc shared links) is easy to reach for in the moment but doesn't survive people
moving on: links rot, accounts close, and experiments have had to be re-run because artifacts
that felt accessible at the time were no longer findable later. Version-controlled, org-owned
homes are the only storage that outlives any one person's involvement. The bar: a fresh clone
plus the referenced HF dataset must reproduce every number in `reports/`.

## Things that are easy to get wrong

- **The three file intakes read with `csv`/`json`, and must not go back to pandas (#72).** `pandas` is a
  dev/ops dependency now (`requirements-dev.txt`, for `log_analyzer/` and `reports/scripts/`), and a test
  asserts no production module imports it. The battery in `tests/test_csv_intake.py` was **measured**
  against `pd.read_csv` before the swap, and three of those measurements are the reason it happened: a row
  with **one surplus field** made pandas consume the first column as the frame's index, so every field
  shifted and the real `pano_id` vanished — silently; **`has_labels` had no fixed type** (bool, int64,
  float64, or `str` for junk *and* for `' True '` with padding), and `select_image_panos` is a plain
  truthiness test, so the `str` cases silently defeated `--all-panos`; and **one blank cell retyped a whole
  column**, so a single missing `width` made every other row's width a float and the blank itself a `NaN`
  that `gsv`'s `is not None` guard cannot see. Blank cells become `None` in `DownloadRunner` and stay `''`
  in `CropRunner` — deliberate, and explained at both seams.
- **`print` and `logging` are two channels with different jobs — do not "unify" them.** `print` is the
  operator-facing run narrative and the warnings **cron mails**; `logging` is the durable per-item detail in
  `scrape.log` / `crop.log`. **A warning that matters goes to both**, which is the depth phase's pattern
  (`logging.error(...)` then `print(...)`), because stdout is how someone hears about it tonight and the log
  is what is still there next week. #52 item 6 read the mixture as inconsistency; it is mostly deliberate,
  `docs/downloader.md` leans on a `WARNING` reaching cron mail, and ~15 tests assert on `capsys`. A print-to-logging sweep
  would break all three. The one real violation — `filter_supported_sources` warning on stdout only — is
  fixed; `caplog` assertions now sit beside its `capsys` ones so a revert fails.
- **`log.csv` column 1 is a wall-clock stamp WITH its offset; every duration is `time.monotonic()`. Don't merge the two clocks (#101).** It was `str(datetime.now())` — a bare local reading, harmless only while every scraper host ran UTC, which is the assumption pinning the schedule to `America/Los_Angeles` removes. `log_analyzer` compares it against `datetime.now(timezone.utc)`, so a bare row on a non-UTC host is silently 7–8 h out, and `.days` floors, so that moves a city across the staleness threshold in *both* directions. `read_log` parses `format="ISO8601", utc=True`, which is also what lets one file hold both eras — without `utc=True` a mixed column comes back as object dtype and `analyze_city` dies on `.dt`, ending the report for every city after it. Symmetrically, the phase durations were wall-clock differences: the Pacific night window contains 02:00 local, so twice a year every city would report a run an hour longer than it was, and rule 4 warns at 3× the median.
- **`log.csv` is positional and headerless.** 18 comma-separated fields, blank-padded. Fields 2–6 are an XML-metadata stub kept at fixed values purely so column positions never shift (that endpoint died in 2022). Blank ≠ 0: blank means the phase never finished. The full table is in `docs/ops.md`; `LOG_CSV_FIELD_COUNT` and `log_analyzer/analyze.py`'s `LOG_COLUMNS` must move together, and a test asserts they do.
- **The depth failure count is not an alert signal.** It includes `unavailable`, a permanent and expected outcome, so early backfill runs show large, entirely normal failure numbers. The success/failure/unavailable split goes to stdout and `scrape.log`.
- **Depth artifacts are un-mirrored on write.** `streetlevel`'s decoder x-mirrors the payload relative to the pano JPEG; `_write_depth_artifact` flips it back (#58), so a consumer can index the stored array with `pano_x`/`pano_y` scaled by width/height, no correction needed. `tests/test_streetlevel_api.py` pins the decode's end-to-end column order so a streetlevel change fails CI rather than silently re-mirroring new artifacts.
- **Depth is not a measurement of the scene.** It's Google's plane-based model: vehicles, people, and vegetation are absent, `-1` means "no plane" (sky *or* anything unmodeled), and curb ramps sit ~0.15 m above the modeled road surface. See `docs/depth.md`'s "What the depth product is (and isn't)" before building anything on it.
- **Labels may have `disagree_count > agree_count`;** the cropper does **not** filter these by default. For stricter filtering, intersect `label_id` with `/v2/access/attributesWithLabels`.
- **There is small but real Y-axis error in label positions** on the pano — diagnosed as uncorrected per-pano camera tilt in the click→pano mapping (SidewalkWebpage#4784); #54 tracks measuring it at crop level here, with a correction to follow if confirmed.
- **The cropper's dims preflight checks the store, not the label's frame.** `pano_width`/`pano_height` is a per-pano value joined from current pano metadata, not a click-time snapshot — measured over 438,410 labels / 172,790 panos, *no* pano carries two frames (`reports/2026-08-10-crop-geometry-review.md`). So it catches a stale image on disk, and cannot catch a label whose `pano_x`/`pano_y` went stale under a re-served pano. Don't let a study assume otherwise: that separation needs the POV replay (#54).
- **`pano_x` is never bounds-checked and must not be.** Column 0 and column `pano_width` are the same place in the world, so the seam modulo reads any finite x correctly; production rows storing `pano_x == pano_width` exist and crop fine. `pano_y` *is* checked, because the poles are not adjacent and a clamp yields clean imagery of the wrong place.
- **`bulk_extract_crops`' counts have one non-disjoint key.** `success + skipped_existing + missing_pano + dims_mismatch + out_of_frame + errors == total`; `shifted_vertically` annotates a success instead of being its own bucket. Adding a bucket without adding it to that sum is how the invariant went stale before — a test asserts the sum from the dict, not from the docstring.

## Desk studies under `reports/scripts/` — six conventions that keep being rediscovered

These bit four scripts at once in the 2026-08-11 review; see `reports/2026-08-11-mapillary-census.md`.

- **Undefined is not zero, and `main()` must not format-spec it.** Analysis functions correctly return
  `None` for a percentage with a zero denominator, a sample sd of one value, a correlation against a
  constant series. `f"{None:.1f}"` then raises `TypeError` at the summary print — after all the compute
  and before `--write`. Print through `studyfmt.fmt` and build artifact values with `studyfmt.num`;
  there is one definition of each and no script may grow a local copy (a test asserts that). Writes use
  `allow_nan=False`, so a NaN reaching the dict aborts the run on its last line.
- **`rawlabels` pins `pano_id` to `str`, and must.** Mapillary image ids are all-numeric, so pandas
  infers `int64` for any Mapillary city — the #46 bug class the two runners already pin against. A
  merge between an int-keyed and a str-keyed frame matches nothing and reports zero coverage rather
  than failing.
- **Mapillary cities live in a separate cache directory.** Every study globs `*.csv` over a directory,
  so a Mapillary city dropped into `.cache/rawlabels/` silently joins the six-city GSV corpus and moves
  every committed artifact. `fetch_rawlabels.py` writes them to `.cache/rawlabels-mapillary/`.
- **Committed-artifact tests do not test code.** Pinning a finding against `reports/data/*.json` proves
  nothing about the function that produced it: the artifact was generated *by* the current code, so a
  revert stays green. Every finding needs a synthetic code-level test beside its corpus pin. Three
  mutation batteries in a row surfaced survivors of exactly this shape.
- **Every number in a report's prose is transcribed from a committed artifact, and a test says so.**
  Two counts hand-typed into `reports/2026-08-11-mapillary-census.md` §6 were wrong by 2× and 6×, and
  nothing about the surrounding sentences looked different for it — a report table is the one place in
  this repo where a plausible number has no compiler and no test. So the script computes the whole
  table, and `TestReportMatchesTheArtifact` asserts each value appears in the markdown. The same round
  found a filtered count (81,667 eligible) quoted as a raw one (82,769): **state which filter a count is
  under, or don't quote it.** Corollary for exclusion rules: **size the rule against the corpus it will
  be applied to, not only the one it was derived from.** `NoSidewalk` was left as an open question after
  a rule was derived on 267 Richmond labels that contain none of it; it is 82,769 labels and the largest
  arm in the six-city corpus the rule actually governs.
- **Two figures in one artifact must each name the frame they were computed on.**
  `click_noise_study.study()` put a matched sigma (referent-filtered, 335,712 labels) and every clustered
  sigma (all 436,348) in one dict and printed them in one column, and nothing recorded that they were
  100,636 labels apart — so a docstring, a test docstring and a report all quoted one against the other
  as a single comparison. The fix is not prose: `populations` names each side, lists the keys it covers,
  and a test asserts every emitted figure is claimed by exactly one side, so the *next* figure added
  cannot land unclaimed. Where the comparison is the point, compute the like-for-like cell too
  (`comparable_only`) rather than leaving a reader to assume the difference is the estimator — here it
  mostly was, but that was a measurement, not a given.

## The gold-standard annotation tool (`corpus_sample.py` → `annotation_tiles.py` → `annotation_subset.py` → `annotate_server.py`)

Four scripts feed each other, and the seams between them are load-bearing:

- **`corpus_sample.py`** draws the study corpus from a rawLabels frame. **Label identity is
  `(city, label_id)`, carried as `label_uid`** — `label_id` restarts at 1 in every deployment, and keying
  on it silently cost 314 labels of a 763-label draw. `pano_id` does *not* collide across cities, which is
  why the per-pano cap and the pano-wise tune/eval split are sound on it.
- **`annotation_tiles.py`** cuts one tile per label and emits **two** files. `tasks.json` is
  annotator-facing and carries no stored coordinate, no jitter, no tile origin and **no seed** — each of
  those recovers the answer, since the tile origin is `stored + jitter - size/2`. `geometry.json` carries
  all of them and is what the analysis uses to map a tile-space annotation back to pano coordinates.
  Tiles are cut at **60°** and the view opens at the whole cut: a tile of angular width F can only
  measure a displacement up to F/2, so a tight tile converts gross errors into `object-absent` and
  deletes the largest errors from the distribution being estimated.
- **Everything angular on that instrument must be angular, including the jitter.** It was `±40–80 px`
  until 2026-08-19, which is 1.5–2.9% of the tile on the 8192-height panos 641 of the 763 drawn labels
  sit on and 7.2–14.4% on the 1664s — so the one device whose job is to keep the stored point off the
  tile centre varied ~5× with resolution and was weakest on 84% of the corpus. It is now
  `JITTER_MIN_FRAC`/`JITTER_MAX_FRAC` of the tile (§4's numbers at the 20° cut §4 was written for),
  which also means changing `CUT_FOV_DEG` can no longer dilute it.
- **`measurable` has exactly one definition, `rawlabels.study_measurable`,** and **no script may read
  the corpus CSV's `measurable` column** — that is a snapshot of the rule at draw time and says 584
  where the live rule says 368. `annotation_subset.py --measurable-only` and
  `annotation_tiles.py --measurable-only` both call it; the latter used to read the column, which is
  the failure the former was written to prevent, one script upstream.
- **Protocol fields come from code, pixel fields come from the rendered file.** `FLAGS`, `FLAG_HELP`,
  `BOX_RULE` and `initial_view_fraction` are properties of the instrument, so
  `annotate_server.tasks_payload` sends them from `annotation_tiles` on every request and
  `annotation_subset.write_subset` refreshes them in the copies it writes. `cut_fov_deg` is the one
  exception: it describes the pixels, so it comes from whatever produced them. Taking the flag *list*
  from the file while taking its *help text* from code is the specific half-measure that shipped a
  queue offering three flags to a server that accepted four — the annotator had no key to press.
- **`annotation_subset.py`** narrows an already-rendered tile set, and is where the *current* referent
  rule is applied. Both its filters fail silently: a queue drawn from the wrong population or missing a
  flag looks perfectly well-formed.
- **`annotate_server.py`** serves tiles to `annotate.html` on loopback and writes one JSON per label per
  annotator. It refuses `geometry.json` **by name** — it sits beside the file that is served, and the
  natural static-file handler would publish the answer key at a guessable URL.

Amendment 1(e) forbids porting the webpage's render path into any of this: Study 1 compares stored
`pano_x`/`pano_y` against gold *in pano coordinates*, so a mapping sharing the projection under test would
make the study measure zero by construction. The tile transform is verified by round-trip against
directly-indexed pixels, never against another implementation.

**The corpus is 8 types; Study 1's measurable set is 4.** The referent rule (2026-08-13) excludes
Occlusion, Crosswalk, NoSidewalk, **Signal** and **Other** by type, plus eleven `(label_type, tag)`
pairs — leaving CurbRamp, NoCurbRamp, Obstacle and SurfaceProblem, 368 of the 763-label corpus. It is a
**placement-measurability** rule, not a corpus rule: the excluded types have real crop consumers and
Study 2 still sizes crops for them. What changed on 2026-08-13 is that they are no longer *annotated* —
if a referent has no located centre it has no tight extent either, so a gold box on one is as arbitrary
as a gold point. The rule is keyed on **pairs, not tags**: `height difference` is excluded under
SurfaceProblem (a run of pavement) and kept under Obstacle (a discrete step). Tags are optional, so the
rule is leaky by construction — 14% of Obstacle labels carry none — which is what the `no-extent` flag
is for, and that flag is **reported as its own bucket, never dropped from a denominator**.

The prereg's §7 is a **decision log**, not an amendment log — plain dated entries recording what changed
and *what was known at the time*, since the ordering (a filter fixed before any gold existed) is the only
part that cannot be reconstructed later. Old references resolve as Amendment 1/2/3 = 2026-08-11/12/13.
Note that changing the referent rule invalidates published artifacts computed under the old one: the
Mapillary census is deliberately **not** regenerated, and `TestTheCommittedRuleIsCurrentOrSuperseded`
fails if the live rule diverges from a committed artifact's recorded rule without the report saying so.

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

See `docs/api-fields.md` for the full field glossary for `/adminapi/panos` and `/adminapi/labels/cvMetadata`.

## Other directories

- `tests/` — pytest suite (network-free; `streetlevel` is stubbed). Covers the depth phase, the `log.csv` contract, the log analyzer, the depth migrator, the docs' internal links (`test_docs.py`), the README's hero figure (`test_make_banner.py`), the three file intakes as one contract (`test_csv_intake.py`), and the cropper (`test_crop_runner.py`: intake, the crop loop's failure taxonomy and count reconciliation, `predict_crop_size` pins). `test_streetlevel_api.py` imports the real `streetlevel` to pin API details and skips itself if it isn't installed. `test_coverage_config.py` pins `.coveragerc` itself — the measured set, and the settings whose loss shows up as a lower number rather than an error.
- `log_analyzer/` — the log analyzer plus `cities.csv`; `log_analyzer/logs/` is a gitignored local cache.
- `flag_panos/` — one-off web tool (HTML/JS) from the 2022 depth-endpoint outage. Not wired into the Python scripts; keep unless asked.
- `samples/` — reference CSV/JSON/XML and a sample pano+crop used for manual testing and as examples for the `-c`/`-f` flags.
