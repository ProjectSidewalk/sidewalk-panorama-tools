"""Tests for log_analyzer/analyze.py.

The analyzer is the only consumer of log.csv, so these tests pin the two things that couple it to
DownloadRunner: the 18-column positional layout, and blank fields meaning "this phase never finished".
"""

import argparse
import ast
import importlib.util
import os
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# analyze.py lives in a directory, not a package, and shares no code with the runners - load it by path. Unlike
# DownloadRunner it defines only functions and constants at module level, so importing it is side-effect-free.
_spec = importlib.util.spec_from_file_location(
    'log_analyzer_analyze', os.path.join(REPO_ROOT, 'log_analyzer', 'analyze.py'))
analyze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyze)


def runner_constant(name):
    """Read a module-level constant out of DownloadRunner.py without importing it.

    DownloadRunner's whole flow runs at import time, so it can't be imported just to read a number.
    """
    with open(os.path.join(REPO_ROOT, 'DownloadRunner.py')) as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found in DownloadRunner.py')


def make_row(start_time, **overrides):
    """One log.csv row: a timestamp plus 17 counts, defaulting to a quiet, healthy run."""
    values = dict.fromkeys(analyze.LOG_COLUMNS[1:], 0)
    values.update(overrides)
    return ','.join([str(start_time)] + [str(values[c]) for c in analyze.LOG_COLUMNS[1:]])


def write_log(path, rows, header=True):
    lines = ([','.join(analyze.LOG_COLUMNS)] if header else []) + rows
    path.write_text('\n'.join(lines) + '\n')
    return path


def days_ago(n):
    """A timestamp exactly n days before the clock analyze_city measures against, so
    `(now - ts).days == n` at every time of day.

    It must be that clock: analyze_city computes staleness from `datetime.now(timezone.utc)`, and
    this used to anchor to *local* midday instead. On a UTC runner that made the elapsed time
    n-1 days plus a fraction for the whole morning, and `.days` floors -- so every CI run before
    12:00 UTC saw one day fewer than the test asked for, while every afternoon run passed.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=n)


def test_days_ago_agrees_with_the_clock_the_analyzer_measures_against():
    """Pins the helper's contract in the same order production uses it: the row is written first,
    the analyzer reads its clock second. Anchoring days_ago to any fixed hour instead reintroduces
    the morning-only failure, because `.days` floors."""
    for n in (0, 1, 3, 10, 400):
        ts = days_ago(n)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert (now - ts).days == n, f'days_ago({n}) did not read back as {n} days old'


def recent_rows(count=7, **overrides):
    """`count` daily rows ending today, so staleness never fires in tests aimed at other checks."""
    return [make_row(days_ago(count - 1 - i), **overrides) for i in range(count)]


def crashed_row(n_days_ago=0):
    """A run that died before any phase finished: a real timestamp, then 17 blanks (#49)."""
    return str(days_ago(n_days_ago)) + ',' * 17


def test_columns_match_the_runners_field_count():
    # If a column is ever added to log.csv, both halves have to move together.
    assert len(analyze.LOG_COLUMNS) == runner_constant('LOG_CSV_FIELD_COUNT')


def test_reads_log_without_a_header(tmp_path):
    # DownloadRunner never writes a header; production files get one by hand at city setup. A city whose
    # header was forgotten must still parse, and must not lose its first run.
    log = write_log(tmp_path / 'log.csv', recent_rows(3, image_success=5), header=False)

    df = analyze.read_log(log)

    assert list(df.columns) == analyze.LOG_COLUMNS
    assert len(df) == 3
    assert df['image_success'].tolist() == [5, 5, 5]


def test_reads_log_with_a_header(tmp_path):
    log = write_log(tmp_path / 'log.csv', recent_rows(3, image_success=5), header=True)

    df = analyze.read_log(log)

    assert list(df.columns) == analyze.LOG_COLUMNS
    assert len(df) == 3


def test_leading_blank_line_is_ignored(tmp_path):
    # write_log_csv_row prefixes every row with "\n", so a freshly created log.csv starts with a blank line.
    log = tmp_path / 'log.csv'
    log.write_text('\n' + '\n'.join(recent_rows(2)))

    assert len(analyze.read_log(log)) == 2


def test_blank_fields_stay_missing_rather_than_zero(tmp_path):
    # A run that died before the image phase leaves those columns blank (#49). Reading them as 0 would make a
    # crashed run look like a legitimately quiet one.
    log = write_log(tmp_path / 'log.csv', recent_rows(3, image_success=5) + [crashed_row(0)])

    df = analyze.read_log(log)

    assert df['image_success'].isna().iloc[-1]
    assert df['image_success'].iloc[0] == 5


def test_stats_line_survives_a_crashed_latest_run(tmp_path):
    # The newest run ending early is exactly when the report matters most, so the summary must not raise on
    # the resulting NaN - it falls back to the last run that did record a count.
    log = write_log(tmp_path / 'log.csv',
                    [make_row(days_ago(n), image_total=1234, image_fail=7) for n in (3, 2, 1)] + [crashed_row(0)])

    line = analyze.city_stats(analyze.read_log(log))

    assert '1,234 total' in line
    assert '7 permanent failures' in line


def test_stats_line_marks_counts_that_were_never_recorded(tmp_path):
    log = write_log(tmp_path / 'log.csv', [crashed_row(0)])

    assert '? total' in analyze.city_stats(analyze.read_log(log))


def test_stale_log_is_critical(tmp_path):
    log = write_log(tmp_path / 'log.csv', [make_row(days_ago(10))])

    issues = analyze.analyze_city('somewhere', log, stale_days=3)

    assert [i['level'] for i in issues] == ['CRITICAL']
    assert '10 days old' in issues[0]['msg']


def test_fresh_quiet_city_is_clean(tmp_path):
    log = write_log(tmp_path / 'log.csv', recent_rows(7, image_skip=100, image_total=100))

    assert analyze.analyze_city('somewhere', log, stale_days=3) == []


def test_missing_log_is_critical(tmp_path):
    issues = analyze.analyze_city('somewhere', tmp_path / 'nope.csv', stale_days=3)

    assert [i['level'] for i in issues] == ['CRITICAL']


def test_repeated_crashes_are_flagged(tmp_path):
    # Without this check a city that dies every night stays silent: NaN compares false in every other check,
    # and start_time keeps advancing so the log never goes stale.
    log = write_log(tmp_path / 'log.csv',
                    [make_row(days_ago(n)) for n in (6, 5, 4)] + [crashed_row(n) for n in (3, 2, 1, 0)])

    issues = analyze.analyze_city('somewhere', log, stale_days=3)

    assert any('ended early' in i['msg'] for i in issues if i['level'] == 'WARNING')


def test_growing_image_failures_are_flagged(tmp_path):
    rows = [make_row(days_ago(6 - i), image_fail=i * 50) for i in range(7)]
    log = write_log(tmp_path / 'log.csv', rows)

    issues = analyze.analyze_city('somewhere', log, stale_days=3)

    assert any('failures growing fast' in i['msg'] for i in issues)


def test_resolve_sftp_requires_host_and_base(monkeypatch):
    monkeypatch.delenv('PS_SFTP_HOST', raising=False)
    monkeypatch.delenv('PS_SFTP_BASE', raising=False)
    args = argparse.Namespace(host=None, base=None, user=None, port=None, key=None)

    with pytest.raises(SystemExit) as excinfo:
        analyze.resolve_sftp(args)

    assert 'PS_SFTP_HOST' in str(excinfo.value)


def test_resolve_sftp_prefers_flags_over_environment(monkeypatch):
    monkeypatch.setenv('PS_SFTP_HOST', 'from-env')
    monkeypatch.setenv('PS_SFTP_BASE', '/from/env')
    args = argparse.Namespace(host='from-flag', base=None, user=None, port=None, key=None)

    settings = analyze.resolve_sftp(args)

    assert settings['host'] == 'from-flag'
    assert settings['base'] == '/from/env'
    assert settings['user'] is None  # optional: the ssh config may supply it


def test_download_log_omits_optional_ssh_arguments(monkeypatch, tmp_path):
    # With only host+base set, sftp must be invoked without -P/-i so ~/.ssh/config stays in charge.
    captured = {}

    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['input'] = kwargs['input']
        return type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()

    monkeypatch.setattr(analyze.subprocess, 'run', fake_run)
    sftp = {'host': 'ps-panos', 'base': '/panos', 'user': None, 'port': None, 'key': None}

    assert analyze.download_log('seattle-wa', tmp_path / 'out.csv', sftp)
    assert '-P' not in captured['cmd'] and '-i' not in captured['cmd']
    assert captured['cmd'][-1] == 'ps-panos'
    assert captured['input'].startswith('get /panos/seattle-wa/log.csv ')
