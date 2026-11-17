# Bayonne end to end: the first Panoramax panos through CropRunner

2026-11-17. Issue #111, item 2. **The check passes**: every served Bayonne label was cut, the manifest carries the
served source and both attribution fields on every row, every stored frame is the app's, and the marked crops put
the dot on the labelled feature wherever the feature can be located. One crop on a posed pano has the dot above
the ground, which is the direction the 2026-10-06 report predicts for a tilt leak; the posed panos are almost all
uniform paving, so this run says little about that tilt either way.

## The question

`bayonne-fr` is the one Project Sidewalk city whose imagery comes from Panoramax. Until this run no crop had been
cut from a Panoramax pano: the provenance manifest (#111, PRs 249 and 250) was built and tested on synthetic rows,
and `reports/2026-09-08-panoramax-api.md` calls the frame of `pano_x`/`pano_y` on a Panoramax image "inference
... not a measurement against a real label". So the issue's last item asks for one real end-to-end pass: download
the labelled panos, crop them with the shipped defaults, and check four things:

* the stored frame is the app's frame (M3);
* the crop run's outcome taxonomy is clean (M4);
* the label is actually under its crop's centre (M6, the visual verdict);
* the manifest carries what the server sent (M7).

## Method

**Swept, not sampled.** All 40 labelled panos and all 81 labels served on 2026-11-17 (snapshot at 14:29:11 UTC).
Not covered: the 58 unlabelled panos (no `--all-panos`), sizing rules v3 and v3-depth, any frame wider than 5760
px (none exists in Bayonne), and any variety in the attribution fields (Bayonne has one producer).

**Platform.** Windows 11, CPython 3.12.10, a fresh venv from `requirements.txt` + `requirements-dev.txt` under
`constraints.txt`, without `streetlevel` (it does not import there, and nothing on this path reaches it).
Production is Ubuntu 22.04 / Python 3.10. Nothing below failed, so no result needed to be checked against that
difference.

**Steps**, all on the public path (no SFTP credentials), sequential, no retries needed:

1. `check_cvmetadata_schema.py --host sidewalk-bayonne.cs.washington.edu`: exit 0, 23 fields served, every field
   CropRunner requires or records present.
2. `bayonne_crop_check.py snapshot`: one GET each of `/adminapi/panos` and `/adminapi/labels/cvMetadata`, the
   bytes committed gzipped with the sha256 of the uncompressed bytes.
3. `DownloadRunner.py ... --skip-depth --max-runtime 30` into a scratch store: one run, Panoramax's own
   sequential loop.
4. Four CropRunner runs into scratch crop roots. Run 1 is the live run (`-d`); runs 2-4 read the snapshot with
   `-f`, so all four see one label set and exactly one live fetch is compared with the snapshot (M9):
   1. the default rule into `crops/`;
   2. the same command again into the same root (the resume check);
   3. `--tilt-correction` into its own root;
   4. `--mark-label` into its own root, for the visual verdict.
5. `bayonne_crop_check.py measure`: reads the scratch store, the three crop stores and the five stdout captures,
   and writes the small text files in `reports/data/2026-11-17-bayonne-panoramax-crop-check/`. Each capture ends
   with the command's `exit=N`. Scratch paths are replaced by `<store>`, `<crops>`, `<tilt-crops>`, `<out>` and
   `<scratch>` before commit.

No imagery is committed: not the panos, not the crops, not the contact sheets.

## Results

### Download and store (M1, M2, M3)

| check | result |
|---|---|
| panos served, all / labelled | 98 / 40 |
| labels served, on distinct panos | 81, on 40 |
| labelled panos with ledger verdict `downloaded=1` | 40 of 40 |
| labelled panos not downloaded | 0 |
| stored files outside a two-char shard | 0 |
| `walk_store_panos()` ids equal the ledger's `1` ids | yes |
| distinct shards over the labelled panos | 39 |
| labelled pano ids that are 36-char UUIDs | 40 of 40 |
| stored bytes, labelled panos | 63.8 MB |
| stored frame equals `/adminapi/panos` and cvMetadata | 40 of 40 |
| stored frame 5376x2688 | 13 panos |
| stored frame 5760x2880 | 27 panos |

The planning probe guessed about 1.3 MB a pano from one HEAD; the 40 average 1.6 MB.

### Crop run 1, the live run (M4)

| outcome | labels |
|---|---|
| rows with usable dims | 81 of 81 |
| `success` | 81 of 81 |
| `skipped_existing` | 0 |
| `missing_pano` | 0 |
| `dims_mismatch` | 0 |
| `out_of_frame` | 0 |
| `black_content` | 0 |
| `no_pose` | 0 |
| `errors` | 0 |
| run 1 crop files on disk, manifest rows | 81, 81 |
| run 1 exit code | 0 |

"Usable dims" is the cropper's own `_metadata_dims`. Every row has them, so `dims_mismatch` 0 is a test of every
label and not a pass by default.

### Windows (M5, a consistency check, not a verification)

| check | result |
|---|---|
| window fov min / median / max (deg) | 16.6 / 33.7 / 90.0 |
| windows at the 90 deg cap on 5376-wide panos | 5 |
| windows at the 90 deg cap on 5760-wide panos | 5 |
| windows crossing the seam | 6 |
| windows shifted vertically | 0 |
| windows downscaled for storage | 0 |
| widest window (px) | 1440 |
| crop file size equals the window's stored size | 81 of 81 |
| standalone transcription gives the same window | 81 of 81 |

The standalone transcription is a second copy of rule v2 in the study script, written from the v2 report without
reading `CropRunner`; a test asserts it does not. Two derivations agreeing is not proof that either is right, which
is why M6 looks at the pixels.

**The storage downscale never ran on Bayonne.** A 90 deg window on a 5760-wide pano is exactly 1440 px, which is
`CROP_MAX_STORED_WIDTH`, so `downscale_for_storage` returned every crop unchanged.

### Manifest, marker, resume and drift (M7, M8, M9)

| check | result |
|---|---|
| manifest rows, `(city, label_id)` unique | 81, yes |
| `city` is `bayonne-fr` on every row | yes |
| source and both attribution fields non-empty and equal to the served row | 81 of 81 |
| distinct attribution pairs | 1 |
| source values | `panoramax` |
| `crop_rule_version` | v2 |
| tilt columns | none / 0.0 / blank / blank |
| marker city, rule | bayonne-fr, v2 |
| marker no-known-gap flag, known gaps | true, 0 |
| run 2 `skipped_existing` | 81 of 81 |
| run 2 exit code | 0 |
| manifest bytes unchanged by run 2 | yes |
| labels in run 1's manifest not in the snapshot / the reverse | 0 / 0 |

The join is on `(city, label_id)` inside the one city, with uniqueness asserted on both sides. All 81 rows carry
one attribution pair (`sig_bayonne`, `etalab-2.0`), so "filled on every crop and equal to the served row" is
checked here, and "varies per picture" is not observable.

### The tilt run (M10, a finding)

| check | result |
|---|---|
| tilt run `no_pose` | 81 of 81 |
| tilt run crops written, manifest rows | 0, 0 |
| tilt run `crop.log` lines naming the reason | 40 |
| tilt run exit code | 0 |
| marked run `success` | 81 of 81 |

The 40 `crop.log` lines are one per pano, each with the reason `no .xml and no .depth.npz beside the pano`.
`--tilt-correction` reads a pose only from those two files, and a Panoramax download writes neither, so on
Panoramax the flag skips every label and cuts nothing. That is the designed behaviour (`no_pose` is a skip, not an
error, and the run exits 0). Where Panoramax's pose actually lives (STAC `pers:pitch`/`pers:roll`, mirrored into
`pano_data`) is in `reports/2026-10-06-non-gsv-levelling.md` and is not re-measured here.

### POV consistency (M11, a finding)

The residual is `r = azimuth(pano_x) - (heading - camera_heading + 180)`, wrapped to (-180, 180], against the
click's offset from the canvas centre in canvas widths. The fit is a gain per zoom through the origin. The served
`zoom` is a float, so each row is binned at its nearest integer zoom with its offset scaled by 2^-(zoom - bin).

| zoom | labels | gain (deg per canvas width) | max raw abs r (deg) | max abs leftover after the fit (deg) |
|---|---|---|---|---|
| zoom 1 | 58 | 110.6 | 37.4 | 4.54 |
| zoom 2 | 10 | 57.9 | 14.5 | 0.47 |
| zoom 3 | 13 | 29.5 | 6.2 | 0.52 |

The gain halves per zoom step, and the app's records agree with themselves under a GSV-style convention (column
0 at `camera_heading - 180`). The larger leftover at zoom 1 sits at the canvas edges, which is where a linear model
of a perspective view is weakest; it is not chased further here. This says nothing about the pixels. Whether the hd
image follows the convention is what M6 checks.

## The visual verdict (M6)

**Who judged.** The implementing model (Claude Opus 5.5), not a person, from contact sheets of the 81
`--mark-label` crops (9 per sheet, each captioned `bayonne-fr:<id>`, its type and its pano's pose class), opening
single crops where a tile was too small. The verdicts are provisional until Jon's spot check below.

**Verdicts**: `on-mark` (the labelled feature is under the dot), `in-crop-off-mark` (the feature is in the crop and
the dot is beside it; `direction` says where the dot sits relative to the feature), `not-visible` (no such feature
in the crop), `unclear` (cannot be judged; the note says why). The pose classes are the 2026-10-06 report's:
`posed` (non-zero pitch and roll stored), `zero` (0/0 stored), `none` (no roll stored).

**The pass criterion, fixed in the plan before the run.** FAIL if no crop is `on-mark`; or `not-visible` is at
least half of the judgeable crops (every verdict but `unclear`); or `in-crop-off-mark` is at least half of them and
three quarters of those lie in one direction on the `zero` and `none` panos too. Under 20 judgeable crops the
outcome is inconclusive. A consistent offset on `posed` panos alone is a finding, never a failure.

| verdict | crops |
|---|---|
| `on-mark` | 47 |
| `in-crop-off-mark` | 1 |
| `not-visible` | 0 |
| `unclear` | 33 |
| off-mark directions | up 1, down 0, left 0, right 0 |
| judgeable crops | 48 |
| outcome | **PASS** |

| pose class | on-mark | in-crop-off-mark | not-visible | unclear |
|---|---|---|---|---|
| posed | 3 | 1 | 0 | 18 |
| zero | 16 | 0 | 0 | 8 |
| none | 28 | 0 | 0 | 7 |

What the 47 `on-mark` crops show: poles, posts, barriers, utility covers, zebra stripes and curb ramps under the
dot, on windows from 16.6 to 90 deg wide, including all six seam crossings and all ten windows at the cap. A
constant azimuth offset of a few degrees would move the dot off a pole or a post, so on these panos the hd image's
column 0 is where the app's records put it. That is the measurement the 2026-09-08 report said was missing.

What the 33 `unclear` crops are: 23 SurfaceProblem labels whose crop is uniform paving or asphalt with no located
defect (the dot is on the surface, but an offset could not show), 7 Occlusion labels (the dot is on the parked car
that hides the sidewalk) and 3 NoSidewalk labels (the dot is on the grass verge). These types have no located
referent, as the plan expected.

**The posed panos barely test the tilt.** 18 of the posed crops are `unclear`: they are cobbled riverside paving,
where a vertical offset of a few degrees leaves the dot on cobbles. The one `in-crop-off-mark` crop,
`bayonne-fr:44`, is on a posed pano, with the dot on the riverside railing about 20 px of a 266 px crop above the
paving at its foot. That is the direction a tilt leak would move a stored point, but one crop is an anecdote, not
the first real-label evidence of the tilt that the plan hoped this cross-tab might give.

**Spot-check list for Jon** (the marked crops are in the scratch root named in the PR; the sheets are
`sheets/sheet_01.jpg`-`sheet_09.jpg`):

* every crop not `on-mark`: `bayonne-fr:44` first, then the 33 `unclear` ones listed in `visual-verdicts.csv`;
* the six seam crossings: `bayonne-fr:7`, `13`, `14`, `34`, `81`, `84`;
* the ten at the 90 deg cap: `bayonne-fr:4`, `7`, `81`, `84`, `87` (5376 wide) and `bayonne-fr:11`, `52`, `53`,
  `56`, `57` (5760 wide);
* one per type: CurbRamp `bayonne-fr:82`, NoCurbRamp `bayonne-fr:5`, Obstacle `bayonne-fr:8`, SurfaceProblem
  `bayonne-fr:41`, Crosswalk `bayonne-fr:21`, Occlusion `bayonne-fr:60`, NoSidewalk `bayonne-fr:78`.

## Findings that are not failures

* **No Bayonne frame is wider than 5760 px**, so the storage downscale never ran, and the 12288x6144 line in #111
  stays unverified on Panoramax imagery. A 12288x6144 frame decodes to 226 MB (`w*h*3`); Bayonne's largest decodes
  to 49.8 MB.
* **One attribution pair across all 81 labels.** The manifest copies it correctly; whether it would copy a
  per-picture value correctly is not observable in Bayonne.
* **`--tilt-correction` is all `no_pose` on Panoramax**, by design (M10).
* **The verdict by pose class** (above): the posed panos are nearly all uniform paving, so the cross-tab does not
  settle whether labels on posed panos sit at the stored point or the rig pixel.

## Where the data lives

`reports/data/2026-11-17-bayonne-panoramax-crop-check/`:

* `panos.json.gz`, `cvmetadata.json.gz`: the two responses, verbatim, gzipped; `snapshot.json`: time, counts,
  platform, and the sha256 of each uncompressed file (the manifest's too).
* `pano_id_log.csv`, `download-log-row.csv`: the download ledger and its `log.csv` row, as written.
* `panos.csv`: per labelled pano: shard, bytes, sha256, stored and app frames, ledger verdict.
* `crops.csv`: per label (`label_uid`): the window, both derivations' sizes, and the crop file's size, bytes and
  sha256.
* `crop_provenance.csv.gz` (run 1's manifest, verbatim, gzipped) and `crop_rule.json` (the store's marker).
* `visual-verdicts.csv`: the per-crop verdicts.
* `run-*.stdout.txt` (the five captures), `run-crop.log.txt`, `run-crop-tilt.log.txt`, `run-scrape.log.txt`.

`reports/data/2026-11-17-bayonne-panoramax-crop-check.json` is the summary, including every table row above.
The schema check's own capture is not committed: it lists every served field name, and its result is quoted in
Method instead.

## Tests that pin it

`tests/test_bayonne_crop_check_report.py`:

* one planted fault per detector on a synthetic store cut by the real `CropRunner.bulk_extract_crops`, plus a
  clean store that reports nothing;
* the standalone transcription against `CropRunner` on a grid of 312 windows, and a perturbed constant on either
  side reported as a disagreement;
* the verdict rule on synthetic tallies on both sides of each threshold;
* the headline counts re-derived from the raw files (the run-1 capture, the gzipped manifest and snapshot,
  `visual-verdicts.csv`, `crops.csv`), not from the summary JSON;
* every table row the script emitted appears in this report;
* no local path in any committed file.

## What changed in the code

Nothing in production. This PR adds the study script, its test, the data and this report.

## Wrong turns

* **The first dry `measure` reported a median window of 90.0 deg.** `studyfmt.percentile` takes a fraction, and
  the script passed 50, which returns the maximum. A planted-gain test now pins a median.
* **The first POV table had a zoom 0 and a 10 deg leftover at zoom 1.** The served `zoom` is a float (0.998,
  1.07, 1.99, ...), and `int()` truncated it. Rows are now binned at the nearest step with the offset rescaled.
* **The download ledger has a header row**, which the first ledger reader counted as a verdict.
* **A mutant moving the direction threshold from 3/4 to 7/10 survived the first test battery**, because the
  unposed clause masked it. A case per side of each clause now kills it.

## Open questions

* The 12288x6144 line of #111, for a Panoramax producer that serves such frames. None does in Bayonne.
* The tilt hand-off: the follow-up for Panoramax labels on posed panos that `reports/2026-10-06-non-gsv-levelling.md`
  recommended was never filed, and its pool is mostly uniform paving (above). Whether to file it or note it on #197
  is Jon's decision.
* The zoom-1 leftover of up to 4.5 deg at the canvas edges, if anyone needs the app's view record to that
  precision.

## Reproduce

```bash
python reports/scripts/bayonne_crop_check.py snapshot --host sidewalk-bayonne.cs.washington.edu \
    --out reports/data/2026-11-17-bayonne-panoramax-crop-check --plain "$B/out"
python DownloadRunner.py sidewalk-bayonne.cs.washington.edu "$B/store/bayonne-fr" --skip-depth --max-runtime 30
python CropRunner.py -d sidewalk-bayonne.cs.washington.edu -s "$B/store/bayonne-fr" -o "$B/crops" --city bayonne-fr
cp "$B/crops/bayonne-fr/crop_provenance.csv" "$B/out/crop_provenance.run1.csv"
python CropRunner.py -f "$B/out/cvmetadata.json" -s "$B/store/bayonne-fr" -o "$B/crops" --city bayonne-fr
python CropRunner.py -f "$B/out/cvmetadata.json" -s "$B/store/bayonne-fr" -o "$B/crops-tilt" --city bayonne-fr --tilt-correction
python CropRunner.py -f "$B/out/cvmetadata.json" -s "$B/store/bayonne-fr" -o "$B/crops-marked" --city bayonne-fr --mark-label
# each CropRunner/DownloadRunner stdout captured to "$B/out/<name>.stdout.txt" with "exit=$?" appended
python reports/scripts/bayonne_crop_check.py measure --store "$B/store/bayonne-fr" --crops "$B/crops/bayonne-fr" \
    --tilt-crops "$B/crops-tilt/bayonne-fr" --marked-crops "$B/crops-marked/bayonne-fr" --runs "$B/out" \
    --scratch "$B" --sheets "$B/sheets" --write
```

`B` is any scratch directory outside the repo. A later run sees whatever labels the server serves then; M9 and the
snapshot's sha256 say whether that moved.
