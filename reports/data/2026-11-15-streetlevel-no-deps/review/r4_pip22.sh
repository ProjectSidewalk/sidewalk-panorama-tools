#!/bin/bash
# Review of the #237 plan, step 4: the proposed two-line install under Ubuntu 22.04's OWN pip (22.0.2, what
# `python3 -m venv` seeds from python3-pip-whl on 22.04), against the REDUCED constraints (derived by
# r0_derive_constraints.py), in a venv that gets nothing else. Then: the real call paths offline, the computed
# requirement closure, pip check, and what later installs do.
# Run as:  wsl -d Ubuntu -- bash <this file>       Deletes nothing; refuses an existing venv directory.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
MEAS="$(cd "$HERE/.." && pwd)"
W="$HOME/claude-work/237"
V="$W/venv-review-pip22d"
REPO="$W/repo"
if [ -e "$V" ]; then echo "$V already exists; this script deletes nothing"; exit 1; fi
# First attempt (r4_pip22_first_attempt_no_distutils.out.txt): pip 22.0.2 imports distutils.cmd, and this WSL has
# no python3-distutils (the box does: python3-venv depends on it). So the venv is seeded as Ubuntu 22.04's
# `python3 -m venv` seeds it - pip 22.0.2 and setuptools 59.6.0, NO wheel - and pip runs with setuptools'
# distutils shim loaded first. That is the one deviation from the box.
export SETUPTOOLS_USE_DISTUTILS=stdlib   # attempts 2-3 (local shim) also failed: distutils.cmd still missing
pip22() { PYTHONPATH="$V/shim" "$V/bin/python" -m pip "$@"; }
date -u +%Y-%m-%dT%H:%M:%SZ
echo "== repo"; git -C "$REPO" log --oneline -1; git -C "$REPO" status --short | head

echo "== derive the reduced constraints and the CI-only pin file from master's constraints.txt"
/usr/bin/python3.10 -I "$HERE/r0_derive_constraints.py" "$REPO/constraints.txt" "$MEAS/03_after_stageA_freeze.txt" "$HERE"
C="$HERE/constraints-reduced.txt"

~/.local/bin/uv venv --seed --python /usr/bin/python3.10 "$V" || exit 1
"$V/bin/python" -m pip uninstall -q -y wheel packaging && "$V/bin/python" -m pip install -q 'pip==22.0.2' 'setuptools==59.6.0'
mkdir "$V/shim" && ln -s "$V/lib/python3.10/site-packages/setuptools/_distutils" "$V/shim/distutils"   # setuptools 59.6.0s own copy of distutils, on PYTHONPATH for pip only
pip22 --version
echo "== seed packages"; pip22 list --format=freeze

echo "== line 1: pip install -r requirements.txt -c constraints.txt   (pip 22.0.2)"
pip22 install -r "$MEAS/proposed/requirements.txt" -c "$C" > "$HERE/r4_line1.log" 2>&1; echo "exit: $?"
tail -4 "$HERE/r4_line1.log"
echo "== line 2: pip install --no-deps -r requirements-nodeps.txt -c constraints.txt"
pip22 install --no-deps -r "$MEAS/proposed/requirements-nodeps.txt" -c "$C" 2>&1 | tail -3; echo "exit: ${PIPESTATUS[0]}"
echo "== freeze (pip 22.0.2) vs the planner's stage-A freeze (pip 26.3)"
pip22 freeze > "$HERE/r4_freeze.txt"
diff <(grep -v '^packaging==' "$MEAS/03_after_stageA_freeze.txt") "$HERE/r4_freeze.txt" && echo "identical (planner's minus packaging)"
echo "== freeze vs the reduced constraints (should be identical, order aside)"
diff <(sort -f "$C") <(sort -f "$HERE/r4_freeze.txt") && echo "identical"
echo "== size"; du -sm "$V"
echo "== pip check"; pip22 check; echo "pip check exit: $?"

echo "== deploy checks from the repo clone (master's code, which this PR does not change)"
( cd "$REPO" && "$V/bin/python" -m py_compile DownloadRunner.py scrape_queue.py downloaders/*.py && echo "py_compile ok" )
( cd "$REPO" && "$V/bin/python" -c "import DownloadRunner, scrape_queue, cron_notify; print('import ok')" )
( cd "$REPO" && "$V/bin/python" -c "import sys, scrape_queue; print('probe_streetlevel:', scrape_queue.probe_streetlevel(sys.executable))" )

echo "== the real streetlevel call paths, offline (r2_callpaths.py)"
( cd "$REPO" && "$V/bin/python" -I "$HERE/r2_callpaths.py" ); echo "r2 exit: $?"

echo "== the computed requirement closure (r3_closure.py)"
"$V/bin/python" -I "$HERE/r3_closure.py" "$MEAS/proposed/requirements.txt" "$MEAS/proposed/requirements-nodeps.txt"

echo "== a later re-run of line 1 (the ordinary Deploying step after a future requirements change)"
pip22 install -r "$MEAS/proposed/requirements.txt" -c "$C" 2>&1 | tail -3; echo "exit: ${PIPESTATUS[0]}"
echo "== a later re-run of line 2"
pip22 install --no-deps -r "$MEAS/proposed/requirements-nodeps.txt" -c "$C" 2>&1 | tail -3; echo "exit: ${PIPESTATUS[0]}"
echo "== an unrelated later install (does pip try to repair streetlevel?)"
pip22 install 'six==1.16.0' 2>&1 | tail -6; echo "exit: ${PIPESTATUS[0]}"
echo "== freeze after the later installs (only six should be new)"
pip22 freeze | diff "$HERE/r4_freeze.txt" - ; true
date -u +%Y-%m-%dT%H:%M:%SZ
