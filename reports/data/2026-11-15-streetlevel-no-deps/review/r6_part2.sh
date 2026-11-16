#!/bin/bash
# Review step 6: PyPI's JSON for streetlevel (anonymous GET) plus the wheels the planner already downloaded.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HOME/claude-work/237"
D="$W/review-pypi"
mkdir -p "$D"
curl -sS --max-time 60 -o "$D/streetlevel.json" https://pypi.org/pypi/streetlevel/json; echo "curl exit: $?"
ls "$W"/dl-streetlevel-0.13.0 "$W"/dl-streetlevel-0.12.11
sha256sum "$W"/dl-streetlevel-0.13.0/*.whl "$W"/dl-streetlevel-0.12.11/*.whl
/usr/bin/python3.10 -I "$HERE/r6_part2.py" "$D/streetlevel.json" "$W"/dl-streetlevel-0.13.0/*.whl "$W"/dl-streetlevel-0.12.11/*.whl
