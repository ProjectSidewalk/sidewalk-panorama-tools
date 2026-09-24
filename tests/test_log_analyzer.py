"""Tests for log_analyzer/analyze.py.

The analyzer is the only consumer of log.csv, so these tests pin the two things that couple it to
DownloadRunner: the 19-column positional layout, and blank fields meaning "this phase never finished".

They also pin the six alert rules themselves. Three of them - extended zero progress, abnormally long
runtime, duplicate runs on one day - had their entire firing branch uncovered until #57, which for an ops
monitor is the worst kind of gap: the failure mode of an alert rule that stops working is silence, and this
one watches a nightly scrape across ~49 production cities with nothing else looking.
"""

import argparse
import ast
import csv
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# analyze.py lives in a directory, not a package, and shares no code with the runners - load it by path. Unlike
# DownloadRunner it defines only functions and constants at module level, so importing it is side-effect-free.
_spec = importlib.util.spec_from_file_location(
    'log_analyzer_analyze', os.path.join(REPO_ROOT, 'log_analyzer', 'analyze.py'))
analyze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyze)

REAL_OPEN_URL = analyze.roster._open_url


@pytest.fixture(autouse=True)
def _no_roster_network(monkeypatch):
    """The suite is network-free, and since the roster host gained a default (#151) a download-mode main()
    that forgets to stub the fetch would reach sidewalk-sea for real - and still pass, whenever something
    else already made the run CRITICAL. So the one socket-touching seam raises something fetch_roster does
    NOT catch: a forgotten stub is a test error, not a quiet CRITICAL line. Tests of the fetch itself
    install their own _open_url over this."""
    def refuse(url, timeout):
        raise AssertionError('a test reached the network for the roster: %s' % url)

    monkeypatch.setattr(analyze.roster, '_open_url', refuse)


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
    """One log.csv row: a timestamp plus the counts, defaulting to a quiet, healthy run with no GSV corpus."""
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
    """A run that died before any phase finished: a real timestamp, then blanks (#49)."""
    return str(days_ago(n_days_ago)) + ',' * (len(analyze.LOG_COLUMNS) - 1)


def old_row(start_time, **overrides):
    """A row as every run wrote it before field 19 existed (#43): 18 fields, no depth corpus size."""
    return make_row(start_time, **overrides).rsplit(',', 1)[0]


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


# ---------------------------------------------------------------------------
# The script as cron runs it
# ---------------------------------------------------------------------------

def run_analyzer(tmp_path, *args, cities=(('seattle-wa', 'Seattle'),), logs=()):
    """Run analyze.py as its own process, out of a copy in tmp_path.

    The copy is the whole trick: SCRIPT_DIR is Path(__file__).parent, so LOGS_DIR and CITIES_FILE follow the
    copy into tmp_path. That makes the run hermetic - no writes into the gitignored log_analyzer/logs/, and
    no chance of reading the real deployed cities.csv - without the script needing to grow flags for it.

    @param logs [(city_id, [row, ...]), ...] written to LOGS_DIR/log-<city_id>.csv the way the download step
                would have left them.
    """
    script = tmp_path / 'analyze.py'
    shutil.copy(os.path.join(REPO_ROOT, 'log_analyzer', 'analyze.py'), script)
    # roster.py travels with it (#133): analyze.py loads its sibling by file location, relative to its own
    # __file__, so a copy without it is not a runnable script. Copying it keeps the hermetic-copy trick
    # honest rather than making the import search back into the repo.
    shutil.copy(os.path.join(REPO_ROOT, 'log_analyzer', 'roster.py'), tmp_path / 'roster.py')

    with open(tmp_path / 'cities.csv', 'w', newline='') as f:
        f.write('city_id,display_name\n')
        for city_id, display_name in cities:
            f.write(f'{city_id},{display_name}\n')

    logs_dir = tmp_path / 'logs'
    logs_dir.mkdir(exist_ok=True)
    for city_id, rows in logs:
        write_log(logs_dir / f'log-{city_id}.csv', list(rows))

    # UTF-8 on both sides of the pipe: the report prints box-drawing characters and status emoji, and a
    # Windows console's default cp1252 can encode none of them. Setting only the child's encoding moves the
    # failure from its stdout to our decode of it.
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, encoding='utf-8', timeout=120,
                          env=dict(os.environ, PYTHONIOENCODING='utf-8'))


class TestTheScriptsExitStatus:
    """The report's exit code, which is the whole of its interface to cron.

    analyze.py runs from a crontab line whose mail-on-failure is the only thing that ever tells anyone a city
    has gone dark, so the mapping from findings to status is load-bearing in a way no amount of correct
    output makes up for. These drive the real script in a real process - the same shape as the runners' own
    __main__ tests - so they hold across any change to how main() returns.
    """

    def test_a_healthy_corpus_exits_zero(self, tmp_path):
        result = run_analyzer(tmp_path, '--no-download',
                              logs=[('seattle-wa', recent_rows(7, image_skip=100, image_total=100))])

        assert result.returncode == 0, result.stdout + result.stderr
        assert 'Seattle' in result.stdout

    def test_a_critical_finding_exits_one(self, tmp_path):
        # Stale by 10 days against the default 3-day threshold. This is the status cron mails on.
        result = run_analyzer(tmp_path, '--no-download',
                              logs=[('seattle-wa', [make_row(days_ago(10))])])

        assert result.returncode == 1, result.stdout + result.stderr
        assert 'Critical : ' in result.stdout

    def test_a_warning_alone_does_not_fail_the_run(self, tmp_path):
        """Only CRITICAL is worth waking someone. A WARNING that exited non-zero would train the fleet's
        operators to ignore the mail, which costs more than the warning is worth."""
        rows = [make_row(days_ago(6 - i), image_fail=i * 50) for i in range(7)]
        result = run_analyzer(tmp_path, '--no-download', logs=[('seattle-wa', rows)])

        assert 'failures growing fast' in result.stdout
        assert result.returncode == 0, result.stdout + result.stderr

    def test_an_unknown_flag_exits_two(self, tmp_path):
        result = run_analyzer(tmp_path, '--no-such-flag')

        assert result.returncode == 2
        assert '--no-such-flag' in result.stderr

    def test_an_unknown_city_is_named_on_stderr(self, tmp_path):
        result = run_analyzer(tmp_path, '--no-download', '--city', 'atlantis')

        assert result.returncode != 0
        assert 'atlantis' in result.stderr

    def test_download_is_on_by_default_and_needs_connection_settings(self, tmp_path, monkeypatch):
        """The flag defaults to --download, so a bare invocation with no PS_SFTP_* set must fail loudly
        rather than silently analyzing whatever stale copies happen to be cached on disk."""
        monkeypatch.delenv('PS_SFTP_HOST', raising=False)
        monkeypatch.delenv('PS_SFTP_BASE', raising=False)

        result = run_analyzer(tmp_path, logs=[('seattle-wa', recent_rows(3))])

        assert result.returncode != 0
        assert 'PS_SFTP_HOST' in result.stderr


# ---------------------------------------------------------------------------
# The alert rules
# ---------------------------------------------------------------------------

def daily_rows(count, offset=0, **overrides):
    """`count` rows, one per day, the newest `offset` days ago.

    offset leaves room to append a differently-shaped row for today without landing two runs on one calendar
    date, which would trip check 6 and put an unrelated INFO in the result.
    """
    return [make_row(days_ago(count + offset - 1 - i), **overrides) for i in range(count)]


def zero_progress_rows(total=130, quiet_tail=30, success=5):
    """A history that downloaded `success` images a day and then stopped `quiet_tail` days ago.

    `total` must exceed ZERO_PROGRESS_DAYS + ZERO_PROGRESS_LOOKBACK for check 3 to look at all - the rule
    deliberately says nothing about a city whose history is too short to know what normal was.
    """
    return [make_row(days_ago(total - 1 - i),
                     image_success=(0 if i >= total - quiet_tail else success))
            for i in range(total)]


class TestExtendedZeroProgressIsFlaggedAsARegression:
    """Check 3: a city that used to download images and has stopped.

    The subtlety worth a test is the word *regression*. A city that has never downloaded anything - one
    brought online but not yet scraping, or one whose whole corpus is already on disk - is not broken, and
    firing on it would put a permanent warning next to a healthy deployment. That distinction lives entirely
    in the `prior_had_some` half of one condition, which nothing exercised before #57.
    """

    def test_a_city_that_stopped_downloading_is_flagged(self, tmp_path):
        log = write_log(tmp_path / 'log.csv', zero_progress_rows())

        issues = analyze.analyze_city('somewhere', log, stale_days=3)

        warnings = [i for i in issues if i['level'] == 'WARNING']
        assert len(warnings) == 1, issues
        assert f'No new images downloaded in {analyze.ZERO_PROGRESS_DAYS} days' in warnings[0]['msg']
        # Naming the last good day is the point of the alert: it tells the operator where to look in
        # scrape.log without having to open the log at all.
        assert days_ago(analyze.ZERO_PROGRESS_DAYS).strftime('%Y-%m-%d') in warnings[0]['msg']

    def test_a_city_that_never_downloaded_anything_is_not_flagged(self, tmp_path):
        # Same shape, same length, no prior successes anywhere - so there is no regression to report.
        log = write_log(tmp_path / 'log.csv', zero_progress_rows(success=0))

        assert analyze.analyze_city('somewhere', log, stale_days=3) == []

    def test_a_history_too_short_to_know_what_normal_was_is_not_flagged(self, tmp_path):
        # One row under the entry gate. The city looks identical to the flagged case except that its record
        # of "used to work" is shorter than the lookback the rule reasons over.
        short = analyze.ZERO_PROGRESS_DAYS + analyze.ZERO_PROGRESS_LOOKBACK
        log = write_log(tmp_path / 'log.csv', zero_progress_rows(total=short))

        assert analyze.analyze_city('somewhere', log, stale_days=3) == []


class TestAnAbnormallyLongRunIsFlagged:
    """Check 4: a recent run far over the city's own historical median.

    The threshold is relative, not absolute, because run length varies by two orders of magnitude across the
    fleet. Both halves of that - *recent*, and *over* rather than *at* - are one comparison each.
    """

    def test_a_recent_run_far_over_the_median_is_flagged(self, tmp_path):
        rows = (daily_rows(10, offset=1, image_minutes=10, total_minutes=10)
                + [make_row(days_ago(0), image_minutes=100, total_minutes=100)])
        log = write_log(tmp_path / 'log.csv', rows)

        issues = analyze.analyze_city('somewhere', log, stale_days=3)

        warnings = [i for i in issues if i['level'] == 'WARNING']
        assert len(warnings) == 1, issues
        assert 'Recent unusually long image phase: 100 min' in warnings[0]['msg']
        assert 'median: 10 min' in warnings[0]['msg']
        assert 'threshold: 30 min' in warnings[0]['msg']

    def test_an_old_long_run_is_not_flagged(self, tmp_path):
        """Only the last 7 runs count. A slow night three weeks ago is history, not an alert - and it still
        drags the median, so a rule that looked at the whole frame would flag it forever."""
        rows = [make_row(days_ago(14), total_minutes=100)] + daily_rows(14, total_minutes=10)
        log = write_log(tmp_path / 'log.csv', rows)

        assert analyze.analyze_city('somewhere', log, stale_days=3) == []

    def test_a_run_at_exactly_the_threshold_is_not_flagged(self, tmp_path):
        multiple = analyze.LONG_RUN_MULTIPLIER
        rows = daily_rows(10, offset=1, total_minutes=10) + [make_row(days_ago(0),
                                                                      total_minutes=10 * multiple)]
        log = write_log(tmp_path / 'log.csv', rows)

        assert analyze.analyze_city('somewhere', log, stale_days=3) == []

    def test_a_long_depth_phase_is_not_a_long_run(self, tmp_path):
        """The depth phase runs to whatever budget it is handed, and under the queue's extra passes (#43)
        that varies by design from one 12-minute slot to ten. A big city given 120 minutes after ten
        12-minute nights is the backfill working, not a hung run - so depth minutes are outside this rule."""
        rows = (daily_rows(10, offset=1, total_minutes=12, depth_minutes=12)
                + [make_row(days_ago(0), total_minutes=120, depth_minutes=120)])
        log = write_log(tmp_path / 'log.csv', rows)

        assert analyze.analyze_city('somewhere', log, stale_days=3) == []

    def test_a_long_image_phase_beside_a_depth_phase_is_still_flagged(self, tmp_path):
        """The discrimination for the test above: taking depth out must not take the rule out with it."""
        rows = (daily_rows(10, offset=1, image_minutes=10, total_minutes=22, depth_minutes=12)
                + [make_row(days_ago(0), image_minutes=120, total_minutes=132, depth_minutes=12)])
        log = write_log(tmp_path / 'log.csv', rows)

        warnings = [i for i in analyze.analyze_city('somewhere', log, stale_days=3) if i['level'] == 'WARNING']
        assert len(warnings) == 1, warnings
        assert 'Recent unusually long image phase: 120 min' in warnings[0]['msg']
        assert 'median: 10 min' in warnings[0]['msg']


class TestOverlappingRunsAreFlagged:
    """Check 6: a run that starts before the previous one's recorded end - two processes on one store.

    This was "more than one run on a calendar day", at INFO. The queue's extra passes (#43) run a city more
    than once a night by design, so that rule would have fired for most of the fleet every night and meant
    nothing. Overlap is what the same-day rule was standing in for - two runs racing on one city's ledgers -
    and it can be read directly from start_time + total_minutes.
    """

    @staticmethod
    def runs_on(day_offset, first_minutes, gap_minutes):
        # Built by replacing the hour rather than by arithmetic on days_ago, so both rows land on one
        # calendar date at every time of day (the flooring trap days_ago's own docstring documents).
        base = days_ago(day_offset).replace(hour=1, minute=0, second=0, microsecond=0)
        return [make_row(base, total_minutes=first_minutes),
                make_row(base + timedelta(minutes=gap_minutes))], base

    def test_a_run_starting_inside_the_previous_one_is_a_warning(self, tmp_path):
        rows, base = self.runs_on(1, first_minutes=120, gap_minutes=60)
        log = write_log(tmp_path / 'log.csv', rows)

        issues = analyze.analyze_city('somewhere', log, stale_days=3)

        assert [i['level'] for i in issues] == ['WARNING'], issues
        assert 'Overlapping runs' in issues[0]['msg']
        assert base.strftime('%Y-%m-%d') in issues[0]['msg']

    def test_two_runs_on_one_day_that_do_not_overlap_are_not_reported(self, tmp_path):
        """The queue's second pass: a re-run hours after the first ended is the design working, not an alert.
        This is the row pair the old same-day rule fired on."""
        rows, _ = self.runs_on(1, first_minutes=12, gap_minutes=300)
        log = write_log(tmp_path / 'log.csv', rows)

        assert analyze.analyze_city('somewhere', log, stale_days=3) == []

    def test_back_to_back_passes_are_not_an_overlap(self, tmp_path):
        """Durations are whole minutes, rounded: an 11.6-minute pass logged as 12 and followed at once by its
        second pass would read as a 24-second collision. The rounding granularity is tolerated."""
        rows, _ = self.runs_on(1, first_minutes=12, gap_minutes=11.7)
        log = write_log(tmp_path / 'log.csv', rows)

        assert analyze.analyze_city('somewhere', log, stale_days=3) == []

    def test_a_start_well_inside_the_tolerance_is_still_an_overlap(self, tmp_path):
        """The discrimination for the tolerance: it forgives rounding, not a real collision."""
        rows, _ = self.runs_on(1, first_minutes=12, gap_minutes=5)
        log = write_log(tmp_path / 'log.csv', rows)

        assert [i['level'] for i in analyze.analyze_city('somewhere', log, stale_days=3)] == ['WARNING']

    def test_a_crashed_run_has_no_end_to_overlap(self, tmp_path):
        """A crashed row's duration is blank (#49), so nothing can be said about what it overlapped."""
        base = days_ago(1).replace(hour=1, minute=0, second=0, microsecond=0)
        rows = [str(base) + ',' * (len(analyze.LOG_COLUMNS) - 1), make_row(base + timedelta(minutes=5))]
        log = write_log(tmp_path / 'log.csv', rows)

        assert not [i for i in analyze.analyze_city('somewhere', log, stale_days=3)
                    if 'Overlapping' in i['msg']]

    def test_an_overlap_older_than_the_window_is_not_reported(self, tmp_path):
        # Only the last 30 rows are considered, so an old overlap does not stay on the report forever.
        rows, _ = self.runs_on(40, first_minutes=120, gap_minutes=60)
        log = write_log(tmp_path / 'log.csv', rows + daily_rows(30))

        assert not [i for i in analyze.analyze_city('somewhere', log, stale_days=3)
                    if 'Overlapping' in i['msg']]


class TestAnUnreadableLogDoesNotAbortTheWholeSweep:
    """A bad file for one city must become that city's CRITICAL, not an exception.

    main() loops over ~49 cities with no per-city try/except around analyze_city, so anything that escapes
    here takes down the report for every city after it - and the ones that come alphabetically later would
    silently stop being monitored at all.
    """

    def test_a_log_with_no_parseable_timestamps_is_critical(self, tmp_path):
        log = write_log(tmp_path / 'log.csv', ['not-a-date' + ',0' * 17])

        issues = analyze.analyze_city('somewhere', log, stale_days=3)

        assert [i['level'] for i in issues] == ['CRITICAL']
        assert 'empty' in issues[0]['msg']

    def test_a_log_that_cannot_be_decoded_is_critical_not_an_exception(self, tmp_path):
        # A truncated or half-binary transfer. 0x81 decodes under neither UTF-8 nor cp1252, so this raises
        # on the CI runner and on a Windows dev box alike.
        log = tmp_path / 'log.csv'
        log.write_bytes(b'start_time,\x81\x81\x81\n')

        issues = analyze.analyze_city('somewhere', log, stale_days=3)

        assert [i['level'] for i in issues] == ['CRITICAL']
        # The exception text is carried through: "could not parse" alone would send the operator to read a
        # file that turns out not to be text.
        assert 'Could not parse log' in issues[0]['msg']
        assert 'codec' in issues[0]['msg']


class TestTimestampWidthsThatDifferBetweenRows:
    """A log.csv whose rows differ in timestamp width, which happens on its own eventually.

    DownloadRunner writes str(datetime.now()), and str() omits the ".ffffff" when the microsecond lands on
    exactly 0. Both forms are valid and nothing rejects either, so any long-lived log will hold a mix.

    read_csv(parse_dates=[...]) cannot read such a column *uniformly*, and its response is to hand back the
    raw strings - no exception, no NaT - which took out `.dt` in analyze_city and, since main() guards no
    city individually, the report for every city after it. The obvious repair, plain errors="coerce", trades
    that crash for something quieter: it locks onto the first width it sees and turns every row of the other
    width into NaT, discarding real runs. Hence format="ISO8601", and hence both tests here.
    """

    def test_neither_width_is_discarded(self, tmp_path):
        rows = [make_row(days_ago(2).replace(microsecond=0)),
                make_row(days_ago(1).replace(microsecond=123456)),
                make_row(days_ago(0).replace(microsecond=0))]
        log = write_log(tmp_path / 'log.csv', rows)

        df = analyze.read_log(log)

        assert len(df) == 3, 'a run was dropped for the width of its timestamp'
        assert df['start_time'].notna().all()

    def test_a_mixed_width_log_still_reaches_the_alert_rules(self, tmp_path):
        # The consequence, end to end: this raised AttributeError("Can only use .dt accessor with
        # datetimelike values") before the fix, out of analyze_city rather than as a finding.
        rows = [make_row(days_ago(9 - i).replace(microsecond=0 if i % 2 else 500000), image_fail=i * 50)
                for i in range(10)]
        log = write_log(tmp_path / 'log.csv', rows)

        issues = analyze.analyze_city('somewhere', log, stale_days=3)

        assert any('failures growing fast' in i['msg'] for i in issues), issues

    def test_a_genuinely_unparseable_timestamp_is_still_dropped(self, tmp_path):
        """Guard the guard: ISO8601 must not have made the parser so permissive that junk gets through as a
        date. The row is dropped, and its absence is what analyze_city reports as an empty log."""
        log = write_log(tmp_path / 'log.csv',
                        [make_row(days_ago(0)), 'not-a-date' + ',0' * 17])

        df = analyze.read_log(log)

        assert len(df) == 1


class TestTheSftpInvocation:
    """The argv handed to sftp. Every one of these mistakes fails identically at 3am: no logs, no report.

    test_download_log_omits_optional_ssh_arguments above pins the *absence* of -P/-i when nothing is
    configured, which leaves the present case - the one every deployment with a non-default port actually
    uses - unpinned.
    """

    @staticmethod
    def capture_sftp(monkeypatch, returncode=0, stderr=''):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            captured['input'] = kwargs['input']
            return type('R', (), {'returncode': returncode, 'stdout': '', 'stderr': stderr})()

        monkeypatch.setattr(analyze.subprocess, 'run', fake_run)
        return captured

    def test_the_port_is_passed_as_a_port_and_not_as_preserve_times(self, monkeypatch, tmp_path):
        """sftp spells the port -P; -p means "preserve modification times". A one-character slip connects to
        22 on every host in the fleet, and there is nothing in the output to say why it failed."""
        captured = self.capture_sftp(monkeypatch)
        sftp = {'host': 'ps-panos', 'base': '/panos', 'user': 'scraper', 'port': '2222', 'key': None}

        assert analyze.download_log('seattle-wa', tmp_path / 'out.csv', sftp)
        assert captured['cmd'][captured['cmd'].index('-P') + 1] == '2222'
        assert '-p' not in captured['cmd']
        assert captured['cmd'][-1] == 'scraper@ps-panos'

    def test_the_identity_file_is_passed(self, monkeypatch, tmp_path):
        captured = self.capture_sftp(monkeypatch)
        sftp = {'host': 'ps-panos', 'base': '/panos', 'user': None, 'port': None, 'key': '/keys/id_ed25519'}

        assert analyze.download_log('seattle-wa', tmp_path / 'out.csv', sftp)
        assert captured['cmd'][captured['cmd'].index('-i') + 1] == '/keys/id_ed25519'

    def test_a_failed_transfer_is_reported_not_swallowed(self, monkeypatch, tmp_path, capsys):
        self.capture_sftp(monkeypatch, returncode=1, stderr='Permission denied (publickey).')
        sftp = {'host': 'ps-panos', 'base': '/panos', 'user': None, 'port': None, 'key': None}

        assert analyze.download_log('seattle-wa', tmp_path / 'out.csv', sftp) is False
        # The server's own words, not ours. "Download failed" alone cannot distinguish a wrong key from a
        # wrong path from a host that is simply down.
        assert 'Permission denied (publickey).' in capsys.readouterr().err


class TestAnIdentityFileIsExpandedBeforeSshSeesIt:

    def test_a_tilde_in_the_key_path_is_expanded(self, monkeypatch):
        """ssh does not expand ~ in -i itself, and PS_SFTP_KEY is the kind of setting people write with one.
        Left unexpanded it looks for a literal directory named '~' and reports only "no such identity"."""
        monkeypatch.setenv('PS_SFTP_HOST', 'ps-panos')
        monkeypatch.setenv('PS_SFTP_BASE', '/panos')
        args = argparse.Namespace(host=None, base=None, user=None, port=None, key='~/.ssh/id_ed25519')

        key = analyze.resolve_sftp(args)['key']

        assert '~' not in key
        assert os.path.isabs(key)
        # expanduser only replaces the leading ~, so the rest keeps whatever separators it was written with.
        assert key.replace('\\', '/').endswith('/.ssh/id_ed25519')


class TestTheDeployedCityListParses:
    """cities.csv is the report's whole notion of what exists. A city missing from it is not monitored, and
    nothing anywhere says so."""

    def test_a_city_list_round_trips(self, tmp_path):
        path = tmp_path / 'cities.csv'
        path.write_text('city_id,display_name\nseattle-wa,"Seattle, WA"\nnewberg-or,Newberg\n')

        cities = analyze.load_cities(path)

        assert [c['city_id'] for c in cities] == ['seattle-wa', 'newberg-or']
        assert cities[0]['display_name'] == 'Seattle, WA'  # the quoted comma is one field, not two

    def test_the_committed_city_list_is_usable(self):
        cities = analyze.load_cities(os.path.join(REPO_ROOT, 'log_analyzer', 'cities.csv'))

        assert cities, 'cities.csv is empty; the analyzer would report on nothing'
        assert all(c['city_id'] for c in cities)
        assert all(c.get('display_name') for c in cities)

    def test_the_committed_city_ids_are_unique(self):
        """main() keys its results dict on city_id, so a duplicated row silently drops one city from the
        report - it is checked, then overwritten by its twin, and the count still looks right."""
        ids = [c['city_id'] for c in analyze.load_cities(
            os.path.join(REPO_ROOT, 'log_analyzer', 'cities.csv'))]

        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        assert not duplicates, f'duplicate city_id in cities.csv: {duplicates}'


# ---------------------------------------------------------------------------
# The whole report
# ---------------------------------------------------------------------------

def run_main(tmp_path, monkeypatch, *args, cities=(('seattle-wa', 'Seattle'),), logs=()):
    """Drive main() in-process against a corpus in tmp_path.

    LOGS_DIR and CITIES_FILE are module globals read at call time, so redirecting them is enough - main()
    needs no flags for it, and adding some would be CLI surface that exists only for tests.
    """
    logs_dir = tmp_path / 'logs'
    logs_dir.mkdir(exist_ok=True)
    cities_file = tmp_path / 'cities.csv'
    with open(cities_file, 'w', newline='') as f:
        f.write('city_id,display_name\n')
        for city_id, display_name in cities:
            f.write(f'{city_id},{display_name}\n')
    for city_id, rows in logs:
        write_log(logs_dir / f'log-{city_id}.csv', list(rows))

    monkeypatch.setattr(analyze, 'LOGS_DIR', logs_dir)
    monkeypatch.setattr(analyze, 'CITIES_FILE', cities_file)
    return analyze.main(list(args))


TWO_CITIES = (('seattle-wa', 'Seattle'), ('newberg-or', 'Newberg'))


def squash(text):
    """Collapse whitespace runs, so assertions read the report's words without pinning its column widths."""
    return ' '.join(text.split())


class TestTheWholeReport:
    """main() end to end - the ~100 lines that were the single largest dark region in the repo (#57).

    Everything the fleet actually sees comes out of here: which cities were checked, what the summary counts
    were, and whether a bad city takes the rest down with it.
    """

    def healthy(self, city_id):
        return (city_id, recent_rows(7, image_skip=100, image_total=100))

    def test_a_healthy_corpus_reports_every_city_and_returns_zero(self, tmp_path, monkeypatch, capsys):
        status = run_main(tmp_path, monkeypatch, '--no-download', cities=TWO_CITIES,
                          logs=[self.healthy('seattle-wa'), self.healthy('newberg-or')])
        out = squash(capsys.readouterr().out)

        assert status == 0
        assert 'SUMMARY — 2 cities checked' in out
        assert 'Critical : 0' in out
        assert 'OK : 2' in out
        assert 'Seattle' in out and 'Newberg' in out

    def test_one_critical_city_returns_one_and_names_it(self, tmp_path, monkeypatch, capsys):
        status = run_main(tmp_path, monkeypatch, '--no-download', cities=TWO_CITIES,
                          logs=[('seattle-wa', [make_row(days_ago(10))]), self.healthy('newberg-or')])
        out = squash(capsys.readouterr().out)

        assert status == 1
        assert 'Critical : 1 seattle-wa' in out
        assert 'OK : 1' in out
        # The healthy city is still reported, not swallowed by its neighbour's failure.
        assert 'Newberg' in out

    def test_a_missing_log_is_that_citys_problem_alone(self, tmp_path, monkeypatch, capsys):
        # No log written for newberg-or at all - the state after a download that never succeeded.
        status = run_main(tmp_path, monkeypatch, '--no-download', cities=TWO_CITIES,
                          logs=[self.healthy('seattle-wa')])
        out = squash(capsys.readouterr().out)

        assert status == 1
        assert 'Log file missing' in out
        assert 'Critical : 1 newberg-or' in out

    def test_the_city_flag_narrows_the_report(self, tmp_path, monkeypatch, capsys):
        status = run_main(tmp_path, monkeypatch, '--no-download', '--city', 'newberg-or',
                          cities=TWO_CITIES, logs=[self.healthy('newberg-or')])
        out = capsys.readouterr().out

        assert status == 0
        assert 'SUMMARY — 1 cities checked' in out
        assert 'Seattle' not in out

    def test_an_unknown_city_names_the_file_it_looked_in(self, tmp_path, monkeypatch):
        """Naming the path matters: the usual cause is that the city was never added to cities.csv, and the
        deployed copy is not necessarily the one the operator is looking at."""
        with pytest.raises(SystemExit) as excinfo:
            run_main(tmp_path, monkeypatch, '--no-download', '--city', 'atlantis', cities=TWO_CITIES)

        assert 'atlantis' in str(excinfo.value)
        assert 'cities.csv' in str(excinfo.value)

    def test_no_download_never_reaches_the_network(self, tmp_path, monkeypatch, capsys):
        def explode(*args, **kwargs):
            raise AssertionError('--no-download must not touch the pano store')

        monkeypatch.setattr(analyze, 'download_log', explode)

        assert run_main(tmp_path, monkeypatch, '--no-download',
                        logs=[self.healthy('seattle-wa')]) == 0

    def test_a_failed_download_does_not_stop_the_other_cities(self, tmp_path, monkeypatch, capsys):
        """The property the fleet depends on. One unreachable city must cost one CRITICAL, not the report -
        a `break` here would silently stop monitoring every city after it in cities.csv.
        """
        monkeypatch.setenv('PS_SFTP_HOST', 'ps-panos.invalid')
        monkeypatch.setenv('PS_SFTP_BASE', '/panos')
        attempted = []

        def fake_download(city_id, dest, sftp):
            attempted.append(city_id)
            return city_id != 'seattle-wa'   # the first city in the list is the one that fails

        monkeypatch.setattr(analyze, 'download_log', fake_download)
        monkeypatch.setattr(analyze.roster, 'fetch_roster',
                            fetch_ok(roster_entry('seattle-wa'), roster_entry('newberg-or')))

        status = run_main(tmp_path, monkeypatch, cities=TWO_CITIES,
                          logs=[self.healthy('seattle-wa'), self.healthy('newberg-or')])
        out = squash(capsys.readouterr().out)

        assert attempted == ['seattle-wa', 'newberg-or'], 'the sweep stopped at the first failure'
        assert status == 1
        # NB the "Download failed" issue itself never reaches stdout: main() `continue`s past the city block
        # that would have printed it, so the operator sees the inline FAILED and the summary line. That is
        # enough to act on, but it does mean the issue text is summary-only.
        assert 'Seattle — downloading… FAILED' in out
        assert 'Critical : 1 seattle-wa' in out
        assert 'OK : 1' in out

    def test_a_city_whose_stats_line_cannot_be_built_still_appears(self, tmp_path, monkeypatch, capsys):
        # The stats line is best-effort: an unreadable log must still produce a city block saying so, rather
        # than an exception out of the summary code.
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir()
        (logs_dir / 'log-seattle-wa.csv').write_bytes(b'start_time,\x81\x81\x81\n')

        status = run_main(tmp_path, monkeypatch, '--no-download')
        out = capsys.readouterr().out

        assert status == 1
        assert 'Seattle' in out
        assert 'Could not parse log' in out


# --- Two eras of start_time in one file (#101) ------------------------------------------------------------
#
# Rows written before #101 are `str(datetime.now())`: a bare local reading. Rows written after carry a UTC
# offset. Production log.csv files are append-only and years old, so every real file will hold both for as
# long as it exists, and read_log has to place both on the same timeline. `utc=True` is what does that: it
# converts an offset-carrying row and reads a bare one as UTC, which is correct because every scraper host
# has run UTC.

def aware_days_ago(n, offset_hours, extra_hours=0):
    """A timestamp exactly n days (plus extra_hours) before the analyzer's clock, rendered in a zone
    offset_hours from UTC - i.e. the same INSTANT the naive helper above would produce, written the way a
    non-UTC host writes it.

    The point of the offset is that the wall reading and the instant disagree. A parser that keeps the wall
    reading and drops the offset places the row offset_hours away from where it belongs.
    """
    instant = datetime.now(timezone.utc) - timedelta(days=n, hours=extra_hours)
    return instant.astimezone(timezone(timedelta(hours=offset_hours))).isoformat(sep=' ',
                                                                                timespec='microseconds')


class TestARowThatNamesItsOffsetIsPlacedByItsInstant:
    """Staleness is `(now_utc - start_time).days > stale_days`, and `.days` floors - so misreading the clock
    by 7 hours moves a city across the threshold whenever the run sits within 7 hours of a day boundary.
    Both directions have a cost, and both are here.
    """

    def test_a_recent_run_written_west_of_utc_is_not_reported_stale(self, tmp_path):
        """3 days 20 hours old is 3 days, and 3 is not > 3. Read as a bare local time from a -07:00 host it
        looks 4 days 3 hours old, and the city is reported CRITICAL for a scrape that ran last night."""
        log = write_log(tmp_path / 'log.csv',
                        [make_row(aware_days_ago(3, offset_hours=-7, extra_hours=20))])

        issues = analyze.analyze_city('richmond-va', log, stale_days=3)

        assert not [i for i in issues if 'days old' in i['msg']], issues

    def test_an_old_run_written_east_of_utc_is_still_reported_stale(self, tmp_path):
        """The missed-alert direction, which is the more expensive one: a city that has not run for over
        four days must not read as three because its host is +02:00. A monitor that under-reports is worse
        than no monitor, because it is trusted."""
        log = write_log(tmp_path / 'log.csv',
                        [make_row(aware_days_ago(4, offset_hours=2, extra_hours=1))])

        issues = analyze.analyze_city('zurich', log, stale_days=3)

        assert [i for i in issues if i['level'] == 'CRITICAL' and 'days old' in i['msg']], issues


class TestALogHoldingBothErasStillParses:

    def test_naive_and_offset_rows_land_on_one_timeline(self, tmp_path):
        """The migration case, and the one that breaks loudest: without utc=True a column mixing the two
        shapes comes back as object dtype, and analyze_city dies on `.dt` a few lines later. main() has no
        per-city try/except, so that ends the report for every city after this one."""
        rows = [make_row(days_ago(4)),                                   # pre-#101 row
                make_row(days_ago(3)),
                make_row(aware_days_ago(1, offset_hours=-7)),            # post-#101 row
                make_row(aware_days_ago(0, offset_hours=-7))]
        log = write_log(tmp_path / 'log.csv', rows)

        df = analyze.read_log(log)

        assert len(df) == 4, 'no row of either era may be dropped'
        assert df["start_time"].dt.tz is not None, 'the column must be tz-aware for the comparisons below'
        assert df["start_time"].is_monotonic_increasing, \
            'sorting must order the two eras by instant, not by their raw text'

    def test_a_mixed_era_log_still_reaches_the_alert_rules(self, tmp_path):
        """Parsing is not the whole contract: the rules downstream have to keep working on the parsed frame.
        A bare `.dropna()`-style fix would satisfy the test above and still leave this one dead."""
        rows = [make_row(days_ago(6 - i), image_fail=100 * i) for i in range(4)]
        rows += [make_row(aware_days_ago(2 - i, offset_hours=-7), image_fail=100 * (4 + i)) for i in range(3)]
        log = write_log(tmp_path / 'log.csv', rows)

        issues = analyze.analyze_city('seattle-wa', log, stale_days=3)

        assert [i for i in issues if 'failures growing fast' in i['msg']], issues

    def test_a_genuinely_unparseable_timestamp_is_still_dropped(self, tmp_path):
        """utc=True must not soften errors="coerce" into accepting junk."""
        log = write_log(tmp_path / 'log.csv',
                        [make_row('not-a-timestamp'), make_row(aware_days_ago(0, offset_hours=-7))])

        assert len(analyze.read_log(log)) == 1


class TestTheAnalyzerParsesWhatTheRunnerWrites:
    """The coupling #101 is actually about. Nothing else in either suite reads column 0 of one module with
    the other module's parser, which is how "written local, compared against UTC" survived unnoticed.
    """

    def test_a_timestamp_from_the_runner_round_trips_through_read_log(self, tmp_path):
        import DownloadRunner

        written = DownloadRunner.log_timestamp()
        log = write_log(tmp_path / 'log.csv', [make_row(written)])

        parsed = analyze.read_log(log)["start_time"].iloc[0]

        assert parsed.tz is not None
        # Same instant, not merely the same characters: this is what makes staleness arithmetic correct.
        assert parsed.to_pydatetime() == datetime.fromisoformat(written)

    def test_a_fresh_run_from_the_runner_is_not_stale(self, tmp_path):
        """The end-to-end statement in the units operators care about: a city that just ran is clean, on a
        host at any offset. This is the assertion that fails if either half of the pair regresses."""
        import DownloadRunner

        log = write_log(tmp_path / 'log.csv', [make_row(DownloadRunner.log_timestamp())])

        assert analyze.analyze_city('seattle-wa', log, stale_days=3) == []


# --- The intake never infers the file's shape (#43, the #46/#72 class) ------------------------------------
#
# Field 19 arrives on production files that already hold a hand-written 18-name header and years of 18-field
# rows. Measured on pandas 3.0.5 before the change: read_csv(names=LOG_COLUMNS) cannot read that file under
# either engine - the C parser fixes the width from the header and dies on the first 19-field row - so every
# city would have gone CRITICAL "Could not parse log" the morning after the deploy. The intake now pads and
# truncates each row itself, so these tests are about positions: every count must land in its own column.

class TestTheIntakeNeverInfersTheShape:

    OLD_HEADER = ','.join(analyze.LOG_COLUMNS[:18])
    NEW_HEADER = ','.join(analyze.LOG_COLUMNS)

    def write(self, tmp_path, rows, header):
        lines = ([header] if header else []) + rows
        path = tmp_path / 'log.csv'
        path.write_text('\n'.join(lines) + '\n')
        return path

    @pytest.mark.parametrize('header', [OLD_HEADER, NEW_HEADER, None], ids=['old-header', 'new-header', 'no-header'])
    @pytest.mark.parametrize('old_first', [True, False], ids=['old-then-new', 'new-then-old'])
    def test_every_count_stays_in_its_own_column(self, tmp_path, header, old_first):
        old = old_row(days_ago(2), image_success=5, total_minutes=7)
        new = make_row(days_ago(1), image_success=5, total_minutes=7, depth_eligible=1000)
        log = self.write(tmp_path, [old, new] if old_first else [new, old], header)

        df = analyze.read_log(log)

        assert list(df.columns) == analyze.LOG_COLUMNS
        assert len(df) == 2
        assert df['image_success'].tolist() == [5, 5]
        assert df['total_minutes'].tolist() == [7, 7], 'the last pre-#43 field must not move'
        assert df['depth_eligible'].isna().iloc[0], 'the old row has no corpus size'
        assert df['depth_eligible'].iloc[1] == 1000

    def test_the_production_transition_file_parses(self, tmp_path):
        """The exact shape every city's file has the morning after the deploy: the hand-written 18-name
        header, then the old rows, then the new ones."""
        rows = [old_row(days_ago(n), image_total=100) for n in (4, 3, 2)] + \
               [make_row(days_ago(n), image_total=100, depth_eligible=100) for n in (1, 0)]
        log = self.write(tmp_path, rows, self.OLD_HEADER)

        df = analyze.read_log(log)

        assert len(df) == 5
        assert df['image_total'].tolist() == [100] * 5

    def test_a_row_wider_than_the_columns_is_dropped_not_shifted(self, tmp_path):
        """One surplus field is the #46 shape: pandas answered it by taking the first column as the index and
        shifting every count one place left, silently. Here the row is left out of the frame and counted for
        rule 9 - not truncated into a run, which is what this test pinned until a stray comma INSIDE a row
        (rather than after its last field) showed the truncation reading total_minutes as the corpus size."""
        good = make_row(days_ago(1), image_success=5, depth_eligible=1000)
        wide = make_row(days_ago(0), image_success=5, depth_eligible=1000) + ',999'
        log = self.write(tmp_path, [good, wide], None)

        df = analyze.read_log(log)

        assert len(df) == 1
        assert df['image_success'].iloc[0] == 5
        assert df['depth_eligible'].iloc[0] == 1000
        assert df.attrs['malformed_rows'] == 1

    def test_a_lone_old_file_reads_with_a_blank_last_column(self, tmp_path):
        log = self.write(tmp_path, [old_row(days_ago(n), image_fail=3) for n in (1, 0)], self.OLD_HEADER)

        df = analyze.read_log(log)

        assert df['image_fail'].tolist() == [3, 3]
        assert df['depth_eligible'].isna().all()


# --- Depth backfill progress (#43) -----------------------------------------------------------------------

def depth_rows(n_days_ago, eligible, resolved_before, requests, ran=True, unavailable=0, transient=0,
               **overrides):
    """A row on which the depth phase made `requests` requests on top of `resolved_before` already in the
    ledger - or, with ran=False, the five-zero row a stand-down writes.

    The three outcomes are separated because the row cannot separate them and the analyzer must not assume
    it can. `unavailable` is a permanent verdict: it IS ledgered, so it resolves the pano, but it is counted
    in depth_fail alongside `transient` - which is NOT ledgered and will be re-requested next run. The
    previous version of this helper called depth_fail "half unavailable" and so encoded the assumption that
    every failure resolves something, which is precisely the arithmetic that let a city report "depth
    complete" with panos that will never have depth. Both default to 0, so a plain call is a clean night.
    """
    if not ran:
        return make_row(days_ago(n_days_ago), depth_eligible=eligible, **overrides)
    saved = requests - unavailable - transient
    return make_row(days_ago(n_days_ago), depth_success=saved, depth_fail=unavailable + transient,
                    depth_skip=resolved_before, depth_total=resolved_before + requests, depth_minutes=12,
                    total_minutes=12, depth_eligible=eligible, **overrides)


class TestDepthProgress:
    """depth_progress is the one place the backfill's figures are defined; every rule and every line of the
    report reads them from here. So each definition is pinned against the row shape that would break it."""

    def test_nothing_can_be_said_before_the_column_exists(self, tmp_path):
        log = write_log(tmp_path / 'log.csv', [old_row(days_ago(n)) for n in (2, 1, 0)])

        assert analyze.depth_progress(analyze.read_log(log)) is None

    def test_resolved_is_read_from_the_newest_row_on_which_the_phase_ran(self, tmp_path):
        """A stand-down row (block latch, --skip-depth, unwritable ledger) is five zeros. It is not "0 resolved":
        reading it that way would report the city un-backfilled the morning after a single stand-down."""
        log = write_log(tmp_path / 'log.csv', [depth_rows(1, 1000, 0, 590),
                                               depth_rows(0, 1000, 0, 0, ran=False)])

        progress = analyze.depth_progress(analyze.read_log(log))

        assert progress['resolved'] == 590
        assert progress['unresolved'] == 410
        assert progress['newest_accounted'] is False

    def test_two_runs_on_one_night_are_one_nights_requests(self, tmp_path):
        """The queue's extra passes put two rows on one date; the nightly rate must not halve for it."""
        base = days_ago(1).replace(hour=1, minute=0, second=0, microsecond=0)
        # The old row makes the log older than the window, so the divisor is DEPTH_RATE_NIGHTS and the two
        # dates that carry rows are not the whole span - the shape a per-logged-date average gets wrong.
        rows = [old_row(days_ago(20)),
                make_row(base, depth_success=300, depth_fail=290, depth_skip=0, depth_total=590,
                         depth_eligible=5000),
                make_row(base.replace(hour=5), depth_success=100, depth_fail=100, depth_skip=590,
                         depth_total=790, depth_eligible=5000),
                depth_rows(0, 5000, 790, 590)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        # Divided by DEPTH_RATE_NIGHTS calendar nights, not by the two dates that happen to carry rows.
        assert progress['nightly'] == pytest.approx((790 + 590) / analyze.DEPTH_RATE_NIGHTS)
        assert progress['resolved'] == 790 + 590

    def test_both_rates_are_measured_over_the_same_nights(self, tmp_path):
        """A city two nights into its log: 590 requests a night, every one saved. Requests were divided by
        the 7-night window and panos by the log's span, so the fleet line read `+590 panos/night on +169
        requests` - more panos resolved than requests made. Every city in its first week has this shape."""
        rows = [depth_rows(1, 10_000, 0, 590), depth_rows(0, 10_000, 590, 590)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['nightly'] == pytest.approx(590)
        assert progress['nightly_resolved'] == pytest.approx(590)
        assert '+590 panos/night on +590 requests' in analyze.fleet_depth_summary({'x': progress})[1]

    def test_nights_left_is_unresolved_over_the_nightly_rate(self, tmp_path):
        rows = [depth_rows(n, 1000, 100 * (2 - n), 100) for n in (2, 1, 0)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['resolved'] == 300
        assert progress['nights_left'] == pytest.approx(7.0)

    def test_a_complete_city_has_no_eta(self, tmp_path):
        rows = [depth_rows(1, 500, 0, 500), make_row(days_ago(0), depth_skip=500, depth_total=500,
                                                       depth_eligible=500)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['unresolved'] == 0
        assert progress['nights_left'] is None

    def test_a_city_that_never_ran_depth_is_all_unresolved_with_no_eta(self, tmp_path):
        rows = [depth_rows(n, 100, 0, 0, ran=False) for n in (2, 1, 0)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert (progress['resolved'], progress['unresolved']) == (0, 100)
        assert progress['nightly'] == 0 and progress['nights_left'] is None
        assert progress['quiet_nights'] == 3

    def test_a_city_with_no_gsv_panos_has_nothing_unresolved(self, tmp_path):
        rows = [depth_rows(0, 0, 0, 0, ran=False)]

        assert analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))['unresolved'] == 0

    def test_quiet_nights_count_back_from_the_newest(self, tmp_path):
        rows = [depth_rows(4, 1000, 0, 100)] + [depth_rows(n, 1000, 100, 0) for n in (3, 2, 1, 0)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['quiet_nights'] == 4
        assert progress['newest_accounted'] is True

    def test_a_shrunken_corpus_does_not_go_negative(self, tmp_path):
        rows = [depth_rows(1, 1000, 0, 900), depth_rows(0, 800, 900, 0)]

        assert analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))['unresolved'] == 0


class TestADepthPhaseThatStoppedMakingProgressIsFlagged:
    """Check 7. Five zeros in the depth columns is what --skip-depth writes, what the block latch writes when
    it stands a run down, what an unwritable ledger writes - and what a finished city writes. Only the corpus
    size tells those apart, and only this rule reads it; nothing else in the analyzer can see the phase."""

    def issues(self, tmp_path, rows):
        return analyze.analyze_city('somewhere', write_log(tmp_path / 'log.csv', rows), stale_days=3)

    def test_three_quiet_nights_with_work_left_is_a_warning(self, tmp_path):
        rows = [depth_rows(3, 1000, 0, 590)] + [depth_rows(n, 1000, 0, 0, ran=False) for n in (2, 1, 0)]

        warnings = [i for i in self.issues(tmp_path, rows) if i['level'] == 'WARNING']

        assert len(warnings) == 1, warnings
        assert 'Depth backfill stalled' in warnings[0]['msg']
        assert '410 of 1,000' in warnings[0]['msg']
        assert 'accounted for nothing' in warnings[0]['msg']

    def test_a_phase_that_ran_but_made_no_requests_names_the_budget(self, tmp_path):
        """depth_total > 0 with no requests: the ledger was read and nothing was asked - the shape of an image
        phase that spent the whole run. A different fix from the stand-down, so a different message."""
        rows = [depth_rows(3, 1000, 0, 590)] + [depth_rows(n, 1000, 590, 0) for n in (2, 1, 0)]

        warnings = [i for i in self.issues(tmp_path, rows) if i['level'] == 'WARNING']

        assert len(warnings) == 1, warnings
        assert '--min-depth-runtime' in warnings[0]['msg']
        assert 'did not run' not in warnings[0]['msg']

    def test_two_quiet_nights_are_not_enough(self, tmp_path):
        rows = [depth_rows(2, 1000, 0, 590)] + [depth_rows(n, 1000, 0, 0, ran=False) for n in (1, 0)]

        assert self.issues(tmp_path, rows) == []

    def test_a_single_request_on_the_newest_night_resets_the_count(self, tmp_path):
        rows = [depth_rows(n, 1000, 0, 0, ran=False) for n in (3, 2, 1)] + [depth_rows(0, 1000, 0, 1)]

        assert self.issues(tmp_path, rows) == []

    def test_a_complete_city_is_quiet_by_design(self, tmp_path):
        rows = [depth_rows(3, 500, 0, 500)] + [depth_rows(n, 500, 500, 0) for n in (2, 1, 0)]

        assert self.issues(tmp_path, rows) == []

    def test_a_city_with_no_gsv_panos_is_never_stalled(self, tmp_path):
        rows = [depth_rows(n, 0, 0, 0, ran=False) for n in (3, 2, 1, 0)]

        assert self.issues(tmp_path, rows) == []

    def test_nothing_fires_before_the_column_exists(self, tmp_path):
        rows = [old_row(days_ago(n)) for n in (3, 2, 1, 0)]

        assert self.issues(tmp_path, rows) == []


def test_a_blank_depth_eligible_alone_is_not_an_early_end(tmp_path):
    """Every row written before field 19 existed is blank there (#43). Six of the last seven runs blank in one
    column is exactly the shape check 5 looks for, so without the PHASE_COLUMNS restriction the whole fleet
    would have read as 'ended early' for a week after the deploy."""
    rows = [old_row(days_ago(n)) for n in (6, 5, 4, 3, 2, 1)] + [make_row(days_ago(0), depth_eligible=100)]

    assert not [i for i in analyze.analyze_city('somewhere', write_log(tmp_path / 'log.csv', rows), stale_days=3)
                if 'ended early' in i['msg']]


class TestTheStatsLineReportsDepth:

    def test_progress_rate_and_eta(self, tmp_path):
        rows = [depth_rows(1, 10000, 0, 590), depth_rows(0, 10000, 590, 590)]

        line = analyze.city_stats(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert 'depth 1,180/10,000 (11.8%)' in line
        assert '+590 panos/night' in line
        assert '~15 nights left' in line

    def test_a_complete_city(self, tmp_path):
        rows = [depth_rows(1, 500, 0, 500), depth_rows(0, 500, 500, 0)]

        assert 'depth complete (500)' in analyze.city_stats(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

    def test_a_city_that_has_not_started(self, tmp_path):
        rows = [depth_rows(0, 500, 0, 0, ran=False)]

        assert 'depth not started (0/500)' in analyze.city_stats(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

    def test_before_the_column_exists_the_line_says_nothing_about_depth(self, tmp_path):
        rows = [old_row(days_ago(n)) for n in (1, 0)]

        assert 'depth' not in analyze.city_stats(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))


class TestTheFleetDepthSummary:

    def test_it_totals_the_fleet_and_names_the_longest_city(self, tmp_path, monkeypatch, capsys):
        seattle = [depth_rows(1, 10000, 0, 590), depth_rows(0, 10000, 590, 590)]
        newberg = [depth_rows(1, 500, 0, 500), depth_rows(0, 500, 500, 0)]

        status = run_main(tmp_path, monkeypatch, '--no-download', cities=TWO_CITIES,
                          logs=[('seattle-wa', seattle), ('newberg-or', newberg)])
        out = squash(capsys.readouterr().out)

        assert status == 0
        assert 'DEPTH BACKFILL — 2 of 2 cities report a corpus' in out
        assert 'resolved 1,680 of 10,500 GSV panos (16.0%)' in out
        assert '1 complete · 0 stalled' in out
        assert 'longest remaining: seattle-wa ~15 nights (8,820 left)' in out

    def test_a_stalled_city_is_counted(self, tmp_path, monkeypatch, capsys):
        seattle = [depth_rows(3, 1000, 0, 590)] + [depth_rows(n, 1000, 0, 0, ran=False) for n in (2, 1, 0)]

        run_main(tmp_path, monkeypatch, '--no-download', logs=[('seattle-wa', seattle)])

        assert '0 complete · 1 stalled' in squash(capsys.readouterr().out)

    def test_nothing_is_printed_before_any_city_reports_a_corpus(self, tmp_path, monkeypatch, capsys):
        run_main(tmp_path, monkeypatch, '--no-download',
                 logs=[('seattle-wa', [old_row(days_ago(n)) for n in (1, 0)])])

        assert 'DEPTH BACKFILL' not in capsys.readouterr().out

    def test_the_function_is_empty_for_an_empty_fleet(self):
        assert analyze.fleet_depth_summary({}) == []
        assert analyze.fleet_depth_summary({'x': None}) == []

    def test_a_fleet_with_no_eta_anywhere_has_no_longest_line(self, tmp_path, monkeypatch, capsys):
        """Every city complete, or none started: there is a block, and nothing to rank."""
        newberg = [depth_rows(1, 500, 0, 500), depth_rows(0, 500, 500, 0)]

        run_main(tmp_path, monkeypatch, '--no-download', logs=[('seattle-wa', newberg)])
        out = squash(capsys.readouterr().out)

        assert 'DEPTH BACKFILL' in out and '1 complete' in out
        assert 'longest remaining' not in out


# ---------------------------------------------------------------------------
# Review fixes (#124). Each class below pins one figure the analyzer reported
# confidently and wrongly, and each was reproduced against the branch before it
# was fixed.
# ---------------------------------------------------------------------------

class TestATransientFailureResolvesNothing:
    """depth_fail carries BOTH a permanent `unavailable` verdict and a transient failure, and only the first
    resolves a pano. depth_total adds all of it, so reading progress from it counted work that will be done
    again tomorrow - and the last stragglers of a backfill are exactly the panos that keep failing, so this
    is the ordinary end-state, not an exotic one."""

    def test_a_night_of_transient_failure_adds_nothing_to_resolved(self, tmp_path):
        rows = [depth_rows(1, 10_000, 0, 1000),
                depth_rows(0, 10_000, 1000, 500, transient=500)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['resolved'] == 1000          # depth_total says 1,500
        assert progress['unresolved'] == 9000

    def test_a_city_is_not_complete_while_its_stragglers_keep_failing(self, tmp_path):
        """The whole remaining candidate set failing transiently made depth_total reach the corpus, so the
        city reported `depth complete` and rule 7 could not fire - it is gated on unresolved > 0, which the
        same arithmetic had just zeroed."""
        rows = [depth_rows(n, 1000, 900, 100, transient=100) for n in (4, 3, 2, 1, 0)]
        log = write_log(tmp_path / 'log.csv', rows)

        progress = analyze.depth_progress(analyze.read_log(log))

        assert progress['resolved'] == 900
        assert progress['unresolved'] == 100
        assert 'depth complete' not in analyze.city_stats(analyze.read_log(log))

    def test_resolved_never_runs_backwards(self, tmp_path):
        """A heavy-failure night followed by a quiet one made the reported figure DROP, which is the tell
        that it was never a cumulative resolved count."""
        night_1 = [depth_rows(1, 10_000, 0, 1000, transient=300)]
        night_2 = night_1 + [depth_rows(0, 10_000, 700, 0)]

        first  = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'a.csv', night_1)))
        second = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'b.csv', night_2)))

        assert first['resolved'] == 700 and second['resolved'] == 700

    def test_resolved_follows_the_newest_row_when_the_corpus_retires_panos(self, tmp_path):
        """depth_skip counts ledgered panos still in TONIGHT's corpus, so when Google retires 180 of the 900
        the ledger holds, the ledger-derived count falls to 720. Taking the maximum over the log instead
        reported 900 resolved of an 800-pano corpus: `depth complete`, with 80 panos never requested."""
        rows = [depth_rows(1, 1000, 0, 900), depth_rows(0, 800, 720, 0)]
        log = write_log(tmp_path / 'log.csv', rows)

        progress = analyze.depth_progress(analyze.read_log(log))

        assert progress['resolved'] == 720
        assert progress['unresolved'] == 80
        assert 'depth complete' not in analyze.city_stats(analyze.read_log(log))

    def test_an_unavailable_verdict_still_resolves_the_pano(self, tmp_path):
        """The discrimination: `unavailable` is permanent and ledgered, so it must NOT be treated as
        outstanding. It arrives one night late, when the next run reads it back as a skip - a lower bound
        that converges, which is the safe direction."""
        rows = [depth_rows(1, 1000, 0, 400, unavailable=400),
                depth_rows(0, 1000, 400, 0)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['resolved'] == 400


class TestACorpusOfZeroIsRefused:
    """depth_eligible is len(gsv_panos), so an empty or source-less pano-list answer writes a plausible 0.
    last_value skips a BLANK field but not a zero one, so one such night erased the city's entire depth
    report - stats line, fleet block and `longest remaining` ranking - and raised nothing."""

    # The corpus shrinks by ten a night, so the row the substitute is taken from is pinned: the NEWEST
    # believable one, 183,680, not the oldest. With one value on every row, `positive[0]` passed too.
    SEVEN_GOOD = [depth_rows(n, 183_680 + (n - 1) * 10, (7 - n) * 590, 590) for n in (7, 6, 5, 4, 3, 2, 1)]

    def test_one_empty_night_does_not_erase_the_city(self, tmp_path):
        rows = self.SEVEN_GOOD + [depth_rows(0, 0, 0, 0, ran=False)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['eligible'] == 183_680
        assert progress['corpus_suspect'] is True

    def test_it_says_so_rather_than_substituting_silently(self, tmp_path):
        rows = self.SEVEN_GOOD + [depth_rows(0, 0, 0, 0, ran=False)]
        log = write_log(tmp_path / 'log.csv', rows)

        warnings = [i for i in analyze.analyze_city('seattle-wa', log, stale_days=3)
                    if i['level'] == 'WARNING']

        assert any('not believable' in w['msg'] for w in warnings), warnings

    def test_a_corpus_that_merely_shrank_is_believed(self, tmp_path):
        """The discrimination against over-refusing. Google retires panos, so a corpus legitimately shrinks -
        even below what the ledger holds, since those panos were resolved while they were still in it. Only 0
        is impossible for a city that has ever reported one."""
        rows = [depth_rows(1, 1000, 0, 900), depth_rows(0, 800, 900, 0)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['eligible'] == 800
        assert progress['corpus_suspect'] is False

    def test_a_city_with_no_gsv_panos_is_not_flagged(self, tmp_path):
        rows = [depth_rows(n, 0, 0, 0, ran=False) for n in (2, 1, 0)]
        log = write_log(tmp_path / 'log.csv', rows)

        assert analyze.depth_progress(analyze.read_log(log))['corpus_suspect'] is False
        assert not [i for i in analyze.analyze_city('nowhere', log, stale_days=3)
                    if 'believable' in i['msg']]


class TestTheRateIsPerCalendarNight:
    """A city only gets a queue slot on the nights the window reaches it (#101), and a night it never ran
    writes no row at all - so averaging over the dates that happen to appear reads three runs spread over a
    month as three consecutive nights."""

    def test_runs_spread_over_a_month_are_not_a_nightly_rate(self, tmp_path):
        rows = [depth_rows(31, 10_000, 0, 600),
                depth_rows(16, 10_000, 600, 600),
                depth_rows(0, 10_000, 1200, 600)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        # 600 requests on one of the last seven nights, not 600 a night.
        assert progress['nightly'] == pytest.approx(600 / analyze.DEPTH_RATE_NIGHTS)
        assert progress['nights_left'] > 90       # read as ~14 while the divisor was the logged rows

    def test_the_eta_divides_panos_by_panos(self, tmp_path):
        """nights_left used to divide unresolved PANOS by REQUESTS, so a night of heavy transient failure -
        which spends requests and resolves nothing - read as a productive one."""
        rows = [depth_rows(n, 10_000, 0, 1000, transient=1000) for n in (2, 1, 0)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['nightly'] > 0            # requests were certainly spent
        assert progress['nightly_resolved'] == 0
        assert progress['nights_left'] is None    # undefined is not zero, and not 10 nights


class TestQuietNightsAreNights:
    def test_quarterly_rows_are_not_three_quiet_nights(self, tmp_path):
        """The message printed a ROW count as a night count: three rows a month apart said 'the last 3
        nights' when it had been ninety days, and a weekly-logging city needed 21 days to reach the
        threshold."""
        rows = [depth_rows(90, 10_000, 0, 600)] + [depth_rows(n, 10_000, 600, 0, ran=False) for n in (60, 30, 0)]

        progress = analyze.depth_progress(analyze.read_log(write_log(tmp_path / 'log.csv', rows)))

        assert progress['quiet_nights'] == 90


class TestRule4CanFireWhenDepthDominatesTheRun:
    """A mature city's image phase is a ledger read and a membership scan while depth spends the whole slot,
    so total minus depth was 0 on essentially every row, the median was 0, and `median > 0` switched the rule
    off for exactly the class of city the fleet is moving into."""

    MATURE = [make_row(days_ago(n), image_minutes=0, depth_minutes=12, total_minutes=12,
                       depth_success=500, depth_skip=(19 - n) * 500, depth_total=(19 - n) * 500 + 500,
                       depth_eligible=100_000) for n in range(19, 0, -1)]

    def test_a_hung_image_phase_is_flagged_though_the_median_is_zero(self, tmp_path):
        rows = self.MATURE + [make_row(days_ago(0), image_minutes=180, depth_minutes=12, total_minutes=192,
                                       depth_success=500, depth_skip=9500, depth_total=10_000,
                                       depth_eligible=100_000)]
        log = write_log(tmp_path / 'log.csv', rows)

        warnings = [i for i in analyze.analyze_city('somewhere', log, stale_days=3)
                    if 'long image phase' in i['msg']]

        assert len(warnings) == 1, warnings
        assert '180 min' in warnings[0]['msg']

    def test_an_ordinary_image_phase_is_not_flagged(self, tmp_path):
        """The other end of the same floor: at a median of 1 the threshold was 3 minutes, so a perfectly
        ordinary 4-minute image phase warned."""
        rows = self.MATURE + [make_row(days_ago(0), image_minutes=4, depth_minutes=12, total_minutes=16,
                                       depth_success=500, depth_skip=9500, depth_total=10_000,
                                       depth_eligible=100_000)]
        log = write_log(tmp_path / 'log.csv', rows)

        assert not [i for i in analyze.analyze_city('somewhere', log, stale_days=3)
                    if 'long image phase' in i['msg']]


class TestRule7NamesCandidatesRatherThanACause:
    """The two-way diagnosis was wrong on BOTH shapes it exists to separate."""

    def test_an_unwritable_ledger_is_not_diagnosed_as_a_budget_problem(self, tmp_path):
        """download_depth_maps returns (0, 0, skipped, skipped) when it cannot open the ledger - not five
        zeros - so this landed in the arm that sends the operator to tune --min-depth-runtime while the store
        is read-only."""
        rows = [make_row(days_ago(n), depth_success=0, depth_fail=0, depth_skip=1850, depth_total=1850,
                         depth_minutes=0, total_minutes=1, depth_eligible=5000) for n in (3, 2, 1, 0)]
        log = write_log(tmp_path / 'log.csv', rows)

        stalled = [i for i in analyze.analyze_city('somewhere', log, stale_days=3)
                   if 'Depth backfill stalled' in i['msg']]

        assert len(stalled) == 1, stalled
        assert 'ledger could not be written' in stalled[0]['msg']

    def test_a_fresh_city_out_of_budget_is_not_told_the_phase_did_not_run(self, tmp_path):
        """The inverse: nothing in the ledger to skip, so depth_total is 0 and the phase read as never having
        run when it ran and simply had no time left."""
        rows = [make_row(days_ago(n), depth_eligible=5000, total_minutes=12) for n in (3, 2, 1, 0)]
        log = write_log(tmp_path / 'log.csv', rows)

        stalled = [i for i in analyze.analyze_city('somewhere', log, stale_days=3)
                   if 'Depth backfill stalled' in i['msg']]

        assert len(stalled) == 1, stalled
        assert 'image phase spending the whole budget' in stalled[0]['msg']
        assert 'crashed before the phase' in stalled[0]['msg']


class TestARowThatIsNotARun:
    """A row of some other width is not a run. One stray comma shifts every count one place right, the 19th
    field falls off the end, and the night reads as a quiet healthy one."""

    def test_a_row_of_the_wrong_width_is_reported(self, tmp_path):
        good = make_row(days_ago(1), image_success=10, depth_eligible=183_680)
        torn = make_row(days_ago(0), image_success=10, depth_eligible=183_680) + ',12'
        log = write_log(tmp_path / 'log.csv', [good, torn])

        warnings = [i for i in analyze.analyze_city('seattle-wa', log, stale_days=3)
                    if i['level'] == 'WARNING']

        assert any('neither 18 nor 19' in w['msg'] for w in warnings), warnings

    def test_a_well_formed_pre_corpus_row_is_not_reported(self, tmp_path):
        """The discrimination: 18 fields is every row written before the corpus column existed, and a fleet
        of those must stay silent."""
        log = write_log(tmp_path / 'log.csv', [old_row(days_ago(n)) for n in (2, 1, 0)])

        assert not [i for i in analyze.analyze_city('somewhere', log, stale_days=3)
                    if 'field count' in i['msg']]

    def test_a_shifted_row_does_not_become_the_corpus(self, tmp_path):
        """Reported is not enough: the first version counted the row for rule 9 and then padded it into the
        frame anyway. A stray comma after field 5 moved every later count one place right, the corpus fell
        off the end, total_minutes (12) landed in its place, and a 183,680-pano city read as
        `0 total | depth 0/12 (0.0%)` - with rule 8 silent, because 12 is a believable corpus."""
        good = [depth_rows(n, 183_680, (8 - n) * 590, 590) for n in range(7, 0, -1)]
        parts = depth_rows(0, 183_680, 8 * 590, 590).split(',')
        parts.insert(5, '')
        log = write_log(tmp_path / 'log.csv', good + [','.join(parts)])

        df = analyze.read_log(log)
        progress = analyze.depth_progress(df)
        issues = analyze.analyze_city('seattle-wa', log, stale_days=3)

        assert len(df) == 7, 'the shifted row is not a run'
        assert progress['eligible'] == 183_680
        assert progress['resolved'] == 8 * 590, 'read from the newest row that IS a run'
        assert 'depth 0/12' not in analyze.city_stats(df)
        assert any('neither 18 nor 19' in i['msg'] for i in issues), issues
        assert not any('not believable' in i['msg'] for i in issues), 'nothing suspect reached corpus_size'

    def test_a_torn_last_row_is_not_tonights_run(self, tmp_path):
        """The realistic torn write: the mount dropped mid-row and the line was cut short. It used to pad
        into a crashed-looking run dated tonight; now the newest run is the last complete row."""
        good = [depth_rows(n, 1000, 0, 100) for n in (2, 1)]
        torn = ','.join(depth_rows(0, 1000, 100, 100).split(',')[:12])
        log = write_log(tmp_path / 'log.csv', good + [torn])

        df = analyze.read_log(log)

        assert len(df) == 2
        assert df.attrs['malformed_rows'] == 1
        assert not [i for i in analyze.analyze_city('somewhere', log, stale_days=3) if 'ended early' in i['msg']]

    def test_a_file_of_only_torn_rows_says_so(self, tmp_path):
        """Dropping every row would otherwise report 'Log file is empty', and the one piece of evidence -
        that something wrote rows of the wrong width - would be lost with the frame it was attached to."""
        torn = [','.join(depth_rows(n, 1000, 0, 100).split(',')[:12]) for n in (1, 0)]
        log = write_log(tmp_path / 'log.csv', torn)

        issues = analyze.analyze_city('somewhere', log, stale_days=3)

        assert [i['level'] for i in issues] == ['CRITICAL'], issues
        assert 'no readable run' in issues[0]['msg'] and '2 row(s)' in issues[0]['msg']


class TestRules2And3CountNightsNotRows:
    """Rule 6 was rewritten because the queue's extra passes put more than one row on a night. These two were
    left counting rows as days, and both are named in days."""

    def test_two_passes_a_night_do_not_halve_the_failure_rate(self, tmp_path):
        # 30 new permanent failures a night, over two rows a night: 15 per row.
        rows = []
        for night in range(9, 0, -1):
            base = days_ago(night).replace(hour=3, minute=0, second=0, microsecond=0)
            done = (9 - night) * 30
            rows.append(make_row(base, image_fail=done + 15))
            rows.append(make_row(base.replace(hour=9), image_fail=done + 30))
        log = write_log(tmp_path / 'log.csv', rows)

        warnings = [i for i in analyze.analyze_city('somewhere', log, stale_days=3)
                    if 'Image failures growing fast' in i['msg']]

        assert len(warnings) == 1, warnings
        assert '~30 new permanent failures/day' in warnings[0]['msg']


class TestTheFleetBlockCountsEveryCityItReportedOn:
    def test_a_city_whose_log_never_arrived_is_in_the_denominator(self, tmp_path, monkeypatch, capsys):
        """main() continues past a failed download before progress is touched, so counting that dict read
        'N of N cities report a corpus' at exactly the moment some were invisible."""
        seattle = [depth_rows(1, 10_000, 0, 590), depth_rows(0, 10_000, 590, 590)]

        run_main(tmp_path, monkeypatch, '--no-download', cities=TWO_CITIES,
                 logs=[('seattle-wa', seattle)])
        out = squash(capsys.readouterr().out)

        assert '1 of 2 cities report a corpus' in out


# ---------------------------------------------------------------------------
# The fleet roster cross-check (#133)
# ---------------------------------------------------------------------------
#
# cities.csv has the #130 gap shape: a city with no row is not monitored, and nothing says so. It went wrong
# twice by hand (newport-ky, then laurens-ia plus the bayonne/bayonne-fr id mismatch) and a third time live:
# washington-dc was re-launched, served /adminapi/panos, and had no row here on 2026-09-22.
#
# These drive log_analyzer/roster.py and analyze.roster_check with a stand-in roster - never a live fetch.

roster_mod = analyze.roster


def roster_entry(city_id, visibility='public', url='auto'):
    if url == 'auto':
        url = 'https://%s.example.org' % city_id if visibility == 'public' else None
    return {'city_id': city_id, 'visibility': visibility, 'url': url}


def roster_body(*entries, status='OK'):
    return json.dumps({'status': status, 'cities': list(entries)}).encode()


def city_rows(*pairs):
    return [{'city_id': cid, 'display_name': name} for cid, name in pairs]


def fetch_ok(*entries):
    def fetch(host):
        return list(entries)
    return fetch


ONE_HOST = ('host.example',)


class TestAnUnlistedCityIsNamedAndCritical:
    """The gap itself. A city the roster knows and cities.csv does not is CRITICAL, by name."""

    def test_it_is_named_and_critical(self):
        lines, critical = analyze.roster_check(
            city_rows(('seattle-wa', 'Seattle')), ONE_HOST,
            fetch=fetch_ok(roster_entry('seattle-wa'), roster_entry('laurens-ia')))

        assert critical is True
        assert any('laurens-ia' in line for line in lines)
        assert not any('seattle-wa' in line and 'not in cities.csv' in line for line in lines)

    def test_a_complete_file_passes(self):
        """No false positive: every roster city has a row, so nothing is named and the exit stays clean."""
        lines, critical = analyze.roster_check(
            city_rows(('seattle-wa', 'Seattle'), ('laurens-ia', 'Laurens')), ONE_HOST,
            fetch=fetch_ok(roster_entry('seattle-wa'), roster_entry('laurens-ia')))

        assert critical is False
        assert not any('not in cities.csv' in line for line in lines)

    def test_an_id_that_differs_by_case_is_still_a_gap(self):
        """city_id is a directory name on the store - <base>/<city_id>/log.csv - so Seattle-WA is a
        different directory. This is the Bayonne finding's shape: a row one character off the id the app
        reads its own panos under, which looked present to every eye that checked."""
        lines, critical = analyze.roster_check(
            city_rows(('Seattle-WA', 'Seattle')), ONE_HOST,
            fetch=fetch_ok(roster_entry('seattle-wa')))

        assert critical is True
        assert any('seattle-wa' in line for line in lines)


class TestPrivateCitiesAreNamedToo:
    """#143's rule, adopted rather than diverged from.

    The mutant this kills is the obvious "only check public cities" filter, which is what scrape_queue did
    until 2026-09-19. Twenty of the fifty-nine deployments are private, and washington-dc - live, serving
    /adminapi/panos - was one of them: a public-only check reports a clean sweep while the just-re-launched
    city has no alarm at all.
    """

    def test_a_private_city_with_no_row_is_named(self):
        lines, critical = analyze.roster_check(
            city_rows(('seattle-wa', 'Seattle')), ONE_HOST,
            fetch=fetch_ok(roster_entry('seattle-wa'),
                           roster_entry('washington-dc', visibility='private')))

        assert critical is True
        assert any('washington-dc' in line for line in lines)

    def test_a_private_city_is_reported_without_inventing_a_host(self):
        """Its url is withheld, and the report says so rather than guessing a hostname. Guessing is exactly
        how sidewalk-dc was found by hand, and it is not a method."""
        lines, _ = analyze.roster_check(
            city_rows(), ONE_HOST,
            fetch=fetch_ok(roster_entry('washington-dc', visibility='private'),
                           roster_entry('seattle-wa')))

        dc = next(line for line in lines if 'washington-dc' in line)
        assert 'private' in dc
        assert 'sidewalk-dc' not in dc

    def test_a_public_city_with_no_url_is_not_called_private(self):
        """The label follows the roster's visibility, not the missing url: a public entry with a null url is
        an upstream fault, and calling it "withheld (private)" would send the operator the wrong way."""
        lines, _ = analyze.roster_check(
            city_rows(), ONE_HOST,
            fetch=fetch_ok(roster_entry('laurens-ia', url=None), roster_entry('seattle-wa')))

        laurens = next(line for line in lines if 'laurens-ia' in line)
        assert 'no url published' in laurens
        assert 'private' not in laurens


class TestTheCheckIsNeverSilentlySkipped:
    """A check skipped every night is the failure it exists to prevent (#130's own rule).

    Both mutants here return ([], False) and read as a passing night.
    """

    def test_no_hosts_is_critical(self):
        """main() can no longer get here - it always passes the defaults - so this is the function's own
        contract: an empty list must not skip the loop and read as a clean check."""
        lines, critical = analyze.roster_check(city_rows(('seattle-wa', 'Seattle')), ())

        assert critical is True
        assert any('no hosts given' in line for line in lines)

    def test_no_roster_served_is_critical(self):
        def fetch(host):
            raise roster_mod.RosterUnavailable('timed out')

        lines, critical = analyze.roster_check(city_rows(), ONE_HOST, fetch=fetch)

        assert critical is True
        assert any('timed out' in line for line in lines)

    def test_an_unexpected_error_is_not_absorbed_as_a_failed_host(self):
        """Only RosterUnavailable means "this host did not answer". Widening the catch "for robustness" would
        turn a bug in the check into a routine CRITICAL line - and would silently disarm _no_roster_network,
        whose whole job is to raise something this does NOT catch."""
        def fetch(host):
            raise RuntimeError('a bug, not a dead host')

        with pytest.raises(RuntimeError):
            analyze.roster_check(city_rows(), ('a.example', 'b.example'), fetch=fetch)

    def test_the_network_guard_fires_through_the_real_fetch(self):
        """End to end: with no fetch stub the real fetch_roster reaches the autouse guard, and the guard's
        error escapes roster_check rather than becoming a report line."""
        with pytest.raises(AssertionError, match='reached the network'):
            analyze.roster_check(city_rows(), ONE_HOST)

    def test_every_host_failing_is_critical_and_names_each(self):
        """Falling back must not turn "the last host failed" into "the check passed", and the operator needs
        every host's reason, not just the last one's."""
        def fetch(host):
            raise roster_mod.RosterUnavailable('down at ' + host)

        lines, critical = analyze.roster_check(city_rows(), ('a.example', 'b.example'), fetch=fetch)

        assert critical is True
        assert any('down at a.example' in line and 'down at b.example' in line for line in lines)


class TestTheNextHostIsTriedWhenOneIsDown:
    """Unlike the SFTP host, the roster host is interchangeable, so one deployment being down is no reason to
    skip the check. scrape_queue.load_roster already asks several; this is the analyzer's version."""

    def test_the_second_host_answers_for_a_dead_first(self):
        asked = []

        def fetch(host):
            asked.append(host)
            if host == 'a.example':
                raise roster_mod.RosterUnavailable('HTTP 503')
            return [roster_entry('seattle-wa')]

        lines, critical = analyze.roster_check(city_rows(('seattle-wa', 'Seattle')),
                                               ('a.example', 'b.example'), fetch=fetch)

        assert asked == ['a.example', 'b.example']
        assert critical is False
        assert any('on b.example' in line for line in lines), 'the report must name the host that answered'

    def test_the_answer_says_which_hosts_failed_first(self):
        def fetch(host):
            if host == 'a.example':
                raise roster_mod.RosterUnavailable('HTTP 503')
            return [roster_entry('seattle-wa')]

        lines, _ = analyze.roster_check(city_rows(('seattle-wa', 'Seattle')),
                                        ('a.example', 'b.example'), fetch=fetch)

        assert any('on b.example' in line and 'a.example: HTTP 503' in line for line in lines)

    def test_at_most_max_hosts_are_asked(self):
        asked = []

        def fetch(host):
            asked.append(host)
            raise roster_mod.RosterUnavailable('down')

        hosts = tuple('h%d.example' % i for i in range(roster_mod.ROSTER_MAX_HOSTS + 2))
        _, critical = analyze.roster_check(city_rows(), hosts, fetch=fetch)

        assert asked == list(hosts[:roster_mod.ROSTER_MAX_HOSTS])
        assert critical is True

    def test_hosts_past_the_cap_are_counted_not_dropped_silently(self):
        def fetch(host):
            raise roster_mod.RosterUnavailable('down')

        n = roster_mod.ROSTER_MAX_HOSTS
        hosts = tuple('h%d.example' % i for i in range(n + 2))
        lines, _ = analyze.roster_check(city_rows(), hosts, fetch=fetch)

        assert any('%d of %d hosts asked' % (n, n + 2) in line for line in lines)

    def test_a_live_first_host_is_the_only_one_asked(self):
        asked = []

        def fetch(host):
            asked.append(host)
            return [roster_entry('seattle-wa')]

        analyze.roster_check(city_rows(('seattle-wa', 'Seattle')), ('a.example', 'b.example'), fetch=fetch)

        assert asked == ['a.example']


class TestTheOptOutMarkerIsExplicit:
    """A '#' row is credited only when it SAYS it is an opt-out.

    scrape_queue can credit a '#' row by checking its second column against the city's host - an
    independent fact. cities.csv's second column is a display name, so there is no such fact here, and any
    rule that merely matched the id would credit the prose comment below. Hence the literal marker.
    """

    def test_a_marked_row_is_credited(self):
        rows = city_rows(('seattle-wa', 'Seattle'),
                         ('#zurich-infra3d', 'not-monitored: infra3d imagery - no GSV to scrape'))
        lines, critical = analyze.roster_check(
            rows, ONE_HOST,
            fetch=fetch_ok(roster_entry('seattle-wa'),
                           roster_entry('zurich-infra3d', visibility='private')))

        assert critical is False
        assert not any('zurich-infra3d' in line for line in lines)

    def test_a_prose_comment_naming_a_city_is_not_credited(self):
        """`# laurens-ia, bayonne-fr launched 2026-09-11` is split by csv into a row whose city_id is
        `# laurens-ia`. Crediting it would silence the very city the comment is about - and it is the exact
        shape scrape_queue's credit rule was written to refuse. Without the marker it stays a comment, so
        the city is still named, which is the safe way round."""
        rows = city_rows(('# laurens-ia', ' bayonne-fr launched 2026-09-11'))
        lines, critical = analyze.roster_check(
            rows, ONE_HOST, fetch=fetch_ok(roster_entry('laurens-ia'), roster_entry('seattle-wa')))

        assert critical is True
        assert any('laurens-ia' in line for line in lines)

    def test_a_bare_hash_row_is_not_credited(self):
        rows = city_rows(('#laurens-ia', ''))
        _, critical = analyze.roster_check(
            rows, ONE_HOST, fetch=fetch_ok(roster_entry('laurens-ia'), roster_entry('seattle-wa')))

        assert critical is True


class TestTheCopyDoesNotDriftFromTheQueues:
    """roster.py is a deliberate copy of scrape_queue's machinery, not an import: the analyzer must keep
    working when the runners are broken. A copy with no pin is a copy that drifts, so both are driven
    against one fixture body - including the refusals, which are where the #99 positive-evidence rule lives.
    """

    @staticmethod
    def queue_parse():
        import scrape_queue
        return scrape_queue.parse_roster

    def test_both_accept_the_measured_shape(self):
        body = roster_body(roster_entry('seattle-wa'),
                           roster_entry('washington-dc', visibility='private'))

        assert ([e['city_id'] for e in roster_mod.parse_roster(body)]
                == [e['city_id'] for e in self.queue_parse()(body)])

    @pytest.mark.parametrize('body', [
        b'not json at all',
        json.dumps({'status': 'OK'}).encode(),
        json.dumps({'status': 'OK', 'cities': []}).encode(),
        json.dumps({'status': 'OK', 'cities': [{'city_id': 'seattle-wa'}]}).encode(),
        json.dumps({'status': 'OK', 'cities': [{'city_id': '', 'visibility': 'public'}]}).encode(),
        json.dumps({'status': 'OK', 'cities': [{'city_id': 'x', 'visibility': 'hidden'}]}).encode(),
        json.dumps({'status': 'OK',
                    'cities': [{'city_id': 'x', 'visibility': 'private'}]}).encode(),
    ])
    def test_both_refuse_the_same_bodies(self, body):
        with pytest.raises(roster_mod.RosterUnavailable):
            roster_mod.parse_roster(body)
        import scrape_queue
        with pytest.raises(scrape_queue.RosterUnavailable):
            self.queue_parse()(body)


class TestTheFailureWordingDoesNotDrift:
    """The copy's other half: `_describe_failure` is what the CRITICAL line says went wrong, so the two
    modules must name the same failure the same way."""

    @pytest.mark.parametrize('error', [
        __import__('urllib.error').error.HTTPError('https://h/v3/api/cities', 503, 'x', {}, None),
        __import__('urllib.error').error.URLError(__import__('socket').timeout('t')),
        __import__('urllib.error').error.URLError(ConnectionRefusedError('refused')),
        __import__('http.client').client.IncompleteRead(b''),
    ], ids=['http-503', 'timeout', 'refused', 'incomplete-read'])
    def test_both_describe_it_alike(self, error):
        import scrape_queue
        assert roster_mod._describe_failure(error) == scrape_queue._describe_failure(error)

    def test_the_constants_match(self):
        import scrape_queue
        for name in ('ROSTER_PATH', 'ROSTER_TIMEOUT_SECONDS', 'ROSTER_MAX_BYTES', 'ROSTER_MAX_HOSTS'):
            assert getattr(roster_mod, name) == getattr(scrape_queue, name), name


class TestTheFetchIsPlainHttps:
    """The one seam that touches a socket: https, the roster path, a named User-Agent, and no credential -
    a keyed roster was considered and rejected (2026-09-23; see roster.py's module docstring)."""

    def test_the_request_it_sends(self, monkeypatch):
        # The suite-wide refusal has to be lifted for this one test: it is _open_url itself under test.
        monkeypatch.setattr(roster_mod, '_open_url', REAL_OPEN_URL)
        seen = {}

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return roster_body(roster_entry('seattle-wa'))

        class FakeOpener:
            def open(self, request, timeout=None):
                seen['url'] = request.full_url
                seen['headers'] = dict(request.header_items())
                seen['timeout'] = timeout
                return FakeResponse()

        import urllib.request as ur
        real = ur.build_opener
        ur.build_opener = lambda *a, **k: FakeOpener()
        try:
            entries = roster_mod.fetch_roster('host.example')
        finally:
            ur.build_opener = real

        assert [e['city_id'] for e in entries] == ['seattle-wa']
        assert seen['url'] == 'https://host.example' + roster_mod.ROSTER_PATH
        assert seen['headers'].get('User-agent') == roster_mod.ROSTER_USER_AGENT
        assert 'Authorization' not in seen['headers']
        assert seen['timeout'] == roster_mod.ROSTER_TIMEOUT_SECONDS


class _RosterMainHarness:
    """Drives main() in download mode with the store stubbed out and a stand-in roster. No tests of its own,
    so the classes below share `_run` without re-running each other's tests."""

    def _run(self, tmp_path, monkeypatch, fetch, cities=(('seattle-wa', 'Seattle'),), argv=(),
             env_host='host.example'):
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(exist_ok=True)
        cities_file = tmp_path / 'cities.csv'
        with open(cities_file, 'w', newline='') as f:
            f.write('city_id,display_name\n')
            for city_id, display_name in cities:
                f.write('%s,%s\n' % (city_id, display_name))

        monkeypatch.setattr(analyze, 'LOGS_DIR', logs_dir)
        monkeypatch.setattr(analyze, 'CITIES_FILE', cities_file)
        # Download mode, because --no-download is offline mode and skips the check - but no store is
        # reachable from a test, so both store seams are stood in for.
        #
        # download_log must SUCCEED and leave a healthy log. A stub returning False books every city
        # CRITICAL ("Download failed"), which makes `status == 1` true whatever the roster did - so the
        # `return 1 if critical else 0` mutant survives and the test proves nothing. It did, on the first
        # version of this test. The roster gap has to be the ONLY critical thing about the run.
        self.downloaded = []

        def fake_download(city_id, dest, sftp):
            self.downloaded.append(city_id)
            write_log(dest, list(recent_rows(3)))
            return True

        monkeypatch.setattr(analyze, 'resolve_sftp', lambda args: {'host': 'h', 'base': '/b', 'user': None,
                                                                  'port': None, 'key': None})
        monkeypatch.setattr(analyze, 'download_log', fake_download)
        monkeypatch.setattr(analyze.roster, 'fetch_roster', fetch)
        if env_host is None:
            monkeypatch.delenv('PS_ROSTER_HOST', raising=False)
        else:
            monkeypatch.setenv('PS_ROSTER_HOST', env_host)
        return analyze.main(list(argv))


class TestTheRosterGapDecidesTheExitCode(_RosterMainHarness):
    """The exit code is the only unattended alarm this script has, and a city with no row here has NO other
    one - the queue at least books its own exit status. So the gap has to reach the return value, not just
    the printed report.

    The mutant is `return 1 if critical else 0` - the pre-#133 line, which prints the gap and exits 0.
    """

    def test_a_complete_roster_on_a_healthy_fleet_exits_zero(self, tmp_path, monkeypatch):
        """The control. Without it, the test below cannot tell "the roster gap set the exit code" from
        "something else about this run was already CRITICAL"."""
        status = self._run(tmp_path, monkeypatch, fetch_ok(roster_entry('seattle-wa')))
        assert status == 0

    def test_an_unlisted_city_exits_nonzero(self, tmp_path, monkeypatch):
        status = self._run(tmp_path, monkeypatch,
                           fetch_ok(roster_entry('seattle-wa'), roster_entry('laurens-ia')))
        assert status == 1

    def test_following_the_opt_out_advice_exits_zero(self, tmp_path, monkeypatch, capsys):
        """The CRITICAL line tells the operator to add `#city,"not-monitored: <why>"`. That row must silence
        the gap AND not be analysed as a city: until the #149 review it was downloaded as `#zurich-infra3d`,
        which fails for real (the stub here succeeds, so the download list is what is asserted), and the run
        exited 1 whichever advice was followed."""
        status = self._run(tmp_path, monkeypatch,
                           fetch_ok(roster_entry('seattle-wa'),
                                    roster_entry('zurich-infra3d', url=None, visibility='private')),
                           cities=(('seattle-wa', 'Seattle'),
                                   ('#zurich-infra3d', '"not-monitored: infra3d imagery"')))
        assert status == 0
        assert self.downloaded == ['seattle-wa']
        out = capsys.readouterr().out
        assert 'Cities: 1 ' in out
        assert 'checked 1 rows' in out

    def test_one_city_is_checked_against_the_whole_file(self, tmp_path, monkeypatch, capsys):
        """`--city` narrows the report, not the cross-check: handed the one filtered row, the check named
        every other city as missing from a file that has them, and exited 1."""
        status = self._run(tmp_path, monkeypatch,
                           fetch_ok(roster_entry('seattle-wa'), roster_entry('laurens-ia')),
                           cities=(('seattle-wa', 'Seattle'), ('laurens-ia', 'Laurens')),
                           argv=['--city', 'seattle-wa'])
        assert status == 0
        assert self.downloaded == ['seattle-wa']
        assert 'not in cities.csv' not in capsys.readouterr().out

    def test_offline_mode_skips_the_check(self, tmp_path, monkeypatch, capsys):
        """--no-download is offline mode; the roster fetch is a network step like the log download."""
        def explode(host):
            raise AssertionError('the roster must not be fetched in offline mode')

        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(exist_ok=True)
        cities_file = tmp_path / 'cities.csv'
        cities_file.write_text('city_id,display_name\nseattle-wa,Seattle\n')
        monkeypatch.setattr(analyze, 'LOGS_DIR', logs_dir)
        monkeypatch.setattr(analyze, 'CITIES_FILE', cities_file)
        monkeypatch.setattr(analyze.roster, 'fetch_roster', explode)

        analyze.main(['--no-download'])

        assert 'Roster cross-check' not in capsys.readouterr().out


class TestTheRosterHostNeedsNoConfiguration(_RosterMainHarness):
    """Every deployment serves the same roster, so the hosts have a default and setting nothing still runs
    the check. The order is flag, then PS_ROSTER_HOST, then DEFAULT_ROSTER_HOSTS."""

    def _asked(self, tmp_path, monkeypatch, **kw):
        asked = []

        def fetch(host):
            asked.append(host)
            return [roster_entry('seattle-wa')]

        status = self._run(tmp_path, monkeypatch, fetch, **kw)
        return status, asked

    def test_the_defaults_are_the_real_hosts(self):
        """Written out, not read back from the constant: every other test here compares the constant with
        itself, so a typo in a hostname would pass them all and fail only in production, as a CRITICAL
        "no host served a roster" every night. Both hosts were checked to serve the roster on 2026-09-24."""
        assert analyze.DEFAULT_ROSTER_HOSTS == ('sidewalk-sea.cs.washington.edu',
                                                'sidewalk-chicago.cs.washington.edu')

    def test_unset_resolves_to_both_real_hosts(self):
        """The resolver, not just the constant: `return DEFAULT_ROSTER_HOSTS[:1]` passed every other test,
        because none needs a second host on the unset path."""
        assert analyze.resolve_roster_hosts(None, None) == ('sidewalk-sea.cs.washington.edu',
                                                            'sidewalk-chicago.cs.washington.edu')

    def test_unset_falls_back_to_chicago_when_seattle_is_down(self, tmp_path, monkeypatch):
        asked = []

        def fetch(host):
            asked.append(host)
            if host == 'sidewalk-sea.cs.washington.edu':
                raise roster_mod.RosterUnavailable('HTTP 503')
            return [roster_entry('seattle-wa')]

        status = self._run(tmp_path, monkeypatch, fetch, env_host=None)

        assert asked == ['sidewalk-sea.cs.washington.edu', 'sidewalk-chicago.cs.washington.edu']
        assert status == 0

    def test_unset_uses_the_default_and_passes(self, tmp_path, monkeypatch):
        """The mutant is losing the default: the check is then CRITICAL and the run exits 1."""
        status, asked = self._asked(tmp_path, monkeypatch, env_host=None)
        assert asked == [analyze.DEFAULT_ROSTER_HOSTS[0]]
        assert status == 0

    @pytest.mark.parametrize('blank', ['', '   ', ' , '], ids=['empty', 'spaces', 'only-commas'])
    def test_a_blank_env_var_is_unset_not_a_host(self, tmp_path, monkeypatch, blank):
        status, asked = self._asked(tmp_path, monkeypatch, env_host=blank)
        assert asked == [analyze.DEFAULT_ROSTER_HOSTS[0]]
        assert status == 0

    def test_the_env_var_overrides_the_default(self, tmp_path, monkeypatch):
        _, asked = self._asked(tmp_path, monkeypatch, env_host='env.example')
        assert asked == ['env.example']

    def test_the_flag_overrides_the_env_var(self, tmp_path, monkeypatch):
        _, asked = self._asked(tmp_path, monkeypatch, env_host='env.example',
                               argv=('--roster-host', 'flag.example'))
        assert asked == ['flag.example']

    def test_a_list_is_split_and_stripped(self):
        assert analyze.resolve_roster_hosts(None, ' a.example , ,b.example ') == ('a.example', 'b.example')

    def test_a_blank_flag_falls_through_to_the_env_var(self):
        assert analyze.resolve_roster_hosts(' ', 'env.example') == ('env.example',)


class TestTheDeployedCityListPassesItsOwnCheck:
    """Sanity checks on the committed cities.csv, the file the production report actually reads. The
    cross-check against the live roster runs nightly in production, not here: the suite is network-free."""

    def test_every_row_is_unique_and_named(self):
        with open(os.path.join(REPO_ROOT, 'log_analyzer', 'cities.csv'), newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        ids = [r['city_id'] for r in rows if not r['city_id'].startswith('#')]
        assert len(ids) == len(set(ids))
        assert all(ids)

    def test_washington_dc_is_monitored(self):
        """Re-launched and live on 2026-09-22 with no row here - the gap this check exists to catch, found
        in the wild while #133 was being written."""
        with open(os.path.join(REPO_ROOT, 'log_analyzer', 'cities.csv'), newline='', encoding='utf-8') as f:
            ids = {r['city_id'] for r in csv.DictReader(f)}
        assert 'washington-dc' in ids
