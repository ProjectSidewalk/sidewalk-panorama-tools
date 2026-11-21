"""reports/2026-11-17-streetlevel-0.13.0.md (#237 Part 2): its quoted numbers against the committed raw outputs.

The numbers the report's findings rest on are transcribed from files in
reports/data/2026-11-17-streetlevel-0.13.0/ (.claude/rules/desk-studies.md). This test re-derives those numbers
from the files and checks that the report quotes them. It does not hold every number in the prose: the report's
last section says which kinds it leaves out, and fix1_report_number_sweep.out.txt lists each one. Like
tests/test_streetlevel_no_deps_report.py it reads only the report and its own data, never the live requirement or
pin files, which are meant to move on.
"""

import fnmatch
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-17-streetlevel-0.13.0')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-11-17-streetlevel-0.13.0.md')


def _read(name):
    with open(os.path.join(DATA, name), encoding='utf-8') as f:
        return f.read()


def _report():
    """The report with every run of whitespace collapsed, so a quote may wrap across lines."""
    with open(REPORT, encoding='utf-8') as f:
        return ' '.join(f.read().split())


def _summary(text):
    """The last pytest summary line (`N failed, M passed, ...`) in a run's output."""
    return re.findall(r'^(\d+ (?:failed|passed)[^\n]*?) in [\d.]+s$', text, re.M)[-1]


def test_both_wheels_declare_the_same_16_requirements():
    text = _read('planning_win_p2_wheel_diff_0.12.11_0.13.0.out.txt')
    old = text.split('== old wheel', 1)[1].split('== new wheel', 1)[0]
    new = text.split('== new wheel', 1)[1].split('\n==', 1)[0]
    declared = [[line.strip() for line in part.splitlines() if line.strip().startswith('Requires-Dist:')]
                for part in (old, new)]
    assert len(declared[0]) == len(declared[1]) == 16
    assert declared[0] == declared[1]
    assert 'The declared requirements are the same 16 in both wheels' in _report()


def test_the_wheel_metadata_version_moved_from_2_4_to_2_5():
    text = _read('review_r_wheels.out.txt')
    versions = re.findall(r"^streetlevel-(\S+)-py3-none-any\.whl [0-9a-f]{64}\n\s+\['Metadata-Version: ([\d.]+)'",
                          text, re.M)
    assert versions == [('0.12.11', '2.4'), ('0.13.0', '2.5')]
    assert '`Metadata-Version` went from %s to %s' % (versions[0][1], versions[1][1]) in _report()


def test_the_fix_merged_and_shipped_on_the_dates_quoted():
    text = _read('planning_win_p2_upstream_github.out.txt')
    merged = re.search(r'"merged_at":"(\d{4}-\d\d-\d\d)T', text).group(1)
    released = re.search(r'^v0\.13\.0 (\d{4}-\d\d-\d\d)T', text.split('== releases', 1)[1], re.M).group(1)
    assert (merged, released) == ('2026-11-04', '2026-11-12')
    report = _report()
    assert '#45 merged on %s' % merged in report
    assert 'shipped in 0.13.0 on %s' % released in report


def test_the_weekly_job_collected_the_capped_range_and_installed_0_12_13():
    """The measured reason for dropping the cap: the weekly unpinned job resolved `<0.13` four days after 0.13.0
    was uploaded, installed 0.12.13, and the tripwire passed there."""
    text = _read('impl_weekly_run_43980786100.out.txt')
    run_date = re.search(r' event=schedule created=(\d{4}-\d\d-\d\d)T', text).group(1)
    collected = re.search(r'^Collecting (streetlevel\S+) ', text, re.M).group(1)
    installed = re.search(r'^Successfully installed streetlevel-(\S+)$', text, re.M).group(1)
    assert (run_date, collected, installed) == ('2026-11-16', 'streetlevel<0.13,>=0.12.11', '0.12.13')
    assert re.search(r'^tests/test_streetlevel_api\.py::test_streetlevel_still_misreads_the_depth_offset PASSED',
                     text, re.M)
    uploaded = re.search(r'^\s+0\.13\.0\s+(\d{4})-(\d\d)-(\d\d)T.*\.whl',
                         _read('planning_win_p2_wheel_diff_0.12.11_0.13.0.out.txt'), re.M).groups()
    assert uploaded == ('2026', '11', '12') and int(run_date[-2:]) - int(uploaded[2]) == 4
    report = _report()
    assert 'ran on %s as run 43980786100, four days after 0.13.0 was uploaded' % run_date in report
    assert 'logged `Collecting %s` and installed %s, so the tripwire passed there' % (collected, installed) in report


def test_every_data_file_the_report_cites_is_committed():
    """A backticked `planning_win_*`, `review_*`, `impl_*` or `fix1_*` name in the report is a file in its data
    folder, and each of those prefixes, which the report lists as globs, matches at least one. Part 1's files are cited
    with their folder (`review/...`), so this pattern does not reach them."""
    with open(REPORT, encoding='utf-8') as f:
        cited = set(re.findall(r'`((?:planning_win|review|impl|fix1)_[^`/\s]+)`', f.read()))
    assert len(cited) >= 25, sorted(cited)
    committed = os.listdir(DATA)
    missing = sorted(name for name in cited if not fnmatch.filter(committed, name))
    assert not missing, 'the report cites data files that are not committed: %s' % missing


def test_the_review_rounds_mutation_counts():
    lines = [line for line in _read('fix1_report_mutants.out.txt').splitlines()
             if line.startswith(('KILLED', 'SURVIVED'))]
    deletions = [line for line in lines if ' delete ' in line]
    assert (len(lines) - len(deletions), len(deletions)) == (11, 3)
    assert not [line for line in lines if line.startswith('SURVIVED')]
    sweep_text = _read('fix1_report_number_sweep.out.txt')
    sweep = re.search(r'^tokens mutated: (\d+), survivors: (\d+)$', sweep_text, re.M)
    listed = re.findall(r'^L\d+ ', sweep_text, re.M)
    assert len(listed) == int(sweep.group(2)), 'the sweep header says %s survivors and lists %d' % (
        sweep.group(2), len(listed))
    report = _report()
    assert 'makes %d edits and %d deletions, one at a time, and each one fails the test' % (
        len(lines) - len(deletions), len(deletions)) in report
    assert 'one at a time: %s of %s still pass' % (sweep.group(2), sweep.group(1)) in report


def test_the_release_is_16_commits_past_0_12_13():
    match = re.search(r'compare v0\.12\.13\.\.\.v0\.13\.0\nstatus=ahead ahead_by=(\d+) total_commits=(\d+)',
                      _read('planning_win_p2_upstream_github.out.txt'))
    assert match.groups() == ('16', '16')
    assert '`v0.12.13...v0.13.0` is 16 commits' in _report()


def test_the_freeze_moves_by_one_streetlevel_line():
    before = set(_read('planning_win_p2_box_like_freeze_before.txt').split())
    after = set(_read('planning_win_p2_box_like_freeze_after.txt').split())
    assert before - after == {'streetlevel==0.12.11'}
    assert after - before == {'streetlevel==0.13.0'}
    assert 'differs in one line, `streetlevel==0.12.11` to `streetlevel==0.13.0`' in _report()


def test_the_two_tests_that_failed_on_0_13_0_are_the_ones_the_report_names():
    failed = re.findall(r'^FAILED (\S+)', _read('planning_win_p2_ci_like_0.13.0.out.txt'), re.M)
    assert failed == ['tests/test_streetlevel_api.py::test_streetlevel_still_misreads_the_depth_offset',
                      'tests/test_suite_isolation.py::TestStreetlevelMustImportWhenRequired::'
                      'test_with_the_variable_the_contract_tests_are_collected_and_run']
    report = _report()
    assert all('`%s`' % test_id in report for test_id in failed)


def test_the_pass_and_fail_counts():
    baseline = _summary(_read('planning_win_p2_ci_like_baseline_0.12.11.out.txt'))
    bumped = _summary(_read('planning_win_p2_ci_like_0.13.0.out.txt'))
    assert (baseline, bumped) == ('484 passed, 17 skipped', '2 failed, 482 passed, 17 skipped')
    assert '%s on 0.12.11; %s on 0.13.0' % (baseline, bumped) in _report()
    # The replacement tests: red on both old releases, green on the pin, and every mutant killed. Each is one
    # row of the report's evidence table, whose result cell starts with the count.
    with open(REPORT, encoding='utf-8') as f:
        rows = [line for line in f if line.startswith('|')]
    for name, expected in (('impl_E_0.12.11.out.txt', '2 failed'), ('impl_E_0.12.13.out.txt', '2 failed'),
                           ('impl_E_0.13.0.out.txt', '2 passed'), ('impl_M1.out.txt', '2 failed'),
                           ('impl_M2.out.txt', '2 failed')):
        assert _summary(_read(name)) == expected, name
        row = [line for line in rows if line.rstrip().endswith('| `%s` |' % name)]
        assert len(row) == 1 and row[0].split('|')[2].strip().startswith(expected), (name, row)


class TestNoLocalPathIsCommitted:
    """The raw outputs had local path prefixes normalised before commit (the report says how). The pattern list
    is copied from tests/test_streetlevel_no_deps_report.py, not imported: each report's test stands alone."""

    PATTERNS = [re.compile(p) for p in (r'AppData', r'/mnt/c/Users', r'/home/[^/\s]+/',
                                        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')]

    def test_no_file_carries_one(self):
        hits = []
        for folder, _, files in os.walk(DATA):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, encoding='utf-8', errors='replace') as f:
                    for number, line in enumerate(f, 1):
                        if any(p.search(line) for p in self.PATTERNS):
                            hits.append('%s:%d' % (os.path.relpath(path, DATA), number))
        assert not hits, hits[:20]
