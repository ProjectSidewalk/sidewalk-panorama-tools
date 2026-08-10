# The backup store holds 99% of what Google dropped — but not always in the label's frame

**2026-08-10** · Phase 1 desk study for the cropper work package (#54, #32) · amends the
[pre-registration](2026-08-09-crop-priors-prereg.md) §3 corpus spec

> **Reproduce offline:** `pytest tests/test_store_coverage.py` (header reader cross-checked against
> Pillow, plus the findings pinned against the committed JSON). **From source** — needs the store
> mounted, so run it on makelab2, not a laptop:
> ```bash
> python reports/scripts/store_coverage.py \
>     /m-makeabilitylab/makeabilitylab/sidewalk_panos/Panoramas \
>     --census reports/data/2026-08-09-photometa-census.json --probed <today> \
>     --write reports/data/<date>-store-coverage.json
> ```

## The question

The [photometa census](2026-08-09-photometa-census.md) measured **47.9% pano survival at Google**,
which is why the pre-registration sources dead-pano pixels from a backup store rather than from
Google. But that spec was written against a store nobody had measured, and it showed:

* the corpus over-draw was calibrated at ~1.7×/2.2×/3× from **Google** survival — the wrong
  constraint if the store is the actual source;
* §3 pointed at the production pano store over SFTP, needing credentials that were an open ask on
  PR #79;
* and the census's headline **"0.0% dims drift"** compared `gsv_data`'s stored dims against
  **Google's** served dims. Neither of those is the JPEG a crop is cut from. Nobody had compared
  our own stored image against anything, which is precisely what #77's dims preflight is about.

Mikey's note that the EC2 backup panos are synced to makelab2 made all three answerable at once.

## Method

The store is `/m-makeabilitylab/makeabilitylab/sidewalk_panos/Panoramas` on makelab2 (symlinked as
`~/ProjectSidewalk/panos`): **15 TB, 54 cities**, laid out `<city>/<pano_id[:2]>/<pano_id>.jpg`
alongside legacy `.depth.txt` and `.xml`. All six study cities are present.

The probe list is **not drawn here** — it is read verbatim out of the committed photometa census's
per-pano records, so the same 1,360 panos are examined and the two studies cross-tabulate directly.
Dimensions come from the JPEG's **SOF header only** (no decode, no Pillow), which is what makes
sweeping a 15 TB store cheap; the header reader is cross-checked against Pillow in the tests.

## Coverage: the store is not the binding constraint

| | n | on store |
|---|---|---|
| **dead at Google** | 709 | **703 (99.2%)** |
| alive at Google | 651 | 651 (100.0%) |
| all sampled | 1,360 | 1,354 (99.6%) |

Dead-at-Google coverage holds where it would break first — the legacy era, where only 33.2% of
panos still exist at Google:

| era of earliest label | dead panos | on store |
|---|---|---|
| legacy (< 2021) | 227 | 222 (**97.8%**) |
| mid | 278 | 277 (99.6%) |
| post-179 | 204 | 204 (100.0%) |

By city, dead-pano coverage is 100% for amsterdam, columbus-oh, newberg-or and oradell-nj; 99.2%
for cdmx; **96.9% for seattle-wa**, which supplies all but one of the six misses. The six panos
absent from both Google and the store are enumerated in the committed JSON — those labels are
unreachable by any route and the corpus spec logs them as such.

Stored JPEGs run p10 5.5 MB / p50 9.7 MB / p90 13.2 MB.

## Frame: the store is a scrape-time archive, and it shows

Reading every recovered JPEG's SOF header (1,354 read, **0 unreadable**):

| comparison | match | differ |
|---|---|---|
| store JPEG vs `gsv_data` stored dims | 1,291 / 1,353 (**95.4%**) | **62** |
| store JPEG vs Google's served dims (alive panos) | 623 / 651 (95.7%) | 28 |

| store JPEG dimensions | count |
|---|---|
| 16384 × 8192 | 1,030 |
| 13312 × 6656 | 318 |
| 3328 × 1664 | 6 |

The mismatch is overwhelmingly one-directional — **61 of 62** are `gsv_data` saying 16384×8192
where the store holds 13312×6656 (the reverse happens once). That is the expected signature of an
archive: the store keeps whatever Google served *at scrape time*, and Google has since re-served
those panos larger.

**This is the number the photometa census could not see.** Its 0.0% was a true statement about
`gsv_data` tracking Google faithfully *for panos that still exist*; it says nothing about the image
on our disk. Against that image the disagreement is **4.6%**, and it independently corroborates the
[era replay study](2026-08-09-era-replay-study.md)'s "frame-change slice" (~3% of in-window
`pano_y` misses invert to an implied height of 6656 while the row now serves 8192). Two different
measurements, same phenomenon.

So **#77's dims preflight has a real hit rate on production data, not a near-zero one**, and §3's
dims-mismatch exclusion is a material filter rather than a formality.

## What this changes in the pre-registration

1. **The store becomes the primary pixel source, for every stratum** — not a fallback for dead
   panos. It covers 99.6% of the sample against Google's 47.9%, needs no download, and gives every
   era the same sourcing path instead of one path for live panos and another for dead ones.
2. **The 1.7×/2.2×/3× over-draw is retired.** It priced Google survival. Against a ~99% source the
   right allowance is a few percent, and the era-graded part disappears entirely (97.8% → 100.0%
   across eras, versus 33.2% → 60.0% at Google).
3. **The `PS_SFTP_*` credential ask is withdrawn.** makelab2 is key-authenticated and reachable
   now.
4. **The dims exclusion is quantified at ~4.6%** and must be applied against the *store's* JPEG,
   not against `gsv_data`.
5. **Endpoint 2 is unaffected.** The tilt regression needs photometa pitch/roll, which exists only
   for panos alive at Google, so it still runs at n ≈ 310 on a survival-selected subsample. Store
   coverage solves pixels, not covariates — worth stating plainly, because "99% coverage" invites
   the assumption that the survival problem is gone. It is gone for imagery only.

## Wrong turns

* **The first instinct was to treat 4.6% as contradicting the photometa census.** It does not; the
  two measure different pairs. The lesson is about how the census's finding was *worded*: "0.0% of
  alive serve different dims than stored" reads as "dims drift is a non-issue" unless you notice
  which two things were compared. The pre-registration's §1 footnote and this report now name the
  pair explicitly in both directions.
* **Coverage was nearly reported as a single 99.6% number.** Split by alive/dead it is the same
  headline, but only the dead half is decision-relevant — that is the population with no
  alternative source — and a pooled figure would have been flattered by the alive half, which is
  trivially 100% because those panos are downloadable anyway.

## Open questions

* **The sample is the census's 1,360, drawn stratified by city × era over labeled panos.** Coverage
  of *unlabeled* panos, and of the 48 cities outside the study, is unmeasured. The Phase 2 corpus
  only draws from the six, so this is not a blocker.
* **Why the six absent panos are absent** (five Seattle legacy, one cdmx mid) is unknown — scrape
  failures, a pre-store era, or a purge. At 0.8% of the dead set it does not move the corpus, and
  the ids are committed so it can be chased later.
* **There is a third frame, and this report does not measure it — but the era study already did.**
  Placing a stored label on a store JPEG means scaling `pano_y × (store_height / recorded_height)`,
  which is only valid if `recorded_height` is the **click-time** height. The 4.6% above compares
  store against `gsv_data`-*now*, so it cannot see the case where a label was clicked at 6656,
  Google later re-served at 8192, `gsv_data` refreshed, and we happened to scrape late: store equals
  recorded, the preflight passes, and the coordinate is still scaled by 1.0 when it needed 1.23.

  That case is nevertheless bounded, because the `pano_y` replay is a direct test of it —
  `pano_y = h/2 − round((h/2)·(pitch/90))` consumes `pano_height`, so an exact replay proves the
  recorded height is the height that produced the stored pixel. Corpus-wide that is
  **433,866 / 436,350 = 99.43% exact**: 2,454 misses inside the bug window (5.01%), 28 post-fix
  (0.065%), and 2 pre-179 (the corrupt rows). Inside the bug window a y-miss is ambiguous between
  the corrupt canvas/POV record — the dominant explanation — and a genuine frame change; the era
  study's frame-change slice puts the latter at ~3% of those misses, i.e. of order **70 labels,
  ~0.016% of the corpus**. Disentangling the two inside the window is the only part still open, and
  §3's exclusions already drop the labels it would touch.

## Where everything lives

| Artifact | Path |
|---|---|
| Coverage + frame numbers, per-pano records (committed) | `reports/data/2026-08-10-store-coverage.json` |
| Analysis | `reports/scripts/store_coverage.py` |
| Tests + findings pins (11/11 mutants killed) | `tests/test_store_coverage.py` |
| The sample this probe reuses | `reports/data/2026-08-09-photometa-census.json` |

---

*Written with [Claude Code](https://claude.com/claude-code) (claude-opus-5[1m]). The corpus spec
had been sized against the constraint that was easy to measure rather than the one that binds; the
store had been sitting there the whole time, and one `ls` would have said so.*
