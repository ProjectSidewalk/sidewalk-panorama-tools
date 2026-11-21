"""Mutate every number in the 0.13.0 report's prose (outside backticks and links), one at a time, and run the
report test against each. Prints the survivors. Restores the report from memory at the end, whatever happens."""
import re
import subprocess
import sys

wt, py = sys.argv[1], sys.argv[2]
path = wt + '/reports/2026-11-17-streetlevel-0.13.0.md'
original = open(path, encoding='utf-8', newline='').read()

protected = []
for m in re.finditer(r'`[^`]*`|\]\([^)]*\)|\[[^\]]*\]\(', original):
    protected.append((m.start(), m.end()))


def is_protected(i):
    return any(a <= i < b for a, b in protected)


tokens = [m for m in re.finditer(r'\d+(?:\.\d+)*', original) if not is_protected(m.start())]
survivors = []
try:
    for m in tokens:
        tok = m.group(0)
        last = tok[-1]
        new = tok[:-1] + str((int(last) + 1) % 10)
        mutated = original[:m.start()] + new + original[m.end():]
        open(path, 'w', encoding='utf-8', newline='').write(mutated)
        r = subprocess.run([py, '-m', 'pytest', 'tests/test_streetlevel_bump_report.py', '-q', '-x',
                            '-p', 'no:cacheprovider'], cwd=wt, capture_output=True, text=True)
        line_no = original.count('\n', 0, m.start()) + 1
        if r.returncode == 0:
            survivors.append((line_no, tok, new, original[max(0, m.start() - 40):m.end() + 20].replace('\n', ' ')))
finally:
    open(path, 'w', encoding='utf-8', newline='').write(original)

print('tokens mutated: %d, survivors: %d' % (len(tokens), len(survivors)))
for s in survivors:
    print('L%d %s -> %s :: %s' % s)
