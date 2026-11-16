#!/bin/bash
# #237 re-run, CI-like: the gated job's install sequence from tests.yml as this PR leaves it, read from a clone of
# the PR branch, then the full suite twice - before the CPU torch lines (what a job that lost torch would run) and
# after them (what the gated job runs) - with SIDEWALK_REQUIRE_STREETLEVEL=1 and --cov, as CI runs it.
# Run as:  wsl -d Ubuntu -- bash <this file>       Deletes nothing; refuses an existing venv directory.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HOME/claude-work/237"
V="$W/venv-impl-ci"
REPO="$W/impl-repo"
if [ -e "$V" ]; then echo "$V already exists; this script deletes nothing"; exit 1; fi
date -u +%Y-%m-%dT%H:%M:%SZ
echo "== repo"; git -C "$REPO" log --oneline -1; git -C "$REPO" status --short | head
~/.local/bin/uv venv --seed --python /usr/bin/python3.10 "$V" || exit 1
"$V/bin/python" -m pip install -q --upgrade pip
"$V/bin/pip" --version
cd "$REPO" || exit 1
C="-c constraints.txt -c constraints-ci.txt"

echo "== step 1: pip install -r requirements.txt -r requirements-dev.txt $C"
"$V/bin/pip" install -r requirements.txt -r requirements-dev.txt $C > "$HERE/rerun_ci_like_step1.log" 2>&1; echo "exit: $?"
tail -2 "$HERE/rerun_ci_like_step1.log"
echo "== step 1: any torch-family download? (expect none)"
grep -i -E '(Collecting|Downloading) (torch|triton|pyequilib|nvidia|cuda)' "$HERE/rerun_ci_like_step1.log"; echo "grep exit: $? (1 = none)"
echo "== step 2: pip install --no-deps -r requirements-nodeps.txt $C"
"$V/bin/pip" install --no-deps -r requirements-nodeps.txt $C 2>&1 | tail -2; echo "exit: ${PIPESTATUS[0]}"
"$V/bin/pip" freeze > "$HERE/rerun_ci_like_freeze_notorch.txt"
echo "== freeze before torch: $(wc -l < "$HERE/rerun_ci_like_freeze_notorch.txt") lines; heavy distributions (expect none):"
grep -i -E '^(torch|triton|pyequilib|nvidia|cuda)' "$HERE/rerun_ci_like_freeze_notorch.txt"; echo "grep exit: $? (1 = none)"
echo "== size before torch"; du -sm "$V"
echo "== suite without torch: SIDEWALK_REQUIRE_STREETLEVEL=1, --cov"
SIDEWALK_REQUIRE_STREETLEVEL=1 "$V/bin/python" -m pytest tests -q -rs --continue-on-collection-errors --cov --cov-report=term-missing:skip-covered -p no:cacheprovider > "$HERE/rerun_ci_like_notorch_pytest.txt" 2>&1; echo "pytest exit: $?"
tail -3 "$HERE/rerun_ci_like_notorch_pytest.txt"

echo "== step 3: torch's requirements"
"$V/bin/pip" install $C filelock typing_extensions sympy networkx jinja2 fsspec 2>&1 | tail -1; echo "exit: ${PIPESTATUS[0]}"
echo "== step 4: CPU-only torch, --no-deps"
"$V/bin/pip" install $C --no-deps torch --index-url https://download.pytorch.org/whl/cpu 2>&1 | tail -1; echo "exit: ${PIPESTATUS[0]}"
echo "== step 5: import torch"
"$V/bin/python" -c "import torch; print(torch.__version__)"; echo "exit: $?"
"$V/bin/pip" freeze > "$HERE/rerun_ci_like_freeze_torch.txt"
echo "== freeze with torch: $(wc -l < "$HERE/rerun_ci_like_freeze_torch.txt") lines; heavy distributions:"
grep -i -E '^(torch|triton|pyequilib|nvidia|cuda|pytest==)' "$HERE/rerun_ci_like_freeze_torch.txt"
echo "== size with torch"; du -sm "$V"
echo "== suite with torch: SIDEWALK_REQUIRE_STREETLEVEL=1, --cov"
SIDEWALK_REQUIRE_STREETLEVEL=1 "$V/bin/python" -m pytest tests -q -rs --continue-on-collection-errors --cov --cov-report=term-missing:skip-covered -p no:cacheprovider > "$HERE/rerun_ci_like_torch_pytest.txt" 2>&1; echo "pytest exit: $?"
tail -3 "$HERE/rerun_ci_like_torch_pytest.txt"
echo "== the two modules CI must run, verbose"
SIDEWALK_REQUIRE_STREETLEVEL=1 "$V/bin/python" -m pytest tests/test_torch_geometry.py tests/test_streetlevel_api.py -q -rs -p no:cacheprovider 2>&1 | tail -3
echo "== clone still clean?"; git -C "$REPO" status --short | head
date -u +%Y-%m-%dT%H:%M:%SZ
