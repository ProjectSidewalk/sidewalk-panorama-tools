"""reports/2026-09-09-depth-backfill-first-nights.md, and the reducer that produced it.

Three layers, because each catches something the others cannot:

  * the reducer against synthetic rows - a fleet average versus a longest city, undefined-is-None, the floor
    regimes from their constants - since a committed artifact was produced BY the code it would otherwise be
    pinning (CLAUDE.md: committed-artifact tests do not test code);
  * the committed JSON being exactly what the script produces from the committed CSV, so the artifact cannot
    drift from the data it claims to summarise;
  * every number the report quotes being in the artifact - the convention since two hand-typed counts in an
    earlier report were wrong by 2x and 6x with nothing about the sentences looking different for it.
"""

import json
import os
import re
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for _p in (SCRIPTS,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import depth_backfill_progress as dbp  # noqa: E402

DATA = os.path.join(REPO_ROOT, 'reports', 'data')
CSV = os.path.join(DATA, '2026-09-09-depth-backfill-progress.csv')
JSON = os.path.join(DATA, '2026-09-09-depth-backfill-progress.json')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-09-09-depth-backfill-first-nights.md')


def row(city_id, eligible, resolved, requests, stop='max-runtime', failed=None, unavailable=None):
    """One scanned row. `failed` defaults to every failure being a permanent `unavailable`, which is what
    the 2026-09-09 snapshot measured; pass them apart to make a transient failure visible."""
    unavailable = requests - requests // 2 if unavailable is None else unavailable
    return {'city_id': city_id, 'eligible': eligible, 'resolved': resolved, 'last_run_requests': requests,
            'last_run_saved': requests // 2, 'last_run_failed': unavailable if failed is None else failed,
            'last_run_unavailable': unavailable, 'last_run_stop': stop}


class TestTheReducer:

    def test_the_fleet_average_and_the_longest_city_are_different_numbers(self):
        """The wrong turn the report records: 1,000 + 100,000 unresolved at 500 a night each is 101 nights on
        average and 200 for the city that sets the finish date."""
        summary = dbp.reduce_progress([row('small', 2000, 1000, 500), row('big', 101000, 1000, 500)])

        assert summary['fleet']['nights_measured_fleet_average'] == pytest.approx(101.0)
        assert summary['fleet']['nights_measured_longest_city'] == pytest.approx(200.0)
        assert summary['longest_remaining'][0]['city_id'] == 'big'

    def test_a_complete_city_has_no_nights_left_and_counts_as_complete(self):
        summary = dbp.reduce_progress([row('done', 500, 500, 0, stop='None')])

        city = summary['cities'][0]
        assert city['complete'] and city['nights_measured'] is None and city['nights_floor_slot'] is None
        assert (summary['fleet']['complete'], summary['fleet']['working']) == (1, 0)
        assert summary['fleet']['nights_measured_longest_city'] is None
        assert summary['fleet']['nights_floor_slot_longest_city'] is None

    def test_work_left_with_no_requests_last_night_has_no_measured_eta(self):
        """A stood-down city: unresolved / 0 is undefined, not zero and not infinity."""
        summary = dbp.reduce_progress([row('stood-down', 1000, 100, 0, stop='None')])

        assert summary['cities'][0]['nights_measured'] is None
        assert summary['fleet']['nights_measured_fleet_average'] is None
        assert summary['cities'][0]['nights_floor_slot'] == pytest.approx(900 / 1920)

    def test_a_city_with_no_gsv_panos_is_neither_complete_nor_working(self):
        summary = dbp.reduce_progress([row('richmond-va', 0, 0, 0, stop='None'), row('x', 100, 100, 0, 'None')])

        assert summary['fleet']['cities_with_gsv'] == 1
        assert summary['fleet']['complete'] == 1

    def test_the_floor_regimes_follow_their_constants(self):
        summary = dbp.reduce_progress([row('a', 3840 + 100, 100, 500)], slot_minutes=12.0,
                                      window_minutes=690.0, floor_gap_seconds=0.375)

        assert summary['parameters']['per_slot_at_floor'] == 1920.0
        assert summary['parameters']['per_night_at_floor'] == 110400.0
        assert summary['cities'][0]['nights_floor_slot'] == pytest.approx(2.0)
        assert summary['fleet']['nights_floor_whole_window'] == pytest.approx(3840 / 110400)

    def test_the_slot_yield_is_the_median_over_every_max_runtime_run(self):
        """The filter is the stop reason and nothing else.

        It used to also require >= 500 requests, which is circular: selecting runs *by* request count and
        then reporting the median request count can only bias it upward. The 245-request run below (an
        image phase that took most of the slot) is exactly the row that filter dropped, and dropping it
        moves the median from 580 to 585."""
        summary = dbp.reduce_progress([row('a', 10000, 100, 590), row('b', 10000, 100, 580),
                                       row('c', 10000, 100, 245), row('d', 500, 500, 0, stop='None')])

        assert summary['fleet']['requests_per_max_runtime_run_median'] == 580.0
        assert summary['fleet']['max_runtime_runs'] == 3

    def test_a_night_the_whole_fleet_ran_slowly_still_has_a_median(self):
        """The failure mode the request-count floor would have had: a fleet-wide back-off puts every run
        under it, and the figure the report is built on silently becomes undefined."""
        summary = dbp.reduce_progress([row('a', 10000, 100, 30), row('b', 10000, 100, 10)])

        assert summary['fleet']['requests_per_max_runtime_run_median'] == 20.0

    def test_no_max_runtime_run_leaves_the_slot_yield_undefined(self):
        summary = dbp.reduce_progress([row('done', 500, 500, 0, stop='None')])

        assert summary['fleet']['requests_per_max_runtime_run_median'] is None
        assert summary['fleet']['seconds_per_request_median'] is None
        assert summary['fleet']['max_runtime_runs'] == 0

    def test_the_seconds_per_request_is_the_slot_divided_by_the_median(self):
        """The report quotes it; it is a derivation, so the reducer owns it rather than a reader's calculator."""
        summary = dbp.reduce_progress([row('a', 10000, 100, 600)], slot_minutes=12.0)

        assert summary['fleet']['seconds_per_request_median'] == pytest.approx(720.0 / 600)

    # --- D: a failure that was not `unavailable` has to be visible ---

    def test_a_transient_failure_is_not_hidden_behind_unavailable(self):
        """`unavailable` is a subset of `failed`. Recording only the subset makes a night of network
        failures read exactly like a clean one."""
        summary = dbp.reduce_progress([row('a', 10000, 100, 100, failed=40, unavailable=30)])

        assert summary['fleet']['failed_last_night'] == 40
        assert summary['fleet']['unavailable_last_night'] == 30
        assert summary['fleet']['transient_failures_last_night'] == 10
        assert summary['cities'][0]['last_run_failed'] == 40

    def test_a_row_with_no_failed_column_leaves_the_fleet_total_undefined(self):
        """A snapshot taken before the column existed: undefined is not zero, and zero would read as
        'we measured no transient failures'."""
        stale = row('a', 10000, 100, 100)
        del stale['last_run_failed']

        summary = dbp.reduce_progress([stale])

        assert summary['cities'][0]['last_run_failed'] is None
        assert summary['fleet']['failed_last_night'] is None
        assert summary['fleet']['transient_failures_last_night'] is None
        assert summary['fleet']['unavailable_last_night'] == 50

    def test_a_corpus_that_shrank_does_not_go_negative(self):
        assert dbp.reduce_progress([row('a', 90, 100, 0, 'None')])['cities'][0]['unresolved'] == 0


class TestTheLogLineParser:

    LINE = ('DEBUG:root:DEPTHDOWNLOAD: Final result: Completed 1753 of 183680 (282 success, 311 failed '
            '[311 unavailable], 1160 skipped, 593 requests, stop_reason=max-runtime)')

    def test_it_reads_the_seattle_line(self):
        assert dbp.parse_final_result(self.LINE) == {
            'eligible': 183680, 'resolved': 1753, 'last_run_requests': 593, 'last_run_saved': 282,
            'last_run_failed': 311, 'last_run_unavailable': 311, 'last_run_stop': 'max-runtime'}

    def test_it_keeps_the_failed_count_apart_from_the_unavailable_one(self):
        """The regex always captured `failed`; the schema dropped it, so a night of transient failures
        looked exactly like a night of permanent ones."""
        line = self.LINE.replace('311 failed [311 unavailable]', '311 failed [300 unavailable]')

        parsed = dbp.parse_final_result(line)

        assert (parsed['last_run_failed'], parsed['last_run_unavailable']) == (311, 300)

    def test_a_finished_city_reports_none(self):
        line = self.LINE.replace('stop_reason=max-runtime', 'stop_reason=None')
        assert dbp.parse_final_result(line)['last_run_stop'] == 'None'

    def test_any_other_line_is_ignored(self):
        assert dbp.parse_final_result('DEBUG:root:DEPTHDOWNLOAD: Processing pano abc') is None

    def test_scan_store_takes_the_last_line_per_city(self, tmp_path):
        city = tmp_path / 'somewhere'
        city.mkdir()
        earlier = self.LINE.replace('593 requests', '100 requests')
        (city / 'scrape.log').write_text('noise\n' + earlier + '\n' + self.LINE + '\n')
        (tmp_path / 'no-log').mkdir()

        rows, census = dbp.scan_store(str(tmp_path))

        assert [r['city_id'] for r in rows] == ['somewhere']
        assert rows[0]['last_run_requests'] == 593
        assert (census['cities_in_store'], census['cities_with_a_final_result']) == (1, 1)

    def test_a_city_whose_log_has_no_final_result_is_named_rather_than_dropped(self, tmp_path):
        """It used to be silently absent, so every fleet total it belongs to was quietly short one city."""
        for city_id in ('has-one', 'rotated-past-it'):
            (tmp_path / city_id).mkdir()
        (tmp_path / 'has-one' / 'scrape.log').write_text(self.LINE + '\n')
        (tmp_path / 'rotated-past-it' / 'scrape.log').write_text('DEBUG:root:DEPTHDOWNLOAD: Processing pano a\n')

        rows, census = dbp.scan_store(str(tmp_path))

        assert [r['city_id'] for r in rows] == ['has-one']
        assert census['cities_in_store'] == 2
        assert census['cities_with_a_final_result'] == 1
        assert census['cities_without_a_final_result'] == ['rotated-past-it']

    def test_a_city_with_fewer_runs_than_the_fleet_is_named_as_behind(self, tmp_path):
        """The log line carries no timestamp (`logging.BASIC_FORMAT`), so the only staleness signal in the
        file is how many runs it recorded: a city that ran two nights while the fleet ran three is
        contributing an older night's row to tonight's totals."""
        for city_id in ('ran-three', 'ran-two'):
            (tmp_path / city_id).mkdir()
        (tmp_path / 'ran-three' / 'scrape.log').write_text((self.LINE + '\n') * 3)
        (tmp_path / 'ran-two' / 'scrape.log').write_text((self.LINE + '\n') * 2)

        rows, census = dbp.scan_store(str(tmp_path))

        assert len(rows) == 2
        assert census['final_result_lines_max'] == 3
        assert census['cities_behind_the_fleet'] == ['ran-two']

    def test_the_census_travels_into_the_reduction_and_is_none_without_a_store(self):
        census = {'cities_in_store': 3, 'cities_with_a_final_result': 1,
                  'cities_without_a_final_result': ['b', 'c'], 'final_result_lines_max': 3,
                  'cities_behind_the_fleet': []}

        assert dbp.reduce_progress([row('a', 10, 1, 5)], store_census=census)['store_scan'] == census
        assert dbp.reduce_progress([row('a', 10, 1, 5)])['store_scan'] is None

    def test_the_csv_round_trips(self, tmp_path):
        rows = [dict(city_id='a', **dbp.parse_final_result(self.LINE))]
        path = str(tmp_path / 'p.csv')

        dbp.write_csv(path, rows)

        assert dbp.read_csv(path) == rows

    @pytest.mark.parametrize('header, data', [
        ('city_id,eligible,resolved,last_run_requests,last_run_saved,last_run_unavailable,last_run_stop',
         'a,10,1,5,3,2,max-runtime'),
        ('city_id,eligible,resolved,last_run_requests,last_run_saved,last_run_failed,last_run_unavailable,'
         'last_run_stop', 'a,10,1,5,3,,2,max-runtime'),
    ], ids=['column absent', 'column blank'])
    def test_a_csv_from_before_the_failed_column_reads_as_undefined(self, tmp_path, header, data):
        """Not zero: zero would be the claim that this snapshot measured no transient failures, which is
        exactly what a snapshot taken before the column existed cannot say."""
        path = tmp_path / 'old.csv'
        path.write_text(header + '\n' + data + '\n')

        rows = dbp.read_csv(str(path))

        assert rows[0]['last_run_failed'] is None
        assert dbp.reduce_progress(rows)['fleet']['transient_failures_last_night'] is None


class TestTheCommittedArtifactIsWhatTheScriptProduces:

    def test_the_json_is_the_reduction_of_the_csv(self):
        with open(JSON) as f:
            committed = json.load(f)
        regenerated = json.loads(json.dumps(dbp.reduce_progress(dbp.read_csv(CSV)), allow_nan=False))
        assert regenerated == committed

    def test_every_failure_in_the_snapshot_was_a_permanent_one(self):
        """What licenses the committed `last_run_failed` column, which was derived rather than re-scanned:
        `request_count` is incremented once per pano and exactly one of success/failure follows it
        (downloaders/gsv.py), so failed == requests - saved; and that equals unavailable on every row
        here, which is the report's "zero push-back in three nights" seen from the other side."""
        rows = dbp.read_csv(CSV)

        assert len(rows) == 52
        for r in rows:
            assert r['last_run_saved'] + r['last_run_failed'] == r['last_run_requests'], r['city_id']
            assert r['last_run_failed'] == r['last_run_unavailable'], r['city_id']

    def test_the_script_runs_from_the_repo_root(self, tmp_path):
        out = str(tmp_path / 'out.json')
        result = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'depth_backfill_progress.py'),
                                 '--csv', CSV, '--write', out], cwd=REPO_ROOT, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert 'longest remaining' in result.stdout
        with open(out) as f, open(JSON) as g:
            assert json.load(f) == json.load(g)


class TestTheReportMatchesTheArtifact:

    @pytest.fixture(scope='class')
    def report(self):
        with open(REPORT, encoding='utf8') as f:
            return f.read()

    @pytest.fixture(scope='class')
    def fleet(self):
        with open(JSON) as f:
            return json.load(f)

    def test_the_fleet_counts(self, report, fleet):
        f = fleet['fleet']
        for key in ('cities', 'cities_with_gsv', 'complete', 'working'):
            assert str(f[key]) in report, key
        for key in ('eligible', 'resolved', 'unresolved', 'requests_last_night', 'failed_last_night'):
            assert '{:,}'.format(f[key]) in report, key
        assert '%.1f%%' % f['resolved_pct'] in report
        assert '({:,} transient)'.format(f['transient_failures_last_night']) in report

    PER_REQUEST_SECONDS = re.compile(r'\*{0,2}([\d.]+) s\*{0,2} (?:per|a) request')

    def test_the_slot_yield_and_its_arithmetic(self, report, fleet):
        """What a slot yielded, and the per-request cost the page used to derive by hand - 720 / the median,
        written as 1.22 where the division gives 1.23.

        Every occurrence is checked, not just one: the page quotes the cost twice, so a pin satisfied by
        finding the value anywhere passes while one of the two says something else."""
        f = fleet['fleet']
        assert '**%.1f**' % f['requests_per_max_runtime_run_median'] in report
        assert str(f['max_runtime_runs']) in report

        quoted = self.PER_REQUEST_SECONDS.findall(report)

        assert quoted, 'no per-request cost found - if the prose dropped it, drop this pin with it'
        assert set(quoted) == {'%.2f' % f['seconds_per_request_median']}

    RATE_SENTENCE = re.compile(r'at ([\d,]+) requests a night ([a-z][a-z0-9-]*)')

    def test_a_per_city_request_rate_quoted_in_prose_is_that_city_s_own(self, report, fleet):
        """The gap that let a hand-typed 590 stand for chicago-il's measured 582 through every other pin:
        the prose quotes a per-city rate, and nothing checked per-city rates at all. 590 divides the
        unresolved count into 457 nights, a number that appears nowhere - the table says 463."""
        rates = {c['city_id']: c['last_run_requests'] for c in fleet['cities']}

        quoted = self.RATE_SENTENCE.findall(report)

        assert quoted, 'no per-city rate sentence found - if the prose dropped it, drop this test with it'
        for number, city_id in quoted:
            assert city_id in rates, city_id
            assert int(number.replace(',', '')) == rates[city_id], city_id

    def test_the_four_regimes(self, report, fleet):
        f = fleet['fleet']
        for key in ('nights_measured_fleet_average', 'nights_measured_longest_city',
                    'nights_floor_slot_longest_city', 'nights_floor_whole_window'):
            assert '**%.0f**' % f[key] in report, key

    def test_the_regime_constants(self, report, fleet):
        p = fleet['parameters']
        assert '{:,.0f}'.format(p['per_slot_at_floor']) in report
        assert '{:,.0f}'.format(p['per_night_at_floor']) in report

    def test_the_five_longest_cities(self, report, fleet):
        assert len(fleet['longest_remaining']) == 5
        for city in fleet['longest_remaining']:
            line = '| %s | %s | %.0f | %.0f |' % (city['city_id'], '{:,}'.format(city['unresolved']),
                                                 city['nights_measured'], city['nights_floor_slot'])
            assert line in report, line

    def test_the_headline_is_the_longest_city(self, report, fleet):
        assert 'a fleet average hid a %.0f-night tail' % fleet['fleet']['nights_measured_longest_city'] in report
