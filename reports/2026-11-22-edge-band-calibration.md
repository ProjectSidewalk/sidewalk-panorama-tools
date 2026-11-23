# Calibrating the edge black-band limit on the real pano store (#179 item 4)

**The question.** `EDGE_BAND_MAX_FRACTION` (0.05) decides when the downloader flags a stitch and what
`scan_black_bands.py` puts on its work-list, and it was set from the tile grid, never measured (#179 item 4).
Does 0.05 separate real black bands from our own stitching faults on the stored panos, per source?

**The answer, on 133,919 of 133,919 frozen ids** (every non-GSV pano on the store, and a census or a 2,500-pano
sample of each folder's GSV panos):

- **Depth cannot separate them on GSV stitches, so rule D1 keeps 0.05 and the choice is the owner's.** 18 GSV
  stitches carry a bottom band that is not the stitcher's tile-row shape as the rule defines it (class U). All 18
  are deeper than both candidates, so 0.05 and 0.0605 give the same 1.68 false flags per 10,000 stored GSV
  stitches (bound 14.24): over the 1-per-10,000 estimate D1 allows. Read on their shape, though, these 18 look
  like tile faults just outside the rule's window, not like imagery (Results, class U).
- **On Mapillary a deep exact-black bottom band is the normal shape of two camera setups**: juneau-ak's
  8000x4000 panos (4,847 of 4,847 over 0.05, mostly at 0.1740) and richmond-va's 11000x5500 ones (4,132 of 5,075,
  mostly at 0.1875). The work-list on such a folder lists camera caps. Panoramax and the other Mapillary folders
  carry none.
- Photospheres: 2 of 869 are over 0.05, by the sweep's definition and the downloader's alike.
- No production behaviour changes. The limit stays 0.05 (decision 6, provisional); comments and docs now point here.

This file opens with the dated decision entry, committed and pushed before any band depth of the run was read
(6a38e90; the addendum in 9ed414c). It is unchanged below; the results follow it.

## Decision entry, 2026-11-22 (written before the measurement)

What was known when this was written:

- **The pilot** (`data/2026-11-22-edge-band-calibration/pilot/`, two read-only probes run by the planner on the
  host that mounts the store, redacted as each file says, and superseded: an inline copy of the detector on
  Pillow 11.0.0, bottom band only in the first). juneau-ak: 17 of 18 sampled panos have an exact-black bottom
  band of 0.174. richmond-va: 4 of 18 at 0.186 to 0.1875, plus one at exact 0 with a near-black band of 0.138.
  On the four measured on all sides, right and top bands are 0. So **the pilot already shows what D3 will
  report for Mapillary**: a deep exact-black bottom band is the normal shape of those cameras, at depths over
  0.05. That is why D3 below is a report and not a rule.
- **Cost**: 0.32 s per GSV pano (mean 11.5 MB, n = 36) and 0.12 s per Mapillary or Panoramax pano (about 2 MB,
  n = 42), one reader. 1,622,312 image-ledger rows over 62 folders.
- **The tile-row tolerance `t` = 8 px**, measured before the run (`data/2026-11-22-edge-band-calibration/tile-tolerance.json`,
  `edge_band_calibration.py tolerance`): black tile rows stitched by `gsv_tiles._stitch_tiles` from real tile
  bodies and Google's out-of-range black tile, LANCZOS-upscaled by it, saved as `gsv.py` saves a stitch, and
  measured by the production file path, read 8 px short (one reduced line) at zoom 3 and 4 and exactly at
  zoom 5, on 16384x8192 and 13312x6656 frames alike.

### Population and design

Every pano `walk_store_panos()` yields in each city folder of the store when `frame` runs; folders with no
shard dirs, display copies and crops are excluded. Ids are classed by form: `gsv22`, `numeric` (Mapillary),
`uuid` (Panoramax), `photosphere` (a `CAoS` id whose file's mtime is on or after 2026-11-05), `other`.
**Census**: every non-`gsv22` id, and every `gsv22` id of a folder holding 2,500 or fewer. **Sample**: 2,500
`gsv22` ids per larger folder, `random.Random('179-calibration:' + city).sample(sorted_ids, 2500)`, drawn once and
frozen in `ids/<city>.txt.gz`. Fleet figures cover the analyzer's roster (`log_analyzer/cities.csv`, `#` rows
left out, as `log_analyzer/analyze.py` does) and are de-duplicated on `pano_id`, first folder in roster order.

### Classes (from the ledger row; exact-black columns unless said)

| Class | Rule |
|---|---|
| P, provider's image | id form `numeric`, `uuid` or `photosphere` |
| F-frame | `gsv22` or `other`, right band > 0 |
| F-rows | `gsv22` or `other`, right 0, bottom > 0, `bottom x height` in `[512k - t, 512k]` for a whole k >= 1 (widened only by the ledger's 4-decimal rounding), t = 8 px |
| U, unresolved | `gsv22` or `other`, right 0, bottom > 0, not tile-aligned |
| N, near-only | exact 0 on both edges, near > 0 |
| clean | all four depths 0 |

### Rules

- **D1, the limit for stitches.** Candidates 0.05 (shipped) and 0.0605. For a candidate, the false-flag rate is
  the stratum-weighted share of stored `gsv22` stitches with a class-U bottom band over it (production's strict
  `>`), with a conservative one-sided 95% upper bound (each sampled stratum's exact binomial bound, weighted; a
  stratum measured whole contributes its exact share). A candidate passes when the estimate is at most 1 per
  10,000 and the bound at most 15 per 10,000. 0.05 passes: confirmed. 0.05 fails and 0.0605 passes: 0.0605,
  provisional. Neither: 0.05 stays and the choice is the owner's. The limit is never lowered.
- **D1 sensitivity.** Every group of 5 or more F-rows panos with one identical bottom depth and right band 0 in
  one (city, frame) is folded into U and D1 is run again; if the outcome differs, 0.05 stays and the choice is
  the owner's.
- **What would fail D1**, from the pilot's ledger-line counts (55 roster GSV folders, 1,600,797 rows; an
  approximation, since ledger lines are not pano counts): the zero-event upper bound is about 11.8 per
  10,000; in the heaviest stratum (chicago-il, weight about 0.186) 2 events over 0.05 fail the estimate; in the
  median sampled stratum (weight about 0.011) 23 do. The same figures computed from `frame.json` are added
  below, in a commit before the sweep starts.
- **D2, near-black.** No definition changes. Counted per class: near band over the limit while the exact band is not.
- **D3, sources. A report, not a rule.** Depth distributions per id form and per (city, frame) from the full
  non-GSV census.
- **D4, photospheres. Report only.** The share over the limit by the sweep's definition and by the downloader's
  in-memory one, and how often they agree.
- **D5, the 98% line minimum. Report only.** How many banded panos change verdict at 0.90 and at 1.00.

Said in advance: exact black cannot come from dark imagery in tiles of the current request shape, so class U can
only fill from the older one.

### Addendum, 2026-11-22, after `frame` and before the sweep

`frame` ran (enumeration only; no pano opened) and froze 133,919 ids over 62 folders (`frame.json`,
`ids/<city>.txt.gz`); 35 folders are sampled. **What would fail D1, computed by
`edge_band_calibration.d1_what_would_fail` from `frame.json`'s weights**, which replace the pilot
approximation above: the zero-event upper bound is 11.80 per 10,000; the heaviest stratum is chicago-il
(weight 0.1800, n = 2,500), where 2 U events over 0.05 fail the estimate; the median sampled stratum is burnaby
(weight 0.0103, n = 2,499 after one id that also sits in an earlier roster folder), where 25 do. No band depth
had been read when this was written; the instrument check (`instrument-check.csv`, the committed fixtures
measured in the run environment) was the only measurement taken.

### Load limits

1 reader to start, 2 at most; 60 MB/s aggregate, held by pausing readers; a 15-minute opening slice at 1 reader
whose latency is written down first; abort on 30 s continuously in uninterruptible sleep, on 120 s of unpaused
time with no ledger row, stdout line or read, or on the last 100 panos of a source taking over 3 times the
baseline; no retry after an abort; a hard stop at 17:00 Pacific.

## Session log

Times are Pacific; the opening slice and main1 on 2026-11-22, main2 on 2026-11-23. Nothing in this section is a
band result.

| Session | Start | End | Readers | Panos ledgered | GB read | Outcome |
|---|---|---|---:|---:|---:|---|
| opening | 08:54 | 09:06 | 1 | 3,415 | 33.0 | stopped by its child's `--max-runtime`; no abort; no-writes check passed |
| main1 | 09:07 | 15:12 | 2 | 127,769 | 1,097.8 | **aborted by the 3x rule** on chicago-il (last 100 GSV panos 65.1 s against 3 x 20.94 s); both children continued and terminated, none left alive; no-writes check passed |
| main2 | 09:08 (11-23) | 09:49 | 1 | 2,735 | 29.6 | sweep complete at 09:23, no abort; extras over 9,987 panos 09:23-09:49; no-writes check passed |

**The opening slice** (`sessions/opening.json`, `baseline.json`): bayonne-fr, then the smallest GSV folders.
Per 100 panos of unpaused time: **20.94 s for GSV** (one reader read 38.6 to 51.5 MB/s on the folders over 100
panos) and **8.16 s for provider bytes** (bayonne-fr's 98 panos only, 18.6 MB/s). Reached of frozen: bayonne-fr 98
of 98, goiania-br 9 of 9, beringen-be 49 of 49, newport-ky 257 of 257, clifton-nj 588 of 588, blackhawk-hills-il
609 of 609, la-piedad-old 619 of 619, fort-wayne-in 695 of 695, hackensack-nj 491 of 758; 3,415 of 133,919 in all.

**main1** (`sessions/main1.json`, `abort-main1.json`, `run-main1.log.txt`, `timing.csv`) was started at 09:07 with 2 readers
and the opening's baseline, to sweep until 16:10 and then run the extras until 16:50. At 09:49 the connection
used to wait on it was lost from this side (exit 255); the host and the job were found healthy at 10:11, and
the session ran detached. It read 1,097.8 GB in 6 h 06 min of wall time, **50.0 MB/s on average**, under the 60
MB/s cap, which paused a reader for 5 to 457 s per GSV folder. At 15:12:52 the 3x rule fired on chicago-il: its
last 100 GSV panos took 65.1 s of unpaused time against a limit of 62.8 s. The dispatcher continued and then
terminated both children (chicago-il and knox-oh, exit -15), none was left alive, and no extras ran. Per the
rule, there was no retry that day. Unpaused time per 100 panos over main1's GSV folders ran 18.2 to 29.0 s, against
the opening's 20.94 s at one reader. After main1, 131,184 of 133,919 frozen ids were measured: 60 folders complete,
chicago-il at 772 of 2,785 and knox-oh at 1,902 of 2,624 (the sorted-id prefix of each frozen list).

**main2** (`sessions/main2.json`, `run-main2.log.txt`), the next morning, with **one reader** (the orchestrator's
call after main1's abort, provisional: it matches the one-reader baseline the 3x rule compares against). The abort
marker was renamed `abort-main1.json`, never deleted. main2 resumed chicago-il and knox-oh from the same farms with
no redraw (the tool found the 772 and 1,902 already ledgered panos current) and finished both: 2,735 panos, 29.6 GB,
at 30.5 s (chicago-il) and 32.3 s (knox-oh) per 100 panos of unpaused time, under the 62.8 s limit. So one reader on
that morning was slower per pano than two readers had been the day before: the per-pano time follows the file
server's load, not only the reader count. The extras pass then read the 9,987 panos with any band, 09:23 to 09:49.
**133,919 of 133,919 frozen ids are measured; every folder has `done.json`.**

**No writes.** Every session's listing of the store root and each city root before and after (`nowrites-*.txt`)
matched: 0 changes to names the tools can create, and 0 other changes. Reading a file may update its access time
on the file server; that is metadata, not content, and the setting cannot be checked from here.

### Deviations

- The opening slice ran 12 minutes, not 15: the dispatcher ends each child's `--max-runtime` 3 minutes before its
  stop, so a `--minutes 15` session's child stops at 12. The baseline rests on 3,317 GSV panos and 98 provider panos.
- The plan named `la-piedad-old` and `validation-study` as folders outside the fleet. Both are rows of
  `log_analyzer/cities.csv` that the analyzer monitors (`analyze.py` drops only `#` rows), so by the plan's rule
  ("whatever the analyzer treats as the roster") they are fleet folders here. Three Mapillary folders are not on
  the roster (brussels-be, kaysville-ut, sacramento-ca) and are reported separately. **Provisional.**
- The planner's "about 13 events" for a median stratum assumed a weight near 0.02; `frame.json` gives 0.0103
  (burnaby), hence 25.
- **The 3x rule fired on two readers sharing the cap**, then held with one reader at a slower per-pano time the
  next morning. The rule caught a slow day, not a stall: per-pano time on this file server moves with its load.
  Nothing was lost. The run resumed from the frozen farm, as designed.
- **main1's wait connection dropped on this side** (exit 255). The dispatcher had been started detached, so it ran
  on under its own limits. The connection, not the job, was what failed.

## Results

Every figure below is emitted by `edge_band_calibration.py analyze` from the committed raw files and stored in
`data/2026-11-22-edge-band-calibration.json`. `tests/test_edge_band_calibration_report.py` re-derives it and checks
that every table row is in this file. Unless a table says otherwise, the population is **every measured frozen
id** (census and sample together, all folders). Depths are the ledger's exact-black columns.

### Classes

| Id form | P | F-frame | F-rows | U | N | clean |
|---|---:|---:|---:|---:|---:|---:|
| gsv22 | 0 | 10 | 4 | 18 | 20 | 111472 |
| numeric | 21410 | 0 | 0 | 0 | 0 | 0 |
| uuid | 98 | 0 | 0 | 0 | 0 | 0 |
| photosphere | 869 | 0 | 0 | 0 | 0 | 0 |
| other | 0 | 0 | 0 | 0 | 0 | 18 |

All 21,410 Mapillary (`numeric`), 98 Panoramax (`uuid`) and 869 photosphere panos are class P whatever their bands
(the provider's bytes). Of 111,524 measured `gsv22` stitches, 111,472 have no band of any kind. The 18 `other` ids
are all clean.

- **F-frame, 10:** 9 seattle-wa stitches with the D4 shape (bottom and right 0.1875), and one keelung-tw stitch
  with a 0.75 band (the pre-#44 zoom-3 grid shape), per `extras.csv.gz`. These are the faults the limit exists for,
  and both candidates flag them.
- **F-rows, 4:** the table below. A whole 512-px row read 8 px short (0.0757 x 6656 = 504 px), within t.
- **N, 20:** a near-black bottom band with no exact one. Never a verdict.
- **U, 18:** below.

| Bottom depth | Frame | Folder | mtime year | Panos |
|---|---|---|---:|---:|
| 0.0757 | 13312x6656 | columbus-oh | 2021 | 3 |
| 0.0757 | 13312x6656 | washington-dc | 2017 | 1 |

### Class U: 18 stitches, and what their shape says

`unresolved.csv` lists them: newberg-or 10, washington-dc 7, columbus-oh 1. They come in exactly two depths:

- **0.0745 on 13312x6656 frames (10 panos):** 0.0745 x 6656 = 496 px, one 512-px tile row read 16 px short. The
  committed tolerance is t = 8 px, so the rule calls them U.
- **0.0769 on 3328x1664 frames (8 panos):** 0.0769 x 1664 = 128 px. A 3328x1664 pano is native at zoom 3, and
  1664 = 3 x 512 + 128, so 128 px is exactly the **partial last tile row** of that grid. The F-rows rule only
  looks for whole 512-px rows, so it cannot see this shape.

Right band 0 and top band 0 on all 18. The in-memory reading equals the file's on all 18. All carry Pillow's
default quantisation tables. That is consistent with a missing bottom tile row in our own stitch. It is an
inference from shape only. Nobody has looked at these panos (decision 7), and the rules were not changed to absorb
them (see Wrong turns).

### D1, the limit for stitches

Strata are the fleet folders' de-duplicated `gsv22` populations. A sampled stratum contributes its exact binomial
upper bound, and a folder measured whole contributes its exact share.

| Run | Candidate | U events | Estimate per 10,000 | Upper bound per 10,000 |
|---|---|---:|---:|---:|
| primary | 0.05 | 18 | 1.68 | 14.24 |
| primary | 0.0605 | 18 | 1.68 | 14.24 |
| folded | 0.05 | 18 | 1.68 | 14.24 |
| folded | 0.0605 | 18 | 1.68 | 14.24 |

Both candidates count the same 18 events, because every U depth (0.0745, 0.0769) is over 0.0605 too. Each gives
an estimate of 1.68 per 10,000 against the 1 allowed, and a bound of 14.24 against the 15 allowed. washington-dc's
7 events in a 2,500-pano sample of a stratum weighted 0.0511 are enough on their own to fail the estimate. The
sensitivity fold found no group of 5 or more identical-depth F-rows panos in one (city, frame), so it changed
nothing.

**Outcome: neither candidate passes. 0.05 stays, and the choice is the owner's** (plan decision 6, provisional).
`EDGE_BAND_MAX_FRACTION` is unchanged, and `tests/test_black_bands.py` now pins it to this decision. The decision
entry's figures had said what would fail: 2 events in chicago-il, or 25 in a median stratum. The real failure was
7 events in a stratum weighted a little over a quarter of chicago-il.

### D2, near-black

| Class | Id form | Near band over 0.05, exact band not |
|---|---|---:|
| P | numeric | 904 |

Panos whose near band (luma at most 2) is over 0.05 while the exact band is not. All 904 are richmond-va's
12288x6144 panos. Their ledgers show a near-black cap of 0.1380 (901 panos) or 0.1367 (3) with no exact band:
the encoder effect the near columns were added for (#213, S4).

### D3, sources (report only)

The provider-bytes depth distribution per (folder, frame, id form) is in the appendix. Only four rows have an
exact band over 0.05:

| Folder | Frame | Id form | Panos | Modal bottom depth | Panos at the mode | Exact band over 0.05 |
|---|---|---|---:|---|---:|---:|
| juneau-ak | 8000x4000 | numeric | 4847 | 0.1740 | 4334 | 4847 |
| kaysville-ut | 8192x4096 | numeric | 1 | 0.1816 | 1 | 1 |
| richmond-va | 11000x5500 | numeric | 5075 | 0.1875 | 3741 | 4132 |
| zurich | 8000x4000 | photosphere | 3 | 0.0720 | 2 | 2 |

- On these two Mapillary camera setups the black cap is between 0.17 and 0.19 deep, past the cropper's H/6
  (0.167) content limit.
- Mapillary at other frame sizes (juneau-ak's 4096x2048 and 5760x2880, laurens-ia, brussels-be, sacramento-ca,
  and richmond-va's other sizes) and Panoramax (bayonne-fr) carry no exact band over 0.05.
- So on juneau-ak and richmond-va the work-list is mostly camera caps. Dropping every crop whose pano is on it
  would drop most of those folders.
- **Which per-source policy follows is the owner's decision 2. No code follows from it here.**

### D4, photospheres (report only)

| Photospheres | Over 0.05 (sweep) | In-memory reading known | Over 0.05 (in memory) | Agree |
|---:|---:|---:|---:|---:|
| 869 | 2 | 869 | 2 | 869 |

- 2 of 869 stored photospheres have a band over 0.05: two zurich files at 0.0720, by both definitions. The
  downloader flags each such pano once, when it saves it.
- The in-memory definition was computed on the 3 photospheres with any band in the file. The other 866 have no
  exact or near band in the file, and are counted as not over in memory: a line that is not near-black at 1/8
  scale cannot be exact black after a box mean.
- Whether to stop flagging photospheres is the owner's decision 3.

### D5, the 98% line minimum (report only)

| Line minimum | Banded panos whose verdict at 0.05 changes |
|---|---:|
| 0.90 | 0 |
| 1.00 | 0 |

Among the 9,987 panos with any band, none changes its verdict at 0.05 when the line minimum is 0.90 or 1.00.
The bands are clean-edged.

### The file witness

| Id form | Pillow default tables | Panos with any band |
|---|---|---:|
| gsv22 | yes | 52 |
| numeric | yes | 9932 |
| photosphere | no | 3 |

Every banded stitch carries Pillow's default quantisation tables, as a stitch of ours should. **But so does every
banded Mapillary file.** Mapillary's files carry the same standard tables, so the witness cannot tell the two
apart. Only the photospheres differ. The plan treated the witness as evidence, not a rule, and that is all it can
be: the classification stands on id form alone. The optional server cross-check of id form against the app's
`source` field was **not run** (optional; skipped to keep load off the shared host).

## What changed in the code

Comments and docs only, in the commit that adds this section: `downloaders/common.py` (the limit's comment states
this calibration), the docstrings in `downloaders/gsv.py`, `scan_black_bands.py` and `tests/test_black_bands.py`,
the comment in `DownloadRunner.py`, `docs/ops.md`, `docs/downloader.md`, `docs/cropper.md`,
`.claude/rules/store-repair.md` and `.claude/rules/downloader.md`. The study script is
`reports/scripts/edge_band_calibration.py`.

## The tests that pin it

`tests/test_edge_band_calibration_report.py` covers both sides:

- **The code**, on synthetic stores: the frozen draw, the farm, the guards, the dispatcher's abort rules,
  signals, exit codes and cap, the redaction, the class rules at their boundaries, D1, its property and its fold,
  the bounds, and the cross-check.
- **The committed run**: no host path or account in any committed file, the instrument check, every ledger id in
  its frozen list, the summary JSON equal to what the raw files reduce to, every table row in this report, and
  every figure claimed by one population.

`tests/test_black_bands.py::test_the_limit_is_the_committed_calibrations_decision` pins the limit to this decision.

## Wrong turns

- **The pilot's instrument** was an inline copy of the detector on another Pillow, bottom band only. It was
  superseded by running the production tool unmodified over a farm.
- **The first plan** read with 8 workers, had no throttle, and redrew the sample on every resume. The review
  replaced that with 1 to 2 readers, a 60 MB/s cap, abort rules and a frozen draw.
- **The F-rows rule did not anticipate the two shapes class U is made of.** t was measured as 8 px on stitches
  made by the stitcher, from imagery tiles above a black row. The stored 13312x6656 bands read 16 px short. The
  rule also only looks for whole 512-px rows, which misses the 128-px partial last row of a native zoom-3 frame.
  Both were found after the decision entry. **By the plan, the rules were not changed to fit them.** Folding these
  18 into F-rows would make D1 pass, so that is exactly the kind of change that must be the owner's: decision 6,
  and the class-U look, decision 7.
- **The opening slice ran 12 minutes, not 15**, because of the child margin (Deviations).

## Open questions

The plan's decisions table (decisions 1 to 10 in the plan on #179) stands. These are sharpened by the results:

- **Decision 6, the limit.** Keep 0.05: a false flag costs one alarm under save-and-flag. Or look at the 18 U
  panos first: if they are tile faults, they are correct flags and D1's premise (U means not ours) does not hold
  for them.
- **Decision 7.** Someone looks at the 18 panos in `unresolved.csv`.
- **Decision 2.** A per-source policy for juneau-ak's and richmond-va's caps (0.1740, 0.1875).
- **Decision 3.** 2 of 869 photospheres are over the limit.
- **Decision 4.** Whether to sweep the whole fleet: F-class panos were found (10 F-frame, 4 F-rows).

## Appendix: reach and the provider-bytes depth table

Frozen and reached per folder ("fleet" = on the analyzer's roster):

| Folder | Frame | Design | Frozen | Reached |
|---|---|---|---:|---:|
| amsterdam | fleet | sample | 2508 | 2508 |
| auckland | fleet | census | 2280 | 2280 |
| bayonne-fr | fleet | census | 98 | 98 |
| beringen-be | fleet | census | 49 | 49 |
| blackhawk-hills-il | fleet | census | 609 | 609 |
| brussels-be | other | census | 882 | 882 |
| burnaby | fleet | sample | 2547 | 2547 |
| cdmx | fleet | sample | 2500 | 2500 |
| chandigarh-india | fleet | sample | 2500 | 2500 |
| chicago-il | fleet | sample | 2785 | 2785 |
| cliffside-park-nj | fleet | census | 1304 | 1304 |
| clifton-nj | fleet | census | 588 | 588 |
| columbia-sc | fleet | sample | 2502 | 2502 |
| columbus-oh | fleet | sample | 2506 | 2506 |
| cuenca | fleet | sample | 2525 | 2525 |
| danville-il | fleet | sample | 2500 | 2500 |
| detroit-mi | fleet | census | 1859 | 1859 |
| fort-wayne-in | fleet | census | 695 | 695 |
| gainesville-fl | fleet | sample | 2500 | 2500 |
| goiania-br | fleet | census | 9 | 9 |
| hackensack-nj | fleet | census | 758 | 758 |
| houston-tx | fleet | census | 2434 | 2434 |
| juneau-ak | fleet | census | 5335 | 5335 |
| kaohsiung-tw | fleet | sample | 2515 | 2515 |
| kaysville-ut | other | census | 300 | 300 |
| keelung-tw | fleet | census | 2009 | 2009 |
| knox-oh | fleet | sample | 2624 | 2624 |
| la-ca | fleet | census | 2172 | 2172 |
| la-piedad | fleet | census | 1823 | 1823 |
| la-piedad-old | fleet | census | 619 | 619 |
| laurens-ia | fleet | census | 4656 | 4656 |
| madison-wi | fleet | sample | 2500 | 2500 |
| maywood-nj | fleet | sample | 2500 | 2500 |
| mendota-il | fleet | sample | 2503 | 2503 |
| new-taipei-tw | fleet | sample | 2502 | 2502 |
| newberg-or | fleet | sample | 2500 | 2500 |
| newport-ky | fleet | census | 257 | 257 |
| niagara-falls-ny | fleet | sample | 2500 | 2500 |
| oradell-nj | fleet | sample | 2500 | 2500 |
| paterson-nj | fleet | sample | 2500 | 2500 |
| pittsburgh-pa | fleet | sample | 2519 | 2519 |
| rancagua-chile | fleet | census | 1737 | 1737 |
| richmond-va | fleet | census | 9407 | 9407 |
| sacramento-ca | other | census | 830 | 830 |
| santiago-chile | fleet | census | 1083 | 1083 |
| sao-paulo-brazil | fleet | census | 1113 | 1113 |
| seattle-wa | fleet | sample | 2548 | 2548 |
| spgg | fleet | sample | 2612 | 2612 |
| st-louis-mo | fleet | sample | 2521 | 2521 |
| taichung-tw | fleet | sample | 2504 | 2504 |
| tainan-tw | fleet | census | 961 | 961 |
| taipei | fleet | sample | 2559 | 2559 |
| teaneck-nj | fleet | sample | 2500 | 2500 |
| tucson-az | fleet | sample | 2500 | 2500 |
| validation-study | fleet | sample | 2500 | 2500 |
| vancouver-wa | fleet | sample | 2500 | 2500 |
| virden-il | fleet | census | 1666 | 1666 |
| walla-walla-wa | fleet | sample | 2501 | 2501 |
| waltham-ma | fleet | sample | 2500 | 2500 |
| washington-dc | fleet | sample | 2500 | 2500 |
| west-chester-pa | fleet | sample | 2500 | 2500 |
| zurich | fleet | sample | 2605 | 2605 |

Provider-bytes panos by (folder, frame, id form):

| Folder | Frame | Id form | Panos | Modal bottom depth | Panos at the mode | Exact band over 0.05 |
|---|---|---|---:|---|---:|---:|
| amsterdam | 13312x6656 | photosphere | 3 | 0.0000 | 3 | 0 |
| amsterdam | 4608x2304 | photosphere | 1 | 0.0000 | 1 | 0 |
| amsterdam | 5376x2688 | photosphere | 3 | 0.0000 | 3 | 0 |
| amsterdam | 8704x4352 | photosphere | 1 | 0.0000 | 1 | 0 |
| bayonne-fr | 5376x2688 | uuid | 55 | 0.0000 | 55 | 0 |
| bayonne-fr | 5760x2880 | uuid | 43 | 0.0000 | 43 | 0 |
| brussels-be | 4096x2048 | numeric | 34 | 0.0000 | 34 | 0 |
| brussels-be | 5376x2688 | numeric | 18 | 0.0000 | 18 | 0 |
| brussels-be | 5760x2880 | numeric | 830 | 0.0000 | 830 | 0 |
| burnaby | 3840x1920 | photosphere | 5 | 0.0000 | 5 | 0 |
| burnaby | 5760x2880 | photosphere | 42 | 0.0000 | 42 | 0 |
| chicago-il | 10240x5120 | photosphere | 1 | 0.0000 | 1 | 0 |
| chicago-il | 3840x1920 | photosphere | 4 | 0.0000 | 4 | 0 |
| chicago-il | 5120x2560 | photosphere | 1 | 0.0000 | 1 | 0 |
| chicago-il | 5376x2688 | photosphere | 1 | 0.0000 | 1 | 0 |
| chicago-il | 5760x2880 | photosphere | 220 | 0.0000 | 220 | 0 |
| chicago-il | 6000x3000 | photosphere | 10 | 0.0000 | 10 | 0 |
| chicago-il | 6656x3328 | photosphere | 1 | 0.0000 | 1 | 0 |
| chicago-il | 7404x3702 | photosphere | 1 | 0.0000 | 1 | 0 |
| chicago-il | 7408x3704 | photosphere | 1 | 0.0000 | 1 | 0 |
| chicago-il | 7410x3705 | photosphere | 1 | 0.0000 | 1 | 0 |
| chicago-il | 7680x3840 | photosphere | 35 | 0.0000 | 35 | 0 |
| chicago-il | 7776x3888 | photosphere | 1 | 0.0000 | 1 | 0 |
| chicago-il | 8192x4096 | photosphere | 2 | 0.0000 | 2 | 0 |
| chicago-il | 8704x4352 | photosphere | 1 | 0.0000 | 1 | 0 |
| columbia-sc | 10892x5446 | photosphere | 1 | 0.0000 | 1 | 0 |
| columbia-sc | 8192x4096 | photosphere | 1 | 0.0000 | 1 | 0 |
| columbus-oh | 10240x5120 | photosphere | 1 | 0.0000 | 1 | 0 |
| columbus-oh | 6720x3360 | photosphere | 5 | 0.0000 | 5 | 0 |
| cuenca | 10240x5120 | photosphere | 1 | 0.0000 | 1 | 0 |
| cuenca | 13824x3646 | photosphere | 1 | 0.0000 | 1 | 0 |
| cuenca | 5376x2688 | photosphere | 2 | 0.0000 | 2 | 0 |
| cuenca | 5760x2880 | photosphere | 1 | 0.0000 | 1 | 0 |
| cuenca | 6912x3456 | photosphere | 18 | 0.0000 | 18 | 0 |
| cuenca | 7776x3888 | photosphere | 1 | 0.0000 | 1 | 0 |
| cuenca | 8704x4352 | photosphere | 1 | 0.0000 | 1 | 0 |
| juneau-ak | 4096x2048 | numeric | 235 | 0.0000 | 235 | 0 |
| juneau-ak | 5760x2880 | numeric | 253 | 0.0000 | 253 | 0 |
| juneau-ak | 8000x4000 | numeric | 4847 | 0.1740 | 4334 | 4847 |
| kaohsiung-tw | 13312x6656 | photosphere | 1 | 0.0000 | 1 | 0 |
| kaohsiung-tw | 5660x2830 | photosphere | 1 | 0.0000 | 1 | 0 |
| kaohsiung-tw | 6000x3000 | photosphere | 1 | 0.0000 | 1 | 0 |
| kaohsiung-tw | 6080x3040 | photosphere | 3 | 0.0000 | 3 | 0 |
| kaohsiung-tw | 6720x3360 | photosphere | 5 | 0.0000 | 5 | 0 |
| kaohsiung-tw | 7680x3840 | photosphere | 1 | 0.0000 | 1 | 0 |
| kaohsiung-tw | 8192x4096 | photosphere | 1 | 0.0000 | 1 | 0 |
| kaohsiung-tw | 8704x4352 | photosphere | 2 | 0.0000 | 2 | 0 |
| kaysville-ut | 5376x2688 | numeric | 299 | 0.0000 | 299 | 0 |
| kaysville-ut | 8192x4096 | numeric | 1 | 0.1816 | 1 | 1 |
| keelung-tw | 5376x2688 | photosphere | 1 | 0.0000 | 1 | 0 |
| knox-oh | 7680x3840 | photosphere | 124 | 0.0000 | 124 | 0 |
| laurens-ia | 5760x2880 | numeric | 4656 | 0.0000 | 4656 | 0 |
| mendota-il | 6720x3360 | photosphere | 3 | 0.0000 | 3 | 0 |
| new-taipei-tw | 13824x3680 | photosphere | 1 | 0.0000 | 1 | 0 |
| new-taipei-tw | 7680x3840 | photosphere | 1 | 0.0000 | 1 | 0 |
| pittsburgh-pa | 12080x6040 | photosphere | 1 | 0.0000 | 1 | 0 |
| pittsburgh-pa | 4096x2048 | photosphere | 17 | 0.0000 | 17 | 0 |
| pittsburgh-pa | 6720x3360 | photosphere | 1 | 0.0000 | 1 | 0 |
| richmond-va | 11000x5500 | numeric | 5075 | 0.1875 | 3741 | 4132 |
| richmond-va | 12288x6144 | numeric | 904 | 0.0000 | 904 | 0 |
| richmond-va | 4096x2048 | numeric | 2539 | 0.0000 | 2538 | 0 |
| richmond-va | 5376x2688 | numeric | 69 | 0.0000 | 69 | 0 |
| richmond-va | 5760x2880 | numeric | 815 | 0.0000 | 815 | 0 |
| richmond-va | 5888x2944 | numeric | 5 | 0.0000 | 5 | 0 |
| sacramento-ca | 4096x2048 | numeric | 9 | 0.0000 | 9 | 0 |
| sacramento-ca | 5376x2688 | numeric | 2 | 0.0000 | 2 | 0 |
| sacramento-ca | 7680x3840 | numeric | 819 | 0.0000 | 819 | 0 |
| seattle-wa | 3840x1920 | photosphere | 31 | 0.0000 | 31 | 0 |
| seattle-wa | 5376x2688 | photosphere | 5 | 0.0000 | 5 | 0 |
| seattle-wa | 5472x2736 | photosphere | 1 | 0.0000 | 1 | 0 |
| seattle-wa | 5660x2830 | photosphere | 1 | 0.0000 | 1 | 0 |
| seattle-wa | 5760x2880 | photosphere | 3 | 0.0000 | 3 | 0 |
| seattle-wa | 7168x3584 | photosphere | 1 | 0.0000 | 1 | 0 |
| seattle-wa | 7200x3600 | photosphere | 1 | 0.0000 | 1 | 0 |
| seattle-wa | 7744x3872 | photosphere | 1 | 0.0000 | 1 | 0 |
| seattle-wa | 8704x4352 | photosphere | 1 | 0.0000 | 1 | 0 |
| spgg | 11000x5500 | photosphere | 112 | 0.0000 | 112 | 0 |
| st-louis-mo | 5760x2880 | photosphere | 21 | 0.0000 | 21 | 0 |
| taichung-tw | 5376x2688 | photosphere | 2 | 0.0000 | 2 | 0 |
| taichung-tw | 8704x4352 | photosphere | 2 | 0.0000 | 2 | 0 |
| taipei | 10240x2564 | photosphere | 1 | 0.0000 | 1 | 0 |
| taipei | 10240x5120 | photosphere | 1 | 0.0000 | 1 | 0 |
| taipei | 2508x1254 | photosphere | 2 | 0.0000 | 2 | 0 |
| taipei | 3840x1920 | photosphere | 33 | 0.0000 | 33 | 0 |
| taipei | 5376x2688 | photosphere | 7 | 0.0000 | 7 | 0 |
| taipei | 5656x2828 | photosphere | 1 | 0.0000 | 1 | 0 |
| taipei | 6656x3328 | photosphere | 1 | 0.0000 | 1 | 0 |
| taipei | 6720x3360 | photosphere | 1 | 0.0000 | 1 | 0 |
| taipei | 8192x4096 | photosphere | 10 | 0.0000 | 10 | 0 |
| walla-walla-wa | 8704x4352 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 10240x3716 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 10240x5120 | photosphere | 3 | 0.0000 | 3 | 0 |
| zurich | 11264x5632 | photosphere | 5 | 0.0000 | 5 | 0 |
| zurich | 12160x6080 | photosphere | 11 | 0.0000 | 11 | 0 |
| zurich | 12800x3366 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 13312x6656 | photosphere | 2 | 0.0000 | 2 | 0 |
| zurich | 14400x7200 | photosphere | 4 | 0.0000 | 4 | 0 |
| zurich | 16384x8192 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 4000x2000 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 4528x1639 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 5376x2688 | photosphere | 7 | 0.0000 | 7 | 0 |
| zurich | 5472x2736 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 5638x2819 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 5656x2828 | photosphere | 4 | 0.0000 | 4 | 0 |
| zurich | 5660x2830 | photosphere | 2 | 0.0000 | 2 | 0 |
| zurich | 6000x3000 | photosphere | 6 | 0.0000 | 6 | 0 |
| zurich | 6080x3040 | photosphere | 2 | 0.0000 | 2 | 0 |
| zurich | 6325x3162 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 6600x3300 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 6720x3360 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 6912x3456 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 7144x3572 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 7200x3600 | photosphere | 2 | 0.0000 | 2 | 0 |
| zurich | 7296x3648 | photosphere | 16 | 0.0000 | 16 | 0 |
| zurich | 7680x3840 | photosphere | 9 | 0.0000 | 9 | 0 |
| zurich | 7744x3872 | photosphere | 1 | 0.0000 | 1 | 0 |
| zurich | 8000x4000 | photosphere | 3 | 0.0720 | 2 | 2 |
| zurich | 8192x4096 | photosphere | 3 | 0.0000 | 3 | 0 |
| zurich | 8704x4352 | photosphere | 4 | 0.0000 | 4 | 0 |
| zurich | 9728x4864 | photosphere | 1 | 0.0000 | 1 | 0 |
