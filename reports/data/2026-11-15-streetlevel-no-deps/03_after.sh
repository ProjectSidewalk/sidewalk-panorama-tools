#!/bin/bash
# #237 measurement, step 3: the venv the proposed mechanism produces, its size, and the suite in it.
# Run as:  wsl -d Ubuntu -- bash <this file>
# Stage A is what the production box would hold. Stages B and C add what CI adds (dev requirements, then
# CPU-only torch for tests/test_torch_geometry.py). The suite runs from the clone at ~/claude-work/237/repo
# (unmodified master), so it tests today's code in the new environment.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HOME/claude-work/237"
cd "$W" || exit 1
V="$W/venv-after"
if [ -e "$V" ]; then echo "$V already exists; move it aside by hand (this script deletes nothing)"; exit 1; fi

echo "== repo"; git -C repo log --oneline -1; git -C repo status --short | head
~/.local/bin/uv venv --seed --python /usr/bin/python3.10 "$V" || exit 1
"$V/bin/python" -m pip install -q --upgrade pip
"$V/bin/pip" --version
echo "== size of the empty venv (pip, setuptools, wheel only)"; du -sm "$V"

echo "== stage A1: pip install -r requirements.txt -c constraints.txt   (proposed requirements.txt)"
"$V/bin/pip" install -r "$HERE/proposed/requirements.txt" -c repo/constraints.txt 2>&1 | tail -3
echo "== stage A2: pip install --no-deps -r requirements-nodeps.txt -c constraints.txt"
"$V/bin/pip" install --no-deps -r "$HERE/proposed/requirements-nodeps.txt" -c repo/constraints.txt 2>&1 | tail -3
echo "== stage A freeze"; "$V/bin/pip" freeze | tee "$HERE/03_after_stageA_freeze.txt"
echo "== stage A size"; du -sh "$V"; du -sm "$V"
echo "== stage A pip check"; "$V/bin/pip" check; echo "pip check exit: $?"
echo "== stage A: the deploy import checks (docs/ops.md, Deploying) and the queue's streetlevel probe"
( cd repo && "$V/bin/python" -m py_compile DownloadRunner.py scrape_queue.py downloaders/*.py && echo "py_compile ok" )
( cd repo && "$V/bin/python" -c "import DownloadRunner, scrape_queue, cron_notify; print('import ok')" )
( cd repo && "$V/bin/python" -c "import sys, scrape_queue; print('probe_streetlevel:', scrape_queue.probe_streetlevel(sys.executable))" )
( cd repo && "$V/bin/python" -c "from streetlevel import streetview; from streetlevel.streetview import api; print('url ok:', api.build_find_panorama_by_id_request_url('testPanoIdAbCdEfGhIj_-', True, 'en')[:60])" )
echo "== stage A: heavy distributions present (expect none)"
"$V/bin/pip" freeze | grep -i -E '^(torch|triton|pyequilib|nvidia|cuda)' ; echo "grep exit: $? (1 = none found)"

echo "== stage B: + requirements-dev.txt"
"$V/bin/pip" install -r "$HERE/proposed/requirements.txt" -r repo/requirements-dev.txt -c repo/constraints.txt 2>&1 | tail -2
echo "== stage B freeze"; "$V/bin/pip" freeze > "$HERE/03_after_stageB_freeze.txt"; wc -l "$HERE/03_after_stageB_freeze.txt"
echo "== stage B size"; du -sm "$V"
echo "== stage B: full suite, SIDEWALK_REQUIRE_STREETLEVEL=1, with coverage (no torch installed)"
( cd repo && SIDEWALK_REQUIRE_STREETLEVEL=1 "$V/bin/python" -m pytest tests -q --continue-on-collection-errors --cov --cov-report=term-missing:skip-covered -p no:cacheprovider > "$HERE/03_after_stageB_pytest.txt" 2>&1; echo "pytest exit: $?" )
tail -45 "$HERE/03_after_stageB_pytest.txt"
echo "== stage B: test_streetlevel_api.py alone, verbose"
( cd repo && SIDEWALK_REQUIRE_STREETLEVEL=1 "$V/bin/python" -m pytest tests/test_streetlevel_api.py -v -p no:cacheprovider 2>&1 | tail -30 )

echo "== stage C: + CPU-only torch, the way tests.yml's package job installs it, but unpinned"
"$V/bin/pip" install -c repo/constraints.txt filelock typing_extensions sympy networkx jinja2 fsspec 2>&1 | tail -1
"$V/bin/pip" install --no-deps torch --index-url https://download.pytorch.org/whl/cpu 2>&1 | tail -1
"$V/bin/pip" freeze > "$HERE/03_after_stageC_freeze.txt"
grep -i -E '^(torch|triton|pyequilib|nvidia|cuda)' "$HERE/03_after_stageC_freeze.txt"
"$V/bin/python" -c "import importlib.metadata as m; print('torch requires:'); print('\n'.join(m.requires('torch') or []))"
echo "== stage C size"; du -sm "$V"
echo "== stage C: full suite again, with coverage"
( cd repo && SIDEWALK_REQUIRE_STREETLEVEL=1 "$V/bin/python" -m pytest tests -q --continue-on-collection-errors --cov --cov-report=term-missing:skip-covered -p no:cacheprovider > "$HERE/03_after_stageC_pytest.txt" 2>&1; echo "pytest exit: $?" )
tail -45 "$HERE/03_after_stageC_pytest.txt"
echo "== clone still clean?"; git -C repo status --short | head
date -u +%Y-%m-%dT%H:%M:%SZ
