# What `streetlevel` really needs, and installing it with `--no-deps`

Issue: [#237](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/237) (Part 1), from
[#167](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/167) item 2. Measured 2026-11-15 on WSL
Ubuntu 22.04.1 with CPython 3.10.6 (the production baseline) against `streetlevel==0.12.11`, the pin, and on
Windows 11 with CPython 3.12.10. Everything cited is in
[`data/2026-11-15-streetlevel-no-deps/`](data/2026-11-15-streetlevel-no-deps/): the scripts, their raw output, the
review's re-measurements under `review/`, and the re-runs on the PR branch as `rerun_*`. The two proposed
requirement files are committed as `proposed/*.txt.proposed`, so nothing indexes them as real requirement files.
The text files were committed exactly as written except for one scripted substitution of local path prefixes:
the measurement folder became `<MEAS>`, any other scratch path `<SCRATCH>`, the WSL work directory
`~/claude-work/237`, and any other WSL home path `~/`. `tests/test_streetlevel_no_deps_report.py` checks that no
home-directory or scratch path is left. The scripts are a record of what ran, not portable tools: they still name
the WSL work directory and, in two of them, the Windows clone they cloned from (`/mnt/d/Git/...`), and running one
elsewhere means editing those two paths.

## The question

The production scraper's venv was 4.9 GB, almost all of it `torch`, `triton` and the `nvidia-*`/`cuda-*` wheels.
No production module imports any of them. They arrive because `streetlevel` declares `pyequilib`, and `pyequilib`
0.6.0 requires `torch`. pip has no per-requirement `--no-deps`, so dropping them means installing `streetlevel`
alone with `--no-deps` and listing, in `requirements.txt`, what it really needs. The question was what that is,
and whether anything it needs is imported lazily, where an import-time check cannot see it.

## Method

Five scripts, each seeing something the others cannot:

| Script | What it sees | What it cannot see |
|---|---|---|
| `probe_imports.py` (`01_probe.sh`) | Installs `streetlevel` alone with `--no-deps`, then imports what production imports and installs, also `--no-deps`, exactly the distribution each failed import names, until the import succeeds. The result is the import-time closure, in the order it was missed. | An import inside a function body, which runs only when that function is called. |
| `static_imports.py` (`02_static.sh`) | Every import statement, at any depth, in every `streetlevel` module loaded after those imports, with whether its top-level name resolves in the venv. | Modules that were never loaded. |
| `loaded_distributions.py` | Which distributions the loaded modules came from, read from each distribution's file list. | Anything not loaded. |
| `review/r2_callpaths.py` | Runs the **real** `api.find_panorama_by_id` body offline, through `gsv._fetch_image_levels` and `gsv._fetch_pano_with_depth_planes`, with a fake requests-like session, recording every import attempted. | Call paths the repo does not use. |
| `review/r3_closure.py` | The declared-requirement closure of the built venv: what is installed but reached from neither requirement file, and every declared requirement left unsatisfied. | Imports that are not declared. |

A grep of the whole installed package for indented imports, `importlib`, `__import__`, `find_spec` and
`except ImportError` (`review/r1_source.out.txt`) covers the modules the scripts never loaded.

## What it needs

`streetlevel` 0.12.11 declares 16 requirements (`01_probe.out.txt`). `from streetlevel import streetview`, which
covers every import production makes (`downloaders/gsv.py`'s photometa and depth seams and
`scrape_queue.probe_streetlevel`), needed 21 distributions in all (`01_probe_production.json`). Of the 16 declared,
9 are reached and 7 are not:

| Reached at import, so listed in `requirements.txt` | Imported by |
|---|---|
| `aiohttp`, `requests`, `numpy`, `pillow` (already listed) | `util.py`, `streetview/*` |
| `pyproj`, `scipy`, `bd09convertor`, `CoordinatesConverter` (new) | `geo.py`, at module level |
| `pyexiv2` (new) | `exif.py` |

Declared but never imported, and so no longer installed: `pyequilib` (the one that pulls torch), `protobuf`,
`pyfrpc`, `pycryptodome`, `cython`, `pytest`, `pytest-asyncio`. The extra modules `tests/test_streetlevel_api.py`
imports needed nothing more.

**The issue guessed wrong.** It expected `protobuf`, `pyfrpc` and `pyexiv2` to be needed and `scipy` and the
two coordinate converters not. In fact the Street View request builder is `streetlevel`'s own
`streetview/protobuf.py`, not the `protobuf` package, and `streetlevel.geo` imports `scipy`, `pyproj` and both
converters at module level.

**No lazy imports.** The static walk found 0 unresolved imports in the 13 loaded modules (`02_static.out.txt`).
The source grep's only hit is `streetlevel/lookaround/__init__.py`, which guards on `find_spec("torch")` and
which this repo never loads. The real call paths ran clean offline (`review/r4_pip22.out.txt`, the r2 section):
both seams return correct results and no unreached module is imported. r2 exits 1 for one reason, an attempted
`import cython`, which is scipy's own guarded probe in `scipy/_lib/_testutils.py`
(`review/r5_who_imports_cython.out.txt`) and harmless. The computed closure has 24 installed distributions, all
reached from the two requirement files, and exactly 7 unsatisfied requirements, all `streetlevel`'s.

## Before and after

| | Distributions | Venv size | Source |
|---|---|---|---|
| master's one-line install | 65 | 4,963 MB | `05_before.out.txt` |
| the two lines, pip 26.3 | 24 (+ uv's seed `packaging`) | 291 MB | `03_after.out.txt` |
| the two lines, pip 22.0.2 (the box's seed) | 24 | 294 MB | `review/r4_pip22.out.txt` |

Before, `nvidia/` alone was 2,724 MB, `torch/` 1,110 MB and `triton/` 690 MB. The box itself was not measured
here: its 4.9 GB is the 2026-10-06 reading in `docs/ops.md`, and the after figure is for Jon to record at deploy.

**The box's own pip.** Ubuntu 22.04's `python3 -m venv` seeds pip 22.0.2 and setuptools 59.6.0 and no `wheel`.
`review/r4_pip22.sh` ran both install lines with exactly that seed, and the freeze was identical to the reduced
`constraints.txt`. `pip check` exits 1 naming the 7, the deploy's import checks pass, re-running either line is a
no-op, and an unrelated later install changes nothing else and prints no resolver notice. `CoordinatesConverter`
ships no `pyproject.toml`, so pip 22.0.2 builds it with the legacy `setup.py install`, which works
(`review/r7_sdist_pip22.out.txt`).

**The PR branch's own files** (`rerun_box_pip22.out.txt`): the same seed, reading the branch's
`requirements.txt`, `requirements-nodeps.txt` and `constraints.txt`, gave a 24-line freeze identical to both the
branch's `constraints.txt` and `review/r4_freeze.txt`, 294 MB, no GPU-stack distribution, the same 7 in
`pip check`, and the same r2 and r3 results.

**Windows** (`06_windows_py312.out.txt`): on Windows 11 / CPython 3.12.10 both lines install without a compiler
and the imports resolve. Wheels exist for CPython 3.10 to 3.13 on manylinux, win_amd64 and macOS for `pyexiv2`,
`pyproj` and `scipy`; `bd09convertor` is a pure wheel and `CoordinatesConverter` a pure-Python sdist
(`review/r9_wheel_platforms.out.txt`). Before this change `pyfrpc` had no Windows wheel and needed a compiler.

## The suite

Master's code in the new environment, `SIDEWALK_REQUIRE_STREETLEVEL=1`, `--cov`, pytest 9.1.1
(`03_after_stage{B,C}_pytest.txt`): without torch, 7033 passed, 20 skipped, 98.86% coverage, with
`torch_geometry.py` at 0%; with CPU torch, 7086 passed, 19 skipped, 99.30%. The gate is `fail_under = 98`. So a
gated job that silently lost torch would still pass, which is why CI now installs it on purpose, pins it, and
imports it in a step of its own.

On the PR branch (`rerun_ci_like.out.txt`, at its third commit), with CI's exact install sequence: the first two
install steps downloaded no torch-family wheel, and the venv held 44 distributions, none of the GPU stack, at
440 MB; after the CPU torch steps, `torch 2.13.0+cpu` imported, at 1257 MB. The suite without torch
(`rerun_ci_like_notorch_pytest.txt`): 7065 passed, 20 skipped, 98.86%; with torch
(`rerun_ci_like_torch_pytest.txt`): 7118 passed, 19 skipped, 99.30%, the same coverage figures as the planning
runs. Both runs also had 3 failures, all `tests/test_docs.py` reporting the link to this report, which that
commit did not yet contain. `tests/test_torch_geometry.py` and `tests/test_streetlevel_api.py` ran together with
71 passed and none skipped.

## Three pin files

`constraints.txt` is defined as the box's `pip freeze`, which cannot be regenerated off the box, and the box keeps
its old venv until the deploy. So master's 65 pins were split mechanically by `review/r0_derive_constraints.py`
(re-run on the branch base: `rerun_r0_derive.out.txt`, identical output) into three sets, every line verbatim:

* **24 stay in `constraints.txt`**: exactly the packages the two lines install.
* **15 move to the new `constraints-ci.txt`**: what the gated CI job installs and the box does not, the pytest
  runner and its requirements, and the CPU torch line's (`torch`, `sympy`, `networkx`, ...). They keep CI on
  exactly the versions it ran before, so a red run on the change means the change, not a new pytest or torch.
* **26 are deleted**: the GPU stack (`triton`, `pyequilib`, 15 `nvidia-*`, 3 `cuda-*`), and `streetlevel`'s
  other unused requirements (`protobuf`, `pyfrpc`, `pycryptodome`, `Cython`, `pytest-asyncio`, and
  `pytest-asyncio`'s own `backports.asyncio.runner`).

`constraints.txt`'s header records the hand deletion until the first refresh from the box after the deploy.

## The box move, end to end (added in review)

The #244 review found that the first version of the move renamed `.venv` away before the new venv was built, so an
interrupted build left the 19:00 run without an interpreter. The move now builds `.venv-237` beside the old venv,
checks it, and swaps it in with `mv .venv .venv-before-237 && ln -s .venv-237 .venv`. `rerun_box_move.sh` ran the
whole procedure with the box's pip 22.0.2 in a clone standing in for the box (`rerun_box_move.out.txt`):

* **The old venv**, built from master's own files, froze to 65 lines identical to master's `constraints.txt`, at
  4969 MB. **An ordinary deploy of this branch into it is a no-op**: both lines exit 0 and the freeze is unchanged.
  The Deploying block's new freeze check prints exactly 41 `<` lines there (the packages #237 drops) and 0 `>`
  lines.
* **Step 1** built and checked `.venv-237` while `.venv` stayed whole: an empty freeze diff, the imports, and a dry
  run with no `streetlevel` warning.
* **Every interruption state** in the `docs/ops.md` table was produced and recovered by the one rollback line:
  killed after `python3 -m venv`; killed inside the swap line (no `.venv`); a failed check after the swap; the
  rollback line run twice; and killed inside the rollback line. Each time the old venv came back with its own pip
  working and its freeze unchanged.
* **After the swap**, `sys.prefix` is the checkout's `.venv`, the freeze diff is empty, `.venv/bin/pip` works,
  `git status --short` is empty (the new `.gitignore` pattern), the nightly's shape (an absolute
  `.venv/bin/python` running `cron_notify.py` around the queue's dry run) exits 0, and the new venv is 294 MB.

**Why a symlink** (`rerun_venv_rename.out.txt`): a venv renamed after it is built keeps its old interpreter path
in its scripts' first lines, so `.venv/bin/pip` exits 126 ("bad interpreter"), while `python -m pip` still works.
Through a symlink both work, and `sys.prefix` is `.venv` either way.

## Part 2: the upstream fix exists

`streetlevel` 0.13.0 was uploaded 2026-11-12, and its `depth.parse_header` reads the offset as one byte
(`04_part2.out.txt`, `review/r6_part2.out.txt`); 0.12.13 still reads two, and there is no 0.12.14. 0.13.0 declares
the same 16 requirements, its `find_panorama_by_id` is identical, and its loaded modules import the same 9
third-party names, so Part 1 stands on it unchanged. The bump is left to its own PR: the pin is `<0.13`,
`test_streetlevel_still_misreads_the_depth_offset` is built to fail on 0.13.0 and lists the cleanups that follow,
and one PR changing both the install mechanism and the upstream version would make a red run ambiguous. Until
then the weekly unpinned job installs 0.12.13, the newest release under `<0.13`.

## Wrong turns

* This WSL has no `python3.10-venv` and no sudo, so `python3 -m venv` failed (`00_first_attempt_no_ensurepip.out.txt`)
  and every venv was made with `uv venv --seed` instead. uv seeds `packaging`, which a `python3 -m venv` does not;
  that is the one extra line in the pip 26.3 freeze.
* `importlib.metadata.packages_distributions()` on 3.10 reads only `top_level.txt` and silently missed numpy, scipy
  and five others; `loaded_distributions.py` reads each distribution's file list instead.
* Three attempts to run pip 22.0.2 in that WSL failed before the fourth worked: no `distutils.cmd` (no
  `python3-distutils`), then a setuptools shim that failed under `python -c` and again from a file
  (`review/r4_pip22_*_attempt_*.out.txt`). The working run puts setuptools' own `_distutils` on `PYTHONPATH`
  for pip only, the one deviation from the box.
* The fetch of 0.13.0 for Part 2 first failed for want of `unzip` (`04_part2_first_attempt_no_unzip.out.txt`).

## Tests that pin it

`tests/test_constraints.py` keeps `streetlevel` alone in `requirements-nodeps.txt` and installed `--no-deps` in both
workflows and every docs install block; keeps the 9 in `requirements.txt` and the 7 out of both files, and fails
when the installed `streetlevel` declares a different set; walks the loaded `streetlevel` modules for in-function or
unresolved imports; keeps the GPU stack out of `constraints.txt`, `constraints-ci.txt` and (under
`SIDEWALK_REQUIRE_STREETLEVEL`) the installed environment; and requires the CPU torch import step.
`tests/test_streetlevel_api.py` runs the real `find_panorama_by_id` with a fake session.
`tests/test_streetlevel_no_deps_report.py` checks this report's headline numbers against the files above.

## Open

* **Vendoring the URL builder** and dropping `streetlevel` would also take `scipy` and `pyproj` off the box. It is a
  production code change and a follow-up, not done here.
* **An upstream request** to make `pyequilib` optional was not filed.
* **The box** has not moved yet: `docs/ops.md`, "Moving the box to the --no-deps install (#237)", is the checklist.
  No live photometa request was made for any of this.
