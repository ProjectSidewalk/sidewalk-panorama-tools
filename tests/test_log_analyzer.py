"""Tests for log_analyzer/analyze.py.

The analyzer is the only consumer of log.csv, so these tests pin the two things that couple it to
DownloadRunner: the 18-column positional layout, and blank fields meaning "this phase never finished".

They also pin the six alert rules themselves. Three of them - extended zero progress, abnormally long
runtime, duplicate runs on one day - had their entire firing branch uncovered until #57, which for an ops
monitor is the worst kind of gap: the failure mode of an alert rule that stops working is silence, and this
one watches a nightly scrape across ~49 production cities with nothing else looking.
"""

import argparse
import ast
import importlib.util
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
        rows = daily_rows(10, offset=1, total_minutes=10) + [make_row(days_ago(0), total_minutes=100)]
        log = write_log(tmp_path / 'log.csv', rows)

        issues = analyze.analyze_city('somewhere', log, stale_days=3)

        warnings = [i for i in issues if i['level'] == 'WARNING']
        assert len(warnings) == 1, issues
        assert 'Recent unusually long run: 100 min' in warnings[0]['msg']
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


class TestTwoRunsOnOneDayAreReportedAsInfo:
    """Check 6: more than one run on a calendar day, which means the cron slot is overlapping itself.

    INFO rather than WARNING - it is usually an operator running the scraper by hand - but it needs saying,
    because two concurrent runs race on the same ledgers.
    """

    @staticmethod
    def two_runs_on(day_offset):
        # Built by replacing the hour rather than by arithmetic on days_ago, so both rows land on one
        # calendar date at every time of day. Subtracting hours instead would silently cross midnight for
        # part of the day, which is the same flooring trap days_ago's own docstring documents.
        base = days_ago(day_offset).replace(hour=1, minute=0, second=0, microsecond=0)
        return [make_row(base), make_row(base.replace(hour=23))], base

    def test_two_runs_on_one_day_are_reported(self, tmp_path):
        rows, base = self.two_runs_on(1)
        log = write_log(tmp_path / 'log.csv', rows)

        issues = analyze.analyze_city('somewhere', log, stale_days=3)

        assert [i['level'] for i in issues] == ['INFO'], issues
        assert 'Multiple runs on same day' in issues[0]['msg']
        assert base.strftime('%Y-%m-%d') in issues[0]['msg']

    def test_runs_on_distinct_days_are_not_reported(self, tmp_path):
        log = write_log(tmp_path / 'log.csv', daily_rows(3))

        assert analyze.analyze_city('somewhere', log, stale_days=3) == []

    def test_a_duplicate_older_than_the_window_is_not_reported(self, tmp_path):
        # Only the last 30 rows are considered, so an old overlap does not stay on the report forever.
        rows, _ = self.two_runs_on(40)
        log = write_log(tmp_path / 'log.csv', rows + daily_rows(30))

        assert not [i for i in analyze.analyze_city('somewhere', log, stale_days=3)
                    if 'Multiple runs' in i['msg']]


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
