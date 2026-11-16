"""Review of the #237 plan, step 3: the declared-requirement closure of a venv, computed rather than eyeballed.

For every installed distribution, reads its Requires-Dist, evaluates the markers for this interpreter (no
extras), and reports each requirement that is not installed or not satisfied by the installed version. In a
venv built by the two proposed install lines, the only unsatisfied requirements should be streetlevel's seven
unreached ones, and every installed distribution should be reachable from requirements.txt +
requirements-nodeps.txt (so no pin kept in constraints.txt is for a package nothing asks for).

    <venv>/bin/python -I <this file> <requirements.txt> <requirements-nodeps.txt>
"""

import importlib.metadata as md
import sys

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name

SEED = {'pip', 'setuptools', 'wheel'}


def roots(paths):
    names = []
    for path in paths:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.split('#')[0].strip()
                if line:
                    names.append(canonicalize_name(Requirement(line).name))
    return names


def main(argv):
    installed = {canonicalize_name(d.metadata['Name']): d for d in md.distributions()}
    edges, unsatisfied = {}, []
    for name, dist in sorted(installed.items()):
        edges[name] = []
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is not None and not req.marker.evaluate({'extra': ''}):
                continue
            target = canonicalize_name(req.name)
            edges[name].append(target)
            have = installed.get(target)
            if have is None:
                unsatisfied.append('%s requires %s: NOT INSTALLED' % (name, raw))
            elif not req.specifier.contains(have.version, prereleases=True):
                unsatisfied.append('%s requires %s: installed %s does not satisfy' % (name, raw, have.version))
    start = roots(argv[1:])
    reached, stack = set(), list(start)
    while stack:
        name = stack.pop()
        if name in reached or name not in installed:
            continue
        reached.add(name)
        stack.extend(edges[name])
    print('installed distributions (excluding pip/setuptools/wheel): %d'
          % len(set(installed) - SEED))
    print('roots from the requirement files: %s' % ', '.join(start))
    print('reached from the roots through declared requirements: %d' % len(reached))
    orphans = sorted(set(installed) - reached - SEED)
    print('installed but NOT reached from the roots: %s' % (orphans or 'none'))
    print('unsatisfied declared requirements (%d):' % len(unsatisfied))
    for line in unsatisfied:
        print('    ' + line)
    for name in sorted(reached):
        print('    %-24s %-12s -> %s' % (name, installed[name].version, ', '.join(sorted(set(edges[name]))) or '-'))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
