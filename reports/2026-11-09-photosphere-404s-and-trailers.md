# Why re-admitted AF1Qip photospheres still raise: unstable 404s, and complete JPEGs with trailers

2026-11-09. A follow-up to [#223](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/223) and
[the 2026-11-05 forms study](2026-11-05-photosphere-forms.md), after
[#233](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/233) deployed and the 901 AF1Qip
write-offs were re-admitted.

## The question

The re-admit worked in chicago-il, knox-oh, spgg and zurich: almost every re-admitted AF1Qip photosphere
downloaded on its first night. But **20** ids stayed unledgered and raised every night (by city:
chicago-il 1, seattle-wa 14, zurich 5). Seattle's were its only work, so from 2026-11-07 its run raised 14 and
answered 0, `images-no-success` fired, and the whole night failed. They raise in two ways: the photo host
answers **404**, or it answers 200 with a body the downloader called "not a complete JPEG (no SOI first or no
EOI last)". Two questions decide what the code should do:

1. Is a 404 from the photo host a verdict, stable per id and carrying one error page, like the CIHM/CIAB 400?
2. Is a "not a complete JPEG" body really cut short?

## Method

Read-only. `measure` (in `reports/scripts/photosphere_404s.py`) asks each unledgered id once, through the
downloader's own `_photosphere_session()`, `_random_header()` and `PHOTOSPHERE_URL`, so the request is the
nightly one, from a residential Seattle address rather than the production box. Unlike the 2026-11-05 sweep it
reads the **whole body**, because the body is the question: per answer it records the status, `Content-Type`,
`Content-Length`, the body's length and a hash prefix, where its last FF D9 sits, the first 32 bytes after it,
and whether PIL decodes the whole image. Bodies are not kept. Three rounds on 2026-11-09, about 6 h apart:
r1 at 2026-11-09T13:19:19+00:00, r2 at 2026-11-09T19:13:10+00:00, r3 at 2026-11-10T01:17:09+00:00, 1.5 s
between requests, **60** requests in all. The rounds were run by a scratch draft of `measure` that made the same
request and recorded the same fields; its output was transcribed into the committed CSV.

The ids came from the box (`pack-history`): every photosphere named in a `photo host answered` line of any
city's `scrape.log` (not rotated, so it spans every night since #227 deployed, but its lines carry no
timestamp), joined with the id's current `pano_id_log.csv` row. **77** AF1Qip ids had raised at least once;
**20** of them were still unledgered.

Committed: `reports/data/2026-11-09-photosphere-404s-measurements.csv` (one row per id per round),
`reports/data/2026-11-09-photosphere-404s-box-history.csv`, and `reports/data/2026-11-09-photosphere-404s.json`,
which `analyze --write` derives from the two. Every number below is in that JSON;
`tests/test_photosphere_404s_study.py` asserts so.

## Findings

### A. The "truncated" bodies are complete JPEGs with trailers

| Answer across r1, r2, r3 | Ids |
|---|---|
| 200 / 200 / 200 | 6 |
| 200 / 200 / 404 | 1 |
| 404 / 404 / 404 | 13 |

**7** ids answered 200, in **20** responses. In **20** of 20 the body's length equals `Content-Length`, so the
host sent everything it declared (and production's urllib3 would have raised on a short read anyway). PIL
decodes **20** of 20 whole. **0** of 20 end with FF D9: each has bytes after its last EOI, the same bytes on
every round:

| Bytes after the last EOI | Ids |
|---|---|
| 111 | 5 |
| 130 | 1 |
| 3257 | 1 |

**5** of the 7 trailers begin with a Samsung `Image_UTC_Data` record; the other two are zero padding. All
**20** responses keep their trailer inside the 64 KB window that store mode's `is_complete_jpeg` already
accepts for camera originals. The downloader's photosphere check required FF D9 to be the very last
two bytes, so it refused complete images every night.

### B. A 404 from the photo host is not a verdict

**13** ids answered 404 on every round. But the 404 does not carry one error page:

| 404 body (Content-Type, bytes) | Responses |
|---|---|
| image/png 897 | 36 |
| text/html 1610 | 4 |

The HTML one is Google's generic `Error 404 (Not Found)!!1` page (its hash differs between requests), and
**3** of the 13 ids got both shapes across the three rounds. More decisive: **1** id that served a complete 200
in r1 and r2 answered that same HTML 404 in r3. The box's history agrees. **5** AF1Qip ids logged a 404 on two
or more nights and are now ledgered as downloaded. Of the 20 unledgered ids, by what they logged on the box:

- `status-404` only: 13
- `status-404 + truncated-200`: 3
- `truncated-200` only: 4

So the host 404s photos it serves on other requests, and a 404 is not evidence the photo is gone. Unlike the
CIHM/CIAB 400 (628 of 628, one page, two sweeps), there is nothing here to hang a permanent `downloaded=0` on.

## What changed in the code

1. The photosphere download uses store mode's rule, now shared as `common.jpeg_is_complete`, and still saves
   the response bytes verbatim, trailer included. The 7 ids should download on the next night.
2. A 404 stays a transient, as #166 says. But `images-no-success` asks whether the run could download anything
   at all, and a photo the host 404s says nothing about that. So `gsv.is_photo_host_answer` (a photo-host 4xx,
   or a 200 with an unusable body) is set aside by the condition, counted in neither its raises nor its answers,
   while `log.csv` field 20 still counts it. Counting it as an answer instead would let a perennial 404 mask a
   real outage of the city's other panos. A 5xx, a `RetryError`, an interstitial, a timeout or a connection
   error is still a raise.

The cost: the 13 seattle ids are still asked once a night each, and stay in field 20.
