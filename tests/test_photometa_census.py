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


class TestCommittedFindings:
    """The census conclusions, pinned against the committed JSON (offline). A re-fetch will
    drift these slowly (panos keep dying); the pins hold the summarize() output over the
    committed records, which is deterministic."""

    @pytest.fixture(scope='class')
    def summary(self):
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
