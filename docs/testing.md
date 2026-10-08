# Tests

```bash
pip3 install -r requirements.txt -r requirements-dev.txt -c constraints.txt
python3 -m pytest tests

# ... with the coverage report CI publishes and gates on
python3 -m pytest tests --cov --cov-report=term-missing
```

CI runs exactly this on Ubuntu 22.04 / Python 3.10 for every push to `master` and every pull request
([`.github/workflows/tests.yml`](../.github/workflows/tests.yml)), against the production box's pinned versions
([`constraints.txt`](ops.md#refreshing-constraintstxt), #167). A second workflow,
[`tests-latest.yml`](../.github/workflows/tests-latest.yml), runs the same suite weekly with no constraints, so
the latest releases are tested too. It never runs on a pull request and gates nothing. Both jobs have a 20-minute timeout, so a test
that hangs instead of failing is a red X rather than a runner held for six hours. There is no pytest timeout,
so a test that loops on an unbounded clock (an instant stand-in `run_one` under a real window) must bound it
itself: a `FakeClock` the stand-in advances, or a call cap that raises. There is no linter configured.

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
  validators and the budget carve-out all read as dead code while nothing fails. Every `omit` pattern is
  anchored to the same root (`${SIDEWALK_COVERAGE_ROOT-.}/tests/*`, ...) for the same reason, and
  `test_coverage_config.py` asserts it: a bare `tests/*` means `<tmp_path>/tests/*` in such a child, and one
  that loaded `tests/conftest.py` from there once took CI's figure from 99.46% to 98.07% with nothing failing.

## What the suite covers

| Area | Files |
|---|---|
| Downloader: run flow, budgets, ledgers, crash/`SIGTERM` behaviour, the positional `log.csv` contract, and both image breakers — #113's permanent-verdict one and GSV's push-back one (#162: what counts and resets, the latch and pace forfeit, both channels, 24 requests for three refused panos through the real fan-out, and a pure-403 block met at the zoom probe tripping it through a real `requests.Session`) | `test_download_runner.py` |
| The image ledger's [two legal row widths](ops.md#fetched_at-and-the-two-row-widths): a mixed-width file reading as the union of its ids, a timestamped `0` row staying terminal, an existing two-column header left alone, a four-field row still counting as damage, the stamp carrying a UTC offset, and a `skipped` verdict writing a blank stamp that the reader still counts | `test_download_runner.py` (`TestTheFetchTimestamp`) |
| Depth phase: ledger semantics, error taxonomy, artifact format, budget flags | `test_depth_phase.py`, `test_depth_helpers.py` |
| Depth pacing (adaptive floor/backoff/jitter) and the cross-run block latch | `test_depth_pacing.py` |
| GSV stitching and the tile endpoint's behaviour, pinned against captured bytes; the tile retry policy (a 429/403, or an interstitial at any status, never retried, a 404 not retried, 5xx bounded, `backoff` silent), a refusal abandoning the fan-out at `thread_count` requests through the real fan-out, one log line per pano, `pushback_reason`'s table, and the zoom probe's refusals (403, an interstitial redirect) through a real session (#162) | `test_gsv_stitcher.py`, `test_gsv_tile_contract.py`, `test_image_downloaders.py` |
| The image ledger contract at both ends: which downloader answers are permanent and which raise, the Mapillary error-envelope shapes measured on 2026-09-05 and the Panoramax item and 404 body measured on 2026-09-08, and a real response from each source driven through the dispatcher into `pano_id_log.csv` | `test_image_downloaders.py`, `test_download_runner.py` |
| Cropper: intake, the crop loop's failure taxonomy and count reconciliation, `predict_crop_size` pins, the equirectangular unit primitives and the token-stream guard that keeps 360/180 out of the rest of the module, label registration measured against planted pixels, the window width's axis on a non-2:1 pano, agreement with the gold-annotation instrument's independent window derivation, opt-in [sizing rule v3](cropper.md#sizing-rule-v3-opt-in) (#32) with the default window byte-identical, and `crop_rule.json`'s sticky rule history, read and rewritten beside the store's city and manifest keys in the per-city marker (a manifest-era marker's JSON bool is not "unreadable") | `test_crop_runner.py` |
| The label type either/or (#123): a name filing under the numeric directory, the legacy id winning when both are present, an unrecognised name **or id** counting as one error rather than a guessed shard, padded names, an error message naming both spellings, and the docs table checked against `LABEL_TYPE_IDS_BY_NAME` pair for pair | `test_crop_runner.py` |
| The [systemic-failure alarm](cropper.md#when-errors-dominate-systemic-failure) (#136): both output channels on a dominated run, silence on a healthy one, on one bad row in ten and on each zero-work shape, the threshold's boundary derived from the constant, and the counts still reconciling on every path it fires on | `test_crop_runner.py` |
| [`--force`](cropper.md#re-cutting-a-store-with---force) (#83): a re-cut byte-identical to a fresh run's crop, `recut` annotating a success and staying out of the disjoint sum (checked against CropRunner's own `DISJOINT_OUTCOMES`), a failed re-cut leaving the old crop whole, `stale_kept` for an old crop a preflight skip, a missing pano or an unreadable pano leaves behind (and only under `--force`, and only when the crop is on disk), and the rule marker's warning saying a forced run is re-cutting rather than telling it to re-run with `--force` | `test_crop_runner.py` (`TestForceRecut`, `TestTheRuleMarker`) |
| The [production-crop-store guard](cropper.md#usage) (#83): every level of the production layout refused with nothing written, each signal alone, the numeric shards never listed, the scan's depth, an unreadable directory refused with exit 3 on both channels rather than crashed on, and a genuine formula store passing | `test_crop_runner.py` (`TestTheProductionCropStoreGuard`) |
| What the crop loop records rather than cuts: the bounded, per-kind-capped `crop.log` (#139) with its counts untouched at any cap; the [provenance manifest](cropper.md#the-provenance-manifest-crop_provenancecsv) (#111) on all three intakes, a row per landed crop and none for anything else, a row whole or not at all under a fault injected *below* the write buffer (failed and torn appends, a failed close, a torn row or header cut back at open), and a failing manifest never taking the run summary down; and `crop_rule.json`'s `provenance_manifest_no_known_gap`, turned false for good by a known gap | `test_crop_log_and_provenance.py` |
| The cropper's [content check](cropper.md#the-content-check-black_content) (#164): a label inside a black band withheld as `black_content` with nothing written (no crop, no `.part`, no provenance row) while the other crops stay byte-identical, `main()` exiting 0 and saying so on both channels, the strict `>` at the shipped 0.5, exact zero rather than a tolerance, the raw cut window measured (not the downscale, the mark or the pano), crops on disk never re-judged, `--force` keeping the old crop as `stale_kept`, its own warning budget with the counts untouched at any cap, the D4 shape's blind spot outside its bands pinned as a known limit, one `black_fraction` primitive shared with the stitcher, and all of it under `--sizing-rule v3` too (a mostly black v3 window withheld, judged raw before the downscale, and the H/6 nadir limit one box for both rules); a truncated pano decoded once rather than once per label, with one `cannot decode` line per pano under the `cannot_open` kind so a real write failure still shows, a resumed pano never decoded, buckets independent of label order, and `stale_kept` on that route under `--force`; and the JSON intake counting a row without `label_id`, a null, a string, a number or a list as one bad label each, refusing a non-array top level (from a file or from the server) with one error naming its source, and deduplicating on the int the loop files under while never collapsing rows with no usable id, with the dropped rows reported as one bounded line on both channels; and the CSV intake's dedupe pinned end to end through `main(-f *.csv)` (`7`/`07` under `--force` cut once with one provenance row, repeated blank or unparseable ids each their own error, the counts reconciling) | `test_crop_content.py` |
| The cropper's opt-in [tilt correction](cropper.md#the-tilt-correction-opt-in-191) (#191): off byte-identical with a pose beside the pano and the pose never read, registration read off the raster (a unique pixel planted at the rig position, the stored point derived by the inverse transform, the cut window holding it where `label_position_in_crop` says and centred on it, for xml and npz poses at the horizon, both poles and the seam, and the `--mark-label` dot on it), the window sized at the stored y under both rules, `no_pose` for an absent pose, an incomplete xml and a NaN npz (not an error, exit 0, both channels, a summary true for every shape, `stale_kept` under `--force`, its own warning budget, every disjoint bucket once with the key set exact), the pose looked up lazily and once per pano (a crop on disk `skipped_existing`, a finished store reading no pose), a lookup that raises or an xml naming an unknown encoding never ending the run, the frame preflight on the corrected y, beta per pose record (never per label era) and the tally line counting only the crops the correction cut, and the marker's beta keys, warning and sticky history, with a marker from before the keys read as uncorrected | `test_crop_tilt.py` |
| `pano_pose.py`, the production home of the #54 frame geometry: the moved functions against literals computed from the pre-move module, `tilt_geometry` a re-export and not a copy, beta scaling the pose (0 the identity, 1 the full transform, the first-order `-beta T(b) h / 180`), the xml and npz pose readers and every way each yields no pose, and the scrape-era rule (xml first, never falling through) | `test_pano_pose.py` |
| The CSV/JSON file intakes as one contract, measured against `pd.read_csv` before pandas was dropped, including the label CSV deduplicating on the int the crop is filed under (`7`/`07`/` 7` one label, blank or unparseable ids never collapsed, the dropped rows reported once on both channels) | `test_csv_intake.py` |
| [Store mode](downloader.md#pulling-from-the-project-sidewalk-pano-store) (`--from-store`): the batch text and its `-`/unprefixed/`./` semantics, the verifiers (a half transfer, trailing bytes, an embedded thumbnail's EOI), redaction layer by layer, and the ledger and `log.csv` effects driven through `main()`. Run against a Python stand-in for `sftp -b -` that models batch-mode abort semantics over a local directory tree, plus `TestAgainstRealOpenSSH`, which drives the real client as `sftp -D <sftp-server>` (no network) and skips where the server binary is not installed | `test_store_sftp.py`, `test_download_runner.py` (`TestStoreMode`, `TestStoreModeCli`) |
| The nightly queue: manifest parsing, ordering and rotation, both budgets, the lock, exit codes, the [extra passes](downloader.md#extra-passes) and the run-summary channel they read, the [run conditions](downloader.md#a-city-can-finish-ok-and-still-fail-the-night) that fail an ok night, the [store marker](ops.md#the-store-marker), and a stop that always kills the city (the runner's side of it is in `test_download_runner.py` and `test_depth_pacing.py`) | `test_scrape_queue.py` |
| The alarm channel: what the sink receives and when, the exit code being the command's (and 4 for a delivery failure after a clean run), truncation on line boundaries with an exact marker, the SNS subject constraints, the `--log` line, SIGTERM forwarded exactly once, and the alarm probe in `ops.md` run as written (it reaches the sink, and runs the nightly's interpreter without writing into its log) | `test_cron_notify.py` |
| Log analyzer, and that its column list moves with the writer's | `test_log_analyzer.py` |
| The [cvMetadata schema tripwire](api-fields.md#checking-the-contract-against-a-live-deployment): the verdict (a missing field named, an added one ignored, the either/or label-type pair), that the required list moves when `CropRunner`'s constants move, reading the field names off a streamed payload prefix, and the exit codes | `test_cvmetadata_schema.py` |
| The offline depth-artifact migrator | `test_migrate_depth_artifacts.py` |
| [Moving a pre-#159 crop store](cropper.md#moving-a-pre-159-store): a type directory moved whole or file by file, a file already at a destination left byte for byte with its source (a collision), the multiset of file bytes unchanged, `--dry-run` writing nothing and predicting the real run, a second run and a run after a kill finishing the job, the store files moved last, another city's or an unreadable marker and a root already named for `--city` refused with nothing touched, only label-type directories moved (any other all-digit directory and any symlink left and listed), a failed listing or `rmdir` counted rather than raised, the attribution to `--city` said on both channels, and the command line's 0/1/2/3 | `test_migrate_crop_store.py` |
| [One store per city](cropper.md#one-store-one-city) (#159): `--city` checked against `log_analyzer/cities.csv` read as a file (a `#` row, an unknown city, an unreadable roster, the spelling rule applied first; every other in-process test reads `tests/conftest.py`'s fixture roster, so an ops edit to the committed one cannot fail them), crops under `<crop-dir>/<city>/` with two cities in one `-o` never touching each other's crop and manifests keyed on `(city, label_id)`, the production guard at the city store, a pre-#159 flat root refused with nothing written (one listing of `-o`; an all-digit directory that is no label type's told only to be moved out by hand, never the migrator; a root named for its city, spelled with a trailing slash or reached through a link, told to point at its parent and never offered the migrator, with the spelled path named back when both it and the link's target match), and a flat store already named for its city adopted with nothing re-cut | `test_crop_store_layout.py` |
| The display copy of a wide panorama: naming, the two writers, the switch that keeps both downloaders' hooks off and the sweep and primitives on, the one store walker and the guard against a second, what `sidecar_is_current` can and cannot see, the shared decompression-bomb ceiling, and the sweep itself, including `--min-width`: every examined panorama in exactly one bucket, a filtered one's copy never read, and a floor at or under the cap refused before anything is written | `test_downscaled_sidecar.py` |
| [One store, one city](cropper.md#one-store-one-city): `--city` is required and strictly a city_id, a fresh store records it, a store without one adopts it, a below-`main()` run carries it forward, a store recorded as another city's (renamed under this city's name) and an unreadable marker are refused by `main()` and by the crop loop with the whole tree byte-for-byte unchanged (a planted crop survives `--force`), and every manifest row carries the city | `test_crop_store_city.py` |
| The [width tripwire](ops.md#the-width-tripwire): the ceiling's value and that it is not written in terms of the display-copy cap, the 16384/16385 boundary, both channels (`caplog` and `capsys`) each carrying the pointer to the runbook on its own, the helper provably writing nothing (a runtime guard on every write route and a lexical check of its body, which kill mutants as a pair), that a wide pano is still stored byte for byte, the call site in each of the three downloaders, GSV's before any request, and the one-time alarm: the first sighting fails `main()` and arms the latch, a later one only warns, deleting the latch re-arms it, only this run's sightings count, and an unwritable latch fails every run | `test_viewer_ceiling.py` |
| The [`fover` repair pass](ops.md#repairing-fover-era-panoramas): the decision table, the byte-for-byte survival of every refusal (the display copy included), the copy being rewritten from the imagery that replaced it but never created where there was none, ledger semantics, the recovery metric, and the CLI surface | `test_refetch_panos.py` |
| A display copy that cannot be rewritten after a swap is deleted (#122): the swap standing and still ledgered, the panorama and every bystander in the shard left byte for byte, the next `downscale_panos.py` sweep (whenever one is run) recreating the copy from the *new* imagery, a delete that also fails reported on both channels and never fatal, and nothing deleted when there was no copy to replace | `test_refetch_panos.py` (`TestTheDisplayCopyFollowsTheSwap`) |
| The desk studies under `reports/scripts/`, and the artifacts they commit | `test_*_census.py`, `test_*_study.py`, `test_depth_backfill_report.py`, `test_studyfmt.py`, `test_committed_data_files.py`, `test_reports_index.py` |
| The [re-render probe](../reports/2026-09-06-rerender-probe.md): displacement, tone and sharpness on synthetic pairs with known answers — including the gain-squared confound that would otherwise read a brightened panorama as a sharpened one — plus the three-component pose fit (a uniform vertical offset is its own component, never a tilt and never dropped; the fit is reported for every row, gated on nothing), the movement verdict against the probe's own gate, and every cell of the report's table and every count in its prose regenerated from the committed artifacts, the click-noise sigma it quotes included | `test_rerender_probe.py`, `test_rerender_probe_report.py` |
| The pose-drift re-render proxy in the standing decay census: the wrap applied to the *difference*, an axis neither census carried reported undefined rather than zero, one panorama counted once however many axes moved, the any-axis union that lets "the same panoramas moved on both axes" be asserted rather than inferred from equal counts, and `--resummarize` regenerating a re-fetch census's `decay` block offline | `test_photometa_census.py` (`TestPoseDrift`, `TestResummarize`) |
| That the docs' internal links and anchors resolve, that cited `docs/` paths exist, that the `log.csv` table in `docs/ops.md` has one row per field, and that every sentence stating the row's width ("19-column `log.csv`" and the like, in the guidance files, the README and `docs/`) says `LOG_CSV_FIELD_COUNT`; and that every `.claude/rules/**/*.md` file is path-scoped (in the one frontmatter shape the test parses) with globs that match real files, that there is no nested `CLAUDE.md`, that every measured module, study test and the log analyzer's test and page load the rules written for them, and that the guidance loaded at startup stays small | `test_docs.py` |
| That the README's hero figure still builds against the current cropper, and isn't stale | `test_make_banner.py` |
| [`constraints.txt`](ops.md#refreshing-constraintstxt) (#167): every line an exact `name==version` pin with no duplicates, every `requirements.txt` package pinned at a version its specifier accepts, the gated job installing with `-c constraints.txt` in every install step (block-scalar `run: |` ones included) and keying its pip cache on it, and the weekly `tests-latest.yml` job installing without it (no `-c`, no `PIP_CONSTRAINT`), on a schedule only (no `pull_request*`/`push`/`workflow_run`/`workflow_call`/`merge_group` trigger), with no `if:` switching it off, the hard `streetlevel` import and a timeout | `test_constraints.py` |

## Four things that are deliberately unusual

**The suite is network-free**, and `streetlevel` is stubbed. One module is the exception:
`test_streetlevel_api.py` imports the *real* `streetlevel` to pin the handful of API details
`downloaders/gsv.py` depends on — the mocked suite can't catch drift there, because the stub accepts any
arguments. It skips itself when `streetlevel` isn't installed (its `pyfrpc` dependency has no wheel on Windows
or macOS and needs a C compiler there) — **except when `SIDEWALK_REQUIRE_STREETLEVEL` is set**, which CI does:
there the import is hard, so a transitive dependency that is missing fails the run with its real message
instead of skipping all ten contract tests behind a green check
([#165](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/165)). Any value but empty or `0`
turns it on. CI also passes `--continue-on-collection-errors`, because that failure is a collection error and
pytest's default on one is to run nothing else; the run still exits nonzero, with every other result kept.

**The suite is also isolated from the machine it runs on, and held to the next pytest**, in three ways
`tests/conftest.py` sets up and `tests/test_suite_isolation.py` pins (#165):

- A session-scoped check fails the run if it changed the repo — `git status`, plus the gitignored
  `reports/scripts/.cache/` stamped file by file, since git cannot see it and the fetcher never replaces a file
  already there. It exists because one test wrote a fake one-label `richmond.csv` into the real Mapillary study
  cache on every run. The failure is reported at the teardown of whichever test ran last; the message names the
  files. Editing the checkout while the suite runs trips it too, including during the child pytests that load
  the real `conftest.py`, where the edit fails the child and reads as that parent test failing. It reports and
  does not undo, so a write is reported **once**: the next run takes the tree as it finds it for its baseline
  and passes, and the named paths have to be removed by hand.
- Every spawned child gets a per-session temp directory (`TMPDIR`/`TEMP`/`TMP`), so the subprocess runner
  tests never take the host's real depth pacing lock, read its real block latch, arm its real width-alarm
  latch (#121, which fails a host's first sighting and only warns after) or take `scrape_queue`'s lock —
  monkeypatching does not reach a child. The pytest process itself keeps the host temp dir, and the session
  dir is removed at exit.
- `PytestRemovedIn10Warning` is an error, added to `filterwarnings` by `conftest.py` when the running pytest has
  the class, so a shape pytest 10 removes fails the PR that adds it rather than every PR on the day pytest 10
  lands. There is deliberately no `pytest<10` bound, and the price runs the other way: CI installs the latest
  pytest, so a new 9.x that deprecates a shape already in the suite fails every open PR the day it ships. That
  is the intended early warning, arriving with its reason in the error rather than as a summary line.

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
surfaced survivors of exactly this shape. The one test that regenerates a study artifact from its gold,
`test_crop_sizing_v3.py`'s `test_the_committed_artifact_reproduces_from_source`, runs only when
`RAMPNET_ROOT` points at a RampNet checkout and skips otherwise, CI included; there the v3 study's figure and
provenance plumbing is covered by the synthetic tests in its `TestStudyLogic`.

Tests asserting POSIX file modes skip themselves on Windows; everything else runs on a Windows dev box.
