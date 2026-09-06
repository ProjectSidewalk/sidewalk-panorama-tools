# Downloader — `DownloadRunner.py`

Fetches the pano list from a Project Sidewalk server, downloads each panorama image, downloads a GSV depth
map for each pano, and appends one row to `log.csv` describing the run. It is the actively maintained tool in
this repo and runs nightly, per city, in production.

A run has two phases:

1. **Image phase** — stitch and save panorama JPEGs. Gated by [`pano_id_log.csv`](ops.md#resume-ledgers);
   restricted to labeled panos unless `--all-panos`.
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
| `--min-depth-runtime MINUTES` | Reserve the tail of `--max-runtime` for depth when depth has unresolved work. Default `0`; **production should pass `60`**. |
| `--max-depth-requests N` | Stop the depth phase after N metadata requests. Useful for throttling the initial backfill. |

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
  misconfigured crontab shows up in cron mail instead of looking like ordinary budget exhaustion.

`--min-depth-runtime` is ignored without `--max-runtime`, and with `--skip-depth`.

## Nightly deployment

The fleet runs as **one queue, from one crontab line**, pinned to one named timezone. `scrape_queue.py` walks
a manifest of cities and starts the next one as soon as the previous one exits.

```cron
# Vixie/Debian cron reads CRON_TZ; a systemd timer takes Timezone= instead. Without it the schedule is UTC
# and drifts an hour against Seattle every March and November.
CRON_TZ=America/Los_Angeles
SHELL=/bin/bash
BASH_ENV=/home/ubuntu/.scraper.env

0 20 * * *  /srv/sidewalk-panorama-tools/.venv/bin/python \
              /srv/sidewalk-panorama-tools/scrape_queue.py \
              --cities /etc/sidewalk/cities.csv --store-root /mnt/panostore \
              --max-runtime 540 --city-max-runtime 240 \
              -- --all-panos --skip-depth
```

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

* Each city is scraped into `<store-root>/<city_id>`.
* **The fqdn cannot be derived from the city_id** — `seattle-wa` is served by `sidewalk-sea`, `columbus-oh` by
  `sidewalk-columbus` — so the two travel together.
* **A row whose `city_id` starts with `#` is skipped**, which is how a city is taken out for a night now that
  it has no crontab line of its own to comment out.
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

### Options that matter in production

| flag | what it does |
|---|---|
| `--max-runtime` | The **window**. Stops *starting* new cities once spent; a city already running is never interrupted. Size it to the night, not to the work. |
| `--city-max-runtime` | Passed to each city as `DownloadRunner`'s own `--max-runtime`, then hard-killed `--kill-grace` minutes later (default 5). **Always set it** — without it one hung city holds the whole queue open, which is the head-of-line cost of serialising. |
| `--only CITY_ID` | Re-run one city through the same machinery — the lock, the budgets, the summary — rather than by hand. Repeatable. |
| `--no-rotate` | Keep manifest order. By default the starting point rotates daily, so a night that truncates does not always drop the same tail cities. |
| `--dry-run` | Print the order and the exact command per city. Takes no lock, so it is safe to run while the queue is running. |
| `-- ...` | Everything after `--` is passed to every city verbatim. |

**Exit codes**, since cron's mail-on-failure is the alert channel: `0` every city ran and succeeded, `1`
something failed, timed out, **or was never reached**, `2` usage, `3` another queue run holds the lock. A city
the window did not reach counts as a failure deliberately — a fleet quietly completing 40 of 53 cities a night
is the silent failure this design exists to surface. If a night's truncation is expected and accepted, the
window is the wrong size.

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
* **The exit code is the run's own**, so cron's mail-on-failure is the alert channel. `SIGTERM` becomes exit
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
live in [`downloaders/`](../downloaders). Panos with any other `source` are skipped with a warning, and are
deliberately **not** written to `pano_id_log.csv`, so a later run (or a later release) can still pick them up.

**Google Street View (`gsv`)** — no configuration needed. Stitches 512×512 tiles from Google's undocumented
`cbk?output=tile` endpoint into one equirectangular JPEG: it determines a working zoom level (5 preferred,
falling back to 3 — a fully black tile at both means there is no imagery), fans the tiles out concurrently
with `aiohttp` and `backoff` retries, pastes them into a canvas sized from the server's width/height, and
upscales zoom-3 panos with LANCZOS. The tile-resolution history is written up in
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
the record asked for, and a 200 carrying Meta's `{"error": {...}}` envelope. That last check is the
defensive one. Every auth failure measured on 2026-09-05 was a non-200 wearing that envelope, and a real
expiry answered 400, so no observed condition reaches it; it covers the one condition nobody can measure
without a live token, a token lacking the needed scope. The stakes are the same either way: one night of
bad auth read as a verdict on the panos wrote 161 false rows into a city's ledger on 2026-09-01, and
replacing the token recovered none of them until the file was hand-edited on the store.

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

## `config.py`

| Setting | Meaning |
|---|---|
| `thread_count` | Tile fan-out for the image phase (default 8). This is I/O-bound async work, so higher is faster up to your network's limit — test on your own connection. |
| `headers_list` | Real request headers, one picked at random per request. Add to it, edit it, or leave it. |
| `proxies` | Set to the `http://`/`https://` sentinel values to disable; otherwise fill in proxy details. |
| `depth_min_request_interval` | Floor (with jitter) on the gap between depth metadata requests; `0` disables. Leave it at `0` unless a canary run shows Google pushing back — see [Depth maps](depth.md#being-a-good-citizen-of-googles-servers). |

## Related

* [Depth maps](depth.md) — the depth phase, the artifact format, and what the depth product is and isn't.
* [Ops](ops.md) — storage layout, the resume ledgers, the `log.csv` columns, and what a crashed run looks like.
* [Repairing `fover`-era panoramas](ops.md#repairing-fover-era-panoramas) — the downloader never revisits an image it already has, so a store scraped before the `fover` fix needs a deliberate pass.
* [Log analyzer](log-analyzer.md) — monitoring the nightly run across all cities.
