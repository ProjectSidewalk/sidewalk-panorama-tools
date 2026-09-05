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

import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

import scrape_queue
from conftest import posix_only

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _isolate_process_state():
    """main() configures logging and installs a SIGTERM handler process-wide; snapshot and restore both so
    these tests cannot leak handlers into each other or into the rest of the suite."""
    root = logging.getLogger()
    prior_handlers, prior_level = list(root.handlers), root.level
    prior_sigterm = signal.getsignal(signal.SIGTERM)
    yield
    for handler in list(root.handlers):
        if handler not in prior_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(prior_level)
    signal.signal(signal.SIGTERM, prior_sigterm)


# --- Stand-in runner ---------------------------------------------------------------------------------------
#
# Behaves like DownloadRunner from the queue's point of view: takes <fqdn> <storage> plus flags, and exits.
# Everything it does is driven by the environment so one script covers every scenario.

FAKE_RUNNER = textwrap.dedent('''
    import os, signal, sys, time

    journal = os.environ['QUEUE_TEST_JOURNAL']
    city = os.path.basename(sys.argv[2])

    def note(event):
        with open(journal, 'a') as f:
            f.write('%s %s %s\\n' % (event, city, ' '.join(sys.argv[1:])))

    note('START')
    if os.environ.get('QUEUE_TEST_IGNORE_SIGTERM') and hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(float(os.environ.get('QUEUE_TEST_SLEEP', '0')))
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

    def test_cities_run_one_at_a_time_in_order(self, tmp_path, fake_runner, journal):
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

    def test_only_runs_just_that_city(self, tmp_path, fake_runner, journal):
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--only', 'bravo-bb')
        assert code == 0
        assert [tuple(line.split()[:2]) for line in journal.read()] == [
            ('START', 'bravo-bb'), ('END', 'bravo-bb')]

    def test_a_window_with_no_per_city_cap_warns(self, tmp_path, fake_runner, journal, capsys):
        """The window only gates STARTING a city, so without a per-city cap it is advisory - one hung city
        holds it open indefinitely. Same discipline as --min-depth-runtime's warning: say so when the
        combination cannot do what it looks like it does."""
        run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate', '--max-runtime', '60')
        assert 'WARNING' in capsys.readouterr().out


class TestDryRun:

    def test_it_prints_the_plan_and_runs_nothing(self, tmp_path, fake_runner, journal, capsys):
        code = run_main(tmp_path, three_cities(tmp_path), fake_runner, '--no-rotate', '--dry-run',
                        '--', '--all-panos')

        out = capsys.readouterr().out
        assert code == 0
        assert journal.read() == []
        assert out.count('sidewalk-') == 3
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
        listed = [line for line in capsys.readouterr().out.splitlines() if 'sidewalk-' in line]
        assert len(listed) == 3
        assert sorted(l.split('sidewalk-')[1].split('.')[0] for l in listed) == ['alpha', 'bravo', 'charlie']


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
