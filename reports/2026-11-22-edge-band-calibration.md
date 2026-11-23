# Calibrating the edge black-band limit on the real pano store (#179 item 4)

**Status: measurement in progress (131,184 of 133,919 frozen ids measured; chicago-il and knox-oh partial; extras
not run).** This file opens with the dated decision entry, committed and pushed before any band depth of the run
was read. The results, the decision and the docs follow in later commits on the same branch; nothing below the
decision entry is a result yet.

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

## Session log (partial; the measurement is not finished)

Times are Pacific, 2026-11-22. Nothing in this section is a band result.

| Session | Start | End | Readers | Panos ledgered | GB read | Outcome |
|---|---|---|---:|---:|---:|---|
| opening | 08:54 | 09:06 | 1 | 3,415 | 33.0 | stopped by its child's `--max-runtime`; no abort; no-writes check passed |
| main1 | 09:07 | 15:12 | 2 | 127,769 | 1,097.8 | **aborted by the 3x rule** on chicago-il (last 100 GSV panos 65.1 s against 3 x 20.94 s); both children continued and terminated, none left alive; no-writes check passed |

**The opening slice** (`sessions/opening.json`, `baseline.json`): bayonne-fr, then the smallest GSV folders.
Per 100 panos of unpaused time: **20.94 s for GSV** (one reader read 38.6 to 51.5 MB/s on the folders over 100
panos) and **8.16 s for provider bytes** (bayonne-fr's 98 panos only, 18.6 MB/s). Reached of frozen: bayonne-fr 98
of 98, goiania-br 9 of 9, beringen-be 49 of 49, newport-ky 257 of 257, clifton-nj 588 of 588, blackhawk-hills-il
609 of 609, la-piedad-old 619 of 619, fort-wayne-in 695 of 695, hackensack-nj 491 of 758; 3,415 of 133,919 in all.

**main1** (`sessions/main1.json`, `abort.json`, `run-main1.log.txt`, `timing.csv`) was started at 09:07 with 2 readers
and the opening's baseline, to sweep until 16:10 and then run the extras until 16:50. At 09:49 the connection
used to wait on it was lost from this side (exit 255); the host and the job were found healthy at 10:11, and
the session ran detached. It read 1,097.8 GB in 6 h 06 min of wall time, **50.0 MB/s on average**, under the 60
MB/s cap, which paused a reader for 5 to 457 s per GSV folder. At 15:12:52 the 3x rule fired on chicago-il: its
last 100 GSV panos took 65.1 s of unpaused time against a limit of 62.8 s. The dispatcher continued and then
terminated both children (chicago-il and knox-oh, exit -15), none was left alive, and no extras ran. Per the
rule, there was no retry that day. Unpaused time per 100 panos over main1's GSV folders ran 18.2 to 29.0 s
(the baseline was 20.94 s at one reader), so two readers were already slower per pano than one.

**Reached of frozen after main1: 131,184 of 133,919.** 60 folders are complete (`done.json`); chicago-il has 772 of
2,785 and knox-oh 1,902 of 2,624 (the sorted-id prefix of each frozen list). 2,735 frozen ids remain, all in those
two folders, and the extras have not run. Not analysed: the decision rules wait for the whole frozen set.

### Deviations so far

- The opening slice ran 12 minutes, not 15: the dispatcher ends each child's `--max-runtime` 3 minutes before its
  stop, so a `--minutes 15` session's child stops at 12. The baseline rests on 3,317 GSV panos and 98 provider panos.
- The plan named `la-piedad-old` and `validation-study` as folders outside the fleet. Both are rows of
  `log_analyzer/cities.csv` that the analyzer monitors (`analyze.py` drops only `#` rows), so by the plan's rule
  ("whatever the analyzer treats as the roster") they are fleet folders here. Three Mapillary folders are not on
  the roster (brussels-be, kaysville-ut, sacramento-ca) and are reported separately. **Provisional.**
- The planner's "about 13 events" for a median stratum assumed a weight near 0.02; `frame.json` gives 0.0103
  (burnaby), hence 25.
