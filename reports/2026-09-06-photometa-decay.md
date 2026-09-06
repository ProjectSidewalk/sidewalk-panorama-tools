# How fast are we actually losing depth? The 2026-08-09 census, re-asked 28 days later

**2026-09-06** · Pre-deploy measurement for the depth rollout
([#43](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/43))

> **Reproduce:** `pytest tests/test_photometa_census.py` offline; the live measurement is
> ```bash
> python reports/scripts/photometa_census.py \
>     --refetch reports/data/2026-08-09-photometa-census.json \
>     --fetched 2026-09-06 --since-days 28 --interval 0.5 \
>     --write reports/data/2026-09-06-photometa-census.json
> ```
> Run from the production scraper box, so the requests leave the IP the fleet actually uses.
> Both censuses embed every per-pano record, so this can be repeated again later.

## The question, and why it mattered

Production has **never run the depth phase**. All 52 crontab lines carry `--skip-depth`, and the store
holds zero `depth_log.csv` files and zero `.depth.npz` artifacts against a corpus of **1,433,104** GSV
panoramas.

The standing argument for hurrying is that this is not a backlog but a loss: a depth map exists only for
a panorama Google still serves — the
[2026-08-09 census](2026-08-09-photometa-census.md) measured **100.0%** of alive panos returning depth and
**0%** of dead ones can — so every panorama that retires before the phase runs takes its depth away
permanently. That argument is only as strong as the retirement *rate*, and nobody had measured it. The
two numbers in circulation were 47.9% alive (census, 2026-08-09) and 39.8% alive
([fover pilot](2026-09-05-fover-refetch-pilot.md), 2026-09-05), which read as an alarming 8-point slide in
under a month — but they are **different populations**, so the comparison was never licensed.

## Method

The 2026-08-09 census embedded its full manifest precisely so the identical sample could be re-fetched
later. `--refetch` replays it verbatim — same 1,360 panos, dead ones included, same
`find_panorama_by_id(download_depth=True)` call path production's depth phase uses. One population, asked
twice, has no strata confound.

**Every alive → dead transition is asked a second time before it is believed.** `run_census` records a
request that *errored* as `found=False`, which is the right conservative call for an alive-rate and
exactly backwards for a decay rate: it books a transient timeout as a permanent death, in the direction
the study would like to believe. Both suspected deaths here were re-asked; neither came back.

## What came out

**Over 28 days, 2 of 651 living panoramas died — and 9 previously-dead ones came back. The living
population went up, not down.**

| | 2026-08-09 | 2026-09-06 |
|---|---:|---:|
| alive | **651** | **658** |
| dead | 709 | 702 |
| sampled | 1,360 | 1,360 |

| transition | n |
|---|---:|
| alive → alive | 649 |
| alive → **dead** | **2** |
| dead → alive ("resurrected") | 9 |
| dead → dead | 700 |
| not re-fetched | 0 |

* **Death rate: 0.31% of the living population in 28 days** (≈0.3% per 30 days).
* **Depth maps that became unobtainable in that window: 2.**
* Deaths by era: legacy **1** of 113, mid **1** of 232, post-179 **0** of 306.
* **3 requests errored — the same 3 panoramas, with the identical message, in both censuses**
  (`ValueError: invalid literal for int() with base 2: ''`, a deterministic `streetlevel` parse failure on
  some response shape). All 3 were already dead in August, so they contribute *no* ambiguity to the death
  count. `reprobe_recovered` is 0.

Two incidental replications, both reassuring for work that rests on them:

* **Depth coverage is still 100.0% of alive panos**, and **dims drift is still 0.0%**.
* The [#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54) tilt prior reproduces:
  |pitch| p50/p90/p99 **0.63 / 2.61 / 6.84°** against August's 0.63 / 2.60 / 6.85°, |roll| **0.91 / 2.15 /
  4.04°** against 0.90 / 2.16 / 4.26°.

## What this changes

**The urgency premise was wrong, and this measurement is what corrects it.** The 47.9% → 39.8% slide was a
population difference between the census's stratified all-city sample and the fover pilot's Seattle
work-list, not a month of decay. Measured like-for-like, the marginal cost of another month of
`--skip-depth` is **~0.3% of the depth still available**, not eight points of it. *If* the whole corpus
behaved like this sample — which is an assumption, since the sample is of **labelled** panoramas and the
corpus is mostly unlabelled ones — 48.4% of 1,433,104 is about 690,000 living panoramas, and 0.3% of that
is on the order of 2,000 panoramas' depth per month.

That is a real cost and it is not zero, but it does not justify racing. It **strengthens** the decision to
pace conservatively: if delay is cheap and an IP block is expensive — a block earned on photometa would
also stop the image phase, since tiles leave the same address — then the right trade is a slow, polite
backfill, which is what the adaptive pacer and the cross-run block latch implement.

The measurement also **was** the canary. 1,360 paced requests from the production IP produced zero
push-back: no interstitial, no 429, no `RetryError`, and the only failures were the three deterministic
parse errors that were already there in August.

### What it does not say

* It is a **28-day window on a population already selected for surviving years**, so the hazard here is
  the hazard of long-lived panoramas, not of the corpus at large. It cannot be extrapolated far.
* It is **one window**. It says nothing about a step change — a bulk re-render or a purge — which is the
  scenario that would actually make delay expensive. The cheap insurance against that is to re-run this
  refetch periodically; it costs 1,360 requests and ~12 minutes.
* "Resurrected" is very likely not resurrection: 9 panoramas that read dead in August and answer now are
  more plausibly August's transient misses. That makes the August alive count a slight *under*count, and
  it is the mirror image of the bias `confirm_deaths` corrects on this side.

## Where the data lives

* `reports/data/2026-09-06-photometa-census.json` — full summary, the `decay` block, and all 1,360
  per-pano records including the re-probe flags.
* `reports/data/2026-08-09-photometa-census.json` — the manifest this replays.
* `reports/scripts/photometa_census.py` — `--refetch`, `sample_from_census`, `confirm_deaths`, `decay`.

## The tests that pin it

`tests/test_photometa_census.py`: `TestSampleFromCensus`, `TestDecay`,
`TestDeathsAreConfirmedBeforeTheyAreBelieved` and `TestTheRefetchCLI` cover the code on synthetic records;
`TestTheDecayReportMatchesTheArtifact` asserts every number in the prose above appears in the committed
artifact. Five mutations were checked against the first three (death rate taken over the sample instead of
over what was alive; `depth_lost` counting every death; re-probing the whole manifest; never recovering; a
left join booking an unfetched pano as dead) and each is caught.
