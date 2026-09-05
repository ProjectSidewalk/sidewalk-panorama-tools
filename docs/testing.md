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
The measured set is the production tree only — the ten modules at the repo root, in `downloaders/` and in
`log_analyzer/`. `reports/` is deliberately outside it: a large body of frozen one-off analysis with its own
dense tests, and averaging it in would let the scraper's number move several points unnoticed. `flag_panos/`
is out because its module scope writes files at import, and `assets/` is out because building the hero
figure is tooling about the repo rather than part of the scraper. `tests/test_coverage_config.py` pins that set exactly,
so adding a module is a deliberate measure-or-omit decision rather than silently either.

Two settings there are load-bearing, and losing either shows up as a *lower number* rather than as an error:

- **`branch = True`** — the gap that motivated the gate was an `if` that only ever went one way (three of the
  log analyzer's six alert rules never fired while every line around them was green).
- **`source = ${SIDEWALK_COVERAGE_ROOT-.}`, not `.`** — coverage resolves a relative source against each
  *process's* CWD, and the runner tests spawn subprocesses with `cwd=tmp_path`. That variable, plus
  `COVERAGE_PROCESS_START` and `COVERAGE_FILE`, is set by `tests/conftest.py`'s `pytest_configure`, and only
  when the parent is itself being measured. Break any of the three and `main()`, the argparse `type=`
  validators and the budget carve-out all read as dead code while nothing fails.

## What the suite covers

| Area | Files |
|---|---|
| Downloader: run flow, budgets, ledgers, crash/`SIGTERM` behaviour, the positional `log.csv` contract | `test_download_runner.py` |
| Depth phase: ledger semantics, error taxonomy, artifact format, budget flags | `test_depth_phase.py`, `test_depth_helpers.py` |
| GSV stitching and the tile endpoint's behaviour, pinned against captured bytes | `test_gsv_stitcher.py`, `test_gsv_tile_contract.py`, `test_image_downloaders.py` |
| The image ledger contract at both ends: which downloader answers are permanent and which raise, the Mapillary error-envelope shapes measured on 2026-09-05, and a real Mapillary response driven through the dispatcher into `pano_id_log.csv` | `test_image_downloaders.py`, `test_download_runner.py` |
| Cropper: intake, the crop loop's failure taxonomy and count reconciliation, `predict_crop_size` pins | `test_crop_runner.py` |
| The CSV/JSON file intakes as one contract, measured against `pd.read_csv` before pandas was dropped | `test_csv_intake.py` |
| Log analyzer, and that its column list moves with the writer's | `test_log_analyzer.py` |
| The offline depth-artifact migrator | `test_migrate_depth_artifacts.py` |
| The [`fover` repair pass](ops.md#repairing-fover-era-panoramas): the decision table, the byte-for-byte survival of every refusal, ledger semantics, the recovery metric, and the CLI surface | `test_refetch_panos.py` |
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
