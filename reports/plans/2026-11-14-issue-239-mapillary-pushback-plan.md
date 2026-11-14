# Plan: a Mapillary push-back stop in the image loop (#239, follow-up 2)

Written 2026-11-14 from a reading of master `eb9bbc6`; nothing was run against Mapillary for it. It is a plan
for Jon's approval, not a record of work done. Follow-up 2 of
[#239](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/239); the pre-fetch docs section
(follow-up 1) is `docs/downloader.md#pre-fetching-a-city-that-has-no-server-yet`.

## The gap

`downloaders/common.retrying_session` retries 429 and 5xx inside the adapter (`total=5`, `backoff_factor=1`).
When Mapillary refuses past that, `session.get` raises `requests.exceptions.RetryError`, the image loop counts
the pano as failed and unledgered, and moves to the next one. Nothing counts these refusals: #113's breaker
counts only `downloaded=0` verdicts, and #162's push-back breaker is `if source == 'gsv'`. So a refusing
Mapillary costs up to 6 metadata requests for every pano left in the list, until `--max-runtime` or the end of
the list, and nothing in the exit code says why.

Nightly that is bounded by the 12-minute slot. A standalone run (the Sacramento pre-fetch, about 12,000 panos)
is bounded only by `--max-runtime`, if one was passed, and a refused host keeps hammering the API that refused
it.

## What counts as push-back

`mapillary.pushback_reason(exc) -> str | None`, beside `gsv.pushback_reason` and on the same rule: only the
host refusing *us* counts, never this box's network and never one image's answer.

| Shape | Reason | Status |
|---|---|---|
| `RetryError` whose message reads `too many 429 error responses` | `'HTTP 429'` | Documented HTTP behaviour; same regex as `gsv_tiles._RETRY_STATUS_RE` |
| `HTTPError` carrying a 429 response | `'HTTP 429'` | Unreachable while 429 is in the forcelist; kept for the day it is not, as gsv does for 403 |
| `HTTPError` (400/403) whose body is a Graph rate-limit envelope: `error.code` in {4, 17, 32, 613} | `'rate limit (code N)'` | **Unmeasured for Mapillary.** These are the Meta Graph API's throttling codes; whether Mapillary's Graph API uses them is not known |
| `RetryError` on 5xx, timeouts, resets, DNS | `None` | An outage, not a refusal |
| 400 code 100 / subcode 33 ("does not exist ... or missing permissions") | `None` | One image's answer (measured 2026-09-06); retired images send it every night |
| Auth envelope (code 190 / `OAuthException`) | `None` here; see decision D3 | A revoked token, not throttling |

The image download (`thumb_original_url` on the CDN) goes through the same session; a 429 there is the same
refusal and counts the same way.

## Design

1. **`downloaders/mapillary.py`**: add `pushback_reason(exc)` and a `RATE_LIMIT_CODES` constant. Reads
   `exc.response.json()` defensively (a non-JSON body is `None`). No change to `download_single_pano`: the
   exceptions it raises already carry what the predicate needs.
2. **`DownloadRunner.py`**: generalise the push-back block from GSV-only to a per-source table, mirroring
   `MAX_CONSECUTIVE_PERMANENT_FAILURES`:
   `MAX_CONSECUTIVE_PUSHBACK = {'gsv': GSV_MAX_CONSECUTIVE_PUSHBACK, 'mapillary': 3}` and
   `PUSHBACK_REASON = {'gsv': gsv.pushback_reason, 'mapillary': mapillary.pushback_reason}`. The counter
   becomes a dict keyed on source; only that source's success resets it (#162's rule, unchanged). On a trip
   the source goes into `tripped` and `refused` (exit 1, no `main` edit, and #113's "false downloaded=0 rows"
   advice stays out of the summary), and `image_stop` records `STOP_BLOCKED`, which the queue already reads as
   "no extra pass".
3. **Only GSV latches.** `gsv.record_google_refusal`, the block latch, probation and the depth pace forfeit
   stay inside `if source == 'gsv'`. A Mapillary refusal is not Google's, and the depth phase never asks
   Mapillary for anything.
4. **One WARNING on both channels on a trip**, worded for Mapillary: how many in a row, the reason, that the
   remaining Mapillary panos are unattempted and retry next run, and that a standalone run should wait before
   re-running. No new run condition: the exit code is the channel, as for #113 and #162.
5. **`images-no-success` is unchanged.** A Mapillary push-back is a raise, as a GSV one is.

Keep the GSV block's behaviour byte-identical. The refactor is the risky part, so the existing #162 tests must
pass unmodified before any Mapillary test is added.

## Tests (`tests/test_download_runner.py`, `tests/test_image_downloaders.py`)

- The predicate: each row of the table above, including the 5xx `RetryError` and the 100/33 body as `None`.
- Three Mapillary 429s in a row trip; the remaining Mapillary panos are unattempted and unledgered; exit 1;
  `image_stop == 'blocked'`; **no latch written** and the pace file untouched.
- Two 429s, a success, two 429s: no trip. Two 429s, a transient (timeout), one 429: trips (transients do not
  reset, #162's rule).
- A mixed city: a Mapillary trip leaves GSV running, and a GSV trip leaves Mapillary running.
- The two counters are independent of #113's (a `downloaded=0` neither counts nor resets push-back).
- `test_store_mode_is_not_in_the_breaker_table`-style pin on the new table's contents.

## Docs

`docs/downloader.md` (the pre-fetch section's rate-limit bullet becomes "stops after three refusals"),
`docs/ops.md` "When the image phase stops trusting a source" (a Mapillary push-back trip and its message),
`.claude/rules/downloader.md`, and `CLAUDE.md` only if a summary there moves.

## Decisions for Jon

- **D1. Threshold 3**, `MAX_CONSECUTIVE_UNDERSIZED`'s reason (one refusal can be a blip, three in a shuffled
  list is the host). Mapillary-only cities have no probation equivalent, because there is no latch.
- **D2. Count the Graph rate-limit envelope codes before they are measured?** Recommended yes: a false trip
  costs one night's Mapillary images and is retried, and a missed refusal is the gap this closes. The
  alternative is 429 only, until a refusal has been observed.
- **D3. Stop on an auth envelope (190) too?** Today a revoked token raises on every Mapillary pano for the
  whole slot. It is the same shape (a condition of the run, not of the pano) and the same fix. Recommended as
  a second reason in the same predicate, worded "token refused", rather than a separate breaker.
- **D4. No fixed-delay flag.** Nothing measured says Mapillary throttles at about 2 requests/s; the stop is
  the guard, and a delay can be added when a refusal is seen.
