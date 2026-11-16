#!/bin/bash
# Review step 9: which platforms have wheels for the five new requirements at their pins (PyPI JSON, anonymous).
# The plan's docs edit claims "wheels on Linux/Windows/macOS for CPython 3.10-3.13 except CoordinatesConverter".
set -u
D="$HOME/claude-work/237/review-pypi"; mkdir -p "$D"
for spec in pyexiv2/2.16.0 pyproj/3.7.1 scipy/1.15.3 bd09convertor/0.0.3 CoordinatesConverter/0.1.5; do
  f="$D/$(echo $spec | tr / _).json"
  curl -sS --max-time 60 -o "$f" "https://pypi.org/pypi/$spec/json"
  /usr/bin/python3.10 -I -c "
import json, sys
d = json.load(open(sys.argv[1]))
names = [u['filename'] for u in d['urls']]
def has(tag): return sorted({n.split('-')[-3] for n in names if n.endswith('.whl') and tag in n.split('-')[-1]})
print('%-24s sdist=%s  pure=%s' % (sys.argv[2], any(n.endswith('.tar.gz') for n in names), any(n.endswith('-none-any.whl') for n in names)))
for plat in ('manylinux', 'win_amd64', 'macosx'):
    print('    %-10s %s' % (plat, ' '.join(has(plat)) or '-'))
" "$f" "$spec"
done
