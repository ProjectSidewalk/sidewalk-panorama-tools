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


def write_rawlabels_csv(path, rows):
    """A rawLabels-shaped CSV carrying only what the loader reads; unset columns stay blank."""
    import rawlabels as rl
    df = pd.DataFrame(rows)
    for c in rl.STUDY_COLUMNS:
        if c not in df:
            df[c] = np.nan
    df[rl.STUDY_COLUMNS].to_csv(path, index=False)
    return str(path)


def label_row(i, az, el, user, pano='p1', width=8192.0, height=4096.0, agree=2, disagree=0):
    return {'label_id': i, 'user_id': user, 'pano_id': pano, 'label_type': 'CurbRamp',
            'time_created': int(pd.Timestamp('2024-01-01', tz='UTC').value // 10 ** 6) + i,
            'agree_count': agree, 'disagree_count': disagree, 'unsure_count': 0,
            'pano_x': az / 360.0 * width, 'pano_y': height / 2 - el / 90.0 * (height / 2),
            'pano_width': width, 'pano_height': height}


class TestLoadCity:
    """The row filter behind the study's "2 corrupt negative-y rows" claim. Previously uncalled."""

    def test_it_keeps_sound_rows_and_drops_unusable_geometry(self, tmp_path):
        rows = [
            label_row(1, 100.0, -10.0, 'u1'),                       # keep
            label_row(2, 100.1, -10.0, 'u2'),                       # keep
            dict(label_row(3, 100.0, -10.0, 'u3'), pano_y=-720.0),  # negative y (the real corruption)
            dict(label_row(4, 100.0, -10.0, 'u4'), pano_y=5000.0),  # y past the frame bottom
            dict(label_row(5, 100.0, -10.0, 'u5'), pano_width=0.0),  # non-positive dims
            dict(label_row(6, 100.0, -10.0, 'u6'), pano_height=np.nan),
            dict(label_row(7, 100.0, -10.0, 'u7'), pano_x=np.nan),
        ]
        kept = cns.load_city(write_rawlabels_csv(tmp_path / 'c.csv', rows))
        assert sorted(kept['label_id']) == [1, 2]

    def test_the_frame_edges_are_inclusive_at_the_top(self, tmp_path):
        """pano_y == 0 is the top row and is legitimate; only negative y is corruption."""
        rows = [dict(label_row(1, 100.0, 0.0, 'u1'), pano_y=0.0)]
        assert len(cns.load_city(write_rawlabels_csv(tmp_path / 'c.csv', rows))) == 1


class TestStudyEndToEnd:
    """The orchestrator that produced the committed summary. It had no test until 2026-08-10, so
    a mistake here would have been blessed by the committed-findings pins rather than caught."""

    @pytest.fixture
    def csv_dir(self, tmp_path):
        d = tmp_path / 'raw'
        d.mkdir()
        rows, i = [], 0
        # 30 two-user duplicate clusters, all validated-correct.
        for k in range(30):
            az, el = 20 + k * 10, -20.0
            rows += [label_row(i := i + 1, az, el, 'u1', pano=f'p{k}'),
                     label_row(i := i + 1, az + 0.2, el - 0.2, 'u2', pano=f'p{k}')]
        # 10 more clusters that are NOT validated-correct (disagree wins): these must be present in
        # `overall` and absent from `validated_only`. Put them in a different depression band so
        # the band split is discriminating too (el -8 -> depression 8 deg -> the 5-15 band).
        for k in range(30, 40):
            az, el = 20 + (k - 30) * 10, -8.0
            rows += [label_row(i := i + 1, az, el, 'u1', pano=f'p{k}', agree=0, disagree=3),
                     label_row(i := i + 1, az + 0.2, el - 0.2, 'u2', pano=f'p{k}', agree=0,
                               disagree=3)]
        write_rawlabels_csv(d / 'atown.csv', rows)
        return str(d)

    def test_it_reports_every_section(self, csv_dir):
        out = cns.study(csv_dir)
        assert out['n_labels'] == 80
        assert out['primary_radius_deg'] == cns.PRIMARY_RADIUS_DEG
        assert out['overall']['n_pairs'] == 40
        assert set(out['radius_sweep']) == {f'{r:g}' for r in cns.RADIUS_SWEEP}
        assert set(out['by_depression_band']) == {'0-5deg', '5-15deg', '15-90deg'}

    def test_pairs_land_in_the_right_depression_band(self, csv_dir):
        """30 clusters at 20 deg depression, 10 at 8 deg — and the bands must total the overall
        count, which is what proves nothing fell outside them."""
        bands = cns.study(csv_dir)['by_depression_band']
        assert bands['0-5deg']['n_pairs'] == 0
        assert bands['5-15deg']['n_pairs'] == 10
        assert bands['15-90deg']['n_pairs'] == 30
        assert sum(b['n_pairs'] for b in bands.values()) == 40

    def test_validated_only_is_computed_on_the_validated_subset(self, csv_dir):
        """The discrimination the committed-findings pin lacked: if `study` accidentally fed the
        full frame to the validated-only branch, sigma alone could not tell (see the note on the
        committed run, where the two medians land on the same pixel atom) — but the pair count can.
        Here 30 of 40 clusters are validated-correct."""
        out = cns.study(csv_dir)
        assert out['validated_only']['n_pairs'] == 30
        assert out['validated_only']['n_pairs'] < out['overall']['n_pairs']

    def test_the_radius_sweep_is_monotone_in_pair_count(self, csv_dir):
        out = cns.study(csv_dir)
        counts = [out['radius_sweep'][f'{r:g}']['n_pairs'] for r in sorted(cns.RADIUS_SWEEP)]
        assert counts == sorted(counts)


class TestCommittedFindings:
    """The study's conclusions, pinned against the committed summary JSON (offline)."""

    @pytest.fixture(scope='class')
    @classmethod
    def summary(cls):
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
        placement noise, not a misplacement tail that validators would have culled.

        On the committed run the two sigma_el values are *bit-identical*, which is real and not a
        copy-paste: d_el is quantised to 180/8192 = 0.0220 deg per pixel, and both medians land on
        the same 22-pixel atom. Because a `rel=0.15` check cannot distinguish that from the
        validated-only branch having been fed the wrong frame, the pair counts are asserted too."""
        assert summary['validated_only']['sigma_el_deg'] == pytest.approx(
            summary['overall']['sigma_el_deg'], rel=0.15)
        assert summary['validated_only']['n_pairs'] < summary['overall']['n_pairs']
        assert summary['validated_only']['n_clusters'] < summary['overall']['n_clusters']

    def test_the_median_lands_on_a_whole_pixel_atom(self, summary):
        """Why the two sigmas coincide, stated as an assertion rather than a footnote."""
        median_abs_d_el = summary['overall']['sigma_el_deg'] / (1.4826 / 2 ** 0.5)
        pixels = median_abs_d_el / (180.0 / 8192)
        assert pixels == pytest.approx(round(pixels), abs=1e-6)

    def test_the_depression_bands_account_for_every_pair(self, summary):
        """The bands start at 0 deg, so any pair whose cluster sits ABOVE the horizon is silently
        outside all three. The committed run has none (the sums match exactly); this assertion is
        what turns a future silent drop into a failure."""
        banded = sum(b['n_pairs'] for b in summary['by_depression_band'].values())
        assert banded == summary['overall']['n_pairs']


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
