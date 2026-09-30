# Cropper — `CropRunner.py`

Cuts one image per Project Sidewalk label out of the downloaded panoramas: **3:2**, centered on the label,
sized by an estimated camera-to-label distance, written to `<crop-dir>/<city>/<label_type_id>/<label_id>.jpg`
— one self-contained store per city under `-o`
([#159](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/159)).

`CropRunner.py` still works but is being replaced, so bugs may linger longer here than in the downloader.
Consumer requirements and the open geometry questions are tracked in
[#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54) and
[#32](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/32).

## Usage

```bash
python3 CropRunner.py (-d <fqdn> | -f <metadata-file>) -s <pano-dir> -o <crop-dir> --city <city_id> [--mark-label] [--force] \
    [--sizing-rule {v2,v3}] [--tilt-correction]
```

| Flag | What it does |
|---|---|
| `-d <fqdn>` | Fetch label metadata from a Project Sidewalk server's `/adminapi/labels/cvMetadata`. Mutually exclusive with `-f`; one is required. |
| `-f <file>` | Read label metadata from a `.csv` or `.json` file (extension is matched case-insensitively). See `samples/`. A `.json` file must hold an **array** of label rows; anything else stops the run before any crop with a `ValueError` traceback naming the file (exit 1, on stderr, not in `crop.log` — the same as a file that is not valid JSON). Inside the array, a row that is not an object or lacks a field the crop loop needs is one counted error, never the end of the run, and rows are deduplicated on the integer their crop is filed under (`1`, `"1"` and `1.0` are one label). A `.csv` file is deduplicated on the same integer (`7`, `07` and ` 7` are one label). |
| `-s <dir>` | **Required.** Directory holding the panos downloaded by `DownloadRunner.py`; they are what the labels are cut out of. |
| `-o <dir>` | **Required.** The root that holds one crop store per city. This run writes only into `<dir>/<city>/`: its crops, and its own `crop.log`, `crop_rule.json` and `crop_provenance.csv` — see [One store, one city](#one-store-one-city) and [What a crop store holds](#what-a-crop-store-holds). |
| `--city <city_id>` | **Required.** The city the labels belong to: an active (not `#`-commented) `city_id` row of `log_analyzer/cities.csv` (`seattle-wa`, `cdmx`), read when the flag is parsed. Anything else — a misspelling, a retired city, an unreadable roster — is exit 2, since the city names a directory and a typo would start a new store; add a missing city to the roster first ([Adding a city](ops.md#adding-a-city), step 3). Names the store, `<crop-dir>/<city>/`; recorded in its `crop_rule.json` and on every provenance row — see [One store, one city](#one-store-one-city). |
| `--mark-label` | Draw a dot at the label position **inside the crop**. Debugging aid, off by default — see the warning below. |
| `--force` | Re-cut a label whose crop already exists instead of skipping it — the repair for a store cut under an older rule. Off by default. See [Re-cutting a store](#re-cutting-a-store-with---force). |
| `--sizing-rule {v2,v3}` | Which crop sizing rule to cut with. **`v2` is the default**, and has been since [#88](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/88) (stores cut before it are v1, [#83](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/83)); `v3` is opt-in — see [Sizing rule v3](#sizing-rule-v3-opt-in). Recorded in `crop_rule.json` and on every provenance row either way. |
| `--tilt-correction` | Centre each crop on the label's rig pixel rather than its stored pixel, by beta per label era. **Off by default**; needs a pose beside each pano, and a pano without one is skipped as `no_pose`. Recorded in `crop_rule.json`. See [The tilt correction](#the-tilt-correction-opt-in-191). |

Example:

```bash
python3 CropRunner.py -d sidewalk-columbus.cs.washington.edu --city columbus-oh \
  -s /sidewalk/columbus/panos/ -o /sidewalk/crops/
```

writes `/sidewalk/crops/columbus-oh/<label_type_id>/<label_id>.jpg`, and the next city pointed at the same
`-o` gets `/sidewalk/crops/<its city_id>/` beside it.

### One store, one city

**Every city gets its own store, `<crop-dir>/<city>/`**
([#159](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/159)). `label_id` restarts at 1 in
every deployment, so inside one directory two cities collide on file names: without `--force` the second
city's label would find the first city's crop and count it `skipped_existing` — another city's imagery,
reported as success — and with `--force` it would **replace** the first city's crop. With the city in the
path, Seattle's label 1 and Chicago's label 1 are two files, and any number of cities can share one `-o`.
Each store is self-contained — its own `crop_rule.json`, `crop_provenance.csv` and `crop.log` — so two cities
cut under different rule versions stay representable.

The store also remembers its city. The first run records `--city` in the store's `crop_rule.json`; a store
cut before the city was recorded adopts the `--city` it is given. A store recorded as a **different** city's
— one renamed or copied into this city's directory by hand — is refused with exit **3** before anything is
created, cut or overwritten, `crop.log` included, and so is a store whose `crop_rule.json` cannot be read or
holds a city that is not a string — "unreadable" is not "no city recorded", and adopting there would hand
the store to whichever city came next. Both messages name the file.

**A `-o` in the old flat layout is refused, not cropped into.** Before #159, `-o` *was* the store:
`<crop-dir>/<label_type_id>/<label_id>.jpg`, with `crop_rule.json` and `crop_provenance.csv` beside the
shards. If `-o` directly holds an all-digit directory, a `crop_rule.json`, a `crop_provenance.csv` or a
`crop_provenance.pre-city.csv`, the run exits **3** before it writes anything (`--force` included), naming
what it found and the command to run next — cropping there would start a second copy of the city's store
beside the old shards and cut every crop again. A root that holds city stores, notes, figures or a stray
`crop.log` passes. A half-migrated root is refused too, until every collision the migrator listed is
settled by hand. An all-digit directory that is not a label type's (a `2024/` of figures, a hand-made
`01/`) is refused too, with a message of its own: it is not evidence of a flat store and the migrator
leaves it where it is, so the message says only to move it out of `-o` by hand and re-run (the re-run
names the migrator if `-o` is a flat store as well). There are three ways out:

* **The store is already named for its city** (`-o /srv/crops/columbus-oh --city columbus-oh`, the form the
  README always showed, or a link to such a directory): nothing moves. The message says so — point `-o` at
  the parent, `/srv/crops`, and the store is `<crop-dir>/<city>/` as it stands; it adopts the city on its
  next run and nothing is re-cut. The migrator is not offered here, and refuses such a root: it would nest
  the store as `columbus-oh/columbus-oh/`.
* **A flat store holding one city's crops, not named for it**:
  [move it with `migrate_crop_store.py`](#moving-a-pre-159-store) first.
* **A flat store that more than one city was cut into cannot be migrated by any tool here.** Nothing on
  disk says which crop is whose — the crop files are named for `label_id` alone, and a marker written
  before #153 records no city — so the migrator would file every crop under the one `--city` it is given,
  and that city's next run would count the other city's crops as its own `skipped_existing`. Set the store
  aside instead (rename it; never delete it) and re-cut each city into a fresh root, or separate it by
  hand: the old manifest's `pano_id` column, checked against each city's pano store, is the evidence.

### Moving a pre-#159 store

```bash
python3 migrate_crop_store.py <crop-dir> --city <city_id> --dry-run   # every move and collision, nothing written
python3 migrate_crop_store.py <crop-dir> --city <city_id>             # then for real
```

`<crop-dir>` is the directory that was `-o` before #159; it stays `-o` afterwards, now holding
`<crop-dir>/<city>/`. **It cannot tell one city's crop from another's**: every crop it moves is filed
under `--city`, and it says so on stdout and stderr whenever it moves (or, under `--dry-run`, would move)
anything. Give it only a store known to hold that one city's crops — see
[the ways out above](#one-store-one-city) for one that does not. The migrator **moves, never copies, and
never replaces**:

* Each label-type directory moves whole — one rename, which over sshfs is one round trip rather than one per
  crop — unless `<crop-dir>/<city>/<label_type_id>/` already exists (a run under the new layout, or a
  migration that died partway). Then it moves file by file, and **a file already at the destination is a
  collision: counted, listed as `COLLISION <src> -> <dst>`, and both are left exactly where they are.** A
  directory or symlink inside a type directory is left and listed; a type directory is removed only once
  empty. A type directory is one named for a label type's id (`1`–`10`); any other all-digit directory,
  and a type directory that is itself a symlink, is left and listed.
* Then the store's own files, each the same way: `crop.log` and its rotated `crop.log.<n>`,
  `crop_provenance.pre-city.csv`, `crop_provenance.csv`, and `crop_rule.json` **last**, so a run that dies
  partway leaves the root still marked as a flat store. Nothing is rewritten: the old manifest's rows keep
  no city, and CropRunner's next run [sets it aside](#the-provenance-manifest-crop_provenancecsv) and
  starts a fresh one.
* Everything else at the root — another city's store, notes, figures — is not touched.
* Before anything moves it refuses (exit **3**) a root already named for `--city` (or a link to one — point
  CropRunner's `-o` at its parent instead), a root that looks like the production canvas-capture store,
  and a root or `<city>/` whose `crop_rule.json` names another city or cannot be read: moving Chicago's
  store into `seattle-wa/` would make the collision this layout ends permanent. A marker with no city — any
  store cut before the city was recorded — passes, and CropRunner adopts the city on its next run. `--city`
  is checked against `log_analyzer/cities.csv` exactly as CropRunner checks it.

It prints `N type directories moved whole, F files moved one by one, K store files moved, C collisions left
in place, L left for a person, E failed` (each "would be" under `--dry-run`), then the next CropRunner
command and, when anything moved or would, a reminder for consumers. When it leaves anything, its last
line says CropRunner refuses the root until it holds no label-type directory, all-digit directory or store
file of its own. It exits **0** when the store is migrated or there was nothing to move,
**1** when anything was left where it was — a collision, a directory or symlink left in place, an all-digit
directory that is not a type directory, a listing, rename or `rmdir` that failed (each one `FAILED` line,
and the sweep goes on; a root that stops being listable ends the run with a message rather than a
traceback), predicted ones included under `--dry-run` — since CropRunner keeps refusing the root until each
is settled by hand, **2** on a usage error (a `crop-dir` that does not exist included), and **3** on a refusal.
It is idempotent and resumable: re-running it after a partial run, or after settling collisions, finishes
the job. **Run one migrator per store at a time**, and not while a CropRunner is cutting into it.

### Never the production crop store

**`-o` must never be the production crop store, and a destination that looks like one is refused.**
SidewalkWebpage serves the Gallery, the label cards and the social preview from a different crop store,
`<root>/<city-id>/<LabelType>/crop_<labelId>.png`. Those are **canvas captures the browser took at label
time** — the annotator's own viewport and zoom, over the imagery Google served that day — so none of them can
be regenerated from anything this repo holds, and a deleted one is gone. This tool's store is
`<crop-dir>/<city>/<label_type_id>/<label_id>.jpg`, cut from the pano store and reproducible at will
([#83](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/83)).

Before it writes anything — before it creates the city store, opens `crop.log` or writes `crop_rule.json` —
the run checks **both `-o` and the city store `<-o>/<city>/`**, and exits with status **3** — a message on
stdout, and the same at `ERROR` on stderr, since `crop.log` would be a write into the store being refused — if
either holds:

* an immediate subdirectory named for a label type (`CurbRamp`, `NoCurbRamp`, … any name in
  `LABEL_TYPE_IDS_BY_NAME`, ignoring case) — this tool names them by numeric id; or
* a `crop_*.png` file in it or up to two directories below it — so `-o` at the production root, at one city
  or at one label type directory is caught.

**Why both** ([#159](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/159)): the two layouts
are now both city-first, so depth no longer separates them — the names do. Ours is all-digit type directories
holding `<label_id>.jpg`; theirs is `LabelType`-named directories holding `crop_<labelId>.png`. Scanned from
`-o`, a formula root's city directories sit one level down and their numeric shards are skipped, so it passes;
a production root is caught by its captures two levels down. The one shape that scan misses is a production
city directory whose type directories are still **empty**, because the name signal is read only at the top of
the scan — so the city store is scanned too, where those directories are the top.

A directory named for a label type refuses even when it is **empty**, deliberately: a city directory holds its
type directories before it holds a single capture. The cost is that an ordinary folder that happens to be
called `Other` or `Signal` refuses `-o`; the message names it. A directory the scan **cannot list** —
`lost+found` at the root of an ext4 volume, `System Volume Information` at a Windows drive's (a permission
error, or Windows' `WinError 1920`), whether the listing fails to open or fails partway through — is refused
the same way, naming it, since what cannot be read cannot be ruled out
([#153](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/153)). Any other I/O error is not
read as a refusal and stops the run as itself.

It **refuses rather than warns**. The two layouts happen to be disjoint on every name component, so this tool
could not overwrite a capture today — but that is a coincidence of naming, not a guard, and it does nothing
for a re-cut campaign that deletes "the crop store" first, which is the mistake `--force` makes more likely.
The scan is cheap by construction: bounded depth, one directory listing per level, stopping at the first hit,
and it never lists the numeric type directories that hold a formula store's crops. A new, empty directory, or
an existing formula store, passes.

### Paths and intake

Both paths used to have defaults — `/crops/` and `/tmp/download_dest/`, the filesystem root and a Docker-only
scratch path — so forgetting one wrote an ML training corpus somewhere nobody would look for it. Since
[#52](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/52) a missing flag is an argparse
error naming it.

Both intakes dedupe on `label_id` as the integer the crop is filed under (`_label_id_key`), keeping the first
row per id, so `7` and `07` are one label rather than two rows cut to the same `7.jpg` (twice under `--force`,
with two provenance rows, until
[#170](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/170)'s follow-up). A row whose
`label_id` is blank or not an integer is never deduplicated: each one is its own counted error. The row keeps
its raw string; only the key is an `int`. A dropped duplicate is not in the run's `total`, so the intake says
how many it dropped, in one line on stdout and in `crop.log` with a few example ids — never a line per row. The CSV intake reads with `csv.DictReader` rather than pandas
([#72](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/72)), so no field's type depends on
what the values happen to look like — the inference that gave an all-numeric Mapillary `pano_id` column
`int64` and crashed every shard slice. It checks the required columns up front, so a header typo is one error
naming the file, not a `KeyError` 200k labels in. Labels are grouped by pano so each pano JPEG is decoded
exactly once for all of its labels. One optional column is read, and only under `--tilt-correction`:
`time_created` (epoch milliseconds, as rawLabels exports it, or ISO 8601), which sets the label's era for
[the tilt correction](#the-tilt-correction-opt-in-191). cvMetadata does not serve it, so on `-d` every label's
era is `unknown`; it is never required, and `REQUIRED_LABEL_COLUMNS` does not list it.

**The label's type arrives under one of two names, and both are accepted**
([#123](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/123)). cvMetadata sent
`label_type_id`, an integer, until
[SidewalkWebpage#4103](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4103) replaced the
label_type lookup table with a Postgres enum (released v11.11.0, 2026-09-02); every deployment now sends
`label_type`, a name like `CurbRamp`, in both JSON and CSV. `resolve_label_type_id()` prefers a usable
`label_type_id` — every archived export carries one, and a store is re-cut from whatever export produced
it — and otherwise maps the name through `LABEL_TYPE_IDS_BY_NAME`.

**The output directory is the numeric id either way.** `<crop-dir>/<city>/<label_type_id>/` is what every consumer
reads and what an existing store is sharded by, so the name is resolved at intake rather than carried
through. A name this map has never heard of means the enum moved upstream again: that row becomes one
counted error naming the value, rather than a guessed id filing a crop into a real training directory with
nothing on disk to say it was a guess.

**An id is checked against the same enum as a name, and the symmetry is deliberate.** An id arriving in an
old export is validated against `LABEL_TYPE_NAMES_BY_ID` before it is believed. Until the 2026-09-18 review
the id path was a bare `int()`, so `label_type_id=99`, `0` and `-3` were all accepted and written to
`<crop-dir>/<city>/99/` as a `success` with exit 0 — an arbitrary shard directory that an ML consumer globbing
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

**Which rule cut a store is recorded in `<crop-dir>/<city>/crop_rule.json` — check it before training on a
directory.** `write_rule_marker()` writes the rule the run selected (`crop_rule_version`), its
`distance_estimator`, every rule's constants, and whether [the tilt correction](#the-tilt-correction-opt-in-191)
is on (`tilt_correction`, plus each era's beta among the constants) before anything is cut, and *warns* — on stdout and in
`crop.log` — rather than refusing when the marker disagrees with the rule this run selected, or, under the
same rule id, when a constant that rule reads has changed (a refit v3 would still call itself v3). A mixed store is the ordinary
result of changing the rule: existing crops are the resume marker and are not re-cut by default, so running
v2 over a v1 store leaves square v1 crops accreting 3:2 ones beside them. `--force` re-cuts every label the run
reaches under the running rule ([below](#re-cutting-a-store-with---force)); note that the marker is rewritten
at the *start* of the run, so a forced run that is interrupted leaves a store the marker names as the new rule
while part of it is still the old one — finish the run before training on it. The warning says which case it is: without `--force` it
says the old crops keep their geometry beside this run's and names `--force` as the remedy; under `--force` it says this
run is re-cutting every label it reaches and that the marker already names the new rule.

The marker's history is **sticky**: `rules_seen` lists every rule the store has been run under, in order of
first use, and `constants_seen` every value each rule's constants have had. Both are only ever appended to,
and the warnings fire on *every* run whose rule or constants are not the only ones the store has seen — not
just the first. That matters for v2 and v3 in particular, because both cut 3:2 crops, so a mixed store is
indistinguishable on disk and the marker is the only evidence. The warning is written before any crop is cut,
so it says what the store holds and what any crop this run cuts will be, not that this run added anything. A
marker that exists but cannot be read (bad JSON, a malformed history, or a rule field that is not a string,
a number or null) is warned about, recorded as `unknown` (for good, in `rules_seen`), and
kept beside the new one as `crop_rule.json.unreadable-<UTC timestamp>`; its city and manifest keys are
carried forward. (A marker that is not a JSON object at all is refused before this, by
[the city check](#one-store-one-city), since it cannot say whose store it is. Only the rule's own fields
are type-checked: `provenance_manifest_no_known_gap` is a JSON bool by design.) Getting one geometry throughout means
re-cutting the store under one rule's constants ([`--force`](#re-cutting-a-store-with---force)), and a
forced pass does **not** clear the history: like `provenance_manifest_no_known_gap`, nothing in the marker
can tell that it reached every crop. **The reset:** once the whole store has been re-cut under one rule,
remove `rules_seen`, `constants_seen` and `previous_crop_rule_version` from `crop_rule.json` — never a
crop — and the next run records only its own rule. The plain-run warning names this reset as well as
`--force`, since after a whole forced re-cut it still fires and another forced pass would not quiet it. Remove those three keys rather than deleting the file:
the file also holds the store's `city` and the manifest's gap record, and a deleted marker turns that
record into `null` (unknown) for good. Do it only after a *whole* re-cut: over a store that still holds
crops from another rule, it makes a mixed store read as a clean one.

### Sizing rule v3 (opt-in)

`--sizing-rule v3` ([#32](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/32)) replaces the
2013 linear distance with the lle #3 calibrated one, and the power law with the geometry it approximates.
Three named steps, composed in `crop_window_fov_deg()`:

1. `label_depression_deg(pano_y, pano_height)` — the label's angle below the horizon, through the elevation
   primitive. A #54 tilt correction, once measured, is an addend here and nowhere else.
2. `blend_distance_m(depression)` — `h / tan(depression)` for depressions of 11.25° and steeper; between the
   horizon and 11.25°, the straight line matching the cotangent's value and slope at 11.25°; `h = 2.3412 m`.
   It saturates at 23.85 m at the horizon, so a label above the horizon gets the horizon's window. It is
   clipped to [0, 50 m], but the 50 m cap is inert under the blend (it tops out at 23.85 m); it matters only
   to the depth-distance spec it was copied from.
3. `geometric_window_fov_deg(distance)` — `2·atan(W / 2d)` with `W = V3_CONTEXT_WIDTH_M = 5.8 m`, clamped to
   v2's 8°–90°. It takes metres, so a measured distance (the depth artifact) would plug in unchanged.

Everything downstream — 3:2, the seam, the shift, the 1440 px storage cap — is v2's. Two consequences of the
constants: **the 8° floor is unreachable** (the horizon window is 13.87°), and **the 90° cap binds from 38.91°**
of depression rather than v2's 26.55°. `W` is the one fitted number, chosen to give v2's median fill on the
same 658 gold aprons; the three distance constants are a transcribed copy of
`reports/scripts/pov_replay.py`'s, pinned equal by `TestBlendDistanceMatchesTheStudyPort`.

Measured in [reports/2026-09-26-crop-sizing-v3.md](../reports/2026-09-26-crop-sizing-v3.md): at the same
median crop, v3's fill is less dispersed and its window tracks the apron better in every city — but it
moves about 40% of the 658 gold ramps' windows by more than 10%. (The report also sets out the minimal
alternative, the blend distance fed into v2's power law, which trades the other way on several columns;
the choice is decision D7 on [#157](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/157) and
the report's §4, not a settled result.) **The default stays v2**: existing crops are re-cut
only under `--force`, so flipping it on a store cut under v2 would mix the geometries unless every store is
re-cut whole ([#83](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/83)). Until that is
decided, run v3 into a fresh `-o`, or over a v2 store only with `--force`; `crop_rule.json` warns if you
mix them, and on every run after. If the default does flip, fold it into the recrop campaign
[#84](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/84) coordinates rather than
re-cutting the stores a second time on its own. [The content check](#the-content-check-black_content)
judges v3's window exactly as it does v2's.

The window itself comes from `compute_crop_box()`, an integer `CropBox(left, top, width, height, shifted)`:

* **x wraps at the equirectangular seam.** The left and right image edges are the same place in the world, so
  a window overlapping the seam is assembled from both edges. In a six-city census of 438,410 labels, 1.52%
  of crops cross the seam; before this was fixed, every one of them carried a black bar
  ([#47](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/47)).
* **y clamps by shifting.** A window that would run past the top or bottom is moved inside the image instead
  of being padded. No crop ever contains synthetic black — black the cropper made. That is geometry; black
  the *stored pano* already holds is a question about content, and
  [the content check](#the-content-check-black_content) answers it.

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

### The tilt correction (opt-in, #191)

A GSV label's stored `pano_x`/`pano_y` is in gravity-levelled pixels, while the stored tiles are in the
camera rig's own frame, in every scrape era. The #54 study found the stored point is off towards the rig
pixel (endpoint C, 79 : 0) by about the whole rig transform
([reports/2026-09-26-tilt-error-study.md](../reports/2026-09-26-tilt-error-study.md),
[#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54), [#191](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/191)). `--tilt-correction` moves each label's
crop centre from the stored pixel to the rig pixel, on both axes. It is **off by default**, and a run without
it cuts exactly what it did before.

* **The move.** `pano_pose.corrected_pixel` applies `rig_pixel_from_gravity_pixel` with the pose scaled by
  beta: `(beta * pitch, beta * roll)`. Beta 0 is the identity and beta 1 the full transform; to first order
  the label moves up or down by `beta * T(b) * h / 180` px, `T(b) = pitch cos b + roll sin b`, and sideways
  by up to a few percent of the window. The geometry lives in `pano_pose.py`, not in `CropRunner.py`, which
  keeps the [#78 rule](#angles-pixels-and-the-factor-of-two) that 360 and 180 appear only in its four unit
  functions.
* **The pose is the pano's own, never guessed.** It is read once per pano: from `<id>.xml` when one sits
  beside the JPEG (a 2019-22 scrape, whose pixels that file describes, even if a newer `.depth.npz` exists),
  otherwise from `<id>.depth.npz`'s `pitch`/`roll` ([docs/depth.md](depth.md#the-artifact)). An `.xml` missing
  a field is no pose; it does not fall through to the npz. A pano with no usable pose is **`no_pose`**:
  every label on it is skipped, not an error, so the exit code does not change. `crop.log` gets one line per
  pano naming why (no file; an incomplete xml; an npz without a finite pitch or roll), and the run ends with
  one line on stdout and in `crop.log` when any were skipped. A re-run cuts them once the depth phase has
  written the pano's artifact.
* **Beta is per era of the label** (`TILT_BETA_BY_ERA`): `post179` (a `time_created` on or after
  2023-03-29 UTC, SidewalkWebpage v7.12.2), `legacy+mid` (before it) and `unknown`. All three are **1.0**,
  the pre-set rule's default, until the per-era estimate on #191 sets them. `unknown` is its own entry
  because cvMetadata serves no `time_created`, so every `-d` label is `unknown`. The run prints one line with
  each era's count and beta, and says so when every label was `unknown`.
* **The window is sized at the stored `pano_y`.** The corrected point only positions it. Rule v2 was fit on
  stored coordinates, and v3's depression is the object's depression below the gravity horizon, which is what
  the stored y records. Re-fitting either rule on corrected coordinates is a follow-up once beta is set.
* **The preflight reads both points.** A label is `out_of_frame` if either its stored or its corrected y is
  outside the image. The exact rotation keeps a corrected y inside `[0, h]`, so the second test only matters
  at the nadir row.
* **A corrected crop is a different crop.** `crop_rule.json` records `tilt_correction` (`on`/`off`) and each
  era's beta as `tilt_beta_post179`, `tilt_beta_legacy_mid` and `tilt_beta_unknown_era`, among the constants
  of both rules. They are `0.0` when the correction is off, since beta 0 is the identity. So a store cut
  without the flag and then with it gets the same-rule mixed-store warning ("was cut ... with
  tilt_beta_post179=0.0 and this run uses 1.0"), and `constants_seen` keeps that history, as for any other
  constant. A marker written before these keys existed stays quiet. Re-cut the whole store with `--force`
  rather than topping it up.
* **Not per crop.** The provenance manifest has no correction column yet, so a mixed store shows only in
  `crop_rule.json`. The column is a header migration and a follow-up of its own. Non-GSV panos
  ([#190](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/190)) and the source-side fix
  ([SidewalkWebpage#4784](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4784)) are out of scope here.

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

## The content check (`black_content`)

The two preflights and the geometry above are all about *where* a window is. None of them looks at what is
in it, and a stored pano can hold black where imagery should be: a stitch that ran past what Google serves
([#156](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/156)'s D4 shape — a frame reported
larger than the pano, ~34% black along its right and bottom 19%), or an old pre-#68 fallback. The stitcher's
own guard rejects only a pano more than *half* black, so those are on disk. Before
[#164](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/164), a label inside such a band —
and the bottom band is where ground features sit — was cut as a black crop, counted `success`, and landed in
a training directory with a clean summary.

**What is measured.** The window exactly as it is cut from the pano, before the storage downscale and before
`--mark-label`'s dot: the fraction of its pixels whose luma is exactly 0
(`downloaders.common.black_fraction`, the stitcher's own primitive). If that is **more than**
`CROP_MAX_BLACK_FRACTION` (0.5), nothing is written — no crop, no `.part`, no provenance row — and the label
is counted `black_content`.

**Why 0.5.** Measured on a 2048×1024 grey pano whose bottom 30% is black, JPEG q95, with the real window
geometry of each sizing rule (the v2 column is the one the threshold was chosen on; v3, `--sizing-rule v3`,
was measured on the same fixture when the two met, and cuts a narrower window at every one of these rows but
the shifted one):

| Label y | Black share of the window, v2 | v3 |
|---|---|---|
| 512 (horizon) | 0.000 | 0.000 |
| 650 | 0.241 | 0.199 |
| 700 | 0.452 | 0.441 |
| 716 | 0.499 | 0.498 |
| 730 | 0.540 | 0.540 |
| 900 (window shifted up) | 0.900 | 0.900 |

Inside a large black JPEG region luma is exactly 0, and the codec's ringing is confined to the rows next to
the edge, so a label inside a band whose window is *not* shifted gets at least half black rows (the rows
from the label down to the window's bottom edge) — a property of a centred window, so it holds under either
rule. A window more black than imagery is not imagery. The other
direction holds too: exact zero over half a window is not a night scene or a black car, because JPEG noise
keeps those off 0 (a band of `(1, 1, 1)` is written). Two consequences are accepted knowingly: a label within
the ringing margin of a band's edge (y=716 above) is written, and so is a partly black crop under half
(y=650, 24% black under v2, 20% under v3).

**A bottom band is caught only when it is deeper than a sixth of the pano — under either rule.** Near the
nadir the window is clamped at `CROP_MAX_FOV_DEG` (90° wide, so at 3:2 it spans 60° of elevation, a third of the pano's height)
and shifted up to end at the bottom row, so every label in the lower sixth gets the *same* window, and its
black share is the band's depth over that window's height — wherever in the band the label sits. The check therefore withholds labels in a
bottom band only when the band is deeper than half the nadir window: **H/6, 16.7% of the pano's height**
(a few rows more on a JPEG, where the edge's ringing rows are not exactly 0). Measured on a 2048×1024 pano,
JPEG q75, labels just inside the band, mid-band and on the bottom row, all three giving the same share:

| Bottom band depth | Black share of each window | Verdict |
|---|---|---|
| 10% | 0.286 | written, `success` |
| 15% | 0.441 | written, `success` |
| 17% | 0.485 | written, `success` (ringing) |
| 18% | 0.535 | withheld |
| 18.75% (#156's D4 shape) | 0.562 | withheld |

The table is v2's, and v3's is the same: v3's window reaches `CROP_MAX_FOV_DEG` from 38.9° of depression,
and the window starts shifting at about 60° under both rules, so the nadir window is the *same box* under
either (measured identical, and the in-memory test is parametrised over both rules and asserts it).

So the D4 bottom band clears the limit by about six points, while a thinner band — a pre-#68 fallback or any
other reported/served ratio under ~1/6 — is written as `success` with up to half of each crop black. Two tests
pin this against the geometry, in memory at the exact row and on a stored JPEG either side of a sixth, each
under both rules. A
right-hand band has the analogous edge case at the seam: the window wraps to imagery from the left edge, so a
label on the last column of a band gets about half black (0.510 measured at 19%, withheld by a hair). A
consumer who wants those crops out too filters below 0.5 (see
[Before you train](#before-you-train-on-these-crops)); judging the rows at and below the label, rather than
the whole window, is a possible stronger check, not made here.

**Not an error.** `black_content` is like `dims_mismatch`: the run refused to trust the imagery rather than
getting anything wrong, so it does not move the exit code or the `SYSTEMIC FAILURE` alarm. It *is* said: one
`crop.log` line per label under its own capped kind, and — when the count is nonzero — one summary line on
stdout **and** in `crop.log`, because a run that withheld every label would otherwise exit 0 in silence.
Since nothing was written, a re-run cuts the label once the pano is repaired (re-downloaded, or replaced by
`refetch_panos.py`).

**It never judges a crop already on disk.** The check runs only on a crop about to be written, after the
"does a crop exist" check, so a store cut before this check keeps whatever it holds (see
[Before you train](#before-you-train-on-these-crops)). Under `--force`, a window that would now be withheld
is not written, so the crop already there is kept byte for byte, and counted `stale_kept` as well as
`black_content`.

**Known limit: the D4 shape outside its bands.** A label *outside* the black bands of a D4 stitch gets clean
imagery — at the wrong scale and position, because the real pano was stretched to the wrong frame. It is 0%
black, its dimensions agree with the metadata, and no crop-level check can see it; a test pins that as a
known limit. A pano-level check belongs on the downloader side (`refetch_panos.py`'s `too_black` gate already
knows this shape for its own swaps), not here: judging the whole pano would mean an eager decode plus a
full-frame luma copy (~134 MB at 16384×8192) per pano, and a threshold calibrated against real zenith and
nadir caps.

## Outcomes, exit code, and re-runs

Nothing in the crop loop is fatal. Every label lands in exactly one bucket and the counts reconcile on every
path, including re-runs:

```
success + skipped_existing + missing_pano + dims_mismatch + out_of_frame + black_content + no_pose + errors == total
```

(`shifted_vertically` and `recut` annotate a success, and `stale_kept` annotates a label `--force` did not
write — see below — so they are deliberately not in that sum.)

The run writes a rotating `crop.log` into the city's store, prints a per-outcome summary, and **exits 1 if
any label errored** — a corrupt pano, a malformed metadata row, a failed write — so a cron wrapper can alert.
Errors are retried on the next run. The exit statuses, all of them:

| Status | Meaning |
|---|---|
| `0` | Every label landed in a non-error bucket (a crop, or a skip the run chose). |
| `1` | At least one label errored; re-running retries them. Also: a `-d` fetch of cvMetadata that fails (`Cannot fetch metadata from webserver`, the reason in `crop.log`), or answers with something other than a JSON array of labels — an error object, say (`The webserver's metadata is not a list of labels`, the URL and what came back in `crop.log`); an `-f` file whose extension is neither `.csv` nor `.json` (the message on stderr), or a `.json` one that does not hold an array (a traceback naming the file); a label-type shard of the city store (`<crop-dir>/<city>/<digits>/`) that cannot be listed, which stops the run before any crop with the shard named on stdout and in `crop.log` (not `3`: nothing judged `-o` to be the production store — the provenance record needs to know whether the store already holds crops, and could not find out); and what an uncaught exception exits with — a manifest that cannot be opened stops the run before any crop, with a traceback. |
| `2` | argparse's usage error: a missing `-s`/`-o`/`--city`, a `--city` that is not a city_id or not an active row of `log_analyzer/cities.csv` (or that file cannot be read, named in the message), both or neither of `-d`/`-f`. |
| `3` | The destination was refused ([under Usage](#usage)): `-o` looks like the production canvas-capture store, or holds a directory the guard cannot list, or is [another city's store](#one-store-one-city), or has a `crop_rule.json` that cannot say whose it is, or is a [pre-#159 flat store](#one-store-one-city) rather than a root of city stores (either `-o` or `<-o>/<city>/` is scanned for the production layout). Also: a `crop_provenance.csv` whose header the run cannot append under ([the manifest](#the-provenance-manifest-crop_provenancecsv)), checked after `crop.log` is opened (so the message is in it too) but before `crop_rule.json` is rewritten or any crop is cut. Otherwise nothing was written and no label was looked at, so re-running changes nothing until `-o` does. |

`check_cvmetadata_schema.py` also exits `3`, for a different reason (the deployment could not be read). The
two tools are never chained, so the codes do not meet, but a wrapper that runs both should not read a `3` as
the same failure.

**A pano that opens but cannot be decoded is decoded once.** `Image.open` reads only the header, so a
truncated file gets past the "cannot open" check. Decoding stays lazy — the preflights and
`skipped_existing` read only the header, so a finished store never decodes a pano — and the first label
that reaches the write decodes it. If that fails, the pano is not decoded again: that label and every later
one on the pano that reaches the write is one counted error (and `stale_kept` under `--force` when its crop
is on disk), and `crop.log` gets one `cannot decode` line for the pano under the `cannot_open` kind. Before
[#164](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/164) each label re-decoded the
whole file (Pillow keeps the pending decode after a failure, 0.25 s per label on a 13312-wide pano) and
logged its own `Failed to crop label` line, spending the `crop_failed` budget that a real write failure
needs. A label's bucket does not depend on where it sits in the list.

The skip outcomes are **not** errors and do not affect the exit code: `missing_pano` (the pano store is
scraped independently and legitimately lags the label list), the two preflight rejections, and
`black_content` ([the content check](#the-content-check-black_content)), and `no_pose` (under
[`--tilt-correction`](#the-tilt-correction-opt-in-191), a pano with no pose beside it; always 0 without the
flag). Those are metadata or imagery the run declined to trust, not work it got wrong.

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
  open: a dead mount that still answers a stat; or whose body cannot be decoded — a truncated file behind a
  good header — one line per pano), `dims_mismatch` (a city re-served wider than the store),
  `out_of_frame`, `black_content` (a window withheld by the content check), and `provenance_unrecorded` (a crop whose manifest row could not be written). Each has its
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
for all its labels, so a failure there is an error for every label that needed it. Corpus-wide that barely matters
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
  theoretical one. A run that is 100% `black_content` also exits 0 with no alarm, but it is not silent:
  that bucket gets its own summary line on both channels
  ([#164](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/164)).

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
* **Known limit: a label the run did not write keeps its old crop.** A preflight skip, a missing pano
  or a pano that cannot be opened all come before the "does a crop exist" check: the `dims_mismatch` and
  `out_of_frame` checks run first, and a pano that is missing or cannot be opened skips every label on it.
  A pano that opens but cannot be decoded fails later, at the first label that reaches its write, and every
  such label is an error. Either way, a label whose crop is on disk is left with that crop untouched — cut under whatever rule cut it,
  while `crop_rule.json` names the new one. A forced run counts these as `stale_kept` (an annotation of the
  skip or error, outside the sum above) and ends with `N labels skipped under --force - by a preflight, a
  missing pano or an unreadable pano - kept a crop already on disk`, on stdout and in `crop.log`.
  `crop.log`'s `dims_mismatch`, `out_of_frame` and `cannot_open` lines (up to `LOG_WARNINGS_PER_KIND` of
  each) and its missing-pano lines include them, without marking which kept an old crop. The crop is
  **not** deleted: whether a forced run should remove a crop it can no longer vouch for is an open decision, not
  something this tool does on its own. A label [the content check](#the-content-check-black_content) withholds
  reaches its write and then declines it, with the same result: its old crop is kept, it counts as
  `stale_kept` too, and the sentence above gains `N of them were withheld by the content check
  (black_content) rather than skipped by a preflight`. A label on a `no_pose` pano under
  [`--tilt-correction`](#the-tilt-correction-opt-in-191) is the same: `stale_kept`, and the sentence gains
  `N of them were on a pano with no pose for the tilt correction (no_pose)`.
* **It re-cuts the labels you hand it, not the directory.** A crop on disk whose label is absent from the
  metadata (deleted upstream, or outside a `-f` subset) is left alone.
* **It does not clear the rule history.** `crop_rule.json`'s `rules_seen` keeps every rule the store was
  ever run under, so the mixed-store warning repeats after a whole re-cut. Once the whole store has been
  re-cut under one rule, remove the three history keys ([the reset](#crop-geometry)), or the marker keeps
  warning about the rule the replaced crops were cut under.

## What a crop store holds

`-o` holds one directory per city, and nothing else is written at that level. Inside `<crop-dir>/<city>/`:

| Path | What |
|---|---|
| `<label_type_id>/<label_id>.jpg` | One crop per label. Its existence is the resume marker: it is not re-cut unless `--force` is passed. |
| `crop_rule.json` | Which city the store belongs to ([One store, one city](#one-store-one-city)), which sizing rule cut it, plus whether the provenance manifest has a known gap (below). |
| `crop_provenance.csv` | One row per crop cut (a re-cut appends one; the last row for a label describes the file): where its pixels came from ([#111](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/111)). |
| `crop.log` | The rotating run log (10 MB × 3). |

### The provenance manifest, `crop_provenance.csv`

A crop is a bare JPEG and every consumer of this store is an ML dataset, so the record of where a crop's
pixels came from has to travel with the crop rather than stay with the app. The manifest is that record:

```
city,label_id,pano_id,source,copyright,license,crop_rule_version
```

`city` is `--city`, on every row, so manifests concatenated across cities keep `(city, label_id)` as the
key — `label_id` alone restarts in every deployment. It is empty only for a caller of `bulk_extract_crops`
below `main()` that passes none.

* **One row per crop, appended as it lands** — after the JPEG is on disk, through one handle held for the
  run. That is the two nightly ledgers' contract (`pano_id_log.csv`, `depth_log.csv`), so a run killed at
  any point leaves a truthful partial file: a header, and rows in which every row describes a crop this tool cut, and a kill can leave at most the crop in flight without its row (a torn row is cut back at the next open and recorded as a gap). Nothing else
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
* **`(city, label_id)` is the key when manifests from more than one city are combined.** `label_id`
  restarts at 1 in every city's database, so it alone collides across stores; `city` is on every row, so
  the pair does not. Within one store, `label_id` is what matches a row to its crop file,
  `<label_type_id>/<label_id>.jpg`. There is no runtime uniqueness check on the pair: the manifest is
  append-only and a re-cut appends a second row for its label by design.
* **A failed append does not lose the crop, and is not counted as an error.** The crop is already on disk
  and is the resume marker, so a plain re-run skips it and could never write the row: counting it in
  `errors` would break the promise that errors retry, and would put one label in two buckets. Each one is
  logged to `crop.log` (under the same per-kind cap as the other per-label warnings, as
  `provenance_unrecorded`), and the run summary prints how many crops went unrecorded, on both channels.
  The counts and the exit code are unchanged. Opening the manifest at all is different: if it cannot be
  opened the run stops before cutting anything, exactly as it does when `crop_rule.json` cannot be written.
  A handle that cannot be *closed* cleanly (a network mount reporting a deferred write error) is said on
  both channels and does not stop the run summary.
* **The header on disk is checked before anything is appended under it**
  ([#159](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/159)). A manifest written before
  rows carried a city starts `label_id,pano_id,source,copyright,license,crop_rule_version`; appending the
  seven-field rows under it would shift every column by one, silently. So a manifest with that header is
  **moved, unchanged, to `crop_provenance.pre-city.csv`** and a fresh manifest is started (said on both
  channels). Its rows are never rewritten or given a city — a store cut before #159 may hold more than one
  city's crops, and a city written onto them would be a guess. If `crop_provenance.pre-city.csv` already
  exists, or the header is anything else, the run stops with exit **3** before cutting anything and names
  the file; nothing is moved or replaced.

**Crops cut before the manifest existed have no rows**, since they are not re-cut without `--force` (a
`--force` pass that reaches them adds their rows). `crop_rule.json` records what the runs know about gaps:

| Key | Meaning |
|---|---|
| `provenance_manifest` | The manifest's file name. |
| `provenance_manifest_started_under` | The crop rule in force when the manifest was started. |
| `provenance_manifest_no_known_gap` | `true` if the store held no crops when the manifest was started and no run since has known of a crop left without a row; `false` once either is known; `null` if a manifest is present with no record of how it started. |
| `provenance_manifest_pre_city` | `crop_provenance.pre-city.csv` once a manifest from before rows carried a city has been set aside (above), and kept after that; `null` if none ever was. The fresh manifest started beside it records `provenance_manifest_no_known_gap: false`, since the crops the old rows describe have no row in it. |

The first two are set by the run that starts the manifest and carried forward by every later run.
`provenance_manifest_no_known_gap` starts the same way and only ever goes from `true` to `false`: a run
turns it `false` when an append fails, when the manifest cannot be closed cleanly, or when it finds a
torn row a killed run left behind — that one is recorded *before* the row is cut, so a run killed straight
after still leaves `false`, and a run that cannot record it stops before cutting anything. A marker the run
cannot read is left as it is rather than rebuilt, and the failure to record is said on both channels. The
`false` is kept for good, even through a `--force` pass
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

**Stores cut before the content check may hold black crops counted `success`.** Crops on disk are never
re-judged ([the content check](#the-content-check-black_content)), so a label that sat inside a black band of
its pano before [#164](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/164) is still there
as a mostly black crop. Filter them the way the cropper now would: drop a crop where
`downloaders.common.black_fraction(img) > 0.5`. That reads the stored crop - re-encoded, and downscaled if
its window was wider than `CROP_MAX_STORED_WIDTH` - rather than the raw window the cropper judges, so the two
can disagree slightly for a label near a band's edge; well inside a band both read the band. A store cut
*with* the check can still hold crops up to half black: a bottom band thinner than a sixth of the pano is
never withheld ([the numbers](#the-content-check-black_content)), so a stricter threshold, such as 0.25, is
the consumer's to choose.

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

**Measured 2026-09-26 ([#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54), [reports/2026-09-26-tilt-error-study.md](../reports/2026-09-26-tilt-error-study.md)).** The depth artifact's planes are in the camera-rig frame, exactly. The stored tiles are not gravity-levelled in either scrape era. **The labelled feature sits off the stored `pano_y`, towards the rig pixel `pano_y - T(b) h/180`** (`T(b) = pitch cos b + roll sin b`). In Jon's blind forced choice on 96 vouched-for labels (adjudicated 2026-09-29), the window shifted that way beat its mirror image 79 : 0. The result is confirmed for post179 labels and leak-dominant but short of the rule for older ones. It gives the direction, not the size, and no correction has landed in CropRunner yet. Until one does, treat a crop's vertical centring as off by up to the pano's tilt at the label's bearing: at the p90, 4.2-4.3 deg over all corpus bearings, and 4.8 deg (37.8% of the window height, at full leak) for the most distant labels.
(An earlier note here referred to an "alternative cropper" in development; that effort was abandoned and #54
supersedes it.)

**`label_id` is unique per city, not globally.** Project Sidewalk runs one database schema per city, so two
cities' crops share file names; that is why each city has its own store. Read one city's crops from
`<crop-dir>/<city>/<label_type_id>/`, or glob `<crop-dir>/*/<label_type_id>/` across cities and key on
`(city, label_id)` — the city is the directory name, and the first column of every manifest row. A consumer
still pointed at the old flat root (`<crop-dir>/<label_type_id>/`) finds nothing there and may report zero
crops rather than fail.

## Related

* [API fields](api-fields.md) — what every column of `/adminapi/labels/cvMetadata` means, and the label type IDs.
* [Checking the contract against a live deployment](api-fields.md#checking-the-contract-against-a-live-deployment)
  — `check_cvmetadata_schema.py`, the tripwire for the next upstream field rename. The last one
  ([SidewalkWebpage#4103](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4103)) cost 16 days of
  zero crops against every deployment with CI green throughout.
* [Reports](../reports/README.md) — the crop-geometry, clamp, and click-noise studies behind the numbers above.
