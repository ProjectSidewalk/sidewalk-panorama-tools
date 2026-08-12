"""Tests for reports/scripts/photometa_census.py — network-free.

The census's non-network parts are pinned: the stratified sample is deterministic under its seed
(so the committed manifest is reproducible), the record extractor reads exactly the streetlevel
fields the repo already pins in test_streetlevel_api.py, and the summarizer's drift/alive
accounting is checked on synthetic records. The live fetch itself is a thin loop over these.
"""

import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import photometa_census as pc  # noqa: E402


def label_frame():
    """Synthetic rawlabels-shaped frame: 30 panos in city A spanning eras, 10 in city B."""
    rows = []
    for i in range(30):
        era_time = {0: '2019-06-01', 1: '2022-06-01', 2: '2025-06-01'}[i % 3]
        rows.append({'pano_id': f'A{i:03d}', 'city': 'a-city',
                     'time_created': pd.Timestamp(era_time, tz='UTC'),
                     'pano_width': 16384.0, 'pano_height': 8192.0})
    for i in range(10):
        rows.append({'pano_id': f'B{i:03d}', 'city': 'b-city',
                     'time_created': pd.Timestamp('2025-06-01', tz='UTC'),
                     'pano_width': 13312.0, 'pano_height': 6656.0})
    df = pd.DataFrame(rows)
    import rawlabels
    return rawlabels.add_era(df)


def _write_rawlabels_csv(path, records):
    """A rawLabels-shaped CSV carrying only what `main`'s sampling step reads; the rest stay blank."""
    import rawlabels as rl
    df = pd.DataFrame([{'pano_id': r['pano_id'],
                        'pano_width': r['stored_width'], 'pano_height': r['stored_height'],
                        'label_id': i,
                        'time_created': int(pd.Timestamp('2022-06-01', tz='UTC').value // 10 ** 6)}
                       for i, r in enumerate(records)])
    for column in rl.STUDY_COLUMNS:
        if column not in df:
            df[column] = np.nan
    df[rl.STUDY_COLUMNS].to_csv(path, index=False)
    return str(path)


class TestSampling:

    def test_deterministic_under_seed(self):
        df = label_frame()
        s1 = pc.build_sample(df, per_stratum=5, seed=42)
        s2 = pc.build_sample(df, per_stratum=5, seed=42)
        assert list(s1['pano_id']) == list(s2['pano_id'])
        s3 = pc.build_sample(df, per_stratum=5, seed=43)
        assert list(s1['pano_id']) != list(s3['pano_id'])

    def test_stratified_by_city_and_era(self):
        s = pc.build_sample(label_frame(), per_stratum=5, seed=42)
        counts = s.groupby(['city', 'era']).size()
        assert counts[('a-city', 'legacy')] == 5
        assert counts[('a-city', 'post179')] == 5
        assert counts[('b-city', 'post179')] == 5
        assert ('b-city', 'legacy') not in counts.index

    def test_one_row_per_pano_with_stored_dims(self):
        s = pc.build_sample(label_frame(), per_stratum=50, seed=1)
        assert s['pano_id'].is_unique
        assert set(s.columns) >= {'pano_id', 'city', 'era', 'stored_width', 'stored_height'}


class TestRecordExtraction:

    @staticmethod
    def fake_pano(width=16384, height=8192, pitch_rad=0.02, roll_rad=-0.01, with_depth=True):
        return SimpleNamespace(
            image_sizes=[SimpleNamespace(x=512, y=256), SimpleNamespace(x=width, y=height)],
            pitch=pitch_rad, roll=roll_rad, heading=1.0,
            date=SimpleNamespace(year=2022, month=5),
            depth=SimpleNamespace(data=np.zeros((256, 512))) if with_depth else None)

    def test_extracts_the_pinned_fields_in_degrees(self):
        r = pc.extract_record(self.fake_pano())
        assert r['found'] is True
        assert r['served_width'] == 16384 and r['served_height'] == 8192
        assert r['pitch_deg'] == pytest.approx(np.degrees(0.02))
        assert r['roll_deg'] == pytest.approx(np.degrees(-0.01))
        assert r['has_depth'] is True
        assert r['capture_date'] == '2022-05'

    def test_missing_pano_is_a_not_found_record(self):
        r = pc.extract_record(None)
        assert r['found'] is False and r['served_width'] is None

    def test_absent_optional_fields_do_not_crash(self):
        p = self.fake_pano(with_depth=False)
        p.date = None
        r = pc.extract_record(p)
        assert r['has_depth'] is False and r['capture_date'] is None


class TestJsonWriting:
    """The 2026-08-09 census shipped 4,916 bare `NaN` tokens: valid to Python, invalid JSON to
    everyone else. These pin the scrub and the writer that now refuse it."""

    @staticmethod
    def frame_with_gaps():
        return pd.DataFrame([
            {'pano_id': 'p1', 'served_width': 16384.0, 'pitch_deg': 1.0, 'error': None},
            {'pano_id': 'p2', 'served_width': None, 'pitch_deg': None, 'error': 'ValueError: x'},
        ])

    def test_the_naive_pandas_scrub_does_not_work(self):
        """Discrimination for the fix: this is the line the census used to ship, and it produces
        NaN, not None, on a float column. If pandas ever changes this, the fix can be simplified —
        but silently keeping the old line must never pass."""
        df = self.frame_with_gaps()
        naive = df.where(pd.notna(df), None).to_dict(orient='records')
        assert naive[1]['served_width'] != naive[1]['served_width'], 'expected NaN, got a real None'

    def test_json_records_produces_real_nones(self):
        recs = pc.json_records(self.frame_with_gaps())
        assert recs[1]['served_width'] is None
        assert recs[1]['pitch_deg'] is None
        assert recs[0]['error'] is None
        assert recs[0]['served_width'] == 16384.0
        assert recs[1]['error'] == 'ValueError: x'

    def test_json_records_output_is_strict_json(self):
        import json
        text = json.dumps({'records': pc.json_records(self.frame_with_gaps())}, allow_nan=False)
        assert 'NaN' not in text

    def test_write_json_refuses_a_nan_rather_than_emitting_it(self, tmp_path):
        import json
        target = tmp_path / 'out.json'
        with pytest.raises(ValueError):
            pc.write_json({'x': float('nan')}, str(target))
        pc.write_json({'x': None}, str(target))
        assert json.loads(target.read_text(encoding='utf-8')) == {'x': None}

    def test_write_json_uses_lf_newlines(self, tmp_path):
        """A committed artifact must not change wholesale depending on who regenerated it."""
        target = tmp_path / 'out.json'
        pc.write_json({'a': 1, 'b': [1, 2]}, str(target))
        assert b'\r\n' not in target.read_bytes()


class TestResummarize:
    """The offline regeneration path — how the roll-wrap fix reached the committed numbers without
    a refetch, and now also how a NaN-token file gets repaired. Previously untested."""

    @staticmethod
    def census_file(tmp_path, records, summary=None):
        import json
        p = tmp_path / 'census.json'
        p.write_text(json.dumps({'source': 's', 'seed': 1,
                                 'summary': summary or {'stale': True},
                                 'records': records}), encoding='utf-8')
        return p

    def records(self):
        return [
            {'pano_id': 'p1', 'city': 'a', 'era': 'mid', 'stored_width': 16384.0,
             'stored_height': 8192.0, 'found': True, 'served_width': 16384, 'served_height': 8192,
             'pitch_deg': 359.9, 'roll_deg': 0.5, 'has_depth': True, 'capture_date': '2020-01'},
            {'pano_id': 'p2', 'city': 'a', 'era': 'mid', 'stored_width': 16384.0,
             'stored_height': 8192.0, 'found': False, 'served_width': None, 'served_height': None,
             'pitch_deg': None, 'roll_deg': None, 'has_depth': None, 'capture_date': None},
        ]

    def test_it_recomputes_the_summary_from_the_embedded_records(self, tmp_path):
        p = self.census_file(tmp_path, self.records())
        out = pc.resummarize(str(p))
        assert 'stale' not in out['summary']
        assert out['summary']['n_sampled'] == 2
        assert out['summary']['alive_pct'] == pytest.approx(50.0)
        # 359.9 must come back wrapped, which is the bug this path was built to fix
        assert out['summary']['tilt']['abs_pitch_p50_deg'] == pytest.approx(0.1, abs=0.01)

    def test_it_writes_the_new_summary_back_to_disk(self, tmp_path):
        import json
        p = self.census_file(tmp_path, self.records())
        pc.resummarize(str(p))
        assert json.loads(p.read_text(encoding='utf-8'))['summary']['n_sampled'] == 2

    def test_it_leaves_the_records_intact(self, tmp_path):
        """resummarize must never be a data-losing operation — the records are the raw measurement
        and the whole reason the same sample can be re-fetched later to measure decay."""
        p = self.census_file(tmp_path, self.records())
        out = pc.resummarize(str(p))
        assert [r['pano_id'] for r in out['records']] == ['p1', 'p2']
        assert out['records'][0]['pitch_deg'] == 359.9

    def test_it_repairs_a_file_written_with_bare_nan_tokens(self, tmp_path):
        """Running the fixed code over a pre-fix artifact is what repaired the committed census."""
        import json
        p = tmp_path / 'census.json'
        p.write_text(json.dumps({'summary': {}, 'records': [
            {'pano_id': 'p1', 'city': 'a', 'era': 'mid', 'stored_width': 1.0, 'stored_height': 1.0,
             'found': True, 'served_width': 1, 'served_height': 1, 'pitch_deg': 0.1,
             'roll_deg': 0.1, 'has_depth': True, 'capture_date': None, 'error': float('nan')}]}),
            encoding='utf-8')
        assert 'NaN' in p.read_text(encoding='utf-8')
        pc.resummarize(str(p))
        text = p.read_text(encoding='utf-8')
        assert 'NaN' not in text
        assert json.loads(text)['records'][0]['error'] is None


class TestOneRenderingOfTheSummary:
    """The two entry points printed the same summary two different ways. `--resummarize` ran the tilt
    quantiles through `fmt`; the live path interpolated them raw, so the *same records* printed
    `None/None/None` on one path and `n/a/n/a/n/a` on the other, and `1.2345678901` against `1.23`
    when the values were there. The unfixed copy was the live one — the path that has just spent a
    network census, under a comment saying exactly that.

    So the test is not "does it format nicely" but "do the two paths agree", which is the property
    that was false and the one that goes on being true only if there is one renderer.
    """

    @staticmethod
    def _records(alive=1, dead=1, tilt=True, errored=0):
        rows = [{'pano_id': f'a{i}', 'city': 'a-city', 'era': 'mid', 'stored_width': 16384.0,
                 'stored_height': 8192.0, 'found': True, 'served_width': 16384,
                 'served_height': 8192, 'pitch_deg': 1.2345678901 if tilt else None,
                 'roll_deg': 0.5 if tilt else None, 'has_depth': True,
                 'capture_date': '2020-01', 'error': None} for i in range(alive)]
        rows += [{'pano_id': f'd{i}', 'city': 'a-city', 'era': 'mid', 'stored_width': 16384.0,
                  'stored_height': 8192.0, 'found': False, 'served_width': None,
                  'served_height': None, 'pitch_deg': None, 'roll_deg': None, 'has_depth': None,
                  'capture_date': None, 'error': 'boom' if i < errored else None}
                 for i in range(dead)]
        return rows

    def _summary(self, **kwargs):
        return pc.summarize(pd.DataFrame(self._records(**kwargs)))

    def test_the_tilt_line_is_rounded_not_raw(self, capsys):
        """The live path printed the full float. Two decimals, on both paths."""
        pc.print_summary(self._summary())
        printed = capsys.readouterr().out
        assert '1.23' in printed and '1.2345678901' not in printed

    def test_the_alive_line_is_rounded_too(self, capsys):
        """One alive of three is 33.333333333333336, which is what an unformatted percentage looks
        like next to a formatted one."""
        pc.print_summary(self._summary(alive=1, dead=2))
        printed = capsys.readouterr().out
        assert 'alive 33.3%' in printed and '33.33' not in printed

    def test_absent_tilt_prints_n_a_rather_than_None(self, capsys):
        """A census whose alive panos carry no tilt is reachable — `_tilt_stats` returns None for
        every quantile — and `None` in a column of numbers is what `studyfmt` exists to prevent."""
        pc.print_summary(self._summary(tilt=False))
        printed = capsys.readouterr().out
        assert 'n/a' in printed and 'None' not in printed

    def test_an_all_dead_census_prints_n_a_for_every_optional_value(self, capsys):
        """Nothing alive means no comparable dims, no depth share and no tilt — six optional values
        at once, and the state a thin or badly-aged sample actually reaches."""
        summary = self._summary(alive=0, dead=2)
        assert summary['dims_drift_pct_of_alive'] is None
        assert summary['depth_available_pct_of_alive'] is None
        pc.print_summary(summary)
        printed = capsys.readouterr().out
        assert 'None' not in printed
        assert printed.count('n/a') == 8, 'two rate columns and six tilt quantiles'

    def test_the_error_count_is_printed(self, capsys):
        """It was on the live line and not the offline one — the same asymmetry, in the direction
        that hides how much of a low alive rate is ambiguity rather than decay."""
        pc.print_summary(self._summary(alive=1, dead=1, errored=1))
        assert 'errors 1' in capsys.readouterr().out

    def test_the_two_entry_points_print_the_same_thing(self, tmp_path, capsys, monkeypatch):
        """The property the drift broke, end to end: one set of records, two CLI paths, identical
        output. The live path is stubbed at `run_census` so no network is touched."""
        import json
        records = [
            {'pano_id': 'p1', 'city': 'a-city', 'era': 'mid', 'stored_width': 16384.0,
             'stored_height': 8192.0, 'found': True, 'served_width': 16384, 'served_height': 8192,
             'pitch_deg': 1.2345678901, 'roll_deg': 0.5, 'has_depth': True, 'capture_date': '2020-01'},
            {'pano_id': 'p2', 'city': 'a-city', 'era': 'mid', 'stored_width': 16384.0,
             'stored_height': 8192.0, 'found': False, 'served_width': None, 'served_height': None,
             'pitch_deg': None, 'roll_deg': None, 'has_depth': None, 'capture_date': None},
        ]
        census = tmp_path / 'census.json'
        census.write_text(json.dumps({'source': 's', 'seed': 1, 'summary': {}, 'records': records}),
                          encoding='utf-8')
        pc.main(['--resummarize', str(census)])
        offline = capsys.readouterr().out

        monkeypatch.setattr(pc, 'run_census', lambda sample, interval: pd.DataFrame(records))
        csv_dir = tmp_path / 'raw'
        csv_dir.mkdir()
        _write_rawlabels_csv(csv_dir / 'a-city.csv', records)
        pc.main([str(csv_dir), '--fetched', '2026-08-11', '--per-stratum', '2'])
        live = capsys.readouterr().out

        # The live path prints a sample line first; the summary below it must match exactly.
        assert live.endswith(offline), f'live:\n{live}\noffline:\n{offline}'
        assert 'alive 50.0%' in offline


class TestCommittedFindings:
    """The census conclusions, pinned against the committed JSON (offline). A re-fetch will
    drift these slowly (panos keep dying); the pins hold the summarize() output over the
    committed records, which is deterministic."""

    @pytest.fixture(scope='class')
    @classmethod
    def summary(cls):
        import json
        path = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-09-photometa-census.json')
        with open(path) as f:
            return json.load(f)['summary']

    def test_half_the_labeled_panos_are_gone(self, summary):
        assert 40.0 <= summary['alive_pct'] <= 60.0
        by_era = summary['by_era']
        assert by_era['legacy']['alive_pct'] < by_era['mid']['alive_pct'] \
            < by_era['post179']['alive_pct']

    def test_alive_panos_serve_their_stored_frame(self, summary):
        assert summary['dims_drift_pct_of_alive'] < 1.0
        assert summary['depth_available_pct_of_alive'] >= 99.0

    def test_the_tilt_prior_is_degree_scale_and_wrapped(self, summary):
        t = summary['tilt']
        assert 1.5 <= t['abs_pitch_p90_deg'] <= 4.0
        assert 1.5 <= t['abs_roll_p90_deg'] <= 3.0
        # the wrap regression guard: an unwrapped summary reported |roll| p99 ~ 360
        assert t['abs_roll_p99_deg'] < 10.0


class TestSummarize:

    def test_alive_drift_and_tilt_accounting(self):
        recs = pd.DataFrame([
            {'pano_id': 'p1', 'city': 'a', 'era': 'legacy', 'stored_width': 16384.0, 'stored_height': 8192.0,
             'found': True, 'served_width': 16384, 'served_height': 8192,
             'pitch_deg': 1.0, 'roll_deg': 0.5, 'has_depth': True, 'capture_date': '2020-01'},
            {'pano_id': 'p2', 'city': 'a', 'era': 'legacy', 'stored_width': 13312.0, 'stored_height': 6656.0,
             'found': True, 'served_width': 16384, 'served_height': 8192,
             'pitch_deg': -2.0, 'roll_deg': 1.5, 'has_depth': False, 'capture_date': '2020-01'},
            {'pano_id': 'p3', 'city': 'a', 'era': 'post179', 'stored_width': 16384.0, 'stored_height': 8192.0,
             'found': False, 'served_width': None, 'served_height': None,
             'pitch_deg': None, 'roll_deg': None, 'has_depth': None, 'capture_date': None},
        ])
        s = pc.summarize(recs)
        assert s['n_sampled'] == 3
        assert s['alive_pct'] == pytest.approx(200 / 3)
        assert s['dims_drift_pct_of_alive'] == pytest.approx(50.0)
        assert s['depth_available_pct_of_alive'] == pytest.approx(50.0)
        assert s['tilt']['abs_pitch_p50_deg'] == pytest.approx(1.5)
        assert s['by_era']['legacy']['alive_pct'] == pytest.approx(100.0)
        assert s['by_era']['post179']['alive_pct'] == pytest.approx(0.0)
        assert s['dims_comparable'] == 2 and s['dims_unknown_stored'] == 0

    def test_a_pano_with_no_stored_dims_is_not_counted_as_drift(self):
        """`NaN != x` is True in pandas, so an uncomparable pano would book as drift and inflate
        the rate. Both rows below serve exactly what a known stored frame would be; only one has
        a stored frame recorded, so the answer must be 0% drift over 1 comparable pano."""
        recs = pd.DataFrame([
            {'pano_id': 'p1', 'city': 'a', 'era': 'mid', 'stored_width': 16384.0,
             'stored_height': 8192.0, 'found': True, 'served_width': 16384, 'served_height': 8192,
             'pitch_deg': 0.5, 'roll_deg': 0.5, 'has_depth': True, 'capture_date': None},
            {'pano_id': 'p2', 'city': 'a', 'era': 'mid', 'stored_width': np.nan,
             'stored_height': np.nan, 'found': True, 'served_width': 16384, 'served_height': 8192,
             'pitch_deg': 0.5, 'roll_deg': 0.5, 'has_depth': True, 'capture_date': None},
        ])
        s = pc.summarize(recs)
        assert s['dims_comparable'] == 1
        assert s['dims_unknown_stored'] == 1
        assert s['dims_drift_pct_of_alive'] == pytest.approx(0.0)

    def test_errors_count_against_alive_and_are_sized_separately(self):
        """alive_pct is a floor: an errored request reads as not-found. errors_pct is how much
        room that leaves."""
        recs = pd.DataFrame([
            {'pano_id': 'p1', 'city': 'a', 'era': 'mid', 'stored_width': 1.0, 'stored_height': 1.0,
             'found': True, 'served_width': 1, 'served_height': 1, 'pitch_deg': 0.1,
             'roll_deg': 0.1, 'has_depth': True, 'capture_date': None, 'error': None},
            {'pano_id': 'p2', 'city': 'a', 'era': 'mid', 'stored_width': 1.0, 'stored_height': 1.0,
             'found': False, 'served_width': None, 'served_height': None, 'pitch_deg': None,
             'roll_deg': None, 'has_depth': None, 'capture_date': None, 'error': 'ValueError: x'},
        ])
        s = pc.summarize(recs)
        assert s['alive_pct'] == pytest.approx(50.0)
        assert s['errors'] == 1
        assert s['errors_pct'] == pytest.approx(50.0)

    def test_wrapped_angles_read_as_small_tilts(self):
        """Google serves roll in [0, 360); a roll of 359.9 deg is a -0.1 deg tilt, and the
        summary must wrap before taking magnitudes — an unwrapped |359.9| would poison every
        percentile. (Found live: the first census run reported |roll| p90 = 359.6.)"""
        recs = pd.DataFrame([
            {'pano_id': 'p1', 'city': 'a', 'era': 'mid', 'stored_width': 1.0,
             'stored_height': 1.0, 'found': True, 'served_width': 1, 'served_height': 1,
             'pitch_deg': 359.9, 'roll_deg': 359.9, 'has_depth': True, 'capture_date': None},
            {'pano_id': 'p2', 'city': 'a', 'era': 'mid', 'stored_width': 1.0,
             'stored_height': 1.0, 'found': True, 'served_width': 1, 'served_height': 1,
             'pitch_deg': 0.3, 'roll_deg': -0.3, 'has_depth': True, 'capture_date': None},
        ])
        t = pc.summarize(recs)['tilt']
        assert t['abs_roll_p50_deg'] == pytest.approx(0.2, abs=0.01)
        assert t['abs_pitch_p90_deg'] < 0.3 + 1e-6
