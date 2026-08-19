# Tests

```bash
pip3 install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests
```

CI runs exactly this on Ubuntu 22.04 / Python 3.10 for every push to `master` and every pull request
([`.github/workflows/tests.yml`](../.github/workflows/tests.yml)). There is no linter configured.

## What the suite covers

| Area | Files |
|---|---|
| Downloader: run flow, budgets, ledgers, crash/`SIGTERM` behaviour, the positional `log.csv` contract | `test_download_runner.py` |
| Depth phase: ledger semantics, error taxonomy, artifact format, budget flags | `test_depth_phase.py`, `test_depth_helpers.py` |
| GSV stitching and the tile endpoint's behaviour, pinned against captured bytes | `test_gsv_stitcher.py`, `test_gsv_tile_contract.py`, `test_image_downloaders.py` |
| Cropper: intake, the crop loop's failure taxonomy and count reconciliation, `predict_crop_size` pins | `test_crop_runner.py` |
| Log analyzer, and that its column list moves with the writer's | `test_log_analyzer.py` |
| The offline depth-artifact migrator | `test_migrate_depth_artifacts.py` |
| The desk studies under `reports/scripts/`, and the artifacts they commit | `test_*_census.py`, `test_*_study.py`, `test_studyfmt.py`, `test_committed_data_files.py`, `test_reports_index.py` |
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

**Committed-artifact tests are not code tests.** Pinning a finding against `reports/data/*.json` proves nothing
about the function that produced it — the artifact was generated *by* the current code, so a revert stays
green. Every finding needs a synthetic, code-level test beside its corpus pin. Three mutation sweeps in a row
surfaced survivors of exactly this shape.

Tests asserting POSIX file modes skip themselves on Windows; everything else runs on a Windows dev box.
