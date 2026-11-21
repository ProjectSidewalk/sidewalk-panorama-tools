#!/usr/bin/env bash
# #237 Part 2: how the impl_E_* and impl_M* outputs were produced (Git Bash on Windows 11, from the worktree root,
# with the test edits applied and the pins still at master's). A record of what ran, run inline at the time; the
# venv and the wheel folder are the implementer's scratch paths.
#
# The venv: uv's CPython 3.10.21, then CI's two install lines at master's pins (streetlevel 0.12.11):
#   uv venv --python 3.10 --seed .venv
#   .venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt -c constraints-ci.txt
#   .venv/Scripts/python -m pip install --no-deps -r requirements-nodeps.txt -c constraints.txt -c constraints-ci.txt
set -u
O=<SCRATCH>/237-impl
T="tests/test_streetlevel_api.py::test_streetlevels_parser_agrees_with_ours_on_a_modelled_zenith tests/test_depth_helpers.py::TestComputeDepthRaster::test_matches_streetlevels_own_decode"

# E: the two tests on each release, swapped in with --no-deps from the wheels PyPI serves.
for v in 0.12.11 0.12.13 0.13.0; do
  .venv/Scripts/python -m pip install -q --no-deps <MEAS>/dl-$v/streetlevel-$v-py3-none-any.whl
  { echo "== $(date -u +%FT%TZ) $(.venv/Scripts/python -c 'import sys,importlib.metadata as m; print("Python", sys.version.split()[0], "streetlevel", m.version("streetlevel"))')"
    echo "== SIDEWALK_REQUIRE_STREETLEVEL=1 python -m pytest -p no:cacheprovider -q -rA $T"
    SIDEWALK_REQUIRE_STREETLEVEL=1 .venv/Scripts/python -m pytest -p no:cacheprovider -q -rA $T 2>&1 \
      | grep -E "^(E  |FAILED|PASSED|[0-9]+ (passed|failed))"
    echo "exit ${PIPESTATUS[0]}"; } > $O/E_$v.out.txt
done

# M: each mutant applied to downloaders/gsv.py with a small Python string replacement, on streetlevel 0.13.0,
# then restored with `git checkout -- downloaders/gsv.py` before the next one.
#   M1: after `header_size, ..., offset = _DEPTH_HEADER.unpack_from(raw, 0)` in _decode_depth_planes, add
#       `offset = struct.unpack_from('<H', raw, 7)[0]` (_DEPTH_HEADER untouched): our decode misreads as 0.12.x did.
#   M2: `_compute_depth_raster` returns `...astype(np.float32)[:, ::-1]`, its columns mirrored.
run() {
  { echo "== $(date -u +%FT%TZ) $1"; echo "$2"
    git diff downloaders/gsv.py | grep -E '^[-+][^-+]'
    SIDEWALK_REQUIRE_STREETLEVEL=1 .venv/Scripts/python -m pytest -p no:cacheprovider -q -rA $T 2>&1 \
      | grep -E "^(E  |FAILED|PASSED|[0-9]+ (passed|failed))"
    echo "exit ${PIPESTATUS[0]}"; } > $O/$1.out.txt
  git checkout -- downloaders/gsv.py
}

# M5: one number the report quotes ("is 16 commits") changed to 17, then tests/test_streetlevel_bump_report.py run
# without the variable. The report was still untracked, so it was restored from a copy rather than by git.
#   sed -i 's/`v0.12.13...v0.13.0` is 16 commits/`v0.12.13...v0.13.0` is 17 commits/' reports/2026-11-17-streetlevel-0.13.0.md
#   .venv/Scripts/python -m pytest -p no:cacheprovider -q -rA tests/test_streetlevel_bump_report.py
