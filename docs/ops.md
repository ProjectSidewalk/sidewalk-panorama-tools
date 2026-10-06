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
| `pano_id_log.csv` | Per-pano image ledger: `pano_id,downloaded,fetched_at` (rows written by a build older than #129 — on the production store, before 2026-09-17 — have no `fetched_at`) |
| `depth_log.csv` | Per-pano depth ledger: `pano_id,saved\|unavailable` |
| `log.csv` | One 19-column row per run |
| `scrape.log` | Rotating run log (10 MB × 3) |
| `refetch_log.csv` | Ledger for the [`fover` repair pass](#repairing-fover-era-panoramas), if one has run here |
| `refetch.log` | That pass's rotating log |

One file lives at the **store root** rather than inside a city: `scrape_queue.log`, the
[queue driver](downloader.md#nightly-deployment)'s own rotating log. It records what ran last night, in what
order, in which [pass](downloader.md#extra-passes) and how long each city took — which no per-city log can,
because none of them can see the ring.

`scrape.log` lives here rather than in the working directory on purpose: cron runs the scraper from whatever
directory it likes, and a relative path scatters every per-pano failure detail somewhere nobody looks.

### Display copies of wide panoramas

> **Switched off 2026-09-09.** `WRITE_DISPLAY_COPIES` in `downloaders/common.py` is `False`, so neither
> downloader writes a display copy any more. Two writers remain, both narrow: `downscale_panos.py`, which
> only runs when a person runs it, and `refetch_panos.py`, which **refreshes a copy already on the store
> after a swap but never creates one**, and deletes it if that refresh fails
> ([why](#a-repaired-panoramas-copy-is-refreshed-never-created)).
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
(`downloaders/gsv.py::resolve_frame`), and it now says so when it does — see
[the width tripwire](#the-width-tripwire) below. So the switch stays one line away rather than in the history.

#### The width tripwire

[#121](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/121). Every downloader warns when a
source hands it a panorama **wider than `VIEWER_MAX_PANO_WIDTH` (16384)** in `downloaders/common.py`. It should
never fire: 16384 is GSV's widest frame today and the fleet's normal, so 16384 itself is silent and 16385 is the
first width that warns.

* **Where it looks.** GSV: in `resolve_frame`, on the width `/adminapi/panos` reports, before any request
  is spent, so a photometa request or probe that then fails cannot swallow it (`refetch_panos.py` reaches it through `resolve_zoom_and_dims`, so a
  repair pass warns on a stored frame that wide too — but there the line lands in that pass's **`refetch.log`**
  and its stdout, not in `scrape.log`, and keeps the `IMAGEDOWNLOAD:` prefix in the middle of the pass's
  `REFETCH:` narrative, so grep a refetched store's `refetch.log*` as well). Mapillary and Panoramax: on the downloaded JPEG's own
  header, after the file is in place — the width of the file actually stored, which is what a viewer is
  handed, rather than anything either source's metadata says.
* **Never a gate.** It does not refuse, alter or delay a download and writes nothing to the store.
* **Both channels, one line per wide pano**, each carrying the whole message: `IMAGEDOWNLOAD: <source> pano
  <id> is <width> px wide, over the viewer ceiling of 16384 (#121) …` in that city's `scrape.log` at
  `WARNING` (`refetch.log` for a repair pass, above), and the same text after `IMAGEDOWNLOAD: WARNING -` on
  stdout. The line's own remedy is deliberately only a pointer — verify the width, then budget the disk
  before any sweep, and read **When it fires** below — because the steps end in a fleet-wide sweep and
  a line acted on alone would skip the budget. No once-per-run latch, deliberately:
  stdout already carries one `Processing pano` line per pano attempted, and the alarm wrapper
  [cuts the middle](#hearing-about-a-bad-night) of a long night's output, where a single announcement is the
  line most likely to be lost.
* **It alarms once per host, then only warns** (decided on
  [#153](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/153), 2026-09-26). Production runs the
  queue under `cron_notify.py --only-on-failure`, so a warning alone reaches no one on a night that exits 0.
  Failing *every* run that sees a wide frame would reach someone, and then keep the city red every night after,
  since Google does not un-widen, hiding any real failure behind a known one. So the **first** `DownloadRunner`
  run on a host to see a frame over the ceiling prints `WIDTH ALARM (#121): …` on both channels, creates a
  latch file and **exits 1**, so the queue books a failed city and the alarm is delivered. Every later run finds
  the latch and prints only `… already alarmed on this host (latch <path>), so not failing the run.`

  * **The latch** is `sidewalk-width-ceiling-alarmed` in the system temp directory, or `--width-alarm-latch
    PATH`. It is on local disk, beside the depth block latch, because a wider frame is a fact about Google
    rather than one city: a per-city latch would alarm once per city. Its content is the UTC time of the first
    sighting, and it is never rewritten.
  * **Delete it to re-arm** — after acting on an alarm, say, so the next change reaches you too.
  * **A latch that cannot be written fails every run** that sees a wide frame, with the path in `scrape.log`:
    a latch nobody can write must not swallow the one alarm it exists for.
  * `refetch_panos.py` warns through the same seam but never arms the latch or changes its exit code; a repair
    pass is run by someone watching it.

  The per-pano lines are still there afterwards, so to find which stores have seen one:

  ```bash
  grep -ls "over the viewer ceiling" */scrape.log* */refetch.log*
  ```

  (the `*` after `.log` takes in the rotated `.1`–`.3` files; `-s` quiets a store with no `refetch.log`).
* **Not a `log.csv` column, and so not a log-analyzer rule — on purpose, not an oversight.** `log.csv` is a
  fixed set of positional fields that the analyzer and other tooling read by position, and #121 asks for any
  persisted width to be proposed there first. The [log analyzer](log-analyzer.md) reads nothing but `log.csv`,
  so without a column there is nothing for a rule to read. Don't file the missing rule as a gap; propose the
  column.
* **Why 16384, and why it is not `2 × DOWNSCALED_MAX_WIDTH`** although it equals that today: the ceiling is
  what 8192-class GPUs can texture (2 × 8192, [above](#why-it-is-off-2026-09-09)); the display-copy cap is how
  wide a copy to write, a separate choice that can be lowered to save disk without changing what any device
  renders. A test pins that the ceiling is not written in terms of the cap.

**When it fires.** First check it is real: the width is in the line, and `jpeg_dimensions` on the stored file
confirms it. Then:

1. Set `WRITE_DISPLAY_COPIES = True` in `downloaders/common.py`. It is a code change on purpose, and
   `TestTheSwitch::test_the_shipped_default_is_off` pins the shipped `False`, so that test changes in the same
   commit, saying why. **The downloaders' hook has no floor:** from then on every newly downloaded panorama
   wider than `DOWNSCALED_MAX_WIDTH` (8192) gets a copy, not only the ones over the ceiling, so new scrapes
   grow the store at the full +63% below even though step 2 does not.
2. Run `python3 downscale_panos.py <storage-dir> --min-width 16384 --dry-run`, then again without `--dry-run`,
   on each affected store ([by hand](#running-the-sweep-by-hand)). That writes copies for the frames **over
   the ceiling only**, the ones 8192-class GPUs cannot render, and reports every other panorama over the cap
   as `under --min-width` without touching or even reading its copy ([#160](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/160)).
   Only if you mean to restore copies for everything over the cap as well, run it again without
   `--min-width` — and **budget the disk before you do:** that sweep writes a copy for every panorama wider
   than `DOWNSCALED_MAX_WIDTH` (8192), which is nearly every modern panorama, so it is the fleet-wide +63%
   below, not a copy of the new frames alone.
3. Tell the web app's maintainers: the on-demand downscale
   ([SidewalkWebpage#5256](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/5256)) absorbs a wider
   frame silently at a cost per view, and its `pano.downscaled.max-width` has to agree with
   `DOWNSCALED_MAX_WIDTH` for it to find the copies.

#### Running the sweep by hand

```bash
python3 downscale_panos.py <storage-dir> --dry-run          # count the missing copies, write nothing
python3 downscale_panos.py <storage-dir>                    # write them
python3 downscale_panos.py <storage-dir> --max-runtime 240  # a nightly-sized slice; the rest report as unreached
python3 downscale_panos.py <storage-dir> --min-width 16384 --dry-run  # only frames over the viewer ceiling (#121)
```

`--min-width PX` ([#160](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/160)) limits the
sweep to panoramas **wider than** `PX`: one over the cap but at or below it is counted as `under --min-width`
and its copy is never read, so it is reported neither as written nor as already having a copy, whatever is on
disk. A stale copy under the floor is therefore left as it is; run without `--min-width` to refresh it.
`16384` — `VIEWER_MAX_PANO_WIDTH`, and GSV's widest frame today — is the value it exists for: the
[width tripwire](#the-width-tripwire)'s remedy, which should touch only what the viewer fleet cannot render.
A value at or below `--max-width` is refused, since it would filter nothing and quietly be the full sweep.
Every panorama the run examines lands in exactly one of written, under the cap, already had a copy, failed and
under `--min-width`; `unreached` is the part the runtime budget never examined. The summary line always ends
`…, N unreached, N under --min-width.`, as `0` when the option is not given.

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

**If that rewrite fails, the copy is deleted (#122), and the next sweep repairs it — whenever one is run.**
Leaving it would be the one
outcome nothing could ever repair: the write is atomic, so a failed rewrite leaves the *old* copy intact, and
`sidecar_is_current` judges from dimensions alone (a decode per panorama is the whole cost the sweep exists
to avoid) while every gate in `refetch_panos` refuses a swap that changes the frame — so that old copy would
have *exactly* the expected dimensions, and every later sweep would report it `current` and write nothing. A
*missing* copy is what the sweep fills: the next `downscale_panos.py` run sees it absent and cuts a fresh one
from the repaired panorama, and until then the web app serves the native file, which is correct, just larger.
**Nothing schedules that sweep** — it runs only when a person [runs it](#running-the-sweep-by-hand) — so the
copy stays missing until someone does; that is a correct state, not one that repairs itself. The swap is
ledgered `replaced` either way, and `refetch.log` gets one `WARNING` line saying the copy was deleted.
Only that one file goes — the exact `.w<cap>.jpg` for the current cap, never the panorama, a copy at
another cap, or anything else in the shard — and only after a swap has landed, never on a refusal.

> **The one case that needs a person:** if the delete *also* fails, the stale copy is back to reading as
> `current` for ever. That is reported on stdout as well as in `refetch.log` (at `ERROR`), naming the file: delete it by
> hand, then run `downscale_panos.py`.

**Copies already on a store are left alone.** They cost disk and nothing else: every walker excludes them by
name, so a sidecar can never be mistaken for a panorama, and the web app serves whichever of the two it
finds. Removing them is an operator decision; the only copy any tool here deletes is the stale one above.

## The store is an archive, not a cache

A stored panorama is frequently **the only copy of that picture that will ever exist**, so replacing one is a
deletion rather than a refresh. Two measurements say so:

* **Roughly half the labelled panoramas are already retired at Google**
  ([47.9% survival](../reports/2026-08-09-photometa-census.md)). Nothing re-fetches those, ever.
* **Of the ones Google *does* still serve, about a quarter come back as a different picture** — same id, same
  frame, re-posed and re-graded pixels
  ([#114](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/114),
  [the re-render probe](../reports/2026-09-06-rerender-probe.md)). For those, the store holds the rendering
  the labels were actually placed on and Google no longer does.

The rule has two tiers, and they are not in tension:

1. **The download path never overwrites an existing panorama.** All three downloaders short-circuit on
   `os.path.isfile` before spending a single request — `gsv.py`, `mapillary.py`, `panoramax.py` alike. That
   check is also the resume marker, which is *why* it is easy to lose: a future writer that resumes some
   other way inherits none of this protection. It is a rule, not an implementation detail.
2. **A repair path may overwrite only on positive proof the replacement is strictly better.** That is what
   [`refetch_panos.py`'s gates](#what-it-will-and-will-not-do) are — `frame_grew`, `upscaled`, `undersized`,
   `too_black`, plus the `.part`-and-rename — with every other outcome leaving the stored bytes untouched.

**The gap this leaves, and what would close it.** A re-render that kept the frame size passes all four of
those gates: `dims_changed` catches only the ones whose dimensions moved, and nothing downstream can see the
rest, since `pano_x`/`pano_y` still index the file and it still looks right. The gate that would close it is a
**horizon-band MAE against the stored file**, refusing above a threshold. It costs **no extra requests** — the
fresh frame is already decoded at the point of the swap — and it separates the two populations by 45.6×
(≤ 0.0742 luma across 59 same-rendering panoramas, ≥ 3.3823 on all 19 re-rendered), so the threshold is not
delicate. Phase-correlation lock at zero shift is the stronger form.

**It is deliberately not built.** No writer runs today: the `fover` pass was decided against, so the gap is
latent rather than active, and a gate for a pass that does not run is speculative code that would rot.
Writing the requirement down is what stops the *next* repair path shipping without it. Adding it means adding
to `refetch_panos.py`'s `OUTCOMES`, deciding whether it belongs in `LEDGERED_OUTCOMES`, and extending the
byte-for-byte "the original survives" battery in `tests/test_refetch_panos.py`.

Noticing a re-render *without* a repair pass is a separate problem, because detecting one in pixels means
fetching the pixels. The cheap proxy is pose: photometa carries `heading`/`pitch`/`roll` at one metadata
request, 15 of the 19 moved (14 with a pose the fit can name), and
[`photometa_census.py --refetch`](../reports/2026-09-06-rerender-probe.md#how-we-would-ever-notice-this-again)
compares them across runs as `pose_drift`. It catches the re-poses, not the pure re-grades.

## Resume ledgers

Both phases resume from an append-only ledger, and both draw the same line: **a row means the outcome is
permanent.** Transient failures leave no row and retry automatically on the next run.

**`pano_id_log.csv` gates the image phase** (`pano_id,downloaded,fetched_at`):

* `1` — image on disk, or a prior success.
* `0` — the source has nothing for this pano. A permanent verdict, one per source:
  * **GSV** — no imagery at any zoom (a fully black tile at both, on a 200), or unknowable dimensions. No
    breaker entry, deliberately: a retired GSV pano is a permanent verdict and an ordinary one, at 7.9–8.4%
    of a large city's rows.
  * **Mapillary** — a 404, or a record that names the image and carries no original-resolution rendition.
    No Mapillary 404 has ever been observed — its "does not exist" is a 400, measured 2026-09-06 — so the
    record with no rendition is the one that fires in practice, and three of them in a row stop the run
    writing any more (see *[When the image phase stops trusting a source](#when-the-image-phase-stops-trusting-a-source)*,
    [#113](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/113)).
  * **Panoramax** — a 404 **carrying the catalog's own `Feature not found` body**, an item affirming a
    field of view other than 360 (a flat contributor photograph in the same bbox), or a well-formed, non-empty
    assets block that offers no `hd` rendition. Each rests on the catalog affirming something rather than on a
    status code or a missing key, *and* three in a row trip the same breaker: two of the three are
    wholesale failures wearing a per-pano face, so the affirmation and the breaker are both wanted here.

  A GSV probe answered with anything but 200, even with a black body
  ([#166](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/166)), a Mapillary error envelope
  on a 200, a 404 whose envelope carries the auth signature (code 190 / `OAuthException`), a body that does
  not name the image, an image body that is not a JPEG, a Panoramax 404 *without* the catalog's body, a
  malformed or empty assets block, and a redirect off a published `hd` href are none of them verdicts and
  leave no row.
* **no row** — never attempted, the last attempt failed transiently (a network blip, a failed tile, a full
  store), or [the breaker](#when-the-image-phase-stops-trusting-a-source) stopped trusting the source (both
  the withheld tripping verdict and every pano skipped after it). Retried next run.

Deleting `0` rows, or the whole file, is the manual force-retry lever; existing `.jpg`s are simply
re-registered as skipped rather than re-downloaded — with a **blank** `fetched_at`, because the ledger has
no evidence of when they were fetched (next section).

A [store-mode pull](downloader.md#pulling-from-the-project-sidewalk-pano-store) (`--from-store`) writes only
`1` rows, each with a blank `fetched_at`. A pano the store does not hold tonight may be scraped tomorrow, so
its absence is never a verdict: it is counted in field 9 for that run, left unledgered, and retried next run.
It writes nothing to `depth_log.csv` either — a pulled `.depth.npz` is ledgered `saved` by the next scrape's
depth phase, which finds it on disk, at zero requests.

### `fetched_at`, and the two row widths

The third field is when the source answered: for a `1` row written by a download, the moment the file
landed; for a `0` row, the moment the source's "nothing here" was read. Local time with an explicit UTC
offset, the same `log_timestamp()` rendering as [`log.csv`'s column 1](#which-clock-each-field-is-on), so it
says which clock it is on. It exists because [the store is an archive](#the-store-is-an-archive-not-a-cache)
and file mtime was the only record of when a panorama was fetched — a record `refetch_panos`'s
`already_clean` gate already leans on, and one that any `cp` or `rsync` without `-t` destroys.

**A `1` row that registers a file already on disk carries a blank stamp.** That is the `skipped` verdict:
the downloader's `os.path.isfile` short-circuit, the source never contacted. The pixels were fetched by some
earlier run whose row is missing — killed between the atomic save and the append, the ledger deleted as the
force-retry lever above, a torn row the reader dropped, a store assembled by copy — and the only evidence of
*when* is the mtime. Stamping the moment of re-registration would relabel a 2019 fetch as today's, on a row
that is never rewritten, and the next copy without `-t` would then destroy the one date that contradicted
it. So the rule for a reader is **a non-empty stamp means we know when; a blank one, or a two-field row,
means the pixels predate the row and mtime is all there is** — the two spellings of "unknown" mean the
same thing.

**Two widths are legal and a long-lived store holds both.** Rows written by a build older than #129 are
`pano_id,downloaded`; rows after it are `pano_id,downloaded,fetched_at`. #129 was committed 2026-09-10 and
reached the production store on 2026-09-17, so on the store the boundary is the 2026-09-17 run, not the commit
date — a reader cutting a production ledger at 09-10 misfiles a week. Four specifics follow:

* **Old rows are never backfilled.** Two fields means it predates the change and mtime is the only evidence
  there will ever be. Inventing a timestamp from an mtime we already distrust would be worse than the blank.
* **A store created before the change keeps its two-column header** above three-field rows, so `head -1`
  under-reports. Deliberate: rewriting a production ledger in place is the O(n²) truncate-on-crash path
  [#55](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/55) removed, over the only record of
  what has been scraped. `progress_check` reads by position and skips the header by value, so a stale header
  costs nothing. A `csv.DictReader` on such a file *will* put the stamp under the `None` key — read it
  positionally.
* **The stamp is the last field on purpose**, so the id and the verdict stay in columns 1 and 2: `cut -d, -f1`,
  `cut -d, -f2` and `awk -F, '$2 == 0'` keep meaning what they meant. An end-anchored `grep ',0$'` does
  **not** — a timestamped row ends in the stamp, so that grep matches only two-field rows and would
  count the false `0` rows after a breaker trip as zero. Match the verdict by column, never by line end.
* **Parse the stamp defensively.** `progress_check` keeps any row whose id and verdict are intact and never
  reads the third field, so a crash can leave a row whose stamp is torn mid-write. A consumer of `fetched_at`
  treats an unparseable value exactly as a blank one — unknown — rather than raising.

**A repaired panorama's stamp dates the pixels the repair replaced.** `refetch_panos` writes nothing to this
ledger, and `refetch_log.csv` is `pano_id,status` with no timestamp, so after a `replaced` swap the current
pixels' fetch time is again recorded only in the mtime. No repair pass runs today, so this is latent; the
rule for a reader is that a `replaced` row in `refetch_log.csv` supersedes `fetched_at` for that pano. Giving
`refetch_log.csv` a stamp of its own is a separate decision — this change deliberately widened one ledger and
not a second.

**A rollback is the one thing to be careful about.** A build older than #129 reads rows with a hard
`len(row) != 2` and silently skips every three-field one — so every verdict recorded since the widening (on
the production store, since 2026-09-17) vanishes, and a fully-timestamped ledger parses as *empty*,
with nothing raised: permanent `0` verdicts stop being terminal and go back to Google nightly, duplicate rows
accumulate, and `log.csv`'s column 9 loses its prior-failure seed. Reader and writer ship in one commit, so
this cannot happen from a partial deploy; it can only happen by deliberately deploying an older build over a
store that has already been written. Roll forward rather than repairing.

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

**It replaces a stored panorama only when the replacement is strictly better** — this tool is the second tier
of [the archive rule](#the-store-is-an-archive-not-a-cache), which is where the reasoning lives and which also
records the one case these gates do not cover. Every outcome but `replaced` leaves the stored bytes untouched,
and the swap itself goes through the same `.part`-and-rename as a download.

The subtlest of the refusals is `frame_grew`, and it is the reason the tool probes before it fetches. The
store is a scrape-time archive and Google re-serves panos larger, so a grid sized from a stored 13312×6656
file can be too small for what Google now holds. That fetch does not return a smaller version of the pano —
it returns the **top-left 81% of it**, at exactly the stored file's dimensions, with no undersized tile and
no black anywhere. Nothing downstream could ever see it. Two requests, spent before the 512-tile fan-out,
rule it out. A probe answered with anything but 200 raises, so the pano counts as a transient failure rather
than as a frame that covers; before [#166](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/166),
a 403 or 404 with a black body passed it.

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
marker and are not re-cut without `--force`](cropper.md#outcomes-exit-code-and-re-runs), so after a pass every crop cut from a `replaced` pano's
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
never shift. Field 19 was added by [#124](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/124) (for [#43](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/43)); every row written before that
deployed has 18 fields, and the analyzer reads them with the last one blank.

| # | field | notes |
|---|-------|-------|
| 1 | run start timestamp | ISO-8601 **with an explicit UTC offset**, e.g. `2026-09-05 20:30:04.277106+00:00`. Rows written before [#101](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/101) are `str(datetime.now())` — the same shape without the offset, and without the `.ffffff` on the rare run whose microsecond landed on exactly 0. Read a bare one as UTC: every scraper host has run UTC, which is why the omission was survivable for as long as it was |
| 2 | metadata successes | always `0` (stub) |
| 3 | metadata failures | always `0` (stub) |
| 4 | metadata skipped | count of image-eligible panos (stub) |
| 5 | metadata total processed | count of image-eligible panos (stub) |
| 6 | metadata phase duration | effectively `0` (stub) |
| 7 | image successes | |
| 8 | image fallback successes | downloaded, but at a fallback resolution — only a lower level was available (zoom 3, or since [#74](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/74) zoom 4) for a frame whose reported dimensions need a higher one, so the stitch was upscaled to reach them. Real imagery, materially less of it. **Not** simply "downloaded at zoom 3": an old pano whose own max zoom is 3 is at its native resolution and counts in field 7. Was a constant `0` before [#52](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/52) because nothing ever returned the verdict, so runs before that show every fallback inside field 7 |
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
| 19 | depth corpus size | the number of GSV panos the depth phase was given — the denominator for the backfill's progress, which nothing else in the row carries (field 16 says how many are resolved, not out of how many). Known before either phase runs, so it is present on a crashed run too; blank only on a row written before it was counted (the timestamp-only rows: a run that died in the pano-list fetch or between it and the phases, a `pano-schema-drift` stop, which fetched the list but ran neither phase, and a stop just before the count — see [Blank fields mark a crashed or stopped run](#blank-fields-mark-a-crashed-or-stopped-run)) and on every row older than the field. Written whether or not depth ran, so a `--skip-depth` or stood-down run reads `0,0,0,0,0,K` — five zeros and the work still waiting. A `0` here is not a corpus: it is what an empty or source-less pano-list answer writes, and the analyzer refuses it in favour of an earlier row rather than reporting the city as having no GSV panos |

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
real counts, and every phase field from the first unfinished phase onward is blank. Field 19, the corpus size,
is not a phase result: it is known before either phase runs, so it is filled on a crashed row too. Visibly
missing data, never a fabricated `0`. A row that is only a timestamp, field 19 included, means neither phase
ran, and `scrape.log` says which of four reasons it was:

- **the pano-list fetch against the webserver failed**, the likeliest: `Run crashed before the scrape started`
  with its traceback;
- **the list was fetched but its schema had moved**, and the run stopped before scraping it (the
  `pano-schema-drift` condition, [#161](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/161)):
  `Pano list schema drift`, and `WARNING: the pano list's schema has moved` on stdout, which cron mails;
- **a stop or crash after the fetch but before the phases**, for example while the store's ledgers are read to
  judge an empty list: `Run crashed before the scrape started` too, with a traceback that is not the fetch's
  (a stop's ends in `SystemExit: 143`);
- **a stop or crash in the instant before the corpus is counted**, at the top of the scrape: `Run failed`.

A stop or crash *after* the count, in the budget split below, leaves every phase field blank but field 19
filled, and also logs `Run failed`.

Blanks are new as of [#49](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/49) — historical
rows are all-integer — so readers must treat them as missing data (`pandas.read_csv` surfaces them as `NaN`,
turning those columns `float64`) rather than feeding them to `int()`.

Fields are accumulated in memory and written once in a `finally`, which is why even a crash between phases
produces a single full-width row. `SIGTERM` is translated into `sys.exit(143)` so a stop runs those `finally`
blocks instead of discarding the evidence. The `try` behind that `finally` opens before anything slow, the
budget split included: with a `--min-depth-runtime` reservation the run reads the city's whole
`depth_log.csv` off the store before either phase starts, and a stop there used to exit 143 with no row at
all. It now leaves a row whose every phase field is blank, the XML stub's included, with field 19 filled,
since the corpus size is counted before the ledger is read; a stop in the instant before even that count
leaves field 19 blank too, never `0`. Between the pano-list fetch and the phases, the handler that writes
the fetch-failure row covers every statement, including the ledger read that judges an empty list, so a
stop there leaves the timestamp-only row.

### The depth failure count is not an alert signal

Field 14 includes `unavailable` — a permanent, expected, non-actionable outcome — so the first backfill runs
show large failure numbers that are entirely normal. The success/failure/unavailable split goes to stdout and
`scrape.log`; the row has no separate column for it.

Its size is not a signal; **a night on which it is the only outcome is.** The fleet saves about 60% of its
requests on a healthy night (63.8% over the week to 2026-09-27; per city anywhere from 35% to 100%), so several nights of requests with field 13 at `0` is an outage, and the
[log analyzer](log-analyzer.md#checks) reports it — see
[When the depth phase saves nothing](#when-the-depth-phase-saves-nothing).

### Reading the backfill from the row

**`depth_total` (field 16) is not the resolved count.** It is success + failed + skipped, and `depth_fail`
carries *transient* failures alongside permanent `unavailable` verdicts — and a transient failure is
deliberately not ledgered, so those panos are requested again next run. Reading progress from field 16 lets a
city report `depth complete` while panos have no artifact and never will (the ordinary end-state of a
backfill: the last stragglers are exactly the ones that keep failing), and makes the figure run **backwards**
when a heavy-failure night is followed by a quiet one.

What is resolved is what the ledger holds: **`15 + 13`** (skipped, i.e. everything the ledger already
accounted for at the start of the run, plus this run's saves). That undercounts by this run's `unavailable`
verdicts — permanent and ledgered, but indistinguishable from transient ones inside field 14 — which arrive
in the count one night later, when the next run reads them back as skips. It is a lower bound that converges,
and it can only ever delay "complete", never assert it falsely.

So the work left is `19 − (15 + 13)`, the request rate is the per-night sum of `13 + 14`, and the ETA is the
work left over the rate at which **panos** (not requests) are being resolved — which is what the
[log analyzer](log-analyzer.md#the-depth-backfill) prints per city and for the fleet.

Two shapes to read carefully:

- A row whose five depth fields are all `0` is a phase that **accounted for nothing** — `--skip-depth`, a
  block latch, `streetlevel` missing, a run that crashed before the phase, or a city whose ledger is still
  empty and whose image phase spent the whole budget. It is not a city with nothing resolved, so the analyzer
  takes "resolved" from the newest row on which the phase ran.
- An **unwritable ledger does not write five zeros.** `download_depth_maps` returns
  `(0, 0, skipped, skipped)` when it cannot open `depth_log.csv`, so the row looks like a phase that reached
  the ledger and made no requests. The row cannot distinguish that from running out of budget, which is why
  the analyzer names both candidates instead of asserting one.

## A GSV pano refused for a frame disagreement

Since [#74](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/74) the image phase refuses a GSV
pano whose app-reported `width`/`height` is not a frame Google serves, rather than stitching the top-left
corner of a larger one. Each refusal is one stdout `WARNING` and one `scrape.log` `ERROR`, both containing
`frame disagreement`; it is counted in `log.csv` field 9, never ledgered, and retried every run. Field 9 is
seeded with older failures, so the count is not readable there. Since
[#185](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/185) the run counts them itself: a
nonzero count prints one end-of-phase line on stdout and in `scrape.log`,

```
IMAGEDOWNLOAD: WARNING - 2 pano(s) refused for a frame disagreement; not ledgered, retried next run. See docs/ops.md, 'A GSV pano refused for a frame disagreement'.
```

writes `frame_refusals` into the run summary, and raises the `frame-disagreement`
[condition](downloader.md#a-city-can-finish-ok-and-still-fail-the-night), so **one refusal fails the night**
and reaches the alarm ([Hearing about a bad night](#hearing-about-a-bad-night)). The condition's detail names
the first refused pano; `grep "frame disagreement" <store>/<city>/scrape.log` still lists the rest. A refusal
is Google answering, so it never feeds the `images-no-success` condition, however many there are. The remedy
is on the app side: a SidewalkWebpage `gsv_data` refresh that brings the stored dimensions up to what Google
serves now. Whether the refused pano should instead be kept at Google's larger frame is #185 Part 2, waiting
on about 30 nights of these counts.

## When the depth phase stands itself down

Several things stop depth without stopping the run (the latch has two sources, the depth phase and the
image phase), and they look identical from `log.csv` (all five depth columns are `0`), so read stdout or
`scrape.log` rather than the row. Each one is also a
[condition](downloader.md#a-city-can-finish-ok-and-still-fail-the-night) that **fails the night** (#161): the
city stays `ok`, but the queue exits 1 and the night's message carries one line per code.

| what you see | code in the night's message | what happened | what to do |
|---|---|---|---|
| `WARNING - Google refused this host N hours ago, so the depth phase is standing down` | `depth-stood-down` | An earlier run on this host was blocked, and the **block latch** is still fresh. If an `IMAGEDOWNLOAD: WARNING - Google refused a photometa request` line comes before it in the same output, *this* run was refused: that is the third row, not this one. Every city skips depth at **zero requests** until it expires (6 h). GSV images still download meanwhile, but on probation (one refused pano stops them) and with every zoom from the tile probe rather than photometa. | Nothing, usually. It is the fleet declining to walk back into the same wall. If it persists past a day, look for a captcha/consent interstitial from this IP. It alarms even on a night nothing was refused, because the latch outlives the window: a stand-down at 19:00 is a refusal the queue never saw (a manual backfill, say). |
| `WARNING - the depth phase stopped early because Google stopped answering` | `depth-refused` | *This* run was refused. It set the latch, so the next city will skip rather than rediscover. | Check for a rate limit before the next night. The pacer backed off for the rest of that run and forfeited the standing the next run would have inherited, so once the latch expires the next city opens at `depth_start_interval` again. |
| `IMAGEDOWNLOAD: WARNING - Google refused a photometa request`, then the first row's line with `0.0 hours ago` | `depth-stood-down` | *This* run's GSV image phase was refused on its per-pano photometa request ([#74](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/74)). It set the latch and forfeited the earned depth pace; the rest of the image phase takes its zooms from the tile probe and still downloads. If the line says the latch *could not be written*, nothing else on this host stands down for it. A 5xx storm on photometa is not this row: it reads `photometa did not answer` and latches nothing. | As the row above: check for a rate limit before the next night. |
| `WARNING - Google refused 3 GSV panos in a row (HTTP 429)` earlier in the same run, then the latch line above | `depth-stood-down` | The **image phase's** push-back breaker tripped and set the latch itself ([below](#when-google-pushes-back-on-the-image-phase)). Unlike the other rows the city does not stay `ok`: the trip puts `gsv` in the tripped set, so it exits 1 and is booked `failed`. | As for the row above: the same host, the same refusal, seen from the tile endpoint instead. |
| `WARNING - the depth phase stopped early after 25 consecutive failures (…)` | `depth-breaker` | 25 transient failures in a row. The breakdown in brackets says whether they were the store or the network. | `storage` dominant: the store is full or unmounted. `network`/`unexpected`: look at the last error before blaming Google. |
| `WARNING - cannot read the depth ledger` / `cannot write the depth ledger` | `depth-ledger-unusable` | `depth_log.csv` could not be opened. The phase sat the run out rather than re-request the whole corpus against a sick store. | Check the mount and the file's permissions. |
| `WARNING - streetlevel is not importable` | `depth-unavailable` | The interpreter the runner ran under cannot import `streetlevel`: a missing or half-written install. | Reinstall `requirements.txt` into `.venv` (see [Deploying](#deploying)). |

The latch is a file in the system temp directory, **not on the store** — it records this host's standing
with Google, and the storage directory a run is given belongs to a single city. `--depth-block-latch PATH`
moves it, for both phases. To clear one by hand, delete the file; a missing, unparseable or implausibly
future-dated latch all mean "not blocked", because a latch nobody can read must never be able to stand the
whole fleet's depth phase down indefinitely.

Beside the **default** latch lives the pacer's **earned standing** (`sidewalk-depth-pace`,
`--depth-pace-state PATH` moves it — the two paths are independent, so moving the latch alone leaves this
file in the temp directory): the request interval and clean streak the last run on this host earned, which
the next run opens at instead of ramping down from `depth_start_interval` again. Deleting it costs one ramp
(~1,400 requests); an unreadable, `NaN`, or day-old file is ignored the same way, and so is one nothing can
parse at all. It never holds a value slower than the opening interval, so it cannot be used to slow the
fleet down, only to keep the speed it has already earned. Only Google's own push-back or refusal forfeits it
(since [#74](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/74) including a photometa
refusal met by the image phase) — a
local network blip or one malformed pano slows the running phase down and leaves the file alone, the same
rule the latch follows when it declines to blame a full disk on Google — and a phase that made no
requests writes nothing.

A `sidewalk-depth-pace.lock` sits beside it. The depth phase holds it for its whole duration so exactly one
live process spends the host's standing; a second concurrent phase logs a `WARNING`, paces itself from
scratch and writes nothing. It is advisory (`flock`/`msvcrt`, released by the OS when the holder dies),
never an `O_EXCL` file, for the reason the queue lock is: a lock that outlived a crash would disable the
feature silently and for ever. Deleting it is safe; it is recreated on demand. Only a *contended* lock is
reported as a second phase; a lock file that cannot be opened or a filesystem that cannot lock at all
(`ENOLCK`) is logged to `scrape.log` as `cannot open the pacing lock`, with nothing on stdout, and the run
remembers nothing.

**Do not read a stood-down phase as lost work.** Nothing is ledgered on either path, so every unresolved
panorama is retried on the next run. See
[Depth → Being a good citizen](depth.md#being-a-good-citizen-of-googles-servers).

## When the depth phase saves nothing

A phase that makes requests and saves none of them looks, from the stall check, like a phase that is
working: a failed request is still a request. The [log analyzer](log-analyzer.md#checks) therefore watches
**saves** separately ([#163](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/163)), and
reports a city whose phase has asked at least 10 times on its last 3 *requesting* nights and saved nothing. A
night with no row, or a stand-down's five zeros, is not counted, so a `--skip-depth` rollback followed by one
bad night does not fire. It has two arms, and `log.csv` can only tell them apart across two runs, because an
`unavailable` verdict is ledgered at once and comes back as the next run's skip (field 15) while a transient
failure never does:

| what you see | what happened | what to do |
|---|---|---|
| 🟡 `Depth phase saved nothing on the last N nights it made requests … retry` | Every request failed **transiently**: the consecutive-failure breaker (network, a full or unmounted store), Google refusing requests, or a payload the decoder chokes on. Nothing was ledgered. | Read the `DEPTHDOWNLOAD` lines in `scrape.log` for the cause. No panos are lost; they retry once it is fixed. |
| 🔴 `Depth phase saved nothing … and is writing panos off` | The ledger grew by at least half of the failures: they are being written as **`unavailable`**, which is permanent. Measured shape: upstream drift (a `streetlevel` or depth-payload change) that makes every pano's depth read as absent, so the whole corpus is written off at ~1,900 panos a night while the stats line shows a healthy rate. | Stop it tonight, then scrub the ledger — below. |

**Scrubbing the ledger after a write-off.** A false `unavailable` row costs that pano its depth for ever (it
is never re-requested); removing a true one costs one request. So err towards removing.

1. **Stop the depth phase:** put `--skip-depth` back after the `--` in the cron line
   ([Rolling back](#rolling-back-smallest-blast-radius-first)). If a queue is running, let it finish or stop it
   the way that section describes.
2. **Copy the ledger first**: `cp -p depth_log.csv depth_log.csv.bak-$(date +%F)` in the city's store
   directory.
3. **Find the last save.** Nothing was saved during the barren span, so its rows are the file's tail:

   `awk -F, '{ sub(/\r$/, "") } $2 == "saved" { n = NR } END { print (n ? n : 1) }' depth_log.csv`

   prints the line number of the last `saved` row (or `1`, the header, if the city never saved), and
   everything after it is the span's `unavailable` rows — the false ones, plus at most a few genuine verdicts
   from the night the drift began. **`depth_log.csv` has CRLF line endings** (`csv.writer`'s default; unlike
   the image ledger, the depth ledger does not override it, and production files are all CRLF, so it stays
   that way). That is why this is not a `grep ',saved$'`: on Linux `$` does not match before the `\r`, so the
   grep finds nothing on the box — while Git Bash on Windows strips the CR and matches, so a dry run on a
   desktop passes. `tests/test_depth_phase.py` runs this exact line against a ledger the depth phase wrote.
4. **Truncate after that line**: `head -n <line> depth_log.csv > depth_log.csv.new && mv depth_log.csv.new
   depth_log.csv`. Before the `mv`, check the line count it drops: about the CRITICAL's written-off figure
   plus the newest run's field 14, whose verdicts the report could not see yet.
5. **Fix the cause, then do one hand run** of that city (`scrape_queue.py … --only <city_id>`) and read its
   `DEPTHDOWNLOAD: Completed …` line: it should be saving again before `--skip-depth` comes off the cron line.

Repeat 2–4 for every city the report flags; drift hits the whole fleet at once, so expect all of them. A
small city can be written off entirely in one night — its phase then walks its whole list, which the analyzer
cannot tell from a finished backfill — so after drift, check every city's `depth_log.csv` for a tail of
`unavailable` rows since the date the large cities name, not only the flagged ones.

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
`pano_id_log.csv` has no source column, so match on the id shape — Mapillary ids are
all-numeric, GSV's are 22-character base64, Panoramax's are UUIDs. Delete those two rows, fix the
credentials, and the next run picks up everything the breaker skipped, because none of it was ledgered.

The run **exits nonzero**, so `scrape_queue.py` books the city as `failed` and the night's message
([Hearing about a bad night](#hearing-about-a-bad-night)) carries the queue summary.
Only the tripped source stops: a city carrying both GSV and Mapillary panos keeps downloading GSV. `log.csv`
is unchanged — its fields are counts of work and the breaker is not one of them, so stdout, `scrape.log`
and the exit code are where this lives.

GSV has no *permanent-verdict* breaker, deliberately: 7.9–8.4% of a large GSV city's ledger is a permanent
verdict (retired imagery), so three in a row is routine there rather than evidence — about every 1,700 panos
at 8.4%. (It has a different one, for Google refusing the host:
[When Google pushes back on the image phase](#when-google-pushes-back-on-the-image-phase).) The
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

## When Google pushes back on the image phase

A tile answered HTTP 429 or 403, or landed on Google's `/sorry/` or consent interstitial (whatever status
the interstitial itself answered with), is **push-back**, not a transient ([#162](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/162)). It is never
retried, the rest of that pano's tiles are abandoned (at most `thread_count` requests were in flight), and
the pano gets exactly one line in `scrape.log`:

```
IMAGEDOWNLOAD: Failed to download pano <id> (HTTP 429): refused by Google: tile (3, 1) answered HTTP 429; 8 of
512 tile requests made, the rest abandoned
```

The zoom probe, which every GSV pano meets first, reads the same way. A probe answered 403 (which since
[#166](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/166) raises rather than handing back a body) is `(HTTP 403)`; one
whose retry policy gave up on 429s is `(HTTP 429)`, with urllib3's message after it; one that landed on the
interstitial, at any status, is `(interstitial)`. A probe that gave up on 5xx **without** an interstitial
anywhere in its path does **not** count: that is Google being ill, not Google refusing us, and latching the
fleet over an outage would stand every city's depth down for six hours. Neither does any other non-200
(404, 410): it stays an ordinary failure, retried next run.

Three refused GSV panos in a row stop GSV images for the rest of that run:

```
IMAGEDOWNLOAD: WARNING - Google refused 3 GSV panos in a row (HTTP 429). Stopping GSV images for this run.
Block latch /tmp/sidewalk-depth-blocked written, so the depth phase stands down too.
IMAGEDOWNLOAD: WARNING - Google pushed back on GSV imagery (HTTP 429); 4210 pano(s) were left unattempted and
nothing was ledgered for them, so they retry next run. No ledger repair is needed. Check this host for a rate
limit before the next run; the depth phase stands down while the block latch /tmp/sidewalk-depth-blocked is fresh.
```

What that means, and how it differs from [the #113 breaker](#when-the-image-phase-stops-trusting-a-source):

- **No ledger repair.** A push-back is never a verdict, so nothing was ledgered for the refused panos and
  nothing is withheld. The trip costs zero false rows.
- **The city is booked `failed`** — the run exits 1 through the same tripped-sources channel — and its run
  summary says `image_stop: blocked`, so `scrape_queue` does not spend an extra pass on it. A budget stop
  later in the same run (another source's pano reaching `--max-runtime`) does not overwrite it.
- **Depth stands down too.** The trip writes the block latch and forfeits the depth pace this host had
  earned, because tiles and photometa leave the same IP: the same run's depth phase, and every city after it
  for 6 hours, skips depth at zero requests ([above](#when-the-depth-phase-stands-itself-down)). If the latch
  cannot be written (its directory is gone, the disk is full), both lines say `could not be written` instead,
  and nothing else stands down: not this run's depth phase, and not the next city.
- **A fresh latch means probation, not a stand-down.** Any image phase starting while the latch is fresh
  (whoever wrote it) prints `GSV images run on probation - one refused pano stops them` and runs normally; the
  first refused GSV pano trips the breaker instead of the third. Images are never skipped on the latch alone —
  the depth phase writes it after a single photometa refusal, and that must not stop every city's images.
- **Only a GSV success resets the count.** A timeout, a 404, a skip, a permanent verdict and any other
  source's outcome neither count nor reset. Other sources keep downloading after a GSV trip.

**What to do.** Look for a rate limit or a captcha on this host's IP before the next night. The count per
city is `grep -cE "Failed to download pano \S+ \((HTTP [0-9]+|interstitial)\)" */scrape.log`.

**403 is push-back by analogy, not by measurement.** The retained `scrape.log*` files (2026-09-27, each city's
last ~40 MB) hold no tile 403 and no tile 429 at all, only 55 retried 503s, every one of which recovered; so
the classification rests on how the depth endpoint behaves, not on data from this one. If the same pano id
keeps turning up as `(HTTP 403)` night after night, rather than many ids in one burst, 403 is a per-pano
answer, not a refusal, and should leave `TILE_PUSHBACK_STATUSES`: under a fresh latch one such pano near the
head of the shuffled list would trip probation and rewrite the latch every night.

## What healthy looks like

A mature city settles into: `image_success` small or zero most nights, stable `image_fail`, and
`image_skip ≈ image_total`. The [log analyzer](log-analyzer.md) encodes the rest of the heuristics, including
what "stale" and "ended early" mean in practice.

During the depth backfill, add: `depth_skip + depth_success` (fields 15 + 13, [what the ledger
holds](#reading-the-backfill-from-the-row)) climbing night over night towards field 19, and `depth_fail`
large but *stable* — it counts `unavailable`, which is permanent and expected, so it is not an alert signal.
The split goes to stdout and `scrape.log`. The analyzer's stats line puts it in one clause:
`depth 1,753/183,680 (1.0%) · +590 panos/night · ~308 nights left`.

## Operating the production host

The nightly scrape runs on one small cloud instance (Ubuntu 22.04 / Python 3.10, the CI baseline) with the repo
at `/srv/sidewalk-panorama-tools` on `master`, its virtualenv at `.venv`, the pano store sshfs-mounted at
`/mnt/panostore` by a systemd mount unit, the city manifest at `/etc/sidewalk/cities.csv`, and the one crontab
line from [Nightly deployment](downloader.md#nightly-deployment). Which instance, its addresses and who holds
which key are deliberately **not** here; they live in the team's private planning repo, next to the rest of the
account-level detail.

### Deploying

Merging to `master` changes nothing on the host until someone pulls — it sat 13 merged PRs behind for eleven
days in September 2026. A deploy is:

```bash
cd /srv/sidewalk-panorama-tools
git status --short                                     # must be empty: a local edit (config.py has carried them) aborts the pull
before=$(git rev-parse HEAD)
git pull --ff-only
git log --oneline -1                                   # what is live now
git diff --stat "$before" HEAD -- requirements.txt     # non-empty -> .venv/bin/pip install -r requirements.txt
.venv/bin/python -m py_compile DownloadRunner.py scrape_queue.py downloaders/*.py
.venv/bin/python -c "import DownloadRunner, scrape_queue"
```

The order matters twice: the requirements check comes *before* the import check, because a new dependency
fails the import first and reads as a broken deploy; and it diffs against a captured SHA rather than `HEAD@{1}`,
because a pull that brought nothing leaves `HEAD@{1}` pointing at the deploy before, so the diff would report the
previous deploy's changes again.

- **Pulling while the queue is running is safe; a `pip install` is less so.** Every repo import is at module
  level, so a city already running keeps the code it loaded, the next city starts on the new tree, and the
  queue process itself keeps its old code until the next night. The 2026-09-17 deploy landed with the queue on
  its 30th city. `streetlevel` is the exception: it is imported lazily when the depth phase starts, so a city in
  its image phase while pip rewrites the package loads whatever is half-written. If `requirements.txt` changed,
  do the install between cities (watch `scrape_queue.log` for the `ok`/`failed` line) or when the queue is idle.
  A half-written package that raises `ImportError` now says so on stdout and fails the night as
  `depth-unavailable` (#161) rather than skipping depth silently; one that raises something else still crashes
  the city and books it `failed`.
- **Create the [store marker](#the-store-marker) BEFORE pulling #161.** From that deploy on, a queue that
  finds no `<store-root>/.pano-store` exits 5 having run nothing, so the first night after a deploy without
  it scrapes nothing (loudly).
- **Roll forward, never back, past 2026-09-17.** [`fetched_at`](#fetched_at-and-the-two-row-widths) widened
  `pano_id_log.csv` to three fields, and a pre-#129 reader skips every three-field row — so every permanent
  verdict recorded since that deploy is re-requested nightly, and a store that has only ever seen the new
  build parses as *empty*. Behaviour rolls back by flag (below), not by checkout.

### The store marker

The queue refuses to scrape a store root that does not carry the file **`<store-root>/.pano-store`** (#161).
Before it, an sshfs mount that had dropped left an ordinary local directory at `/mnt/panostore`, and the
queue created the store root there, every city found no ledgers, re-downloaded its corpus onto the 30 GiB
root disk and exited 0 — the files hidden again once the mount came back. The marker lives **on the remote
store**, so it survives remounts and is absent from the empty directory under the mount point.

- **Missing at startup:** the queue prints one line on stderr naming the path and exits **5** having run
  nothing and written nothing — not the store root, not `scrape_queue.log`, not the lock. `cron_notify`
  passes 5 through, so the night's message says `exit 5`.
- **Missing before a city starts** (the check repeats before every city, in every pass — sshfs can drop at
  02:00): that city is booked `store_missing` and never started. The summary gathers them into one
  `STORE_MISSING (store not mounted; not started): …` line, the totals line says `N not started (store not
  mounted)`, and the night exits 1. The check is per city, so if the mount returns, later cities run.
- `--dry-run` prints a `WARNING` when the marker is missing and keeps its exit code.
- `DownloadRunner` does not check the marker: a hand run into an arbitrary directory is a documented use.

`chown root:root` + `chmod 555` on the underlying mount point
([downloader.md](downloader.md#if-the-pano-store-is-on-another-host)) stays as defence in depth.

**Creating it** (once per store root, and once for any new store root or host):

1. With the store mounted — `findmnt /mnt/panostore` shows `fuse.sshfs` — and as the cron user:
   `printf 'Project Sidewalk pano store; see docs/ops.md#the-store-marker\n' > /mnt/panostore/.pano-store`
2. Prove the directory *under* the mount is unmarked, without unmounting:
   `sudo mkdir -p /tmp/under && sudo mount --bind / /tmp/under && ls -la /tmp/under/mnt/panostore` must be
   empty. Remove a stray `.pano-store` there; if an earlier unmounted scrape left city directories or
   ledgers there, move them aside (e.g. to `/var/tmp/under-panostore-<date>/`) rather than deleting them,
   since they may be the only copy of that night's downloads. Before the next step, confirm the mount is
   made by root — `systemctl cat mnt-panostore.mount` shows a system unit with no `User=` — because a
   user-mode `fusermount` needs write access to the mount point, and after the `chmod 555` a refused remount
   would exit 5 every night. While it is bound,
   `sudo chown root:root /tmp/under/mnt/panostore && sudo chmod 555 /tmp/under/mnt/panostore`; then
   `sudo umount /tmp/under`. Record the date in the private runbook.
3. `.venv/bin/python scrape_queue.py --cities /etc/sidewalk/cities.csv --store-root /mnt/panostore --dry-run`
   prints no marker `WARNING` (and no `streetlevel is not importable` one).
4. Optional proof of the alarm: a throwaway crontab line (same crontab, so it inherits `SHELL`/`BASH_ENV`)
   running the queue under `cron_notify` with `--store-root /tmp/no-marker --only <city>` exits 5 before
   running anything, a `…: exit 5 on <host>` message arrives, and `cron_notify_probe.log` says `published`.
   Delete the line.

**The morning after:** `exit 5` in `~/cron_notify.log` means the store was not mounted at 19:00
(`systemctl status mnt-panostore.mount`, restart it, check the marker); a `STORE_MISSING` line means the mount
dropped mid-night.

### Rolling back, smallest blast radius first

1. **Stop the depth backfill:** add `--skip-depth` after the `--` in the cron line. Images are unaffected.
2. **Take one city out for a night:** prefix its manifest row with `#`.
3. **Re-run one city through the queue's own machinery** (lock, budgets, summary):
   `scrape_queue.py --cities … --store-root … --only <city_id> -- --all-panos`.
4. **Reinstall an earlier crontab** from the dated backups in the user's home (`crontab <file>`). Take a new
   backup first; one of the old ones still carries a secret and is mode 600 for that reason.
5. **Stop everything:** `crontab -r` after backing up — and if a queue is running,
   `pkill -TERM -f '^\S+/python \S+/scrape_queue\.py'` as well, because removing the crontab only cancels future
   starts. SIGTERM is the right signal: the queue translates it into an orderly exit that stops the city it is
   supervising, which in turn writes its `log.csv` row, and releases the lock. The pattern is anchored so it
   matches the queue process and **not** the [`cron_notify.py`](#hearing-about-a-bad-night) wrapper around it,
   whose own argv also contains `scrape_queue.py`: the wrapper forwards a SIGTERM it receives to the queue, so a
   bare `pkill -f scrape_queue.py` would deliver two, and the second lands while the queue is stopping its city.
   The queue then kills the city outright rather than orphaning it (#161), but the kill costs that city's
   `log.csv` row, which the first SIGTERM would have written. The store is untouched by any of this.

### Adding a city

1. Append `city_id,fqdn` to the manifest. Both halves come from `https://<fqdn>/v3/api/cities`: the `city_id`
   must be the app's own id (it reads its crops from `<store-root>/<city_id>`, so any other name scrapes into a
   directory it never looks at), and the fqdn cannot be derived from the id. Check
   `https://<fqdn>/adminapi/panos` answers first.
2. `scrape_queue.py … --dry-run` takes no lock, shows the city in the plan while the queue runs, and
   [cross-checks the manifest](downloader.md#the-manifest-is-cross-checked-against-the-fleet) — any city
   still missing a row, or listed under the wrong id, is named and the dry run exits 1. A private deployment
   that is deliberately not scraped here gets a `#city_id,fqdn` row instead, once.
3. Add the same `city_id` to `log_analyzer/cities.csv`, or the analyzer never looks at it — and `CropRunner.py`
   refuses `--city` for it (exit 2), since that roster is what it checks the name of a crop store against
   ([#159](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/159)). A `#` row there means
   two things at once: the analyzer stops monitoring the city, **and** `CropRunner` stops accepting it. So
   do not comment a city out of `cities.csv` to quiet the analyzer if its crops are still wanted.
4. Nothing else: `DownloadRunner` creates `<store-root>/<city_id>` on its first run.

### Hearing about a bad night

The queue's nonzero exit is the alarm, and every doc in this repo used to say "cron mails it". **The production
host cannot send mail** — it has no MTA, and from the day it was built (2026-09-01) until the wrapper below went
in (#141), `syslog` recorded `No MTA installed, discarding output` after every nightly while nothing else raised a
flag. So the crontab line runs the queue through `cron_notify.py`, which is cron's own rule with the delivery made
pluggable:

```
cron_notify.py --name scrape-queue --only-on-failure --log /home/ubuntu/cron_notify.log \
    --sink 'aws sns publish --region us-west-2 --topic-arn <arn> --subject "$NOTIFY_SUBJECT" --message file://$NOTIFY_BODY_FILE' \
    -- <the queue command>
```

- **The wrapper runs the command, streams its stdout+stderr through, and when it exits hands the whole capture
  to `--sink`.** Its default is cron's rule — deliver whenever there was any output, which is what lets a
  `WARNING` on an otherwise clean night reach anyone — and production passes `--only-on-failure` instead
  (decided 2026-09-18): **a message on a bad night, silence on a good one**. The subject is
  `scrape-queue: exit N on <host>`, the body is the queue's output, and a failure that printed nothing is
  still delivered with a body saying so. The cost is that a `WARNING` on a night that exited 0 is not mailed;
  it is still in that city's `scrape.log`. That cost is why every shape the runner itself calls a failure —
  a refused or stood-down depth phase, a missing Mapillary token, an empty pano list — is now a
  [condition](downloader.md#a-city-can-finish-ok-and-still-fail-the-night) that makes the night exit 1
  (#161). The summary carries **one line per condition kind**, naming the first city and listing the rest, so
  a refusal followed by 40 stood-down cities is two lines:

  ```
  [queue] depth-refused: Google refused the depth phase; latch written - 1 city, first chicago-il: HTTP 429 ...
  [queue] depth-stood-down: depth stood down on the block latch - 40 cities, first columbus-oh: latch set 0.2h ago (...); also ...
  [queue] 53/53 cities ok, 0 failed, 0 timed out, 0 not reached, conditions: depth-refused, depth-stood-down; 610.2 min total
  ```

  The first night after this lands may surface a long-standing silent condition; that is intended. The sink gets the body on stdin and in the file `$NOTIFY_BODY_FILE`
  names (`aws` reads it with `file://`, which sidesteps the 128 KB single-argument limit a `"$(cat)"` would
  hit), plus `$NOTIFY_SUBJECT` and `$NOTIFY_EXIT`.
- **Delivery is SNS, published with the instance role** — no credential on the box, no mail-service
  onboarding; an email subscription on the topic does the rest. The topic ARN, the role policy and who
  subscribes are in the private runbook. Nothing in this repo knows what the sink is; a host with a working
  MTA could pass `--sink 'mail -s "$NOTIFY_SUBJECT" ops@example.org'` instead.
- **The exit code is the queue's own.** The one code the wrapper adds is **4**: the queue exited 0 and the sink
  failed. A queue exit 1 plus a sink failure stays 1. Either way the wrapper prints `cron_notify: sink failed
  ...` on stderr — which cron discards on this host, which is what `--log` is for: one line per run, `<stamp
  with offset> exit <code> published <n> bytes` / `nothing to publish` / `sink failed (exit N)`. **A failed
  publish is only visible there**, so `tail -1 ~/cron_notify.log` is part of the morning check below. A
  queue that cannot be *started* (a moved interpreter, a bad path after a deploy) is reported *through* the
  sink with exit 127, since that is precisely the night nobody would otherwise hear about.
- **The message is the alarm, not the record.** SNS caps a message at 256 KB and a backlog night prints one
  line per pano, so the wrapper cuts the capture to `--max-bytes` (200 000 by default), keeping the head and
  most of the tail — the queue's summary is at the end — with a `[cron_notify] N bytes omitted` line between
  them. The full narrative is always `<store-root>/scrape_queue.log` (one `ok`/`failed`/`timed_out` line per
  city per pass, and one `not reached` line naming the cities a truncated night skipped) and each city's
  `scrape.log`; the [log analyzer](log-analyzer.md) encodes the checks a reader would otherwise make by hand.
- **Stopping the queue by hand: signal the queue, not the wrapper** — the anchored `pkill` in
  [rollback lever 5](#rolling-back-smallest-blast-radius-first) — because the wrapper forwards a SIGTERM it
  receives to the queue exactly once, so signalling both delivers two.

**Verify any change to this the way `BASH_ENV` was:** a throwaway line in the same crontab (so it inherits
`SHELL` and `BASH_ENV`), one minute out, with the sink copied from the nightly line:

```cron
<M> <H> * * *  /srv/sidewalk-panorama-tools/.venv/bin/python /srv/sidewalk-panorama-tools/cron_notify.py --name probe --only-on-failure --log /home/ubuntu/cron_notify_probe.log --sink '<the same sink>' -- false
```

A message with `probe: exit 1` in the subject arrives and `tail -1 ~/cron_notify_probe.log` says
`published`; delete the line. **`--only-on-failure` is not optional in the probe.** Without it the wrapper
is under cron's rule — deliver when the command *printed* something — and `false` prints nothing, so the sink
is never called and no message can arrive *whether or not the channel works*: the probe fails every time and
points at the delivery, while the log says `nothing to publish`. The first recipe here omitted it, and on
2026-09-19 the probe was run that way twice and the channel suspected before anyone read the decision in
`main()`. `--log` is there so a probe whose publish fails still leaves `sink failed` (or `sink could not
start`) somewhere; stderr under cron goes to the same nowhere this section is about. It is a file of its own
because the log line carries no job name: in `~/cron_notify.log` a probe's `exit 1 published` would read as
a failed night, and would be the `tail -1` [the morning check](#the-morning-after-a-deploy) reads. Record the
date it was proven in the private runbook; a channel nobody has seen deliver is the one this section exists
because of.

### The morning after a deploy

- No message arrived — and `tail -1 ~/cron_notify.log` carries last night's date and says
  `nothing to publish (clean run, --only-on-failure)`. The message is failure-only, so silence is ambiguous
  on its own: it is a quiet night *or* a wrapper that never ran, and the log line is what tells them apart.
- `scrape_queue.log`: every city `ok (exit 0)`, and `pass 2 starting` if any ran out of budget.
- `scrape_queue.log` ends with `manifest checked against N public and M private cities (roster from …)`, and
  `grep ERROR scrape_queue.log` prints nothing — a `cities missing from the manifest` or
  `manifest not cross-checked` line is the night's failure
  ([the cross-check](downloader.md#the-manifest-is-cross-checked-against-the-fleet)), whether or not the mail
  arrived. So is a line naming a condition code (`depth-refused: …`, `mapillary-token-missing: …`) —
  [the codes table](downloader.md#a-city-can-finish-ok-and-still-fail-the-night) says what each means.
- `tail -1 <city>/log.csv` has 19 fields (2026-09-17 and later); blanks mean a phase never finished.
- `grep -h "backing off" */scrape.log | grep -E "\((HTTP [0-9]+|[0-9]+ retries were needed)\)"` prints nothing —
  a push-back from Google would be the first sign the pacer's persisted standing is too aggressive. The reason
  in parentheses matters: `(network failure)` and `(unexpected failure)` are the loop's own arms, one timeout
  anywhere in 52 cities writes one, and neither touches the persisted standing. `Google is refusing requests` in
  any `scrape.log` is the stand-down itself. That grep is the **depth** phase's pacer only; the image phase
  has its own line below.
- `grep -hE "Failed to download pano \S+ \((HTTP [0-9]+|interstitial)\)" */scrape.log` prints nothing — a
  line there is Google refusing a tile or a zoom probe
  ([When Google pushes back](#when-google-pushes-back-on-the-image-phase)). `Backing off _fetch_tile` lines no
  longer appear at all since #162 (`backoff` logs nothing now); historical ones in rotated logs are the
  evidence of how often tiles were refused before.
- `grep -ls "over the viewer ceiling" */scrape.log* */refetch.log*` prints nothing. A hit is [the width
  tripwire](#the-width-tripwire): a source now serves panoramas wider than 8192-class GPUs can render. It never
  fails a night and is never mailed on a clean one, so this grep is the only place it surfaces — and it runs
  only when someone runs it, here or under [routine checks](#routine-checks). A hit may be old — a line persists until rotation ages it out, so `grep -h` it to read the timestamps.
- The analyzer's fleet block, for the checks it encodes.

### Routine checks

The alarm carries only a nonzero exit, so some things that matter never reach anyone on a night that exits 0.
**Nothing runs these, and no schedule is set for them** — they are what to look at whenever someone looks at
the fleet, beyond the analyzer:

- `grep -ls "over the viewer ceiling" */scrape.log* */refetch.log*` prints nothing. A hit is [the width
  tripwire](#the-width-tripwire); it is never mailed on a clean night, so this grep is the only place it
  surfaces. A hit may be old — a line persists until rotation ages it out, so `grep -h` it to read the timestamps.
- `tail -1 ~/cron_notify.log` carries last night's date. A failed publish is visible
  [only there](#hearing-about-a-bad-night).
