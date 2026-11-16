#!/bin/bash
# #237 re-run, box-like: review/r4_pip22.sh adapted to read the PR branch's own requirements.txt,
# requirements-nodeps.txt and constraints.txt (from a clone of the branch) instead of the plan's proposed copies.
# Same seed as r4: Ubuntu 22.04's `python3 -m venv` gives pip 22.0.2 and setuptools 59.6.0 with no wheel, and
# pip runs with setuptools' distutils shim on PYTHONPATH because this WSL has no python3-distutils (the one
# deviation from the box). Then the deploy checks, review/r2_callpaths.py and review/r3_closure.py in that venv.
# Run as:  wsl -d Ubuntu -- bash <this file>       Deletes nothing; refuses an existing venv directory.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HOME/claude-work/237"
V="$W/venv-impl-pip22"
REPO="$W/impl-repo"
if [ -e "$V" ]; then echo "$V already exists; this script deletes nothing"; exit 1; fi
export SETUPTOOLS_USE_DISTUTILS=stdlib
pip22() { PYTHONPATH="$V/shim" "$V/bin/python" -m pip "$@"; }
date -u +%Y-%m-%dT%H:%M:%SZ
echo "== repo"; git -C "$REPO" log --oneline -1; git -C "$REPO" status --short | head
cd "$REPO" || exit 1

~/.local/bin/uv venv --seed --python /usr/bin/python3.10 "$V" || exit 1
"$V/bin/python" -m pip uninstall -q -y wheel packaging && "$V/bin/python" -m pip install -q 'pip==22.0.2' 'setuptools==59.6.0'
mkdir "$V/shim" && ln -s "$V/lib/python3.10/site-packages/setuptools/_distutils" "$V/shim/distutils"
pip22 --version
echo "== seed packages"; pip22 list --format=freeze

echo "== line 1: pip install -r requirements.txt -c constraints.txt   (pip 22.0.2)"
pip22 install -r requirements.txt -c constraints.txt > "$HERE/rerun_box_pip22_line1.log" 2>&1; echo "exit: $?"
tail -3 "$HERE/rerun_box_pip22_line1.log"
echo "== line 2: pip install --no-deps -r requirements-nodeps.txt -c constraints.txt"
pip22 install --no-deps -r requirements-nodeps.txt -c constraints.txt 2>&1 | tail -3; echo "exit: ${PIPESTATUS[0]}"
pip22 freeze > "$HERE/rerun_box_pip22_freeze.txt"
echo "== freeze: $(wc -l < "$HERE/rerun_box_pip22_freeze.txt") lines"
echo "== freeze vs the branch's constraints.txt (should be identical, order and comments aside)"
diff <(grep -v '^#' constraints.txt | grep -v '^$' | sort -f) <(sort -f "$HERE/rerun_box_pip22_freeze.txt") && echo "identical"
echo "== freeze vs review/r4_freeze.txt (the plan's proposed files, same seed)"
diff "$HERE/review/r4_freeze.txt" "$HERE/rerun_box_pip22_freeze.txt" && echo "identical"
echo "== heavy distributions (expect none)"
grep -i -E '^(torch|triton|pyequilib|nvidia|cuda)' "$HERE/rerun_box_pip22_freeze.txt"; echo "grep exit: $? (1 = none)"
echo "== size"; du -sm "$V"
echo "== pip check"; pip22 check; echo "pip check exit: $?"

echo "== the deploy checks (docs/ops.md, Moving the box to the --no-deps install)"
"$V/bin/python" -m py_compile DownloadRunner.py scrape_queue.py downloaders/*.py && echo "py_compile ok"
"$V/bin/python" -c "import DownloadRunner, scrape_queue, cron_notify; print('import ok')"
"$V/bin/python" -c "from streetlevel import streetview; from streetlevel.streetview import api; print('streetlevel import ok')"
"$V/bin/python" -c "import sys, scrape_queue; print('probe_streetlevel:', scrape_queue.probe_streetlevel(sys.executable))"

echo "== the real streetlevel call paths, offline (review/r2_callpaths.py; exit 1 is scipy's guarded cython probe, see review/r5)"
"$V/bin/python" -I "$HERE/review/r2_callpaths.py"; echo "r2 exit: $?"
echo "== the computed requirement closure (review/r3_closure.py)"
"$V/bin/python" -I "$HERE/review/r3_closure.py" requirements.txt requirements-nodeps.txt

echo "== re-running both lines is a no-op"
pip22 install -r requirements.txt -c constraints.txt 2>&1 | grep -c 'Requirement already satisfied'; echo "exit: ${PIPESTATUS[0]}"
pip22 install --no-deps -r requirements-nodeps.txt -c constraints.txt 2>&1 | grep -c 'Requirement already satisfied'; echo "exit: ${PIPESTATUS[0]}"
pip22 freeze | diff "$HERE/rerun_box_pip22_freeze.txt" - && echo "freeze unchanged"
echo "== clone still clean?"; git -C "$REPO" status --short | head
date -u +%Y-%m-%dT%H:%M:%SZ
