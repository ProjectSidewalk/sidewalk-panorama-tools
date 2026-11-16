"""After importing the given modules, list every third-party distribution that ended up in sys.modules (#237).

The cross-check for probe_imports.py: that script finds what an import FAILS without; this one reports what
an import LOADS, by mapping each loaded top-level module to the distribution whose RECORD lists it. Run it in
any venv where the imports succeed.

(The first version used importlib.metadata.packages_distributions(). On Python 3.10 that reads only
top_level.txt, which wheels built without setuptools do not ship, so it silently left out numpy, scipy, attrs,
idna, urllib3, typing_extensions and aiohappyeyeballs. Its output is kept as 01_probe.out.txt's cross-check
section; this version reads each distribution's file list instead.)

Usage:
    <venv>/bin/python loaded_distributions.py <module> [<module> ...]
"""

import importlib
import importlib.metadata as md
import sys


def top_levels(dist):
    tops = set()
    for path in dist.files or []:
        first = path.parts[0]
        if first.endswith(('.dist-info', '.data', '.libs')) or first in ('..', '__pycache__'):
            continue
        tops.add(first[:-3] if first.endswith('.py') else first.split('.')[0])
    return tops


def main(argv):
    before = set(sys.modules)
    for module in argv[1:]:
        importlib.import_module(module)
    loaded = {name.split('.')[0] for name in set(sys.modules) - before}
    rows = []
    for dist in md.distributions():
        hit = sorted(top_levels(dist) & loaded)
        if hit:
            rows.append((dist.metadata['Name'], dist.version, hit))
    for name, version, hit in sorted(rows, key=lambda r: r[0].lower()):
        print('%s==%s  (modules: %s)' % (name, version, ', '.join(hit)))
    print('--- %d distributions loaded' % len(rows))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
