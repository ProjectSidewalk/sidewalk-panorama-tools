#!/bin/bash
# #237 measurement, step 1: what does importing streetlevel (installed --no-deps) actually reach?
# Run as:  wsl -d Ubuntu -- bash <this file>     (a script file, so zsh never expands anything in it)
# Writes its transcript to stdout; the caller redirects it into 01_probe.out.txt.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HOME/claude-work/237"
mkdir -p "$W"
cd "$W" || exit 1

echo "== host"; lsb_release -ds; python3 --version; date -u +%Y-%m-%dT%H:%M:%SZ

if [ ! -d repo ]; then
  git -c safe.directory='*' clone -q /mnt/d/Git/sidewalk-panorama-tools repo || exit 1
fi
echo "== repo"; git -C repo log --oneline -1

if [ -e venv-p1 ]; then echo "venv-p1 already exists; move it aside by hand (this script deletes nothing)"; exit 1; fi
# WSL here has no python3.10-venv (no ensurepip) and no sudo, so the venv is made with uv, seeded with pip, on
# the system interpreter. `uv` is used ONLY to create the venv; every install below is plain pip, as on the box.
~/.local/bin/uv venv --seed --python /usr/bin/python3.10 venv-p1 || exit 1
venv-p1/bin/python -m pip install -q --upgrade pip
echo "== pip"; venv-p1/bin/pip --version

echo "== install streetlevel alone, --no-deps, at the constraints pin"
venv-p1/bin/pip install --no-deps -c repo/constraints.txt streetlevel 2>&1 | tail -2
venv-p1/bin/pip freeze

echo "== declared requirements of the installed streetlevel"
venv-p1/bin/python -c "import importlib.metadata as m; print(m.version('streetlevel')); print('\n'.join(sorted(m.requires('streetlevel'))))"

echo "== tier P: the three imports production makes (downloaders/gsv.py:1744,1777,2048; scrape_queue.probe_streetlevel)"
venv-p1/bin/python "$HERE/probe_imports.py" repo/constraints.txt "$HERE/01_probe_production.json" \
    streetlevel streetlevel.streetview streetlevel.streetview.api
echo "probe exit: $?"
echo "== freeze after tier P"; venv-p1/bin/pip freeze

echo "== tier T: the extra modules only tests/test_streetlevel_api.py imports"
venv-p1/bin/python "$HERE/probe_imports.py" repo/constraints.txt "$HERE/01_probe_tests.json" \
    streetlevel.streetview.panorama streetlevel.streetview.depth streetlevel.streetview.parse streetlevel.dataclasses
echo "probe exit: $?"
echo "== freeze after tier T"; venv-p1/bin/pip freeze

echo "== cross-check: distributions loaded by those imports"
venv-p1/bin/python "$HERE/loaded_distributions.py" streetlevel streetlevel.streetview streetlevel.streetview.api \
    streetlevel.streetview.panorama streetlevel.streetview.depth streetlevel.streetview.parse streetlevel.dataclasses

echo "== pip check (expected to complain about streetlevel's undeclared-as-installed requirements)"
venv-p1/bin/pip check
echo "pip check exit: $?"

echo "== size"; du -sh venv-p1; du -sm venv-p1
