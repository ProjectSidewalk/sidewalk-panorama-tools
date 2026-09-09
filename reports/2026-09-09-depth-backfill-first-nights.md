# The depth backfill's first three nights: a fleet average hid a 463-night tail

**2026-09-09** · Post-deploy measurement for the depth rollout
([#43](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/43))

> **Reproduce:** `pytest tests/test_depth_backfill_report.py` offline; the reduction is
> ```bash
> python reports/scripts/depth_backfill_progress.py \
>     --csv reports/data/2026-09-09-depth-backfill-progress.csv \
>     --write reports/data/2026-09-09-depth-backfill-progress.json
> ```
> and the snapshot itself is `--store <pano store root> --csv-out <file>`, run against the store on 2026-09-09
> after the third night's queue had finished. Since 2026-09-09 `log.csv` carries the corpus size and
> `log_analyzer/analyze.py` reports these figures nightly ([#124](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/124));
> this is the snapshot they were built to replace.

## The question, and why it mattered

Depth went live for the whole fleet on 2026-09-06 (`scrape_queue.py --max-runtime 690 --city-max-runtime 12
-- --all-panos --min-depth-runtime 6`), sized on a figure from the
[2026-09-06 decay report](2026-09-06-photometa-decay.md)'s deployment comment: at a 12-minute slot, "~47
nights" for the 1.43 M-pano corpus, and given a measured retirement rate of ~0.3% of panos per 28 days,
"a trade we can simply accept rather than engineer around". Three nights later the question was simply
whether that was true — and nothing could answer it: `log.csv` recorded how many panos each run had resolved
but never out of how many, and the analyzer had no depth rule at all.

## Method

Every city's `scrape.log` ends each depth phase with one `DEPTHDOWNLOAD: Final result` line carrying the
corpus size (`Completed N of TOTAL`), the cumulative resolved count, that run's request count, and why it
stopped. `depth_backfill_progress.py --store` takes the last such line per city; every city had exactly three
(west-chester-pa four, counting the 2026-09-06 canary). The reduction divides each city's unresolved count by
its last run's requests, which is the rate the fleet actually ran at, and compares it with two regimes that
were not yet deployed:

* **floor, fixed slot** — a 12-minute slot with the pacer at its 0.25 s floor from the first request: the gap
  is drawn `uniform(0.25, 0.5)` and measured between request starts, so the 0.077 s median request is absorbed
  and a slot yields **1,920** requests;
* **floor, whole window** — the same rate over the whole 690-minute window (**110,400** requests a night),
  shared among whichever cities still have work. A fleet figure, since the window is one resource.

Fleet-wide push-back was checked separately: `grep -l "backing off\|refusing requests\|block latch"
*/scrape.log` over all 52 cities.

## What came out

**Zero push-back, latch or breaker events in three nights.** And every city with a backlog stopped on
`max-runtime` after a median of **586** requests per full slot, at 1.22 s each — because every city is a
fresh process that opens at 1.0 s and needs 1,400 clean requests to reach the floor, so no slot ever got there.

| fleet, 2026-09-09 | |
|---|---:|
| cities in the manifest | 52 |
| with GSV panos | 51 |
| already complete | **13** |
| with work left | 38 |
| GSV panos eligible for depth | **1,433,938** |
| resolved (saved + unavailable) | **76,251** (5.3%) |
| unresolved | **1,357,687** |
| depth requests on the third night | **22,237** |

That third night the queue used **477 of its 690 minutes**: the 13 complete cities exit in seconds and give
nothing back, and the 38 working ones are each capped at 12 minutes whether they have 400 panos left or
270,000. So the window was already a third unused, and the share grows every night a small city finishes.

| nights to finish | |
|---|---:|
| measured rate, **fleet average** (the 47-night figure's method) | **61** |
| measured rate, **longest city** | **463** |
| floor + fixed 12-minute slot, longest city | **140** |
| floor + whole window shared (extra passes) | **12** |

The five cities that set the fleet's finish date:

| city | unresolved | nights, measured | nights, floor + slot |
|---|---:|---:|---:|
| chicago-il | 269,581 | 463 | 140 |
| kaohsiung-tw | 208,137 | 350 | 108 |
| seattle-wa | 181,927 | 307 | 95 |
| columbia-sc | 149,546 | 261 | 78 |
| vancouver-wa | 140,858 | 240 | 73 |

Two things follow. **The "47 nights" was a fleet average, and it hid the number that matters by an order of
magnitude**: the fleet finishes when its slowest city does, and at 590 requests a night chicago-il alone is
1.3 years. And **the ramp, not the floor, set the rate** — exactly the case `docs/depth.md` had reserved for
persisting the pacer's interval across runs.

## What changed in the code

Three PRs, each one idea:

1. [#124](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/124) — `log.csv` field 19 (the corpus
   size), an analyzer intake that no longer infers the file's shape, `depth_progress()` with per-city ETA, a
   stalled-backfill warning, and a fleet block. Measured on the way: pandas cannot parse the transition file a
   19th column leaves behind under either engine.
2. [#125](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/125) — the pacer keeps the speed it
   has *earned* across runs (never a back-off), so a slot runs at the floor from its first request: the
   "floor + fixed slot" column.
3. This PR — the queue's extra passes: once every city has had its slot, the cities that ran out of budget
   are run again with the larger of a slot and an equal share of what the window has left, until a slot no
   longer fits: the "floor + whole window" row.

## Where the data lives, and the tests that pin it

* `reports/data/2026-09-09-depth-backfill-progress.csv` — one row per city, straight from the log lines.
* `reports/data/2026-09-09-depth-backfill-progress.json` — the reduction above, every figure this page quotes.
* `tests/test_depth_backfill_report.py` — the reducer against synthetic rows (a fleet average versus a longest
  city; undefined-is-None; the floor regimes from their constants), the committed JSON being exactly what the
  script produces from the committed CSV, and every number in this page being in the artifact.

## Wrong turns

* **The fleet average.** The 47-night figure divided the corpus by the fleet's nightly total. That is the
  right arithmetic for a fleet that shares its window and the wrong one for a fleet of fixed slots, where the
  finish date is the largest city's — a 10× error in the direction that made the slot look acceptable.
* **"A trade we can simply accept."** That conclusion rested on the average. At 0.3% retirement per 28 days a
  47-night backfill costs half a percent of the reachable depth; a 463-night one costs about five.
* **Persisting the back-off.** The first design for [#125](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/125)
  persisted the pacer's whole interval. `on_pushback` is fed by every network failure, not only by Google, so
  one DNS blip would have handed the next 51 cities a 30 s gap. Caught in design review; only earned speed
  persists.
* **Fair shares below a slot.** The first design for the extra passes divided the leftover window equally
  with no floor: 213 minutes over 39 cities is 5.5 each, below `--min-depth-runtime`, which zeroes every image
  phase and mails a warning per city. The share is floored at a slot.
* **`read_csv` on the transition file.** Verified before shipping the column rather than after: both pandas
  engines fail on an 18-name header followed by 19-field rows.

## Open questions

* **The floor at volume.** The 0.25 s floor is evidenced by 1,360 requests in the 2026-08-09 census and now
  by ~66,000 across three nights at ~1.2 s. Extra passes at the floor mean up to ~110,000 requests a night from
  one IP; the pacer backs off on the first retry and the block latch stands the fleet down on a refusal, but
  the first full night at that rate is the real canary. Check `grep -l "backing off" */scrape.log` the morning
  after.
* **Corpus growth.** The eligible count rose from 1,433,104 (2026-09-01) to 1,433,938 in eight days — 834 new
  panos, or ~100 a night, negligible against the rate but nonzero: the backfill is a moving target, and a
  city's "complete" is a nightly re-check, which the analyzer's per-night rule already accounts for.
