# Plan: a completeness check for panoramas already on disk (#239, follow-up 3)

Written 2026-11-14 from a reading of master `eb9bbc6`. A plan for Jon's approval, not a record of work done.
Follow-up 3 of [#239](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/239). Revised the
same day after the PR #240 review (Fable 5.2): an override for a false refusal, the `log.csv` column, store
mode, and the quarantine's order and filesystem.

## The gap

All three HTTP downloaders return `DownloadResult.skipped` on `os.path.isfile(<id>.jpg)`, and the runner then
ledgers `1` with a blank `fetched_at`. The file is never opened. **Store mode has the same hole**:
`_partition_for_pull` puts a pano whose final path `isfile` into `local` and ledgers it unread. So does the
depth phase for an existing `.depth.npz` (out of scope here, decision D5).

The runner's own writes are atomic (`.part` and rename) and checked before the rename, so this is a hole for
files **another tool** put there: a store assembled partly by another downloader (Sacramento: 830 files), a
copy that died partway, an old pre-atomic file whose ledger row was deleted. A truncated file ledgered `1` is
permanent: it is the resume marker, nothing refetches it, and the cropper fails on its labels every run. A run
killed between the atomic save and the ledger append also leaves an unledgered file, but a whole one.

The check exists: `store_sftp.is_complete_jpeg` (a readable SOF plus an EOI that closes the main image, up to
64 KB of trailer, `common.jpeg_is_complete`). It reads the header and seeks near the end: two small reads per
file, not a decode. It proves a whole JPEG, not the original rendition, and it **can refuse a good file**: a
real JPEG with more than 64 KB after its EOI fails. That has happened before (7 of 7 photospheres refused
nightly until the trailer window was added, `reports/2026-11-09-photosphere-404s-and-trailers.md`).

## Two parts

### A. The runner checks before it ledgers a file it found on disk

Only files on disk **and not in the ledger** reach this path, so a mature city pays nothing measurable; a
store copied in from elsewhere is exactly the set that needs checking. A truncated file **already ledgered**
is never reached, deliberately, and a test pins that (so nobody "fixes" it by re-checking every skip).

- One helper, `common.stored_pano_is_complete(path)`, moved out of `store_sftp` so the runner does not import
  the SFTP module for a file check. Called in two places: the scrape loop on `DownloadResult.skipped`
  (the path is `<storage>/<id[:2]>/<id>.jpg`, the one layout every downloader writes), and store mode's
  `pull_panos_from_store` on the `local` list `_partition_for_pull` returns. **Not inside
  `_partition_for_pull`**: `pull_depth_from_store` shares it with `DEPTH_SUFFIX`, and a JPEG check there would
  refuse every `.depth.npz`.
- **Complete**: unchanged (ledger `1`, blank stamp).
- **Incomplete**: not ledgered, the file not touched. One ERROR per pano naming the path. It is a new per-run
  counter, `incomplete`, which is **not** a raise and **not** an answer (so `images-no-success` and field 20
  are unchanged), and **neither counts toward nor resets** #113's or #162's breakers, like a skip.
- **`log.csv` fields 7–11:** `download_pano` returns only a result code and `total_completed` is the sum of the
  four counters, so the runner has to decide the column (decision D2). Recommended: count it in field 9
  (failed), which is what an unledgered, retried-next-run outcome already is, and in no new column.
- **End of phase:** one WARNING on both channels and a run condition `incomplete-on-disk`, detail the first
  path and the count, so the night fails under the queue (decision D3).
- **The override** for a false refusal: an operator who has checked the file appends its `1` row to
  `pano_id_log.csv` by hand (documented in `docs/ops.md`), after which it is never checked again. Recommended
  in addition: a PIL decode as a second opinion before refusing (decision D4), which must be
  `Image.open(...).load()`: `open` alone and `verify()` do not catch a truncated JPEG (to be pinned by a test). A file that decodes whole is
  ledgered with an INFO line naming the trailer size; only a file that fails both is refused.
- Leaving a refused file means it is reported on every run until someone acts. That is deliberate: the store
  is an archive (`docs/ops.md`), and the runner must not delete or replace a stored file on its own judgement.

### B. A sweep for the operator: `verify_store.py`

Read-only by default, for a store assembled by another tool before its first scrape, or after a copy. Shaped
like `scan_black_bands.py` (resumable ledger, `--max-runtime`, `walk_store_panos`, never a hand glob). Covers
ledgered files too, which part A never reaches, so it is what Sacramento's 830 need.

```
python3 verify_store.py <storage-dir> [--max-runtime MINUTES] [--report CSV] [--quarantine DIR]
```

- Writes `pano_id,verdict,bytes` for each incomplete file to `--report`. Exit codes: 0 nothing incomplete,
  1 incomplete files found (read-only) or anything left in place (`--quarantine`), 2 usage, 3 refused.
- `--quarantine DIR`, in this order and under the queue lock:
  1. **Remove every bad pano's ledger row first, in one rewrite**, through a shared `rewrite_ledger` with a backup named for this tool
     (`.verify-<stamp>.csv`; today `rewrite_ledger` hard-codes `.readmitted-`, so it gains a suffix
     parameter). In the other order, an interrupted run leaves a `1` row with no file, never refetched.
  2. **Then move the files** to `DIR/<id[:2]>/<id>.jpg`. Never overwrite; a collision is listed and the file
     left. `DIR` is usually on another filesystem, so a move is a copy plus a delete: copy, verify the copy
     byte-for-byte, then delete. This makes the sweep the first tool to delete a `.jpg` from the archive, and
     it does so only after the bytes exist elsewhere (decision D6).
  3. **Siblings:** a `.w8192.jpg` display copy goes with its pano (it was made from the bad file). A
     `.depth.npz` stays: it came from photometa, not from the image, and is checked separately (D5).
- Refuses `--quarantine` inside the storage root, since a quarantined file there would be found by the walker.

## Tests

- Part A, scrape and store mode: a truncated file with no ledger row is not ledgered, is byte-identical
  afterwards, lands in field 9, is noted once as the condition, leaves field 20 and both breakers unchanged; a
  complete file is ledgered as before; a 111-byte camera trailer is complete; a 100 KB trailer on a JPEG that
  decodes is ledgered under D4 and refused without it; a truncated **ledgered** file is not reached.
- Part B: read-only by default (the store tree's hash is unchanged) and exits 1 when it finds one;
  `--quarantine` removes the row before moving, keeps a backup, verifies the copy before deleting, moves the
  display copy and not the depth artifact, lists a collision, is refused inside the root, and resumes across
  `--max-runtime`. A failure injected between the two steps leaves no `1` row without a file.
- The new condition joins `DownloadRunner.RUN_CONDITIONS` and `scrape_queue.CONDITION_LABELS` together (a
  test pins the two equal).
- Coverage: `verify_store.py` joins the measured set (`tests/test_coverage_config.py`).

## Docs

`docs/downloader.md` (the pre-fetch section's first bullet, and its run-conditions table), `docs/ops.md` (the
condition, the override and the sweep, beside "The store is an archive"; field 9's wording in the `log.csv`
columns table), `.claude/rules/downloader.md`, `.claude/rules/store-repair.md`
(the sweep), and the `CLAUDE.md` command list.

## Decisions for Jon

- **D1. Report, don't repair, in the runner.** Recommended. The alternative (the runner quarantines and
  refetches) makes the nightly scrape the first code since the archive rule to move a stored file without an
  operator.
- **D2. The column:** field 9 (recommended), or a new field 21, which is #182's widening question.
- **D3. Fail the night** (`incomplete-on-disk` as a run condition). Recommended: a broken file is sitting in
  the store unledgered and stays until someone acts.
- **D4. A decode as a second opinion** before refusing. Recommended, given the trailer rule's history; it
  costs one decode per refused file, which is rare.
- **D5. Depth artifacts** (`.depth.npz` found on disk, `is_complete_npz`): same treatment, later, or never.
  Recommended later, as its own change: the depth phase has its own ledger and its own conditions.
- **D6. Part B deletes after a verified copy.** Recommended, with the copy verified. The alternative is
  same-filesystem quarantine only (a rename), which is refused when `DIR` is elsewhere.
- **D7. Build B now, or only A?** A covers anything the runner ledgers from here on; B is for stores already
  assembled, so Sacramento needs B. Recommended both. **B's quarantine depends on A:** without A, a scrape run
  between B's two steps finds the file unledgered and re-ledgers it `1` unread.
