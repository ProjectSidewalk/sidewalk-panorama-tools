# Ops — the store, the ledgers, and the run log

What a nightly `DownloadRunner.py` run leaves behind, and how to read it. For monitoring across all cities,
see the [log analyzer](log-analyzer.md).

## Storage layout

Everything lives under the storage root, sharded by the first two characters of the pano id:

| Path | What |
|---|---|
| `<pano_id[:2]>/<pano_id>.jpg` | Stitched panorama |
| `<pano_id[:2]>/<pano_id>.depth.npz` | [Depth artifact](depth.md#the-artifact) |
| `pano_id_log.csv` | Per-pano image ledger: `pano_id,downloaded` |
| `depth_log.csv` | Per-pano depth ledger: `pano_id,saved\|unavailable` |
| `log.csv` | One 18-column row per run |
| `scrape.log` | Rotating run log (10 MB × 3) |

`scrape.log` lives here rather than in the working directory on purpose: cron runs the scraper from whatever
directory it likes, and a relative path scatters every per-pano failure detail somewhere nobody looks.

## Resume ledgers

Both phases resume from an append-only ledger, and both draw the same line: **a row means the outcome is
permanent.** Transient failures leave no row and retry automatically on the next run.

**`pano_id_log.csv` gates the image phase** (`pano_id,downloaded`):

* `1` — image on disk, or a prior success.
* `0` — the source has nothing for this pano: no imagery at any zoom, or unknowable dimensions. A permanent
  verdict.
* **no row** — never attempted, or the last attempt failed transiently (a network blip, a failed tile, a full
  store). Retried next run.

Deleting `0` rows, or the whole file, is the manual force-retry lever; existing `.jpg`s are simply
re-registered as skipped rather than re-downloaded.

**`depth_log.csv` gates the depth phase** with the same semantics — see
[Depth maps → The ledger](depth.md#the-ledger). The artifacts on disk are the ground truth; deleting the
ledger just makes the next run re-stat artifacts and re-request whatever is unresolved.

Panos filtered out for an unsupported `source` are deliberately not ledgered either, so adding support later
picks them up.

## Two things that keep a killed run honest

* **Images are written through a `.part` file and renamed into place.** An existing `.jpg` *is* the resume
  marker, so a download killed mid-write would otherwise leave a truncated file that every later run reports
  as a completed success. A stray `*.jpg.part` on the store is debris from a killed run and is safe to delete;
  the next run rewrites it.
* **Unattempted panos are shuffled each run.** Because a transient failure leaves no ledger row, it keeps its
  place in the server's ordering, so a stable iteration order would re-attempt the same failing head block
  every night and spend `--max-runtime` before reaching new work. Shuffling also stops a source-clustered
  `/adminapi/panos` response from starving whichever source sorts last. The depth phase shuffles for the same
  reason.

## The `log.csv` columns

Each run appends **one row of 18 positional comma-separated fields, with no header**, parsed by the
[log analyzer](log-analyzer.md). Durations are whole minutes (rounded). Fields 2–6 describe the XML metadata
phase — a stub since Google killed that endpoint in 2022, kept at fixed values purely so the column positions
never shift.

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
| 14 | depth failures | includes permanent `unavailable` outcomes — **not an alert signal**, see below |
| 15 | depth skipped | panos already resolved in `depth_log.csv` |
| 16 | depth total processed | sum of fields 13–15 |
| 17 | depth phase duration | |
| 18 | total run duration | |

`LOG_CSV_FIELD_COUNT` in `DownloadRunner.py` and `LOG_COLUMNS` in `log_analyzer/analyze.py` must move
together; a test asserts they do.

### Blank fields mark a crashed or stopped run

A run that crashes — or is stopped — still appends a full 18-field row: every phase that completed keeps its
real counts, and every field from the first unfinished phase onward is blank. Visibly missing data, never a
fabricated `0`. A row that is only a timestamp means the run died before scraping started, most likely because
the pano-list fetch against the webserver failed.

Blanks are new as of [#49](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/49) — historical
rows are all-integer — so readers must treat them as missing data (`pandas.read_csv` surfaces them as `NaN`,
turning those columns `float64`) rather than feeding them to `int()`.

Fields are accumulated in memory and written once in a `finally`, which is why even a crash between phases
produces a single full-width row. `SIGTERM` is translated into `sys.exit(143)` so a stop runs those `finally`
blocks instead of discarding the evidence.

### The depth failure count is not an alert signal

Field 14 includes `unavailable` — a permanent, expected, non-actionable outcome — so the first backfill runs
show large failure numbers that are entirely normal. The success/failure/unavailable split goes to stdout and
`scrape.log`; `log.csv` keeps its fixed 18-column shape, so there was no room for a separate column.

## What healthy looks like

A mature city settles into: `image_success` small or zero most nights, stable `image_fail`, and
`image_skip ≈ image_total`. The [log analyzer](log-analyzer.md) encodes the rest of the heuristics, including
what "stale" and "ended early" mean in practice.
