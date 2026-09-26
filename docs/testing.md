# Tests

```bash
pip3 install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests

# ... with the coverage report CI publishes and gates on
python3 -m pytest tests --cov --cov-report=term-missing
```

CI runs exactly this on Ubuntu 22.04 / Python 3.10 for every push to `master` and every pull request
([`.github/workflows/tests.yml`](../.github/workflows/tests.yml)). There is no linter configured.

## Coverage

CI reports coverage on every run and fails the build below the `fail_under` floor in
[`.coveragerc`](../.coveragerc) ([#57](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/57)).
The measured set is the production tree only — every module at the repo root, in `downloaders/` and in
`log_analyzer/`. `reports/` is deliberately outside it: a large body of frozen one-off analysis with its own
dense tests, and averaging it in would let the scraper's number move several points unnoticed. `flag_panos/`
is out because its module scope writes files at import, and `assets/` is out because building the hero
figure is tooling about the repo rather than part of the scraper. `tests/test_coverage_config.py` pins that set exactly,
so adding a module is a deliberate measure-or-omit decision rather than silently either.

Two settings there are load-bearing, and losing either shows up as a *lower number* rather than as an error:

- **`branch = True`** — the gap that motivated the gate was an `if` that only ever went one way (three of the
  log analyzer's alert rules never fired while every line around them was green; there were six at the
  time of that measurement and there are nine now).
- **`source = ${SIDEWALK_COVERAGE_ROOT-.}`, not `.`** — coverage resolves a relative source against each
  *process's* CWD, and the runner tests spawn subprocesses with `cwd=tmp_path`. That variable, plus
  `COVERAGE_PROCESS_START` and `COVERAGE_FILE`, is set by `tests/conftest.py`'s `pytest_configure`, and only
  when the parent is itself being measured. Break any of the three and `main()`, the argparse `type=`
  validators and the budget carve-out all read as dead code while nothing fails.

## What the suite covers

| Area | Files |
|---|---|
| Downloader: run flow, budgets, ledgers, crash/`SIGTERM` behaviour, the positional `log.csv` contract | `test_download_runner.py` |
| The image ledger's [two legal row widths](ops.md#fetched_at-and-the-two-row-widths): a mixed-width file reading as the union of its ids, a timestamped `0` row staying terminal, an existing two-column header left alone, a four-field row still counting as damage, the stamp carrying a UTC offset, and a `skipped` verdict writing a blank stamp that the reader still counts | `test_download_runner.py` (`TestTheFetchTimestamp`) |
| Depth phase: ledger semantics, error taxonomy, artifact format, budget flags | `test_depth_phase.py`, `test_depth_helpers.py` |
| Depth pacing (adaptive floor/backoff/jitter) and the cross-run block latch | `test_depth_pacing.py` |
| GSV stitching and the tile endpoint's behaviour, pinned against captured bytes | `test_gsv_stitcher.py`, `test_gsv_tile_contract.py`, `test_image_downloaders.py` |
| The image ledger contract at both ends: which downloader answers are permanent and which raise, the Mapillary error-envelope shapes measured on 2026-09-05 and the Panoramax item and 404 body measured on 2026-09-08, and a real response from each source driven through the dispatcher into `pano_id_log.csv` | `test_image_downloaders.py`, `test_download_runner.py` |
| Cropper: intake, the crop loop's failure taxonomy and count reconciliation, `predict_crop_size` pins, the equirectangular unit primitives and the token-stream guard that keeps 360/180 out of the rest of the module, label registration measured against planted pixels, the window width's axis on a non-2:1 pano, and agreement with the gold-annotation instrument's independent window derivation | `test_crop_runner.py` |
| The label type either/or (#123): a name filing under the numeric directory, the legacy id winning when both are present, an unrecognised name **or id** counting as one error rather than a guessed shard, padded names, an error message naming both spellings, and the docs table checked against `LABEL_TYPE_IDS_BY_NAME` pair for pair | `test_crop_runner.py` |
| The [systemic-failure alarm](cropper.md#when-errors-dominate-systemic-failure) (#136): both output channels on a dominated run, silence on a healthy one, on one bad row in ten and on each zero-work shape, the threshold's boundary derived from the constant, and the counts still reconciling on every path it fires on | `test_crop_runner.py` |
| The CSV/JSON file intakes as one contract, measured against `pd.read_csv` before pandas was dropped | `test_csv_intake.py` |
| [Store mode](downloader.md#pulling-from-the-project-sidewalk-pano-store) (`--from-store`): the batch text and its `-`/unprefixed/`./` semantics, the verifiers (a half transfer, trailing bytes, an embedded thumbnail's EOI), redaction layer by layer, and the ledger and `log.csv` effects driven through `main()`. Run against a Python stand-in for `sftp -b -` that models batch-mode abort semantics over a local directory tree, plus `TestAgainstRealOpenSSH`, which drives the real client as `sftp -D <sftp-server>` (no network) and skips where the server binary is not installed | `test_store_sftp.py`, `test_download_runner.py` (`TestStoreMode`, `TestStoreModeCli`) |
| The nightly queue: manifest parsing, ordering and rotation, both budgets, the lock, exit codes, the [extra passes](downloader.md#extra-passes) and the run-summary channel they read (the runner's side of it is in `test_download_runner.py` and `test_depth_pacing.py`) | `test_scrape_queue.py` |
| The alarm channel: what the sink receives and when, the exit code being the command's (and 4 for a delivery failure after a clean run), truncation on line boundaries with an exact marker, the SNS subject constraints, the `--log` line, and SIGTERM forwarded exactly once | `test_cron_notify.py` |
| Log analyzer, and that its column list moves with the writer's | `test_log_analyzer.py` |
| The [cvMetadata schema tripwire](api-fields.md#checking-the-contract-against-a-live-deployment): the verdict (a missing field named, an added one ignored, the either/or label-type pair), that the required list moves when `CropRunner`'s constants move, reading the field names off a streamed payload prefix, and the exit codes | `test_cvmetadata_schema.py` |
| The offline depth-artifact migrator | `test_migrate_depth_artifacts.py` |
| The display copy of a wide panorama: naming, the two writers, the switch that keeps both downloaders' hooks off and the sweep and primitives on, the one store walker and the guard against a second, what `sidecar_is_current` can and cannot see, the shared decompression-bomb ceiling, and the sweep itself | `test_downscaled_sidecar.py` |
| The [`fover` repair pass](ops.md#repairing-fover-era-panoramas): the decision table, the byte-for-byte survival of every refusal (the display copy included), the copy being rewritten from the imagery that replaced it but never created where there was none, ledger semantics, the recovery metric, and the CLI surface | `test_refetch_panos.py` |
| The desk studies under `reports/scripts/`, and the artifacts they commit | `test_*_census.py`, `test_*_study.py`, `test_depth_backfill_report.py`, `test_studyfmt.py`, `test_committed_data_files.py`, `test_reports_index.py` |
| The [re-render probe](../reports/2026-09-06-rerender-probe.md): displacement, tone and sharpness on synthetic pairs with known answers — including the gain-squared confound that would otherwise read a brightened panorama as a sharpened one — plus the three-component pose fit (a uniform vertical offset is its own component, never a tilt and never dropped; the fit is reported for every row, gated on nothing), the movement verdict against the probe's own gate, and every cell of the report's table and every count in its prose regenerated from the committed artifacts, the click-noise sigma it quotes included | `test_rerender_probe.py`, `test_rerender_probe_report.py` |
| The pose-drift re-render proxy in the standing decay census: the wrap applied to the *difference*, an axis neither census carried reported undefined rather than zero, one panorama counted once however many axes moved, the any-axis union that lets "the same panoramas moved on both axes" be asserted rather than inferred from equal counts, and `--resummarize` regenerating a re-fetch census's `decay` block offline | `test_photometa_census.py` (`TestPoseDrift`, `TestResummarize`) |
| That the docs' internal links and anchors resolve, and that cited `docs/` paths exist | `test_docs.py` |
| That the README's hero figure still builds against the current cropper, and isn't stale | `test_make_banner.py` |

## Three things that are deliberately unusual

**The suite is network-free**, and `streetlevel` is stubbed. One module is the exception:
`test_streetlevel_api.py` imports the *real* `streetlevel` to pin the handful of API details
`downloaders/gsv.py` depends on — the mocked suite can't catch drift there, because the stub accepts any
arguments. It skips itself when `streetlevel` isn't installed (its `pyfrpc` dependency has no wheel on Windows
or macOS and needs a C compiler there).

**Live re-checks against external services sit behind an opt-in env var**, so CI stays offline while the
capture scripts that produced `tests/fixtures/` remain runnable on demand.

That is also why the one check of a *live* contract is not a test. `check_cvmetadata_schema.py` asks a
deployment which fields it serves and compares them against what `CropRunner` requires; it lives outside
`tests/` precisely so the suite above stays offline. The suite cannot fail on a contract it never touches,
and for 16 days in September 2026 it did not
([#135](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/135)).

**Committed-artifact tests are not code tests.** Pinning a finding against `reports/data/*.json` proves nothing
about the function that produced it — the artifact was generated *by* the current code, so a revert stays
green. Every finding needs a synthetic, code-level test beside its corpus pin. Three mutation sweeps in a row
surfaced survivors of exactly this shape.

Tests asserting POSIX file modes skip themselves on Windows; everything else runs on a Windows dev box.
