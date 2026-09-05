"""Tests for reports/scripts/refetch_pilot.py — the reducer for a `fover` re-fetch pilot (#73).

The pilot itself needs the scraper box and a live endpoint, so what is pinned here is the arithmetic
that turns its output into the artifact a report cites. Two things matter more than the rest:

* **Undefined is not zero.** A pilot that swapped nothing has no median recovery; reporting 0.0 would
  read as "we measured no recovery" rather than "we measured nothing", and that distinction is the
  whole point of the run. Same failure the studyfmt review found in four scripts at once.
* **The horizon band is the control, not a second measurement.** If the summary ever stopped
  subtracting it, every figure would silently include our own JPEG re-encode and the pilot would
  report recovery that is not there.
"""

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for _p in (REPO_ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import refetch_pilot as rf  # noqa: E402
import studyfmt  # noqa: E402


def record(pano_id, bottom_mae, horizon_mae, halving=0.5, width=13312, height=6656):
    return {
        'pano_id': pano_id, 'zoom': 5, 'width': width, 'height': height, 'old_bytes': 1,
        'bottom': {'rows_px': [4608, 6656], 'mae_old_vs_new': bottom_mae,
                   'halve_restore_new': halving},
        'horizon': {'rows_px': [2048, 4608], 'mae_old_vs_new': horizon_mae,
                    'halve_restore_new': 1.3},
        'recovered_above_noise': round(bottom_mae - horizon_mae, 6),
    }


class TestReadOutcomes:
    def test_it_counts_by_status_and_skips_the_header(self, tmp_path):
        path = tmp_path / 'refetch_log.csv'
        path.write_text('pano_id,status\na,replaced\nb,gone\nc,gone\n')

        assert rf.read_outcomes(str(path)) == {'replaced': 1, 'gone': 2}

    def test_a_torn_line_is_skipped(self, tmp_path):
        path = tmp_path / 'refetch_log.csv'
        path.write_text('pano_id,status\na,replaced\nb\n')

        assert rf.read_outcomes(str(path)) == {'replaced': 1}


class TestReadMeasurements:
    def test_it_reads_one_object_per_line_and_tolerates_a_trailing_blank(self, tmp_path):
        path = tmp_path / 'm.jsonl'
        path.write_text(json.dumps(record('a', 1.0, 0.4)) + '\n\n'
                        + json.dumps(record('b', 1.2, 0.5)) + '\n')

        assert [r['pano_id'] for r in rf.read_measurements(str(path))] == ['a', 'b']


class TestPercentile:
    def test_an_empty_series_is_undefined_not_zero(self):
        assert rf.percentile([], 0.5) is None

    @pytest.mark.parametrize('q,expected', [(0.0, 1), (0.5, 3), (1.0, 5)])
    def test_it_reads_by_nearest_rank(self, q, expected):
        assert rf.percentile([5, 1, 3, 2, 4], q) == expected

    def test_one_value_is_every_percentile_of_itself(self):
        assert rf.percentile([7.5], 0.1) == rf.percentile([7.5], 0.9) == 7.5


class TestSummarise:
    def test_survival_is_measured_over_what_google_was_actually_asked(self):
        """`absent` and `not_affected` never reached Google, so counting them would understate survival.
        The denominator is the panoramas that got past the store-only gates."""
        counts = {'replaced': 6, 'gone': 4, 'absent': 50, 'not_affected': 20}

        s = rf.summarise(__import__('collections').Counter(counts), [])

        assert s['probed_at_google'] == 10
        assert s['still_served_pct'] == 60.0

    def test_rates_are_undefined_rather_than_zero_when_nothing_was_probed(self):
        s = rf.summarise(__import__('collections').Counter({'absent': 3}), [])

        assert s['still_served_pct'] is None
        assert s['clean_refetch_pct'] is None
        assert s['undersized_pct'] is None

    def test_an_empty_pilot_reports_undefined_recovery_not_a_confident_zero(self):
        s = rf.summarise(__import__('collections').Counter({'gone': 3}), [])

        assert s['recovered_above_noise']['median'] is None
        assert s['recovered_above_noise']['n_positive'] == 0
        assert s['bottom_band_mae_median'] is None

    def test_the_headline_is_the_bottom_band_net_of_the_horizon_control(self):
        records = [record('a', 1.0, 0.4), record('b', 1.4, 0.4), record('c', 0.9, 0.5)]

        s = rf.summarise(__import__('collections').Counter({'replaced': 3}), records)

        assert s['recovered_above_noise']['median'] == pytest.approx(0.6)
        assert s['recovered_above_noise']['n_positive'] == 3
        assert s['bottom_band_mae_median'] == pytest.approx(1.0)
        assert s['horizon_band_mae_median'] == pytest.approx(0.4)

    def test_a_pilot_that_recovers_nothing_reads_as_nothing(self):
        """Discrimination, and the outcome the report has to be able to state: if the fresh frame differs
        from the stored one by as much in the untouched horizon band as in the bottom band, the whole
        difference is our re-encode and there is nothing to recover."""
        records = [record('a', 0.4, 0.4), record('b', 0.35, 0.45)]

        s = rf.summarise(__import__('collections').Counter({'replaced': 2}), records)

        assert s['recovered_above_noise']['n_positive'] == 0
        assert s['recovered_above_noise']['median'] <= 0

    def test_the_halving_reference_is_carried_so_the_gain_can_be_read_against_a_ceiling(self):
        records = [record('a', 1.0, 0.4, halving=0.57)]

        s = rf.summarise(__import__('collections').Counter({'replaced': 1}), records)

        assert s['bottom_band_halving_cost_median'] == pytest.approx(0.57)

    def test_a_pano_google_serves_larger_counts_as_served_not_retired(self):
        """`frame_grew` means Google holds this panorama - bigger than we asked for. Counting it as
        retired would understate survival, which is the one figure the retirement argument rests on. It
        stays out of the tile-level rates, though, because no tiles were ever fetched for it."""
        counts = __import__('collections').Counter({'replaced': 3, 'frame_grew': 2, 'gone': 5})

        s = rf.summarise(counts, [])

        assert s['probed_at_google'] == 10
        assert s['still_served_pct'] == 50.0
        assert s['frame_grew'] == 2
        assert s['undersized_pct'] == 0.0     # over the 3 fetched, not the 5 served

    def test_undersized_is_reported_against_the_panoramas_google_answered_for(self):
        counts = __import__('collections').Counter({'replaced': 8, 'undersized': 2, 'gone': 90})

        s = rf.summarise(counts, [])

        assert s['undersized_pct'] == 20.0

    def test_recovery_is_also_broken_down_by_frame_with_its_n(self):
        """The two zoom-5 geometries put different fractions of their height in the half-res band, so the
        headline is also reported per frame - with n, so a handful of the smaller frame is not read as a
        finding. Each side must be computed on its own records, not on the pool."""
        records = [record('a', 1.0, 0.4, width=16384, height=8192),
                   record('b', 1.4, 0.4, width=16384, height=8192),
                   record('d', 1.8, 0.4, width=16384, height=8192),
                   record('c', 0.3, 0.5, width=13312, height=6656)]

        s = rf.summarise(__import__('collections').Counter({'replaced': 4}), records)

        assert set(s['by_frame']) == {'16384x8192', '13312x6656'}
        # The 13312 side is the discrimination: its own median is -0.2, while any pooled figure (nearest-rank
        # median of -0.2, 0.6, 1.0, 1.4 is 1.0) would read as recovery.
        assert s['by_frame']['16384x8192'] == {'n': 3, 'median': pytest.approx(1.0), 'n_positive': 3}
        assert s['by_frame']['13312x6656'] == {'n': 1, 'median': pytest.approx(-0.2), 'n_positive': 0}
        assert s['recovered_above_noise']['median'] == pytest.approx(1.0)

    def test_an_empty_pilot_has_no_frames(self):
        assert rf.summarise(__import__('collections').Counter({'gone': 3}), [])['by_frame'] == {}


class TestMain:
    def write_pilot(self, tmp_path, ledger_rows, records):
        ledger = tmp_path / 'refetch_log.csv'
        ledger.write_text('pano_id,status\n' + ''.join('%s,%s\n' % r for r in ledger_rows))
        measurements = tmp_path / 'refetch_measurements.jsonl'
        measurements.write_text(''.join(json.dumps(r) + '\n' for r in records))
        return str(ledger), str(measurements)

    def test_it_writes_a_readable_artifact_with_the_probe_date_it_was_given(self, tmp_path, capsys):
        ledger, measurements = self.write_pilot(
            tmp_path, [('a', 'replaced'), ('b', 'gone')], [record('a', 1.0, 0.4)])
        out = tmp_path / 'pilot.json'

        assert rf.main([ledger, measurements, '--probed', '2026-08-20', '--write', str(out)]) == 0

        payload = json.loads(out.read_text())
        assert payload['probed'] == '2026-08-20'
        assert payload['summary']['still_served_pct'] == 50.0
        assert [r['pano_id'] for r in payload['records']] == ['a']
        assert 'NaN' not in out.read_text(), 'the artifact must be readable by any JSON parser'

    def test_it_writes_nothing_without_write(self, tmp_path):
        ledger, measurements = self.write_pilot(tmp_path, [('a', 'gone')], [])

        rf.main([ledger, measurements])

        assert list(tmp_path.glob('*.json')) == []

    def test_an_empty_pilot_prints_rather_than_crashing_on_a_format_spec(self, tmp_path, capsys):
        """The exact bug studyfmt exists for: undefined quantities reaching a print with a format spec,
        after all the work and before the write."""
        ledger, measurements = self.write_pilot(tmp_path, [('a', 'absent')], [])

        assert rf.main([ledger, measurements]) == 0
        assert 'n/a' in capsys.readouterr().out

    def test_it_uses_the_shared_formatters(self):
        assert rf.fmt is studyfmt.fmt
        assert rf.num is studyfmt.num
