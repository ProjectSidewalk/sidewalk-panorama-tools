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


class TestMonthlySeries:
    """The series behind the report's figure — the evidence that dates the fix to a single day.
    No test called it before 2026-08-10."""

    @staticmethod
    def _frame(rows):
        return pd.DataFrame([
            {'era': era, 'time_created': pd.Timestamp(t, tz='UTC'),
             'replayable_x': True, 'replayable_y': True, 'exact_x': ex, 'exact_y': ey}
            for era, t, ex, ey in rows])

    def test_it_buckets_by_month_and_reports_percentages(self):
        """The x and y rates must differ within a month, or a swapped column reads as correct —
        which is exactly the mutant that survived the first version of this test."""
        s = ers.monthly_series(self._frame([
            ('post179', '2024-09-03', False, False),
            ('post179', '2024-09-14', False, True),
            ('post179', '2024-09-21', True, True),
            ('post179', '2024-09-28', True, True),
            ('post179', '2024-10-02', False, True),
            ('post179', '2024-10-19', True, True),
        ]))
        assert sorted(s) == ['2024-09', '2024-10']
        assert s['2024-09'] == {'n': 4, 'exact_x_pct': 50.0, 'exact_y_pct': 75.0}
        assert s['2024-10'] == {'n': 2, 'exact_x_pct': 50.0, 'exact_y_pct': 100.0}

    def test_it_covers_only_post179_rows(self):
        """legacy/mid rows must not leak into a post-179 client-behaviour series."""
        s = ers.monthly_series(self._frame([
            ('legacy', '2019-05-02', False, False),
            ('mid', '2022-05-02', False, False),
            ('post179', '2024-10-02', True, True),
        ]))
        assert list(s) == ['2024-10']

    def test_an_unreplayable_month_reports_none_not_zero(self):
        """A month with no replayable rows has no rate; reporting 0% would read as total failure."""
        df = self._frame([('post179', '2025-02-01', False, False)])
        df['replayable_x'] = df['replayable_y'] = False
        s = ers.monthly_series(df)
        assert s['2025-02'] == {'n': 1, 'exact_x_pct': None, 'exact_y_pct': None}


class TestPostFixDriftSignature:

    def test_the_decomposition_runs_on_post_fix_rows(self):
        """The report attributes residual post-fix x misses to camera_heading drift; that claim
        was previously argued only from the pre-179 decomposition. study_city now runs the same
        test on the population the claim is about."""
        rows = []
        for pano, delta in (('a', 2.0), ('b', -1.5)):
            for k, t in enumerate(('2025-01-01', '2025-02-01')):
                rows.append({'pano_id': pano, 'era': 'post179',
                             'time_created': pd.Timestamp(t, tz='UTC'),
                             'exact_x': False, 'dx_deg': delta + 0.001 * (1 if k else -1)})
        post_fix = pd.DataFrame(rows)
        out = ers.drift_decomposition(post_fix)
        assert out['n_panos'] == 2 and out['n_labels'] == 4
        assert out['median_within_pano_sigma_deg'] < 0.01
        assert out['across_pano_sigma_deg'] > 1.0

    def test_in_window_rows_are_excluded_from_the_post_fix_slice(self):
        """The slice study_city passes is post-179 AND on/after BUG_WINDOW_END; an in-window row
        must not contaminate it."""
        assert ers.BUG_WINDOW_END == pd.Timestamp('2024-09-26', tz='UTC')
        df = pd.DataFrame({
            'era': ['post179'] * 2,
            'time_created': pd.to_datetime(['2024-09-25', '2024-09-26'], utc=True),
        })
        sliced = df[(df['era'] == 'post179') & (df['time_created'] >= ers.BUG_WINDOW_END)]
        assert len(sliced) == 1


class TestStudyCity:
    """The per-city assembler that produced every committed number, previously uncalled by tests."""

    @staticmethod
    def _csv(tmp_path, rows):
        """Write a rawLabels-shaped CSV with only the columns the loader reads."""
        cols = rl.STUDY_COLUMNS
        df = pd.DataFrame(rows)
        for c in cols:
            if c not in df:
                df[c] = np.nan
        df[cols].to_csv(tmp_path / 'city.csv', index=False)
        return str(tmp_path / 'city.csv')

    def _rows(self):
        """Three self-consistent post-fix rows plus one legacy row, built by the forward math so
        the replay must be exact, and one row with no pano dims so the missing-counters move."""
        base = TestReplayFrame._forward_frame()
        rows = []
        for i in range(4):
            ts = ['2019-05-01', '2025-01-01', '2025-01-02', '2025-02-01'][i]
            rows.append({
                'label_id': i, 'user_id': f'u{i}', 'pano_id': base['pano_id'][i],
                'label_type': 'CurbRamp',
                'time_created': int(pd.Timestamp(ts, tz='UTC').value // 10 ** 6),
                'heading': base['heading'][i], 'pitch': base['pitch'][i], 'zoom': base['zoom'][i],
                'canvas_x': base['canvas_x'][i], 'canvas_y': base['canvas_y'][i],
                'canvas_width': 720.0, 'canvas_height': 480.0,
                'pano_x': base['pano_x'][i], 'pano_y': base['pano_y'][i],
                'pano_width': 8192.0, 'pano_height': 4096.0,
                'camera_heading': 100.0, 'camera_pitch': 0.0,
            })
        rows.append(dict(rows[-1], label_id=99, pano_id='p9', pano_width=np.nan,
                         pano_height=np.nan))
        return rows

    def test_it_assembles_every_section(self, tmp_path):
        out = ers.study_city(self._csv(tmp_path, self._rows()))
        assert set(out) >= {'n_labels', 'date_range', 'era_counts', 'missing', 'fixed_frame_rows',
                            'nonstandard_canvas_rows', 'eras', 'post179_bug_window',
                            'post179_monthly', 'drift_signature_pre179', 'drift_signature_post_fix'}
        assert out['n_labels'] == 5
        assert out['date_range'] == ['2019-05-01', '2025-02-01']
        assert out['era_counts'] == {'post179': 4, 'legacy': 1}

    def test_self_consistent_rows_replay_exactly(self, tmp_path):
        out = ers.study_city(self._csv(tmp_path, self._rows()))
        assert out['eras']['post179']['exact_y_pct'] == pytest.approx(100.0)
        assert out['eras']['legacy']['exact_x_pct'] == pytest.approx(100.0)
        assert out['post179_bug_window']['post_fix']['exact_x_pct'] == pytest.approx(100.0)

    def test_a_row_without_dims_is_counted_missing_and_not_replayed(self, tmp_path):
        """Blank must stay blank: the row is excluded from the replay base, not scored as a miss.
        This is the accounting that reconciles 438,410 corpus labels to 436,348 censused."""
        out = ers.study_city(self._csv(tmp_path, self._rows()))
        assert out['missing']['pano_dims'] == 1
        assert out['eras']['post179']['n'] == 4
        assert out['eras']['post179']['replayable_y'] == 3

    def test_the_canvas_and_fixed_frame_counters(self, tmp_path):
        rows = self._rows()
        rows[0]['canvas_width'] = 1024.0
        rows[1]['pano_width'], rows[1]['pano_height'] = 13312.0, 6656.0
        out = ers.study_city(self._csv(tmp_path, rows))
        assert out['nonstandard_canvas_rows'] == 1  # only the 1024-wide row; missing pano dims
        assert out['fixed_frame_rows']['n'] == 1    # do not make a canvas nonstandard
        assert out['fixed_frame_rows']['by_era'] == {'post179': 1}

    def test_it_separates_pre179_drift_from_post_fix_drift(self, tmp_path):
        """Three panos with the same +2 deg camera_heading drift, one per window. Each drift key
        must see only its own population, and the in-window pano must land in neither — that is
        the whole point of adding the post-fix decomposition alongside the pre-179 one."""
        base = TestReplayFrame._forward_frame()
        rows = []
        windows = (('pre', '2019-05-01'), ('inw', '2024-01-15'), ('pf', '2025-03-01'))
        for w, (pano, ts) in enumerate(windows):
            for j in range(2):
                i = w * 2 + j
                rows.append({
                    'label_id': i, 'user_id': 'u', 'pano_id': pano, 'label_type': 'CurbRamp',
                    'time_created': int(pd.Timestamp(ts, tz='UTC').value // 10 ** 6) + j,
                    'heading': base['heading'][i], 'pitch': base['pitch'][i],
                    'zoom': base['zoom'][i], 'canvas_x': base['canvas_x'][i],
                    'canvas_y': base['canvas_y'][i], 'canvas_width': 720.0,
                    'canvas_height': 480.0, 'pano_x': base['pano_x'][i],
                    'pano_y': base['pano_y'][i], 'pano_width': 8192.0, 'pano_height': 4096.0,
                    'camera_heading': 102.0, 'camera_pitch': 0.0,  # served 2 deg off production
                })
        out = ers.study_city(self._csv(tmp_path, rows))

        assert out['drift_signature_pre179']['n_panos'] == 1
        assert out['drift_signature_pre179']['n_labels'] == 2
        assert out['drift_signature_post_fix']['n_panos'] == 1
        assert out['drift_signature_post_fix']['n_labels'] == 2
        # Both recover the constant-within-pano signature. The bound is one pano-x pixel expressed
        # in degrees (360/8192 = 0.0439) — stored pano_x is an integer, so two labels sharing one
        # true drift can still round to neighbouring columns. That rounding floor is exactly what
        # the study means by "within-pano sigma at rounding-noise level".
        one_pixel_deg = 360.0 / 8192
        for key in ('drift_signature_pre179', 'drift_signature_post_fix'):
            assert out[key]['median_within_pano_sigma_deg'] <= one_pixel_deg, key
        # y is untouched by camera_heading in every window
        assert out['eras']['post179']['exact_y_pct'] == pytest.approx(100.0)

    def test_a_missing_canvas_width_counts_as_nonstandard(self):
        """Documented behaviour, not an accident: a NaN canvas fails the == 720 test, so it lands
        in nonstandard_canvas_rows. Production has zero such rows, so this only ever matters if a
        future export starts omitting the column."""
        df = ers.replay_frame(TestReplayFrame._forward_frame())
        df.loc[0, 'canvas_width'] = np.nan
        assert int((~((df['canvas_width'] == 720.0) & (df['canvas_height'] == 480.0))).sum()) == 1


class TestCommittedFindings:
    """The study's conclusions, pinned against the committed summary JSON (offline; reruns of the
    analysis on a fresh fetch may shift decimals, but these properties are the findings)."""

    @pytest.fixture(scope='class')
    @classmethod
    def summary(cls):
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
