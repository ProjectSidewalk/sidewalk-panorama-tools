# Calibrating the edge black-band limit on the real pano store (#179 item 4)

**Status: measurement in progress.** This file opens with the dated decision entry, committed and pushed before
any band depth of the run was read. The results, the decision and the docs follow in later commits on the same
branch; nothing below the decision entry is a result yet.

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

### Load limits

1 reader to start, 2 at most; 60 MB/s aggregate, held by pausing readers; a 15-minute opening slice at 1 reader
whose latency is written down first; abort on 30 s continuously in uninterruptible sleep, on 120 s of unpaused
time with no ledger row, stdout line or read, or on the last 100 panos of a source taking over 3 times the
baseline; no retry after an abort; a hard stop at 17:00 Pacific.
