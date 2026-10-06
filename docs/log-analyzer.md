# Log analyzer — `log_analyzer/analyze.py`

Monitors the nightly scrape across every city. It pulls each city's `log.csv` off the pano store over SFTP,
flags the ones that look broken, and reports the [depth backfill](#the-depth-backfill) per city and for the
fleet. This is an ops tool you run from a workstation or a cron box — the scraper neither knows nor needs it,
and it shares no code with the runners. **Pull the repo before running it after a deploy**: the column list
it reads by position moves with the runner's ([#43](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/43)
added field 19, [#182](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/182) field 20), and an older analyzer against newer rows would misplace every count.

It needs only `pandas` plus the `sftp` client binary (`openssh-client`). `pandas` lives in
`requirements-dev.txt`, not `requirements.txt` — nothing the scraper or cropper runs imports it
([#72](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/72)) — so a box running only the
analyzer wants `pip3 install 'pandas>=2.0'`, not the whole dev file. The floor is not cosmetic:
`read_log` parses timestamps with `format="ISO8601"`, which 1.x does not accept, and the inference it
would otherwise fall back to discards every row whose timestamp width differs from the first row's.

## Connection settings

Read from the environment, or the matching flag. Host and base path are **required and have no defaults** — a
wrong default would silently analyze the wrong store.

| Variable | Flag | |
|---|---|---|
| `PS_SFTP_HOST` | `--host` | **required** — host, or an `~/.ssh/config` `Host` alias |
| `PS_SFTP_BASE` | `--base` | **required** — remote directory holding the per-city folders |
| `PS_SFTP_USER` | `--user` | optional — omit when the ssh config supplies it |
| `PS_SFTP_PORT` | `--port` | optional — omit for 22 |
| `PS_SFTP_KEY`  | `--key`  | optional — omit to let ssh choose (ssh config / agent) |
| `PS_ROSTER_HOST` | `--roster-host` | optional — deployments serving `/v3/api/cities` for the roster cross-check below, comma-separated and tried in order; omit for `sidewalk-sea`, then `sidewalk-chicago` |

Setting up an `~/.ssh/config` `Host` alias is the tidiest option: with the user, port, and key declared there,
only `PS_SFTP_HOST` and `PS_SFTP_BASE` are needed.

```bash
export PS_SFTP_HOST=... PS_SFTP_BASE=... PS_SFTP_USER=... PS_SFTP_PORT=... PS_SFTP_KEY=~/.ssh/...

python3 log_analyzer/analyze.py                    # download all city logs, then analyze
python3 log_analyzer/analyze.py --no-download      # re-analyze the local cache
python3 log_analyzer/analyze.py --city seattle-wa  # one city
python3 log_analyzer/analyze.py --stale-days 5     # custom staleness threshold
```

Exit status is `1` when any city has a CRITICAL issue, so cron's mail-on-failure does the alerting — where it runs
on a host that can mail; the production scraper host cannot, and runs its own alarm through
[`cron_notify.py`](ops.md#hearing-about-a-bad-night), which would serve this script the same way. Downloaded
logs are cached in `log_analyzer/logs/` (gitignored).

`log_analyzer/cities.csv` maps `city_id` → display name; each `city_id` must match that city's folder name on
the pano store **exactly**. Add a row when a new city is deployed.

**A city missing from this file is not monitored** — and since this *is* the monitoring layer, such a city has
no alarm at all; the queue at least books its own exit status. That gap used to be silent, and it bit three
times: `newport-ky` was scraped nightly for weeks while sitting outside the analyzer, the 2026-09-17 sweep found
`laurens-ia` with no row and Bayonne's row reading `bayonne` where the app calls itself `bayonne-fr`, and on
2026-09-22 `washington-dc` — re-launched and live — had no row either. Each was found by hand.

**Since [#133](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/133) the report cross-checks
itself.** After the per-city blocks it asks a deployment for `/v3/api/cities`, falling back to the next if one
does not answer (at most three) — every deployment serves the same roster of every city — and names, at
CRITICAL, each city with no row here:

```
🔴  Roster cross-check — checked 55 rows against 39 public + 20 private cities on sidewalk-sea.cs.washington.edu
    🔴 [CRITICAL] not in cities.csv: laurens-ia (https://sidewalk-laurens.cs.washington.edu)
```

The hosts default to `sidewalk-sea.cs.washington.edu`, then `sidewalk-chicago.cs.washington.edu` if Seattle does
not answer. Unlike the SFTP settings these can safely have a default: every deployment on the current release
serves the same roster, so the choice decides only whether the check runs, never what it compares against. The
fallback is a second app stage on the same host, so it covers one deployment being down, not the whole app
host. `--roster-host` or `PS_ROSTER_HOST` replaces the list. Four rules are load-bearing:

- **It is a comparison, not auto-discovery.** This file stays the source of what to monitor; generating it from
  the roster would silently pick up cities nobody decided to watch.
- **It keys on `city_id`**, because the store path is `<base>/<city_id>/log.csv` and the app reads its panos
  under its *own* id. The Bayonne row would have matched on fqdn and still been wrong.
- **Private cities count** ([#143](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/143)).
  Twenty of the fifty-nine deployments are private, `washington-dc` among them.
- **It never fails open.** If no host serves a roster, that is CRITICAL — a check silently skipped every night
  is the failure it exists to prevent. `--no-download` is offline mode and skips it.

To record a deployment that is deliberately **never** monitored, give it a `#` row carrying the marker:

```csv
#zurich-infra3d,"not-monitored: infra3d imagery, nothing here to scrape"
```

The marker is required. Unlike the queue's manifest, this file has no host column to check a `#` row against,
so without an explicit marker a prose comment (`# laurens-ia, bayonne-fr launched 2026-09-11`, which `csv`
splits into a row for `laurens-ia`) would silence the very city it is about.

## Checks

| Level | Condition |
|-------|-----------|
| 🔴 CRITICAL | Log download failed, or the file is missing/empty/unparseable |
| 🔴 CRITICAL | Last log entry is more than `--stale-days` days old (default 3) |
| 🟡 WARNING | `image_fail` growing by ≥20/day (7-night average) — new panos failing. Averaged over calendar **nights**, not rows: the queue's extra passes put more than one row on a night |
| 🟡 WARNING | Zero new images for 30 consecutive **nights**, after a period that had some (regression), **and there was work** ([#163](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/163)): the image-eligible corpus (field 5) grew by at least 3 from the last night before the window to the newest night in it, or field 5 − field 11 on the newest row — eligible panos the image phase never attempted — is at least 3. That difference is a lower bound, loose by the ledger rows no longer in the list: it was −1 in 50 of 56 cities on 2026-09-27 (so 4 real unattempted panos to fire) and −1,460 in chicago-il, where only the growth arm can. A blank field 5 is unknown rather than zero, and fires as before; so is a written 0, because field 5 is the image list's length and an empty pano-list answer writes 0 there (a 0 on the baseline night would otherwise read as the whole corpus arriving, and a month of empty answers as a flat corpus). A mature city with nothing new is silent: three of the 26 warnings on 2026-09-19 were exactly that. Field 11 alone is not evidence — it counts *attempts*, so a starved image phase does not grow it |
| 🟡 WARNING | **A steady set of transient image failures** — field 20 (this run's raised image attempts, [#182](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/182)) at least 10 on each of the last 7 logged nights, taking each night's maximum across its passes, with no image downloaded on any of them. Nothing is ledgered for those panos, so they retry every night; look at the `IMAGEDOWNLOAD` errors in `scrape.log`. A night with field 20 blank (an older row, or an image phase that did not finish) is no evidence and breaks the run. Thresholds unmeasured ([#178](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/178)). See [What `log.csv` could not show before field 20](#what-logcsv-could-not-show-before-field-20) |
| 🟡 WARNING | A recent **image phase** took >3× the historical median, with the median floored at `LONG_RUN_MIN_MEDIAN` minutes before the multiplier. Read from `image_minutes` directly: a mature city's image phase is a ledger read while depth spends the whole slot, so an unfloored median is 0 and the rule could never fire. **What it can see:** inside the [queue](downloader.md#nightly-deployment) a hung image phase is killed at its slot plus `--kill-grace`, which leaves `image_minutes` blank, so this rule never sees it (the ended-early rule below does, at ≥3 of the last 7 runs — a single killed night is below that and goes unreported). It fires on legitimate long phases, such as a city's first scrape (laurens-ia, 83 min, 2026-09-17), and on hand runs without `--max-runtime` |
| 🟡 WARNING | ≥3 of the last 7 runs ended early (blank columns) |
| 🟡 WARNING | Two runs **overlapped**: one started before the previous one's recorded end — two processes racing on one city's ledgers. Same-day runs alone are not reported; the queue's extra passes produce them by design |
| 🟡 WARNING | **Depth backfill stalled**: no depth request on the last 3 calendar nights while panos remain unresolved. The message names the *candidates* rather than asserting a cause, because the row cannot tell them apart: a phase that accounted for panos but made no requests is either out of budget or unable to write the ledger (which returns `(0, 0, skipped, skipped)`, not five zeros), and a phase that accounted for nothing is `--skip-depth`, a block latch, `streetlevel` missing, a crash before the phase, or a fresh city whose image phase spent the budget |
| 🔴 CRITICAL | **Depth phase saved nothing and is writing panos off**: the WARNING below, and the ledger grew by at least half of the failures before the newest run — they are coming back as `unavailable` rows, which are never re-requested. That is upstream drift (a depth payload `streetlevel` can no longer read), and every night it runs costs those panos their depth until the ledger is scrubbed: see [When the depth phase saves nothing](ops.md#when-the-depth-phase-saves-nothing) |
| 🟡 WARNING | **Depth phase saved nothing** on the last 3 nights it made requests (nights with no row, or a stand-down's five zeros, are not counted — they are no evidence either way) despite ≥10 requests, with panos unresolved and the newest requesting run stopping short of its list ([#163](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/163)). The stall rule above counts *requests*, and a failed request is a request, so a phase whose every attempt fails used to read as healthy. The failures are transient: less than half came back as ledger skips, so the panos retry. A city whose last few panos fail every night walks its whole list (field 16 = field 19) and is not reported: that is the ordinary end of a backfill. Once requests stop altogether the night is the stall rule's, so one outage is never reported twice |
| 🟡 WARNING | **The newest GSV corpus size is not believable** — a `0` in field 19, which is what an empty or source-less `/adminapi/panos` answer writes. The backfill is measured against the newest earlier row instead, rather than the city silently dropping out of the report |
| 🟡 WARNING | **Rows that are not runs**, from the last 7 days — a field count that is not one of 18, 19 or 20, i.e. a torn or corrupted write. Every count in such a row is shifted, so it is left out of every figure rather than read as a run (a file holding nothing else is CRITICAL, and says so). Dated by the row's own field 1, which is written first and so survives a tear; a row whose stamp did not survive whole counts as recent, because that is exactly what tonight's tear can look like. "Whole" means the full `YYYY-MM-DD HH:MM:SS`: a date parser accepts a prefix, and would read `2026-09-2` as 2 September |
| 🔵 INFO | **Historical rows that are not runs** — the same, when every such row is older than 7 days: one line with the total and the newest date, recorded rather than alerted ([#163](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/163)). On 2026-09-19, 20 of the morning's 26 warnings were torn rows dated 2022 to 2026-05, which nothing on the current build wrote |

Thresholds are module constants near the top of `analyze.py`. A city whose findings are all INFO prints the
blue icon and counts as OK.

A healthy mature city looks like: `image_success` small or zero most days, stable `image_fail`,
`image_skip ≈ image_total`.

### What `log.csv` could not show before field 20

**A steady set of panos failing the image phase transiently every night goes unreported once it has been
steady for a week.** Field 9 mixes the
permanent failures already ledgered, tonight's permanent ones and tonight's transient ones, so a transient set
of constant size T adds T to every night's figure and the night-to-night change the growing-failures check
reads is about zero. The one exception is onset: the night T first appears, field 9 jumps by T, and the
growing-failures check averages that jump over 7 nights, so a set of T ≥ 140 (7 × its threshold of 20) is
flagged for up to a week — mislabelled as `new permanent failures/day`, but flagged. A smaller set is never
reported, and a larger one stops being reported after the week. Field 5 − field 11 does not see it either, because those panos *are* attempted and field
11 counts attempts. Measured: 30 nights of 200 transient failures each, no downloads and a flat corpus
return no finding. **This is a gap #163 opened, not one it found:** before it, the zero-new-images check
fired on exactly that city after 30 nights, because it asked for no evidence of work. The evidence gate
cannot tell this city from a mature one with nothing new to fetch (field 5 flat, field 5 − field 11 = 0 in
both), and silencing the second is what the gate is for. Detecting it would need the runner to write tonight's transient count as a field 20 — and `LOG_COLUMNS` here
and `LOG_CSV_FIELD_COUNT` in `DownloadRunner.py` move together, which a test asserts. The depth phase does not
have this blind spot: a phase that requests and saves nothing is reported above.

**Field 20 now carries that count** ([#182](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/182), from [#169](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/169)'s open item 3): each run writes its image attempts that raised — the runner's `images-no-success` counter, so a push-back counts and a frame disagreement does not. The steady-transient-set check above reads it: the **max** of each night's rows (the queue's extra passes re-attempt the same set, so a sum would count it twice), at least `TRANSIENT_FAIL_MIN_PER_NIGHT` (10) on each of the last `TRANSIENT_FAIL_NIGHTS` (7) nights, with nothing downloaded on any of them. It needs no 30-night quiet tail and no 90-day lookback, so it fires a week in. Both thresholds are **unmeasured** — [#178](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/178)'s week of nights is what will size them. A blank field 20 is no evidence and breaks the run, so **rows older than the field are still blind**: the measured case above, on rows written before the field, still returns no finding.

## The depth backfill

Every city's stats line carries a depth clause once its `log.csv` has a row with field 19, the corpus size
([#43](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/43)):

```
depth 1,753/183,680 (1.0%) · +590 panos/night · ~308 nights left
depth complete (2,709)
depth not started (0/5,381)
depth 27,001/100,000 (27.0%) · +1,889 panos/night · ~39 nights left · nothing saved in 10 requesting nights
```

The last line is a phase that is asking and saving nothing (the CRITICAL above): its ledger is growing by
every failure, so the rate and the ETA beside it are counting `unavailable` verdicts, not depth. The trailing
clause is there so the line cannot read as a healthy backfill. It counts the nights that made requests, the
same number the WARNING and CRITICAL lead with, not the calendar nights since the last save.

The report ends with the fleet's block — resolved out of eligible, panos resolved and requests made per
night, how many cities are complete, stalled or saving nothing, and the three cities with the longest road
ahead. That last
line is the operational number: the fleet finishes when its slowest city does, and a fleet *average* (the
47-night estimate that sized the current slots) hid a tail more than ten times longer.

How the figures are defined, since each definition is a trap the other way (the row-level reasoning is in
[ops.md](ops.md#reading-the-backfill-from-the-row)):

* **resolved** is what the ledger holds: `depth_skip + depth_success` (fields 15 + 13) of the newest row on
  which the phase actually ran. **Not** `depth_total` (field 16): that adds `depth_fail`, which carries
  transient failures that are re-requested next run, and counting them let a city report `depth complete`
  with panos that will never have depth. A row whose five depth fields are all zero is a phase that did not
  run, not a city with nothing resolved.
* **rate** — both of them — is per **calendar night**, over the last 7 nights ending at the newest row, a
  night with no row counting as 0 and the divisor being the nights the log actually covers (so a city two
  nights into its log is measured over two nights, for panos *and* for requests). Per night, not per row:
  the [queue](downloader.md#nightly-deployment) can run a city more than once a night, and a per-row average
  would halve on every re-run. Not per *logged* night either: a city the window did not reach writes no row,
  and averaging the dates that happen to appear read three runs spread over a month as three nights.
* **ETA** is unresolved ÷ panos resolved per night — panos over panos, never panos over requests, because a
  night of heavy transient failure spends requests and resolves nothing — and is simply absent when either is
  zero. Undefined is not zero.

## Two things about the parsing

**It reads the [20 positional columns](ops.md#the-logcsv-columns) by position**, tolerating a header row that
may or may not be there: `write_log_csv_row` never writes one, and production files get theirs by hand at city
setup. Rows are read with the `csv` module and padded or truncated to the column count *before* pandas sees
them, so nothing about the file's shape is inferred — measured before field 19 shipped, `read_csv` could not
parse the file every production city has (an 18-name hand-written header, years of 18-field rows, then
19-field rows — and since [#182](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/182), 20-field ones after those) under either engine. Blank fields stay `NaN` — a crashed run must never read as a quiet one —
so every check guards against NaN rather than coercing to `int`.

**It uses `sftp -b -`** (batch mode via stdin) rather than `scp`, because the store runs a restricted SFTP
subsystem that doesn't speak the SCP wire protocol; newer `scp` clients default to SFTP-over-SSH and fail with
`mtime.sec not present`.
