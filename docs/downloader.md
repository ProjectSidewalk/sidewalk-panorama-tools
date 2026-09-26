# Downloader — `DownloadRunner.py`

Fetches the pano list from a Project Sidewalk server, downloads each panorama image, downloads a GSV depth
map for each pano, and appends one row to `log.csv` describing the run. It is the actively maintained tool in
this repo and runs nightly, per city, in production.

A run has two phases:

1. **Image phase** — stitch and save panorama JPEGs. It no longer writes the 8192 px
   [display copy](ops.md#display-copies-of-wide-panoramas) beside a wider panorama: that was switched off on
   2026-09-09 and the reasoning is in that section. Gated by
   [`pano_id_log.csv`](ops.md#resume-ledgers); restricted to labeled panos unless `--all-panos`.
2. **Depth phase** — one metadata request per unresolved pano, saving a `.depth.npz` artifact where Google has
   one. Gated by `depth_log.csv`. Always covers **every** pano, labeled or not. See [Depth maps](depth.md).

Both phases share one `--max-runtime` budget, and `log.csv` gets its row in a `finally` even if the run
crashes — see [Ops](ops.md).

## Install

Python 3.10 on Ubuntu 22.04 is the supported baseline — that is what CI installs and what production runs.
Newer Pythons and other Linux distros work; macOS works for development. No Docker, no root, no FUSE
capabilities.

```bash
git clone https://github.com/ProjectSidewalk/sidewalk-panorama-tools.git
cd sidewalk-panorama-tools
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Two platform notes:

* **`streetlevel` needs `pyfrpc`**, which publishes manylinux wheels for CPython 3.9–3.13 but no Windows or
  macOS wheels. On Linux the install is pure wheels; elsewhere pip builds it from the sdist, which needs a C
  compiler (`apt install python3-dev gcc`, or Xcode command-line tools, or MSVC Build Tools on Windows). Only
  the depth phase imports it — `--skip-depth` runs fine without it.
* **At least 2 GB RAM.** A 16384×8192 pano is 384 MB decoded (`16384 × 8192 × 3` bytes), and low-memory
  machines crash on images that size.

## Run it

```bash
python3 DownloadRunner.py <sidewalk-fqdn> <storage-dir> [options]
```

`<sidewalk-fqdn>` looks like `sidewalk-columbus.cs.washington.edu` — visit that URL for the dropdown listing
every publicly deployed city you can pull from. `<storage-dir>` is the root of the pano store; the
[layout under it](ops.md#storage-layout) is created as needed.

```bash
# Everything for Columbus, into a local store
python3 DownloadRunner.py sidewalk-columbus.cs.washington.edu /srv/panos/columbus-oh

# Images only, capped at two hours, from a CSV pano list instead of the API
python3 DownloadRunner.py sidewalk-columbus.cs.washington.edu /srv/panos/columbus-oh \
  --skip-depth --max-runtime 120 -c my-panos.csv
```

## Options

| Flag | What it does |
|---|---|
| `-c <csv>` | Read the pano list from a CSV instead of `/adminapi/panos`. See `samples/` for the shape. |
| `--all-panos` | Download **images** for panos users visited but never labeled. Does not affect depth, which always covers every pano. |
| `--skip-depth` | Skip the depth phase (it is on by default). |
| `--max-runtime MINUTES` | Stop *starting* new downloads and requests after this much wall time. Sized to the nightly cron slot ([#38](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/38)). |
| `--min-depth-runtime MINUTES` | Reserve the tail of `--max-runtime` for depth when depth has unresolved work — a *share* of the budget, so size it against the slot, not the night. Default `0`; the production line passes `6` of its 12-minute `--city-max-runtime` (below), and must stay below it or no images are downloaded. |
| `--max-depth-requests N` | Stop the depth phase after N metadata requests. Useful for throttling the initial backfill. |
| `--depth-block-latch PATH` | Where a refusal from Google is remembered so the next city stands down instead of rediscovering it. Defaults to a file in the system temp directory - local disk, not the store. Moves the latch for **both** phases: the GSV image phase reads it before each photometa request and writes it on a refusal ([#74](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/74)). See [Depth maps](depth.md#being-a-good-citizen-of-googles-servers). |
| `--depth-pace-state PATH` | Where the depth pacer remembers the request interval this host has *earned*, so the next city opens there instead of ramping down from `depth_start_interval` again. Only earned speed is kept — a push-back **from Google** or a refusal (in either phase) resets it, a local failure does not, a phase that made no requests writes nothing, and a day-old file is ignored. The phase holds `<PATH>.lock` while it runs, so a second concurrent phase paces itself from scratch. Defaults to a file beside the *default* block latch; `--depth-block-latch` does not move it. |
| `--run-summary-file PATH` | Write a small JSON object (`image_stop`, `depth_stop`) naming what stopped each phase. `scrape_queue` passes this and reads it back to decide which cities still have work; nothing else reads it, and without the flag nothing is written. No default, deliberately — a default path would write into whatever CWD cron started in. |

Budgets are measured with `time.monotonic()`, never the wall clock, so an NTP step or a DST transition cannot
stretch or shrink a run.

### How the two phases split the budget

`--max-runtime` bounds the *whole run*, and the cron slot doesn't care which phase spends the clock. Because
images run first, a big image backlog — a mapathon influx, which is also exactly when many new panos want
depth — could starve the depth backfill night after night. `--min-depth-runtime` counters that: whenever
`depth_log.csv` shows unresolved work, the image phase stops *starting* new panos at
`max-runtime − min-depth-runtime`.

Three consequences worth knowing:

* **It is a reservation, not a hard floor on depth wall time.** A pano already downloading when the image
  share runs out finishes anyway (eating into the reserved slice), and depth still ends at `--max-runtime` —
  on light nights images finish early and depth gets the slack too.
* **It only applies while depth has work.** Once every GSV pano is resolved in `depth_log.csv`, nothing is
  reserved and the image phase keeps the whole budget.
* **A reservation at or above `--max-runtime` zeroes the image phase.** The run downloads **no images** and
  prints `WARNING: --min-depth-runtime (X) >= --max-runtime (Y); NO images will be downloaded this run`, so a
  misconfigured crontab shows up in the night's message instead of looking like ordinary budget exhaustion.

`--min-depth-runtime` is ignored without `--max-runtime`, and with `--skip-depth`.

## Nightly deployment

The fleet runs as **one queue, from one crontab line**, on a box whose clock is set to Seattle's timezone.
`scrape_queue.py` walks
a manifest of cities and starts the next one as soon as the previous one exits — and then, while the window
has a slot left, runs the cities that ran out of budget again ([extra passes](#extra-passes)).

The line in production (the queue since 2026-09-06; depth on; 52 cities × 12 minutes inside an 11.5-hour window
that ends 06:30 Pacific), wrapped in [`cron_notify.py`](ops.md#hearing-about-a-bad-night) because the host
cannot send mail:

```cron
# No CRON_TZ: this cron ignores it (see below). The box timezone IS the schedule.
SHELL=/bin/bash
BASH_ENV=/home/ubuntu/.scraper.env

0 19 * * *  /srv/sidewalk-panorama-tools/.venv/bin/python \
              /srv/sidewalk-panorama-tools/cron_notify.py --name scrape-queue --only-on-failure \
              --log /home/ubuntu/cron_notify.log \
              --sink 'aws sns publish --region us-west-2 --topic-arn <arn> --subject "$NOTIFY_SUBJECT" --message file://$NOTIFY_BODY_FILE' \
              -- /srv/sidewalk-panorama-tools/.venv/bin/python \
              /srv/sidewalk-panorama-tools/scrape_queue.py \
              --cities /etc/sidewalk/cities.csv --store-root /mnt/panostore \
              --max-runtime 690 --city-max-runtime 12 \
              -- --all-panos --min-depth-runtime 6
```

`--min-depth-runtime` stays below the per-city cap deliberately: at or above it the runner downloads no
images at all. To stop the depth backfill without touching anything else, add `--skip-depth` after the
queue's `--` (the second one; the first ends the wrapper's own arguments).

**The wrapper is cron's mail rule with the delivery made pluggable.** It runs the queue, streams its stdout and
stderr through, and when the queue exits hands the capture to `--sink` — by default whenever there was any
output, cron's rule; with `--only-on-failure`, production's choice, only on a nonzero exit — with the exit
code, a one-line subject and a file path in the environment. The exit code cron sees is the queue's own. What the sink is (SNS, on the production host), what the wrapper does when the
sink fails, how it cuts a backlog night's output to fit, and how to verify a change to it are in
[Hearing about a bad night](ops.md#hearing-about-a-bad-night).

**The timezone lives in the box, not in the crontab.** The first version of this line carried
`CRON_TZ=America/Los_Angeles`, which is how cronie (Fedora/RHEL) pins a schedule to a zone — and Ubuntu 22.04
ships Debian's Vixie cron (`3.0pl1-137ubuntu3`), which silently ignores it. Measured 2026-09-17: eleven nights of
`queue starting` at `19:00:01` in a `scrape_queue.log` stamped in the box's then-UTC local time, i.e. **noon
Pacific**, the working day the queue exists to stay out of
([#101](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/101)). The fix is
`timedatectl set-timezone America/Los_Angeles` on the box plus a cron restart (`KillMode=process`, so a running
queue survives it): cron schedules in the system's local time, so `0 19` is 19:00 Pacific and follows DST.
Nothing the scraper writes depends on the box's zone — `log.csv` column 1, `pano_id_log.csv`'s `fetched_at` and
the pacer's state file all carry their offset or are epoch values (that was the point of #101's timestamp change)
— but `scrape_queue.log` stamps are the box's local time with **no offset**, so the switch is invisible in the
file: `queue starting` reads `19:00:01` on both sides of it, and a reader has to know that stamps written
before the switch are UTC and those after are Pacific, seven hours apart in real time. Do not go looking for a step
between the two eras; there is none to find, which is exactly how eleven nights at noon went unnoticed. Do not
put `CRON_TZ` back: it reads as a fix and is not one. A systemd timer would pin the zone for real — it goes
*inside* the calendar spec, `OnCalendar=*-*-* 19:00 America/Los_Angeles`, there is no `Timezone=` directive —
but this is one line, and cron's any-output rule — which [`cron_notify.py`](ops.md#hearing-about-a-bad-night)
keeps, on a host that cannot mail — comes for free, where a timer needs an `OnFailure=` unit.

**Why a queue rather than 53 slots**
([#101](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/101)). The old shape was one line
per city, staggered every 15–30 minutes across the whole UTC day. Each city took the next free slot at
onboarding and the ring wrapped, so **32 of 53 cities ran between 07:00 and 19:00 Pacific** — the working day
on the hosts the runs actually load, which are the pano store and the app servers, both in Seattle whatever
timezone the city itself is in. The fleet's measured work is ~16 minutes a day in steady state, so the whole
ring fits comfortably in one night; the cases that are *not* steady state — a newly-onboarded city, a depth
backfill, a large new batch of labels — are exactly the ones you do not want landing mid-afternoon. A queue
also serialises **by construction**, which is what the stagger was for, and cannot develop the gaps and
collisions that 53 hand-picked slot numbers do as cities come and go.

**Pin a real timezone, not an offset.** `America/Los_Angeles`, not `UTC-7`: the point is to follow DST rather
than re-derive the hour by hand twice a year. Not per-city local time either — Project Sidewalk is global and
there is no shared night, but the load is not in the city.

### The manifest

Two required columns, `city_id` and `fqdn`:

```csv
city_id,fqdn
seattle-wa,sidewalk-sea.cs.washington.edu
columbus-oh,sidewalk-columbus.cs.washington.edu
#richmond-va,sidewalk-richmond.cs.washington.edu
```

* Each city is scraped into `<store-root>/<city_id>`, **and `city_id` must be the app's own id** — the one
  `https://<fqdn>/v3/api/cities` reports for it — because that is the directory the app reads: it cuts
  AI-label crops from `<pano.images.directory>/<city-id>/…` with its own configured id
  (SidewalkWebpage's `MediaDirs.cityDir`). A row under any other name scrapes into a directory the app never
  looks at, which looks exactly like the city never having been scraped. Bayonne's row read `bayonne` for one
  afternoon; the app calls itself `bayonne-fr`.
* **The fqdn cannot be derived from the city_id** — `seattle-wa` is served by `sidewalk-sea`, `columbus-oh` by
  `sidewalk-columbus` — so the two travel together. Both halves come from `/v3/api/cities`.
* **A row whose `city_id` starts with `#` is skipped**, which is how a city is taken out for a night now that
  it has no crontab line of its own to comment out. It is also the only way to say "known, and deliberately
  not scraped here" — a city with no row at all, public or private, fails every night for ever, by design.
  Keep both columns on it: the cross-check below credits a disabled row as a decision only while its fqdn is
  the city's host — or, where the roster gives no host to match (every private city), while it is *shaped*
  like one and is not another roster city's host — so `#laurens-ia,` with the fqdn dropped is a gap, not a
  decision.
* `--cities` has no default on purpose: which cities a host scrapes is a deployment fact, and a wrong default
  would quietly scrape the wrong fleet. There is a worked example at
  [`samples/scrape_queue_cities.csv`](../samples/scrape_queue_cities.csv); the real one lives on the host,
  next to the crontab.

To generate it from the per-city crontab it replaces:

```bash
{ echo 'city_id,fqdn'
  crontab -l | grep -oP 'DownloadRunner\.py \K\S+ \S+' \
    | sed -E 's#(\S+) .*/([^/ ]+)$#\2,\1#' | sort
} > /etc/sidewalk/cities.csv
```

#### The manifest is cross-checked against the fleet

`laurens-ia` and `bayonne-fr` launched on 2026-09-11 and neither got a row. The queue ran green — 53/53 ok,
six nights running — because a city the manifest does not name does not exist to it, until the auto-labeler's
first Laurens labels showed blank Gallery cards
([SidewalkWebpage#5390](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/5390); the app cuts those
crops from what this scraper stores). The manifest stays explicit — a default that scrapes the wrong fleet is
worse — so the omission is made loud instead
([#130](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/130)).

Every deployment serves `GET /v3/api/cities`, the same list of every city from every host: `city_id`, `url`
(`https://<fqdn>`, or `null` for a private city) and `visibility`. After the fleet has run, the queue asks
the first manifest host that will answer — the ones whose city ran ok tonight first, most recent first, at
most three — and names every city whose `city_id` has no row, **private ones included**
([#143](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/143); the check was public-only
until 2026-09-19, and 20 of the 59 deployments are private). The report takes one of three shapes:

```
[queue] cities missing from the manifest: laurens-ia (sidewalk-laurens.cs.washington.edu), bayonne-fr (sidewalk-bayonne.cs.washington.edu; the manifest calls it 'bayonne', the app reads <store-root>/bayonne-fr), zurich (private; url not published)
[queue]   add one city_id,fqdn row per city - city_id must be the app's own id, because that is the directory it reads; a '#city_id,fqdn' row records one that is deliberately not scraped here
[queue] 54/54 cities ok, 0 failed, 0 timed out, 0 not reached, 3 cities missing from the manifest; 610.2 min total
[queue] manifest checked against 39 public and 20 private cities (roster from sidewalk-sea.cs.washington.edu)
```
```
[queue] 54/54 cities ok, 0 failed, 0 timed out, 0 not reached; 610.2 min total
[queue] manifest checked against 39 public and 20 private cities (roster from sidewalk-sea.cs.washington.edu)
```
```
[queue] ERROR: manifest not cross-checked - no roster from sidewalk-sea.cs.washington.edu (timed out), sidewalk-columbus.cs.washington.edu (HTTP 502), sidewalk-cdmx.cs.washington.edu (not JSON) (3 of 54 hosts tried)
[queue] 54/54 cities ok, 0 failed, 0 timed out, 0 not reached, manifest not cross-checked; 610.2 min total
```

The first fails the night: a missing row is the silent failure this exists to catch, and the exit code is the
one unattended alarm. So does the third, deliberately — the hosts asked have just served `/adminapi/panos`, so
three of them not serving the roster is a broken check (an API rename, a proxy in the box's environment, a
moved endpoint) rather than weather, and a check that is quietly skipped every night is worse than none. Both
failure shapes sit above the totals with the other things that went wrong, and the totals line carries the
count or says the check did not run, so "54/54 ok" is never printed above an exit 1 the runs did not earn.
(Until 2026-09-18 the third shape's line came *last*, under a clean totals line and every extra-pass line.)
The attempts are also narrated in `scrape_queue.log` at INFO as they happen, so a check sitting in a 30 s
timeout does not read, in the log, like a queue that died before its summary.

Only the measured shape is read as a roster (a JSON object whose `cities` list carries a string `city_id` and a
`public`/`private` visibility on every entry, with at least one public city); anything else a host sends — an
error envelope, a proxy's HTML — counts as that host not answering. The GET ignores `HTTP(S)_PROXY` from the
environment for the same reason `DownloadRunner`'s session does. A `#`-disabled row counts as a decision
rather than a gap, but only while it still names the city's host: `csv` splits a prose comment on its commas
too, so `# laurens-ia, bayonne-fr launched 2026-09-11` reads as a row for `laurens-ia`, and crediting the id
alone would have silenced the very city the check exists for.

A private city's roster entry carries `url: null` — the app withholds it deliberately — so there is no host to
match and the check names it by id: `zurich (private; url not published)`. That is also why the private
deployments that are never scraped here (study and scratch instances such as `validation-study` and
`crowdstudy`) each need a `#` row once: the manifest, not the code, records that decision, and without the
row the city fails every night. Where the roster gives no host to match — every private city, and any public
one whose `url` the app left null or unparseable — the row is credited while its second column is shaped like
a hostname (labels and dots, nothing else) — weaker evidence than a host match, and the strongest there is;
the prose-comment shape and an empty column both fail it. The residual is a comment of the exact form
`# zurich, ops.md`: a live private id before the comma and a single dotted token after it would read as a
decision, so do not start a comment with a city id and a comma.
`--dry-run` on the current manifest lists exactly which rows are still needed. A shared secret that would make the app publish the private urls was
considered and is not worth building: it would buy the hint text, not the detection.

`--dry-run` runs the same check, so a hand-run before a launch answers "is everything wired?" without waiting
for the night: a gap exits 1 there too; a roster nobody serves is only a WARNING on a dry run (the same line,
with that word in place of ERROR, and exit 0), since that is someone at a keyboard, possibly offline, reading
the plan. Offline, a dry run now waits up to 3 × 30 s before saying so. Hosts are asked once each: a manifest
with several rows on one host does not spend the whole cap on it.

### Options that matter in production

| flag | what it does |
|---|---|
| `--max-runtime` | The **window**. Stops *starting* new cities once spent; a city already running is never interrupted. Size it to the night, not to the work. |
| `--city-max-runtime` | Passed to each city as `DownloadRunner`'s own `--max-runtime`, then hard-killed `--kill-grace` minutes later (default 5). **Always set it** — without it one hung city holds the whole queue open, which is the head-of-line cost of serialising. |
| `--only CITY_ID` | Re-run one city through the same machinery — the lock, the budgets, the summary — rather than by hand. Repeatable. |
| `--no-rotate` | Keep manifest order. By default the starting point rotates daily, so a night that truncates does not always drop the same tail cities. |
| `--single-pass` | Run every city once and leave the rest of the window unused — today's behaviour before [extra passes](#extra-passes). `--only` implies it. |
| `--dry-run` | Print the order and the exact command per city, then run the [manifest cross-check](#the-manifest-is-cross-checked-against-the-fleet). Takes no lock, so it is safe to run while the queue is running. |
| `-- ...` | Everything after `--` is passed to every city verbatim. |

**Exit codes**, since the exit is the alert: it is the subject line of the night's message
([Hearing about a bad night](ops.md#hearing-about-a-bad-night)), and it is the code cron sees: `0` every city ran and succeeded and the
manifest names every city, `1` something failed, timed out, **was never reached**, **a city has no manifest
row** (private or public), or no host would serve the roster to check that, `2` usage, `3` another queue run holds the
lock. A city the window did not reach counts as a failure deliberately — a fleet quietly completing 40 of 53
cities a night is the silent failure this design exists to surface. If a night's truncation is expected and
accepted, the window is the wrong size.

### Extra passes

Pass 1 gives every city its guaranteed slot, `--city-max-runtime`, in the night's rotated order: that is the
head-of-line guarantee, and it is unchanged. Then, **while a full slot of the window remains, the cities whose
previous run stopped on its budget are run again**, each with the larger of a slot and an equal share of what
is left, recomputed as each one starts; passes repeat until a slot no longer fits or nobody hit their budget.

Why ([#43](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/43),
[report](../reports/2026-09-09-depth-backfill-first-nights.md)): three nights into the depth backfill the queue
was using 477 of its 690 minutes. Thirteen small cities were already complete and exited in seconds, giving
nothing back, while every city with a backlog was capped at 12 minutes whether it had 400 panos left or
270,000 — so the five largest cities were 240–463 nights out while a third of every night went unused, and the
share grew every night a small city finished. Spending the window on whoever still has work is what "size the
window to the night, not the work" was always supposed to mean.

Three rules that are load-bearing:

* **Who has work is read from what the runner reported, not from how long the queue watched it.** The queue
  passes each city a `--run-summary-file`; `DownloadRunner` writes what stopped each phase, and a phase that
  stopped on `max-runtime` — either phase — is one that would have kept going. Only an `ok` run qualifies: a
  crash says nothing about work left and re-running it is a crash loop; a timed-out city was killed past its
  budget and would be killed again. Nothing crosses nights and nothing reads the store.

  Timing the subprocess instead is wrong in **both** directions, because the queue measures from spawn to
  exit while the runner's budget clock starts only after its pano-list fetch. A *complete* city whose
  `/adminapi/panos` prologue outlasts its 12-minute slot exits having downloaded nothing and still measures
  over budget — so it was re-run in every pass of every night, forever. And with the production
  `--min-depth-runtime 6`, a city whose image phase stopped on its reserved 6-minute share while depth then
  exhausted its own list exits at minute ~6.4 of 12 — so the city that most needed the leftover window was
  the one denied it.

  The other depth stop reasons are reasons **not** to re-run, and each has to arrive as itself rather than
  collapsed into "stopped early": `blocked` means the host is standing down for six hours and a re-run would
  spend the slot rediscovering that, `consecutive-failures` is a tripped breaker that would trip again, and
  `max-requests` is a per-process cap the operator asked for, which re-running would silently multiply. If no
  summary arrives at all the queue falls back to the old elapsed-time rule, which is at least a *necessary*
  condition. That case is narrower than it sounds: a `DownloadRunner` from before the flag existed does not
  quietly write nothing, it refuses the unrecognised argument and exits 2, so a queue newer than its runner
  books every city `FAILED (exit 2)` and re-runs none of them; and a runner killed mid-run exits nonzero,
  which is never re-run either. What actually reaches the fallback is an `ok` run whose summary could not be
  written (the runner warns on stdout) or one where an operator's own `--run-summary-file` after `--` won.
* **The share is never below a slot, and the depth reservation moves with it.** 213 minutes over 39 working
  cities is 5.5 each, below the production line's `--min-depth-runtime`, which would zero every image phase
  and mail a warning per city; and a fraction of a minute can kill a city inside its pano-list fetch. So
  `--city-max-runtime` is both the pass-1 guarantee and the floor under every later share, and extra passes
  need both it and `--max-runtime`.

  Symmetrically at the top end: `--min-depth-runtime` is a *share* of `--max-runtime`, so the queue scales it
  by the same factor it enlarges the budget by. Passing it through unchanged would hand a 120-minute endgame
  slot 114 minutes of image phase and leave depth the same 6 it had in pass 1 — the opposite of what these
  passes exist for. It is only ever scaled up, never down: pass 1's last city can be clamped *below* the slot
  by what is left of the window, and enlarging the reservation towards a smaller budget is how you get a run
  that downloads no images at all.
* **A later pass never reports a city as "not reached".** That alarm belongs to pass 1: a fleet whose
  guaranteed slots do not fit its window is not completing. Running out of window in pass 3 is the design
  working. A crash in a later pass still fails the night, because a crash is a crash.

What it looks like: the queue log gets `pass 2 starting: 17 cities still had work, 213.0 min of window
left`, and the summary a line per pass — `pass 2: 17 cities re-run in 204.0 min - chicago-il 12.0, …`. The
"N/M cities ok" figure counts pass 1 only, one per city.

**A city re-run in a night writes one `log.csv` row per run**, which the [analyzer](log-analyzer.md) does
not yet expect: its "Multiple runs logged on the same calendar day" rule fires at INFO for every re-run
city, and its 30-*row* recent window covers about ten days once a city writes three rows a night. Both are
addressed in #124; until that lands, read those INFO lines as the extra passes working rather than as
something wrong.

**One queue at a time.** The queue takes an advisory lock (default: the system temp directory, *not* the store
— the store is a network mount whose lock semantics are not guaranteed, and the overlap being prevented is
between runs on this host). The OS releases it when the holding process dies, so a killed run does not wedge
the fleet the way a leftover lock file would. Nothing in this repo had a lock before: with 53 unsynchronised
slots, a slow run and the next slot could put two processes on one city's `pano_id_log.csv`, `log.csv` and
`scrape.log`.

The queue writes its own rotating `scrape_queue.log` at the **store root**, beside the per-city directories.
It answers "what ran last night, in what order, and how long did each city take" — which no per-city log can,
because none of them can see the ring.

### One city, by hand

The queue is a driver, not a replacement for the runner. A single city is still just:

```cron
0 1 * * *  /srv/sidewalk-panorama-tools/.venv/bin/python \
             /srv/sidewalk-panorama-tools/DownloadRunner.py \
             sidewalk-columbus.cs.washington.edu /mnt/panostore/columbus-oh \
             --max-runtime 360 --min-depth-runtime 60
```

* **Give it the venv interpreter by absolute path.** Cron's `PATH` is minimal, and `source activate` buys
  nothing a direct path doesn't.
* **The exit code is the run's own**, so the queue — and the night's message — can read it. `SIGTERM` becomes exit
  143 *after* the `finally` that writes the `log.csv` row, so stopping a run still leaves evidence.
* **Nothing is written relative to the CWD.** `scrape.log` and `log.csv` both land in `<storage-dir>`.
* **Sizing:** `--max-runtime` is the slot, `--min-depth-runtime 60` reserves the tail for depth. Overlapping
  city runs share Google's patience — see the per-process caveat in
  [Depth maps](depth.md#being-a-good-citizen-of-googles-servers).

### If the pano store is on another host

Mount it once on the scraper box and point `<storage-dir>` at the mount — the runner has no opinion about
what kind of filesystem it writes to. An `/etc/fstab` sshfs entry, a systemd `.mount` unit, or NFS all work;
what matters is that the mount is up before the cron slot and reconnects on its own. For sshfs, roughly:

```
user@store.example.edu:/panos  /mnt/panostore  fuse.sshfs
    _netdev,IdentityFile=/root/.ssh/id_rsa,reconnect,ServerAliveInterval=15,allow_other  0 0
```

A systemd `.mount` unit is the version running in production since 2026-09-01. Three things about it are not
obvious. The unit's **filename must match the mount point** — `/mnt/panostore` becomes `mnt-panostore.mount` —
or systemd refuses to load it. systemd mounts as **root**, so the identity file and the host key must be where
*root's* ssh looks, under `/root/.ssh/`: an entry in the cron user's `known_hosts` is never consulted, which is
a location problem rather than a permission one (root can read the file either way), and ssh rejects a private
key whose mode is more permissive than `0600`. And **`uid=`/`gid=` hand the mounted files to whoever cron runs
as** — substitute that account's real ids rather than assuming 1000, because with `default_permissions` a wrong
id gives a mount that looks healthy while every write returns `EACCES`.

```ini
[Unit]
Description=Pano store (sshfs)
After=network-online.target
Wants=network-online.target

[Mount]
What=user@store.example.edu:/panos
Where=/mnt/panostore
Type=fuse.sshfs
Options=IdentityFile=/root/.ssh/id_rsa,port=2222,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,allow_other,default_permissions,uid=1000,gid=1000
TimeoutSec=90

[Install]
WantedBy=remote-fs.target
```

Two things deliberately absent. `allow_other` needs `user_allow_other` in `/etc/fuse.conf` only when a
**non-root** user mounts; `fusermount` skips that check for root, so a systemd unit does not need it. And
`_netdev` is an fstab-generator directive — a native unit derives no ordering from `Options=`, so the explicit
`After=`/`Wants=` is what actually does the work.

**Harden the mount point itself.** This is the one failure the unit makes *more* likely rather than less:

```bash
chown root:root /mnt/panostore && chmod 555 /mnt/panostore
```

`reconnect` only covers the ssh session dropping *within* sshfs's lifetime. If sshfs exits, the unit goes
inactive and the mount point reverts to an ordinary local directory — and if that directory is writable, the
next cron run downloads into the root filesystem instead, reports success, and fills the disk. An unwritable
mount point turns that silent corruption into the ordinary per-pano write failures described next. (An
`.automount` unit is the other answer; it remounts on access rather than failing.)

A store that is full — or unmounted, *provided the mount point is unwritable as above* — shows up as counted,
retried per-pano failures, never as a crash and never as a ledgered permanent verdict, so the next run picks the
work back up once the mount is fixed. The depth
phase's circuit breaker prints the failure breakdown by cause, which is how you tell a full disk from Google
pushing back ([Depth maps](depth.md#being-a-good-citizen-of-googles-servers)).

### Migrating off the old Docker image

Until August 2026 the supported path was a `projectsidewalk/scraper` image whose entrypoint sshfs-mounted the
store *inside the container*, which is why the documented `docker run` needed `--cap-add SYS_ADMIN
--device=/dev/fuse --security-opt apparmor:unconfined`. Most of that machinery existed to undo problems Docker
itself introduced: forwarding `SIGTERM` past PID 1, keeping the runner's exit status from being clobbered by
the unmount, and stopping `/app` — a CWD that died with the container — from swallowing the logs. Running the
venv from cron needs none of it.

To migrate: create the venv as above, mount the store on the host, replace each `docker run` crontab line with
the form above, and drop the `id_rsa` that used to be baked into the image. Flags and semantics are unchanged.
Nothing about the store's on-disk layout changes, so a store the image has been writing to since 2022 is
picked up as-is.

**Done on the production box 2026-09-01**, and three things about it are worth passing on.

The old host could not be migrated in place. Its Python was **3.5** (Ubuntu 16.04), and the image it was
actually running carried **3.8** — that image was built from the `ubuntu:20.04` Dockerfile this repo shipped
until Aug 2026, not the `ubuntu:22.04` one that was eventually deleted. Both sit below this page's 3.10
baseline, so the move had to be to a new machine rather than a new virtualenv on the old one.

The per-flag promise above held, but **the sizing did not transfer** — and the retired crontab was not what this
page recommends. Every city carried `--max-runtime 1320` and no `--min-depth-runtime` at all, against the
`360 / 60` above. That is harmless only while runs finish in seconds because they have nothing to download.

Sizing is also not a per-city question. With ~50 cities on a 15-minute stagger the schedule spans half a day, so
no `--max-runtime` large enough to do real work avoids overlap and shrinking the flag alone cannot fix it: the
levers are the stagger and how many runs may overlap. That matters most for depth, whose throttle is
per-process — see [Depth maps](depth.md#being-a-good-citizen-of-googles-servers).

## Imagery sources

Each pano is dispatched to a source-specific module by the `source` field from `/adminapi/panos`; the modules
live in [`downloaders/`](../downloaders). Three sources are supported: `gsv` and `panoramax` always, and
`mapillary` when `MAPILLARY_ACCESS_TOKEN` is set. Panos with any other `source` are skipped with a warning,
and are deliberately **not** written to `pano_id_log.csv`, so a later run (or a later release) can still
pick them up.

**Google Street View (`gsv`)** — no configuration needed. Stitches 512×512 tiles from Google's undocumented
`cbk?output=tile` endpoint into one equirectangular JPEG. To choose the zoom it asks Google's photometa
endpoint — the one the depth phase already uses, without the depth payload, a 16 KB answer — which zoom
levels this pano is served at, and picks the highest level that is exactly the tile grid the app's
`width`/`height` implies ([#74](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/74)).
If no level is (or its tiles are not 512 px), the pano is **refused** rather than stitched: fetching anyway
would save the top-left corner of a larger pano at the app's exact dimensions, with nothing in the file to
show it.

If photometa is unavailable, or says the pano is gone, the older two-tile probe picks the zoom instead (a
fully black tile at both zoom 5 and zoom 3 means there is no imagery — still the only evidence a permanent
"no imagery" verdict rests on). The probe cannot tell a frame from a crop, so on that path two more tiles —
the ones just past the frame's grid — are checked before the fan-out, and imagery there is the same refusal.
The first time a run falls back to the probe it says so once on stdout and in `scrape.log`; after three
photometa failures in a row that run stops asking photometa at all. A refusal *from Google* on that photometa
request writes the same block latch the depth phase uses (see
[Depth → Being a good citizen](depth.md#being-a-good-citizen-of-googles-servers)): for the next 6 hours
every city on this host takes its zooms from the probe, the depth phase stands down, and the depth pacer's
earned standing is forfeited, exactly as a depth-phase refusal forfeits it.

**A refused pano** is a `WARNING` on stdout and one `ERROR` in `scrape.log`, both containing
`frame disagreement`; it is counted among the night's image failures (`log.csv` field 9, which also carries
older failures, so the count is not readable from there), is not written to `pano_id_log.csv`, and is
retried every run. Production mails stdout only on a night that exits nonzero
([Hearing about a bad night](ops.md#hearing-about-a-bad-night)), so on an ordinary night the `WARNING` is
not delivered — count refusals with `grep "frame disagreement" <store>/<city>/scrape.log`. The remedy is on
the app side: a SidewalkWebpage `gsv_data` refresh that brings the pano's stored `width`/`height` up to what
Google serves now. Expected volume is near zero: all 651 live panos sampled by the
[2026-08-09 photometa census](../reports/2026-08-09-photometa-census.md) served exactly the dimensions the
app stores.

A new pano costs one photometa request where the probe cost two; a pano the probe has to answer costs the
two probe tiles plus the two frame-check tiles; a retired one costs one photometa request plus the two probe
tiles, once. The tiles are then fanned out concurrently with `aiohttp` and `backoff` retries, pasted into a
canvas sized from the app's width/height, and upscaled with LANCZOS when only a lower level exists. One
visible consequence: historic **five-level** panos (5376×2688) now download at their native zoom 4 and count
as plain successes. The probe could only answer zoom 5 or 3, so where it answered 3 they were upscaled and
counted as fallback successes; a city's `log.csv` field 8 can therefore drop while field 7 rises by the same
amount. The tile-resolution history is written up in
[reports/2026-08-07-cbk-tile-resolution.md](../reports/2026-08-07-cbk-tile-resolution.md).

**Mapillary (`mapillary`)** — resolves `thumb_original_url` through the
[Graph API v4](https://www.mapillary.com/developer/api-documentation) and downloads the original-resolution
equirectangular image. Requires a token:

1. Create one at <https://www.mapillary.com/dashboard/developers> (default read scopes are enough).
2. Export it as `MAPILLARY_ACCESS_TOKEN` before running — read it rather than typing it on the command line,
   so it never lands in `~/.bash_history`:

```bash
read -rs MAPILLARY_ACCESS_TOKEN && export MAPILLARY_ACCESS_TOKEN
python3 DownloadRunner.py <sidewalk-fqdn> <storage-dir>
```

The downloader sends it as an `Authorization: OAuth` header, never as an `access_token` query parameter, so
it cannot reach a URL — which matters because `requests` puts the full URL into an `HTTPError`'s message,
and `DownloadRunner` logs that verbatim for a failed pano. That is not a hypothetical: production's
`scrape.log` held a live token in cleartext, on the shared store, after a night of Mapillary 400s.

**What the ledger learns from Mapillary.** Two answers are permanent and write a `downloaded=0` row: a 404,
and a 200 whose body names the image and carries no `thumb_original_url`. Everything else raises and leaves
no row, so the pano is retried next run: any other status, a body that is not JSON, a JSON body that is not
the record asked for, a 200 carrying Meta's `{"error": {...}}` envelope, a 404 whose envelope carries the
auth signature (code 190, or type `OAuthException`), and an image body that is not a JPEG. The envelope
checks are the defensive ones. Every auth failure measured on 2026-09-05 was a non-200 wearing that
envelope, and a real expiry answered 400, so no observed condition reaches them; they cover the one
condition nobody can measure without a live token, a token lacking the needed scope. The 404 check is keyed
on the auth signature rather than on any envelope because a Meta-style 404 for a retired image carries an
envelope too, and refusing those would re-request every retired image nightly forever. The stakes are the
same either way: one night of bad auth read as a verdict on the panos wrote 161 false rows into a city's
ledger on 2026-09-01, and replacing the token recovered none of them until the file was hand-edited on the
store.

Measured 2026-09-06 with the production token: Mapillary answers an id it does not have with a **400**, not
a 404, carrying `code` 100 / `error_subcode` 33 and a message that itself conflates "does not exist" with
"cannot be loaded due to missing permissions". That answer raises like every other 400 and is not ledgered:
a token lacking the needed scope would produce the same body for every pano in the city, which is the
2026-09-01 incident by another route. The cost is one metadata request per retired image per night. A 404
has never been observed, so the 404 branch is the documented shape rather than the measured one.

That leaves **one permanent-verdict path reachable in production**: a 200 that names the image and carries no
`thumb_original_url`. The scope-less token — the one auth condition nobody can measure without a live token —
need not arrive as an envelope at all: Meta's Graph family commonly answers a permission-denied field by
*omitting* it from an otherwise healthy 200 record, which is exactly that shape, and it would ledger every
pano in the city.

**So that path has a run-level breaker** ([#113]). Three consecutive permanent verdicts from one source stop
this run ledgering that source: its remaining panos are left unattempted, the run says so on stdout and in
`scrape.log`, and it exits nonzero — which `scrape_queue.py` books as a failed city, so the night's message
carries it. The
verdict that trips the breaker is itself withheld, so a trip costs two false rows rather than three.

It is keyed on the **source**, not on the no-rendition verdict specifically. That is broader than the shape
above by design: it also covers a mass 404, which carries identical risk and has simply never been observed.
And it costs nothing in headroom, because the base rates differ by over two orders of magnitude — measured over
the production ledgers on 2026-09-06, richmond-va (the only Mapillary city, whole corpus after the 2026-09-05
catch-up) has **0 permanent verdicts in 9,229 rows**, while the large GSV cities run **7.9–8.4%** because
retired imagery is permanent and ordinary. A source-blind breaker would stop a healthy GSV city about every
1,700 panos, which is why `MAX_CONSECUTIVE_PERMANENT_FAILURES` is a per-source table and GSV is not in it.
A new imagery source declares its own threshold there rather than growing a second breaker — and a source
with no entry has **no breaker at all**, so adding one is part of adding a source.

**Panoramax took its entry with its first city** ([#110](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/110)),
also at 3, and needs it more than Mapillary does. Mapillary has one permanent verdict shape; Panoramax
has three, and two of them are wholesale failures wearing a per-pano face. A picture affirming a field of
view other than 360 is refused one at a time, but **323 of the 1,000 pictures in the Bayonne bbox are flat
92° photographs** — the only thing keeping them out of the corpus is that the app filters its own search to
360, which is a property of the layer above that this scraper cannot check. A missing `hd` asset is the same
shape one federated instance wide. If either ever goes wrong the candidates are shuffled, so the breaker
trips within about ninety panos on the first night and the night's message says so — instead of the city writing itself
off a third at a time, silently and permanently.

Only a **success** resets the count — not a transient failure, and not a skip. See
[ops.md](ops.md#when-the-image-phase-stops-trusting-a-source) for why that distinction is the whole
difference between a breaker that fires and one that cannot.

[#113]: https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/113

Where the reason lands: the night's message carries the count (`N failed` in the `IMAGEDOWNLOAD` line) and nothing
else, so from the mail alone an auth envelope and a network outage look the same. The envelope's `type`,
`code` and `message` are in `scrape.log` on the store, one line per pano.

Without the token, Mapillary panos are filtered out of the run rather than failed — **silently enough to
miss**, so a city that should have Mapillary imagery and downloads none is the symptom of a token that never
arrived.

**Under cron, keep it out of the crontab body.** `crontab -l` output lands in backups, screenshots and
pastes, and the file itself outlives the person who wrote it. Put it in a mode-`600` file and let bash source
it for every line — the crontab sets `BASH_ENV`, cron propagates that into each job's environment, and
non-interactive bash then reads it. **These two lines have to sit above every job line**: Vixie/Debian cron
accumulates environment assignments as it parses the crontab top to bottom, so a `SHELL=`/`BASH_ENV=` placed
after the job lines takes effect for none of them — the same kind of silent no-op as a token that never
arrived, above.

```cron
SHELL=/bin/bash
BASH_ENV=/home/ubuntu/.scraper.env
```

```bash
# /home/ubuntu/.scraper.env — mode 600, owned by the cron user
export MAPILLARY_ACCESS_TOKEN='MLY|...'
```

`SHELL=/bin/bash` is load-bearing: under `/bin/sh` (dash) `BASH_ENV` is ignored and the token is simply
unset, which fails as a *quiet* filtering-out rather than an error. Verify with a throwaway crontab line
that echoes `${#MAPILLARY_ACCESS_TOKEN}` to a file — the length, never the value. The first Mapillary city is
measured in [reports/2026-08-11-mapillary-census.md](../reports/2026-08-11-mapillary-census.md).

**Panoramax (`panoramax`)** — the French open street-level imagery commons, run by IGN and OpenStreetMap
France, and the source behind Bayonne ([#110]). **Keyless**: no token, no account, nothing to configure or
keep out of a crontab. It is a *federation* — `api.panoramax.xyz` is a meta-catalog over instances that each
hold their own pictures — so the downloader resolves `GET /api/pictures/<uuid>` at the catalog and then
follows the `assets.hd` href to whichever instance holds the pixels (`panoramax.ign.fr` for Bayonne's own
survey, `panoramax.openstreetmap.fr` for contributor pictures on the same streets). The href is followed,
never built.

Ids are UUIDs, so a Panoramax city shards over at most 256 `<pano_id[:2]>` directories.

**What the ledger learns from Panoramax.** Three answers are permanent and write a `downloaded=0` row:

| Answer | Why it is a property of the picture |
|---|---|
| `404` at `/api/pictures/<id>` **carrying the catalog's own body** | The catalog does not have it. Measured 2026-09-08: `{"status": 404, "message": "Feature not found"}`, and both fields are required. Unlike Mapillary's 404 the body is not read for an auth signature — there is no token, so there is no "we are not allowed to see it" state to confuse with "it is not there" — but it is read for the affirmation itself, because the status alone does not say the *catalog* answered. `{"message": "Not Found"}` is what AWS API Gateway's HTTP API answers an unknown route with, and the usual shape of a hand-written JSON 404 |
| An item whose `field_of_view` is not `360` | It is a flat photograph, and this scraper stores equirectangular panoramas — see below |
| An item with no `hd` asset | The catalog affirms the picture, lists its renditions, and `hd` is not among them. Only a well-formed, **non-empty** assets block that does not offer `hd` — an empty block, or an `hd` entry whose href is missing, null or empty, is the catalog changing shape, and raises. Every picture measured in the Bayonne bbox carries `hd`, `sd` and `thumb` |

Everything else raises and is retried next run: any other status, a 404 without the catalog's own body, a
body that is not the item asked for, an error envelope on a `200`, an empty assets block, an `hd` href that
is missing, null, empty or not an absolute `https://` URL, and a `200` whose body is not a JPEG.

**Why the projection guard exists.** Panoramax is a commons, not a fleet: anyone can contribute, so a city's
bounding box carries other people's pictures too. Measured over 1,000 pictures in the Bayonne bbox
(2026-09-08), **323 were flat 92° photographs** rather than panoramas. Nothing downstream of the downloader
inspects projection — a flat JPEG saved as `<pano_id>.jpg` would be cropped with the equirectangular seam
modulo and look entirely plausible — so a picture is refused when its item *affirms* a field of view other
than 360. An item that states none is downloaded: absence of evidence is not a verdict. The full measurement
is in [reports/2026-09-08-panoramax-api.md](../reports/2026-09-08-panoramax-api.md).

Bayonne's frames are 5760×2880 and 5376×2688 — smaller than any GSV city's, and well under the
[#115](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/115) display-copy cap, so no sidecar
is written for them. Licence varies **per picture** (`etalab-2.0` and `CC-BY-SA-4.0` over the 1,000 measured; the catalog permits others);
carrying it beside the crops is [#111](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/111).

[#110]: https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/110

## `config.py`

| Setting | Meaning |
|---|---|
| `thread_count` | Tile fan-out for the image phase (default 8). This is I/O-bound async work, so higher is faster up to your network's limit — test on your own connection. |
| `headers_list` | Real request headers, one picked at random per request. Add to it, edit it, or leave it. |
| `proxies` | Set to the `http://`/`https://` sentinel values to disable; otherwise fill in proxy details. |
| `depth_min_request_interval` | The **floor** on the gap between depth metadata requests (default `0.25` s) — the one setting that decides how aggressive this host can ever get, since nothing draws a shorter gap. A run opens at `depth_start_interval` (`1.0` s), or wherever the last run on the host earned its way down to, and decays towards the floor only on sustained clean requests; `depth_max_request_interval` (`30` s) caps the back-off. `0` disables the throttle but not the reaction to push-back. See [Depth maps](depth.md#being-a-good-citizen-of-googles-servers). |

## Related

* [Depth maps](depth.md) — the depth phase, the artifact format, and what the depth product is and isn't.
* [Ops](ops.md) — storage layout, the resume ledgers, the `log.csv` columns, and what a crashed run looks like.
* [Repairing `fover`-era panoramas](ops.md#repairing-fover-era-panoramas) — the downloader never revisits an image it already has, so a store scraped before the `fover` fix needs a deliberate pass.
* [Log analyzer](log-analyzer.md) — monitoring the nightly run across all cities.
