"""Tests for reports/scripts/rawlabels.py + era_replay_study.py — the era/projection replay study.

The study's claims only mean something if the machinery discriminates: the wrapped seam diff, the
era bucketing at the two client boundaries, the self-consistency of a forward-then-replay round
trip, and the per-pano-constant drift decomposition are each pinned on synthetic frames where the
right answer is known by construction. The loader is pinned on five real Newberg rows
(tests/fixtures/rawlabels_newberg_head.csv, public /v3/api/rawLabels bytes, fetched 2026-08-09).
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import era_replay_study as ers  # noqa: E402
import pov_replay  # noqa: E402
import rawlabels as rl  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'fixtures', 'rawlabels_newberg_head.csv')


class TestLoader:

    def test_loads_the_fixture(self):
        """Five real Newberg rows: replay inputs present and typed, JSON/text columns dropped."""
        df = rl.load_rawlabels(FIXTURE)
        assert len(df) == 5
        for col in ('label_id', 'pano_id', 'label_type', 'canvas_x', 'canvas_y',
                    'canvas_width', 'canvas_height', 'heading', 'pitch', 'zoom',
                    'pano_x', 'pano_y', 'pano_width', 'pano_height',
                    'camera_heading', 'camera_pitch', 'camera_roll'):
            assert col in df.columns, col
        assert 'validations' not in df.columns  # big JSON blob, never loaded
        assert df['heading'].dtype == np.float64

    def test_time_created_is_utc_datetime(self):
        """time_created arrives as epoch milliseconds; the first fixture row is 2019-01-30 UTC."""
        df = rl.load_rawlabels(FIXTURE)
        assert str(df['time_created'].dt.tz) == 'UTC'
        assert df['time_created'].iloc[0].strftime('%Y-%m-%d') == '2019-01-30'

    def test_era_assignment_at_the_boundaries(self):
        """legacy < 2021-01-01 <= mid < 2023-03-29 (evolution 179) <= post179, boundaries UTC."""
        ms = lambda s: pd.Timestamp(s, tz='UTC').value // 10**6
        df = pd.DataFrame({'time_created': pd.to_datetime([
            ms('2020-12-31T23:59:59'), ms('2021-01-01T00:00:00'),
            ms('2023-03-28T23:59:59'), ms('2023-03-29T00:00:00')], unit='ms', utc=True)})
        assert list(rl.add_era(df)['era']) == ['legacy', 'mid', 'mid', 'post179']


class TestWrappedDx:

    def test_a_small_difference_is_itself(self):
        assert ers.wrapped_dx(np.array([100]), np.array([90]), np.array([8192]))[0] == 10

    def test_the_seam_wraps(self):
        """stored 5 vs replay 16379 on a 16384-wide pano is 10 px clockwise, not -16374."""
        assert ers.wrapped_dx(np.array([5]), np.array([16379]), np.array([16384]))[0] == 10

    def test_half_width_lands_positive(self):
        """The convention is (-w/2, w/2]: an exact half-turn reads +w/2."""
        assert ers.wrapped_dx(np.array([4096]), np.array([0]), np.array([8192]))[0] == 4096


class TestReplayFrame:

    @staticmethod
    def _forward_frame(camera_heading_stored=None):
        """A synthetic frame whose pano_x/pano_y are produced by the forward math itself, with
        production camera_heading 100; optionally store drifted metadata to simulate what the API
        serves today."""
        n = 6
        rng = {'canvas_x': np.array([100.0, 360, 500, 20, 700, 360]),
               'canvas_y': np.array([100.0, 240, 400, 460, 30, 240]),
               'heading': np.array([10.0, 123.4, 250, 340, 80, 200]),
               'pitch': np.array([-20.0, -10.5, -30, -5, -15, -25]),
               'zoom': np.array([1.0, 1, 2, 3, 2, 1])}
        pov_h, pov_p = pov_replay.pov_if_centered(
            rng['canvas_x'], rng['canvas_y'], rng['heading'], rng['pitch'], rng['zoom'])
        px, py = pov_replay.pano_xy_from_pov(pov_h, pov_p, np.full(n, 100.0), 8192, 4096)
        df = pd.DataFrame(rng)
        df['canvas_width'], df['canvas_height'] = 720.0, 480.0
        df['pano_x'], df['pano_y'] = px, py
        df['pano_width'], df['pano_height'] = 8192, 4096
        df['camera_heading'] = 100.0 if camera_heading_stored is None else camera_heading_stored
        df['camera_pitch'] = 0.0
        df['pano_id'] = ['p1', 'p1', 'p1', 'p2', 'p2', 'p2']
        return df

    def test_the_round_trip_is_exact(self):
        """Stored values produced by the forward math replay bit-for-bit: the study's null case."""
        out = ers.replay_frame(self._forward_frame())
        assert out['exact_x'].all() and out['exact_y'].all()
        assert (out['dx'] == 0).all() and (out['dy'] == 0).all()

    def test_camera_heading_drift_shows_up_in_x_only(self):
        """Serve camera_heading 102 where production used 100: y still exact, and the implied
        drift (dx in degrees) recovers +2 deg on every row."""
        out = ers.replay_frame(self._forward_frame(camera_heading_stored=102.0))
        assert out['exact_y'].all()
        assert not out['exact_x'].any()
        assert out['dx_deg'].to_numpy() == pytest.approx(np.full(6, 2.0), abs=0.05)

    def test_a_row_without_camera_heading_is_not_replayable_in_x(self):
        """NaN camera_heading: x replay is NaN/not-exact, y replay still runs."""
        df = self._forward_frame()
        df.loc[0, 'camera_heading'] = np.nan
        out = ers.replay_frame(df)
        assert not out['exact_x'].iloc[0]
        assert out['exact_y'].iloc[0]
        assert np.isnan(out['dx_deg'].iloc[0])


class TestDriftDecomposition:

    def test_per_pano_constant_signature(self):
        """Two panos with constant deltas +2.0 and -1.5 (plus rounding jitter): within-pano sigma
        reads as noise, across-pano spread reads as real."""
        df = pd.DataFrame({
            'pano_id': ['a', 'a', 'a', 'b', 'b'],
            'exact_x': [False] * 5,
            'dx_deg': [2.001, 1.999, 2.000, -1.501, -1.499],
        })
        out = ers.drift_decomposition(df)
        assert out['n_panos'] == 2
        assert out['median_within_pano_sigma_deg'] < 0.01
        assert out['across_pano_sigma_deg'] > 1.0

    def test_single_label_panos_are_excluded(self):
        df = pd.DataFrame({'pano_id': ['a', 'b'], 'exact_x': [False, False],
                           'dx_deg': [2.0, -1.5]})
        assert ers.drift_decomposition(df)['n_panos'] == 0


class TestCommittedFindings:
    """The study's conclusions, pinned against the committed summary JSON (offline; reruns of the
    analysis on a fresh fetch may shift decimals, but these properties are the findings)."""

    @pytest.fixture(scope='class')
    def summary(self):
        import json
        path = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-09-era-replay-summary.json')
        with open(path) as f:
            return json.load(f)

    def test_six_cities_with_provenance(self, summary):
        assert summary['fetched'] == '2026-08-09'
        assert sorted(summary['cities']) == ['amsterdam', 'cdmx', 'columbus-oh',
                                             'newberg-or', 'oradell-nj', 'seattle-wa']

    def test_pano_y_is_exact_outside_the_bug_window(self, summary):
        """The projection itself is right: y replays >= 99.99% for legacy/mid rows in every city
        (the entire corpus has exactly 2 corrupt negative-pano_y rows, both Seattle mid-era), and
        >= 99.9% post-fix — the real misses live in the bug window."""
        for city, d in summary['cities'].items():
            for era in ('legacy', 'mid'):
                if era in d['eras']:
                    assert d['eras'][era]['exact_y_pct'] >= 99.99, (city, era)
            assert d['post179_bug_window']['post_fix']['exact_y_pct'] >= 99.9, city

    def test_the_bug_window_shows_in_every_city_with_volume(self, summary):
        """In-window y agreement drops below 96.5% everywhere the window has >= 500 labels."""
        for city, d in summary['cities'].items():
            w = d['post179_bug_window']['in_window']
            if w['n'] >= 500:
                assert w['exact_y_pct'] < 96.5, (city, w)
                assert w['exact_y_pct'] < d['post179_bug_window']['post_fix']['exact_y_pct'], city

    def test_pre179_x_misses_carry_the_drift_signature(self, summary):
        """Camera-heading drift, not projection error: within-pano sigma at rounding-noise level,
        across-pano sigma an order of magnitude larger, in every city with enough drifted panos."""
        for city, d in summary['cities'].items():
            sig = d['drift_signature_pre179']
            if sig['n_panos'] >= 40:
                assert sig['median_within_pano_sigma_deg'] <= 0.02, (city, sig)
                assert sig['across_pano_sigma_deg'] >= 5 * max(sig['median_within_pano_sigma_deg'], 0.01), (city, sig)


class TestWindowSplit:

    def test_the_bug_window_boundary(self):
        """2024-09-26 UTC splits post179: the last bad Seattle day (09-25, version bump 7.20.7)
        falls in_window; the first clean stretch falls post_fix."""
        df = pd.DataFrame({
            'era': ['post179'] * 4,
            'time_created': pd.to_datetime(['2024-09-25T23:59:59', '2024-09-26T00:00:00',
                                            '2023-04-01T00:00:00', '2025-01-01T00:00:00'], utc=True),
            'replayable_x': [True] * 4, 'replayable_y': [True] * 4,
            'exact_x': [False, True, False, True], 'exact_y': [False, True, False, True],
        })
        s = ers.window_split(df)
        assert s['in_window']['n'] == 2 and s['post_fix']['n'] == 2
        assert s['in_window']['exact_y_pct'] == pytest.approx(0.0)
        assert s['post_fix']['exact_y_pct'] == pytest.approx(100.0)


class TestSummarize:

    def test_per_era_rates(self):
        """Counts and exact rates aggregate by era; quantiles come from the mismatches."""
        df = pd.DataFrame({
            'era': ['legacy'] * 4 + ['post179'] * 2,
            'exact_x': [True, False, False, True, True, True],
            'exact_y': [True] * 6,
            'replayable_x': [True] * 6,
            'replayable_y': [True] * 6,
            'dx_deg': [0.0, 1.0, -3.0, 0.0, 0.0, 0.0],
            'dy': [0] * 6,
        })
        s = ers.summarize_eras(df)
        assert s['legacy']['n'] == 4
        assert s['legacy']['exact_x_pct'] == pytest.approx(50.0)
        assert s['post179']['exact_x_pct'] == pytest.approx(100.0)
        assert s['legacy']['abs_dx_deg_of_misses']['p50'] == pytest.approx(2.0)
        assert s['post179']['exact_y_pct'] == pytest.approx(100.0)
