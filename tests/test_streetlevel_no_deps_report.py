"""reports/2026-11-15-streetlevel-no-deps.md (#237): its headline numbers against the committed raw outputs.

Every number in the report's prose is transcribed from a file in reports/data/2026-11-15-streetlevel-no-deps/
(.claude/rules/desk-studies.md). This test re-derives the headline ones from those files and checks that the
report quotes them, and that tests/test_constraints.py's two requirement sets are the measured split rather than
a hand-typed copy of it. The behaviour those sets enforce is tested in tests/test_constraints.py.
"""

import json
import os
import re

import pytest
from packaging.utils import canonicalize_name

from test_constraints import STREETLEVEL_REACHED, STREETLEVEL_UNREACHED, _content_lines

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-15-streetlevel-no-deps')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-11-15-streetlevel-no-deps.md')


def _read(*parts):
    with open(os.path.join(DATA, *parts), encoding='utf-8') as f:
        return f.read()


def _report():
    with open(REPORT, encoding='utf-8') as f:
        return f.read()


def _section(text, start, end):
    """The lines strictly between the line starting `start` and the next line starting `end`."""
    lines = text.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith(start))
    j = next(n for n in range(i + 1, len(lines)) if lines[n].startswith(end))
    return lines[i + 1:j]


def _declared():
    """streetlevel 0.12.11's declared requirement names, as 01_probe.sh printed them (the first line is its
    version)."""
    lines = _section(_read('01_probe.out.txt'), '== declared requirements', '== tier P')
    assert lines[0] == '0.12.11'
    return {canonicalize_name(re.split(r'[<>=!~ ;]', line, maxsplit=1)[0]) for line in lines[1:]}


def _closure():
    with open(os.path.join(DATA, '01_probe_production.json'), encoding='utf-8') as f:
        return [canonicalize_name(name) for name in json.load(f)['installed_in_order']]


class TestTheSplit:

    def test_16_declared_9_reached_7_not_and_21_in_the_closure(self):
        declared, closure = _declared(), _closure()
        assert len(declared) == 16
        assert len(closure) == len(set(closure)) == 21
        reached = declared & set(closure)
        assert (len(reached), len(declared - reached)) == (9, 7)

    def test_the_constants_in_test_constraints_are_the_measured_split(self):
        declared = _declared()
        reached = declared & set(_closure())
        assert STREETLEVEL_REACHED == reached
        assert STREETLEVEL_UNREACHED == declared - reached

    @pytest.mark.parametrize('phrase', ['16 requirements', '21 distributions', '9 are reached and 7 are not'])
    def test_the_report_quotes_it(self, phrase):
        assert phrase in _report()


class TestBeforeAndAfter:

    def test_65_distributions_and_4963_mb_before(self):
        text = _read('05_before.out.txt')
        assert _section(text, '== freeze', '== heavy') == ['65']
        assert len(_content_lines(os.path.join(DATA, '05_before_freeze.txt'))) == 65
        assert re.search(r'^== size\n4\.9G\t.*\n4963\t', text, re.M)

    def test_24_after_and_the_branch_pins_are_exactly_them(self):
        reduced = _content_lines(os.path.join(DATA, 'review', 'constraints-reduced.txt'))
        assert len(reduced) == 24
        assert _content_lines(os.path.join(REPO_ROOT, 'constraints.txt')) == reduced
        ci = _content_lines(os.path.join(DATA, 'review', 'constraints-ci.txt'))
        assert len(ci) == 15
        assert _content_lines(os.path.join(REPO_ROOT, 'constraints-ci.txt')) == ci

    def test_291_mb_with_pip_26_and_294_mb_with_the_boxs_pip_22(self):
        after = _read('03_after.out.txt')
        assert re.search(r'^== stage A size\n291M\t.*\n291\t', after, re.M)
        assert re.search(r'^== size\n294\t', _read('review', 'r4_pip22.out.txt'), re.M)
        assert re.search(r'^== size\n294\t', _read('rerun_box_pip22.out.txt'), re.M)

    def test_the_ci_like_rerun(self):
        text = _read('rerun_ci_like.out.txt')
        assert re.search(r'^== size before torch\n440\t', text, re.M) and '440 MB' in _report()
        assert re.search(r'^== size with torch\n1257\t', text, re.M) and '1257 MB' in _report()
        assert re.search(r'^== step 5: import torch\n2\.13\.0\+cpu\n', text, re.M)
        assert 'freeze before torch: 44 lines' in text and '44 distributions' in _report()
        assert text.count('grep exit: 1 (1 = none)') == 2, 'a torch-family download or GPU-stack distribution'

    def test_the_24_15_26_split(self):
        text = _read('review', 'r4_pip22.out.txt')
        assert 'box (kept in constraints.txt): 24' in text
        assert 'ci  (moved to constraints-ci.txt): 15' in text
        assert 'gone (deleted): 26' in text
        assert _read('rerun_r0_derive.out.txt').count('identical') == 2

    @pytest.mark.parametrize('phrase', ['| 65 | 4,963 MB |', '| 291 MB |', '| 294 MB |', '**24 stay in `constraints.txt`**',
                                        '**15 move to the new `constraints-ci.txt`**', '**26 are deleted**'])
    def test_the_report_quotes_them(self, phrase):
        assert phrase in _report()


class TestTheSuiteFigures:

    @pytest.mark.parametrize('name, summary, total', [
        ('03_after_stageB_pytest.txt', '7033 passed, 20 skipped', '98.86%'),
        ('03_after_stageC_pytest.txt', '7086 passed, 19 skipped', '99.30%'),
        ('rerun_ci_like_notorch_pytest.txt', '7065 passed, 20 skipped', '98.86%'),
        ('rerun_ci_like_torch_pytest.txt', '7118 passed, 19 skipped', '99.30%'),
    ])
    def test_the_planning_runs(self, name, summary, total):
        text = _read(name)
        assert summary in text and 'Total coverage: %s' % total in text
        assert summary in _report() and total in _report()


class TestNoLocalPathIsCommitted:
    """The raw outputs had local path prefixes normalised before commit (the report says how)."""

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
