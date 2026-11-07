"""reports/2026-11-05-field20-fleet-raisers.md, and the reducer that produced it (#182).

Four layers: the reducer against synthetic rows (a committed artifact was produced BY the code it would otherwise
pin - .claude/rules/desk-studies.md), the committed JSON being exactly what the script derives from the
committed extracts, the real analyzer agreeing with the script on the committed extract, and every table line
the script prints appearing in the report.
"""

import gzip
import importlib.util
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
import field20_raisers as f20  # noqa: E402
import fleet_thresholds as ft  # noqa: E402

# analyze.py is loaded by path, as tests/test_log_analyzer.py loads it (a directory, not a package).
_spec = importlib.util.spec_from_file_location(
    'log_analyzer_analyze', os.path.join(REPO_ROOT, 'log_analyzer', 'analyze.py'))
analyze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyze)

DATA = os.path.join(REPO_ROOT, 'reports', 'data')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-11-05-field20-fleet-raisers.md')


def row(ts, raised, success=0, width=20):
    """A packed log.csv row as read_log_csv returns it: field 20 = `raised` ('' blank), fields 7/8 = success/0."""
    fields = [ts] + ['0'] * 18 + [str(raised)]
    fields[6] = str(success)
    return {'ts': ts, 'fields': fields[:width] + [''] * (19 - width)}


def night(day, hour=19):
    return '2026-10-%02d %02d:00:00.000000-07:00' % (day, hour)


class TestTheReducer:

    def test_a_nights_value_is_the_max_of_its_passes_and_a_blank_is_ignored(self):
        nights = f20.night_values([row(night(10), 200), row(night(10, 23), 3), row(night(11), ''),
                                   row(night(11, 21), 200), row(night(12), '')])
        assert nights['2026-10-10']['raised'] == 200
        assert nights['2026-10-11']['raised'] == 200
        assert nights['2026-10-12']['raised'] is None, 'a night of only blanks is unknown, never 0'

    def test_a_row_before_field_20_is_no_night_at_all(self):
        assert f20.night_values([row(night(10), 5, width=19)]) == {}

    def test_a_pass_after_midnight_belongs_to_the_evening_before(self):
        assert f20.night_of('2026-10-11 02:30:00-07:00') == '2026-10-10'
        assert f20.night_of('2026-11-02 19:00:00-08:00') == '2026-11-02', 'the stamp\'s own offset, PST included'

    def test_the_rule_needs_every_night_known_and_at_the_floor(self):
        nights = {('2026-10-%02d' % d): {'raised': 3, 'downloaded': 0, 'rows': 1} for d in range(1, 8)}
        assert f20.fires(nights, '2026-10-07', 3, 7)
        assert not f20.fires(nights, '2026-10-07', 4, 7)
        nights['2026-10-04']['raised'] = None
        assert not f20.fires(nights, '2026-10-07', 3, 7), 'an unknown night breaks the streak'
        del nights['2026-10-04']
        assert not f20.fires(nights, '2026-10-07', 3, 7), 'so does a missing one'
        assert f20.longest_streak(nights, f20.calendar('2026-10-01', '2026-10-07')) == 3

    def test_downloads_are_counted_per_night_and_unknown_stays_unknown(self):
        nights = f20.night_values([row(night(10), 5, success=7), row(night(10, 22), 5, success=2)])
        assert nights['2026-10-10']['downloaded'] == 9

    def test_the_queue_condition_is_dated_to_its_night(self):
        lines = ['2026-10-08 01:22:06,538 ERROR burnaby: ok (exit 0) in 0.5 min; conditions: images-no-success',
                 '2026-10-08 20:00:00,000 ERROR burnaby: ok (exit 0) in 0.5 min; conditions: edge-band']
        assert f20.queue_condition_nights(lines, 'images-no-success') == {'burnaby': ['2026-10-07']}

    def test_attribution_buckets_by_kind_and_prefix_and_skips_frame_refusals(self):
        lines = ['ERROR:root:IMAGEDOWNLOAD: Failed to download pano CAoSa due to error cbk probe answered 400, not 200',
                 'ERROR:root:IMAGEDOWNLOAD: Failed to download pano CAoSa due to error cbk probe answered 400, not 200 '
                 '(after 0.4 s)',
                 'ERROR:root:IMAGEDOWNLOAD: Failed to download pano xyz due to error pano xyz: frame disagreement: x',
                 'ERROR:root:IMAGEDOWNLOAD: Failed to download pano 0a1b2c3d-0000-4000-8000-00000000abcd due to error '
                 'Panoramax hd asset for picture 0a1b2c3d-0000-4000-8000-00000000abcd redirected (302)']
        buckets = f20.attribution(lines)
        assert buckets[0] == {'kind': 'cbk probe answered 400, not 200', 'prefix': 'CAoS', 'lines': 2,
                              'distinct_panos': 1}
        assert buckets[1]['prefix'] == 'other' and '<uuid>' in buckets[1]['kind']
        assert len(buckets) == 2

    def test_the_scripts_rule_is_the_analyzers(self):
        assert (f20.RULE_MIN_RAISED, f20.RULE_NIGHTS) == (analyze.STEADY_RAISE_MIN_RAISED,
                                                          analyze.STEADY_RAISE_NIGHTS)


class TestTheCommittedArtifact:

    def test_the_json_is_what_the_extracts_reduce_to(self):
        with open(os.path.join(DATA, f20.RESULT_FILE), encoding='utf-8') as f:
            committed = json.load(f)
        assert json.loads(json.dumps(f20.analyze(DATA))) == committed

    @pytest.mark.parametrize('name', sorted(set(f20.FILES.values()) - {f20.FILES['dc_zero_rows'],
                                                                         f20.FILES['dc_census']}))
    def test_no_extract_carries_a_token(self, name):
        """richmond-va's scrape.log on the store still holds the 2026-09-01 Mapillary token leak (#100)."""
        with gzip.open(os.path.join(DATA, name), 'rt', encoding='utf-8') as f:
            text = f.read()
        assert ft.redact(text) == text
        assert 'MLY|' not in text.replace('MLY|<redacted>', '')

    def test_the_real_analyzer_fires_on_exactly_the_cities_the_script_says(self, tmp_path):
        """rule 11 on each city's committed rows (UTC dates, the analyzer's own clock) against the script's
        runner-clock nights: the two groupings must agree on the fleet's answer."""
        rows = {}
        with gzip.open(os.path.join(DATA, f20.FILES['log_csv']), 'rt', encoding='utf-8') as f:
            for line in f:
                city, rest = line.rstrip('\n').split(',', 1)
                rows.setdefault(city, []).append(rest)
        firing = []
        for city, city_rows in sorted(rows.items()):
            path = tmp_path / ('%s.csv' % city)
            path.write_text('\n'.join(city_rows) + '\n', encoding='utf-8')
            if any(i['msg'].startswith('Steady raisers') for i in analyze.analyze_city(city, path, stale_days=10**6)):
                firing.append(city)
        result = f20.analyze(DATA)
        assert firing == result['sweep']['%d|%d' % (f20.RULE_MIN_RAISED, f20.RULE_NIGHTS)]['cities_last_night']
        assert firing, 'guards the guard: a rule that fired nowhere would make this vacuous'


class TestTheReportMatchesTheArtifact:

    def test_every_table_line_is_in_the_report(self):
        with open(REPORT, encoding='utf-8') as f:
            report = f.read()
        missing = [line for line in f20.tables(f20.analyze(DATA)).splitlines() if line and line not in report]
        assert not missing, missing[:5]
