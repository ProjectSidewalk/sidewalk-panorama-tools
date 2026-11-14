# Plan: a completeness check for panoramas already on disk (#239, follow-up 3)

Written 2026-11-14 from a reading of master `eb9bbc6`. A plan for Jon's approval, not a record of work done.
Follow-up 3 of [#239](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/239).

## The gap

All three HTTP downloaders return `DownloadResult.skipped` on `os.path.isfile(<id>.jpg)` and the runner then
ledgers `1` with a blank `fetched_at`. The file is never opened. The runner's own writes are atomic (`.part`
and rename) and checked before the rename, so this is only a hole for files **another tool** put there: a
store assembled partly by another downloader (Sacramento: 830 files), a copy that died partway, an old
pre-atomic file whose ledger row was deleted. A truncated file ledgered `1` is permanent: it is the resume
marker, nothing refetches it, and the cropper fails on its labels every run.

The check already exists: `store_sftp.is_complete_jpeg` (a readable SOF plus an EOI that closes the main
image, up to 64 KB of trailer, `common.jpeg_is_complete`). It reads the header and seeks near the end, so it
costs two small reads per file, not a decode.

## Two parts

### A. The runner checks before it ledgers a skip

Only files that are on disk **and not in the ledger** reach the skip path, so on a mature city this is rare
and costs nothing measurable; on a store copied in from elsewhere it is exactly the set that needs checking.

- In `download_panorama_images`, on `DownloadResult.skipped`, call `common.stored_pano_is_complete(path)` (a
  thin wrapper, moved to `common` so the runner does not import the SFTP module for a file check).
- **Complete**: unchanged (ledger `1`, blank stamp).
- **Incomplete**: do **not** ledger, do **not** touch the file, count it as a new outcome `incomplete` (not a
  raise, not an answer, so `images-no-success` and field 20 are unchanged), and log one ERROR per pano naming
  the path. At the end of the phase, one WARNING on both channels and a new run condition
  `incomplete-on-disk` with the first path in the detail, so the night fails under the queue.
- Leaving the file means the pano is reported again every run until someone acts. That is deliberate: the
  store is an archive (`docs/ops.md`), and the runner must not delete or replace a stored file on its own
  judgement. Each later run costs one check, not a request.

### B. A sweep for the operator: `verify_store.py`

Read-only by default, for a store assembled by another tool before it is first scraped, or after a copy.
Shaped like `scan_black_bands.py` (resumable ledger, `--max-runtime`, `walk_store_panos`).

```
python3 verify_store.py <storage-dir> [--max-runtime MINUTES] [--report CSV] [--quarantine DIR]
```

- Walks every stored pano (`common.walk_store_panos`, never a hand glob), ledgered or not, and writes
  `pano_id,verdict,bytes` for each incomplete one to `--report`.
- `--quarantine DIR` **moves** each incomplete file to `DIR/<id[:2]>/<id>.jpg` (never deletes, never
  overwrites, a collision is listed and left) and removes its row from `pano_id_log.csv` under the queue lock,
  with a backup, through the readmit tools' `rewrite_ledger`. The next run then fetches it fresh. Exit codes as
  `migrate_crop_store.py`: 0 done, 1 anything left in place, 2 usage, 3 refused.
- Refuses `--quarantine` inside the storage root (a quarantined file must not be found by the walker).

## Tests

- Runner: a truncated file on disk with no ledger row is not ledgered, the file is byte-identical afterwards,
  the condition is noted once with the first path, field 20 is unchanged; a complete file is ledgered as
  before; a file with a 111-byte camera trailer is complete (the photosphere fixture).
- A truncated file **already in the ledger** is not reached by part A (pinned, so nobody "fixes" it by
  re-checking every skip).
- `verify_store.py`: read-only by default (store tree hash unchanged); `--quarantine` moves, rewrites the
  ledger, keeps a backup, lists a collision; refused inside the root; resumable across `--max-runtime`.
- Coverage: `verify_store.py` joins the measured set (`tests/test_coverage_config.py`).

## Docs

`docs/downloader.md` (the pre-fetch section's first bullet becomes "checked before it is ledgered"),
`docs/ops.md` (a section for the condition and the sweep, beside "The store is an archive"),
`.claude/rules/downloader.md` and `.claude/rules/store-repair.md` (the sweep), the `CLAUDE.md` command list.

## Decisions for Jon

- **D1. Report, don't repair, in the runner.** Recommended. The alternative (the runner quarantines and
  refetches) makes the nightly scrape the first code since the archive rule to move a stored file without an
  operator.
- **D2. Fail the night** (`incomplete-on-disk` is a run condition). Recommended: it means a broken file is
  sitting in the store unledgered, and it stays until someone acts.
- **D3. Mapillary and Panoramax files only, or GSV too?** The check is the same; recommended for every
  source, since a copy can cut any file short.
- **D4. Build B now, or only A?** A closes the hole for anything the runner ledgers from here on; B is for
  stores already assembled. Sacramento needs B (its 830 files are already ledgered), so recommended both.
