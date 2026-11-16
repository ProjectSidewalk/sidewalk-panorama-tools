#!/bin/bash
# Review step 7: r4 installed CoordinatesConverter from pip's wheel CACHE, so the sdist build the box would do
# was not exercised. Rebuild it from the sdist with no cache, under pip 22.0.2 / setuptools 59.6.0 / no wheel
# (the Ubuntu 22.04 `python3 -m venv` seed), in the r4 venv. Then re-run the deploy import checks.
set -u
W="$HOME/claude-work/237"
V="$W/venv-review-pip22d"
export SETUPTOOLS_USE_DISTUTILS=stdlib
pip22() { PYTHONPATH="$V/shim" "$V/bin/python" -m pip "$@"; }
pip22 --version
pip22 list --format=freeze | grep -iE '^(pip|setuptools|wheel)=='
echo "== what the sdist ships (pyproject.toml or setup.py only?)"
D="$W/review-sdist"; mkdir -p "$D"
pip22 download --no-cache-dir --no-deps --no-binary :all: -d "$D" 'CoordinatesConverter==0.1.5' 2>&1 | tail -2
tar -tzf "$D"/[Cc]oordinates[Cc]onverter-0.1.5.tar.gz | grep -E "(setup.py|setup.cfg|pyproject.toml|__init__.py)$"; echo "(build files only; pyproject.toml absent => pip 22.0.2 without wheel takes the legacy setup.py install path)"
echo "== reinstall from the sdist, no cache"
pip22 install --no-cache-dir --force-reinstall --no-deps --no-binary :all: 'CoordinatesConverter==0.1.5' 2>&1 | tail -6
echo "exit: ${PIPESTATUS[0]}"
pip22 freeze | grep -i coordinates
cd "$W/repo" && "$V/bin/python" -c "import DownloadRunner, scrape_queue, cron_notify; from streetlevel import streetview; import CoordinatesConverter; print('imports ok')"
