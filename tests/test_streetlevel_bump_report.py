"""reports/2026-11-17-streetlevel-0.13.0.md (#237 Part 2): its quoted numbers against the committed raw outputs.

Every number in the report's prose is transcribed from a file in reports/data/2026-11-17-streetlevel-0.13.0/
(.claude/rules/desk-studies.md). This test re-derives those numbers from the files and checks that the report
quotes them. Like tests/test_streetlevel_no_deps_report.py it reads only the report and its own data, never the
live requirement or pin files, which are meant to move on.
"""

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
