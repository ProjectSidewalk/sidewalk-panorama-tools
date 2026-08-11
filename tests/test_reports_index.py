"""The reports/ index table is the only map of the investigation series, and it is maintained by hand.

Three ways it rots, all of which have happened or nearly happened while the series grew to eleven
reports in five days:

1. **A row lands out of date order.** The 2026-08-11 off-axis row was appended between two 2026-08-10
   rows, so the next report appended to the bottom would have continued after a stale date and the
   ordering would have degraded from there. A reader scanning chronologically skips whatever sits
   after a later date.
2. **A row points at a file that isn't there** — a rename, or a row written before the report.
3. **A report exists with no row**, which is the same as not existing for anyone who did not write it.

None of these fail anything today, so they are checked here. This file deliberately parses the table
rather than trusting it: the parse is strict, and `test_the_index_parses_at_all` guards against a
formatting change silently making every assertion below vacuous.
"""

import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(REPO_ROOT, 'reports')
INDEX = os.path.join(REPORTS_DIR, 'README.md')

# | YYYY-MM-DD | [Title](file.md) | Outcome |
ROW = re.compile(r'^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*\[(.+?)\]\((.+?)\)\s*\|\s*(.+?)\s*\|$')


def _rows():
    with open(INDEX, encoding='utf-8') as f:
        lines = f.read().splitlines()
    out = []
    for line in lines:
        m = ROW.match(line.strip())
        if m:
            out.append({'date': m.group(1), 'title': m.group(2),
                        'path': m.group(3), 'outcome': m.group(4)})
    return out


def _report_files():
    return sorted(f for f in os.listdir(REPORTS_DIR)
                  if f.endswith('.md') and f != 'README.md')


def test_the_index_parses_at_all():
    """Guards the guard: a table reformat that stopped matching ROW would make every test below pass
    over an empty list. Pinned against the report files on disk, not against a magic number."""
    rows = _rows()
    assert rows, f'no index rows matched in {INDEX} — the ROW pattern is stale'
    assert len(rows) == len(_report_files()), \
        f'{len(rows)} index rows vs {len(_report_files())} report files'


def test_the_index_is_in_date_order():
    dates = [r['date'] for r in _rows()]
    assert dates == sorted(dates), \
        'index rows must ascend by date; out of order at ' + str(
            [(a, b) for a, b in zip(dates, dates[1:]) if a > b])


@pytest.mark.parametrize('row', _rows(), ids=[r['path'] for r in _rows()])
def test_every_indexed_report_exists(row):
    assert os.path.exists(os.path.join(REPORTS_DIR, row['path'])), row['path']


@pytest.mark.parametrize('row', _rows(), ids=[r['path'] for r in _rows()])
def test_every_rows_date_matches_its_filename(row):
    """The date column is what the ordering test sorts on, so it has to be the report's own date and
    not a hand-typed one that drifted from the filename."""
    assert row['path'].startswith(row['date']), (row['date'], row['path'])


def test_every_report_file_is_indexed():
    indexed = {r['path'] for r in _rows()}
    missing = [f for f in _report_files() if f not in indexed]
    assert not missing, f'report files with no index row: {missing}'
