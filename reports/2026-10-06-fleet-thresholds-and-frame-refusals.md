# Fleet measurements: the `images-no-success` thresholds, frame-disagreement refusals, and DC's write-offs

2026-10-06. For [#178](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/178) (the
10-raise floor `IMAGE_NO_SUCCESS_MIN_RAISED` and the 60 s mean-raise gate
`IMAGE_NO_SUCCESS_MIN_MEAN_RAISE_SECONDS`), [#185](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/185)
Part 1 (how often the image phase refuses a pano for a frame disagreement),
[#184](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/184) (washington-dc's `downloaded=0`
rows) and, partly, [#166](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/166) (a GSV entry
in `MAX_CONSECUTIVE_PERMANENT_FAILURES`).

## The question

[#174](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/174) made a night with no answered image
attempt a run condition that fails the night. Its two thresholds were merged without a fleet measurement:
the floor was checked on richmond-va and bayonne-fr only, and the gate is the reviewer's figure. The worry
in #178 is that a mature GSV city whose only candidates are perennial raisers alarms every night for
nothing. [#156](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/156) (zoom from photometa)
went live in the same 2026-10-05 deploy (c9ff03c) and changes what a raise is, so #178 asks for the
measurement on that code.

## Method

Read-only, from the production store's own files, on 2026-10-06 before that evening's run:

* every city's `log.csv` (rows from 2026-09-01, the venv box's first day), every city's `scrape.log`
  (one file per city; none had rotated), the queue's `scrape_queue.log`, and washington-dc's
  `pano_id_log.csv`. Pulled with `reports/scripts/fleet_logs_pull.sh`, packed by
  `reports/scripts/fleet_thresholds.py pack`, and committed as the extracts listed under "Data".
* The pack step redacts Mapillary tokens. One was still present in richmond-va's `scrape.log` on the store
  (the 2026-09-01 leak described in [#100](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/100)).
  None reaches the committed files, and a test asserts it.
* `fleet_thresholds.py analyze` reduces the extracts. It makes no requests.

**scrape.log has no timestamps**, so a raise line cannot be dated directly. Each finished image phase writes
one `IMAGEDOWNLOAD: Final result: Completed X of Y (s success, f fallback success, F failed, K skipped)` line,
and the same run writes one `log.csv` row whose fields 7-11 hold the same five counts. The reducer pairs the
two sequences from the end backwards and checks every pair field by field. **All 2,125 image runs in 56
cities paired, with no mismatch** (`alignment` in the JSON). Each run's night is its local date twelve hours
earlier, so a pass-2 run at 01:00 belongs to the night before.

**`answered` comes from the ledger arithmetic**, because log.csv's field 9 is seeded from earlier `downloaded=0`
rows. A run's own permanent verdicts are field 9 minus its raises and frame refusals, minus the previous
run's field 9 net of the same. A run is marked unknown (`n/a`), never zero, when the city's previous row is
not the previous image run: the first run in the window, or a run after a crash or a schema-drift stop. That
applies to 62 of 2,125 runs, and only one of them had a raise (richmond-va, 2026-08-31).

**No per-raise duration is recorded anywhere durable.** That is #178's step 1, which has not landed. The
duration bound used here is the image phase's whole duration (field 12, rounded to whole minutes, so
`(minutes + 0.5) * 60 / raises`) on runs with nothing answered. It is an upper bound, because the phase also
spends time on the candidate loop. On a run that also downloaded panos it is meaningless, so the threshold
sweep only evaluates runs where nothing was answered.

## Scope: what was measured

Nights 2026-08-31 to 2026-10-05: 36 nights, 2,125 image runs, 56 cities. **Only one night, 2026-10-05,
ran #156's code** (57 image runs, 2,066 successes). #178 asks for about a week. The pre-deploy nights are
still informative for the floor, because the perennial raisers are the same panos before and after (below),
but the post-deploy column is a single night.

## A. The `images-no-success` thresholds (#178)

Over 36 nights, **only three cities ever raised in the image phase**:

| City | Image runs | Runs with raises | Max raises in a run | Runs with raises and 0 answered | Max raises with 0 answered | Distinct raising panos | Upper bound on mean raise, 0-answered runs (s) |
|---|---|---|---|---|---|---|---|
| chicago-il | 54 | 8 | 5 | 2 | 5 | 5 | 6-6 |
| richmond-va | 43 | 1 | 1 | 0 | n/a | 1 | n/a |
| st-louis-mo | 54 | 35 | 16 | 1 | 16 | 16 | 2-2 |

* **All 21 GSV perennial raisers are third-party photospheres.** Every one has a `CAoS...` id: 16 in
  st-louis-mo and 5 in chicago-il. They are the same panos every night. Before #156 they raised `cannot
  identify image file` (the tile body is not an image). Since #156 they raise `cbk probe answered 400, not
  200`. Neither error is ledgered, so they come back as candidates every night.
* **st-louis-mo's nightly raise count is 16 on any night that reaches the end of its list.** On 2026-10-04
  (pre-deploy) it raised 16 and answered 0. Under the current floor of 10, that night fires the count arm.
  It did not alarm only because the condition was not deployed yet. On 2026-10-05, the first night on the
  new code, the same 16 raised again, but 513 new panos were answered, so nothing fired
  (`condition_lines_observed` = 0, and the queue's only condition that night was washington-dc's
  `pano-schema-drift`). **The first st-louis-mo night with no new panos will fail the night under the current
  floor.** Nothing in the post-deploy data suggests otherwise: the 16 still raise.
* chicago-il's 5 never reach 10.

Floor sweep, count arm, runs with 0 answered:

| Era | Floor (raises) | Runs that fire | City-nights | Cities |
|---|---|---|---|---|
| pre-deploy | 5 | 3 | 3 | chicago-il, st-louis-mo |
| pre-deploy | 10 | 1 | 1 | st-louis-mo |
| pre-deploy | 15 | 1 | 1 | st-louis-mo |
| pre-deploy | 16 | 1 | 1 | st-louis-mo |
| pre-deploy | 17 | 0 | 0 | - |
| pre-deploy | 20 | 0 | 0 | - |
| pre-deploy | 25 | 0 | 0 | - |
| pre-deploy | 30 | 0 | 0 | - |
| pre-deploy | 50 | 0 | 0 | - |
| post-deploy | 5 | 0 | 0 | - |
| post-deploy | 10 | 0 | 0 | - |
| post-deploy | 15 | 0 | 0 | - |
| post-deploy | 16 | 0 | 0 | - |
| post-deploy | 17 | 0 | 0 | - |
| post-deploy | 20 | 0 | 0 | - |
| post-deploy | 25 | 0 | 0 | - |
| post-deploy | 30 | 0 | 0 | - |
| post-deploy | 50 | 0 | 0 | - |

* **The raises are fast, not slow.** On the 0-answered runs, a raise took at most about 2 s (st-louis-mo,
  16 in a 0-minute phase) to 6 s (chicago-il). That is an upper bound. The "slow perennial raiser" the 60 s
  gate was sized against does not appear in this fleet.

Gate sweep, budget arm (needs 0 answered, a `max-runtime` stop and at least one raise):

| Era | Gate (s) | Runs that could fire | City-nights | Cities |
|---|---|---|---|---|
| pre-deploy | 30 | 0 | 0 | - |
| pre-deploy | 60 | 0 | 0 | - |
| pre-deploy | 90 | 0 | 0 | - |
| pre-deploy | 120 | 0 | 0 | - |
| pre-deploy | 180 | 0 | 0 | - |
| post-deploy | 30 | 0 | 0 | - |
| post-deploy | 60 | 0 | 0 | - |
| post-deploy | 90 | 0 | 0 | - |
| post-deploy | 120 | 0 | 0 | - |
| post-deploy | 180 | 0 | 0 | - |

* **The budget arm never could have fired** at any gate from 30 s to 180 s. Every 0-answered run with raises
  finished its list (stop `none`). The budget arm is therefore driven entirely by the blackhole case it was
  written for (about 210 s per raise). The 60 s gate is about 10 times the measured upper bound of a real
  perennial raise and under a third of the blackhole, so the data leaves it where it is.

Every run with a raise:

| City | Night | Era | Raised | Answered | Successes | Image min | Stop | Mean raise upper bound (s) | Raise kinds |
|---|---|---|---|---|---|---|---|---|---|
| chicago-il | 2026-09-29 | pre-deploy | 1 | 983 | 983 | 120 | max-runtime | 7230 | cannot identify image file x1 |
| chicago-il | 2026-09-30 | pre-deploy | 1 | 869 | 869 | 105 | none | 6330 | cannot identify image file x1 |
| chicago-il | 2026-10-01 | pre-deploy | 5 | 96 | 96 | 12 | none | 150 | cannot identify image file x5 |
| chicago-il | 2026-10-01 | pre-deploy | 5 | 0 | 0 | 0 | none | 6 | cannot identify image file x5 |
| chicago-il | 2026-10-02 | pre-deploy | 5 | 449 | 449 | 54 | none | 654 | cannot identify image file x5 |
| chicago-il | 2026-10-03 | pre-deploy | 5 | 0 | 0 | 0 | none | 6 | cannot identify image file x5 |
| chicago-il | 2026-10-04 | pre-deploy | 5 | 1 | 1 | 0 | none | 6 | cannot identify image file x5 |
| chicago-il | 2026-10-05 | post-deploy | 5 | 6 | 6 | 1 | none | 18 | cbk probe answered 400, not 200 x5 |
| richmond-va | 2026-08-31 | pre-deploy | 1 | n/a | 0 | 0 | none | 30 | N Client Error: Bad Request for url: https://graph.mapillary.com/N?fields=thumb_ x1 |
| st-louis-mo | 2026-09-12 | pre-deploy | 1 | 45 | 45 | 6 | max-runtime | 390 | cannot identify image file x1 |
| st-louis-mo | 2026-09-14 | pre-deploy | 2 | 46 | 46 | 6 | max-runtime | 195 | cannot identify image file x2 |
| st-louis-mo | 2026-09-15 | pre-deploy | 1 | 46 | 46 | 6 | max-runtime | 390 | cannot identify image file x1 |
| st-louis-mo | 2026-09-16 | pre-deploy | 1 | 48 | 48 | 6 | max-runtime | 390 | cannot identify image file x1 |
| st-louis-mo | 2026-09-17 | pre-deploy | 3 | 89 | 89 | 11 | max-runtime | 230 | cannot identify image file x3 |
| st-louis-mo | 2026-09-18 | pre-deploy | 1 | 49 | 49 | 6 | max-runtime | 390 | cannot identify image file x1 |
| st-louis-mo | 2026-09-18 | pre-deploy | 5 | 110 | 110 | 13 | max-runtime | 162 | cannot identify image file x5 |
| st-louis-mo | 2026-09-19 | pre-deploy | 1 | 51 | 51 | 6 | max-runtime | 390 | cannot identify image file x1 |
| st-louis-mo | 2026-09-19 | pre-deploy | 2 | 150 | 150 | 18 | max-runtime | 555 | cannot identify image file x2 |
| st-louis-mo | 2026-09-20 | pre-deploy | 8 | 275 | 275 | 33 | max-runtime | 251 | cannot identify image file x8 |
| st-louis-mo | 2026-09-21 | pre-deploy | 1 | 49 | 49 | 6 | max-runtime | 390 | cannot identify image file x1 |
| st-louis-mo | 2026-09-21 | pre-deploy | 14 | 299 | 299 | 37 | max-runtime | 161 | cannot identify image file x14 |
| st-louis-mo | 2026-09-22 | pre-deploy | 8 | 50 | 50 | 6 | max-runtime | 49 | cannot identify image file x8 |
| st-louis-mo | 2026-09-22 | pre-deploy | 15 | 75 | 75 | 10 | none | 42 | cannot identify image file x15 |
| st-louis-mo | 2026-09-23 | pre-deploy | 2 | 48 | 48 | 6 | max-runtime | 195 | cannot identify image file x2 |
| st-louis-mo | 2026-09-23 | pre-deploy | 7 | 349 | 349 | 42 | max-runtime | 364 | cannot identify image file x7 |
| st-louis-mo | 2026-09-24 | pre-deploy | 7 | 273 | 273 | 32 | max-runtime | 279 | cannot identify image file x7 |
| st-louis-mo | 2026-09-25 | pre-deploy | 1 | 48 | 48 | 6 | max-runtime | 390 | cannot identify image file x1 |
| st-louis-mo | 2026-09-25 | pre-deploy | 2 | 295 | 295 | 36 | max-runtime | 1095 | cannot identify image file x2 |
| st-louis-mo | 2026-09-26 | pre-deploy | 1 | 51 | 51 | 6 | max-runtime | 390 | cannot identify image file x1 |
| st-louis-mo | 2026-09-26 | pre-deploy | 5 | 308 | 308 | 37 | max-runtime | 450 | cannot identify image file x5 |
| st-louis-mo | 2026-09-27 | pre-deploy | 4 | 346 | 346 | 42 | max-runtime | 638 | cannot identify image file x4 |
| st-louis-mo | 2026-09-28 | pre-deploy | 2 | 50 | 50 | 6 | max-runtime | 195 | cannot identify image file x2 |
| st-louis-mo | 2026-09-28 | pre-deploy | 3 | 363 | 363 | 43 | max-runtime | 870 | cannot identify image file x3 |
| st-louis-mo | 2026-09-29 | pre-deploy | 4 | 511 | 511 | 63 | max-runtime | 952 | cannot identify image file x4 |
| st-louis-mo | 2026-09-30 | pre-deploy | 1 | 49 | 49 | 6 | max-runtime | 390 | cannot identify image file x1 |
| st-louis-mo | 2026-09-30 | pre-deploy | 16 | 1094 | 1094 | 134 | max-runtime | 504 | cannot identify image file x16 |
| st-louis-mo | 2026-10-01 | pre-deploy | 3 | 45 | 45 | 6 | max-runtime | 130 | cannot identify image file x3 |
| st-louis-mo | 2026-10-01 | pre-deploy | 16 | 264 | 264 | 34 | none | 129 | cannot identify image file x16 |
| st-louis-mo | 2026-10-02 | pre-deploy | 3 | 48 | 48 | 6 | max-runtime | 130 | cannot identify image file x3 |
| st-louis-mo | 2026-10-02 | pre-deploy | 16 | 197 | 197 | 24 | none | 92 | cannot identify image file x16 |
| st-louis-mo | 2026-10-03 | pre-deploy | 16 | 9 | 9 | 1 | none | 6 | cannot identify image file x16 |
| st-louis-mo | 2026-10-04 | pre-deploy | 16 | 0 | 0 | 0 | none | 2 | cannot identify image file x16 |
| st-louis-mo | 2026-10-05 | post-deploy | 1 | 40 | 40 | 6 | max-runtime | 390 | cbk probe answered 400, not 200 x1 |
| st-louis-mo | 2026-10-05 | post-deploy | 16 | 513 | 513 | 78 | none | 294 | cbk probe answered 400, not 200 x16 |

### Recommendation for #178

1. **Floor: raise `IMAGE_NO_SUCCESS_MIN_RAISED` from 10 to 25.** The observed maximum with nothing answered is
   16 (st-louis-mo, every quiet night). 17 is the smallest value that does not fire on the measured fleet.
   25 adds margin for a few more photospheres in st-louis-mo's list. A real outage on a mature city still
   reaches 25 only if the city has 25 candidates, so for small mature cities the count arm is weaker at 25
   than at 10. With 10, st-louis-mo would alarm every quiet night. That trade-off is Jon's decision.
2. **Better alternative, which can sit beside 1:** stop counting these 21 as raises. A `cbk probe answered
   400` for a `CAoS...` id is Google answering that it serves no tiles for this id. Under #156 that is a
   verdict about the pano, not about the network. Making it a permanent `downloaded=0` (or an "answered"
   outcome, as a frame refusal already is) would remove the whole perennial population. The floor could then
   stay at 10. This needs its own issue and tests. It is a downloader verdict change, so it is not proposed
   inside this PR.
3. **Gate: keep 60 s.** No measured run comes near it, in either direction.
4. **Re-measure after #178's step 1 lands.** With durations on the `ERROR` line, a week of post-deploy
   nights can replace the upper bounds here with real per-raise durations.

## B. Frame-disagreement refusals (#185 Part 1)

`frame disagreement` lines across all 56 cities' `scrape.log`: **0**. Distinct panos: 0. App and Google
frames: none to report. The photometa-arm warnings beside it (`photometa reports ... tiles, not 512`, `tile
... past a ... grid` on the probe arm, `photometa unavailable`, refused, or given up) are also **0** in every
city. The exposure is one post-deploy night with 2,066 successful image downloads, all through the photometa
arm, and no refusal (`post_deploy_frame_refused` = 0). This matches the 0.0% dims drift in
`reports/2026-08-09-photometa-census.md` and `reports/2026-09-06-photometa-decay.md`.

### Recommendation for #185

* **Part 2 is not warranted now.** At a measured rate of 0, keeping a larger original has nothing to keep.
* **Part 1 is still worth landing.** It is cheap, and it is the only thing that would show the rate leaving
  0. Today a refusal is visible only to a `grep`. Revisit Part 2 when Part 1's count is nonzero on any night.

## C. washington-dc's `downloaded=0` rows (#184)

From washington-dc's `pano_id_log.csv` (78,301 rows, all three-field):

| fetched_at date | rows | `downloaded=0` | longest consecutive `downloaded=0` |
|---|---|---|---|
| 2026-09-24 | 470 | 469 | 280 |
| 2026-09-25 | 880 | 880 | 880 |

* **1,349 `downloaded=0` rows**, all stamped on or after 2026-09-10: 469 on 2026-09-24 and 880 on
  2026-09-25, the first night washington-dc was in the queue (pass 1 and pass 2). No other date has a 0 row.
  The rest of the ledger is 76,951 blank-stamped `skipped` rows (files already on the store from the
  pre-2024 `dc/` scrape) and 1 success stamped 2026-09-24.
* **scrape.log cannot tell which of these were dimensionless write-offs.** A no-dims write-off and a
  black-probe write-off both return `None` from `resolve_frame` and log nothing. DC's `scrape.log` has 0
  raise lines and 0 black-stitch lines. The circumstantial case that all 1,349 are dimensionless is strong:
  `/adminapi/panos` omits width/height on 78,300 of 78,301 DC records, and the night's one success is
  consistent with the one record that has them. But that comes from the live API, not from this log, so the
  count here is "1,349, cause not recorded".

## D. Consecutive `downloaded=0` streaks (#166), partial

The ask was the longest natural streak of consecutive `downloaded=0` rows within one night, for every GSV
city's stamped ledger rows, to size a GSV entry in `MAX_CONSECUTIVE_PERMANENT_FAILURES`. **Only washington-dc
was measured.** The pull of the other cities' ledgers was not run in this session, so it is still open.

washington-dc's streaks are in the table under C: **280** on 2026-09-24 (broken once by its single success)
and **880** on 2026-09-25. **These are not natural streaks.** They are the dimensionless write-off that D8
now stops before either phase runs. They are the failure a GSV breaker entry would exist to catch, so they
show that any limit up to 280 would have tripped on that night. They are not the worst *legitimate* streak
that the limit must sit above. That number still needs the fleet ledgers.

## Wrong turns

* **I first planned to pull only `scrape.log` and grep it per night.** It has no timestamps, so per-night
  attribution only exists through the log.csv pairing. Pairing by count equality was chosen because it can
  fail loudly, and it did not fail.
* **I expected `scrape.log` to be up to 40 MB per city** (10 MB x 3 rotations) and planned to filter on the
  host. Every city's file is under 0.5 MB and none has rotated, because nearly every line is DEBUG noise
  from short nights. The whole set was pulled.
* **I read st-louis-mo's 2026-10-05 pass-2 "mean raise upper bound" of 294 s** as a slow raise before I saw
  the 513 successes in the same phase. The bound is only meaningful on 0-answered runs, which is why the
  sweep evaluates only those, and the per-run table prints it for context only.
* **The floor sweep's 0 post-deploy hits are not evidence that 10 is safe.** The only post-deploy night
  happened to have new panos in both raising cities. The pre-deploy rows, with the same 21 panos, carry the
  floor argument.

## Data

* `reports/data/2026-10-06-fleet-log-csv.csv.gz`: `city,<log.csv row>` for every row from 2026-09-01.
* `reports/data/2026-10-06-fleet-scrape-log.tsv.gz`: `city<TAB><line>` for every scrape.log line, with
  tokens redacted.
* `reports/data/2026-10-06-fleet-scrape-queue-log.txt.gz`: the queue's log, with tokens redacted.
* `reports/data/2026-10-06-dc-ledger-downloaded-0.csv.gz`: washington-dc's 1,349 `downloaded=0` rows.
* `reports/data/2026-10-06-dc-ledger-census.json`: DC's ledger counts by verdict and date, and the streaks.
* `reports/data/2026-10-06-fleet-thresholds.json`: everything above, derived from the files listed here.
* `tests/test_fleet_thresholds_report.py`: synthetic tests of the reducer, the JSON re-derived from the
  extracts, and every table line here checked against the script's output.

## Open questions

* What do the post-deploy nights after 2026-10-05 show? The first quiet st-louis-mo night is the test of
  recommendation 1.
* Should a `cbk probe answered 400` on a `CAoS...` id become a permanent verdict? That is recommendation 2,
  and it needs its own issue.
* #166 D for the GSV fleet: not measured here.
