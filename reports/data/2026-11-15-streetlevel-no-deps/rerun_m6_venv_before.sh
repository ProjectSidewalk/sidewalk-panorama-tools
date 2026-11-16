#!/bin/bash
# #237 re-run, M6: the live tests in master's own venv (venv-before: master's one-line install, torch and the
# GPU stack included). test_this_environment_has_no_gpu_stack must FAIL there under SIDEWALK_REQUIRE_STREETLEVEL,
# naming the 20 GPU-stack distributions, and skip without it. The other live checks (T7, T8, T11) run too:
# streetlevel's declaration and source are the same whichever way it was installed, so they pass there.
# Run as:  wsl -d Ubuntu -- bash <this file> <commit>     Deletes nothing; refuses an existing clone directory.
set -u
W="$HOME/claude-work/237"
V="$W/venv-before"
REPO="$W/impl-repo-tests"
if [ -e "$REPO" ]; then echo "$REPO already exists; this script deletes nothing"; exit 1; fi
date -u +%Y-%m-%dT%H:%M:%SZ
git clone -q --branch 237-streetlevel-no-deps /mnt/d/Git/sidewalk-panorama-tools "$REPO" || exit 1
cd "$REPO" || exit 1
git checkout -q "$1" && git log --oneline -1
"$V/bin/pip" freeze | wc -l
echo "== with SIDEWALK_REQUIRE_STREETLEVEL=1 (expect the GPU-stack test to FAIL, the rest of the live checks to pass)"
SIDEWALK_REQUIRE_STREETLEVEL=1 "$V/bin/python" -m pytest -p no:cacheprovider -q -rs --tb=line \
  "tests/test_constraints.py::TestTheGpuStackIsNotInstalled" \
  "tests/test_constraints.py::TestStreetlevelIsInstalledWithoutItsDependencies::test_they_are_what_the_installed_streetlevel_declares" \
  "tests/test_constraints.py::TestTheInstalledStreetlevel" \
  "tests/test_streetlevel_api.py::test_the_real_find_panorama_by_id_uses_the_session_it_is_given" 2>&1 | tail -8
echo "== without the variable (expect the GPU-stack test to SKIP)"
"$V/bin/python" -m pytest -p no:cacheprovider -q -rs "tests/test_constraints.py::TestTheGpuStackIsNotInstalled" 2>&1 | tail -3
date -u +%Y-%m-%dT%H:%M:%SZ
