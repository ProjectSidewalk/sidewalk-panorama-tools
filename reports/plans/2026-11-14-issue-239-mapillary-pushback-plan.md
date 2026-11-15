# Plan: a Mapillary push-back stop in the image loop (#239, follow-up 2)

Written 2026-11-14 from a reading of master `eb9bbc6`; nothing was run against Mapillary for it. It is a plan
for Jon's approval, not a record of work done. Follow-up 2 of
[#239](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/239); the pre-fetch docs section
(follow-up 1) is `docs/downloader.md#pre-fetching-a-city-that-has-no-server-yet`. Revised the same day after
the PR #240 review (Fable 5.2), which found the first draft's "reuse #162's block" design touched GSV-only
code it did not list.

## The gap

`downloaders/common.retrying_session` (shared by Mapillary and Panoramax) retries 429 and 5xx inside the
adapter: `Retry(total=5, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)`. Two
consequences:

- **Past the retries**, `session.get` raises `requests.exceptions.RetryError`; the image loop counts the pano
  as failed and unledgered and moves on. Nothing counts these: #113's breaker counts only `downloaded=0`
  verdicts, and #162's push-back breaker is `if source == 'gsv'`. A refusing Mapillary costs 6 metadata
  requests and about 30 s of backoff for every pano left, until `--max-runtime` or the end of the list.
- **Within the retries**, urllib3 2.7.0 (the pinned version) defaults to `respect_retry_after_header=True`
  with `retry_after_max=21600`, so a 429 carrying `Retry-After` sleeps up to 6 h per retry *inside one
  request*. `--max-runtime` is checked only between panos (`DownloadRunner.py`, the loop head), so it cannot
  interrupt that, and nightly the queue's kill grace becomes the only bound. Whether Mapillary sends
  `Retry-After` is unmeasured.

Nightly the first is bounded by the 12-minute slot. A standalone pre-fetch (Sacramento, about 12,000 panos)
is bounded only by `--max-runtime`, and the second is bounded by nothing.

## What counts as push-back

`mapillary.pushback_reason(exc) -> str | None`, on `gsv.pushback_reason`'s rule: only the host refusing *us*
counts, never this box's network and never one image's answer.

| Shape | Reason | Status |
|---|---|---|
| `RetryError` whose message reads `too many 429 error responses` | `'HTTP 429'` | Documented HTTP behaviour |
| `HTTPError` carrying a 429 response | `'HTTP 429'` | Unreachable while 429 is in the forcelist; kept for the day it is not, as gsv does for 403 |
| A Graph rate-limit envelope, **`MapillaryRateLimited`** (below) | `'rate limit (code N)'` | **Unmeasured for Mapillary** (decision D2) |
| `RetryError` on 5xx, timeouts, resets, DNS | `None` | An outage, not a refusal |
| 400 code 100 / subcode 33 ("does not exist ... or missing permissions") | `None` | One image's answer (measured 2026-09-06); retired images send it every night |
| Auth envelope (code 190 / `OAuthException`) | `None`, or `'token refused'` under D3 | A revoked token, not throttling |

The status regex: `gsv_tiles._RETRY_STATUS_RE` is private. Make it public in `sidewalk_panorama_tools/gsv_tiles.py`
(`RETRY_STATUS_RE`) and import it, rather than copy it; `.claude/rules/package.md` governs the rename.

## Design

1. **`downloaders/mapillary.py` reads the envelope where the response is still open.** The runner cannot:
   `MapillaryErrorResponse` is a bare `RuntimeError` carrying only a capped message, and the image response is
   `stream=True` on a session closed by the time the exception reaches the loop. So `download_single_pano`
   reads the Graph body once, before `raise_for_status()`, and when it is an envelope whose `error.code` is
   in `RATE_LIMIT_CODES` raises `MapillaryRateLimited(MapillaryErrorResponse)` with `.code` set. An envelope
   on a 200 (`original_rendition_url`) gets the same check. Under D3, `is_auth_envelope` bodies raise
   `MapillaryTokenRefused` the same way. A body that is not JSON is never an envelope. The CDN image fetch is
   not read for envelopes; only its 429 `RetryError` counts.
2. **`downloaders/mapillary.py`**: `pushback_reason(exc)` reads exception **types and attributes only** (the
   `RetryError` message, `HTTPError.response.status_code`, `MapillaryRateLimited.code`). It never parses a
   response body.
3. **`DownloadRunner.py`: a separate block beside #162's, not a generalisation of it.** The GSV block, its
   `refused` set and summary (worded for Google and the latch), `last_pushback`, the run-time probation limit
   and the `refused by Google` per-pano line all stay untouched, because `reports/scripts/fleet_thresholds.py`
   parses that line and the trip line (`RAISE_RE`, `TRIP_RE`). The new block keeps its own
   `consecutive_mapillary_pushback`, resets only on a Mapillary success, and on reaching
   `MAPILLARY_MAX_CONSECUTIVE_PUSHBACK` (3) adds `'mapillary'` to `tripped` (exit 1, no `main` edit) and to a
   new `mapillary_refused` flag that selects its own end-of-phase summary. Neither #113's "false downloaded=0
   rows" advice nor #162's latch wording reaches it, which needs one more edit: the end-of-phase split
   `verdict_tripped = tripped - refused` must also subtract the push-back-tripped Mapillary, or #113's summary
   ("check that source's credentials ... look for false downloaded=0 rows") prints for it.
4. **Per-pano line.** A Mapillary push-back keeps the ordinary `Failed to download pano %s due to error %s
   (after %.1f s)` line, so `RAISE_RE` still counts it as a raise. The trip gets one WARNING on both channels,
   `IMAGEDOWNLOAD: WARNING - Mapillary refused N panos in a row (reason). Stopping Mapillary images for this
   run ...`, deliberately not matching `TRIP_RE`'s two alternatives; whether `fleet_thresholds.py` should learn
   it is decision D5.
5. **`image_stop` is left alone** (decision D6). The trip already exits 1, so the queue books the city `failed`
   and `stopped_on_budget` returns before reading the stop. Setting `blocked` would change two things the
   first draft missed: a later `max-runtime` stop can no longer be recorded (the loop never overwrites
   `blocked`), and the `images-no-success` budget arm, keyed on `image_stop == 'max-runtime'`, would be
   switched off for the GSV half of a mixed city.
6. **Only GSV latches.** `gsv.record_google_refusal`, the block latch, probation and the depth pace are
   untouched. The depth phase never asks Mapillary for anything.
7. **Cap `Retry-After`** (decision D1): build Mapillary's session with `respect_retry_after_header=False`, or
   with a cap of a few seconds (`retry_after_max`, present in the pinned urllib3 2.7.0 but not necessarily in
   every version `requirements.txt`'s `>=2.0` allows; the cap would need a raised floor), so a refusal reaches the counter instead of sleeping. `retrying_session` is
   shared with Panoramax, so this is a parameter, not a change to its defaults.
8. **`images-no-success` is unchanged.** A Mapillary push-back is a raise, as a GSV one is.

## Tests (`tests/test_download_runner.py`, `tests/test_image_downloaders.py`)

- The predicate: each row of the table, including the 5xx `RetryError` and the 100/33 body as `None`; a
  `MapillaryErrorResponse` that is not the subclass is `None`.
- `download_single_pano` raises `MapillaryRateLimited` with the code for an envelope at 400, 403 and on a 200.
- Three Mapillary 429s in a row trip; the remaining Mapillary panos are unattempted and unledgered; exit 1;
  `image_stop` unchanged; no latch written and the pace file untouched; the Mapillary summary, never the
  Google one and never #113's.
- Two 429s, a success, two 429s: no trip. Two 429s, a timeout, one 429: trips (transients do not reset).
- A mixed city: a Mapillary trip leaves GSV running and its `images-no-success` budget arm live; a GSV trip
  leaves Mapillary running.
- Independence from #113 (a `downloaded=0` neither counts nor resets push-back) and from #162.
- The session's `Retry-After` setting is pinned, and Panoramax's session is unchanged.
- The #162 tests pass unmodified.

## Docs

`docs/downloader.md` (the pre-fetch section's rate-limit bullet), `docs/ops.md` "When the image phase stops
trusting a source", `.claude/rules/downloader.md`, `.claude/rules/package.md` for the regex rename, and
`CLAUDE.md` only if a summary there moves.

## Decisions for Jon

- **D1. `Retry-After`: ignore it, or cap it at a few seconds?** Recommended: ignore it for Mapillary. With a
  threshold of 3 the run stops after about 90 s of refusals either way; honouring the header can hold one
  request for hours, past any budget.
- **D2. Count the Graph rate-limit envelope codes before they are measured?** Meta's Graph API documents 4,
  17, 32 and 613, plus an 80000 series for business-use limits; whether Mapillary's Graph API uses either was
  not checked. Recommended: 429 only to start, plus logging any envelope code seen on a 400/403 so the next
  refusal is measured. The alternative counts the listed codes now: a false trip costs one night's Mapillary
  images, retried.
- **D3. Stop on an auth envelope (190) too?** Today a revoked token raises on every Mapillary pano for the
  whole slot. Recommended as a second reason in the same predicate, worded "token refused".
- **D4. Threshold 3**, `MAX_CONSECUTIVE_UNDERSIZED`'s reason. No probation equivalent, since there is no latch.
- **D5. Teach `fleet_thresholds.py` the Mapillary trip line?** Recommended no until a Mapillary city trips:
  the script measures GSV thresholds.
- **D6. `image_stop` on a Mapillary trip:** left alone (recommended, item 5), `blocked`, or a new value the
  queue ignores.
- **D7. No fixed-delay flag.** Nothing measured says Mapillary throttles at about 2 requests/s.
