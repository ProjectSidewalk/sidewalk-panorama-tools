"""Mutants for the review-round report tests (#252 findings 2 and 6). Each edits one file, runs the report test,
and restores the file byte for byte. Prints KILLED/SURVIVED per mutant."""
import os
import shutil
import subprocess
import sys

wt, py = sys.argv[1], sys.argv[2]
REPORT = 'reports/2026-11-17-streetlevel-0.13.0.md'
DATA = 'reports/data/2026-11-17-streetlevel-0.13.0/'

MUTANTS = [
    ('report: installed 0.12.12 (review mutant 1)', REPORT, 'and installed 0.12.13, so', 'and installed 0.12.12, so'),
    ('report: Metadata 2.4 -> 2.6 (review mutant 2)', REPORT, 'from 2.4 to 2.5', 'from 2.4 to 2.6'),
    ('report: Metadata 2.3 -> 2.5', REPORT, 'from 2.4 to 2.5', 'from 2.3 to 2.5'),
    ('report: Collecting <0.14', REPORT, '`Collecting streetlevel<0.13,>=0.12.11`', '`Collecting streetlevel<0.14,>=0.12.11`'),
    ('report: run date 11-17', REPORT, 'ran on 2026-11-16 as run', 'ran on 2026-11-17 as run'),
    ('report: five days after', REPORT, 'four days after 0.13.0', 'five days after 0.13.0'),
    ('report: #45 merged 11-05', REPORT, '#45 merged on 2026-11-04', '#45 merged on 2026-11-05'),
    ('report: shipped 11-13', REPORT, 'shipped in 0.13.0 on 2026-11-12', 'shipped in 0.13.0 on 2026-11-13'),
    ('report: cite a file that does not exist', REPORT, '(`impl_weekly_run_43980786100.out.txt`)', '(`impl_weekly_run_43980786101.out.txt`)'),
    ('data: tripwire FAILED in weekly log', DATA + 'impl_weekly_run_43980786100.out.txt', 'offset PASSED', 'offset FAILED'),
    ('data: weekly installed 0.13.0', DATA + 'impl_weekly_run_43980786100.out.txt', 'Successfully installed streetlevel-0.12.13', 'Successfully installed streetlevel-0.13.0'),
]
DELETES = [DATA + 'review_r_wheels.out.txt', DATA + 'impl_weekly_run_43980786100.out.txt', DATA + 'impl_scrub.py']


def run():
    r = subprocess.run([py, '-m', 'pytest', 'tests/test_streetlevel_bump_report.py', '-q', '-p', 'no:cacheprovider'],
                       cwd=wt, capture_output=True, text=True)
    return r.returncode, r.stdout.strip().splitlines()[-1]


print('baseline', run())
for label, rel, old, new in MUTANTS:
    path = os.path.join(wt, rel)
    original = open(path, 'rb').read()
    text = original.decode('utf-8')
    assert text.count(old) == 1, (label, text.count(old))
    try:
        open(path, 'wb').write(text.replace(old, new).encode('utf-8'))
        code, summary = run()
    finally:
        open(path, 'wb').write(original)
    print('%-8s %s :: %s' % ('KILLED' if code else 'SURVIVED', label, summary))
for rel in DELETES:
    path = os.path.join(wt, rel)
    keep = path + '.mutant-keep'
    shutil.move(path, keep)
    try:
        code, summary = run()
    finally:
        shutil.move(keep, path)
    print('%-8s delete %s :: %s' % ('KILLED' if code else 'SURVIVED', os.path.basename(rel), summary))
print('after', run())
