# Cropper — `CropRunner.py`

Cuts one image per Project Sidewalk label out of the downloaded panoramas: **3:2**, centered on the label,
sized by an estimated camera-to-label distance, written to `<crop-dir>/<label_type_id>/<label_id>.jpg`.

`CropRunner.py` still works but is being replaced, so bugs may linger longer here than in the downloader.
Consumer requirements and the open geometry questions are tracked in
[#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54) and
[#32](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/32).

## Usage

```bash
python3 CropRunner.py (-d <fqdn> | -f <metadata-file>) -s <pano-dir> -o <crop-dir> [--mark-label] [--force]
```

| Flag | What it does |
|---|---|
| `-d <fqdn>` | Fetch label metadata from a Project Sidewalk server's `/adminapi/labels/cvMetadata`. Mutually exclusive with `-f`; one is required. |
| `-f <file>` | Read label metadata from a `.csv` or `.json` file (extension is matched case-insensitively). See `samples/`. |
| `-s <dir>` | **Required.** Directory holding the panos downloaded by `DownloadRunner.py`; they are what the labels are cut out of. |
| `-o <dir>` | **Required.** Where crops are written. `crop.log`, `crop_rule.json` and `crop_provenance.csv` go here too — see [What a crop store holds](#what-a-crop-store-holds). |
| `--mark-label` | Draw a dot at the label position **inside the crop**. Debugging aid, off by default — see the warning below. |
| `--force` | Re-cut a label whose crop already exists instead of skipping it — the repair for a store cut under an older rule. Off by default. See [Re-cutting a store](#re-cutting-a-store-with---force). |

Example:

```bash
python3 CropRunner.py -d sidewalk-columbus.cs.washington.edu \
  -s /sidewalk/columbus/panos/ -o /sidewalk/columbus/crops/
```

**`-o` must never be the production crop store, and a destination that looks like one is refused.**
SidewalkWebpage serves the Gallery, the label cards and the social preview from a different crop store,
`<root>/<city-id>/<LabelType>/crop_<labelId>.png`. Those are **canvas captures the browser took at label
time** — the annotator's own viewport and zoom, over the imagery Google served that day — so none of them can
be regenerated from anything this repo holds, and a deleted one is gone. This tool's store is
`<crop-dir>/<label_type_id>/<label_id>.jpg`, cut from the pano store and reproducible at will
([#83](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/83)).

Before it writes anything — before it creates `-o`, opens `crop.log` or writes `crop_rule.json` — the run
checks the destination and exits with status **3** — a message on stdout, and the same at `ERROR` on stderr,
since `crop.log` would be a write into the store being refused — if it finds either:

* an immediate subdirectory named for a label type (`CurbRamp`, `NoCurbRamp`, … any name in
  `LABEL_TYPE_IDS_BY_NAME`, ignoring case) — this tool names them by numeric id; or
* a `crop_*.png` file in `-o` or up to two directories below it — so `-o` at the production root, at one city
  or at one label type directory is caught.

A directory named for a label type refuses even when it is **empty**, deliberately: a city directory holds its
type directories before it holds a single capture. The cost is that an ordinary folder that happens to be
called `Other` or `Signal` refuses `-o`; the message names it. A directory the scan **cannot list** —
`lost+found` at the root of an ext4 volume, `System Volume Information` at a Windows drive's — is refused the
same way, naming it, since what cannot be read cannot be ruled out
([#153](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/153)).

It **refuses rather than warns**. The two layouts happen to be disjoint on every name component, so this tool
could not overwrite a capture today — but that is a coincidence of naming, not a guard, and it does nothing
for a re-cut campaign that deletes "the crop store" first, which is the mistake `--force` makes more likely.
The scan is cheap by construction: bounded depth, one directory listing per level, stopping at the first hit,
and it never lists the numeric type directories that hold a formula store's crops. A new, empty directory, or
an existing formula store, passes.

Both paths used to have defaults — `/crops/` and `/tmp/download_dest/`, the filesystem root and a Docker-only
scratch path — so forgetting one wrote an ML training corpus somewhere nobody would look for it. Since
[#52](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/52) a missing flag is an argparse
error naming it.

Both intakes dedupe on `label_id`, and the CSV intake reads with `csv.DictReader` rather than pandas
([#72](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/72)), so no field's type depends on
what the values happen to look like — the inference that gave an all-numeric Mapillary `pano_id` column
`int64` and crashed every shard slice. It checks the required columns up front, so a header typo is one error
naming the file, not a `KeyError` 200k labels in. Labels are grouped by pano so each pano JPEG is decoded
exactly once for all of its labels.

**The label's type arrives under one of two names, and both are accepted**
([#123](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/123)). cvMetadata sent
`label_type_id`, an integer, until
[SidewalkWebpage#4103](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4103) replaced the
label_type lookup table with a Postgres enum (released v11.11.0, 2026-09-02); every deployment now sends
`label_type`, a name like `CurbRamp`, in both JSON and CSV. `resolve_label_type_id()` prefers a usable
`label_type_id` — every archived export carries one, and a store is re-cut from whatever export produced
it — and otherwise maps the name through `LABEL_TYPE_IDS_BY_NAME`.

**The output directory is the numeric id either way.** `<crop-dir>/<label_type_id>/` is what every consumer
reads and what an existing store is sharded by, so the name is resolved at intake rather than carried
through. A name this map has never heard of means the enum moved upstream again: that row becomes one
counted error naming the value, rather than a guessed id filing a crop into a real training directory with
nothing on disk to say it was a guess.

**An id is checked against the same enum as a name, and the symmetry is deliberate.** An id arriving in an
old export is validated against `LABEL_TYPE_NAMES_BY_ID` before it is believed. Until the 2026-09-18 review
the id path was a bare `int()`, so `label_type_id=99`, `0` and `-3` were all accepted and written to
`<crop-dir>/99/` as a `success` with exit 0 — an arbitrary shard directory that an ML consumer globbing
`crops/*/` reads as a new label type. That is the same poisoning the name path refuses, so a guarantee that
held on only one half of the input space was worse than none: the docstring claimed both.

## Crop geometry

Crops are **3:2** (`CROP_ASPECT_W_OVER_H = 1.5`), and their width comes from `crop_window_width()` —
**sizing rule v2**. `predict_crop_size()`, the experimentally fit formula mapping pano-y to distance to crop
size, is evaluated in the 6656-px pano height its constants were fit on, scaled back into the pano's own
pixels, scaled up by `CROP_SIZE_SCALE = 2.5`, and clamped to `CROP_MIN_FOV_DEG = 8°`–`CROP_MAX_FOV_DEG = 90°`
**as an angle** rather than as pixels. `downscale_for_storage()` then caps what is written at
`CROP_MAX_STORED_WIDTH = 1440` px, never upscaling.

Under v1 the formula was fed native pixels and clamped in pixels, so the same ramp asked for a window
1.86–4.09× different depending only on the panorama's resolution — and the largest panoramas got the
tightest crops. Every v2 constant is one measured number:
[reports/2026-08-19-crop-sizing-v2.md](../reports/2026-08-19-crop-sizing-v2.md).

**Which rule cut a store is recorded in `<crop-dir>/crop_rule.json` — check it before training on a
directory.** `write_rule_marker()` writes `CROP_RULE_VERSION` plus every constant before anything is cut, and
*warns* rather than refusing when the marker disagrees with the running rule. A mixed store is the ordinary
result of changing the rule: existing crops are the resume marker and are not re-cut by default, so running
v2 over a v1 store leaves square v1 crops accreting 3:2 ones beside them. `--force` re-cuts every label the run
reaches under the running rule ([below](#re-cutting-a-store-with---force)); note that the marker is rewritten
at the *start* of the run, so a forced run that is interrupted leaves a store the marker describes as all-v2
while part of it is still v1 — finish the run before training on it. The warning says which case it is: without `--force` it
reports a store left holding both geometries and names `--force` as the remedy; under `--force` it says this
run is re-cutting every label it reaches and that the marker already names the new rule.

The window itself comes from `compute_crop_box()`, an integer `CropBox(left, top, width, height, shifted)`:

* **x wraps at the equirectangular seam.** The left and right image edges are the same place in the world, so
  a window overlapping the seam is assembled from both edges. In a six-city census of 438,410 labels, 1.52%
  of crops cross the seam; before this was fixed, every one of them carried a black bar
  ([#47](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/47)).
* **y clamps by shifting.** A window that would run past the top or bottom is moved inside the image instead
  of being padded. No crop ever contains synthetic black.

A shifted crop still contains its label, but not at the center. Those are counted separately in the run
summary (`shifted_vertically`) and logged with their offset, so a consumer that assumes centering can see how
many it got. In that same census only two labels needed a shift, and both turned out to be corrupt rows.

### Angles, pixels, and the factor of two

Every conversion between degrees and pixels in `CropRunner.py` goes through four one-line functions —
`azimuth_deg_to_px` / `azimuth_px_to_deg` (1.0 of width = 360°) and `elevation_deg_to_px` /
`elevation_px_to_deg` (1.0 of height = 180°) — and the constants `360` and `180` appear nowhere else in the
module. A test asserts that by walking the token stream, so a new call site cannot quietly reintroduce them.

This is not fussiness about naming. A fraction of width and a fraction of height are different units, and
production panoramas are 2:1, which makes degrees-per-pixel equal on the two axes and therefore makes a
wrong-axis conversion return the right answer for the wrong reason — until it meets a pano that is not 2:1,
or until the correction gets written twice and cancels. That second case shipped: a published figure in
`label-latlng-estimation` put a depth panel beside a photo crop, captioned as the same window, stretched
vertically by exactly 2. Nothing threw
([#78](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/78)).

`crop_window_fov_deg()` is the sizing rule expressed as what it is — an angle — and `crop_window_width()` is
that angle in this pano's pixels, one `azimuth_deg_to_px` call against the pano's width and nothing else. The
axis matters even though no production pano can show it: a width is horizontal, so its pixels are azimuth
pixels, while the angle itself is read off the regression's height-normalised size through the elevation
conversion. On a 2:1 pano the two agree to the bit, which is how the elevation form stood in for the width
until [#106](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/106)'s review; a square pano
would have made the window exactly 2× too wide, and a test on one now holds the axis.

### Where the label is inside the crop

`label_position_in_crop(pano_x, pano_y, box, pano_width, scale=1.0)` answers it, and is the only place that
does. It carries the three things a hand-rolled `pano_x - left` gets wrong: the **seam** (a window that wraps
puts a low x near the right of the crop, not at a large negative offset), the **vertical shift** (on a
clamped window the label is not at the center, so it comes off `box.top` rather than off the window's
midpoint), and the **storage rescale** (pass `scale = stored_width / box.width` to ask about the stored file
rather than the cut window). It returns floats — the caller decides how to round.

`--mark-label` draws its dot there, and the registration tests assert it by planting a uniquely coloured
pixel in a synthetic pano and reading it back out of the cut crop, at the horizon, at both poles and across
the seam. That matters because the alternative is a caption: the panel above was *labelled* "the same
window" while being twice the height, and nothing but the label said otherwise.

Details and measurements: [reports/2026-08-10-crop-geometry-review.md](../reports/2026-08-10-crop-geometry-review.md)
and [reports/2026-08-09-clamp-census.md](../reports/2026-08-09-clamp-census.md).

## The two preflights

Two checks reject a label rather than emit a quietly wrong crop.

**Pano dimensions (`dims_mismatch`).** If the label metadata's pano dimensions disagree with the image on
disk, the label is skipped with a warning. This is a **store-integrity** check: the metadata describes the
pano as it is served *now*, the image was stitched to whatever the API reported when it was downloaded, so a
disagreement means the store is stale (or, on the Mapillary path, that `thumb_original_url` served a
different size than was recorded). It does **not** detect a label whose `pano_x`/`pano_y` went stale under a
pano re-served at a new resolution: those dimensions are a per-pano value that gets refreshed along with the
pano, so such a row looks perfectly consistent. Separating those needs the click→pano replay
([#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54)). Measured over 438,410 labels /
172,790 panos, no pano carries two frames.

Dimensions are read from `pano_width`/`pano_height`, or from `width`/`height` for the older CSV export shape
(`samples/metadata-seattle.csv`). Those latter names are generic — if you supply your own CSV where
`width`/`height` mean something else, a canvas or a bounding box, **every row is skipped as a dimension
mismatch**. The skip is loud and counted, so this surfaces as a number rather than as bad crops.

**Label position (`out_of_frame`).** A `pano_y` outside the image is skipped, because there is no way to
recover it: the poles are not adjacent, so clamping produces clean imagery of a place the label is not in.
`pano_x` is deliberately **not** checked — column 0 and column `pano_width` are the same place in the world,
so the seam wrap reads any finite x correctly, and production rows storing `pano_x == pano_width` exactly do
exist and crop fine.

## Outcomes, exit code, and re-runs

Nothing in the crop loop is fatal. Every label lands in exactly one bucket and the counts reconcile on every
path, including re-runs:

```
success + skipped_existing + missing_pano + dims_mismatch + out_of_frame + errors == total
```

(`shifted_vertically` and `recut` annotate a success, and `stale_kept` annotates a label `--force` never
reached the write for — see below — so they are deliberately not in that sum.)

The run writes a rotating `crop.log` into the crop directory, prints a per-outcome summary, and **exits 1 if
any label errored** — a corrupt pano, a malformed metadata row, a failed write — so a cron wrapper can alert.
Errors are retried on the next run. The exit statuses, all of them:

| Status | Meaning |
|---|---|
| `0` | Every label landed in a non-error bucket (a crop, or a skip the run chose). |
| `1` | At least one label errored; re-running retries them. Also what an uncaught exception exits with — a manifest or a crop shard that cannot be opened stops the run before any crop, with a traceback. |
| `2` | argparse's usage error: a missing `-s`/`-o`, both or neither of `-d`/`-f`. |
| `3` | The destination was refused ([under Usage](#usage)): `-o` looks like the production canvas-capture store, or holds a directory the guard cannot list. Nothing was written and no label was looked at, so re-running changes nothing until `-o` does. |

`check_cvmetadata_schema.py` also exits `3`, for a different reason (the deployment could not be read). The
two tools are never chained, so the codes do not meet, but a wrapper that runs both should not read a `3` as
the same failure.

The skip outcomes are **not** errors and do not affect the exit code: `missing_pano` (the pano store is
scraped independently and legitimately lags the label list) and the two preflight rejections. Those are
metadata the run declined to trust, not work it got wrong.

### `crop.log` stays bounded under a flood

Every per-label warning goes to `crop.log`, which is the durable record of *which* labels failed and why.
Under one systemic fault that record used to destroy itself
([#139](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/139)): the malformed-row warning
carried the whole row, ~300–500 B repr'd, so the
[#123](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/123) run's ~260,000 of them came
to ~100 MB through the `10 MB × 3` rotation — every earlier run's history rotated out, and about 70% of the
flood's own lines with it. Two bounds now apply, and both are needed:

* **One line is bounded.** The malformed-row warning names `label_id` and `pano_id` first (`?` when the row
  does not carry a readable one), then the reason and the row, each clipped to `LOG_ROW_REPR_MAX_CHARS`
  (200) with `...`. An ordinary bad row keeps its detail; a pathological one cannot make one line huge.
* **One run is bounded.** Each *kind* of per-label warning is logged at most `LOG_WARNINGS_PER_KIND` (100)
  times per run. The first line dropped is replaced by one notice naming the kind, and the end of the run
  logs one total — `Suppressed N per-label warnings this run (malformed_row: …, crop_failed: …)` — ahead of
  the `SYSTEMIC FAILURE` line, which stays the last thing written. The kinds are `malformed_row`,
  `crop_failed` (a failed write: a full or read-only store), `cannot_open` (a pano that exists but will not
  open: a dead mount that still answers a stat), `dims_mismatch` (a city re-served wider than the store),
  `out_of_frame`, and `provenance_unrecorded` (a crop whose manifest row could not be written). Each has its
  own budget, so a flood of one cannot hide the first lines of another, which may be the actual cause.

**Suppression drops lines, never counts.** Every label is still counted in its bucket, so the summary, the
invariant above, the alarm below and the exit code all read exactly what they read before; stdout is
unchanged. `missing_pano` is deliberately **not** capped: it is the normal state of a city whose scrape is
catching up rather than a fault, it is one line per pano rather than per label, and which panos are missing
is what an operator topping up a store wants listed.

### When errors dominate: `SYSTEMIC FAILURE`

If at least **half** the run's labels errored, the summary ends with one extra line, to stdout **and** to
`crop.log`, that starts with the greppable `SYSTEMIC FAILURE`
([#136](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/136)):

```
SYSTEMIC FAILURE: 260000 of 260000 labels errored (100.0%). At that rate this is one cause rather than
that many independent per-row faults - check the label metadata's shape ...
```

This exists because when cvMetadata changed shape
([#123](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/123)) the bookkeeping was
entirely correct — `errors == total`, the invariant reconciled, the exit code was 1 — and the run still
read as ordinary noise: 260,000 identical per-row `WARNING`s, where "everything failed" differs from
"three labels failed" only in length. `CropRunner` is hand-run rather than on cron, so unlike
`DownloadRunner` there is no mail-on-failure carrying the exit code to anyone.

`SYSTEMIC_ERROR_FRACTION = 0.5` is the threshold, and half is chosen for what it means rather than as a
tuned number: it is the point where errors stop being a minority outcome. Firing only at 100% would be
tuned to the one incident we have seen and defeated by a single label of noise; firing at ~10% would sit
inside the range an ordinary bad night can reach, since a corrupt slice of the store is a genuinely
per-row fault and the loop is built to survive it.

**A corrupt pano contributes one error per label on it, not one error.** The loop decodes each pano once
for all its labels, so a failure there does `errors += len(labels)`. Corpus-wide that barely matters
(~2.5 labels per pano), but the `-f` route over a study subset is exactly the few-panos-many-labels shape:
a 40-label run where one truncated file carries 22 labels prints `22 of 40 ... (55.0%)` for a single bad
file. A one-label run that errors likewise reads 100%. Neither is wrong, and neither is harmful, but both
are worth knowing before treating `grep SYSTEMIC FAILURE` across logs as a count of real incidents.

The denominator is `total` — every label the run was handed. So the degenerate cases read correctly and
none of them fires: a run with no labels at all, a re-run over a finished store (100% `skipped_existing`),
and a city whose pano scrape is still catching up (100% `missing_pano`).

What keeps those quiet is **the threshold test itself**, not either guard: with `errors == 0`,
`errors < fraction * total` already holds for any `total > 0`. `total > 0` guards the *division* — its
only distinguishing input is `{'total': 0, 'errors': 1}`, which the crop loop cannot produce but a caller
of `systemic_failure_line` can — and `errors > 0` is redundant at the shipped fraction, kept as a
statement of intent. The function's docstring carries the measured truth table; two rounds of review
described these guards wrongly, in opposite directions, before it was written down that way.

Three blind spots come with that denominator, and only the first is benign:

- **A mature store topping up a handful of labels, every one of which fails to write**, is a small
  fraction of a large total and does not trip it. That run still exits 1 and still logs a warning per
  label (up to the per-kind cap above) — the signal it had before.
- **`missing_pano` dilutes the denominator.** A run over a city that is 60% un-scraped, whose output
  store then fills up mid-run or hits a per-file write failure, errors on every label it reaches — 40%
  of `total`. Silent. (A *read-only* `-o` is not this case: `write_rule_marker` writes `crop_rule.json`
  before the loop and outside any `try`, so a read-only mount raises before the first label and there is
  no summary at all. The arithmetic of the dilution is the point; that particular cause is not reachable.)
  The #123 shape itself is immune, because the up-front metadata parse `continue`s on a bad row so it
  never reaches the pano-existence check — an ordering a test now pins, since folding the two passes
  together would turn the immunity into dilution silently.
- **A run that is 100% `dims_mismatch` or 100% `out_of_frame` is silent *and exits 0*.** Those are skip
  buckets, so neither the alarm nor the exit code can see them. That is right for a lagging scrape and
  wrong for a schema move that changes what the dims or `pano_x`/`pano_y` fields mean, or for Google
  re-serving a city's panos wider than the stored frame. Zero crops, exit 0, no alarm, no cron mail — the
  silent-completion shape [#101](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/101)
  exists to prevent. Out of scope for #136, which is about `errors`, but it is the next gap, not a
  theoretical one.

It is a second *reading* of the counts, not a bucket: nothing about the invariant above changes, the exit
code is what it always was, and the per-outcome summary is still printed in full.

**Re-running does not regenerate existing crops — unless you pass `--force`.** By default a crop already on
disk is the resume marker and is skipped (`skipped_existing`). A store cropped before the seam fix keeps its
black-padded crops, one cropped before crop sizes became deterministic holds a mix of (for example) 503- and
504-px crops for the same predicted size, and — the case that matters now — every crop cut under sizing rule
v1 is the wrong size and shape for v2.

### Re-cutting a store with `--force`

`--force` ([#83](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/83)) re-cuts a label whose
crop already exists instead of skipping it. It is the repair for a store cut under an older rule, and it is
**for the ML crop store this tool writes, only** — the directory `-o` points at, never the production
canvas-capture store described [under Usage](#usage), which nothing can re-create and which the destination
guard refuses with or without `--force`.

* **Each crop is replaced atomically.** The new crop is written to `<label_id>.jpg.part` and renamed over the
  old one, so a run that dies mid-write leaves the old crop whole rather than a truncated file.
* **A re-cut is a `success`.** The summary adds one line, `N of those crops were re-cut over one already on
  disk (--force).`, and the counts dict carries `recut` — an annotation of `success`, like
  `shifted_vertically`, so the invariant above is unchanged. A label whose crop did not exist is a plain
  success and is not counted as re-cut.
* **Nothing else changes.** Missing panos, the two preflights and errors behave exactly as without it, so a
  label the run skips keeps whatever crop it already had — a forced run over a half-scraped pano store is
  not a whole-store re-cut. Check the summary's skip counts before relying on a store being one geometry.
* **Known limit: a label skipped before its write keeps its old crop.** A preflight skip, a missing pano
  or an unreadable pano all come before the "does a crop exist" check: the `dims_mismatch` and
  `out_of_frame` checks run first, and a pano that is missing or cannot be opened skips every label on it.
  A label whose crop is on disk is then skipped with that crop untouched — cut under whatever rule cut it,
  while `crop_rule.json` names the new one. A forced run counts these as `stale_kept` (an annotation of the
  skip or error, outside the sum above) and ends with `N labels skipped under --force - by a preflight, a
  missing pano or an unreadable pano - kept a crop already on disk`, on stdout and in `crop.log`.
  `crop.log`'s `dims_mismatch`, `out_of_frame` and `cannot_open` lines (up to `LOG_WARNINGS_PER_KIND` of
  each) and its missing-pano lines include them, without marking which kept an old crop. The crop is
  **not** deleted: whether a forced run should remove a crop it can no longer vouch for is an open decision, not
  something this tool does on its own.
* **It re-cuts the labels you hand it, not the directory.** A crop on disk whose label is absent from the
  metadata (deleted upstream, or outside a `-f` subset) is left alone.

## What a crop store holds

| Path | What |
|---|---|
| `<label_type_id>/<label_id>.jpg` | One crop per label. Its existence is the resume marker: it is not re-cut unless `--force` is passed. |
| `crop_rule.json` | Which sizing rule cut the store, plus whether the provenance manifest has a known gap (below). |
| `crop_provenance.csv` | One row per crop: where its pixels came from ([#111](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/111)). |
| `crop.log` | The rotating run log (10 MB × 3). |

### The provenance manifest, `crop_provenance.csv`

A crop is a bare JPEG and every consumer of this store is an ML dataset, so the record of where a crop's
pixels came from has to travel with the crop rather than stay with the app. The manifest is that record:

```
label_id,pano_id,source,copyright,license,crop_rule_version
```

* **One row per crop, appended as it lands** — after the JPEG is on disk, through one handle held for the
  run. That is the two nightly ledgers' contract (`pano_id_log.csv`, `depth_log.csv`), so a run killed at
  any point leaves a truthful partial file: a header, and a row for each crop that exists. Nothing else
  gets a row — not a skip, a preflight rejection, or a failed write. The file is append-only, so every
  re-cut appends a row; the last row for a label describes the file.
* **A row reaches the file whole or not at all**
  ([#153](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/153)). The handle is unbuffered
  and each row is one write, so a row whose append failed cannot sit in a buffer and be written later by
  the next row's flush. After a failed append the handle is dropped and reopened at once, and
  reopening — like every run's first open — cuts the file back to its last complete line. So half a row
  can never sit in the middle of the file, a run whose last append tore does not end torn, and a torn last
  row (a run killed mid-append) is cut away
  rather than closed off with a newline, which inside a quoted `copyright` would swallow every later row.
  If nothing is left, the header is written again.
* **`source`, `copyright` and `license` are copied from the label metadata verbatim, and written empty when
  the metadata does not state them** — never inferred from the source or anything else. `copyright` is
  the field the app keeps the producer credit under (`pano_data.copyright`, which it renders beside
  `pano_data.license`), so the column keeps that name rather than being renamed on the way through.
  **Today cvMetadata sends none of the three**
  ([API fields](api-fields.md#adminapilabelscvmetadata--the-croppers-label-list)), so a `-d` run writes
  all three empty, and older CSV exports fill `source` and sometimes `copyright`. They fill in by
  themselves if the endpoint starts sending them; none of them is a required column, on any of the three
  intakes.
* **`crop_rule_version` is per row**, because a store can hold more than one geometry (see
  [Crop geometry](#crop-geometry)).
* **`(pano_id, label_id)` is the key when manifests from more than one city are combined.** The manifest
  has no `city` column, and `label_id` restarts at 1 in every city's database; `pano_id` does not collide
  across cities, so the pair is unique where `label_id` alone is not. Within one store, `label_id` is
  what matches a row to its crop file, `<label_type_id>/<label_id>.jpg`.
* **A failed append does not lose the crop, and is not counted as an error.** The crop is already on disk
  and is the resume marker, so a plain re-run skips it and could never write the row: counting it in
  `errors` would break the promise that errors retry, and would put one label in two buckets. Each one is
  logged to `crop.log` (under the same per-kind cap as the other per-label warnings, as
  `provenance_unrecorded`), and the run summary prints how many crops went unrecorded, on both channels.
  The counts and the exit code are unchanged. Opening the manifest at all is different: if it cannot be
  opened the run stops before cutting anything, exactly as it does when `crop_rule.json` cannot be written.
  A handle that cannot be *closed* cleanly (a network mount reporting a deferred write error) is said on
  both channels and does not stop the run summary.

**Crops cut before the manifest existed have no rows**, since they are not re-cut without `--force` (a
`--force` pass that reaches them adds their rows). `crop_rule.json` records what the runs know about gaps:

| Key | Meaning |
|---|---|
| `provenance_manifest` | The manifest's file name. |
| `provenance_manifest_started_under` | The crop rule in force when the manifest was started. |
| `provenance_manifest_no_known_gap` | `true` if the store held no crops when the manifest was started and no run since has known of a crop left without a row; `false` once either is known; `null` if a manifest is present with no record of how it started. |

The first two are set by the run that starts the manifest and carried forward by every later run.
`provenance_manifest_no_known_gap` starts the same way and only ever goes from `true` to `false`: a run
turns it `false` when an append fails, when the manifest cannot be closed cleanly, or when it finds and
cuts a torn row a killed run left behind — and the `false` is kept for good, even through a `--force` pass
that re-cuts every crop, because nothing in the marker can tell that such a pass reached every row-less
crop (one whose label is absent from its metadata keeps no row). Deleting the manifest restarts it, and
the restarted one is recorded as `false` if the store already holds crops.

**The key is a record of what runs reported, not a coverage check.** A run killed between a crop's rename
and its row reports nothing, so even a `true` store can hold a crop with no row. Coverage is the
manifest's rows against the crops on disk, matched on `label_id` within one store — the only complete
answer, and the one to compute before relying on the manifest for a `false` or `null` store, or for a
`true` one that matters. A backfill is out of scope here, and would be a one-off in the shape of
`migrate_depth_artifacts.py`.

## Before you train on these crops

**Crops from `mapillary` and `panoramax` panoramas carry a licence, and it has to follow them into any
dataset.** Both sources publish imagery under open licences that require attribution — Mapillary uniformly
under CC BY-SA 4.0, Panoramax per picture, with the contributor choosing among `CC-BY-SA-4.0`, `CC-BY-4.0`
and `etalab-2.0`. `crop_provenance.csv` is where a crop's source, producer credit (`copyright`) and
licence are recorded; carry those rows with the crops. An empty `license` cell records that the metadata
stated none.

**Crops produced before `--mark-label` existed all carry a burned-in dark-red (128, 0, 0) dot at the label
position.** Marking used to be a `MARK_LABEL = True` constant at the top of the file, on for every run. That
dot sits directly over the feature of interest and is exactly what a model will learn instead of the feature.
Re-cut such a store with `--force` rather than reuse it
([#48](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/48)).

**You will likely want to filter out labels where `disagree_count > agree_count`.** These come from human
validations by other Project Sidewalk users; the cropper does **not** filter them by default. A stricter
option is to query `/v2/access/attributesWithLabels` for the city and keep only labels whose `label_id`
appears there too — a more aggressive filter that also removes labels from users we suspect of low-quality
data on some heuristics. The tradeoff is the usual one: more data vs. more accurate data.

**There is small but real error in the y-position of labels on the pano** (first observed Apr 2023). The
candidate root cause is diagnosed — the click→pano mapping corrects for camera heading but not for per-pano
camera tilt, [SidewalkWebpage#4784](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4784) — and
[#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54) tracks measuring the effect at
crop level here, with a correction to follow if the measurement confirms it. A separate render-side effect is
measured in
[reports/2026-08-10-off-target-markers-validate.md](../reports/2026-08-10-off-target-markers-validate.md).
(An earlier note here referred to an "alternative cropper" in development; that effort was abandoned and #54
supersedes it.)

**`label_id` is unique per city, not globally.** Project Sidewalk runs one database schema per city, so crops
from two cities can collide on filename. Key on `(city, label_id)` when you combine them.

## Related

* [API fields](api-fields.md) — what every column of `/adminapi/labels/cvMetadata` means, and the label type IDs.
* [Checking the contract against a live deployment](api-fields.md#checking-the-contract-against-a-live-deployment)
  — `check_cvmetadata_schema.py`, the tripwire for the next upstream field rename. The last one
  ([SidewalkWebpage#4103](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4103)) cost 16 days of
  zero crops against every deployment with CI green throughout.
* [Reports](../reports/README.md) — the crop-geometry, clamp, and click-noise studies behind the numbers above.
