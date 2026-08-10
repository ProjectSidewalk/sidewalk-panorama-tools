"""Tests for reports/scripts/click_noise_study.py — placement-noise measurement from co-located
same-type duplicate labels.

Every estimator property the study leans on is pinned on synthetic frames where the answer is known
by construction: the seam-aware clustering, the same-user dedup, the >= 2-distinct-users rule, and
recovery of a known injected sigma from the pair-difference estimator.
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

import click_noise_study as cns  # noqa: E402


def make_frame(az, el, users, pano='p', label_type='CurbRamp', width=8192, height=4096):
    """Rows from angular positions: pano_x/y computed from az/el on a standard frame."""
    az = np.asarray(az, float)
    el = np.asarray(el, float)
    return pd.DataFrame({
        'label_id': np.arange(len(az)),
        'pano_id': pano,
        'label_type': label_type,
        'user_id': users,
        'time_created': pd.to_datetime(np.arange(len(az)), unit='s', utc=True),
        'pano_x': az / 360.0 * width,
        'pano_y': height / 2 - el / 90.0 * (height / 2),
        'pano_width': float(width),
        'pano_height': float(height),
    })


class TestClustering:

    def test_close_labels_cluster_across_the_seam(self):
        """0.2 deg apart across azimuth 0/360: one cluster, not two."""
        df = make_frame([359.9, 0.1], [-10, -10], ['u1', 'u2'])
        out = cns.cluster_labels(df, radius_deg=0.5)
        assert out['cluster_id'].nunique() == 1

    def test_distant_labels_stay_separate(self):
        df = make_frame([100, 110], [-10, -10], ['u1', 'u2'])
        out = cns.cluster_labels(df, radius_deg=1.5)
        assert out['cluster_id'].nunique() == 2

    def test_azimuth_distance_shrinks_with_elevation(self):
        """Near the pole, 3 deg of azimuth is a small great-circle distance: at el=-80 it
        clusters at radius 1.0; at the horizon it does not."""
        near_pole = make_frame([100, 103], [-80, -80], ['u1', 'u2'])
        horizon = make_frame([100, 103], [0, 0], ['u1', 'u2'])
        assert cns.cluster_labels(near_pole, radius_deg=1.0)['cluster_id'].nunique() == 1
        assert cns.cluster_labels(horizon, radius_deg=1.0)['cluster_id'].nunique() == 2

    def test_different_types_never_cluster(self):
        df = pd.concat([make_frame([100], [-10], ['u1'], label_type='CurbRamp'),
                        make_frame([100.1], [-10], ['u2'], label_type='Obstacle')])
        out = cns.cluster_labels(df, radius_deg=1.5)
        assert out['cluster_id'].nunique() == 2


class TestPairs:

    def test_same_user_repeats_are_dropped(self):
        """A user's second label in a cluster is not a noise sample (it's a double-submit)."""
        df = cns.cluster_labels(make_frame([100, 100.1, 100.2], [-10, -10, -10],
                                           ['u1', 'u1', 'u2']), radius_deg=1.5)
        pairs = cns.cluster_pairs(df)
        assert len(pairs) == 1  # u1-vs-u2 once, not twice

    def test_single_user_clusters_yield_no_pairs(self):
        df = cns.cluster_labels(make_frame([100, 100.1], [-10, -10], ['u1', 'u1']),
                                radius_deg=1.5)
        assert len(cns.cluster_pairs(df)) == 0

    def test_pair_separation_is_greatcircleish(self):
        """A pure-elevation offset of 0.4 deg gives d_el = 0.4 and d_az = 0."""
        df = cns.cluster_labels(make_frame([100, 100], [-10.0, -10.4], ['u1', 'u2']),
                                radius_deg=1.5)
        pairs = cns.cluster_pairs(df)
        assert pairs['d_el'].iloc[0] == pytest.approx(0.4, abs=0.05)
        assert pairs['d_az'].iloc[0] == pytest.approx(0.0, abs=0.02)


class TestCommittedFindings:
    """The study's conclusions, pinned against the committed summary JSON (offline)."""

    @pytest.fixture(scope='class')
    def summary(self):
        import json
        path = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-09-click-noise-summary.json')
        with open(path) as f:
            return json.load(f)

    def test_the_corpus_is_big_enough_to_lean_on(self, summary):
        assert summary['overall']['n_pairs'] >= 10000
        assert summary['overall']['n_clusters'] >= 8000

    def test_per_axis_sigma_is_half_a_degree_scale(self, summary):
        """The number the pre-registration budgets against: per-axis placement noise between
        independent users is 0.3-0.6 deg depending on clustering radius, ~0.5 deg at the
        primary radius."""
        for axis in ('sigma_az_deg', 'sigma_el_deg'):
            assert 0.25 <= summary['overall'][axis] <= 0.75, axis
        sweep = [summary['radius_sweep'][r]['sigma_el_deg'] for r in ('0.75', '1', '1.5', '2')]
        assert 0.25 <= sweep[0] <= 0.40  # the tight-core estimate

    def test_sigma_grows_with_radius(self, summary):
        """No plateau across the sweep: the pair population is a mixture (click-noise core plus
        genuinely distinct neighbours), so a single sigma must be quoted with its radius."""
        sweep = [summary['radius_sweep'][r]['sigma_el_deg'] for r in ('0.75', '1', '1.5', '2')]
        assert sweep == sorted(sweep)
        assert sweep[-1] > sweep[0] * 1.5

    def test_validation_does_not_deflate_sigma(self, summary):
        """Restricting to validated-correct labels moves sigma_el < 15%: the estimate measures
        placement noise, not a misplacement tail that validators would have culled."""
        assert summary['validated_only']['sigma_el_deg'] == pytest.approx(
            summary['overall']['sigma_el_deg'], rel=0.15)


class TestSigmaRecovery:

    def test_known_sigma_comes_back(self):
        """Inject sigma = 0.3 deg per axis into 400 two-label clusters: the robust estimator
        recovers it within 15%."""
        rng = np.random.default_rng(7)
        frames = []
        for i in range(400):
            az0, el0 = rng.uniform(20, 340), rng.uniform(-40, -5)
            frames.append(make_frame(az0 + rng.normal(0, 0.3, 2) / np.cos(np.radians(el0)),
                                     el0 + rng.normal(0, 0.3, 2),
                                     ['u1', 'u2'], pano=f'p{i}'))
        df = cns.cluster_labels(pd.concat(frames, ignore_index=True), radius_deg=3.0)
        pairs = cns.cluster_pairs(df)
        s = cns.sigma_from_pairs(pairs)
        assert s['sigma_az_deg'] == pytest.approx(0.3, rel=0.15)
        assert s['sigma_el_deg'] == pytest.approx(0.3, rel=0.15)
        assert s['n_pairs'] == 400

    def test_outliers_do_not_move_the_robust_sigma(self):
        """Adding 5% wild pairs (5 deg apart) moves the robust sigma by < 15% — the measured
        shift is ~11% (a 5% outlier mass moves a median that much); a classical std would
        move several-fold, which is what this test discriminates against."""
        rng = np.random.default_rng(11)
        frames = []
        for i in range(400):
            el0 = rng.uniform(-40, -5)
            off = 5.0 if i < 20 else abs(rng.normal(0, 0.3))
            frames.append(make_frame([100, 100], [el0, el0 - off], ['u1', 'u2'], pano=f'p{i}'))
        df = cns.cluster_labels(pd.concat(frames, ignore_index=True), radius_deg=6.0)
        clean = cns.sigma_from_pairs(cns.cluster_pairs(df).iloc[20:])
        dirty = cns.sigma_from_pairs(cns.cluster_pairs(df))
        assert dirty['sigma_el_deg'] == pytest.approx(clean['sigma_el_deg'], rel=0.15)
