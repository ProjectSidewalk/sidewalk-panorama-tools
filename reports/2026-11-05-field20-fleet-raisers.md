# Field 20 across the fleet: who raises every night, and the steady-raisers rule sized from it

2026-11-05. For [#182](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/182): the log
analyzer rule that reads `log.csv` field 20 (`image_raised`, this run's image attempts that raised, which are
unledgered and so retried the next run).

## The question

Field 20 has been written since the 2026-10-05/06 deploy
([#199](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/199)). A rule shipped with it was
pulled before merge, because its floor would have been a guess: the worry was a population of panos that
raise every night forever (a retired Mapillary image, say), which would make the rule a permanent morning
warning nobody can act on. #182 stayed open until the field had been read across the fleet, with three
questions (Jon's comments of 2026-10-06):

1. **The floor and the window.** How many raises a night, for how many nights, before it is a finding?
2. **The relation to `images-no-success`**, which already fails a night on which at least 10 attempts raised
   and nothing was answered. Should the new rule be gated on "nothing downloaded" as well?
3. **How a night is aggregated** across the queue's passes: max or sum, and what a pass with a blank field 20
   does to it.

## Method

Read-only, from the production store's own files, on 2026-11-05 before that evening's run:

* every city's `log.csv` and `scrape.log`, and the queue's `scrape_queue.log`, pulled with the file list of
  `reports/scripts/fleet_logs_pull.sh` (washington-dc's `pano_id_log.csv` was pulled too and is not used).
* Packed by the 2026-10-06 report's packer, which now takes the pull's prefix:
  `python reports/scripts/fleet_thresholds.py pack <raw-dir> --prefix 2026-11-05- --since 2026-10-06 --no-dc-ledger`.
  The pack redacts Mapillary tokens; richmond-va's `scrape.log` on the store still holds the 2026-09-01 leak
  ([#100](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/100)), and a test asserts no
  committed extract carries one.
* Reduced by `python reports/scripts/field20_raisers.py --write`, which reads nothing but the committed
  extracts and prints every table below.

Definitions (the analyzer's rule uses the same ones; its test runs the real analyzer over the committed
extract and checks it fires on exactly the cities the script says):

* **A night** is the local date twelve hours before a row's own stamp, on the runner's clock. The analyzer
  groups by UTC date instead; the two put every pass of a night on one date except where noted below.
* **A night's field 20 is the max over its rows**, never the sum. The queue's extra passes re-attempt the same
  unledgered set, and a pass that stopped on its budget saw only part of it: chicago-il's first pass on
  2026-11-01 wrote 99 and its second 276. A blank field 20 beside a written one is ignored.
* **A night with no 20-field row, or only blank ones, is unknown**, never 0, and it breaks a streak.
* **Downloads** on a night are fields 7 + 8 summed over its rows. **`images-no-success` nights** are the
  nights `scrape_queue.log` reported that condition for the city.
* **The attribution** reads the raise lines of each city's retained `scrape.log`. That file has no timestamps
  (`fleet_thresholds.py`'s docstring), so it covers whatever the store still holds, not exactly the window;
  frame-disagreement lines are excluded as answers.

## The numbers

Window: 2026-10-11 to 2026-11-04, 25 nights; 57 cities, 57 with a 20-field row.

Runner-clock nights whose rows fall on two UTC dates (the analyzer groups by UTC date): 1.

That night is bayonne-fr's 2026-10-16, an afternoon hand run beside the evening's; both rows read 0, so it
changes nothing below.

### Every city whose field 20 was ever nonzero

| City | nights with field 20 | nights nonzero | first-last nonzero | max | last 10 nights (min-max) | longest nonzero streak | nonzero nights with downloads | `images-no-success` nights | rule fires (nights) |
|---|---|---|---|---|---|---|---|---|---|
| chicago-il | 25 | 25 | 2026-10-11 to 2026-11-04 | 276 | 176-276 | 25 | 23 | 1 | 19 |
| st-louis-mo | 25 | 25 | 2026-10-11 to 2026-11-04 | 16 | 16-16 | 25 | 24 | 1 | 19 |
| bayonne-fr | 25 | 5 | 2026-10-11 to 2026-10-15 | 15 | 0-0 | 5 | 0 | 5 | 0 |
| burnaby | 25 | 13 | 2026-10-23 to 2026-11-04 | 10 | 10-10 | 13 | 3 | 10 | 7 |
| taipei | 25 | 22 | 2026-10-14 to 2026-11-04 | 4 | 4-4 | 22 | 0 | 0 | 16 |
| houston-tx | 25 | 8 | 2026-10-28 to 2026-11-04 | 1 | 0-1 | 8 | 6 | 0 | 0 |
| sao-paulo-brazil | 25 | 6 | 2026-10-30 to 2026-11-04 | 1 | 0-1 | 6 | 5 | 0 | 0 |
| seattle-wa | 25 | 25 | 2026-10-11 to 2026-11-04 | 1 | 1-1 | 25 | 10 | 0 | 0 |

Zero on every night: 49 cities.

Two things stand out. **A set holds its size once it starts**: st-louis-mo, burnaby, taipei and seattle-wa
read one value on every one of the last 10 nights, which is what an unledgered set retried nightly looks like.
And **the largest steady sets are in cities that still download**: chicago-il downloaded on 23 of its 25
raising nights and st-louis-mo on 24 of 25, so `images-no-success` (nothing answered) fired for each on one
night only. Before this rule, no check saw either.

This corrects the 2026-10-06 report's reading of st-louis-mo (that it raises 16 with nothing answered on every
quiet night): over this window it answered something on 24 of 25 nights.

### What the raisers are

| City | raise kind | pano id | lines | distinct panos |
|---|---|---|---|---|
| chicago-il | cbk probe answered 400, not 200 | CAoS | 5690 | 276 |
| chicago-il | cannot identify image file | CAoS | 27 | 5 |
| st-louis-mo | cbk probe answered 400, not 200 | CAoS | 628 | 16 |
| st-louis-mo | cannot identify image file | CAoS | 188 | 16 |
| bayonne-fr | Panoramax hd asset for picture <uuid> redirected (N); not following a redirect o | other | 135 | 15 |
| burnaby | cbk probe answered 400, not 200 | CAoS | 140 | 10 |
| taipei | cbk probe answered 400, not 200 | CAoS | 88 | 4 |
| houston-tx | cbk probe answered 400, not 200 | CAoS | 11 | 1 |
| sao-paulo-brazil | cbk probe answered 400, not 200 | CAoS | 7 | 1 |
| seattle-wa | cbk probe answered 400, not 200 | CAoS | 30 | 1 |

Every raise in the seven GSV cities is on a `CAoS` pano id, a user-contributed photosphere: the legacy cbk
endpoint refuses it (`cbk probe answered 400, not 200`), or answers with something that is not an image.
chicago-il's 276 distinct ids are exactly its nightly field 20, as st-louis-mo's 16, burnaby's 10 and
taipei's 4 are theirs. [#223](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/223)
(merged as [#227](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/227) on 2026-11-05, not yet
deployed) fetches these photospheres from the photo host instead. bayonne-fr's 15 were Panoramax's
HD-asset redirect, fixed by [#220](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/220),
after which its field 20 is 0 on every night.

So over 25 nights **every steady raiser was a real, fixable fault**, and none was the "perennial raiser
nobody can fix" the floor was feared to need to clear.

### Sizing: how many cities each (floor, window) would ever have flagged

| window (nights) | floor 1 | floor 2 | floor 3 | floor 4 | floor 5 | floor 10 | floor 16 | floor 17 |
|---|---|---|---|---|---|---|---|---|
| 3 | 8 | 5 | 5 | 5 | 4 | 4 | 2 | 1 |
| 5 | 8 | 5 | 5 | 5 | 4 | 4 | 2 | 1 |
| 7 | 6 | 4 | 4 | 4 | 3 | 3 | 2 | 1 |
| 10 | 5 | 4 | 4 | 4 | 3 | 3 | 2 | 1 |
| 14 | 4 | 3 | 3 | 3 | 2 | 2 | 2 | 1 |

Floor 3 over 7 nights: fires at some point for burnaby, chicago-il, st-louis-mo, taipei; on the last night for burnaby, chicago-il, st-louis-mo, taipei.

## The decision

* **Floor 3, window 7 nights** (`STEADY_RAISE_MIN_RAISED`, `STEADY_RAISE_NIGHTS` in `log_analyzer/analyze.py`).
  3 is above the one-pano sets (seattle-wa, houston-tx, sao-paulo-brazil: one photosphere each, real but a
  morning warning apiece for one pano) and below every multi-pano set observed (taipei's 4 is the smallest).
  At a 7-night window floors 2 to 4 flag the same number of cities; 3 keeps a margin on both sides. 7 nights
  is Jon's draft window and #182's "steady".
* **The trade of the window:** bayonne-fr's 5-night set never reached 7 nights, but `images-no-success`
  failed its night the first time (on all 5 of its raising nights). A longer window only delays the same
  four cities.
* **Max per night, not sum** (question 3), with a blank pass ignored beside a written one; tests pin
  `(200, 3)` and `(200, blank)` both reading 200, and a night of only blanks breaking the streak.
* **No "nothing downloaded" gate** (question 2). chicago-il is the case #182 describes, and it downloads:
  gating on no downloads would leave the rule blind to the one city it exists for. The two checks split the
  ground: `images-no-success` is the night on which nothing is answered (and fails the night through the
  queue); the steady-raisers WARNING is the city that still downloads, or raises fewer than that condition's
  floor of 10 (taipei: 4 a night, nothing downloaded, never `images-no-success`).
* **WARNING, not CRITICAL**: the panos are retried, not lost.

## What #223's deploy should do

Every raise in this window except bayonne-fr's was a `CAoS` photosphere, so after #227 deploys field 20
should drop to 0 in chicago-il, st-louis-mo, burnaby, taipei, seattle-wa, houston-tx and sao-paulo-brazil,
and the rule should fall silent within a night. **Re-read field 20 after that deploy**: whatever still raises
then is the population the floor was originally meant for, and the floor should be checked against it.

## What changed in the code

* `log_analyzer/analyze.py`: rule 11, `steady_raisers`, the two constants with their reasoning, and the
  `image_raised` column comment. Tests: `tests/test_log_analyzer.py` (`TestRule11ReportsASteadySetOfRaisers`,
  which replaces the old "parsed but not yet judged" pin).
* `reports/scripts/fleet_thresholds.py`: `pack` takes `--prefix`, `--since` and `--no-dc-ledger`, so this
  pull is packed by the same code as the 2026-10-06 one; that report's extracts and test are unchanged.
* `reports/scripts/field20_raisers.py` and `tests/test_field20_raisers_report.py`, new.

## Wrong turns

* The brief this work started from said st-louis-mo and burnaby fire `images-no-success` every night. The
  queue log says burnaby on 10 of its 13 raising nights and st-louis-mo on one; st-louis-mo downloads.
* The first attribution pass folded Panoramax picture UUIDs digit by digit, so bayonne-fr's one error read as
  15 kinds. The script folds UUIDs before `raise_kind`.

## Data

* `reports/data/2026-11-05-fleet-log-csv.csv.gz`, `reports/data/2026-11-05-fleet-scrape-log.tsv.gz`,
  `reports/data/2026-11-05-fleet-scrape-queue-log.txt.gz`: the packed, redacted extracts.
* `reports/data/2026-11-05-field20-fleet-raisers.json`: the reduction.

## Open questions

* The read after #223's deploy (above).
* Whether the WARNING should name a pano of the set. It cannot: `log.csv` carries a count, and `scrape.log`
  is the record of which panos.
