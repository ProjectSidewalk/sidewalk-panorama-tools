#!/bin/bash
# #237 measurement, step 5: the "before" venv - what master's documented install produces today.
# Run as:  wsl -d Ubuntu -- bash <this file>      (downloads several GB of wheels)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HOME/claude-work/237"
cd "$W" || exit 1
V="$W/venv-before"
if [ -e "$V" ]; then echo "$V already exists; move it aside by hand (this script deletes nothing)"; exit 1; fi

echo "== repo"; git -C repo log --oneline -1
date -u +%Y-%m-%dT%H:%M:%SZ
~/.local/bin/uv venv --seed --python /usr/bin/python3.10 "$V" || exit 1
"$V/bin/python" -m pip install -q --upgrade pip
"$V/bin/pip" --version
echo "== pip install -r requirements.txt -c constraints.txt   (master's files, no cache)"
"$V/bin/pip" install --no-cache-dir -r repo/requirements.txt -c repo/constraints.txt 2>&1 | tail -3
echo "== freeze"; "$V/bin/pip" freeze | tee "$HERE/05_before_freeze.txt" | wc -l
echo "== heavy distributions present"
grep -i -E '^(torch|triton|pyequilib|nvidia|cuda)' "$HERE/05_before_freeze.txt"
echo "== size"; du -sh "$V"; du -sm "$V"
echo "== the twenty largest packages (MB)"
du -sm "$V"/lib/python3.10/site-packages/* | sort -rn | head -20
date -u +%Y-%m-%dT%H:%M:%SZ
