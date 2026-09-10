# Ops — the store, the ledgers, and the run log

What a nightly `DownloadRunner.py` run leaves behind, and how to read it. For monitoring across all cities,
see the [log analyzer](log-analyzer.md).

## Storage layout

Everything lives under the storage root, sharded by the first two characters of the pano id:

| Path | What |
|---|---|
| `<pano_id[:2]>/<pano_id>.jpg` | Stitched panorama |
| `<pano_id[:2]>/<pano_id>.depth.npz` | [Depth artifact](depth.md#the-artifact) |
| `<pano_id[:2]>/<pano_id>.w8192.jpg` | [Display copy](#display-copies-of-wide-panoramas) of a panorama wider than 8192 px. **No longer written automatically** — see that section |
| `pano_id_log.csv` | Per-pano image ledger: `pano_id,downloaded` |
| `depth_log.csv` | Per-pano depth ledger: `pano_id,saved\|unavailable` |
| `log.csv` | One 19-column row per run |
| `scrape.log` | Rotating run log (10 MB × 3) |
| `refetch_log.csv` | Ledger for the [`fover` repair pass](#repairing-fover-era-panoramas), if one has run here |
| `refetch.log` | That pass's rotating log |

One file lives at the **store root** rather than inside a city: `scrape_queue.log`, the
[queue driver](downloader.md#nightly-deployment)'s own rotating log. It records what ran last night, in what
order, and how long each city took — which no per-city log can, because none of them can see the ring.

`scrape.log` lives here rather than in the working directory on purpose: cron runs the scraper from whatever
directory it likes, and a relative path scatters every per-pano failure detail somewhere nobody looks.

### Display copies of wide panoramas

> **Switched off 2026-09-09.** `WRITE_DISPLAY_COPIES` in `downloaders/common.py` is `False`, so neither
> downloader writes a display copy any more. Two writers remain, both narrow: `downscale_panos.py`, which
> only runs when a person runs it, and `refetch_panos.py`, which **refreshes a copy already on the store
> after a swap but never creates one** ([why](#a-repaired-panoramas-copy-is-refreshed-never-created)).
> **Why the feature is off**, and why the code is kept rather than reverted, is
> [directly below](#why-it-is-off-2026-09-09). Read that before turning it back on.

A display copy is a stored panorama re-encoded at the width a viewer can texture, written beside the native
file as `<pano_id>.w8192.jpg`. #115 built it on the premise that Pannellum renders an equirectangular image
as **one WebGL texture**, so 8192 px — a common `MAX_TEXTURE_SIZE` — was the ceiling and every wider panorama
(newer GSV imagery is 16384 × 8192, Richmond's Mapillary imagery 11000) was displayable only through a copy.

#### Why it is off (2026-09-09)

Four measurements, none of which were available when #115 was written:

* **Pannellum's ceiling is `2 × MAX_TEXTURE_SIZE`, not `1 ×`.** It uploads an equirectangular image as two
  half-width textures, so its own refusal test is `max(width/2, height) > MAX_TEXTURE_SIZE` and its error
  reports the maximum as `2*L`. **A device advertising 8192 renders a 16384-wide panorama** — exactly the
  widest frame GSV produces and exactly what the store holds. #115's premise was wrong by a factor of two,
  and so was the `pano.downscaled.max-width` comment in the web app it was copied from.
* **Measured on real phones**, not inferred: iPhone 13 Pro reports 16384 (ceiling 32768), Pixel 7 Pro
  reports 8192 (ceiling 16384). Both really hold 2 × 8192² RGBA — 512 MiB, in 380 ms and 486 ms.
  *(If anyone re-runs this: `texImage2D(…, null)` returns in ~1 ms because drivers only reserve address
  space. Write every row with `texSubImage2D` and then `gl.finish()`, or the probe proves nothing.)*
* **The demand does not justify a derivative.** Across the five largest production cities the store holds
  140,599 wide panoramas whose imagery has expired at Google — the only ones ever served from the store —
  and they were viewed **29 times in ninety days**. That is about **4,850 copies cut per copy looked at**,
  against a backfill estimated at **6.4 TB and ~8 days**.
* **The population that genuinely cannot render 16384 is ~50 users a year**, on 365 days of production
  analytics: Nexus 5 / 5X on Adreno 330/418, ~2.2% of mobile users and ~0.1% of all traffic. iOS needs no
  copy at all (A9 and later report 16384).

The web app now cuts the copy **on demand**, at the width the client asks for
([SidewalkWebpage#5256](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/5256)), using
`ImageReadParam.setSourceSubsampling` so the reduction happens inside the JPEG decode: **~105 MB of heap and
~2.0 s**. The strip-read code it replaced needed **~400 MB of heap and ~10 s** per panorama inside a 1.5 GB
web-app heap, which is what OOM-killed production JVMs
([SidewalkWebpage#5239](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/5239)) — so the "Java's
ImageIO has no DCT-domain scaling, therefore the derivative belongs with the scraper that already holds the
full raster" half of #115's rationale is retired too.

**Why the code is kept rather than reverted.** The margin it was built for is real and is exactly **zero**:
`16384 = 2 × 8192`, and GSV has already widened its frames once (13312 → 16384). The day it widens again,
every 8192-class GPU — every Mali-G710-era Android, which is most of the non-Apple fleet — stops rendering
stored panoramas natively, and the affected population jumps from ~2% of mobile to most of Android. **This
repo is the only place that sees GSV's reported frame width at the moment it changes**
(`downloaders/gsv.py::resolve_zoom_and_dims`, tracked by #121). So the switch stays one line away rather
than in the history.

#### Running the sweep by hand

```bash
python3 downscale_panos.py <storage-dir> --dry-run          # count the missing copies, write nothing
python3 downscale_panos.py <storage-dir>                    # write them
python3 downscale_panos.py <storage-dir> --max-runtime 240  # a nightly-sized slice; the rest report as unreached
```

**Budget the disk first, per city.** A display copy is not a thumbnail: measured on the committed
`samples/sample_pano.jpg` (13312 × 6656, 6.08 MB), the 8192-wide copy is **3.82 MB — 63% of the native file**
at the `DOWNSCALED_JPEG_QUALITY` of 85 (2.82 MB, 46%, at 75). Nearly every modern panorama is over the cap, so
sweeping the whole fleet is close to a **+63% commitment on the whole store**, taken all at once and never
given back — for a derivative the numbers above say is viewed about once per 4,850 copies cut. `--dry-run`
prints the count it would write, which is the number to multiply; check `df` on the store first.

#### What is still load-bearing

Three properties of the sidecar hold for as long as any copy is on a store:

* **The width is in the name.** The web app looks for exactly the name its own configured cap produces
  (`pano.downscaled.max-width`, 8192), and checks the header before serving it, so a change of cap is a new
  sidecar rather than an ambiguous overwrite — change `DOWNSCALED_MAX_WIDTH` in `downloaders/common.py` and
  the app's setting together, then re-run the sweep.
* **A sidecar is never a panorama.** Everything that lists `*.jpg` in a shard — `refetch_panos.py
  --from-store`, the sweep's own walk — excludes it by name (`is_downscaled_sidecar`). The cropper never
  sees one: crops are always cut from the native file, by exact path.
* **A failed sidecar never fails the panorama.** This governs the downloaders only while the switch is on,
  so today it describes a path nothing takes; it still governs `refetch_panos`, below. With the switch on,
  the native file is in place by the time the copy is written and is the [resume marker](#resume-ledgers);
  raising would re-attempt the pano every night, skip it at the exists() check, and never write the copy.
  That also makes the sweep **a repair pass, not a one-off**: `download_single_pano` returns `skipped` at its
  `exists()` check *before* the sidecar code, so a `display copy not written` line in `scrape.log` is only
  ever cleared by running `downscale_panos.py` again.

<a id="a-repaired-panoramas-copy-is-refreshed-never-created"></a>

##### A repaired panorama's copy is refreshed, never created

`refetch_panos.py` swaps the native bytes only under an unchanged frame, so a sidecar left behind keeps the
width its name promises and reads as current to the sweep for ever: the viewer would go on serving a copy of
exactly the imagery the repair replaced. So `_refresh_display_copy` is the one thing the switch does **not**
fully turn off. It writes where a copy is already on the store and nowhere else — the switch says stop making
a new artifact nobody asked for, not start lying in the one that is already there. It never fails the swap
over it: the panorama is already on disk at that point, and re-fetching it would cost ~512 requests to redo
work that has landed. Crops are the artifact that is still *not* refreshed — see the `replaced` rows in
`refetch_log.csv`.

> ⚠ **If that rewrite fails, the sweep cannot repair it — delete the sidecar first.**
> `scrape.log` gets one `display copy not rewritten` line and the swap is ledgered `replaced` regardless.
> But `sidecar_is_current` judges from dimensions alone (a decode per panorama is the whole cost the sweep
> exists to avoid), and every gate in `refetch_panos` refuses a swap that changes the frame — so the stale
> copy has *exactly* the expected dimensions and every later sweep reports it `current`, writing nothing.
> `rm` the named `.w8192.jpg`, then run `downscale_panos.py`, which will see it absent and cut a fresh one.

**Copies already on a store are left alone.** They cost disk and nothing else: every walker excludes them by
name, so a sidecar can never be mistaken for a panorama, and the web app serves whichever of the two it
finds. Removing them is an operator decision, not something any tool here does.

## Resume ledgers

Both phases resume from an append-only ledger, and both draw the same line: **a row means the outcome is
permanent.** Transient failures leave no row and retry automatically on the next run.

**`pano_id_log.csv` gates the image phase** (`pano_id,downloaded`):

* `1` — image on disk, or a prior success.
* `0` — the source has nothing for this pano. A permanent verdict, one per source:
  * **GSV** — no imagery at any zoom, or unknowable dimensions. No breaker entry, deliberately: a retired
    GSV pano is a permanent verdict and an ordinary one, at 7.9–8.4% of a large city's rows.
  * **Mapillary** — a 404, or a record that names the image and carries no original-resolution rendition.
    No Mapillary 404 has ever been observed — its "does not exist" is a 400, measured 2026-09-06 — so the
    record with no rendition is the one that fires in practice, and three of them in a row stop the run
    writing any more (see *[When the image phase stops trusting a source](#when-the-image-phase-stops-trusting-a-source)*,
    [#113](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/113)).
  * **Panoramax** — a 404 **carrying the catalog's own `Feature not found` body**, an item affirming a
    field of view other than 360 (a flat contributor photograph in the same bbox), or a well-formed assets
    block that offers no `hd` rendition. Each rests on the catalog affirming something rather than on a
    status code or a missing key, *and* three in a row trip the same breaker: two of the three are
    wholesale failures wearing a per-pano face, so the affirmation and the breaker are both wanted here.

  A Mapillary error envelope on a 200, a 404 whose envelope carries the auth signature
  (code 190 / `OAuthException`), a body that does not name the image, an image body that is not a JPEG, a
  Panoramax 404 *without* the catalog's body, a malformed assets block, and a redirect off a published `hd`
  href are none of them verdicts and leave no row.
* **no row** — never attempted, the last attempt failed transiently (a network blip, a failed tile, a full
  store), or [the breaker](#when-the-image-phase-stops-trusting-a-source) stopped trusting the source (both
  the withheld tripping verdict and every pano skipped after it). Retried next run.

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

> **Decided 2026-09-05: the `fover` pass does not run.** The [pilot](../reports/2026-09-05-fover-refetch-pilot.md)
> re-fetched 200 Seattle panoramas against a copy of the store and found nothing to recover: the 512-px polar
> bodies CBK serves without `fover` are server-side upscales of the same data it served at 256 px with it, so
> the stored files already hold everything Google has for those rows. It also found that Google has re-rendered
> about a quarter of the panoramas it still serves, so a bulk re-fetch would have replaced those with a
> different picture, not a sharper one. The tool stays, as a tested, non-destructive repair pass for whatever
> next needs one; the section below describes it as built.

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
| `upscaled` | only a fallback zoom was available; swapping would be a 4× **downgrade** | zoom-3 grid (≤32) |
| `undersized` | a tile still came back below 512 px: the CBK request is costing resolution again. Not swapped, **not ledgered**, and three in a row stop the run with exit 1 | full |
| `too_black` | the fresh stitch has more black than a real panorama does | full |
| `replaced` | swapped in | full |

The outcomes that cost requests — every one except `undersized` — are remembered in
`<storage-dir>/refetch_log.csv`, with the same rule the two nightly ledgers use: **a row means the outcome is
permanent.** Anything transient — a failed tile, a mostly-black stitch, a full store — is counted, logged, and
left unledgered, so it retries on the next run.

The five zero-request outcomes are **not** ledgered, on purpose. Each is recomputed from the store in
microseconds, so a row would buy nothing — and two of them are properties of the flags rather than of the
pano: `already_clean` moves with `--fixed-after`, `dims_changed` with `--allow-dims-change` and whichever
work-list was passed. Ledgering them would lock one run's flag values in. Not ledgering them is also what makes
a re-run after a finished sweep cost nothing even if the ledger is deleted: a repaired file's mtime is newer
than `--fixed-after`, so it comes back `already_clean`.

`undersized` is the other exception, in the other direction. With `fover` gone no tile should ever come back
below 512 px, so one that does means the request is costing resolution again — a property of the URL, not of
the pano. A permanent row per pano would burn the whole work-list against a bug in the request and exit 0
while doing it. Instead the run stops after three consecutive undersized fetches, exits 1 if it saw any, and
ledgers none of them; fix the request (`tests/test_gsv_tile_contract.py` pins the parameters) before
re-running.

**`--fixed-after` is the one flag you must set deliberately.** It defaults to `2026-08-07`, the date the fix
was merged, which is the earliest defensible answer. The right value is the date the scraper box actually
picked the fix up — **`2026-09-01` for the current production box**, which ran a checkout 183 commits behind
until the cutover ([history](history.md)), so everything it scraped between those two dates is `fover`-era
and the default would skip it. Setting it late costs a wasted re-fetch, setting it early skips files that do need repair —
but because `already_clean` is not ledgered, correcting it later and re-running picks those files up. The
gate reads the file's mtime, and nothing else can tell a clean file from a `fover`-era one (finding 6 of the
report), so mtime is load-bearing: a copy or restore that did not preserve mtimes makes every pano
`already_clean`, which is the safe direction but means the pass silently does nothing. A dry run's
`already_clean` count is the tell.

**`--allow-dims-change` is off, and re-framing is a separate decision.** By default the fetch uses the
*stored* file's frame, not the work-list's. Where the two disagree — [4.6% of a sampled
store](../reports/2026-08-10-store-coverage.md), nearly all of it the store holding an older, smaller frame —
the pano stops at `dims_changed` rather than being silently re-framed, because changing a pano's dimensions
moves every label's pixel coordinates relative to the image.

**Replacing a pano does not refresh crops already cut from it.** [Existing crops are the cropper's resume
marker and are never re-cut](cropper.md#outcomes-exit-code-and-re-runs), so after a pass every crop cut from a `replaced` pano's
polar band is still the half-resolution one, and nothing on disk says so. The ledger is the list: delete the
crops of every pano with a `replaced` row (`grep ,replaced refetch_log.csv`) and re-run the cropper to pick the
repair up. The crops are the point of the pass, so plan that step with it.

### Getting a work-list

Affected *labels* are identifiable by geometry even though affected *panoramas* are not identifiable by image
analysis, which is what makes this tractable. `reports/scripts/pano_y_histogram.py --write-worklist` bins
every label's `pano_y` against the measured bands and writes the panos with a label in one:

```bash
python3 reports/scripts/pano_y_histogram.py sidewalk-seattle.cs.washington.edu --write-worklist
```

`--write-worklist` leaves the dated histogram artifact and its figure alone (it implies `--no-analysis`): the
histogram is a measurement dated 2026-08-07 and quoted by date in the report, while a work-list is regenerated
whenever a city's labelling has moved on.

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
run stops itself after five consecutive transient failures — or three consecutive undersized fetches — rather
than spending the rest of the budget on a wall. Candidates are shuffled before the run, so a session that hits
its budget did a random slice of the work-list, not its head. `--measure` records what each re-fetch actually
recovered to `refetch_measurements.jsonl`, one line per swap as it lands; it decodes the stored frame as well
as the fresh one, so it roughly doubles peak memory and is meant for a pilot.

## The `log.csv` columns

Each run appends **one row of 19 positional comma-separated fields, with no header**, parsed by the
[log analyzer](log-analyzer.md). Durations are whole minutes (rounded). Fields 2–6 describe the XML metadata
phase — a stub since Google killed that endpoint in 2022, kept at fixed values purely so the column positions
never shift. Field 19 was added on 2026-09-09 ([#43](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/43));
rows written before then have 18 fields, and the analyzer reads them with the last one blank.

| # | field | notes |
|---|-------|-------|
| 1 | run start timestamp | ISO-8601 **with an explicit UTC offset**, e.g. `2026-09-05 20:30:04.277106+00:00`. Rows written before [#101](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/101) are `str(datetime.now())` — the same shape without the offset, and without the `.ffffff` on the rare run whose microsecond landed on exactly 0. Read a bare one as UTC: every scraper host has run UTC, which is why the omission was survivable for as long as it was |
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
| 19 | depth corpus size | the number of GSV panos the depth phase was given — the denominator for the backfill's progress, which nothing else in the row carries (field 16 says how many are resolved, not out of how many). Known before either phase runs, so it is present on a crashed run too; blank only on a run that died in the pano-list fetch, and on every row older than the field. Written whether or not depth ran, so a `--skip-depth` or stood-down run reads `0,0,0,0,0,K` — five zeros and the work still waiting |

`LOG_CSV_FIELD_COUNT` in `DownloadRunner.py` and `LOG_COLUMNS` in `log_analyzer/analyze.py` must move
together; a test asserts they do.

### Which clock each field is on

Field 1 is the **wall clock, stamped with its offset** — the one thing a wall clock is good for, namely
*when* the run happened. Every duration (fields 6, 12, 17, 18) is measured on `time.monotonic()` instead, so
neither an NTP step nor a DST transition can invent or delete an hour of runtime. That distinction stopped
being academic when the schedule moved into a named timezone: the Pacific night window the
[queue](downloader.md#nightly-deployment) runs in contains 02:00 local, so a wall-clock duration would be an
hour out twice a year — and the log analyzer warns at 3× the median runtime, so the whole fleet would have
reported an abnormally long run on the same night, with nothing actually wrong.

### Blank fields mark a crashed or stopped run

A run that crashes — or is stopped — still appends a full 19-field row: every phase that completed keeps its
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
`scrape.log`; the row has no separate column for it.

### Reading the backfill from the row

Field 16 (`depth_total`) is success + failed + skipped: every pano the ledger accounts for by the end of the
run, i.e. **the cumulative resolved count**, and field 19 is the corpus. So `19 − 16` is the work left, the
per-night sum of `13 + 14` is the rate, and their quotient is the ETA — which is exactly what the
[log analyzer](log-analyzer.md#the-depth-backfill) prints per city and for the fleet. One shape to read
carefully: a row whose five depth fields are all `0` is a phase that **did not run** (the block latch,
`--skip-depth`, an unwritable ledger, `streetlevel` missing), not a city with nothing resolved. The analyzer
takes "resolved" from the newest row on which the phase ran.

## When the depth phase stands itself down

Two mechanisms stop depth without stopping the run, and they look identical from `log.csv` (all five depth
columns are `0`), so read stdout or `scrape.log` rather than the row:

| what you see | what happened | what to do |
|---|---|---|
| `WARNING - Google refused this host N hours ago, so the depth phase is standing down` | An earlier run on this host was blocked, and the **block latch** is still fresh. Every city skips depth at **zero requests** until it expires (6 h). | Nothing, usually. It is the fleet declining to walk back into the same wall. If it persists past a day, look for a captcha/consent interstitial from this IP. |
| `WARNING - the depth phase stopped early because Google stopped answering` | *This* run was refused. It set the latch, so the next city will skip rather than rediscover. | Check for a rate limit before the next night. The pacer will also have backed off, and each new city process starts fresh at `depth_start_interval`. |

The latch is a file in the system temp directory, **not on the store** — it records this host's standing
with Google, and the storage directory a run is given belongs to a single city. `--depth-block-latch PATH`
moves it. To clear one by hand, delete the file; a missing, unparseable or implausibly future-dated latch
all mean "not blocked", because a latch nobody can read must never be able to stand the whole fleet's depth
phase down indefinitely.

**Do not read a stood-down phase as lost work.** Nothing is ledgered on either path, so every unresolved
panorama is retried on the next run. See
[Depth → Being a good citizen](depth.md#being-a-good-citizen-of-googles-servers).

## When the image phase stops trusting a source

A `downloaded=0` row is permanent and is only undone by hand-editing `pano_id_log.csv` on the store, so the
image loop stops writing them once one source produces three in a row
([#113](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/113)):

```
IMAGEDOWNLOAD: WARNING - 3 consecutive permanent failures from source mapillary. That is a condition of the
run, not of the panos, so nothing more from this source is ledgered tonight.
IMAGEDOWNLOAD: WARNING - breaker tripped for mapillary; 214 pano(s) were left unattempted and nothing was
ledgered for them, so they retry next run. Check that source's credentials before the next run, then look
for false downloaded=0 rows in pano_id_log.csv.
```

**What to do.** Check the source's credentials first — the condition this guards is a token that has lost
the scope it needs, which Mapillary can answer by omitting the image URL from an otherwise healthy record
rather than by erroring.

Then repair the ledger. **Exactly two** false `downloaded=0` rows are written before the breaker has enough
evidence to fire — the threshold is 3 and the tripping verdict is withheld — and since only a *success*
resets the count, those two are always the tripped source's last two rows, adjacent. Nothing else in the run
is damage.

They are **not** necessarily the last rows *in the file*. Only the tripped source stops; a city carrying both
GSV and Mapillary keeps downloading and ledgering GSV afterwards, so the tail can be thousands of GSV rows.
`pano_id_log.csv` is `pano_id,downloaded` with no source column, so match on the id shape — Mapillary ids are
all-numeric, GSV's are 22-character base64, Panoramax's are UUIDs. Delete those two rows, fix the
credentials, and the next run picks up everything the breaker skipped, because none of it was ledgered.

The run **exits nonzero**, so `scrape_queue.py` books the city as `failed` and cron mails the queue summary.
Only the tripped source stops: a city carrying both GSV and Mapillary panos keeps downloading GSV. `log.csv`
is unchanged — its fields are counts of work and the breaker is not one of them, so stdout, `scrape.log`
and the exit code are where this lives.

GSV has no breaker, deliberately: 7.9–8.4% of a large GSV city's ledger is a permanent verdict (retired
imagery), so three in a row is routine there rather than evidence — about every 1,700 panos at 8.4%. The
table is per source, in `DownloadRunner.MAX_CONSECUTIVE_PERMANENT_FAILURES`; a source with no entry is
unlimited, so **a new source declares its own threshold or gets no breaker at all**.

**Mapillary and Panoramax both carry 3.** They fail differently, so the first thing to check differs: a
Mapillary trip points at the token, while **Panoramax is keyless and has no credential to check**. There,
look at the catalog instead — `api.panoramax.xyz` answering 404 for pictures that exist (a renamed endpoint,
or a CDN), an instance that has stopped publishing `hd` renditions, or the app's own 360° filter having
stopped holding, which would hand this scraper the third of the Bayonne bbox that is flat 92° photographs.
The repair is the same either way: delete the tripped source's last two `downloaded=0` rows and re-run.

**Only a success resets the count.** Not a transient failure and not a skip. A transient reset was the first
version of this and it defeated the breaker on the fault it was built for: Mapillary answers "does not exist
or missing permissions" with a 400, which raises, and every retired image answers that way on *every* run
forever, because a transient is never ledgered and so is a candidate again the next night. Those raises,
shuffled among the live panos, would reset the count constantly — the run would write false permanent rows
for most of the live panos and might never trip. A raise is not evidence that the source is answering
honestly; it is no evidence about the source at all.

## What healthy looks like

A mature city settles into: `image_success` small or zero most nights, stable `image_fail`, and
`image_skip ≈ image_total`. The [log analyzer](log-analyzer.md) encodes the rest of the heuristics, including
what "stale" and "ended early" mean in practice.

During the depth backfill, add: `depth_total` climbing night over night towards field 19, and `depth_fail`
large but *stable* — it counts `unavailable`, which is permanent and expected, so it is not an alert signal.
The split goes to stdout and `scrape.log`. The analyzer's stats line puts it in one clause:
`depth 1,753/183,680 (1.0%) · +590/night · ~308 nights left`.
