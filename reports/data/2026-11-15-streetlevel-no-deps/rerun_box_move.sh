#!/bin/bash
# #244 review round: the box move of docs/ops.md ("Moving the box to the --no-deps install (#237)"), end to end, in a
# clone of the branch standing in for /srv/sidewalk-panorama-tools, with the box's pip (22.0.2, setuptools 59.6.0,
# no wheel). The OLD .venv is built from master's own requirements.txt and constraints.txt (b2c8d58), as the box's
# is. Then: an ordinary deploy of this branch into the old venv (is it a no-op?), the Deploying block's checks, the
# move's step 1 and step 2 as written, and the rollback line from every interruption state in the table.
# Deviations from the box, as in rerun_box_pip22.sh: venvs come from `uv venv --seed` then pip 22.0.2/setuptools
# 59.6.0 (no python3-venv here), and pip runs with setuptools' distutils on PYTHONPATH (no python3-distutils here).
# Repo code runs under `unshare -rn` (no network), so the dry run's roster check cannot reach a server.
# Run as:  wsl -d Ubuntu -- bash <this file>       Deletes nothing; refuses an existing work directory.
set -u
S="$HOME/claude-work/237/box-move"
if [ -e "$S" ]; then echo "$S already exists; this script deletes nothing"; exit 1; fi
mkdir -p "$S" || exit 1
NN="unshare -rn"
date -u +%Y-%m-%dT%H:%M:%SZ
git clone -q --branch 237-streetlevel-no-deps /mnt/d/Git/sidewalk-panorama-tools "$S/srv" || exit 1
cd "$S/srv" || exit 1
git log --oneline -1
git show b2c8d58:requirements.txt > "$S/master-requirements.txt"
git show b2c8d58:constraints.txt > "$S/master-constraints.txt"
mkdir -p "$S/store" && touch "$S/store/.pano-store"

# A venv the way Ubuntu 22.04's `python3 -m venv` seeds one (see the header for the one deviation).
mkvenv() { ~/.local/bin/uv venv -q --seed --python /usr/bin/python3.10 "$1" && "$1/bin/python" -m pip uninstall -q -y wheel packaging \
  && "$1/bin/python" -m pip install -q 'pip==22.0.2' 'setuptools==59.6.0' 2>&1 | grep -v -i 'upgrad\|version 22'; }
mkvenv "$S/shimsrc"; mkdir "$S/shim"; ln -s "$S/shimsrc/lib/python3.10/site-packages/setuptools/_distutils" "$S/shim/distutils"
export PYTHONPATH="$S/shim" SETUPTOOLS_USE_DISTUTILS=stdlib
quiet() { grep -v -i 'consider upgrading\|You are using pip version'; }
freezediff() { diff <("$1/bin/pip" freeze | sort -f) <(grep -v '^#' constraints.txt | grep . | sort -f); }
state() { echo "   state: .venv=$( [ -L .venv ] && echo "symlink->$(readlink .venv)" || ([ -d .venv ] && echo dir || echo MISSING) )" \
  "| .venv-237=$([ -d .venv-237 ] && echo yes || echo no) | .venv-before-237=$([ -d .venv-before-237 ] && echo yes || echo no)" \
  "| torch importable from .venv: $($NN .venv/bin/python -c 'import torch' >/dev/null 2>&1 && echo yes || echo no)"; }
ROLLBACK='[ -L .venv ] && unlink .venv; [ -e .venv ] || mv .venv-before-237 .venv'

echo "================ the OLD venv: master's one-line install, pip 22.0.2"
mkvenv .venv
.venv/bin/pip install -q -r "$S/master-requirements.txt" -c "$S/master-constraints.txt" 2>&1 | quiet; echo "install exit: ${PIPESTATUS[0]}"
.venv/bin/pip freeze > "$S/old-freeze.txt"; echo "old freeze: $(wc -l < "$S/old-freeze.txt") lines"
diff <(sort -f "$S/old-freeze.txt") <(grep -v '^#' "$S/master-constraints.txt" | grep . | sort -f) >/dev/null && echo "old freeze == master's constraints.txt"
echo "old .venv: $(du -sm .venv | cut -f1) MB"

echo "================ an ordinary deploy of this branch into the old venv (docs/ops.md, Deploying)"
.venv/bin/pip install -r requirements.txt -c constraints.txt 2>&1 | quiet | grep -v 'Requirement already satisfied'; echo "line 1 exit: ${PIPESTATUS[0]}"
.venv/bin/pip install --no-deps -r requirements-nodeps.txt -c constraints.txt 2>&1 | quiet | grep -v 'Requirement already satisfied'; echo "line 2 exit: ${PIPESTATUS[0]}"
.venv/bin/pip freeze | diff "$S/old-freeze.txt" - && echo "freeze unchanged: the two lines are a no-op in the old venv"
echo "-- the Deploying block's freeze diff on the old venv: < lines (packages #237 dropped), > lines (should be 0)"
freezediff .venv | grep -c '^<'; freezediff .venv | grep -c '^>'
$NN .venv/bin/python -m py_compile DownloadRunner.py scrape_queue.py downloaders/*.py && echo "py_compile ok"
$NN .venv/bin/python -c "import DownloadRunner, scrape_queue, cron_notify" && echo "import ok"
$NN .venv/bin/python -c "from streetlevel import streetview" && echo "streetlevel ok"
state

echo "================ interruption 1: killed during step 1 (python3 -m venv done, no install yet)"
mkvenv .venv-237
state
echo "================ step 1, from the top (each line idempotent; the venv command re-run on the existing dir)"
[ -d .venv-237 ] || mkvenv .venv-237
.venv-237/bin/pip install -r requirements.txt -c constraints.txt 2>&1 | quiet | tail -1; echo "line 1 exit: ${PIPESTATUS[0]}"
.venv-237/bin/pip install --no-deps -r requirements-nodeps.txt -c constraints.txt 2>&1 | quiet | tail -1; echo "line 2 exit: ${PIPESTATUS[0]}"
freezediff .venv-237 && echo "freeze diff: empty"
$NN .venv-237/bin/python -m py_compile DownloadRunner.py scrape_queue.py downloaders/*.py && echo "py_compile ok"
$NN .venv-237/bin/python -c "import DownloadRunner, scrape_queue, cron_notify" && echo "import ok"
$NN .venv-237/bin/python -c "from streetlevel import streetview; from streetlevel.streetview import api" && echo "streetlevel ok"
$NN .venv-237/bin/python scrape_queue.py --cities samples/scrape_queue_cities.csv --store-root "$S/store" --dry-run > "$S/dry1.txt" 2>&1; echo "dry run exit: $?; streetlevel warnings: $(grep -ci 'streetlevel is not importable' "$S/dry1.txt")"
state

echo "================ interruption 2: killed inside the swap line, after mv and before ln"
mv .venv .venv-before-237
state
echo "-- rollback line"; eval "$ROLLBACK"; state
.venv/bin/pip --version | cut -c1-12; echo "old .venv/bin/pip exit: ${PIPESTATUS[0]}"

echo "================ step 2: the swap, as one line"
mv .venv .venv-before-237 && ln -s .venv-237 .venv
state
freezediff .venv && echo "freeze diff through the symlink: empty"
$NN .venv/bin/python -c "from streetlevel import streetview; import sys; print(sys.prefix.replace('$S', '<S>'))"
echo "first line of .venv/bin/pip: $(head -1 .venv/bin/pip | sed "s#$S#<S>#")"
.venv/bin/pip --version | cut -c1-12; echo ".venv/bin/pip exit: ${PIPESTATUS[0]}"
echo "first line of .venv-before-237/bin/pip: $(head -1 .venv-before-237/bin/pip | sed "s#$S#<S>#")   (the new venv's python: hence 'never run it while renamed')"
echo "git status --short (must be empty):"; git status --short; echo "(end)"
echo "-- the nightly's shape: an absolute .venv/bin/python running cron_notify around the queue (dry run, no network)"
$NN "$S/srv/.venv/bin/python" "$S/srv/cron_notify.py" --sink 'cat > /dev/null' --only-on-failure -- \
  "$S/srv/.venv/bin/python" "$S/srv/scrape_queue.py" --cities samples/scrape_queue_cities.csv --store-root "$S/store" --dry-run > "$S/dry2.txt" 2>&1
echo "cron_notify exit: $?; streetlevel warnings: $(grep -ci 'streetlevel is not importable' "$S/dry2.txt")"
echo "new venv: $(du -sm .venv-237 | cut -f1) MB"

echo "================ interruption 3: a check after the swap fails -> rollback line"
eval "$ROLLBACK"; state
.venv/bin/pip freeze | diff "$S/old-freeze.txt" - && echo "old venv back, freeze as before"
.venv/bin/pip --version | cut -c1-12; echo "old .venv/bin/pip exit: ${PIPESTATUS[0]}"
echo "-- rollback line again (idempotent)"; eval "$ROLLBACK"; state

echo "================ interruption 4: killed inside the rollback line, after unlink"
mv .venv .venv-before-237 && ln -s .venv-237 .venv
unlink .venv
state
echo "-- rollback line"; eval "$ROLLBACK"; state

echo "================ finish the move: swap again"
mv .venv .venv-before-237 && ln -s .venv-237 .venv
state
freezediff .venv && echo "freeze diff: empty"
echo "git status --short (must be empty):"; git status --short; echo "(end)"
echo "== a later ordinary deploy on the moved box: the Deploying block's checks"
.venv/bin/pip install -r requirements.txt -c constraints.txt 2>&1 | quiet | grep -v 'Requirement already satisfied'; echo "line 1 exit: ${PIPESTATUS[0]}"
.venv/bin/pip install --no-deps -r requirements-nodeps.txt -c constraints.txt 2>&1 | quiet | grep -v 'Requirement already satisfied'; echo "line 2 exit: ${PIPESTATUS[0]}"
freezediff .venv && echo "freeze diff: empty"
$NN .venv/bin/python -c "from streetlevel import streetview" && echo "streetlevel ok"
date -u +%Y-%m-%dT%H:%M:%SZ
