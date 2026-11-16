#!/bin/bash
# Review of the #237 plan, step 1: read streetlevel 0.12.11's own source in venv-after.
# Run as: wsl -d Ubuntu -- bash <this file>
set -u
V="$HOME/claude-work/237/venv-after"
S="$V/lib/python3.10/site-packages/streetlevel"
echo "== pip and python in venv-after"; "$V/bin/pip" --version
echo "== streetlevel/__init__.py"; cat "$S/__init__.py"; echo "(eof)"
echo "== streetlevel/streetview/__init__.py"; cat "$S/streetview/__init__.py"; echo "(eof)"
echo "== streetlevel/util.py (whole)"; cat -n "$S/util.py"
echo "== api.py find_panorama_by_id (the URL builder's body is not printed; r2 runs it)"; sed -n '185,215p' "$S/streetview/api.py"
echo "== every metadata / dynamic-import / optional-import construct anywhere in the installed package"
grep -rnE "importlib|pkg_resources|__import__|import_module|metadata\.|require\(|except ImportError|except ModuleNotFoundError|find_spec" "$S" --include=*.py || echo "(none)"
echo "== every 'import' or 'from' statement that is indented (inside a def/class/if/try) anywhere in the package"
grep -rnE "^[[:space:]]+(import |from [.a-zA-Z_]+ import )" "$S" --include=*.py || echo "(none)"
