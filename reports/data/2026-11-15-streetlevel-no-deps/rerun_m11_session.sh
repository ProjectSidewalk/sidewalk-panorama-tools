#!/bin/bash
# #237 re-run, M11: does test_the_real_find_panorama_by_id_uses_the_session_it_is_given discriminate? In a throwaway
# venv built with the branch's two install lines (plus the dev requirements for pytest), run it on streetlevel as
# installed, then patch the INSTALLED streetlevel/streetview/api.py so find_panorama_by_id ignores its session
# (`session=None`), and run it again: it must fail (the fallback reaches for the network, which the suite refuses).
# The patch is in this venv only; nothing in the repo is touched.
# Run as:  wsl -d Ubuntu -- bash <this file>       Deletes nothing; refuses an existing venv directory.
set -u
W="$HOME/claude-work/237"
V="$W/venv-impl-m11"
REPO="$W/impl-repo"
if [ -e "$V" ]; then echo "$V already exists; this script deletes nothing"; exit 1; fi
date -u +%Y-%m-%dT%H:%M:%SZ
cd "$REPO" || exit 1
git log --oneline -1
~/.local/bin/uv venv --seed --python /usr/bin/python3.10 "$V" || exit 1
"$V/bin/python" -m pip install -q --upgrade pip
"$V/bin/pip" install -q -r requirements.txt -r requirements-dev.txt -c constraints.txt -c constraints-ci.txt; echo "line 1 exit: $?"
"$V/bin/pip" install -q --no-deps -r requirements-nodeps.txt -c constraints.txt -c constraints-ci.txt; echo "line 2 exit: $?"
T="tests/test_streetlevel_api.py::test_the_real_find_panorama_by_id_uses_the_session_it_is_given"
echo "== unpatched"
SIDEWALK_REQUIRE_STREETLEVEL=1 "$V/bin/python" -m pytest -p no:cacheprovider -q --tb=line "$T" 2>&1 | tail -3
API="$V/lib/python3.10/site-packages/streetlevel/streetview/api.py"
echo "== patch: find_panorama_by_id passes session=None"
grep -n "def find_panorama_by_id(" -A 3 "$API" | head -4
# Only the first `session=session,` after `def find_panorama_by_id(` - the sync function's.
"$V/bin/python" -c "
import sys
p = sys.argv[1]
t = open(p, encoding='utf-8').read()
head, sep, tail = t.partition('def find_panorama_by_id(')
assert sep and tail.count('session=session,') >= 1
tail = tail.replace('session=session,', 'session=None,', 1)
open(p, 'w', encoding='utf-8').write(head + sep + tail)
" "$API"
grep -n "def find_panorama_by_id(" -A 3 "$API" | head -4
echo "== patched (expect the test to FAIL)"
SIDEWALK_REQUIRE_STREETLEVEL=1 "$V/bin/python" -m pytest -p no:cacheprovider -q --tb=line "$T" 2>&1 | tail -4
echo "== clone still clean?"; git -C "$REPO" status --short | head
date -u +%Y-%m-%dT%H:%M:%SZ
