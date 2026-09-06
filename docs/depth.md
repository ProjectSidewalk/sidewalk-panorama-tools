# Depth maps

`DownloadRunner.py` downloads a depth map for every GSV pano by default (`--skip-depth` turns it off), using
the [streetlevel](https://github.com/sk-zk/streetlevel) library to fetch Google's photometa response. Not
every pano has depth data — third-party and some older panos don't — so the phase saves it where available
and records the outcome either way.

The depth payload itself is decoded in-repo: every streetlevel release through 0.12.11 misreads a header
byte, which makes its parser crash on the ~1% of panos whose zenith is a modeled surface (tunnels, overpass
soffits) — see [sk-zk/streetlevel#45](https://github.com/sk-zk/streetlevel/pull/45). Once that fix ships, the
bypass can be revisited.

> **Read [What the depth product is (and isn't)](#what-the-depth-product-is-and-isnt) before building
> anything on this.** It is Google's plane-based model of the scene, not a measurement of it.

## The artifact

Written next to the pano's `.jpg`, at `<first-2-chars-of-pano-id>/<pano_id>.depth.npz`. Load it with numpy:

```python
import numpy as np
d = np.load("aB/aBcDeF....depth.npz")
d["depth"]           # float32 (height, width), typically 256x512; metres from the camera; -1 = no plane
d["plane_indices"]   # uint8 (height, width): index into the plane list; 0 = no plane (exactly where depth is -1)
d["planes_n"]        # float32 (P, 3): plane normals, verbatim from Google's payload (pano-local frame)
d["planes_d"]        # float32 (P,): plane offsets; a plane is {p : p·n = d}, so its perpendicular camera distance is |d| / ||n||
d["heading"]         # camera heading in radians (NaN if Google omitted it); likewise d["pitch"], d["roll"]
d["format_version"]  # 3; version 2 lacked the three plane fields; absent means pre-mirror-fix (see below)
```

**The array shares the JPEG's orientation** — column 0 of `d["depth"]` is the leftmost column of the pano
image. streetlevel's decoder delivers the payload x-mirrored relative to the imagery; we flip it back on
write, and contract tests pin the decoder's end-to-end output orientation (both the ray-direction formula and
the write order) so an upstream change fails CI instead of silently re-mirroring new artifacts
([#58](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/58)). **An artifact with no
`format_version` field predates that fix and is horizontally flipped** — see [Migrating a pre-v2
store](#migrating-a-pre-v2-store).

### Sampling depth under a label

```python
col = int(pano_x / pano_width * d["depth"].shape[1]) % d["depth"].shape[1]
row = min(int(pano_y / pano_height * d["depth"].shape[0]), d["depth"].shape[0] - 1)
meters = d["depth"][row, col]
```

Truncation, not `round()`: each depth pixel covers a *range* of pano columns, and flooring picks the pixel
containing the position, where rounding would pick the pixel whose edge is nearest — a systematic half-pixel
shift. The payload is angular (~0.7°/pixel; the horizon at θ = π/2 falls midway between the two middle rows,
not on a single row), so this scaling works at any pano resolution.

**Frame caveat.** `pano_x` and the pano raster are both *heading-centred*: column 0 sits at compass bearing
`pano_yaw − 180°`, the vehicle's forward direction at image centre. The legacy pre-evolution-179 `sv_image_x`
is *north-referenced* (`sv_image_x / 13312 × 360` is a true compass bearing). Mixing the legacy value with the
raster or this array displaces a label by up to half a panorama — and by nothing at all on a pano that happens
to face south, so a one-example sanity check can pass on the wrong convention.

### The plane fields

The plane fields ([#56](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/56)) are the raw
material for per-pano **camera height** and **ground tilt**. Google's depth is plane-based, and `depth` is
derived from the planes via

```
depth[r, c] == |planes_d[i] / (v(r, c) · planes_n[i])|     for i = plane_indices[r, c] > 0
```

where `v(r, c)` is the unit ray at `θ = (h−r−0.5)/h·π`, `φ = (w−c−0.5)/w·2π + π/2`. That identity is
CI-tested and is the operational definition of the normals' frame: `plane_indices` shares `depth`'s
row/column order, and the normals are untouched by the mirror fix. The writer also refuses to emit an
artifact whose `plane_indices == 0` mask doesn't match its `depth == -1` mask, so the correspondence holds by
construction rather than by assumption.

`downloaders/gsv.py` ships the reference derivations, `ground_plane_from_artifact(d)` and
`camera_height_from_artifact(d)` (its `|d| / ||n||`, sign-insensitive):

```python
from downloaders.gsv import camera_height_from_artifact
height_m = camera_height_from_artifact(d, default=2.5)  # per-pano camera height above the modeled ground
```

The ground plane is picked as the near-horizontal plane that most of the pano's *below-horizon* pixels land
on — rows from `(h+1)//2` on, which are exactly those with `θ < π/2` (plain `h//2` for the even heights every
real raster has). Both halves of that rule matter: ranking on verticality alone lets a few pixels of an
overpass soffit or tunnel ceiling — flatter than any real cambered road — outrank tens of thousands of pixels
of actual road, and the returned "camera height" then silently becomes the height of the ceiling. When no
plane below the horizon qualifies, the helpers return `None` (or your `default`) rather than a confident
wrong answer.

## The ledger

`depth_log.csv` is an append-only `pano_id,status` ledger of **resolved** outcomes:

| status | meaning |
|---|---|
| `saved` | artifact written |
| `unavailable` | pano gone from Google, or no depth payload — permanent and expected |

Ledgered panos are never re-requested. Transient network failures are *not* ledgered, so they retry on the
next run. **The artifacts on disk are the ground truth** — deleting the ledger is safe and just makes the next
run re-check everything; existing artifacts are re-registered without re-downloading.

## Migrating a pre-v2 store

Any store scraped before the [#58](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/58) fix
holds x-mirrored artifacts, and the scraper will never correct them on its own — existing artifacts are never
re-fetched or rewritten. `migrate_depth_artifacts.py` fixes them offline: it scans a storage root, flips every
artifact whose `format_version` is missing or below 2, and stamps it, leaving v2 artifacts byte-for-byte
untouched. It is idempotent, so re-running on a healthy store is a no-op.

```bash
python3 migrate_depth_artifacts.py /path/to/storage --dry-run   # count pre-v2 artifacts, change nothing
python3 migrate_depth_artifacts.py /path/to/storage             # rewrite them in place
```

There is **no offline migration from v2 to v3**: the plane fields v3 adds were never stored by the v2 writer,
so they can only come from a re-fetch. A v2 artifact reaches v3 by deleting the artifact *and* its
`depth_log.csv` row, which makes the next run re-request it. (Only pre-[#56](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/56)
dev and test runs ever produced a v2 artifact — no production store has run the depth phase.) The plane fields
cost roughly 10–30 KB per pano on top of v2's 50–200 KB.

## Runtime budget

The depth phase runs after the image phase and the two share one `--max-runtime`. `--min-depth-runtime`
reserves the tail of that budget for depth whenever the ledger shows unresolved work; `--max-depth-requests`
caps the phase's request volume during backfill. The exact semantics, and the three ways the reservation can
surprise you, are in [Downloader → How the two phases split the budget](downloader.md#how-the-two-phases-split-the-budget).

## Being a good citizen of Google's servers

The phase is serial — one metadata request in flight at a time, unlike the image phase's `thread_count`
fan-out — and on top of that:

* **Requests stop when Google pushes back.** The photometa endpoint doesn't answer scraping pressure with an
  HTTP 429; it serves (or redirects to) a captcha/consent interstitial carrying a 200, which would otherwise
  look identical to one pano having a bad payload. A response hook spots those, and the phase stops for the
  run rather than spending the rest of its budget on a wall. Exhausting the retry policy against 429/5xx is
  treated the same way.
* **A circuit breaker** stops the phase after 25 consecutive transient failures, with escalating back-off
  (30 s / 2 min / 5 min) before it gives up. Nothing is concluded from a trip — every unresolved pano simply
  retries next run — but the run prints a loud warning breaking the failure streak down by cause (e.g.
  `24 storage, 1 network`) and naming the last error. Storage failures (a full or unmounted store) count
  toward the breaker too but skip the back-off — waiting cannot un-fill a disk — so read that breakdown before
  assuming a Google rate limit: `[Errno 28] No space left on device` points at the store, not the network. A
  run that stops on its `--max-runtime` or `--max-depth-requests` budget (or finishes its list) after failures
  prints a warning with the last error too, so a store that fills mid-run can't hide behind a budget stop.
* **The pacing is adaptive** ([#43](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/43)).
  A run opens at `config.depth_start_interval` (1.0 s), **doubles** on any sign of push-back, and earns its way
  back down towards `config.depth_min_request_interval` (0.25 s) by a factor of 0.8 only after 200 consecutive
  clean requests. The gap actually slept is drawn `uniform(interval, 2 × interval)` — full-width, so there is
  no fixed cadence to key on. The previous scheme was `interval + uniform(0, 0.25 × interval)`, which is a
  near-constant rhythm.
  * **The floor is the one knob that matters.** Nothing ever draws a gap shorter than it, so
    `depth_min_request_interval` alone decides how aggressive this host can get. This is bounded exploration,
    not a rate finder hunting for the limit. The 0.25 s default is the pace the
    [2026-08-09 photometa census](../reports/2026-08-09-photometa-census.md) ran 1,360 requests at with no
    push-back — the fastest rate this repo has live evidence for.
  * **Push-back means more than a 429.** urllib3 retries 429/5xx *inside* the adapter, so a retried status
    never reaches a response hook and one that exhausts the policy raises `RetryError` instead of returning.
    What the observer actually keys on is the **retry history** of a response that eventually succeeded:
    Google made us try again. That is the earliest warning available, and reacting to it is the whole point
    of pacing rather than only stopping at the point of refusal.
  * **Setting the floor to 0 disables the throttle, not the reaction.** A back-off from 0 would stay 0, so a
    push-back always lands on at least `DEPTH_PACE_MIN_BACKOFF` (1 s).
  * **The throttle is per-process.** That is safe only because `scrape_queue.py` runs one city at a time;
    going back to concurrent per-city cron lines would multiply the rate Google sees by however many overlap.
* **A refusal is remembered across runs — the block latch.** Standing down for the *run* is not enough when
  the fleet is 52 cities through one queue: each would rediscover a live block with fresh requests aimed at
  the endpoint that just refused us, which is how a soft refusal is escalated into a ban that stops the image
  phase too (tiles leave the same IP). So a blocked stop writes a timestamp file, and any depth phase starting
  within `DEPTH_BLOCK_LATCH_HOURS` (6) skips itself entirely, at zero requests, with a `WARNING` on stdout.
  * **Only a blocked stop latches.** The circuit breaker counts storage failures too, and a full disk says
    nothing about Google.
  * **It lives on local disk** (`--depth-block-latch` overrides), *not* the pano store: the storage directory
    a run is given belongs to a single city, so a latch there could not be cross-city even in principle, and
    what is being remembered is this host's standing with Google. Same reasoning as `scrape_queue`'s lock.
  * **Every ambiguous latch resolves towards scraping.** Missing, unparseable, or dated implausibly far in the
    future all mean "not blocked" — a latch nobody can read must never be able to stand the whole fleet's
    depth phase down indefinitely.
* **Sizing, for context.** A photometa request measured **0.077 s median** from the production box, and the
  corpus is 1,433,104 GSV panos, so at the default floor the backfill is on the order of a fortnight of
  nights. This page used to say it was "inherently a multi-month job" and used that to argue for leaving
  pacing off; both halves of that were wrong.

## Ops notes specific to depth

* **Depth ignores `--all-panos`.** The image phase only downloads labeled panos unless you pass that flag, but
  depth always covers every GSV pano the server knows about — including ones nobody has labeled, and ones
  whose image download failed or was never attempted. It costs one metadata request per pano either way, and
  the goal is depth for the whole corpus. So the depth phase's pano count is normally larger than the image
  phase's; both are printed at startup.
* **Unresolved panos are shuffled each run.** Iteration order is otherwise stable, so a cluster of panos that
  fail every time would monopolise `--max-depth-requests` run after run and the backfill would never reach
  anything behind it.
* **The depth failure count in `log.csv` is not an alert signal.** It includes `unavailable` — a permanent,
  expected, non-actionable outcome — so the first backfill runs show large failure numbers that are entirely
  normal. The success/failure/unavailable split is printed to stdout and `scrape.log`; `log.csv` keeps its
  18-column positional shape, so there was no room for a separate column.
* **Storage or ledger write failures** (a full or unmounted store) are treated as transient per-pano failures
  and retried next run — the phase deliberately never lets them escape.

## What the depth product is (and isn't)

The depth map is Google's plane-based encoding decoded to a per-pixel distance grid — and it is **not a
measurement of the scene**. Analysis of 409 payloads
([label-latlng-estimation#9](https://github.com/ProjectSidewalk/label-latlng-estimation/issues/9)) shows a
constructed model of terrain plus extruded building footprints: >99% of pixels lie on near-flat or
near-vertical surfaces, with none of the intermediate slopes (car roofs, tree canopy, pitched roofs) a real
reconstruction would have. Consequences for anything built on it:

* **Vehicles, people, and vegetation are absent.** A ray aimed at a parked car passes through it and returns
  the ground behind — a distance *overestimate* that can't be detected from the depth alone, only from imagery.
* **Under a label, depth is close to plain trigonometry** — ~91% of ground pixels fall within 1 m of
  `camera_height / tan(depression)`. The payload's added value is terrain relief and rays that hit a facade.
* **Curb ramps sit ~0.15 m above the modeled road surface**, so rays overshoot them by roughly 0.5 m at
  typical label distances. That's a bias, not noise.
* **`-1` means "no plane"** — sky *and* anything unmodeled. It is not "very far away".
* **Building geometry drifts between captures** (facades from re-captures of the same street differ by a
  couple of metres), so don't treat facade distances as survey-grade.

A live census of what fraction of labeled panos still serve a payload at all is in
[reports/2026-08-09-photometa-census.md](../reports/2026-08-09-photometa-census.md).
