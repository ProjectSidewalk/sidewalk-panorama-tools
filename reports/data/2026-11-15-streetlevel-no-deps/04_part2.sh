#!/bin/bash
# #237 Part 2 evidence: does any streetlevel release carry the depth-offset fix (sk-zk/streetlevel#45)?
# Downloads wheels into their own directories and only READS them (unzip -p); nothing is installed or run.
# Run as:  wsl -d Ubuntu -- bash <this file>
set -u
# No unzip on this WSL: read a wheel member with the system python, isolated (-I), never importing from it.
unzip() { /usr/bin/python3 -I -c "import sys, zipfile; sys.stdout.write(zipfile.ZipFile(sys.argv[1]).read(sys.argv[2]).decode())" "$2" "$3"; }
W="$HOME/claude-work/237"
cd "$W" || exit 1
date -u +%Y-%m-%dT%H:%M:%SZ
for v in 0.12.11 0.12.13 0.13.0; do
  d="$W/dl-streetlevel-$v"
  mkdir -p "$d"
  if ! ls "$d"/*.whl >/dev/null 2>&1; then
    venv-p1/bin/pip download -q --no-deps --only-binary :all: -d "$d" "streetlevel==$v" || echo "download of $v failed"
  fi
  whl=$(ls "$d"/*.whl 2>/dev/null | head -1)
  echo "== streetlevel $v: $(basename "$whl") sha256 $(sha256sum "$whl" | cut -d' ' -f1)"
  echo "-- depth.py parse_header"
  unzip -p "$whl" streetlevel/streetview/depth.py | grep -n -A7 "def parse_header"
  echo "-- Requires-Dist / Requires-Python"
  unzip -p "$whl" "streetlevel-$v.dist-info/METADATA" | grep -E "^(Requires-Dist|Requires-Python|Version)"
  echo "-- api.find_panorama_by_id signature and imports"
  unzip -p "$whl" streetlevel/streetview/api.py | grep -n -E "^import|^from|def find_panorama_by_id\b|def build_find_panorama_by_id_request_url"
done
