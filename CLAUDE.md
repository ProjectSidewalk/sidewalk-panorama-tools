# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository. It holds what applies everywhere; each subsystem's rules are in a file that Claude Code loads only when a matching file is read - see "Guidance layout" below - so **before changing a module, read its rules file** (they are plain Markdown; open them by path).

## Purpose

Python tooling that works with data from [Project Sidewalk](https://github.com/ProjectSidewalk/SidewalkWebpage) to (1) download Google Street View / Mapillary panoramas and their GSV depth maps, (2) crop sidewalk-accessibility labels out of those panoramas for ML/CV use, and (3) monitor the nightly scrape across all cities. `DownloadRunner.py` is actively maintained; `CropRunner.py` still works but is being replaced (bugs may linger).

## Common Commands

Everything runs from a virtualenv — **there is no Docker in this repo**. The image and its sshfs entrypoint were retired from the repo in Aug 2026 and from the production box on 2026-09-01 (`docs/history.md`); production is **one** crontab line running `scrape_queue.py` (#101) from `.venv/bin/python`, with the pano store mounted on the host — `docs/ops.md`, Operating the production host.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip3 install -r requirements.txt

# Downloader (one city)
python3 DownloadRunner.py <fqdn> <storage-dir> [-c <csv>] [--all-panos] [--skip-depth] \
    [--max-runtime MINUTES] [--min-depth-runtime MINUTES] [--max-depth-requests N]

# Collaborators with SFTP credentials from the PS team: pull already-scraped panos instead of scraping (#30).
# The default (no flag) downloads from the provider yourself. CITY_ID = the city's folder on the store; no default.
PS_SFTP_HOST=... PS_SFTP_BASE=... python3 DownloadRunner.py <fqdn> <storage-dir> --from-store <city_id> [--with-depth]

# Nightly queue (the whole fleet, one cron line). --dry-run prints the plan and takes no lock. Refuses (exit 5) a
# --store-root without its .pano-store marker (#161; docs/ops.md, "The store marker").
python3 scrape_queue.py --cities <manifest.csv> --store-root <dir> \
    [--max-runtime MINUTES] [--city-max-runtime MINUTES] [--only CITY_ID] [--no-rotate] [--single-pass] [--dry-run] \
    -- [DownloadRunner args...]

# The alarm channel (#141): cron's mail-on-output rule for a host with no MTA. Runs COMMAND, streams its output
# through, and hands the capture to --sink (production: `aws sns publish ...`) when there is any. Exit code is
# COMMAND's; 4 = COMMAND exited 0 and the sink failed; 127 = COMMAND could not start (reported through the sink).
python3 cron_notify.py --sink '<shell command>' [--name NAME] [--only-on-failure] [--max-bytes N] [--log FILE] -- COMMAND...

# Cropper (exits 1 if any label errored; missing/untrusted panos alone are not an error). --force re-cuts existing
# crops (#83); a -o that looks like the production canvas-capture crop store, or that crop_rule.json records as
# another city's, is refused: exit 3, nothing written. --city is required (label_id restarts per city; #159)
# and must be an active row of log_analyzer/cities.csv (exit 2 otherwise). --sizing-rule v3 is opt-in (#32).
python3 CropRunner.py (-d <fqdn> | -f <metadata.csv|.json>) -s <pano-dir> -o <crop-dir> --city <city_id> [--mark-label] [--force] [--sizing-rule {v2,v3}]

# flag_panos JSON -> CSV, for one city (one-off tool; see flag_panos/README.md)
python3 flag_panos/json_to_csv.py --city <city> [--dir <dir>]

# Log analyzer (needs PS_SFTP_HOST + PS_SFTP_BASE; PS_ROSTER_HOST optional, defaults to sidewalk-sea then -chicago; see docs/log-analyzer.md)
python3 log_analyzer/analyze.py [--no-download] [--city <city_id>] [--stale-days N]

# Does a live deployment still serve every cvMetadata field CropRunner needs? (#135) Outside tests/ on
# purpose - the suite is network-free. No default host. Exits 0 ok / 1 field missing / 3 unreadable.
python3 check_cvmetadata_schema.py --host <fqdn>            # or set PS_CVMETADATA_HOST

# One-off migrator for pre-v2 depth artifacts
python3 migrate_depth_artifacts.py <storage-dir> [--dry-run]

# One-off, move-only migrator for a pre-#159 flat crop store into <crop-dir>/<city>/ (#159). Never replaces a
# file: a collision is listed and both are left. Exits 0 done / 1 anything left in place / 2 usage / 3 refused.
python3 migrate_crop_store.py <crop-dir> --city <city_id> [--dry-run]

# The display copy beside every wide pano (#115); idempotent, resumable. Neither downloader writes one any
# more (switched off 2026-09-09) - this is the only writer that CREATES one, and it runs only when you run
# it. refetch_panos refreshes a copy already on the store after a swap, but never creates one, and deletes
# it if that refresh fails (#122).
python3 downscale_panos.py <storage-dir> [--dry-run] [--max-runtime MINUTES] [--max-width PX] [--min-width PX]

# One-off repair pass for fover-era panos (#73). Never backfills, never downgrades; see docs/ops.md
python3 reports/scripts/pano_y_histogram.py <city-fqdn> --write-worklist
python3 refetch_panos.py <storage-dir> (--worklist <csv.gz> | --from-store) [--dry-run] \
    [--fixed-after YYYY-MM-DD] [--max-runtime MINUTES] [--min-pano-interval SECONDS] [--measure]

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
| `docs/downloader.md` | install, options, runtime budgets, imagery sources, `config.py`, the nightly queue and its cron line, pulling from the pano store |
| `docs/cropper.md` | crop geometry, preflights, outcome taxonomy, consumer warnings |
| `docs/depth.md` | artifact format, plane fields, ledger, migration, rate-limit behaviour, what depth is/isn't |
| `docs/ops.md` | storage layout, resume ledgers, the 19-column `log.csv`, crashed-run semantics, the `fover` repair pass, operating the production host (deploy, rollback, adding a city, the missing-MTA alarm gap) |
| `docs/log-analyzer.md` | SFTP settings, the per-city checks, and the depth backfill report |
| `docs/api-fields.md` | `/adminapi/panos` and `/adminapi/labels/cvMetadata` glossaries, label type IDs, and the live schema tripwire |
| `docs/testing.md` | what the suite covers |
| `docs/history.md` | removed code, and why |

`tests/test_docs.py` fails if a relative link or an anchor (cross-page **or** same-page) stops resolving, if a docs page is not linked from the README, if a `docs/*.md` path cited in a Python comment **or in any guidance file** (this one, `.claude/rules/*.md`, a nested `CLAUDE.md`) goes missing, or if a sentence stating `log.csv`'s width (here, the README or `docs/`) disagrees with `LOG_CSV_FIELD_COUNT` — so the pointers above are checked, not decorative. Links are scanned over the joined page text, so one that hard-wraps across a newline is still checked.

**Guidance layout.** This file is loaded into every session. The per-module rules are in files Claude Code loads only on demand: a `.claude/rules/*.md` file when a file matching the `paths:` globs in its frontmatter is read, a nested `CLAUDE.md` when a file under its directory is read. `@import` lines were deliberately not used - they load at startup and free nothing. `tests/test_docs.py` checks that every rules file is path-scoped, that every glob matches at least one file, and that the startup set stays under Claude Code's size limit, so a typo cannot quietly turn a rules file into startup context or into nothing.

| File | Loads when a matching file is read | Covers |
|---|---|---|
| `.claude/rules/downloader.md` | `DownloadRunner.py`, `downloaders/`, `config.py`, their tests, `docs/downloader.md` | the nightly run, the image phase, the per-source verdict contracts, store mode, the two breakers |
| `.claude/rules/depth.md` | `downloaders/gsv.py`, `migrate_depth_artifacts.py`, the depth tests, `docs/depth.md` | adaptive pacing, the block latch, the artifact format and its migrator |
| `.claude/rules/cropper.md` | `CropRunner.py`, `migrate_crop_store.py`, `check_cvmetadata_schema.py`, the crop tests, `docs/cropper.md`, `docs/api-fields.md` | the crop pipeline end to end, the label-type table |
| `.claude/rules/tilt.md` | `CropRunner.py`, `reports/scripts/tilt_*.py`, the tilt tests and data | the #54 frames and signs, the adjudication data, the crop-time correction |
| `.claude/rules/queue.md` | `scrape_queue.py`, `cron_notify.py`, their tests, `docs/ops.md` | the nightly queue and the alarm channel |
| `.claude/rules/store-repair.md` | `refetch_panos.py`, `downscale_panos.py`, `downloaders/common.py`, their tests | the refetch gates, the display-copy sweep, the width tripwire |
| `log_analyzer/CLAUDE.md` | anything under `log_analyzer/` | the analyzer's rules and the depth backfill report |
| `reports/scripts/CLAUDE.md` | anything under `reports/scripts/` | the desk-study conventions and the annotation tool |

**Coverage** is configured in `.coveragerc` (#57) and gated by its `fail_under`. Three things about it are load-bearing and easy to break by "simplifying":

- **The measured set is the production tree only** — every top-level/`downloaders/`/`log_analyzer/` module (a count is not written here: it has been stale twice). `reports/*` and `flag_panos/*` are omitted, the first because averaging a large body of frozen study tooling in would let the scraper's number move several points unnoticed, the second because its module scope writes files at import. `tests/test_coverage_config.py` asserts the resolved set exactly, so adding a module is a deliberate measure-or-omit decision.
- **`source` is written as `${SIDEWALK_COVERAGE_ROOT-.}`, not `.`** — coverage resolves a relative source against each *process's* CWD, and the runner tests spawn subprocesses with `cwd=tmp_path`. `tests/conftest.py`'s `pytest_configure` sets that variable (plus `COVERAGE_PROCESS_START` and `COVERAGE_FILE`) only when the parent is itself being measured. Break any of the three and `main()`, the argparse `type=` validators and the budget carve-out all read as dead: `DownloadRunner.py` drops from 97.6% to 87.9% with nothing failing. Every `omit` pattern is anchored to the same root (`${SIDEWALK_COVERAGE_ROOT-.}/tests/*`, ...) for the same reason, and `test_coverage_config.py` asserts it: a relative omit is anchored to the child's CWD too, so a child that loads `tests/conftest.py` from `tmp_path` put it in the measured set and took CI from 99.46% to 98.07% with nothing failing (#171 review). The same hook, unconditionally and *before* that early-returning coverage block, points the children's `TMPDIR`/`TEMP`/`TMP` at a per-session dir (#165), so they never touch the host's real pacing lock, block latch, width-alarm latch or queue lock. Keep it above the `return`: in an ordinary measured run that `return` never fires (the hook sets `COVERAGE_PROCESS_START` itself, below it), but a session started with the variable already exported, or with no `coverage` installed, does return early, and would then skip the redirect.
- **`branch = True`** — the gap that motivated the gate was an `if` that only ever went one way (three of the log analyzer's six alert rules never fired while every line around them was green; that was the count at the time of the #57 measurement, and rule 4's `median > 0` guard turned out to be a fourth such `if` — see the log_analyzer notes below).

## Architecture

`DownloadRunner.py` (#52) and `CropRunner.py` (#48) both follow the same extracted #52.1 shape: `build_parser()` / `configure_logging()` / `run(...)` / `main(argv=None)` behind an `if __name__ == '__main__'` guard, so **importing either has no side effects** and tests can drive the real flow in-process. (`tests/test_log_analyzer.py` still lifts `LOG_CSV_FIELD_COUNT` out of `DownloadRunner.py` with `ast`, but that is now just avoiding the import cost, not a workaround for a module-scope `parse_args`.) Per-source download logic lives in the `downloaders/` package, which is also safe to import.

The per-module detail lives in the rules files named in "Guidance layout"; the summaries here say what each module is and which file to read before changing it.

**DownloadRunner.py** - orchestrates one city's nightly run: fetch the pano list from `/adminapi/panos` (or a CSV), run the image phase then the depth phase under one monotonic `--max-runtime`, append one 19-column `log.csv` row in a `finally`, and write `--run-summary-file` (the stop reasons plus the run *conditions*, #161) for the queue. Store mode (`--from-store`, #30) pulls already-scraped panos over SFTP instead of scraping. Rules: `.claude/rules/downloader.md`.

**downloaders/** - per-source imagery: `gsv.py` (tile stitching at the photometa-resolved zoom, the tile push-back breaker, and the depth phase), `mapillary.py`, `panoramax.py`, `store_sftp.py`, and `common.py` (shared image primitives, the display-copy switch, the width tripwire). Which answers are permanent ledger verdicts and which raise is a contract, and every entry in it is a measurement. Rules: `.claude/rules/downloader.md`; the depth phase's pacing, block latch and artifacts: `.claude/rules/depth.md`.

**CropRunner.py** - cuts one 3:2 crop per label from the stored panos into `<crop-dir>/<city>/<label_type_id>/<label_id>.jpg`: intake (cvMetadata or a file), grouping by pano, two preflights and a content check, the sizing rule (v2 default, v3 opt-in), a seam-wrapping window, atomic writes, a provenance manifest and a sticky rule marker. Nothing in the crop loop is fatal and the counts reconcile on every path. Rules: `.claude/rules/cropper.md` (also `migrate_crop_store.py`, `check_cvmetadata_schema.py` and the label-type table); the #54 tilt geometry and its crop-time correction: `.claude/rules/tilt.md`.

**scrape_queue.py** and **cron_notify.py** - the nightly driver (one cron line walks the city manifest; two composing budgets; an advisory lock; extra passes for cities that stopped on a budget; the roster cross-check; the store marker; an exit code that fails the night on any run condition) and the alarm channel that delivers that exit code through `--sink` on a host with no MTA. Rules: `.claude/rules/queue.md`.

**log_analyzer/analyze.py** - ops monitoring: pulls each city's `log.csv` off the store, applies the per-city rules, reports the depth backfill, and cross-checks `cities.csv` against the live roster with its own copy of the roster code. Rules: `log_analyzer/CLAUDE.md`.

**Repair and migration passes** - `refetch_panos.py` (re-fetch behind four refusal gates; the `fover` pass it was built for does not run), `downscale_panos.py` (the display-copy sweep; the feature is switched off and this is its only creator), `migrate_depth_artifacts.py` (pre-v2 artifacts into v2 order) and `migrate_crop_store.py` (a pre-#159 flat crop store into `<crop-dir>/<city>/`). Rules: `.claude/rules/store-repair.md` for the first two, `.claude/rules/depth.md` and `.claude/rules/cropper.md` for the migrators.

## Storage layout

Everything lives under the storage root, with two-char pano-id prefix sharding:

| Path | What |
|------|------|
| `<pano_id[:2]>/<pano_id>.jpg` | Stitched panorama |
| `<pano_id[:2]>/<pano_id>.depth.npz` | Depth artifact (see below) |
| `<pano_id[:2]>/<pano_id>.w8192.jpg` | Display copy of a pano wider than 8192 px (#115). **Not written automatically since 2026-09-09** — only by `downscale_panos.py`, plus `refetch_panos` refreshing one that already exists, and deleting it if that refresh fails (#122). Copies written before the switch are still on the store, so: **never list the store's `*.jpg` by hand — call `downloaders.common.walk_store_panos()`**, the one definition of "this file is a panorama"; a walker that forgets `is_downscaled_sidecar()` does not fail, it invents pano ids like `<real id>.w8192` |
| `pano_id_log.csv` | Per-pano image ledger: `pano_id,downloaded,fetched_at`; two-field rows predate #129 (committed 2026-09-10, on the production store from 2026-09-17) and are never backfilled, and a `skipped` verdict (file already on disk, source never contacted) writes a **blank** stamp rather than laundering an unknown fetch time into today's - blank and absent both mean unknown. `progress_check` accepts **both** widths - a bare `len(row) != 2` silently parses a timestamped ledger as EMPTY. The stamp is last so the verdict stays in column 2; an end-anchored `grep ',0$'` no longer matches a `0` row |
| `depth_log.csv` | Per-pano depth ledger: `pano_id,saved\|unavailable` |
| `log.csv` | One 19-column row per run |
| `scrape.log` | Rotating run log (10 MB × 3) |

At the store **root** (the queue's `--store-root`, one level above the per-city storage roots): `scrape_queue.log`, and **`.pano-store`**, the store marker the operator creates once on the remote store (#161). The queue refuses to run without it.

**The store is an archive, not a cache — `docs/ops.md` → "The store is an archive, not a cache" is the rule, and it binds any code that writes a `.jpg`.** ~52% of labelled panos are already retired at Google and ~24% of the survivors come back re-rendered (#114), so a stored pano is usually the only copy of *that picture* that will ever exist and replacing one is a deletion, not a refresh. Two tiers: **the download path never overwrites** (all three downloaders short-circuit on `os.path.isfile` — that check is also the resume marker, which is exactly why a future writer that resumes differently would inherit no protection), and **a repair path overwrites only on positive proof the replacement is strictly better** (`refetch_panos.py`'s four gates). The uncovered case is a re-render at **identical dimensions** — `dims_changed` catches only the ones that changed frame, and nothing downstream can see the rest. The gate that would close it (horizon-band MAE against the stored file, zero extra requests, 45.6× separation) is deliberately **not built**, because no writer runs; the rule is what stops the next one shipping without it.

**`pano_id_log.csv` gates the image phase** — ids already in it are skipped on later runs. Note the caveat in `docs/ops.md`: a permanent verdict is ledgered and never retried, while a transient failure leaves no row.

**`depth_log.csv` gates the depth phase**, but only for permanent outcomes: `saved` and `unavailable` are ledgered and never retried; transient errors (including storage failures) are counted but **not** ledgered, so they retry next run. The artifact on disk is ground truth — deleting the ledger just makes the next run re-stat artifacts and re-request unresolved panos.

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

The module-specific ones are in the rules files; these apply across the tree.

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
  operator-facing run narrative and the warnings **the night's message carries** (cron's mail rule, delivered by `cron_notify.py` on the production host, #141); `logging` is the durable per-item detail in
  `scrape.log` / `crop.log`. **A warning that matters goes to both**, which is the depth phase's pattern
  (`logging.error(...)` then `print(...)`), because stdout is how someone hears about it tonight and the log
  is what is still there next week. #52 item 6 read the mixture as inconsistency; it is mostly deliberate,
  `docs/downloader.md` leans on a `WARNING` reaching the night's message, and ~15 tests assert on `capsys`. A print-to-logging sweep
  would break all three. The one real violation — `filter_supported_sources` warning on stdout only — is
  fixed; `caplog` assertions now sit beside its `capsys` ones so a revert fails. **A warning that should
  fail the night also goes into the run summary as a condition** (#161, `note_condition`): stdout reaches
  the message only on a night that already exits nonzero, because production delivers with `--only-on-failure`.
- **`log.csv` column 1 is a wall-clock stamp WITH its offset; every duration is `time.monotonic()`. Don't merge the two clocks (#101).** It was `str(datetime.now())` — a bare local reading, harmless only while every scraper host ran UTC, which is the assumption pinning the schedule to `America/Los_Angeles` removes. `log_analyzer` compares it against `datetime.now(timezone.utc)`, so a bare row on a non-UTC host is silently 7–8 h out, and `.days` floors, so that moves a city across the staleness threshold in *both* directions. `read_log` parses `format="ISO8601", utc=True`, which is also what lets one file hold both eras — without `utc=True` a mixed column comes back as object dtype and `analyze_city` dies on `.dt`, ending the report for every city after it. Symmetrically, the phase durations were wall-clock differences: the Pacific night window contains 02:00 local, so twice a year every city would report a run an hour longer than it was, and rule 4 warns at 3× the median.
- **`log.csv` is positional and headerless.** 19 comma-separated fields, blank-padded. Fields 2–6 are an XML-metadata stub kept at fixed values purely so column positions never shift (that endpoint died in 2022). Blank ≠ 0: blank means the phase never finished. Field 19 (`DEPTH_ELIGIBLE_FIELD`) is the GSV corpus size, written from the `finally` because it is known before any phase runs — so it is present on a crashed run and blank only on the crash-before-the-pano-list path and on rows written before #124 deployed. The full table is in `docs/ops.md`; `LOG_CSV_FIELD_COUNT` and `log_analyzer/analyze.py`'s `LOG_COLUMNS` must move together, and a test asserts they do. **Field 11 is not the image corpus; field 5 is** (#163): `image_total` is prior + tonight's attempts, `xml_total` is `len(image_pano_infos)`, and only the second grows when the image phase is starved.

## Other directories

- `tests/` — pytest suite (network-free; `streetlevel` is stubbed). `docs/testing.md` has the file-by-file map of what each test module covers; keep it current when adding a module. Three things the map does not say: `test_streetlevel_api.py` imports the real `streetlevel` to pin API details and skips itself if it isn't installed — unless `SIDEWALK_REQUIRE_STREETLEVEL` is set, as CI sets it, which makes the import hard so a missing transitive dependency fails the run instead of skipping it (#165); `conftest.py` fails a run that changed the repo (`git status` plus the gitignored `reports/scripts/.cache/`, stamped per file; reported once, and the next run takes that tree as its baseline), gives every spawned child a per-session temp dir, and makes `PytestRemovedIn10Warning` an error (`test_suite_isolation.py` pins all three); and `test_coverage_config.py` pins `.coveragerc` itself — the measured set, and the settings whose loss shows up as a lower number rather than an error.
- `log_analyzer/` — the log analyzer plus `cities.csv`; `log_analyzer/logs/` is a gitignored local cache.
- `flag_panos/` — one-off web tool (HTML/JS) from the 2022 depth-endpoint outage. Not wired into the Python scripts; keep unless asked.
- `samples/` — reference CSV/JSON/XML and a sample pano+crop used for manual testing and as examples for the `-c`/`-f` flags.
