"""reports/2026-10-06-fleet-thresholds-and-frame-refusals.md, and the reducer that produced it.

Three layers, as in the other report tests: the reducer against synthetic logs (a committed artifact was
produced BY the code it would otherwise pin - .claude/rules/desk-studies.md), the committed JSON being exactly
what the script derives from the committed extracts, and every table cell the script prints appearing in the
report.
"""

import gzip
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import fleet_thresholds as ft  # noqa: E402

DATA = os.path.join(REPO_ROOT, 'reports', 'data')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-10-06-fleet-thresholds-and-frame-refusals.md')


def final(success, fallback, failed, skipped, total=None):
    completed = success + fallback + failed + skipped
    return ('DEBUG:root:IMAGEDOWNLOAD: Final result: Completed %d of %d (%d success, %d fallback success, '
            '%d failed, %d skipped)' % (completed, completed if total is None else total, success, fallback,
                                        failed, skipped))


def raise_line(pano, error='cannot identify image file <_io.BytesIO object at 0x7f>'):
    return 'ERROR:root:IMAGEDOWNLOAD: Failed to download pano %s due to error %s' % (pano, error)


FRAME = ("ERROR:root:IMAGEDOWNLOAD: Failed to download pano P9 due to error pano P9: frame disagreement: the "
         "app's frame is 13312x6656 but Google serves this pano at 16384x8192, and no reported level is that "
         "frame's tile grid")


def csv_row(ts, success, fallback, failed, skipped, minutes=0):
    fields = [ts, '0', '0', '9', '9', '0', str(success), str(fallback), str(failed), str(skipped),
              str(success + fallback + failed + skipped), str(minutes)]
    return {'ts': ts, 'fields': fields + [''] * (19 - len(fields))}


class TestTheReducer:

    def test_a_frame_refusal_is_an_answer_not_a_raise(self):
        runs = ft.split_image_runs([raise_line('A'), FRAME, final(0, 0, 2, 5)])
        assert len(runs[0]['raise_ids']) == 1
        assert runs[0]['frame'][0]['kind'] == 'frame'

    def test_runs_pair_from_the_end_and_a_disagreeing_pair_stops_the_pairing(self):
        runs = ft.split_image_runs([final(1, 0, 0, 0), final(2, 0, 0, 0), final(3, 0, 0, 0)])
        rows = [csv_row('2026-10-01 19:00:00-07:00', 9, 0, 0, 0), csv_row('2026-10-02 19:00:00-07:00', 2, 0, 0, 0),
                csv_row('2026-10-03 19:00:00-07:00', 3, 0, 0, 0)]
        pairs, mismatch = ft.align_runs(runs, rows)
        assert [idx for _, idx in pairs] == [1, 2]
        assert mismatch['row'][0] == 9 and mismatch['run'][0] == 1

    def test_answered_comes_from_the_seed_chain_and_is_unknown_without_a_previous_row(self):
        # Run 1: seed unknown. Run 2: 16 raises, nothing else -> field 9 grows by exactly the raises.
        lines = [raise_line('X%d' % i) for i in range(16)] + [final(0, 0, 100 + 16, 50)]
        lines += [raise_line('X%d' % i) for i in range(16)] + [final(0, 0, 100 + 16, 50)]
        rows = [csv_row('2026-10-03 19:00:00-07:00', 0, 0, 116, 50),
                csv_row('2026-10-04 19:00:00-07:00', 0, 0, 116, 50)]
        recs, mismatch, _, _ = ft.build_runs('c', ft.split_image_runs(lines), rows)
        assert mismatch is None
        assert recs[0]['answered'] is None
        assert recs[1]['answered'] == 0 and recs[1]['permanent'] == 0
        assert ft.count_arm(recs[1], 10) and not ft.count_arm(recs[1], 17)

    def test_a_crashed_row_between_two_runs_leaves_answered_unknown(self):
        """The crash may have ledgered rows, so the seed chain is broken - unknown, never a guessed 0."""
        lines = [final(0, 0, 100, 50)] + [raise_line('X%d' % i) for i in range(12)] + [final(0, 0, 112, 50)]
        crashed = {'ts': '2026-10-04 01:00:00-07:00', 'fields': ['2026-10-04 01:00:00-07:00'] + [''] * 18}
        rows = [csv_row('2026-10-03 19:00:00-07:00', 0, 0, 100, 50), crashed,
                csv_row('2026-10-04 19:00:00-07:00', 0, 0, 112, 50)]
        recs, mismatch, _, _ = ft.build_runs('c', ft.split_image_runs(lines), rows)
        assert mismatch is None and recs[1]['answered'] is None
        assert not ft.count_arm(recs[1], 10)

    def test_a_permanent_verdict_counts_as_answered(self):
        lines = [final(0, 0, 100, 50), raise_line('X'), final(0, 0, 102, 50)]
        rows = [csv_row('2026-10-03 19:00:00-07:00', 0, 0, 100, 50),
                csv_row('2026-10-04 19:00:00-07:00', 0, 0, 102, 50)]
        recs, _, _, _ = ft.build_runs('c', ft.split_image_runs(lines), rows)
        assert recs[1]['permanent'] == 1 and recs[1]['answered'] == 1

    def test_the_budget_arm_needs_a_max_runtime_stop(self):
        rec = {'answered': 0, 'stop': None, 'raised': 2, 'mean_raise_seconds_upper_bound': 500.0}
        assert not ft.budget_arm(rec, 60.0)
        assert ft.budget_arm(dict(rec, stop='max-runtime'), 60.0)

    def test_a_night_is_the_local_date_twelve_hours_earlier(self):
        assert ft.night_of('2026-09-25 01:11:38.140210-07:00') == '2026-09-24'
        assert ft.night_of('2026-09-24 20:04:41.142876-07:00') == '2026-09-24'

    def test_zero_streaks_are_per_run_and_a_pass_crossing_midnight_is_one_run(self):
        """Grouping by calendar date split a pass that crossed midnight in two (#208 review, nit 4)."""
        starts = ['2026-09-24 23:00:00-07:00', '2026-09-25 01:11:00-07:00']
        streaks = ft.zero_streaks([('0', '2026-09-24 23:50:00-07:00'), ('0', '2026-09-25 00:10:00-07:00'),
                                   ('1', '2026-09-25 00:20:00-07:00'), ('0', '2026-09-25 00:30:00-07:00'),
                                   ('0', '2026-09-25 01:20:00-07:00')], starts)
        assert streaks[starts[0]] == {'night': '2026-09-24', 'rows': 4, 'downloaded_0': 3,
                                      'longest_zero_streak': 2}
        assert streaks[starts[1]]['longest_zero_streak'] == 1

    @pytest.mark.parametrize('line, secret', [
        ('url=https://graph.mapillary.com/1?fields=x&access_token=MLY|123|abcdef rest', 'abcdef'),
        # one case per pattern, so dropping either regex fails (#208 review, nit 3)
        ('headers: Authorization: OAuth MLY|1|abc123 rest', 'abc123'),
        ('url=https://graph.mapillary.com/1?access_token=EAAGsecretvalue rest', 'EAAGsecretvalue'),
    ])
    def test_a_mapillary_token_is_redacted(self, line, secret):
        out = ft.redact(line)
        assert secret not in out and out.endswith(' rest')

    def test_a_header_row_is_not_a_run(self, tmp_path):
        """'start_time' >= '2026-09-01' as strings, so the date filter let 53 header rows in (#208 review)."""
        path = tmp_path / 'log.csv.gz'
        with gzip.open(path, 'wt', encoding='utf-8') as f:
            f.write('c,start_time,image_success\nc,2026-10-01 19:00:00-07:00,0,0,9,9,0,1,0,0,0,1,0\n')
        rows = ft.read_log_csv(str(path))
        assert [r['ts'] for r in rows['c']] == ['2026-10-01 19:00:00-07:00']

    def test_a_pairing_that_a_shift_also_passes_is_flagged_and_a_raise_run_is_not_identified(self):
        same = [final(0, 0, 5, 50)] * 4
        rows = [csv_row('2026-10-0%d 19:00:00-07:00' % (i + 1), 0, 0, 5, 50) for i in range(4)]
        assert ft.shift_identification(ft.split_image_runs(same), rows)['shift1_passes_fully']
        # A raise in a stretch of identical tuples cannot be dated by the counts.
        lines = [final(0, 0, 5, 50), raise_line('A'), final(0, 0, 5, 50), final(0, 0, 5, 50)]
        assert not ft.shift_identification(ft.split_image_runs(lines), rows)['raise_runs_identified']
        # Distinct counts pin it.
        lines = [final(1, 0, 5, 50), raise_line('A'), final(2, 0, 5, 50), final(3, 0, 5, 50)]
        rows = [csv_row('2026-10-0%d 19:00:00-07:00' % (i + 1), i + 1, 0, 5, 50) for i in range(3)]
        ident = ft.shift_identification(ft.split_image_runs(lines), rows)
        assert ident == {'shift1_passes_fully': False, 'raise_runs_identified': True}

    def test_an_edge_band_refusal_is_its_own_bucket_and_an_answer(self):
        """#213's EdgeBandError message carries 'frame disagreement' too; it is not a frame refusal."""
        edge = ('ERROR:root:IMAGEDOWNLOAD: Failed to download pano P8 due to error pano P8: frame disagreement: '
                'the stitch has an exactly-black band 12.0% deep along the bottom')
        runs = ft.split_image_runs([edge, FRAME, final(0, 0, 2, 5)])
        assert runs[0]['edge_band'] == ['P8'] and len(runs[0]['frame']) == 1 and not runs[0]['raise_ids']
        summary = ft.frame_summary([], {'c': [edge, FRAME]})
        assert summary['refusal_lines'] == 1 and summary['edge_band_lines'] == {'c': 1}


class TestTheCommittedArtifact:

    def test_the_json_is_what_the_extracts_reduce_to(self):
        with open(os.path.join(DATA, ft.RESULT_FILE), encoding='utf-8') as f:
            committed = json.load(f)
        assert json.loads(json.dumps(ft.analyze(DATA))) == committed

    def test_every_run_pairs_with_its_log_csv_row(self):
        result = ft.analyze(DATA)
        assert all(a['mismatch'] is None for a in result['alignment'].values())

    @pytest.mark.parametrize('name', [ft.LOG_CSV_FILE, ft.SCRAPE_LOG_FILE, ft.QUEUE_LOG_FILE])
    def test_no_extract_carries_a_token(self, name):
        with gzip.open(os.path.join(DATA, name), 'rt', encoding='utf-8') as f:
            text = f.read()
        assert ft.redact(text) == text


class TestTheReportMatchesTheArtifact:

    def test_every_table_line_is_in_the_report(self):
        with open(REPORT, encoding='utf-8') as f:
            report = f.read()
        missing = [line for line in ft.tables(ft.analyze(DATA)).splitlines() if line and line not in report]
        assert not missing, missing[:5]
