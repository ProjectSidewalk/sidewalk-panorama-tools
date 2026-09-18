"""Tests for scrape_queue.py — the serialised nightly driver that replaces 53 crontab slots (#101).

The queue's whole job is process orchestration, so most of these drive it for real: a stand-in runner script
is written into tmp_path and the queue spawns it exactly as it would spawn DownloadRunner.py. That stand-in
journals every invocation, which is what lets the ordering, serialisation, budget and pass-through claims be
assertions about observed behaviour rather than about the argv the queue happened to build.

The three properties that are cheap to break and expensive to notice:
  - the ordering guarantee (one city at a time, in a knowable order),
  - the lock (nothing here had one before, and a lock that outlives a crash is worse than none),
  - the exit code (cron mails on nonzero, so a fleet that stops completing has to make it nonzero).
"""

import http.client
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

import scrape_queue
from conftest import posix_only

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# scrape_queue.main() adds a handler to the root logger and installs a SIGTERM handler, both process-wide.
# conftest's autouse _isolate_process_state snapshots and restores exactly that around every test in the
# suite, so this module does not carry its own copy - which is the point of it having been lifted there.


# --- Stand-in runner ---------------------------------------------------------------------------------------
#
# Behaves like DownloadRunner from the queue's point of view: takes <fqdn> <storage> plus flags, and exits.
# Everything it does is driven by the environment so one script covers every scenario.

FAKE_RUNNER = textwrap.dedent('''
    import json, os, signal, sys, time

    journal = os.environ['QUEUE_TEST_JOURNAL']
    city = os.path.basename(sys.argv[2])

    def note(event):
        with open(journal, 'a') as f:
            f.write('%s %s %s\\n' % (event, city, ' '.join(sys.argv[1:])))

    note('START')
    if os.environ.get('QUEUE_TEST_IGNORE_SIGTERM') and hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(float(os.environ.get('QUEUE_TEST_SLEEP', '0')))

    # The run summary, exactly as DownloadRunner writes it (--run-summary-file). QUEUE_TEST_STOP names the
    # phase that stopped on its budget - '' for a run that worked through its whole list. Which cities are
    # still working is driven from HERE rather than from how long this process took, because that is the
    # real contract: the queue must not be able to tell them apart by timing.
    #
    # The stop is reported on a city's FIRST run only, and it is what makes these tests terminate: a city
    # that reported work every time would be re-run until the window closed, so the test would take the
    # whole window in real seconds and its "was it re-run?" assertion would be racing pass 1 against the
    # clock. Reporting once means the passes stop because the work ran out, which is also the behaviour
    # worth pinning.
    if '--run-summary-file' in sys.argv:
        target = sys.argv[sys.argv.index('--run-summary-file') + 1]
        stop = os.environ.get('QUEUE_TEST_STOP_%s' % city.replace('-', '_').upper(),
                              os.environ.get('QUEUE_TEST_STOP', ''))
        first_run_marker = os.path.join(os.path.dirname(journal), 'ran-%s' % city)
        if os.path.exists(first_run_marker) and not os.environ.get('QUEUE_TEST_STOP_EVERY_RUN'):
            stop = ''
        open(first_run_marker, 'a').close()
        summary = {'image_stop': None, 'depth_stop': None}
        if stop:
            summary['%s_stop' % stop.split(':')[0]] = stop.split(':')[1]
        with open(target, 'w') as f:
            json.dump(summary, f)

    note('END')
    sys.exit(int(os.environ.get('QUEUE_TEST_EXIT', '0')))
''')


@pytest.fixture
def fake_runner(tmp_path):
    path = tmp_path / 'fake_runner.py'
    path.write_text(FAKE_RUNNER)
    return str(path)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / 'journal.txt'
    monkeypatch.setenv('QUEUE_TEST_JOURNAL', str(path))

    def read():
        return path.read_text().splitlines() if path.exists() else []

    return SimpleNamespace(path=str(path), read=read)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Every main() run now ends with one GET to a manifest host (#130), and the manifests here name
    `.invalid` hosts. The suite is network-free, so the one socket-touching seam is replaced with a refusal
    for every test - which also means every existing main() test exercises the real "no host answered"
    path, not a bypass. Tests about the check install their own roster over this."""
    def refuse(url, timeout):
        raise urllib.error.URLError('no network in tests')

    monkeypatch.setattr(scrape_queue, '_open_url', refuse)


@pytest.fixture
def fleet_in_step(monkeypatch):
    """A fleet whose roster lists exactly the manifest's enabled cities, all public, so the cross-check
    (#130) passes and a test about something else can still assert a clean exit 0. Explicit in the
    signature rather than folded into the autouse stub: a test that says nothing about the fleet gets the
    "no host answered" night, which is the one every other assertion has to survive."""
    remembered = []
    real_read = scrape_queue.read_city_list

    def read_city_list(path, disabled=None):
        remembered[:] = real_read(path, disabled=disabled)
        return list(remembered)

    def open_url(url, timeout):
        return roster_body(*[roster_entry(c.city_id, url='https://' + c.fqdn) for c in remembered])

    monkeypatch.setattr(scrape_queue, 'read_city_list', read_city_list)
    monkeypatch.setattr(scrape_queue, '_open_url', open_url)


def write_manifest(tmp_path, rows, name='cities.csv', header='city_id,fqdn'):
    path = tmp_path / name
    path.write_text('\n'.join([header] + list(rows)) + '\n')
    return str(path)


def three_cities(tmp_path):
    return write_manifest(tmp_path, ['alpha-aa,sidewalk-alpha.invalid',
                                     'bravo-bb,sidewalk-bravo.invalid',
                                     'charlie-cc,sidewalk-charlie.invalid'])


# --- The manifest ------------------------------------------------------------------------------------------

class TestTheManifestIsReadStrictly:
    """This one file decides which fifty-odd cities are scraped tonight. Every failure mode of a lenient
    parse is a fleet that quietly stops doing part of its job, so all of them raise instead."""

    def test_rows_are_read_in_file_order(self, tmp_path):
        cities = scrape_queue.read_city_list(three_cities(tmp_path))
        assert [c.city_id for c in cities] == ['alpha-aa', 'bravo-bb', 'charlie-cc']
        assert cities[0].fqdn == 'sidewalk-alpha.invalid'

    def test_a_missing_column_is_named_rather_than_read_as_blank(self, tmp_path):
        """The guard fetch_pano_ids_csv already has (#72): without it every row's city_id reads as blank,
        every city is skipped, and the queue exits 0 having scraped nothing."""
        path = write_manifest(tmp_path, ['alpha-aa,sidewalk-alpha.invalid'], header='city,fqdn')
        with pytest.raises(ValueError, match='city_id'):
            scrape_queue.read_city_list(path)

    def test_a_city_with_no_fqdn_is_refused(self, tmp_path):
        """There is no rule that derives one - seattle-wa is served by sidewalk-sea - so a blank cannot be
        filled in and must not be run as an empty hostname."""
        path = write_manifest(tmp_path, ['alpha-aa,sidewalk-alpha.invalid', 'bravo-bb,'])
        with pytest.raises(ValueError, match='bravo-bb'):
            scrape_queue.read_city_list(path)

    def test_a_duplicate_city_is_refused(self, tmp_path):
        """A city listed twice is scraped twice in a night. Because the two runs never overlap, every log
        involved looks perfectly healthy - there is nothing downstream that would ever report it."""
        path = write_manifest(tmp_path, ['alpha-aa,sidewalk-alpha.invalid', 'alpha-aa,sidewalk-alpha.invalid'])
        with pytest.raises(ValueError, match='more than once'):
            scrape_queue.read_city_list(path)

    def test_a_hashed_row_is_skipped(self, tmp_path):
        """Replaces the affordance the queue takes away: commenting out one city's crontab line."""
        path = write_manifest(tmp_path, ['alpha-aa,sidewalk-alpha.invalid',
                                         '#bravo-bb,sidewalk-bravo.invalid',
                                         'charlie-cc,sidewalk-charlie.invalid'])
        assert [c.city_id for c in scrape_queue.read_city_list(path)] == ['alpha-aa', 'charlie-cc']

    def test_an_empty_manifest_is_refused(self, tmp_path):
        """Exiting 0 having run nothing is indistinguishable from a healthy quiet night."""
        path = write_manifest(tmp_path, ['#everything,is-commented-out.invalid'])
        with pytest.raises(ValueError, match='no cities'):
            scrape_queue.read_city_list(path)

    def test_a_byte_order_mark_does_not_break_the_header(self, tmp_path):
        """A manifest edited in Excel carries a BOM, which would otherwise glue itself to 'city_id' and fire
        the column guard on a perfectly good file."""
        path = tmp_path / 'bom.csv'
        path.write_bytes(b'\xef\xbb\xbfcity_id,fqdn\nalpha-aa,sidewalk-alpha.invalid\n')
        assert [c.city_id for c in scrape_queue.read_city_list(str(path))] == ['alpha-aa']


class TestTheCommittedExampleManifestIsUsable:

    def test_it_parses(self):
        cities = scrape_queue.read_city_list(os.path.join(REPO_ROOT, 'samples', 'scrape_queue_cities.csv'))
        assert cities, 'the example must contain at least one runnable city'
        assert all(c.fqdn.endswith('.cs.washington.edu') for c in cities), cities

    def test_its_cities_are_real_ones(self):
        """An example nobody can check against reality drifts into a plausible-looking fiction. Every
        city_id in it must be a city the log analyzer also knows about."""
        import csv
        with open(os.path.join(REPO_ROOT, 'log_analyzer', 'cities.csv'), newline='') as f:
            known = {row['city_id'] for row in csv.DictReader(f)}
        sample = scrape_queue.read_city_list(os.path.join(REPO_ROOT, 'samples', 'scrape_queue_cities.csv'))
        assert {c.city_id for c in sample} <= known, \
            'example manifest names cities that are not in log_analyzer/cities.csv'


# --- Ordering ----------------------------------------------------------------------------------------------

class TestTheOrderTheQueueRunsIn:

    def cities(self, n=5):
        return [scrape_queue.City('city-%d' % i, 'host-%d.invalid' % i) for i in range(n)]

    def test_without_rotation_the_manifest_order_is_kept(self):
        cities = self.cities()
        assert scrape_queue.plan_order(cities, rotation_ordinal=None) == cities

    def test_rotation_moves_the_starting_point_by_one_each_day(self):
        cities = self.cities()
        assert [c.city_id for c in scrape_queue.plan_order(cities, rotation_ordinal=0)][0] == 'city-0'
        assert [c.city_id for c in scrape_queue.plan_order(cities, rotation_ordinal=1)][0] == 'city-1'

    def test_rotation_is_a_rotation_and_never_drops_or_repeats_a_city(self):
        """The failure that would matter: an off-by-one that silently loses tonight's first city."""
        cities = self.cities()
        for day in range(13):
            order = scrape_queue.plan_order(cities, rotation_ordinal=day)
            assert sorted(order) == sorted(cities), 'day %d changed the set of cities' % day

    def test_over_a_full_cycle_every_city_leads_once(self):
        """Why rotation exists. Truncation always lands on the tail, so a fixed order means the same cities
        lose every time the window closes early - and they are the ones nobody is watching."""
        cities = self.cities()
        leaders = {scrape_queue.plan_order(cities, rotation_ordinal=d)[0].city_id for d in range(len(cities))}
        assert leaders == {c.city_id for c in cities}

    def test_two_runs_on_one_day_agree(self):
        """Keyed on the date ordinal, not on a random seed or the clock, so an operator can reproduce any
        night's order and a retry does not reshuffle the queue."""
        cities = self.cities()
        assert scrape_queue.plan_order(cities, rotation_ordinal=7) == \
               scrape_queue.plan_order(cities, rotation_ordinal=7)

    def test_only_narrows_to_the_named_cities_in_the_order_given(self):
        cities = self.cities()
        order = scrape_queue.plan_order(cities, only=['city-3', 'city-1'], rotation_ordinal=99)
        assert [c.city_id for c in order] == ['city-3', 'city-1'], \
            'a single-city re-run must not depend on what day it is run on'

    def test_only_with_an_unknown_city_is_refused(self):
        """A typo would otherwise run nothing and exit like a completed queue."""
        with pytest.raises(ValueError, match='city-99'):
            scrape_queue.plan_order(self.cities(), only=['city-99'])

    def test_an_empty_city_list_does_not_divide_by_zero(self):
        assert scrape_queue.plan_order([], rotation_ordinal=5) == []


# --- Budgets -----------------------------------------------------------------------------------------------

class TestTheTwoBudgetsCompose:
    """Passing either budget alone has a distinct failure. The city cap alone lets the last city of the night
    run an hour past the window; the remaining window alone lets the FIRST city eat the whole night, which is
    the head-of-line problem serialising introduces. So the city gets the smaller of the two."""

    def test_the_smaller_of_the_two_wins(self):
        assert scrape_queue._city_budget(60, 20) == 20
        assert scrape_queue._city_budget(60, 90) == 60

    def test_either_alone_is_used(self):
        assert scrape_queue._city_budget(60, None) == 60
        assert scrape_queue._city_budget(None, 20) == 20

    def test_neither_means_no_budget(self):
        assert scrape_queue._city_budget(None, None) is None

    @pytest.mark.parametrize('bad', ['0', '-5', 'nan', 'inf', 'abc'])
    def test_a_budget_that_would_misbehave_quietly_fails_at_parse_time(self, bad):
        """DownloadRunner's own _reservation_minutes exists for this (#52): 0 skips every city while exiting
        like a completed run, and nan compares false against everything so no budget is ever enforced."""
        with pytest.raises(SystemExit) as exc:
            scrape_queue.build_parser().parse_args(['--cities', 'x', '--store-root', 'y', '--max-runtime', bad])
        assert exc.value.code == 2

    def test_a_valid_fractional_budget_is_accepted(self):
        args = scrape_queue.build_parser().parse_args(
            ['--cities', 'x', '--store-root', 'y', '--max-runtime', '0.5'])
        assert args.max_runtime == 0.5


class TestTheCommandBuiltForOneCity:

    def city(self):
        return scrape_queue.City('alpha-aa', 'sidewalk-alpha.invalid')

    def test_the_store_directory_is_the_city_id_under_the_root(self):
        cmd = scrape_queue.build_command(self.city(), '/store', 'py', 'runner.py', None, [])
        assert cmd == ['py', 'runner.py', 'sidewalk-alpha.invalid', os.path.join('/store', 'alpha-aa')]

    def test_a_budget_is_passed_as_the_runners_own_max_runtime(self):
        """Enforced by the runner, not only by killing it: a runner that stops itself writes its log.csv row
        and leaves a clean ledger. The kill is the backstop, not the mechanism."""
        cmd = scrape_queue.build_command(self.city(), '/store', 'py', 'runner.py', 90, [])
        assert cmd[-2:] == ['--max-runtime', '90']

    def test_pass_through_arguments_come_last(self):
        cmd = scrape_queue.build_command(self.city(), '/store', 'py', 'runner.py', 90,
                                         ['--all-panos', '--skip-depth'])
        assert cmd[-2:] == ['--all-panos', '--skip-depth']

    def test_no_budget_means_no_max_runtime_flag(self):
        cmd = scrape_queue.build_command(self.city(), '/store', 'py', 'runner.py', None, ['--all-panos'])
        assert '--max-runtime' not in cmd

    def test_the_queues_summary_path_goes_before_the_pass_through_so_an_operators_own_wins(self):
        """argparse is last-one-wins, so the queue's --run-summary-file has to precede `--` arguments for an
        operator who passes their own to get it. A mutant that appended the queue's flag last survived the
        suite until this pin existed (#126 review)."""
        cmd = scrape_queue.build_command(self.city(), '/store', 'py', 'runner.py', 12,
                                         ['--run-summary-file', '/mine.json'],
                                         run_summary_path='/queue.json')

        assert cmd[-2:] == ['--run-summary-file', '/mine.json']
        assert cmd.index('/queue.json') < cmd.index('/mine.json')


# --- One city ----------------------------------------------------------------------------------------------

class TestRunningOneCity:

    def run(self, city_id, fake_runner, tmp_path, budget=None, kill_grace=1.0, args=()):
        return scrape_queue.run_city(scrape_queue.City(city_id, 'host.invalid'), str(tmp_path / 'store'),
                                     sys.executable, fake_runner, budget, kill_grace, list(args))

    def test_a_clean_run_is_ok(self, fake_runner, journal, tmp_path):
        result = self.run('alpha-aa', fake_runner, tmp_path)
        assert (result.outcome, result.exit_code) == ('ok', 0)
        assert [line.split()[0] for line in journal.read()] == ['START', 'END']

    def test_a_failing_city_is_recorded_and_does_not_raise(self, fake_runner, journal, tmp_path, monkeypatch):
        """The fleet's availability must not depend on its worst member - the same reason nothing in the
        cropper's crop loop is fatal (#48). Recording it is what keeps it visible."""
        monkeypatch.setenv('QUEUE_TEST_EXIT', '1')
        result = self.run('alpha-aa', fake_runner, tmp_path)
        assert (result.outcome, result.exit_code) == ('failed', 1)

    def test_a_runner_whose_file_is_missing_is_a_failure(self, tmp_path):
        """Popen succeeds here - the interpreter starts and then cannot find the script - so this is the
        ordinary nonzero-exit path, not the spawn failure below. Both must be survivable, and they are
        different branches."""
        result = self.run('alpha-aa', str(tmp_path / 'no-such-runner.py'), tmp_path)
        assert result.outcome == 'failed'
        assert result.exit_code not in (0, None)

    def test_an_interpreter_that_cannot_be_spawned_is_a_failure_not_a_crash(self, tmp_path):
        """A missing interpreter raises OSError out of Popen itself, and it fails identically for all 53
        cities. Reporting it per city and letting the summary make the pattern obvious beats dying on the
        first one with a traceback and leaving 52 cities unscraped and unexplained."""
        result = scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                                       str(tmp_path / 'no-such-python'), 'runner.py', None, 1.0, [])
        assert (result.outcome, result.exit_code) == ('failed', None)
        assert result.seconds is not None, 'a city that never started still took some measurable time'

    def test_a_city_that_overruns_its_budget_is_stopped(self, fake_runner, journal, tmp_path, monkeypatch):
        """Serialising introduces head-of-line blocking: without this, one hung city holds every city behind
        it for the rest of the night, and the queue's window never closes."""
        monkeypatch.setenv('QUEUE_TEST_SLEEP', '60')
        started = time.monotonic()

        result = self.run('alpha-aa', fake_runner, tmp_path, budget=0.005, kill_grace=0.005)

        assert result.outcome == 'timed_out'
        assert time.monotonic() - started < 30, 'the queue waited far longer than the budget allowed'
        assert [line.split()[0] for line in journal.read()] == ['START'], 'the city should not have finished'

    @posix_only
    def test_a_city_that_ignores_sigterm_is_killed(self, fake_runner, journal, tmp_path, monkeypatch):
        """SIGTERM first, because DownloadRunner turns it into sys.exit(143) so its finally blocks run and
        the log.csv evidence row still lands (#49). But asking has to have a deadline, or a wedged process
        holds the queue open exactly as if nothing had been sent.

        POSIX only: on Windows terminate() is TerminateProcess, which cannot be ignored, so there is no
        escalation to test.
        """
        monkeypatch.setenv('QUEUE_TEST_SLEEP', '60')
        monkeypatch.setenv('QUEUE_TEST_IGNORE_SIGTERM', '1')
        monkeypatch.setattr(scrape_queue, 'TERM_TO_KILL_SECONDS', 1)
        started = time.monotonic()

        result = self.run('alpha-aa', fake_runner, tmp_path, budget=0.005, kill_grace=0.005)

        assert result.outcome == 'timed_out'
        assert time.monotonic() - started < 30

    def test_the_city_is_told_its_budget(self, fake_runner, journal, tmp_path):
        self.run('alpha-aa', fake_runner, tmp_path, budget=90)
        assert '--max-runtime 90' in journal.read()[0]


# --- The queue ---------------------------------------------------------------------------------------------

class FakeClock:
    """A monotonic clock the test advances by hand, injected in place of scrape_queue's `time`.

    Patching the real time module would be global; the queue reads only time.monotonic(), so a two-attribute
    stand-in covers it exactly.
    """

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class TestTheQueueRunsEveryCityInOrder:

    def cities(self, n=3):
        return [scrape_queue.City('city-%d' % i, 'host-%d.invalid' % i) for i in range(n)]

    def record_run(self, calls, minutes=0.0, clock=None):
        def run_one(city, store_root, python_exe, runner_path, budget, kill_grace, runner_args, env=None):
            calls.append((city.city_id, budget))
            if clock is not None:
                clock.now += minutes * 60.0
            return scrape_queue.CityResult(city.city_id, 'ok', 0, minutes * 60.0)
        return run_one

    def test_every_city_runs_when_there_is_no_window(self):
        calls = []
        results = scrape_queue.run_queue(self.cities(), '/store', 'py', 'r.py', [],
                                        run_one=self.record_run(calls))
        assert [c for c, _ in calls] == ['city-0', 'city-1', 'city-2']
        assert [r.outcome for r in results] == ['ok'] * 3

    def test_cities_the_window_did_not_reach_are_reported_not_silently_dropped(self, monkeypatch):
        """The condition the whole change exists to make visible. A queue that quietly completes 2 of 3
        cities every night is indistinguishable from a healthy one unless it says so."""
        clock = FakeClock()
        monkeypatch.setattr(scrape_queue, 'time', clock)
        calls = []

        results = scrape_queue.run_queue(self.cities(), '/store', 'py', 'r.py', [],
                                        max_runtime_minutes=10,
                                        run_one=self.record_run(calls, minutes=6, clock=clock))

        assert [c for c, _ in calls] == ['city-0', 'city-1'], 'city-2 must not be started'
        assert [r.outcome for r in results] == ['ok', 'ok', 'skipped_deadline']
        assert results[2].exit_code is None and results[2].seconds is None

    def test_the_window_gates_starting_a_city_and_never_interrupts_one(self, monkeypatch):
        """Same rule as the image phase's budget (#51): a partial pano or a torn ledger costs more than
        finishing late. city-1 starts with 4 minutes left and runs 6; that is a completed city, not a
        failure."""
        clock = FakeClock()
        monkeypatch.setattr(scrape_queue, 'time', clock)
        calls = []

        results = scrape_queue.run_queue(self.cities(), '/store', 'py', 'r.py', [],
                                        max_runtime_minutes=10,
                                        run_one=self.record_run(calls, minutes=6, clock=clock))

        assert results[1].outcome == 'ok'
        assert clock.now / 60.0 == 12, 'the second city was cut short'

    def test_a_citys_budget_is_clamped_by_what_is_left_of_the_window(self, monkeypatch):
        clock = FakeClock()
        monkeypatch.setattr(scrape_queue, 'time', clock)
        calls = []

        scrape_queue.run_queue(self.cities(), '/store', 'py', 'r.py', [],
                               max_runtime_minutes=10, city_max_runtime=8,
                               run_one=self.record_run(calls, minutes=6, clock=clock))

        assert calls[0][1] == 8, 'the first city gets its own cap, which is the smaller'
        assert calls[1][1] == 4, 'the second gets what is left of the window, which now is'


class TestTheExitCode:
    """cron mails on a nonzero exit, so this is the fleet's only unattended alarm."""

    def results(self, *outcomes):
        return [scrape_queue.CityResult('c%d' % i, o, 0 if o == 'ok' else 1, 1.0)
                for i, o in enumerate(outcomes)]

    def test_an_all_clear_night_is_zero(self):
        assert scrape_queue.exit_code_for(self.results('ok', 'ok')) == 0

    def test_a_failed_city_is_nonzero(self):
        assert scrape_queue.exit_code_for(self.results('ok', 'failed')) == 1

    def test_a_timed_out_city_is_nonzero(self):
        assert scrape_queue.exit_code_for(self.results('ok', 'timed_out')) == 1

    def test_a_city_the_window_did_not_reach_is_nonzero(self):
        """Deliberately not "expected, therefore fine". A fleet completing 40 of 53 cities every night is
        the exact silent failure #101 is about; if the truncation is accepted, the window is the wrong size."""
        assert scrape_queue.exit_code_for(self.results('ok', 'skipped_deadline')) == 1


class TestTheSummary:

    def test_it_names_every_city_that_did_not_simply_work(self):
        results = [scrape_queue.CityResult('alpha-aa', 'ok', 0, 60.0),
                   scrape_queue.CityResult('bravo-bb', 'failed', 1, 30.0),
                   scrape_queue.CityResult('charlie-cc', 'skipped_deadline', None, None)]

        text = scrape_queue.summarise(results, 1.5)

        assert 'bravo-bb' in text and 'charlie-cc' in text
        assert 'alpha-aa' not in text, 'a clean night should not need a screen of output'
        assert '1/3 cities ok' in text

    def test_the_headline_counts_cities_not_runs(self):
        """With extra passes (#43) a city can appear several times in the results; "N/M cities ok" must
        still be a per-city figure, or a night that re-ran one city five times reads as 8/8."""
        results = [scrape_queue.CityResult('alpha-aa', 'ok', 0, 720.0, 12.0, 1),
                   scrape_queue.CityResult('bravo-bb', 'ok', 0, 5.0, 12.0, 1),
                   scrape_queue.CityResult('alpha-aa', 'ok', 0, 720.0, 12.0, 2),
                   scrape_queue.CityResult('alpha-aa', 'ok', 0, 700.0, 12.0, 3)]

        text = scrape_queue.summarise(results, 36.0)

        assert '2/2 cities ok' in text
        assert 'pass 2: 1 cities re-run' in text and 'pass 3: 1 cities re-run' in text
        assert 'alpha-aa 12.0' in text

    def test_a_failure_in_a_later_pass_is_named_with_its_pass(self):
        results = [scrape_queue.CityResult('alpha-aa', 'ok', 0, 720.0, 12.0, 1),
                   scrape_queue.CityResult('alpha-aa', 'failed', 1, 3.0, 12.0, 2)]

        text = scrape_queue.summarise(results, 12.1)

        assert 'alpha-aa' in text and 'FAILED' in text and 'pass 2' in text

    def test_a_city_that_never_started_does_not_format_its_missing_numbers(self):
        """None is not zero. `'%.1f' % None` raises, on the last line of the run, after everything worked -
        the failure mode the studies keep rediscovering (reports/2026-08-11-mapillary-census.md)."""
        text = scrape_queue.summarise([scrape_queue.CityResult('a', 'skipped_deadline', None, None)], 0.0)
        assert 'SKIPPED_DEADLINE' in text


# --- The lock ----------------------------------------------------------------------------------------------

LOCK_HOLDER = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, %r)
    import scrape_queue
    with scrape_queue.exclusive_lock(sys.argv[1]):
        open(sys.argv[2], 'w').write('held')
        time.sleep(120)
''')


class TestOnlyOneQueueRunsAtATime:
    """There was no lock anywhere in this repo before #101. With 53 unsynchronised crontab slots, a slow run
    and the next slot could put two processes on one city's pano_id_log.csv, log.csv and scrape.log."""

    def test_a_second_acquisition_is_refused_while_the_first_is_held(self, tmp_path):
        lock = str(tmp_path / 'q.lock')
        with scrape_queue.exclusive_lock(lock):
            with pytest.raises(scrape_queue.QueueLocked):
                with scrape_queue.exclusive_lock(lock):
                    pass

    def test_the_lock_is_released_when_the_block_exits(self, tmp_path):
        lock = str(tmp_path / 'q.lock')
        with scrape_queue.exclusive_lock(lock):
            pass
        with scrape_queue.exclusive_lock(lock):
            pass  # must not raise

    def test_the_lock_is_released_when_the_holder_exits_uncleanly(self, tmp_path):
        """The property the whole choice of API rests on. An O_EXCL lock file - the obvious implementation -
        survives the crash that created it, so one killed run stops the entire fleet every night after,
        which is far worse than the overlap the lock prevents.
        """
        lock = str(tmp_path / 'q.lock')
        marker = tmp_path / 'held'
        holder = tmp_path / 'holder.py'
        holder.write_text(LOCK_HOLDER % REPO_ROOT)
        proc = subprocess.Popen([sys.executable, str(holder), lock, str(marker)])
        try:
            deadline = time.monotonic() + 30
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.exists(), 'the holder never took the lock'
            with pytest.raises(scrape_queue.QueueLocked):
                with scrape_queue.exclusive_lock(lock):
                    pass
        finally:
            proc.kill()
            proc.wait()
        # The OS releases an advisory lock when its holder dies. Nothing cleans up after it, on purpose.
        with scrape_queue.exclusive_lock(lock):
            pass

    def test_the_lock_file_records_the_holders_pid(self, tmp_path):
        """It is left behind rather than unlinked - unlinking races another process that already opened the
        same path - so it may as well say who to look for.

        Read after release, not during: msvcrt's lock is MANDATORY, so on Windows the file cannot even be
        opened for reading while it is held, where flock is advisory and it can. The claim being pinned is
        that the pid survives in the file, which holds on both.
        """
        lock = tmp_path / 'q.lock'
        with scrape_queue.exclusive_lock(str(lock)):
            pass
        assert lock.read_text().strip() == str(os.getpid())

    @posix_only
    def test_the_pid_is_there_while_the_lock_is_still_held(self, tmp_path):
        """The case it is actually for: an operator finding the lock file while something holds it. Only
        assertable where the lock is advisory, i.e. the platform production runs on."""
        lock = tmp_path / 'q.lock'
        with scrape_queue.exclusive_lock(str(lock)):
            assert lock.read_text().strip() == str(os.getpid())

    def test_the_windows_lock_api_is_driven_correctly(self, monkeypatch, tmp_path):
        """Exercises the arm of _try_lock this platform does not take, by substituting a msvcrt-shaped
        module. Without this the untaken branch is only ever checked by whichever OS CI happens to run.
        """
        calls = []

        class FakeMsvcrt:
            LK_NBLCK = 3

            def locking(self, fd, mode, nbytes):
                calls.append((mode, nbytes))

        monkeypatch.setattr(scrape_queue, '_lock_module', lambda: FakeMsvcrt())
        with scrape_queue.exclusive_lock(str(tmp_path / 'q.lock')):
            pass
        assert calls == [(FakeMsvcrt.LK_NBLCK, 1)], 'must be a NON-blocking single-byte lock'

    def test_a_refusal_from_the_platform_becomes_queuelocked(self, monkeypatch, tmp_path):
        """Whatever OSError the platform raises has to arrive as the one exception main() handles - it is
        the difference between exit 3 with an explanation and an unhandled traceback out of cron."""
        class Refusing:
            LK_NBLCK = 3

            def locking(self, fd, mode, nbytes):
                raise OSError(11, 'Resource temporarily unavailable')

        monkeypatch.setattr(scrape_queue, '_lock_module', lambda: Refusing())
        with pytest.raises(scrape_queue.QueueLocked, match='Resource temporarily unavailable'):
            with scrape_queue.exclusive_lock(str(tmp_path / 'q.lock')):
                pass

    def test_the_default_lock_is_on_local_disk_not_the_store(self):
        """The store is a network mount whose advisory-lock semantics are not guaranteed, and the overlap
        being prevented is between runs on this host."""
        assert scrape_queue.default_lock_path().startswith(
            os.path.realpath(__import__('tempfile').gettempdir())) or \
            os.path.isabs(scrape_queue.default_lock_path())


# --- main() ------------------------------------------------------------------------------------------------

def run_main(tmp_path, manifest, fake_runner, *extra, store=None):
    """Drive main() with a lock private to this test.

    The production default is one lock per HOST, which is right for production and wrong here: without this
    two pytest processes on one machine - a second run in another terminal, pytest-xdist, a CI matrix sharing
    a runner - contend for it, and the loser exits 3 having run nothing. That failure looks like a bug in
    whatever the test was actually asserting. Passed first so a test that cares about the lock can override
    it by passing its own --lock in `extra`.
    """
    store = store or str(tmp_path / 'store')
    return scrape_queue.main(['--lock', str(tmp_path / 'test.lock'),
                              '--cities', manifest, '--store-root', store,
                              '--runner', fake_runner, '--python', sys.executable, *extra])


class TestTheWholeQueueEndToEnd:

    def test_cities_run_one_at_a_time_in_order(self, tmp_path, fake_runner, journal, fleet_in_step):
        """The ordering guarantee, observed rather than inferred: a parallel implementation interleaves the
        journal, and no other assertion in this file would notice."""
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate')

        assert code == 0
        assert [tuple(line.split()[:2]) for line in journal.read()] == [
            ('START', 'alpha-aa'), ('END', 'alpha-aa'),
            ('START', 'bravo-bb'), ('END', 'bravo-bb'),
            ('START', 'charlie-cc'), ('END', 'charlie-cc'),
        ]

    def test_each_city_is_scraped_into_its_own_directory(self, tmp_path, fake_runner, journal):
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate')
        starts = [line for line in journal.read() if line.startswith('START')]
        assert 'sidewalk-alpha.invalid' in starts[0]
        assert os.path.join('store', 'alpha-aa') in starts[0]

    def test_arguments_after_the_separator_reach_every_city(self, tmp_path, fake_runner, journal):
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate', '--',
                 '--all-panos', '--skip-depth')
        starts = [line for line in journal.read() if line.startswith('START')]
        assert len(starts) == 3
        assert all('--all-panos --skip-depth' in line for line in starts), starts

    def test_one_failing_city_does_not_stop_the_ones_behind_it(self, tmp_path, fake_runner, journal,
                                                               monkeypatch):
        monkeypatch.setenv('QUEUE_TEST_EXIT', '1')
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate')
        assert code == 1
        assert len([line for line in journal.read() if line.startswith('START')]) == 3

    def test_the_queue_log_lands_on_the_store_and_not_the_cwd(self, tmp_path, fake_runner, journal,
                                                              monkeypatch):
        """Same reasoning as scrape.log (#49): under cron the CWD is wherever the process happened to start.
        This log answers "what ran last night, in what order" - which no per-city log can, because none of
        them can see the ring."""
        elsewhere = tmp_path / 'cwd'
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate')

        assert (tmp_path / 'store' / 'scrape_queue.log').exists()
        assert not (elsewhere / 'scrape_queue.log').exists()
        assert 'alpha-aa' in (tmp_path / 'store' / 'scrape_queue.log').read_text()

    def test_a_second_queue_exits_three_without_running_anything(self, tmp_path, fake_runner, journal):
        """The one condition 53 unsynchronised crontab slots could not detect."""
        lock = str(tmp_path / 'q.lock')
        with scrape_queue.exclusive_lock(lock):
            code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--lock', lock)

        assert code == 3
        assert journal.read() == [], 'nothing may run while another queue holds the lock'

    def test_an_unreadable_manifest_exits_two_and_says_so(self, tmp_path, fake_runner, capsys):
        code = run_main(tmp_path, str(tmp_path / 'absent.csv'), fake_runner)
        assert code == 2
        assert 'city list' in capsys.readouterr().err

    def test_only_runs_just_that_city(self, tmp_path, fake_runner, journal, fleet_in_step):
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--only', 'bravo-bb')
        assert code == 0
        assert [tuple(line.split()[:2]) for line in journal.read()] == [
            ('START', 'bravo-bb'), ('END', 'bravo-bb')]

    def test_a_window_with_no_per_city_cap_warns(self, tmp_path, fake_runner, journal, capsys):
        """The window only gates STARTING a city, so without a per-city cap it is advisory - one hung city
        holds it open indefinitely. Same discipline as --min-depth-runtime's warning: say so when the
        combination cannot do what it looks like it does."""
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate', '--max-runtime', '60')
        # The flag, not just 'WARNING': every run now also prints the manifest check's own WARNING when no
        # host answers (#130), which is the case under this suite's network stub, so the bare word would
        # be true with this warning deleted.
        assert '--city-max-runtime' in capsys.readouterr().out


def plan_lines(out):
    """The city_ids in the order a --dry-run plan lists them, read off its numbered lines."""
    ids = []
    for line in out.splitlines():
        # "  1. <python> <runner> <fqdn> <store-root>/<city_id> ..." - the store directory is argv[4].
        if re.match(r'\s+\d+\. ', line):
            ids.append(os.path.basename(line.split()[4]))
    return ids


class TestDryRun:

    def test_it_prints_the_plan_and_runs_nothing(self, tmp_path, fake_runner, journal, capsys):
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate', '--dry-run',
                        '--', '--all-panos')

        out = capsys.readouterr().out
        assert code == 0
        assert journal.read() == []
        # The numbered plan lines, not a count of 'sidewalk-': the manifest check's WARNING names the hosts it
        # tried (#130), and a checkout path can carry the word too (this repo's does, on one machine).
        assert plan_lines(out) == ['alpha-aa', 'bravo-bb', 'charlie-cc'], out
        assert '--all-panos' in out

    def test_it_takes_no_lock_and_creates_no_store(self, tmp_path, fake_runner, journal):
        """A dry run is for reading, including while tonight's queue is running."""
        lock = str(tmp_path / 'q.lock')
        with scrape_queue.exclusive_lock(lock):
            code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--dry-run', '--lock', lock)
        assert code == 0
        assert not (tmp_path / 'store').exists()

    def test_it_shows_the_order_rotation_chose(self, tmp_path, fake_runner, capsys):
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--dry-run')
        listed = plan_lines(capsys.readouterr().out)
        assert len(listed) == 3
        assert sorted(listed) == ['alpha-aa', 'bravo-bb', 'charlie-cc']


class _FakeProc:
    """A Popen-shaped object whose wait() times out a set number of times before returning.

    Lets the timeout -> SIGTERM -> kill escalation be driven deterministically on any platform, with no
    sleeps and no unkillable child. The real-process versions above stay as the integration check; this is
    what pins the sequence.
    """

    def __init__(self, timeouts, exit_code=143):
        self._timeouts = timeouts
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        if self._timeouts:
            self._timeouts -= 1
            raise subprocess.TimeoutExpired('cmd', timeout)
        return self._exit_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class TestStoppingACityThatWillNotStop:

    def run_with(self, monkeypatch, tmp_path, proc):
        monkeypatch.setattr(scrape_queue.subprocess, 'Popen', lambda *a, **k: proc)
        monkeypatch.setattr(scrape_queue, 'TERM_TO_KILL_SECONDS', 0)
        return scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                                     'py', 'runner.py', 1.0, 1.0, [])

    def test_a_city_over_its_budget_is_asked_before_it_is_taken(self, monkeypatch, tmp_path):
        """SIGTERM first, because DownloadRunner turns it into sys.exit(143) so its finally blocks run and
        the log.csv evidence row still lands (#49). Killing outright would trade a few seconds' wait for a
        missing row on the one night someone will want it."""
        proc = _FakeProc(timeouts=1)

        result = self.run_with(monkeypatch, tmp_path, proc)

        assert proc.terminated and not proc.killed
        assert (result.outcome, result.exit_code) == ('timed_out', 143)

    def test_a_city_that_ignores_the_request_is_killed(self, monkeypatch, tmp_path):
        """Asking has to have a deadline, or a wedged process holds every city behind it for the rest of the
        night exactly as if nothing had been sent."""
        proc = _FakeProc(timeouts=2, exit_code=-9)

        result = self.run_with(monkeypatch, tmp_path, proc)

        assert proc.terminated and proc.killed
        assert result.outcome == 'timed_out'

    def test_a_city_that_finishes_in_time_is_neither_signalled_nor_killed(self, monkeypatch, tmp_path):
        """Guard the guard: the escalation must be reachable only through the timeout, not on every run."""
        proc = _FakeProc(timeouts=0, exit_code=0)

        result = self.run_with(monkeypatch, tmp_path, proc)

        assert not proc.terminated and not proc.killed
        assert (result.outcome, result.exit_code) == ('ok', 0)


class TestThePassThroughSeparator:
    """argparse consumes the '--' it uses to end its own options, and that handling has changed across 3.x
    releases. A stray separator reaching DownloadRunner is an argparse error that fails the city."""

    def test_a_leading_separator_is_dropped(self):
        assert scrape_queue.strip_separator(['--', '--all-panos']) == ['--all-panos']

    def test_arguments_without_one_are_untouched(self):
        assert scrape_queue.strip_separator(['--all-panos', '--skip-depth']) == ['--all-panos', '--skip-depth']

    def test_only_the_leading_one_is_dropped(self):
        """A '--' further along belongs to whatever the runner does with it, not to us."""
        assert scrape_queue.strip_separator(['--', '-c', '--', 'x']) == ['-c', '--', 'x']

    def test_nothing_at_all_is_fine(self):
        assert scrape_queue.strip_separator([]) == []

    def test_no_separator_reaches_the_runner_whatever_argparse_did(self, tmp_path, fake_runner, journal):
        """The claim that actually matters, stated end to end rather than about the helper."""
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate', '--', '--all-panos')
        starts = [line for line in journal.read() if line.startswith('START')]
        assert starts and all(' -- ' not in line and not line.endswith(' --') for line in starts), starts


class TestTheQueueLogIsEvidenceNotCargo:

    def test_a_log_that_cannot_be_opened_does_not_take_the_queue_down(self, tmp_path, capsys):
        """Same rule as scrape.log (#49): losing the log must not lose the night's scraping. A directory
        where the log file belongs is the shape an operator actually produces."""
        blocked = tmp_path / 'scrape_queue.log'
        blocked.mkdir()

        scrape_queue.configure_logging(str(blocked))

        logging.warning('still logging')
        assert 'Could not open' in capsys.readouterr().err

    def test_the_posix_lock_api_is_driven_correctly(self, monkeypatch, tmp_path):
        """The mirror of the msvcrt test above: substitute an fcntl-shaped module so the arm this platform
        does not take is still exercised. Between the two, both arms are covered on both platforms rather
        than each being checked only by whichever OS happens to run CI.
        """
        calls = []

        class FakeFcntl:
            LOCK_EX = 2
            LOCK_NB = 4

            def flock(self, fd, operation):
                calls.append(operation)

        monkeypatch.setattr(scrape_queue, '_lock_module', lambda: FakeFcntl())
        with scrape_queue.exclusive_lock(str(tmp_path / 'q.lock')):
            pass
        # Exclusive and non-blocking: blocking would silently queue behind last night's run instead of
        # reporting it, and a shared lock would not exclude anything at all.
        assert calls == [FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB]


class TestAStoppedQueueDoesNotOrphanTheCityItIsRunning:
    """The one overlap the lock cannot catch.

    A cron timeout wrapper's SIGTERM (or an operator's kill) unwinds the queue, which releases the lock -
    but the DownloadRunner it was supervising is a separate process and keeps scraping into the store.
    Tomorrow's queue then starts alongside it, two processes on one city's pano_id_log.csv, which is exactly
    what the lock exists to prevent, arrived at through the door the lock cannot watch.
    """

    def stop_during(self, monkeypatch, tmp_path, error):
        class _InterruptedProc(_FakeProc):
            def wait(self, timeout=None):
                if not self.terminated:
                    raise error
                return 143

        proc = _InterruptedProc(timeouts=0)
        monkeypatch.setattr(scrape_queue.subprocess, 'Popen', lambda *a, **k: proc)
        monkeypatch.setattr(scrape_queue, 'TERM_TO_KILL_SECONDS', 0)
        return proc

    def test_a_sigterm_to_the_queue_stops_the_city_too(self, monkeypatch, tmp_path):
        """SystemExit is what main()'s SIGTERM handler raises, and it is not an Exception - so a bare
        `except Exception` here would let the child outlive us."""
        proc = self.stop_during(monkeypatch, tmp_path, SystemExit(143))

        with pytest.raises(SystemExit):
            scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                                  'py', 'runner.py', None, 1.0, [])

        assert proc.terminated, 'the city was left running after the queue was told to stop'

    def test_a_keyboard_interrupt_stops_the_city_too(self, monkeypatch, tmp_path):
        """The interactive form of the same stop, and also not an Exception."""
        proc = self.stop_during(monkeypatch, tmp_path, KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                                  'py', 'runner.py', None, 1.0, [])

        assert proc.terminated

    def test_the_stop_is_not_swallowed(self, monkeypatch, tmp_path):
        """Stopping the child must not turn the queue's own stop into a normal result - that would keep the
        queue running through a SIGTERM, one city at a time, for the rest of the night."""
        proc = self.stop_during(monkeypatch, tmp_path, SystemExit(143))
        with pytest.raises(SystemExit) as exc:
            scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                                  'py', 'runner.py', None, 1.0, [])
        assert exc.value.code == 143


# --- Extra passes: the window is spent on the cities that still have work (#43) ----------------------------
#
# Measured on the production store after the depth backfill's first three nights: the queue used 477 of its
# 690-minute window, because 13 small cities were already complete and exited in seconds while every city
# with a backlog was capped at its 12-minute slot - and the five largest cities were 240-463 nights out. Pass
# 1 is unchanged (every city gets its guaranteed slot). Then, while a full slot of window remains, the cities
# whose previous run STOPPED ON ITS BUDGET are run again, each with the larger of a slot and an equal share
# of what is left. Who has work is read from tonight's own pass 1, so no cross-night state is needed.

def result(city_id, outcome='ok', seconds=0.0, budget=None, pass_number=1, exit_code=None,
           stop_reasons=None):
    if exit_code is None:
        exit_code = 0 if outcome == 'ok' else 1
    return scrape_queue.CityResult(city_id, outcome, exit_code, seconds, budget, pass_number, stop_reasons)


def stops(image=None, depth=None):
    """A run summary as DownloadRunner writes it: what stopped each phase, or None if nothing did."""
    return {'image_stop': image, 'depth_stop': depth}


class TestWhichCitiesStillHaveWorkReportedByTheRunner:
    """The runner says why each phase stopped and the queue applies the re-run policy to that.

    Reading it from queue-measured elapsed time instead was wrong in both directions: the queue times the
    whole subprocess while DownloadRunner's budget clock starts after the pano-list fetch, so a COMPLETE
    city whose prologue outlasts its slot measured as "has work" forever, and a city whose image phase
    stopped on its --min-depth-runtime share while depth finished early measured as "finished" with a live
    image backlog."""

    def test_an_image_phase_stopped_on_budget_has_more_work(self):
        assert scrape_queue.stopped_on_budget(
            result('a', seconds=384.0, budget=12.0, stop_reasons=stops(image='max-runtime')))

    def test_the_image_backlog_case_the_elapsed_rule_got_wrong(self):
        """The false NEGATIVE. Image stopped on its 6-minute reserved share, depth then exhausted its own
        list, so the process exited at 6.4 of 12 minutes with an image backlog intact. Elapsed time said
        'finished'; the runner says the image phase stopped on its budget."""
        assert scrape_queue.stopped_on_budget(
            result('seattle-wa', seconds=6.4 * 60, budget=12.0, stop_reasons=stops(image='max-runtime')))

    def test_a_depth_phase_stopped_on_budget_has_more_work(self):
        assert scrape_queue.stopped_on_budget(
            result('a', seconds=740.0, budget=12.0, stop_reasons=stops(depth='max-runtime')))

    def test_a_city_that_finished_both_phases_has_no_work_however_long_it_took(self):
        """The false POSITIVE. A complete city whose /adminapi/panos prologue alone outlasts the slot exits
        having downloaded nothing; elapsed time re-ran it in every pass of every night, forever."""
        assert not scrape_queue.stopped_on_budget(
            result('chicago-il', seconds=12 * 60 + 5, budget=12.0, stop_reasons=stops()))

    @pytest.mark.parametrize('depth_stop', ['blocked', 'consecutive-failures', 'max-requests'])
    def test_a_depth_phase_stopped_for_any_other_reason_is_not_re_run(self, depth_stop):
        """Only a BUDGET stop means "more time would help". A blocked host stands down for six hours and a
        re-run just spends the slot rediscovering it; a tripped breaker would trip again; --max-depth-requests
        is a per-process cap the operator asked for, so re-running silently multiplies it."""
        assert not scrape_queue.stopped_on_budget(
            result('a', seconds=740.0, budget=12.0, stop_reasons=stops(depth=depth_stop)))

    def test_the_image_phase_wins_even_when_depth_stood_down(self):
        """The exact production shape: the latch stands depth down at zero requests while the image phase
        really did stop on its share. The city has work and the depth reason must not veto it."""
        assert scrape_queue.stopped_on_budget(
            result('a', seconds=380.0, budget=12.0,
                   stop_reasons=stops(image='max-runtime', depth='blocked')))

    @pytest.mark.parametrize('outcome', ['failed', 'timed_out', 'skipped_deadline'])
    def test_only_a_clean_run_qualifies(self, outcome):
        """A crash says nothing about work left and re-running it is a crash loop; a hung city was killed
        past its budget and would be killed again; a city the window never reached never started."""
        assert not scrape_queue.stopped_on_budget(
            result('a', outcome=outcome, seconds=720.0, budget=12.0,
                   stop_reasons=stops(image='max-runtime')))

    def test_no_result_at_all_is_no_work(self):
        assert not scrape_queue.stopped_on_budget(None)


class TestWhichCitiesStillHaveWorkWhenTheRunnerSaidNothing:
    """The fallback, for an 'ok' run that produced no summary: one whose summary could not be written, or
    one where an operator's own --run-summary-file after `--` displaced the queue's. (NOT an older
    DownloadRunner - that one refuses the flag and exits 2, so it is booked failed and never re-run; see
    test_a_runner_that_does_not_know_the_flag_is_a_failed_city_not_a_fallback.) It is the old elapsed-time
    rule, kept because it is at least a NECESSARY condition (a run that stopped on budget always measures
    at least its budget), and because losing the window entirely is worse than the wasted slot its false
    positives cost."""

    def test_a_run_that_reached_its_budget_has_more_work(self):
        assert scrape_queue.stopped_on_budget(result('a', seconds=720.0, budget=12.0))

    def test_a_run_that_finished_one_second_early_does_not(self):
        """The discrimination against a fraction: 0.9 x 12 min would re-run this city for nothing."""
        assert not scrape_queue.stopped_on_budget(result('a', seconds=719.0, budget=12.0))

    def test_a_run_with_no_budget_cannot_have_stopped_on_it(self):
        assert not scrape_queue.stopped_on_budget(result('a', seconds=720.0, budget=None))

    def test_an_empty_summary_is_not_a_missing_one(self):
        """A runner that reported "nothing stopped either phase" is authoritative; only the absence of a
        summary falls back. Without this the two paths collapse and the false positive returns."""
        assert not scrape_queue.stopped_on_budget(
            result('a', seconds=720.0, budget=12.0, stop_reasons=stops()))


class TestTheRunSummaryVocabularyIsTheRunnersOwn:
    """The queue compares against literal strings, so a rename in the runner would silently stop every
    re-run - the queue would just see an unfamiliar reason and treat every city as finished."""

    def test_the_budget_stop_string_is_the_one_gsv_writes(self):
        gsv = pytest.importorskip('downloaders.gsv')
        assert scrape_queue.STOP_MAX_RUNTIME == gsv.DEPTH_STOP_MAX_RUNTIME

    def test_every_other_depth_stop_reason_is_known_and_not_a_budget_stop(self):
        gsv = pytest.importorskip('downloaders.gsv')
        others = {gsv.DEPTH_STOP_BLOCKED, gsv.DEPTH_STOP_CONSECUTIVE_FAILURES, gsv.DEPTH_STOP_MAX_REQUESTS}
        assert scrape_queue.STOP_MAX_RUNTIME not in others
        for reason in others:
            assert not scrape_queue.stopped_on_budget(
                result('a', seconds=720.0, budget=12.0, stop_reasons=stops(depth=reason)))


class TestThePassBudget:

    def test_it_is_the_slot_when_the_share_is_smaller(self):
        assert scrape_queue._extra_pass_budget(12.0, remaining_minutes=213.0, cities_left=39) == 12.0

    def test_it_is_the_equal_share_when_that_is_larger(self):
        assert scrape_queue._extra_pass_budget(12.0, remaining_minutes=625.0, cities_left=5) == 125.0

    def test_the_last_city_of_a_pass_gets_everything_left(self):
        assert scrape_queue._extra_pass_budget(12.0, remaining_minutes=40.0, cities_left=1) == 40.0


class TestTheQueueSpendsTheWholeWindow:
    """run_queue's extra passes, driven in-process with a stand-in run_one and a hand-advanced clock."""

    def cities(self, n=3):
        return [scrape_queue.City('city-%d' % i, 'host-%d.invalid' % i) for i in range(n)]

    @staticmethod
    def scripted(calls, clock, minutes_by_city):
        """A run_one whose cities each have a fixed amount of WORK, in minutes, that carries across passes: a
        run stops at its budget with the rest of the work left over, as DownloadRunner does, or ends early
        when the work runs out. Records (city, budget, pass) per call."""
        work_left = dict(minutes_by_city)

        def run_one(city, store_root, python_exe, runner_path, budget, kill_grace, runner_args, env=None):
            left = work_left.get(city.city_id, 0.0)
            took = min(left, budget) if budget is not None else left
            work_left[city.city_id] = left - took
            calls.append((city.city_id, budget, len([c for c in calls if c[0] == city.city_id]) + 1))
            clock.now += took * 60.0
            return scrape_queue.CityResult(city.city_id, 'ok', 0, took * 60.0)
        return run_one

    def run(self, monkeypatch, minutes_by_city, window, slot, n=3, **kwargs):
        clock = FakeClock()
        monkeypatch.setattr(scrape_queue, 'time', clock)
        calls = []
        results = scrape_queue.run_queue(self.cities(n), '/store', 'py', 'r.py', [],
                                         max_runtime_minutes=window, city_max_runtime=slot,
                                         run_one=self.scripted(calls, clock, minutes_by_city), **kwargs)
        return calls, results, clock

    def test_a_city_that_ran_to_its_budget_is_run_again_and_one_that_finished_early_is_not(self, monkeypatch):
        calls, results, _ = self.run(monkeypatch, {'city-0': 99, 'city-1': 0.1, 'city-2': 0.1},
                                     window=100, slot=12)

        assert [c for c, _, _ in calls] == ['city-0', 'city-1', 'city-2', 'city-0']
        assert [r.pass_number for r in results] == [1, 1, 1, 2]

    def test_pass_two_hands_the_working_city_what_the_window_has_left(self, monkeypatch):
        calls, results, _ = self.run(monkeypatch, {'city-0': 99, 'city-1': 0.1, 'city-2': 0.1},
                                     window=100, slot=12)

        # Pass 1 spent 12 + 0.1 + 0.1 = 12.2 min; one city still has work, so it gets all 87.8 that remain.
        assert calls[3] == ('city-0', pytest.approx(87.8), 2)
        assert results[3].budget_minutes == pytest.approx(87.8)

    def test_the_share_is_equal_among_the_cities_still_working_and_recomputed_as_they_finish(self, monkeypatch):
        """Two cities with work and 76 min left: 38 each. The first finishes at minute 20 of its 38, so the
        second is recomputed from what is actually left, not from the plan."""
        calls, _, _ = self.run(monkeypatch, {'city-0': 32, 'city-1': 99, 'city-2': 0.0},
                               window=100, slot=12)

        pass_two = [c for c in calls if c[2] == 2]
        assert pass_two[0] == ('city-0', pytest.approx(38.0), 2)
        assert pass_two[1] == ('city-1', pytest.approx(56.0), 2), 'city-0 used 20 of its 38; city-1 gets 76 - 20'

    def test_a_pass_budget_is_never_below_a_slot(self, monkeypatch):
        """213 minutes left for 39 working cities is 5.5 each - below --min-depth-runtime, which would zero
        every image phase and mail a warning per city, and a fraction of a minute can kill a city inside its
        pano-list fetch. So the share is floored at a slot, and the pass stops when a slot no longer fits."""
        calls, _, _ = self.run(monkeypatch, {'city-%d' % i: 99 for i in range(10)}, window=180, slot=12, n=10)

        pass_two = [c for c in calls if c[2] == 2]
        assert pass_two, 'there was window left after pass 1 (180 - 120 = 60 min)'
        assert all(budget >= 12 for _, budget, _ in pass_two)
        assert len(pass_two) == 5, '60 minutes is five full slots, not ten 6-minute ones'

    def test_passes_repeat_until_the_window_is_spent(self, monkeypatch):
        calls, results, clock = self.run(monkeypatch, {'city-0': 999, 'city-1': 0.0, 'city-2': 0.0},
                                         window=100, slot=12)

        # Pass 1: 12 min. Pass 2: all 88 left. Nothing fits after that.
        assert [c for c, _, _ in calls] == ['city-0', 'city-1', 'city-2', 'city-0']
        assert clock.now / 60.0 == pytest.approx(100.0), 'the window is spent, not left on the table'

    def test_passes_stop_when_nobody_hit_their_budget(self, monkeypatch):
        calls, results, clock = self.run(monkeypatch, {'city-0': 1, 'city-1': 1, 'city-2': 1},
                                         window=100, slot=12)

        assert len(calls) == 3
        assert clock.now / 60.0 == pytest.approx(3.0), 'a fleet with no work does not wait out the window'

    def test_a_failed_city_is_not_run_again(self, monkeypatch):
        clock = FakeClock()
        monkeypatch.setattr(scrape_queue, 'time', clock)
        calls = []

        def run_one(city, store_root, python_exe, runner_path, budget, kill_grace, runner_args, env=None):
            calls.append(city.city_id)
            clock.now += budget * 60.0  # every city runs to its budget...
            outcome = 'failed' if city.city_id == 'city-1' else 'ok'  # ...and one of them crashes there
            return scrape_queue.CityResult(city.city_id, outcome, 0 if outcome == 'ok' else 1, budget * 60.0)

        scrape_queue.run_queue(self.cities(), '/store', 'py', 'r.py', [], max_runtime_minutes=60,
                               city_max_runtime=12, run_one=run_one)

        assert 'city-1' not in calls[3:], 'a crash says nothing about work left, and re-running it is a crash loop'
        assert calls[3:] == ['city-0', 'city-2']

    def test_a_later_pass_never_reports_a_city_as_not_reached(self, monkeypatch):
        """skipped_deadline is pass 1's alarm: a city the guaranteed slot never reached is a fleet that is not
        completing. Running out of window in pass 3 is the design working."""
        _, results, _ = self.run(monkeypatch, {'city-%d' % i: 999 for i in range(3)}, window=50, slot=12)

        assert all(r.outcome == 'ok' for r in results)
        assert scrape_queue.exit_code_for(results) == 0

    def test_a_crash_in_a_later_pass_still_fails_the_night(self, monkeypatch):
        clock = FakeClock()
        monkeypatch.setattr(scrape_queue, 'time', clock)
        seen = []

        def run_one(city, store_root, python_exe, runner_path, budget, kill_grace, runner_args, env=None):
            seen.append(city.city_id)
            clock.now += budget * 60.0
            crashed = seen.count(city.city_id) == 2  # fine in pass 1, crashes in pass 2
            return scrape_queue.CityResult(city.city_id, 'failed' if crashed else 'ok', 1 if crashed else 0,
                                           budget * 60.0)

        results = scrape_queue.run_queue(self.cities(1), '/store', 'py', 'r.py', [], max_runtime_minutes=60,
                                         city_max_runtime=12, run_one=run_one)

        assert [r.outcome for r in results] == ['ok', 'failed']
        assert scrape_queue.exit_code_for(results) == 1

    def test_single_pass_is_exactly_the_old_behaviour(self, monkeypatch):
        calls, results, clock = self.run(monkeypatch, {'city-0': 999, 'city-1': 0.0, 'city-2': 0.0},
                                         window=100, slot=12, extra_passes=False)

        assert [c for c, _, _ in calls] == ['city-0', 'city-1', 'city-2']
        assert [r.pass_number for r in results] == [1, 1, 1]

    def test_without_a_slot_there_is_nothing_to_share(self, monkeypatch):
        """--city-max-runtime is both the pass-1 guarantee and the pass-2 floor; without it a second pass has
        no unit to hand out, so there is none. (With no slot, pass 1 hands the first city the whole window,
        so the first city here does 20 of its minutes and stops on budget only in the sense that matters:
        it is the window, not a slot, and there is nothing left to share.)"""
        calls, _, _ = self.run(monkeypatch, {'city-0': 100, 'city-1': 0.0, 'city-2': 0.0},
                               window=100, slot=None)

        assert len(calls) == 1, 'pass 1 with no slot gives the first city the window; no pass follows'

    def test_without_a_window_there_is_nothing_to_spend(self, monkeypatch):
        calls, _, _ = self.run(monkeypatch, {'city-0': 999, 'city-1': 0.0, 'city-2': 0.0},
                               window=None, slot=12)

        assert len(calls) == 3

    def test_a_pass_stops_when_a_slot_no_longer_fits_even_with_minutes_left(self, monkeypatch):
        """178 - 120 = 58 minutes after pass 1: four slots fit, then 10 minutes are left. A queue that
        starts a fifth city there hands it a slot the window cannot honour."""
        calls, _, clock = self.run(monkeypatch, {'city-%d' % i: 99 for i in range(10)}, window=178, slot=12,
                                   n=10)

        pass_two = [c for c in calls if c[2] == 2]
        assert len(pass_two) == 4
        assert clock.now / 60.0 == pytest.approx(168.0), '10 minutes are left for tomorrow, not overspent'

    def test_passes_repeat_while_cities_keep_finishing_early(self, monkeypatch):
        """Pass 2 gives two working cities 38 each; the second finishes after 18, leaving 20 - enough for a
        slot, so the city that is still working gets a third pass rather than the window going unused."""
        calls, results, clock = self.run(monkeypatch, {'city-0': 999, 'city-1': 30, 'city-2': 0.0},
                                         window=100, slot=12)

        assert [(c, p) for c, _, p in calls] == [('city-0', 1), ('city-1', 1), ('city-2', 1),
                                                 ('city-0', 2), ('city-1', 2), ('city-0', 3)]
        assert calls[-1][1] == pytest.approx(20.0)
        assert clock.now / 60.0 == pytest.approx(100.0)

    def test_a_claimed_budget_stop_with_no_slot_configured_starts_no_pass(self, monkeypatch):
        """With no slot, pass 1 hands each city what the window has left, so a run that stops on budget has
        spent the window and the question never arises in production. The guard is still real: a pass with
        no slot has no floor and no unit, so a result that CLAIMS a budget stop must not start one."""
        clock = FakeClock()
        monkeypatch.setattr(scrape_queue, 'time', clock)
        calls = []

        def run_one(city, store_root, python_exe, runner_path, budget, kill_grace, runner_args, env=None):
            calls.append(city.city_id)
            clock.now += 600.0  # ten minutes of real time, whatever the budget was...
            return scrape_queue.CityResult(city.city_id, 'ok', 0, budget * 60.0)  # ...and a claimed budget stop

        scrape_queue.run_queue(self.cities(), '/store', 'py', 'r.py', [], max_runtime_minutes=100,
                               city_max_runtime=None, run_one=run_one)

        assert calls == ['city-0', 'city-1', 'city-2']

    def test_every_result_carries_its_budget_and_pass(self, monkeypatch):
        _, results, _ = self.run(monkeypatch, {'city-0': 99, 'city-1': 0.1, 'city-2': 0.1}, window=100, slot=12)

        assert [(r.budget_minutes, r.pass_number) for r in results[:3]] == [(12, 1), (12, 1), (12, 1)]
        assert results[3].pass_number == 2


class TestTheRunSummaryReachesTheQueue:
    """run_city asks each city for a run summary and reads it back onto the result.

    This is the whole channel finding 1 turns on, so it is asserted on the argv the city actually received
    and on the CityResult that came back - not on the queue's own timing, which is precisely the thing that
    was wrong."""

    def test_the_city_is_asked_for_one(self, fake_runner, journal, tmp_path):
        scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                              sys.executable, fake_runner, 12.0, 1.0, [])

        start = [line for line in journal.read() if line.startswith('START')][0]
        assert '--run-summary-file' in start

    def test_what_the_city_reported_lands_on_the_result(self, fake_runner, journal, tmp_path, monkeypatch):
        monkeypatch.setenv('QUEUE_TEST_STOP', 'image:max-runtime')

        result = scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                                       sys.executable, fake_runner, 12.0, 1.0, [])

        assert result.stop_reasons == {'image_stop': 'max-runtime', 'depth_stop': None}
        assert scrape_queue.stopped_on_budget(result._replace(budget_minutes=12.0))

    def test_a_city_that_finished_reports_no_stop_however_long_it_took(self, fake_runner, journal, tmp_path,
                                                                      monkeypatch):
        """The false positive, end to end: this run outlasts its budget on the queue's clock and must still
        come back as finished, because the city said nothing stopped it."""
        monkeypatch.setenv('QUEUE_TEST_SLEEP', '0.4')

        result = scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                                       sys.executable, fake_runner, 0.002, 1.0, [])

        assert result.seconds > 0.002 * 60, 'the run must outlast its budget for this to be the case'
        assert result.stop_reasons == {'image_stop': None, 'depth_stop': None}
        assert not scrape_queue.stopped_on_budget(result._replace(budget_minutes=0.002))

    def test_a_runner_that_cannot_write_a_summary_leaves_the_fallback_in_charge(self, journal, tmp_path):
        """A runner that exits 0 without a summary - the file could not be written, or the queue's flag was
        displaced by an operator's own - carries None and the elapsed rule decides. This stand-in accepts the
        flag and ignores it; a DownloadRunner from before the flag existed would NOT do that (below)."""
        mute_runner = tmp_path / 'mute_runner.py'
        mute_runner.write_text('import os,sys\n'
                               "open(os.environ['QUEUE_TEST_JOURNAL'],'a').write('START %s x\\n'"
                               " % os.path.basename(sys.argv[2]))\n")

        result = scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                                       sys.executable, str(mute_runner), 12.0, 1.0, [])

        assert result.stop_reasons is None

    def test_a_runner_that_does_not_know_the_flag_is_a_failed_city_not_a_fallback(self, journal, tmp_path):
        """The mixed-version deployment the docs used to describe as "falls back": a runner whose parser
        predates --run-summary-file refuses the argument (argparse, exit 2). The queue books that as a
        FAILURE - never re-run - not as a run with work left; the docs now say so. Pinned against a stand-in
        with DownloadRunner's argparse shape, since the real pre-flag file is history, not a fixture."""
        strict_runner = tmp_path / 'strict_runner.py'
        strict_runner.write_text('import argparse, os, sys\n'
                                 "open(os.environ['QUEUE_TEST_JOURNAL'],'a').write('START %s x\\n'"
                                 " % os.path.basename(sys.argv[2]))\n"
                                 'p = argparse.ArgumentParser()\n'
                                 "p.add_argument('d'); p.add_argument('s'); p.add_argument('--max-runtime')\n"
                                 'p.parse_args()\n')

        result = scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(tmp_path / 'store'),
                                       sys.executable, str(strict_runner), 12.0, 1.0, [])

        assert (result.outcome, result.exit_code) == ('failed', 2)
        assert not scrape_queue.stopped_on_budget(result._replace(budget_minutes=12.0))

    def test_the_summary_file_does_not_litter_the_store(self, fake_runner, journal, tmp_path):
        """It is a temp file per run, not an artifact: the store holds the city's panos and ledgers, and a
        stray run_summary.json beside them would be read as one."""
        store = tmp_path / 'store'
        scrape_queue.run_city(scrape_queue.City('alpha-aa', 'host.invalid'), str(store),
                              sys.executable, fake_runner, 12.0, 1.0, [])

        assert not list(store.rglob('run_summary.json'))


class TestExtraPassesEndToEnd:
    """Through main() and a real stand-in runner. The scripted stand-in above proves the arithmetic; this
    proves the plumbing - the flags, the log, the summary - with real processes and the real clock.

    Which cities still have work is driven by what the stand-in REPORTS, not by making it outlive its
    budget: the old version raced three 1.5 s sleeps against a 12 s window, so a slow interpreter start
    could exhaust the window in pass 1 and fail the exit-code assertion. Nothing here now depends on how
    long a city takes.
    """

    def test_a_city_that_reports_a_budget_stop_is_run_again(self, tmp_path, fake_runner, journal,
                                                            monkeypatch, fleet_in_step):
        monkeypatch.setenv('QUEUE_TEST_STOP', 'image:max-runtime')
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate',
                        '--max-runtime', '5', '--city-max-runtime', '0.02', '--kill-grace', '0.1')

        starts = [line.split()[1] for line in journal.read() if line.startswith('START')]
        assert code == 0
        # Exactly two runs each: one in pass 1, one in pass 2, and then nothing - because the second run
        # reported no stop. An exact count, not `> 3`, so a pass loop that failed to notice the work had
        # run out (and spent the rest of the window re-running finished cities) fails here too.
        assert starts[:3] == ['alpha-aa', 'bravo-bb', 'charlie-cc']
        assert sorted(starts) == sorted(['alpha-aa', 'bravo-bb', 'charlie-cc'] * 2), starts
        log = (tmp_path / 'store' / 'scrape_queue.log').read_text()
        assert 'pass 2 starting' in log
        assert 'pass 3 starting' not in log

    def test_cities_that_report_no_stop_are_not_run_again(self, tmp_path, fake_runner, journal, monkeypatch,
                                                          fleet_in_step):
        """The discrimination the old timing-based test could not make: same window, same slot, same
        sleeps - only the reported reason differs, and no extra pass may happen."""
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate',
                        '--max-runtime', '5', '--city-max-runtime', '0.02', '--kill-grace', '0.1')

        starts = [line.split()[1] for line in journal.read() if line.startswith('START')]
        assert code == 0
        assert starts == ['alpha-aa', 'bravo-bb', 'charlie-cc'], 'no city had work left'
        assert 'pass 2 starting' not in (tmp_path / 'store' / 'scrape_queue.log').read_text()

    def test_only_the_city_with_work_is_re_run(self, tmp_path, fake_runner, journal, monkeypatch):
        monkeypatch.setenv('QUEUE_TEST_STOP_BRAVO_BB', 'depth:max-runtime')
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate',
                 '--max-runtime', '5', '--city-max-runtime', '0.02', '--kill-grace', '0.1')

        starts = [line.split()[1] for line in journal.read() if line.startswith('START')]
        assert starts[:3] == ['alpha-aa', 'bravo-bb', 'charlie-cc']
        assert starts[3:] == ['bravo-bb'], 'only bravo-bb reported work left, got %r' % (starts[3:],)

    def test_a_city_that_stood_down_on_the_block_latch_is_not_re_run(self, tmp_path, fake_runner, journal,
                                                                     monkeypatch):
        """A re-run would spend its slot rediscovering the same refusal - and 52 of them would escalate a
        soft refusal into a real ban, which is what the latch exists to prevent."""
        monkeypatch.setenv('QUEUE_TEST_STOP', 'depth:blocked')
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate',
                 '--max-runtime', '5', '--city-max-runtime', '0.02', '--kill-grace', '0.1')

        starts = [line.split()[1] for line in journal.read() if line.startswith('START')]
        assert starts == ['alpha-aa', 'bravo-bb', 'charlie-cc']

    def test_single_pass_runs_each_city_once(self, tmp_path, fake_runner, journal, monkeypatch):
        """Every city reports a budget stop, so only --single-pass can be what stops the second pass."""
        monkeypatch.setenv('QUEUE_TEST_STOP', 'image:max-runtime')
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate', '--single-pass',
                 '--max-runtime', '5', '--city-max-runtime', '0.02', '--kill-grace', '0.1')

        assert len([line for line in journal.read() if line.startswith('START')]) == 3

    def test_only_implies_a_single_pass(self, tmp_path, fake_runner, journal, monkeypatch):
        """--only is the manual re-run affordance: the operator asked for that city, once."""
        monkeypatch.setenv('QUEUE_TEST_STOP', 'image:max-runtime')
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--only', 'bravo-bb',
                 '--max-runtime', '5', '--city-max-runtime', '0.02', '--kill-grace', '0.1')

        assert [line.split()[1] for line in journal.read() if line.startswith('START')] == ['bravo-bb']

    def test_the_summary_reports_the_pass(self, tmp_path, fake_runner, journal, monkeypatch, capsys):
        monkeypatch.setenv('QUEUE_TEST_STOP', 'image:max-runtime')
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate',
                 '--max-runtime', '5', '--city-max-runtime', '0.02', '--kill-grace', '0.1')

        out = capsys.readouterr().out
        assert '3/3 cities ok' in out
        assert 'pass 2:' in out

    def test_dry_run_says_passes_cannot_be_shown(self, tmp_path, fake_runner, capsys):
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--dry-run',
                 '--max-runtime', '690', '--city-max-runtime', '12')

        assert 'extra passes' in capsys.readouterr().out.lower()


# --- Review fixes: the summary, the banner, the stop, and the reservation --------------------------------

def _clock_that_jumps_past(minutes):
    """time.monotonic that is normal once and then far in the future: a window spent before city 1 starts."""
    calls = {'n': 0}
    real = time.monotonic

    def fake():
        calls['n'] += 1
        return real() + (0 if calls['n'] < 2 else minutes * 60.0)
    return fake


class TestTheSummaryCountsEveryRunNotJustPassOne:
    """The totals line is the one grep-able aggregate in the cron mail, so it must not contradict the exit
    code. It counted `pass_number == 1` only, so a city hard-killed in pass 2 read as `0 timed out` on a
    night that exited 1 - while the per-run lines directly above it named the kill."""

    def test_a_failure_in_a_later_pass_is_counted(self):
        results = [result('alpha', seconds=60.0, budget=12.0),
                   result('alpha', outcome='failed', seconds=5.0, budget=12.0, pass_number=2)]

        assert '1 failed' in scrape_queue.summarise(results, 1.0)

    def test_a_timeout_in_a_later_pass_is_counted(self):
        results = [result('alpha', seconds=60.0, budget=12.0),
                   result('alpha', outcome='timed_out', exit_code=137, seconds=900.0, budget=12.0,
                          pass_number=2)]

        assert '1 timed out' in scrape_queue.summarise(results, 1.0)

    def test_the_fleet_denominator_still_counts_cities_not_runs(self):
        """The other half of the same line: a night that re-ran one city five times is a fleet of 2, not 7."""
        results = [result('alpha', seconds=60.0, budget=12.0), result('bravo', seconds=60.0, budget=12.0)]
        results += [result('alpha', seconds=60.0, budget=12.0, pass_number=n) for n in (2, 3, 4, 5, 6)]

        assert '2/2 cities ok' in scrape_queue.summarise(results, 1.0)

    def test_the_totals_line_never_disagrees_with_the_exit_code(self):
        """The property behind both: if the night exits 1, the totals must name something nonzero."""
        results = [result('alpha', seconds=60.0, budget=12.0),
                   result('alpha', outcome='timed_out', exit_code=137, seconds=900.0, budget=12.0,
                          pass_number=2)]
        totals = [ln for ln in scrape_queue.summarise(results, 1.0).splitlines() if 'cities ok' in ln][0]

        assert scrape_queue.exit_code_for(results) == 1
        assert '0 failed, 0 timed out, 0 not reached' not in totals, totals


class TestTheSummaryLeadsWithWhatWentWrong:
    """stdout is what cron mails, so the crashes must not be buried inside twenty skip lines. Grouping and
    run order are not in conflict: within an outcome the run order is kept."""

    def named_lines(self, results):
        return [ln.split()[1] for ln in scrape_queue.summarise(results, 1.0).splitlines()
                if ln.startswith('[queue] ') and 'cities ok' not in ln and '====' not in ln
                and not ln.startswith('[queue] pass ')]

    def test_failures_come_before_skips(self):
        results = [result('city-%02d' % n, outcome='skipped_deadline', seconds=None, exit_code=None)
                   for n in range(20)]
        results.insert(9, result('boom', outcome='failed', seconds=5.0))
        results.insert(15, result('hung', outcome='timed_out', exit_code=137, seconds=900.0))

        assert self.named_lines(results)[:2] == ['boom', 'hung'], self.named_lines(results)[:5]

    def test_run_order_is_kept_within_an_outcome(self):
        results = [result('zulu', outcome='failed', seconds=1.0),
                   result('alpha', outcome='failed', seconds=1.0)]

        assert self.named_lines(results) == ['zulu', 'alpha'], 'grouped by outcome, not re-sorted by name'


class TestASkippedCityIsNamedByItsCityId:
    """The skipped_deadline results were built as CityResult(c, ...) over City tuples, so the alarm line
    printed the whole namedtuple - leaking the fqdn into cron mail and blowing the %-24s column. The PR then
    made that same field a dict key, where a City can never match a city_id lookup."""

    def skipped(self, monkeypatch, tmp_path):
        cities = [scrape_queue.City('alpha-aa', 'sidewalk-alpha.invalid')]
        monkeypatch.setattr(scrape_queue.time, 'monotonic', _clock_that_jumps_past(90.0))
        return scrape_queue.run_queue(cities, str(tmp_path), sys.executable, 'runner.py', [],
                                      max_runtime_minutes=1.0, city_max_runtime=12.0)

    def test_a_skipped_result_keys_on_a_string(self, monkeypatch, tmp_path):
        results = self.skipped(monkeypatch, tmp_path)

        assert [r.outcome for r in results] == ['skipped_deadline']
        assert [r.city_id for r in results] == ['alpha-aa']
        assert {r.city_id: r for r in results}.get('alpha-aa') is not None

    def test_the_summary_does_not_leak_the_fqdn(self, monkeypatch, tmp_path):
        summary = scrape_queue.summarise(self.skipped(monkeypatch, tmp_path), 1.0)

        assert 'sidewalk-alpha.invalid' not in summary
        assert 'alpha-aa' in summary


class TestTheBannerDoesNotPromisePassesThatCannotHappen:
    """run_queue returns after pass 1 without both budgets, but the start banner announced them anyway -
    while --dry-run, two screens away, guarded on exactly that."""

    def test_a_window_with_no_slot_says_extra_passes_are_off(self, tmp_path, fake_runner, journal, capsys):
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--max-runtime', '5')

        out = capsys.readouterr().out
        assert 'extra passes off' in out
        assert 'extra passes on' not in out

    def test_a_slot_with_no_window_says_extra_passes_are_off(self, tmp_path, fake_runner, journal, capsys):
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--city-max-runtime', '5')

        assert 'extra passes off' in capsys.readouterr().out

    def test_both_budgets_says_on(self, tmp_path, fake_runner, journal, capsys):
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--max-runtime', '5',
                 '--city-max-runtime', '0.02')

        assert 'extra passes on' in capsys.readouterr().out


class TestAStoppedQueueStillReportsWhatItDid:
    """A SIGTERM mid-pass used to propagate straight out of main(), skipping summarise entirely: every city
    that had run that night vanished from stdout. Pre-#43 the queue occupied 477 of 690 minutes; it now
    occupies all 690 by design, so the window in which a cron timeout wrapper or an operator's kill can land
    inside it is the whole night, and the evidence lost is several passes deep."""

    def test_the_summary_survives_a_stop(self, tmp_path, fake_runner, journal, monkeypatch, capsys):
        calls = []

        def run_one(city, *a, **k):
            calls.append(city.city_id)
            if len(calls) == 2:
                raise KeyboardInterrupt('the queue is being stopped')
            return scrape_queue.CityResult(city.city_id, 'ok', 0, 30.0)

        monkeypatch.setattr(scrape_queue, 'run_city', run_one)
        with pytest.raises(KeyboardInterrupt):
            scrape_queue.main(['--lock', str(tmp_path / 'test.lock'), '--no-rotate',
                               '--cities', write_manifest(tmp_path, ['alpha-aa,h1', 'bravo-bb,h2']),
                               '--store-root', str(tmp_path / 'store'),
                               '--runner', fake_runner, '--python', sys.executable])

        out = capsys.readouterr().out
        assert calls == ['alpha-aa', 'bravo-bb'], calls
        # The city that finished before the stop is still counted, so the record of the night survives.
        assert '1/1 cities ok' in out, out

    def test_a_clean_night_reports_exactly_once(self, tmp_path, fake_runner, journal, capsys):
        """Discrimination against printing the summary twice - once in the finally and once after."""
        run_main(tmp_path, three_cities(tmp_path), fake_runner)

        assert capsys.readouterr().out.count('==== summary ====') == 1


class TestTheDepthReservationScalesWithAnEnlargedSlot:
    """--min-depth-runtime is a reservation carved out of --max-runtime, and the queue rewrites the budget
    for every extra pass - so leaving the reservation at its pass-1 value hands almost the whole enlarged
    slot to the image phase, which is the opposite of what the passes exist for (#43)."""

    def test_a_bigger_budget_gets_a_proportionally_bigger_reservation(self):
        args = scrape_queue.scale_depth_reservation(['--all-panos', '--min-depth-runtime', '6'],
                                                    budget_minutes=120.0, slot_minutes=12.0)

        assert args == ['--all-panos', '--min-depth-runtime', '60']

    def test_the_equals_form_is_rewritten_too(self):
        args = scrape_queue.scale_depth_reservation(['--min-depth-runtime=6'],
                                                    budget_minutes=24.0, slot_minutes=12.0)

        assert args == ['--min-depth-runtime=12']

    def test_a_pass_one_slot_is_left_exactly_alone(self):
        original = ['--all-panos', '--min-depth-runtime', '6']

        assert scrape_queue.scale_depth_reservation(original, 12.0, 12.0) == original

    def test_a_smaller_budget_is_never_scaled_up(self):
        """Pass 1's last city can be clamped BELOW the slot by the window; scaling must never enlarge the
        reservation, which at or past the budget zeroes the image phase entirely."""
        args = scrape_queue.scale_depth_reservation(['--min-depth-runtime', '6'], 3.0, 12.0)

        assert args == ['--min-depth-runtime', '6']

    def test_no_reservation_means_nothing_to_scale(self):
        assert scrape_queue.scale_depth_reservation(['--all-panos'], 120.0, 12.0) == ['--all-panos']

    def test_a_malformed_reservation_is_passed_through_untouched(self):
        """The runner owns argument validation; the queue must not turn a typo into a traceback of its own
        and take the whole fleet down with it."""
        original = ['--min-depth-runtime', 'six']

        assert scrape_queue.scale_depth_reservation(original, 120.0, 12.0) == original

    def test_it_reaches_the_command_an_extra_pass_actually_runs(self, tmp_path, fake_runner, journal,
                                                                monkeypatch):
        """Through run_queue, not just the helper: a correct fix that is never called is the failure mode."""
        monkeypatch.setenv('QUEUE_TEST_STOP', 'depth:max-runtime')
        run_main(tmp_path, write_manifest(tmp_path, ['alpha-aa,h1']), fake_runner, '--no-rotate',
                 '--max-runtime', '5', '--city-max-runtime', '0.02', '--kill-grace', '0.1',
                 '--', '--min-depth-runtime', '0.01')

        starts = [line for line in journal.read() if line.startswith('START')]
        assert len(starts) == 2, starts
        assert '--min-depth-runtime 0.01' in starts[0], starts[0]
        assert '--min-depth-runtime 0.01' not in starts[1], 'pass 2 must scale the reservation'


class TestReadingARunSummaryResolvesEveryDoubtTowardsTheFallback:
    """None means "fall back to the elapsed rule", so every malformed shape has to produce None rather than
    a confident wrong answer. The distinction that matters most is the last one: an object naming neither
    phase is a summary that says NOTHING, not one that says "nothing stopped me" - and only the latter is
    allowed to mark a city finished."""

    def write(self, tmp_path, text):
        path = tmp_path / 'summary.json'
        path.write_text(text)
        return str(path)

    def test_a_well_formed_summary_comes_back_with_both_keys(self, tmp_path):
        assert scrape_queue.read_run_summary(self.write(tmp_path, '{"image_stop": "max-runtime"}')) == {
            'image_stop': 'max-runtime', 'depth_stop': None}

    def test_a_missing_file_is_no_summary(self, tmp_path):
        assert scrape_queue.read_run_summary(str(tmp_path / 'nothing-here.json')) is None

    def test_a_truncated_write_is_no_summary(self, tmp_path):
        assert scrape_queue.read_run_summary(self.write(tmp_path, '{"image_stop": "max-run')) is None

    def test_something_that_is_not_an_object_is_no_summary(self, tmp_path):
        assert scrape_queue.read_run_summary(self.write(tmp_path, '["max-runtime"]')) is None

    def test_an_object_naming_neither_phase_is_no_summary(self, tmp_path):
        """The discrimination against treating `{}` as "nothing stopped me": an empty object is what a
        half-written or foreign file looks like, and marking a city finished on it would silently strand
        its backlog every night."""
        assert scrape_queue.read_run_summary(self.write(tmp_path, '{}')) is None
        assert scrape_queue.read_run_summary(self.write(tmp_path, '{"other": 1}')) is None

    def test_an_unknown_extra_key_is_ignored_not_fatal(self, tmp_path):
        summary = self.write(tmp_path, '{"image_stop": null, "depth_stop": null, "future_field": 7}')

        assert scrape_queue.read_run_summary(summary) == {'image_stop': None, 'depth_stop': None}


# --- The manifest is cross-checked against the fleet (#130) -------------------------------------------------
#
# laurens-ia and bayonne-fr launched on 2026-09-11 with no manifest row, and the queue ran green for six nights:
# a city the manifest does not name does not exist to it. Every deployment serves GET /v3/api/cities, the list
# of every city with its id, url and visibility, so once a night the queue asks one manifest host for it and
# names the PUBLIC cities that have no row. The manifest stays the deployment fact; the roster is the
# cross-check, and the exit code is the alarm.
#
# The key is city_id, not fqdn, and that is a measurement: the app reads AI-label crops from
# <pano.images.directory>/<city-id>/, its OWN id - the one the roster reports - so a row under any other name
# scrapes into a directory the app never looks at. Bayonne's row was `bayonne` for one afternoon; the app calls
# itself `bayonne-fr`. Keying on fqdn would have let that through.


def roster_entry(city_id, url='auto', visibility='public'):
    """One /v3/api/cities entry, in the shape measured 2026-09-17 (only the fields the check reads, plus one it
    ignores). url='auto' derives the production-shaped host; None is what every private city carries."""
    if url == 'auto':
        url = 'https://sidewalk-%s.cs.washington.edu' % city_id.split('-')[0]
    return {'city_id': city_id, 'url': url, 'visibility': visibility, 'country_id': 'usa'}


def roster_body(*entries, status='OK'):
    return json.dumps({'status': status, 'cities': list(entries)}).encode()


def serve_roster(monkeypatch, *entries, hosts=None, calls=None):
    """Install a fake fleet: every host (or only `hosts`) answers /v3/api/cities with `entries`."""
    calls = [] if calls is None else calls

    def open_url(url, timeout):
        calls.append(url)
        host = url.split('//')[1].split('/')[0]
        if hosts is not None and host not in hosts:
            raise urllib.error.URLError('connection refused')
        return roster_body(*entries)

    monkeypatch.setattr(scrape_queue, '_open_url', open_url)
    return calls


def cities(*rows):
    return [scrape_queue.City(city_id, fqdn) for city_id, fqdn in rows]


class TestTheRosterIsReadOnPositiveEvidence:
    """A 200 is not a roster. An error envelope, a proxy's HTML, a login page, a renamed field - each is a
    body that parses, and each would turn the check into "manifest checked against 0 public cities"
    forever, which is a check that never runs wearing the face of one that passed (the #99 rule)."""

    def test_the_measured_shape_is_read(self):
        entries = scrape_queue.parse_roster(roster_body(roster_entry('seattle-wa'),
                                                        roster_entry('zurich', url=None, visibility='private')))
        assert [e['city_id'] for e in entries] == ['seattle-wa', 'zurich']

    @pytest.mark.parametrize('body', [
        b'<html>Sign in</html>',
        b'',
        b'\xff\xfe not text',
        b'[]',
        b'{"status": "ERROR", "message": "no"}',
        b'{"status": "OK", "cities": {}}',
        b'{"status": "OK", "cities": []}',
        b'{"status": "OK", "cities": [1]}',
        b'{"status": "OK", "cities": [{"url": "https://x", "visibility": "public"}]}',
        b'{"status": "OK", "cities": [{"city_id": 7, "visibility": "public"}]}',
        b'{"status": "OK", "cities": [{"city_id": "", "visibility": "public"}]}',
        b'{"status": "OK", "cities": [{"city_id": "a", "visibility": "hidden"}]}',
        b'{"status": "OK", "cities": [{"city_id": "a", "visibility": "public"}, '
        b'{"city_id": "b", "visibility": "hidden"}]}',
        b'{"status": "OK", "cities": [{"city_id": "a", "visibility": "public"}, {"city_id": "b", "url": null}]}',
    ], ids=['html', 'empty', 'not-utf8', 'list', 'error-envelope', 'cities-not-a-list', 'no-cities',
            'entry-not-an-object', 'no-city-id', 'city-id-not-a-string', 'blank-city-id',
            'unknown-visibility', 'unknown-visibility-beside-a-public-city', 'no-visibility-beside-a-public-city'])
    def test_anything_else_is_not_a_roster(self, body):
        """The two 'beside a public city' cases are the discriminating ones: a lone unknown value is also
        refused by the no-public-city rule, so only an unknown value NEXT TO a public entry proves the
        per-entry rule exists. A third visibility upstream must be a decision here, not silently 'not
        public'."""
        with pytest.raises(scrape_queue.RosterUnavailable):
            scrape_queue.parse_roster(body)

    def test_a_roster_with_no_public_city_is_not_a_roster(self):
        """The discrimination that matters: an upstream rename of the value 'public' would otherwise read as
        'every city is private', the unlisted set would be empty, and the line would say 'checked against
        0 public cities' every night. A live fleet always lists at least one public city."""
        with pytest.raises(scrape_queue.RosterUnavailable):
            scrape_queue.parse_roster(roster_body(roster_entry('zurich', url=None, visibility='private')))

    def test_the_fetch_asks_the_documented_endpoint_with_a_timeout(self, monkeypatch):
        seen = {}

        def open_url(url, timeout):
            seen['url'], seen['timeout'] = url, timeout
            return roster_body(roster_entry('seattle-wa'))

        monkeypatch.setattr(scrape_queue, '_open_url', open_url)
        scrape_queue.fetch_roster('sidewalk-sea.cs.washington.edu')

        assert seen['url'] == 'https://sidewalk-sea.cs.washington.edu/v3/api/cities'
        assert seen['timeout'] == scrape_queue.ROSTER_TIMEOUT_SECONDS > 0

    @pytest.mark.parametrize('error, expected', [
        (urllib.error.HTTPError('https://x', 502, 'Bad Gateway', {}, None), 'HTTP 502'),
        (urllib.error.URLError(socket.timeout()), 'timed out'),
        (socket.timeout(), 'timed out'),
        (urllib.error.URLError('Name or service not known'), 'Name or service not known'),
        (http.client.BadStatusLine('HTTP/1.1 garbage'), 'BadStatusLine'),
        (http.client.IncompleteRead(b'partial'), 'IncompleteRead'),
        (UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte'), 'UnicodeDecodeError'),
        (ConnectionResetError('reset'), 'reset'),
    ], ids=['http-error', 'connect-timeout', 'read-timeout', 'dns', 'bad-status-line', 'incomplete-read',
            'not-text', 'reset'])
    def test_every_way_a_host_can_fail_is_one_named_refusal(self, monkeypatch, error, expected):
        """http.client's exceptions are not OSErrors and a decode error is a ValueError: either escaping
        would print a traceback AFTER the summary, on the one night the check had something to say."""
        def open_url(url, timeout):
            raise error

        monkeypatch.setattr(scrape_queue, '_open_url', open_url)
        with pytest.raises(scrape_queue.RosterUnavailable) as excinfo:
            scrape_queue.fetch_roster('sidewalk-sea.cs.washington.edu')
        assert expected in str(excinfo.value)

    def test_the_opener_ignores_the_environments_proxies(self, monkeypatch):
        """urllib honours HTTP(S)_PROXY from the environment by default. DownloadRunner's session sets
        trust_env=False for the scraper box's sake (its env is sourced from BASH_ENV); this is the same
        policy for the same reason - a proxy's login page is a 200 that is not a roster, every night."""
        built = {}

        class FakeOpener:
            def open(self, request, timeout=None):
                built['url'] = request.full_url
                built['agent'] = request.get_header('User-agent')
                return io.BytesIO(b'{}')

        def build_opener(*handlers):
            built['handlers'] = handlers
            return FakeOpener()

        monkeypatch.setattr(scrape_queue.urllib.request, 'build_opener', build_opener)
        # The suite-wide stub has to be lifted for this one test: it is _open_url itself under test.
        monkeypatch.setattr(scrape_queue, '_open_url', REAL_OPEN_URL)

        assert scrape_queue._open_url('https://sidewalk-sea.cs.washington.edu/v3/api/cities', 5) == b'{}'
        proxies = [h for h in built['handlers'] if isinstance(h, urllib.request.ProxyHandler)]
        assert len(proxies) == 1 and proxies[0].proxies == {}, built['handlers']
        assert 'sidewalk-panorama-tools' in built['agent']


REAL_OPEN_URL = scrape_queue._open_url


class TestWhichCitiesAreMissingFromTheManifest:

    def test_a_public_city_with_no_row_is_missing_with_its_host(self):
        roster = [roster_entry('seattle-wa'), roster_entry('laurens-ia')]
        manifest = cities(('seattle-wa', 'sidewalk-sea.cs.washington.edu'))

        assert scrape_queue.unlisted_cities(roster, manifest, {}) == [
            scrape_queue.Unlisted('laurens-ia', 'sidewalk-laurens.cs.washington.edu', None)]

    def test_a_private_city_with_no_row_is_not_missing(self):
        """The production manifest scrapes 15 private cities too, but it need not: the question is only
        whether every PUBLIC city has a row. Private cities carry url null, so there would be nothing to
        print for them anyway."""
        roster = [roster_entry('seattle-wa'), roster_entry('zurich', url=None, visibility='private')]

        assert scrape_queue.unlisted_cities(roster, cities(('seattle-wa', 'sidewalk-sea.cs.washington.edu')),
                                            {}) == []

    def test_a_manifest_that_is_a_superset_is_fine(self):
        roster = [roster_entry('seattle-wa')]
        manifest = cities(('seattle-wa', 'sidewalk-sea.cs.washington.edu'), ('zurich', 'sidewalk-zurich.x'))

        assert scrape_queue.unlisted_cities(roster, manifest, {}) == []

    def test_the_key_is_the_city_id_not_the_host(self):
        """The Bayonne shape, pinned with the real ids. The row `bayonne,sidewalk-bayonne...` names the right
        host, and the app reads <store>/bayonne-fr/ - so the city IS missing, and the report says why."""
        roster = [roster_entry('bayonne-fr', url='https://sidewalk-bayonne.cs.washington.edu')]
        manifest = cities(('bayonne', 'sidewalk-bayonne.cs.washington.edu'))

        assert scrape_queue.unlisted_cities(roster, manifest, {}) == [
            scrape_queue.Unlisted('bayonne-fr', 'sidewalk-bayonne.cs.washington.edu', 'bayonne')]

    def test_the_id_is_compared_exactly(self):
        """It is a directory name, so `Seattle-WA` is a different directory."""
        roster = [roster_entry('seattle-wa')]

        assert scrape_queue.unlisted_cities(roster, cities(('Seattle-WA', 'sidewalk-sea.cs.washington.edu')),
                                            {}) != []

    def test_the_host_is_compared_case_insensitively_for_the_misnamed_hint(self):
        roster = [roster_entry('bayonne-fr', url='https://Sidewalk-Bayonne.cs.washington.edu')]
        manifest = cities(('bayonne', 'sidewalk-bayonne.CS.washington.edu'))

        assert scrape_queue.unlisted_cities(roster, manifest, {})[0].misnamed_as == 'bayonne'

    def test_a_public_city_with_no_url_is_still_missing_and_no_host_is_guessed(self):
        roster = [roster_entry('new-xx', url=None)]

        assert scrape_queue.unlisted_cities(roster, [], {}) == [scrape_queue.Unlisted('new-xx', None, None)]

    def test_a_url_without_a_scheme_still_yields_its_host(self):
        roster = [roster_entry('new-xx', url='sidewalk-new.cs.washington.edu/')]

        assert scrape_queue.unlisted_cities(roster, [], {})[0].fqdn == 'sidewalk-new.cs.washington.edu'

    def test_a_disabled_row_counts_as_decided(self):
        """`#richmond-va,sidewalk-richmond...` is a city someone took out for a night, not one nobody knows
        about; nagging about it every night would teach people to ignore the line."""
        roster = [roster_entry('richmond-va')]
        disabled = {'richmond-va': 'sidewalk-richmond.cs.washington.edu'}

        assert scrape_queue.unlisted_cities(roster, [], disabled) == []

    def test_a_disabled_row_is_credited_only_with_its_host(self):
        """csv splits a prose comment on its commas too, so `# laurens-ia, bayonne-fr launched 2026-09-11`
        reads as city_id '# laurens-ia' - and the sample manifest's comments are exactly that style. Crediting
        the id alone would silence the very city the feature exists for. The row has to still name the host."""
        roster = [roster_entry('laurens-ia')]
        disabled = {'laurens-ia': 'bayonne-fr launched 2026-09-11 - add when live'}

        assert [u.city_id for u in scrape_queue.unlisted_cities(roster, [], disabled)] == ['laurens-ia']


class TestTheManifestRecordsItsDisabledRows:

    def test_a_hashed_row_is_reported_with_its_host(self, tmp_path):
        disabled = {}
        cities_read = scrape_queue.read_city_list(
            write_manifest(tmp_path, ['seattle-wa,sidewalk-sea.cs.washington.edu',
                                      '#richmond-va,Sidewalk-Richmond.cs.washington.edu',
                                      '# columbus-oh , sidewalk-columbus.cs.washington.edu']),
            disabled=disabled)

        assert [c.city_id for c in cities_read] == ['seattle-wa']
        assert disabled == {'richmond-va': 'sidewalk-richmond.cs.washington.edu',
                            'columbus-oh': 'sidewalk-columbus.cs.washington.edu'}

    def test_comment_rows_are_harmless(self, tmp_path):
        disabled = {}
        scrape_queue.read_city_list(
            write_manifest(tmp_path, ['#', '#,', '# a comment, with a comma', '   ',
                                      'seattle-wa,sidewalk-sea.cs.washington.edu']),
            disabled=disabled)

        assert disabled == {'a comment': 'with a comma'}

    def test_the_parameter_is_optional_and_the_return_value_unchanged(self, tmp_path):
        manifest = write_manifest(tmp_path, ['seattle-wa,sidewalk-sea.cs.washington.edu', '#x,y'])

        assert scrape_queue.read_city_list(manifest) == scrape_queue.read_city_list(manifest, disabled={})


class TestTheRosterIsFetchedBestEffortButBounded:

    def two(self):
        return cities(('alpha-aa', 'sidewalk-alpha.invalid'), ('bravo-bb', 'sidewalk-bravo.invalid'))

    def test_the_first_host_that_answers_is_used(self, monkeypatch):
        calls = serve_roster(monkeypatch, roster_entry('alpha-aa'), hosts=['sidewalk-bravo.invalid'])

        check = scrape_queue.check_manifest(self.two(), {})

        assert check.roster_host == 'sidewalk-bravo.invalid'
        assert check.attempts == [('sidewalk-alpha.invalid', 'connection refused')]
        assert len(calls) == 2

    def test_no_more_than_the_cap_is_tried(self, monkeypatch):
        """Ten dead hosts at 30 s each is five minutes after the fleet already finished, for a check that is
        going to say 'not cross-checked' whatever the tenth one says."""
        manifest = cities(*[('c%d' % i, 'sidewalk-c%d.invalid' % i) for i in range(10)])
        calls = serve_roster(monkeypatch, hosts=[])

        check = scrape_queue.check_manifest(manifest, {})

        assert check.roster_host is None
        assert len(calls) == len(check.attempts) == scrape_queue.ROSTER_MAX_HOSTS == 3
        assert check.hosts_total == 10

    def test_the_hosts_that_ran_ok_tonight_are_asked_first(self):
        """The check runs after the fleet, so the hosts that just served /adminapi/panos are the ones to ask;
        a host that failed its scrape is the last one whose roster call should be spent on."""
        manifest = cities(('a', 'ha'), ('b', 'hb'), ('c', 'hc'), ('d', 'hd'))
        results = [result('a', outcome='failed'), result('b'), result('c', outcome='timed_out'),
                   result('d'), result('b', pass_number=2)]

        assert scrape_queue.roster_hosts(manifest, results) == ['hb', 'hd', 'ha', 'hc']

    def test_a_body_that_is_not_a_roster_moves_on_to_the_next_host(self, monkeypatch):
        bodies = {'sidewalk-alpha.invalid': b'<html>login</html>',
                  'sidewalk-bravo.invalid': roster_body(roster_entry('alpha-aa'))}

        def open_url(url, timeout):
            return bodies[url.split('//')[1].split('/')[0]]

        monkeypatch.setattr(scrape_queue, '_open_url', open_url)
        check = scrape_queue.check_manifest(self.two(), {})

        assert check.roster_host == 'sidewalk-bravo.invalid'
        assert check.attempts == [('sidewalk-alpha.invalid', 'not JSON')]


class TestTheReportAndTheExitCodeAgree:

    def clean_night(self):
        return [result('alpha-aa', seconds=60.0, budget=12.0), result('bravo-bb', seconds=60.0, budget=12.0)]

    def gap(self, *unlisted, host='sidewalk-alpha.invalid'):
        return scrape_queue.ManifestCheck(host, 3, list(unlisted), [], 2)

    def test_a_gap_is_named_before_the_totals_and_counted_on_them(self):
        """stdout is what cron mails and the totals line is what people read first. A totals line reading
        '2/2 cities ok, 0 failed, 0 timed out, 0 not reached' above exit 1 would contradict the exit code,
        which is the property TestTheSummaryCountsEveryRunNotJustPassOne pins for the runs."""
        check = self.gap(scrape_queue.Unlisted('laurens-ia', 'sidewalk-laurens.cs.washington.edu', None),
                         scrape_queue.Unlisted('bayonne-fr', 'sidewalk-bayonne.cs.washington.edu', 'bayonne'))
        lines = scrape_queue.summarise(self.clean_night(), 1.0, check).splitlines()

        gap = [i for i, ln in enumerate(lines) if 'public cities missing from the manifest:' in ln][0]
        totals = [i for i, ln in enumerate(lines) if 'cities ok' in ln][0]
        assert gap < totals
        assert 'laurens-ia (sidewalk-laurens.cs.washington.edu)' in lines[gap]
        assert ("bayonne-fr (sidewalk-bayonne.cs.washington.edu; the manifest calls it 'bayonne', "
                "the app reads <store-root>/bayonne-fr)") in lines[gap]
        assert '2 public cities missing from the manifest' in lines[totals]
        assert scrape_queue.exit_code_for(self.clean_night(), check) == 1

    def test_a_missing_city_without_a_url_is_named_without_a_guessed_host(self):
        check = self.gap(scrape_queue.Unlisted('new-xx', None, None))
        text = scrape_queue.summarise(self.clean_night(), 1.0, check)

        assert 'new-xx (url not published)' in text

    def test_a_checked_manifest_says_so_and_changes_nothing(self):
        text = scrape_queue.summarise(self.clean_night(), 1.0, self.gap())

        assert 'manifest checked against 3 public cities (roster from sidewalk-alpha.invalid)' in text
        assert 'missing from the manifest' not in text
        assert '2/2 cities ok, 0 failed, 0 timed out, 0 not reached;' in text
        assert scrape_queue.exit_code_for(self.clean_night(), self.gap()) == 0

    def test_no_roster_is_a_warning_that_names_what_was_tried_and_fails_the_night(self):
        """Decided 2026-09-17: the hosts asked have just served /adminapi/panos, so none of them serving the
        roster is a broken check - an API rename, an env proxy, a moved endpoint - not weather, and a check
        silently skipped every night is the failure #130 exists to prevent."""
        check = scrape_queue.ManifestCheck(None, 0, [], [('sidewalk-alpha.invalid', 'timed out'),
                                                         ('sidewalk-bravo.invalid', 'HTTP 502')], 54)
        text = scrape_queue.summarise(self.clean_night(), 1.0, check)

        assert ('WARNING: manifest not cross-checked - no roster from sidewalk-alpha.invalid (timed out), '
                'sidewalk-bravo.invalid (HTTP 502) (2 of 54 hosts tried)') in text
        assert scrape_queue.exit_code_for(self.clean_night(), check) == 1

    def test_without_a_check_the_old_rule_stands(self):
        assert scrape_queue.exit_code_for(self.clean_night()) == 0
        assert scrape_queue.exit_code_for(self.clean_night(), None) == 0
        assert 'manifest' not in scrape_queue.summarise(self.clean_night(), 1.0)

    def test_a_failed_run_and_a_clean_check_is_still_a_failed_night(self):
        results = [result('alpha-aa', outcome='failed')]

        assert scrape_queue.exit_code_for(results, self.gap()) == 1


class TestTheCrossCheckEndToEnd:

    def test_a_launched_city_nobody_added_fails_the_night_on_both_channels(self, tmp_path, fake_runner,
                                                                           journal, monkeypatch, capsys,
                                                                           caplog):
        """The Laurens night, replayed: three cities scrape fine, the fleet has a fourth."""
        serve_roster(monkeypatch, roster_entry('alpha-aa', url='https://sidewalk-alpha.invalid'),
                     roster_entry('bravo-bb', url='https://sidewalk-bravo.invalid'),
                     roster_entry('charlie-cc', url='https://sidewalk-charlie.invalid'),
                     roster_entry('laurens-ia'))
        caplog.set_level(logging.INFO)

        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate')

        out = capsys.readouterr().out
        assert code == 1
        assert '3/3 cities ok, 0 failed, 0 timed out, 0 not reached, 1 public cities missing' in out
        assert 'laurens-ia (sidewalk-laurens.cs.washington.edu)' in out
        # Both channels (the print/logging rule): stdout is tonight's mail, the log is next week's evidence.
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any('laurens-ia' in r.getMessage() for r in errors), caplog.text
        assert (tmp_path / 'store' / 'scrape_queue.log').read_text().count('laurens-ia') >= 1

    def test_a_manifest_in_step_with_the_fleet_is_a_clean_night(self, tmp_path, fake_runner, journal,
                                                                monkeypatch, capsys):
        serve_roster(monkeypatch, roster_entry('alpha-aa', url='https://sidewalk-alpha.invalid'),
                     roster_entry('bravo-bb', url='https://sidewalk-bravo.invalid'),
                     roster_entry('charlie-cc', url='https://sidewalk-charlie.invalid'),
                     roster_entry('zurich', url=None, visibility='private'))

        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate')

        out = capsys.readouterr().out
        assert code == 0, out
        assert 'manifest checked against 3 public cities' in out

    def test_no_roster_fails_the_night_and_says_which_hosts_were_asked(self, tmp_path, fake_runner, journal,
                                                                       capsys):
        """Under the suite's network stub every host refuses, which is exactly the shape a broken check has."""
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate')

        out = capsys.readouterr().out
        assert code == 1
        assert 'WARNING: manifest not cross-checked' in out
        assert 'sidewalk-alpha.invalid (no network in tests)' in out
        assert '3 of 3 hosts tried' in out

    def test_a_disabled_row_counts_as_present_end_to_end(self, tmp_path, fake_runner, journal, monkeypatch,
                                                         capsys):
        serve_roster(monkeypatch, roster_entry('alpha-aa', url='https://sidewalk-alpha.invalid'),
                     roster_entry('bravo-bb', url='https://sidewalk-bravo.invalid'))
        manifest = write_manifest(tmp_path, ['alpha-aa,sidewalk-alpha.invalid',
                                             '#bravo-bb,sidewalk-bravo.invalid'])

        code = run_main(tmp_path, manifest, fake_runner, '--no-rotate')

        assert code == 0, capsys.readouterr().out

    def test_the_check_runs_under_only_too(self, tmp_path, fake_runner, journal, monkeypatch, capsys):
        """Laurens and Bayonne launched together. The operator re-running one of them by hand is the moment
        to hear about the other."""
        serve_roster(monkeypatch, roster_entry('alpha-aa', url='https://sidewalk-alpha.invalid'),
                     roster_entry('laurens-ia'))

        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--only', 'alpha-aa')

        out = capsys.readouterr().out
        assert code == 1
        assert '1/1 cities ok' in out  # the city itself was fine
        assert 'laurens-ia' in out

    def test_the_check_does_not_run_while_the_queue_is_being_stopped(self, tmp_path, fake_runner, journal,
                                                                     monkeypatch, capsys):
        """No network IO on the way out: the stop has to unwind through run_city's handler and release the
        lock, not sit in a 30 s connect on a host that may be the reason for the stop."""
        calls = serve_roster(monkeypatch, roster_entry('alpha-aa', url='https://sidewalk-alpha.invalid'))

        def run_one(city, *a, **k):
            raise KeyboardInterrupt('the queue is being stopped')

        monkeypatch.setattr(scrape_queue, 'run_city', run_one)
        with pytest.raises(KeyboardInterrupt):
            run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate')

        assert calls == []
        assert '==== summary ====' in capsys.readouterr().out

    def test_the_check_does_not_run_when_another_queue_holds_the_lock(self, tmp_path, fake_runner, journal,
                                                                      monkeypatch):
        calls = serve_roster(monkeypatch, roster_entry('alpha-aa', url='https://sidewalk-alpha.invalid'))
        lock = str(tmp_path / 'q.lock')
        with scrape_queue.exclusive_lock(lock):
            code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--lock', lock)

        assert code == 3
        assert calls == []


class TestDryRunCrossChecksToo:
    """So a hand-run before a launch answers "is everything wired?" without waiting for the night."""

    def test_a_gap_is_named_and_exits_one_while_still_running_nothing(self, tmp_path, fake_runner, journal,
                                                                      monkeypatch, capsys):
        serve_roster(monkeypatch, roster_entry('alpha-aa', url='https://sidewalk-alpha.invalid'),
                     roster_entry('laurens-ia'))
        lock = str(tmp_path / 'q.lock')
        with scrape_queue.exclusive_lock(lock):
            code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--dry-run', '--no-rotate',
                            '--lock', lock)

        out = capsys.readouterr().out
        assert code == 1
        assert journal.read() == []
        assert not (tmp_path / 'store').exists()
        assert 'laurens-ia (sidewalk-laurens.cs.washington.edu)' in out
        assert plan_lines(out) == ['alpha-aa', 'bravo-bb', 'charlie-cc']

    def test_an_unreachable_roster_is_advisory_on_a_dry_run(self, tmp_path, fake_runner, journal, capsys):
        """The nightly path fails on it; a dry run is someone at a keyboard, possibly offline, reading the
        plan - the WARNING is enough, and the plan is still printed above it."""
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--dry-run', '--no-rotate')

        out = capsys.readouterr().out
        assert code == 0
        assert 'WARNING: manifest not cross-checked' in out
        assert plan_lines(out) == ['alpha-aa', 'bravo-bb', 'charlie-cc']

    def test_a_manifest_in_step_with_the_fleet_exits_zero(self, tmp_path, fake_runner, journal, monkeypatch,
                                                          capsys):
        serve_roster(monkeypatch, roster_entry('alpha-aa', url='https://sidewalk-alpha.invalid'),
                     roster_entry('bravo-bb', url='https://sidewalk-bravo.invalid'),
                     roster_entry('charlie-cc', url='https://sidewalk-charlie.invalid'))

        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--dry-run')

        assert code == 0
        assert 'manifest checked against 3 public cities' in capsys.readouterr().out
