#!/bin/bash
# #237 measurement, step 2: in the venv step 1 left (streetlevel --no-deps + the 21 distributions the import
# reached), list what the imports load and every import statement in the streetlevel modules they load.
# Run as:  wsl -d Ubuntu -- bash <this file>
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HOME/claude-work/237" || exit 1
MODS="streetlevel streetlevel.streetview streetlevel.streetview.api streetlevel.streetview.panorama streetlevel.streetview.depth streetlevel.streetview.parse streetlevel.dataclasses"

echo "== distributions loaded (RECORD-based)"
venv-p1/bin/python -W ignore "$HERE/loaded_distributions.py" $MODS

echo "== every import statement in the loaded streetlevel modules"
venv-p1/bin/python -W ignore "$HERE/static_imports.py" $MODS

echo "== the same, for the production imports only"
venv-p1/bin/python -W ignore "$HERE/static_imports.py" streetlevel streetlevel.streetview streetlevel.streetview.api | tail -12

echo "== what streetlevel/streetview/__init__.py and api.py import"
cat venv-p1/lib/python3.10/site-packages/streetlevel/streetview/__init__.py
echo "-- api.py imports"
grep -n "^import\|^from\|^\s\+import \|^\s\+from .* import" venv-p1/lib/python3.10/site-packages/streetlevel/streetview/api.py
echo "-- depth.py parse_header"
grep -n -A12 "def parse_header" venv-p1/lib/python3.10/site-packages/streetlevel/streetview/depth.py
echo "-- packages in the streetlevel wheel"
ls venv-p1/lib/python3.10/site-packages/streetlevel
