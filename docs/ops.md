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
| `refetch_log.csv` | Ledger for the [`fover` repair pass](#repairing-fover-era-panoramas), if one has run here |
| `refetch.log` | That pass's rotating log |

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

## Repairing `fover`-era panoramas

`refetch_panos.py` re-fetches panoramas that were downloaded while the CBK URL still carried `fover`, which
made Google serve the polar rows of a zoom-5 grid at half size — 320 of 512 tiles on a 16384×8192 frame. The
parameter is gone ([#68](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/68)), so new
downloads are clean, but the scraper never revisits an image it already has, so everything scraped before the
fix keeps its half-resolution polar caps until something re-fetches it deliberately. The measurement behind
all of this is [the CBK tile resolution report](../reports/2026-08-07-cbk-tile-resolution.md).

```bash
python3 refetch_panos.py <storage-dir> --worklist reports/data/<date>-fover-refetch-worklist-<city>.csv.gz \
    --max-runtime 240 --min-pano-interval 2 --dry-run
```

Run it **on the scraper box, against the same store the nightly cron writes to.** The two never conflict: the
nightly run skips any pano that already has a `.jpg`, and this one only ever replaces a `.jpg` that is
already there.

### What it will and will not do

It **repairs; it never backfills.** A pano with no image on disk is skipped — `DownloadRunner.py` owns
downloading, along with the ledger semantics that go with it. It writes nothing to `pano_id_log.csv`,
`depth_log.csv`, `log.csv`, or any depth artifact. Depth stays valid because artifacts index by fraction of
the frame, and the frame does not change.

**It replaces a stored panorama only when the replacement is strictly better.** Roughly half the labelled
panoramas in the store no longer exist at Google ([47.9% survival](../reports/2026-08-09-photometa-census.md)),
so for much of any work-list the file on disk is the only copy that will ever exist. Every outcome but
`replaced` leaves those bytes untouched, and the swap itself goes through the same `.part`-and-rename as a
download.

The subtlest of the refusals is `frame_grew`, and it is the reason the tool probes before it fetches. The
store is a scrape-time archive and Google re-serves panos larger, so a grid sized from a stored 13312×6656
file can be too small for what Google now holds. That fetch does not return a smaller version of the pano —
it returns the **top-left 81% of it**, at exactly the stored file's dimensions, with no undersized tile and
no black anywhere. Nothing downstream could ever see it. Two requests, spent before the 512-tile fan-out,
rule it out.

| Outcome | Meaning | Requests |
|---|---|---|
| `absent` | no `.jpg` on disk — nothing to repair | 0 |
| `unreadable` | the stored file is not a readable JPEG; left for a human | 0 |
| `not_affected` | the stored frame implies a max zoom below 5, and the band is a zoom-5-only effect | 0 |
| `already_clean` | the file was written on or after `--fixed-after`, so it never carried `fover` | 0 |
| `dims_changed` | the work-list's frame disagrees with the stored one — see below | 0 |
| `gone` | Google no longer serves this pano at any zoom | ≤2 |
| `frame_grew` | Google now serves this pano **larger**, so this frame would fetch a crop of it | ≤4 |
| `upscaled` | only a fallback zoom was available; swapping would be a 4× **downgrade** | full |
| `undersized` | a tile still came back below 512 px, so there is nothing to gain | full |
| `too_black` | the fresh stitch has more black than a real panorama does | full |
| `replaced` | swapped in | full |

Every one of those is remembered in `<storage-dir>/refetch_log.csv`, with the same rule the two nightly
ledgers use: **a row means the outcome is permanent.** Anything transient — a failed tile, a mostly-black
stitch, a full store — is counted, logged, and left unledgered, so it retries on the next run. The four
zero-request outcomes are what keep a pass affordable, and they also mean a re-run after a finished sweep
costs nothing even if the ledger is deleted: a repaired file's mtime is newer than `--fixed-after`.

**`--fixed-after` is the one flag you must set deliberately.** It defaults to `2026-08-07`, the date the fix
was merged, which is the earliest defensible answer. The right value is the date the scraper box actually
picked the fix up: setting it late costs a wasted re-fetch, setting it early skips files that do need repair.

**`--allow-dims-change` is off, and re-framing is a separate decision.** By default the fetch uses the
*stored* file's frame, not the work-list's. Where the two disagree — [4.6% of a sampled
store](../reports/2026-08-10-store-coverage.md), nearly all of it the store holding an older, smaller frame —
the pano stops at `dims_changed` rather than being silently re-framed, because changing a pano's dimensions
moves every label's pixel coordinates relative to the image.

### Getting a work-list

Affected *labels* are identifiable by geometry even though affected *panoramas* are not identifiable by image
analysis, which is what makes this tractable. `reports/scripts/pano_y_histogram.py --write-worklist` bins
every label's `pano_y` against the measured bands and writes the panos with a label in one:

```bash
python3 reports/scripts/pano_y_histogram.py sidewalk-seattle.cs.washington.edu \
    --write-worklist --no-analysis
```

That is ~7.5% of Seattle's labelled panoramas and ~4.5% of Columbus's. `--from-store` is the escape hatch for
a wider pass — every stored pano, which for the full store is several orders of magnitude more traffic, so
size it before starting.

### Sizing a pass

`--dry-run` answers this exactly, and costs nothing but a header read per pano. Seattle's work-list against
the production store on 2026-08-19:

| | |
|---|---|
| considered | 7,914 |
| `absent` — in the label DB, no image on the store | 16 |
| `dims_changed` | 72 (0.9%) |
| would fetch | **7,826** |

Nothing came back `not_affected` or `already_clean`, which is the expected shape: every pano a label-derived
work-list names is a zoom-5 frame, and none of them had been re-fetched yet. At 512 tiles and ~10 MB each
that pass is ~78 GB and ~4.0M tile requests — and about half of it will return `gone`.

One 16384×8192 pano is 512 tile requests and ~10 MB, so bandwidth is roughly the size of the slice being
repaired, and about half of it buys nothing because the pano is gone. `--min-pano-interval` is the throttle
that matters (it paces whole panos, not tiles), `--max-runtime` and `--max-panos` bound a session, and the
run stops itself after five consecutive transient failures rather than spending the rest of the budget on a
wall. `--measure` records what each re-fetch actually recovered to `refetch_measurements.jsonl`; it decodes
the stored frame as well as the fresh one, so it roughly doubles peak memory and is meant for a pilot.

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
| 8 | image fallback successes | downloaded, but at a fallback resolution — only zoom 3 was available for a frame whose reported dimensions need zoom 5, so the stitch was upscaled to reach them. Real imagery, materially less of it. **Not** simply "downloaded at zoom 3": an old pano whose own max zoom is 3 is at its native resolution and counts in field 7. Was a constant `0` before [#52](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/52) because nothing ever returned the verdict, so runs before that show every fallback inside field 7 |
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
