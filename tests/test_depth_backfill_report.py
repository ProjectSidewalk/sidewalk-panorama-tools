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


def row(city_id, eligible, resolved, requests, stop='max-runtime'):
    return {'city_id': city_id, 'eligible': eligible, 'resolved': resolved, 'last_run_requests': requests,
            'last_run_saved': requests // 2, 'last_run_unavailable': requests - requests // 2,
            'last_run_stop': stop}


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

    def test_the_slot_yield_is_the_median_over_full_slots_only(self):
        """A run whose image phase spent half the slot (245 requests) is not what a slot yields."""
        summary = dbp.reduce_progress([row('a', 10000, 100, 590), row('b', 10000, 100, 580),
                                       row('c', 10000, 100, 245), row('d', 500, 500, 0, stop='None')])

        assert summary['fleet']['requests_per_full_slot_median'] == 585.0

    def test_a_corpus_that_shrank_does_not_go_negative(self):
        assert dbp.reduce_progress([row('a', 90, 100, 0, 'None')])['cities'][0]['unresolved'] == 0


class TestTheLogLineParser:

    LINE = ('DEBUG:root:DEPTHDOWNLOAD: Final result: Completed 1753 of 183680 (282 success, 311 failed '
            '[311 unavailable], 1160 skipped, 593 requests, stop_reason=max-runtime)')

    def test_it_reads_the_seattle_line(self):
        assert dbp.parse_final_result(self.LINE) == {
            'eligible': 183680, 'resolved': 1753, 'last_run_requests': 593, 'last_run_saved': 282,
            'last_run_unavailable': 311, 'last_run_stop': 'max-runtime'}

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

        rows = dbp.scan_store(str(tmp_path))

        assert [r['city_id'] for r in rows] == ['somewhere']
        assert rows[0]['last_run_requests'] == 593

    def test_the_csv_round_trips(self, tmp_path):
        rows = [dict(city_id='a', **dbp.parse_final_result(self.LINE))]
        path = str(tmp_path / 'p.csv')

        dbp.write_csv(path, rows)

        assert dbp.read_csv(path) == rows


class TestTheCommittedArtifactIsWhatTheScriptProduces:

    def test_the_json_is_the_reduction_of_the_csv(self):
        with open(JSON) as f:
            committed = json.load(f)
        regenerated = json.loads(json.dumps(dbp.reduce_progress(dbp.read_csv(CSV)), allow_nan=False))
        assert regenerated == committed

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
        for key in ('eligible', 'resolved', 'unresolved', 'requests_last_night'):
            assert '{:,}'.format(f[key]) in report, key
        assert '%.1f%%' % f['resolved_pct'] in report
        assert '%.0f' % f['requests_per_full_slot_median'] in report

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
