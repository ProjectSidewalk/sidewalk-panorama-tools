"""Review of the #237 plan, step 0: derive the reduced constraints.txt and a CI-only pin file mechanically.

Splits master's constraints.txt (the box's 65-line freeze) three ways, keeping every line verbatim and in order:
  box  - names in the stage-A freeze of the proposed install (03_after_stageA_freeze.txt), minus `packaging`,
         which uv's `venv --seed` put there (a `python3 -m venv` does not);
  ci   - the CI-only set the gated job installs on purpose and master pins today: the pytest runner's own
         requirements and the CPU torch line's (CI_ONLY below);
  gone - everything else, the GPU stack and streetlevel's other unreached requirements.
Writes <out>/constraints-reduced.txt and <out>/constraints-ci.txt (bodies only, no header) and prints the split.

    python3 -I <this file> <master constraints.txt> <stageA freeze> <out dir>
"""

import os
import re
import sys

PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$')
CI_ONLY = {'pytest', 'pluggy', 'iniconfig', 'exceptiongroup', 'tomli', 'packaging', 'pygments',
           'torch', 'filelock', 'fsspec', 'jinja2', 'markupsafe', 'mpmath', 'networkx', 'sympy'}


def canon(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def main(constraints, freeze, out):
    with open(freeze, encoding='utf-8') as f:
        after = {canon(PIN.match(l.strip()).group(1)) for l in f if PIN.match(l.strip())}
    after.discard('packaging')
    box, ci, gone = [], [], []
    with open(constraints, encoding='utf-8') as f:
        for line in f:
            m = PIN.match(line.strip())
            if not m:
                continue
            name = canon(m.group(1))
            (box if name in after else ci if name in CI_ONLY else gone).append(line.strip())
    assert len(box) + len(ci) + len(gone) == 65, (len(box), len(ci), len(gone))
    assert {canon(PIN.match(l).group(1)) for l in box} == after, 'a stage-A package has no master pin'
    assert {canon(PIN.match(l).group(1)) for l in ci} == CI_ONLY, 'a CI-only name has no master pin'
    for name, lines in (('constraints-reduced.txt', box), ('constraints-ci.txt', ci)):
        with open(os.path.join(out, name), 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
    print('box (kept in constraints.txt): %d' % len(box)); print('    ' + ' '.join(box))
    print('ci  (moved to constraints-ci.txt): %d' % len(ci)); print('    ' + ' '.join(ci))
    print('gone (deleted): %d' % len(gone)); print('    ' + ' '.join(gone))
    return 0


if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:]))
