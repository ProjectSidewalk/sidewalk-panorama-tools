# Reports

Write-ups of data-driven investigations: what was measured, how, what came out, and what changed as a
result. One file per investigation, named `YYYY-MM-DD-topic.md`, with:

* `figures/` — plots and diagrams, same date prefix.
* `data/` — the computed results a report cites, small enough to commit and read in a diff.
* `scripts/` — the analysis that produced them, runnable from the repo root. Bulk inputs (multi-MB API
  dumps) are cached under `scripts/.cache/` and gitignored, never committed; the script re-fetches them.

These exist because conclusions about external behaviour — Google's endpoints, imagery quirks, thresholds
calibrated against real data — get re-argued months later when nobody can reproduce the original
measurement. An issue thread is not replicable and does not fail CI when it stops being true.

## What a report should carry

* **The question**, and why it mattered at the time.
* **Method** — exactly how the measurement was taken, and enough detail to repeat it. Say what was swept
  versus sampled; the difference has already burned us once.
* **The numbers**, in tables. Not "roughly 60%" — the counts.
* **Where the data lives.** Every claim should point at committed fixtures or a committed measurement file
  (see `tests/fixtures/tiles/`), not at a paragraph.
* **The tests that pin it**, so the finding fails CI if it stops holding.
* **What changed in the code**, with the commit.
* **Wrong turns.** Explicitly. The reasoning that produced a wrong answer is the part most likely to be
  repeated, and it is the part that never survives into a commit message.
* **Open questions**, so a reader knows what is settled and what isn't.

## Relationship to tests and fixtures

A report is the narrative; the tests and fixtures are the enforcement. Neither replaces the other:

* Raw measurements go in the repo as data (e.g. `tests/fixtures/tiles/fover_band_map.json`), with the metric
  and its rationale written into the file.
* Real captured bytes go in as fixtures with a `manifest.json` recording provenance.
* Assertions over that data go in the normal suite; live re-checks against the external service go behind an
  opt-in env var so CI stays network-free.
* The report links all three together and explains *why* they are shaped that way.

## Index

| Date | Report | Outcome |
|---|---|---|
| 2026-08-07 | [CBK tile resolution and the `fover` parameter](2026-08-07-cbk-tile-resolution.md) | Dropped `fover`; recovered full zoom-5 resolution. Recommends against re-downloading the store. Issues #73, #74; PR #68. |
| 2026-08-09 | [Cropper consumer requirements](2026-08-09-cropper-consumer-requirements.md) | Requirements survey of RampNet 2.0 / validator-ai / tagger-ai / sidewalk-ai-api; sets the pre-registered thresholds for the #54/#32 studies (placement ≤ 0.5°, margin 3–4.5× object half-extent). |
