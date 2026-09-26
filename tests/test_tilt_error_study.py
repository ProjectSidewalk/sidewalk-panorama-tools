"""Tests for reports/scripts/tilt_error_study.py — the #54 tilt study's analysis.

Two jobs, split as in the other report tests:

* the study's logic on synthetic frames whose answers are known by construction (TestFit ...
  TestConventions) - these are what prove anything about the code;
* the committed conclusions pinned against reports/data/2026-09-26-tilt-error-study.json, and every
  number the report quotes found in the report (TestCommittedFindings, TestReportMatchesTheArtifact).
  A committed-artifact test cannot catch a code regression - the artifact was produced by the code -
  which is why each pin sits beside a synthetic test of the function that produced it, and why
  TestTheArtifactIsTheCode re-runs `analyze` on the committed data and demands the committed JSON.
"""

import ast
import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import CropRunner  # noqa: E402
import tilt_error_study as tes  # noqa: E402
import tilt_frame as tf  # noqa: E402
import tilt_geometry as tg  # noqa: E402
from PIL import Image  # noqa: E402

SUMMARY_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-26-tilt-error-study.json')
REPORT_MD = os.path.join(REPO_ROOT, 'reports', '2026-09-26-tilt-error-study.md')
STUDY_PY = os.path.join(SCRIPTS, 'tilt_error_study.py')
DATA = os.path.join(REPO_ROOT, 'reports', 'data')


def _clustered(beta_p, beta_r, n_groups=200, per=12, noise=0.3, group_sd=0.0, seed=0):
    rng = np.random.default_rng(seed)
    g = np.repeat(np.arange(n_groups), per)
    xp = rng.normal(0, 2, g.size)
    xr = rng.normal(0, 1.5, g.size)
    u = rng.normal(0, noise, g.size) + rng.normal(0, group_sd, n_groups)[g] * np.sign(xp)
    return beta_p * xp + beta_r * xr + u, xp, xr, g


class TestFit:
    @pytest.mark.parametrize('bp, br', [(1.0, -1.0), (0.0, 0.0), (1.0, 1.0)])
    def test_recovers_planted_coefficients(self, bp, br):
        y, xp, xr, g = _clustered(bp, br)
        f = tes.fit_two_coefficient(y, xp, xr, g)
        assert f['beta_p'] == pytest.approx(bp, abs=0.03)
        assert f['beta_r'] == pytest.approx(br, abs=0.03)
        assert f['ci_p'][0] < bp < f['ci_p'][1]
        assert f['n'] == y.size and f['n_clusters'] == 200

    def test_cluster_robust_se_grows_with_within_cluster_correlation(self):
        y0, xp, xr, g = _clustered(1, 1, noise=0.3, group_sd=0.0, seed=1)
        y1, *_ = _clustered(1, 1, noise=0.3, group_sd=0.6, seed=1)
        assert tes.fit_two_coefficient(y1, xp, xr, g)['se_p'] > 2 * tes.fit_two_coefficient(y0, xp, xr, g)['se_p']

    def test_wrong_sign_mutant_is_detected(self):
        y, xp, xr, g = _clustered(1.0, 1.0)
        f = tes.fit_two_coefficient(-y, xp, xr, g)
        assert f['ci_p'][1] < -0.9 and f['ci_r'][1] < -0.9


def _synthetic_facades(frame, n_panos=150, seed=2):
    """Plumb facades (gravity-horizontal normals) expressed in the artifact frame, either through the
    rig rotation or not."""
    rng = np.random.default_rng(seed)
    pose, fac = [], []
    for i in range(n_panos):
        p, r = rng.normal(0, 2.5), rng.normal(0, 1.5)
        pid = 'p%04d' % i
        pose.append(dict(city='x', pano_id=pid, pitch_deg=p, roll_deg=r, npz_present=1))
        for b in rng.uniform(-180, 180, 4):
            bb, el = (tg.gravity_to_rig(b, 0.0, p, r) if frame == 'rig' else (b, 0.0))
            v = tg.direction_rfu(bb, el) * rng.choice([-1, 1])    # normals are unoriented
            n = -v                                                 # RFU -> artifact (x left, y back, z down)
            fac.append(dict(city='x', pano_id=pid, plane_index=1, support_px=500, n_x=n[0], n_y=n[1], n_z=n[2], d=5))
    return pd.DataFrame(fac), pd.DataFrame(pose)


class TestFacadeFrame:
    def test_rig_frame_planes_give_beta_one(self):
        f = tes.facade_frame_fit(*_synthetic_facades('rig'))
        assert f['beta_p'] == pytest.approx(1, abs=0.01) and f['beta_r'] == pytest.approx(1, abs=0.01)

    def test_gravity_frame_planes_give_beta_zero(self):
        f = tes.facade_frame_fit(*_synthetic_facades('gravity'))
        assert abs(f['beta_p']) < 0.01 and abs(f['beta_r']) < 0.01


def _synthetic_lean(beta, attenuation=0.7, offset_sd=1.0, n=120, seed=3, beta_r=None, noise=0.2, cal_deg=2.0):
    rng = np.random.default_rng(seed)
    rows, pose = [], []
    centres = (np.arange(12) + 0.5) / 12 * 360 - 180
    for i in range(n):
        p, r = rng.normal(0, 3), rng.normal(0, 2)
        off = rng.normal(0, offset_sd)
        pid = 'q%04d' % i
        pose.append(dict(city='x', pano_id=pid, pitch=p, roll=r))
        for variant, ep, er in (('base', 0, 0), ('cal_pitch', cal_deg, 0), ('cal_roll', 0, cal_deg)):
            for k, c in enumerate(centres):
                br = beta if beta_r is None else beta_r
                true = tg.vertical_lean_deg(c, beta * p, br * r) + tg.vertical_lean_deg(c, ep, er)
                rows.append(dict(arm='a', city='x', pano_id=pid, variant=variant, bin=k, bin_centre_deg=c,
                                 extra_pitch=ep, extra_roll=er, lean_deg=attenuation * true + off + rng.normal(0, noise)))
    return pd.DataFrame(rows), pd.DataFrame(pose)


class TestTileFrame:
    def test_pano_fixed_effects_remove_a_per_pano_offset(self):
        lean, pose = _synthetic_lean(1.0, attenuation=1.0, offset_sd=5.0)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert f['raw']['beta_p'] == pytest.approx(1, abs=0.05)

    def test_calibration_divides_out_attenuation(self):
        lean, pose = _synthetic_lean(1.0, attenuation=0.6)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert f['raw']['beta_p'] == pytest.approx(0.6, abs=0.05)
        assert f['a_pitch'] == pytest.approx(0.6, abs=0.05)
        assert f['beta_p_calibrated'] == pytest.approx(1.0, abs=0.08)
        assert f['beta_r_calibrated'] == pytest.approx(1.0, abs=0.08)

    def test_fixed_effects_matter_when_bins_are_missing(self):
        """With every bin present the per-pano mean of the regressors is zero and a pano offset is
        harmless; with bins missing (a sky-only sector yields None) an offset that scales with the
        pose - a scene effect of steep streets - biases a fit without fixed effects."""
        rng = np.random.default_rng(9)
        lean, pose = _synthetic_lean(1.0, attenuation=1.0, offset_sd=0.0, seed=9)
        pose_map = pose.set_index('pano_id')
        lean['lean_deg'] += 3.0 * lean['pano_id'].map(pose_map['pitch'])
        lean = lean[(lean['variant'] != 'base') | (rng.random(len(lean)) > 0.4)]
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert f['raw']['beta_p'] == pytest.approx(1, abs=0.1)

    def test_calibration_uses_only_this_arms_panos(self):
        """Two eras share one lean file; each era's attenuation must come from its own panos."""
        a, pa = _synthetic_lean(1.0, attenuation=0.6, seed=4)
        b, pb = _synthetic_lean(1.0, attenuation=0.3, seed=5)
        b['pano_id'] = 'z' + b['pano_id']
        pb['pano_id'] = 'z' + pb['pano_id']
        lean = pd.concat([a, b], ignore_index=True)
        f = tes.tile_frame_fit(lean, pa.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert f['a_pitch'] == pytest.approx(0.6, abs=0.05)

    def test_flags(self):
        lean, pose = _synthetic_lean(1.0, attenuation=0.6)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert f['gravity_band_excluded'] and not f['saturated'] and f['raw_excludes_zero']
        lean, pose = _synthetic_lean(0.0, attenuation=0.6)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert f['gravity_band_excluded'] is False and not f['raw_excludes_zero']
        lean, pose = _synthetic_lean(1.0, attenuation=0.05)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert f['saturated']
        assert f['gravity_band_excluded'] is None      # calibrated CIs mean nothing when saturated
        assert f['raw_excludes_zero']                  # the raw rejection still reads

    def test_gravity_exclusion_needs_both_axes(self):
        """Pitch rig-aligned, roll levelled: one calibrated CI above the band is not enough."""
        lean, pose = _synthetic_lean(1.0, attenuation=0.6, beta_r=0.0)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert f['ci_p_calibrated'][0] > 0.3 and f['ci_r_calibrated'][0] < 0.3
        assert f['gravity_band_excluded'] is False

    def test_gravity_exclusion_reads_the_lower_bounds(self):
        """A calibrated CI straddling the band edge (0.3) does not exclude it."""
        lean, pose = _synthetic_lean(0.3, attenuation=0.6, noise=1.5, seed=12, n=40)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}), n_boot=0)
        for ax in ('p', 'r'):                      # both upper bounds above the edge, both lower below it
            assert f['ci_%s_calibrated' % ax][0] < 0.3 < f['ci_%s_calibrated' % ax][1]
        assert f['gravity_band_excluded'] is False

    def test_the_calibration_tilt_is_read_from_the_rows(self):
        """The instrument writes the tilt it added; if it added 3 deg, a is measured against 3, not an
        assumed 2 (a 2-deg constant would read a 1.5x too high here)."""
        lean, pose = _synthetic_lean(1.0, attenuation=0.6, cal_deg=3.0)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert f['a_pitch'] == pytest.approx(0.6, abs=0.05) and f['a_roll'] == pytest.approx(0.6, abs=0.05)

    def test_bootstrap_with_unit_weights_is_the_point_estimate(self):
        """The bootstrap's sufficient statistics reproduce tile_frame_fit exactly at weight 1, so a
        replicate is the same estimator, resampled."""
        lean, pose = _synthetic_lean(0.8, attenuation=0.5)
        pose = pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'})
        f = tes.tile_frame_fit(lean, pose, n_boot=0)
        base = lean[lean['variant'] == 'base']
        c = np.radians(base['bin_centre_deg'].to_numpy(float))
        pm = base['pano_id'].map(pose.set_index('pano_id')['pitch_deg']).to_numpy(float)
        rm = base['pano_id'].map(pose.set_index('pano_id')['roll_deg']).to_numpy(float)
        key = ('x/' + base['pano_id']).to_numpy()
        y, xp, xr = (tes._demean(v, key) for v in (base['lean_deg'], -pm * np.sin(c), rm * np.cos(c)))
        b = tes.calibrated_bootstrap(y, xp, xr, key, lean, weights=np.ones((1, len(np.unique(key)))))
        assert b['p'][0] == pytest.approx(f['beta_p_calibrated'], rel=1e-9)
        assert b['r'][0] == pytest.approx(f['beta_r_calibrated'], rel=1e-9)

    def test_a_bootstrap_replicate_is_the_fit_on_the_resampled_panos(self):
        """Weight 1 on half the panos and 0 on the rest must equal tile_frame_fit on that half, the
        calibration slopes included - a bootstrap that held `a` fixed at the full sample would not."""
        lean, pose = _synthetic_lean(0.8, attenuation=0.5, n=60, seed=21)
        cal = lean['variant'] != 'base'                          # the two halves calibrate differently
        lean.loc[cal, 'lean_deg'] += (np.where(lean.loc[cal, 'pano_id'] < 'q0030', 0.3, -0.3)
                                      * np.sin(np.radians(lean.loc[cal, 'bin_centre_deg'])))
        pose = pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'})
        half = sorted(pose['pano_id'])[:30]
        want = tes.tile_frame_fit(lean[lean['pano_id'].isin(half)], pose[pose['pano_id'].isin(half)], n_boot=0)
        base = lean[lean['variant'] == 'base']
        c = np.radians(base['bin_centre_deg'].to_numpy(float))
        pm = base['pano_id'].map(pose.set_index('pano_id')['pitch_deg']).to_numpy(float)
        rm = base['pano_id'].map(pose.set_index('pano_id')['roll_deg']).to_numpy(float)
        key = ('x/' + base['pano_id']).to_numpy()
        y, xp, xr = (tes._demean(v, key) for v in (base['lean_deg'], -pm * np.sin(c), rm * np.cos(c)))
        w = np.array([[1.0 if k[2:] in half else 0.0 for k in np.unique(key)]])
        b = tes.calibrated_bootstrap(y, xp, xr, key, lean, weights=w)
        assert b['p'][0] == pytest.approx(want['beta_p_calibrated'], rel=1e-9)
        assert b['r'][0] == pytest.approx(want['beta_r_calibrated'], rel=1e-9)

    def test_bootstrap_ci_covers_and_widens_with_a_noisy_calibration(self):
        lean, pose = _synthetic_lean(1.0, attenuation=0.6)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}), n_boot=300)
        for ax in ('p', 'r'):
            lo, hi = f['ci_%s_calibrated_boot' % ax]
            assert lo < f['beta_%s_calibrated' % ax] < hi and lo < 1.0 < hi

    def test_gravity_tiles_give_zero(self):
        lean, pose = _synthetic_lean(0.0, attenuation=0.6)
        f = tes.tile_frame_fit(lean, pose.rename(columns={'pitch': 'pitch_deg', 'roll': 'roll_deg'}))
        assert abs(f['beta_p_calibrated']) < 0.08 and abs(f['beta_r_calibrated']) < 0.08


class TestVerdicts:
    def test_frame_verdict_rules(self):
        assert tes.frame_verdict((0.95, 1.05), (0.93, 1.02), rig=(0.9, 1.1), gravity=(-0.1, 0.1)) == 'rig'
        assert tes.frame_verdict((-0.05, 0.05), (-0.02, 0.04), rig=(0.9, 1.1), gravity=(-0.1, 0.1)) == 'gravity'
        assert tes.frame_verdict((0.5, 1.05), (0.93, 1.02), rig=(0.9, 1.1), gravity=(-0.1, 0.1)) == 'undecided'

    def test_c_direction_split(self):
        """The leak window is above the stored one when T > 0 and below when T < 0, so a judge who
        simply preferred rings lower in the frame could not produce a leak majority in both halves."""
        key = {'a': {'order': ['leak', 'stored', 'antileak'], 'era_arm': 'x', 'T_deg': 5.0},
               'b': {'order': ['leak', 'stored', 'antileak'], 'era_arm': 'x', 'T_deg': -5.0},
               'c': {'order': ['leak', 'stored', 'antileak'], 'era_arm': 'x', 'T_deg': -6.0}}
        d = tes.c_by_direction({'a': 'A', 'b': 'A', 'c': 'B'}, key)
        assert d['leak_above'] == {'n': 1, 'leak': 1, 'stored': 0, 'antileak': 0, 'none': 0}
        assert d['leak_below'] == {'n': 2, 'leak': 1, 'stored': 1, 'antileak': 0, 'none': 0}

    def test_sign_test_is_exact(self):
        assert tes.sign_test(37, 0)['p_one_sided'] == pytest.approx(0.5 ** 37)
        assert tes.sign_test(5, 5)['p_one_sided'] > 0.5
        assert tes.sign_test(0, 0)['p_one_sided'] is None

    def test_score_judge_splits_exposure_direction_and_xml_posed_post179(self):
        order = ['leak', 'stored', 'antileak']
        key = {'t1': dict(order=order, era_arm='post179', T_deg=5.0, scrape_era='xml'),
               't2': dict(order=order, era_arm='post179', T_deg=-5.0, scrape_era='modern'),
               't3': dict(order=order, era_arm='legacy+mid', T_deg=5.0, scrape_era='modern'),
               't4': dict(order=order, era_arm='legacy+mid', T_deg=-5.0, scrape_era='xml')}
        j = tes.score_judge({'t1': 'A', 't2': 'B', 't3': 'C', 't4': 'none'}, key, exposed=('t1', 't3'))
        assert j['exposed_in_figure'] == {'n': 2, 'stored': 0, 'leak': 1, 'antileak': 1, 'none': 0}
        assert j['not_exposed'] == {'n': 2, 'stored': 1, 'leak': 0, 'antileak': 0, 'none': 1}
        assert j['leak_vs_antileak'] == {'leak': 1, 'antileak': 1, 'p_one_sided': pytest.approx(0.75)}
        assert j['post179_without_xml_posed'] == {'n': 1, 'stored': 1, 'leak': 0, 'antileak': 0, 'none': 0}
        assert j['arms']['legacy+mid']['verdict'] == 'split'
        assert 'none_tokens' not in j['arms']['legacy+mid']          # per-token verdicts stay sealed
        assert j['arms']['post179']['posthoc_decisive_share_leak'] == pytest.approx(0.5)

    def test_rig_pixel_moves_in_x_off_the_horizon_only(self):
        base = dict(pano_width=2048.0, pano_height=1024.0, pitch_deg=0.0, roll_deg=5.0)
        on = dict(base, pano_x=1536.0, pano_y=512.0)
        on['T_deg'] = float(tg.tilt_term_deg(on['pano_x'] / 2048 * 360 - 180, 0.0, 5.0))
        low = dict(base, pano_x=1024.0, pano_y=700.0)
        low['T_deg'] = float(tg.tilt_term_deg(0.0, 0.0, 5.0))
        assert tes.rig_pixel_shift_stats({'a': on})['max_abs_dx_px'] < 0.5
        assert tes.rig_pixel_shift_stats({'b': low})['max_abs_dx_px'] > 3

    def test_c_decisive_share_excludes_none(self):
        a = {'n': 24, 'stored': 2, 'leak': 17, 'antileak': 0, 'none': 5}
        assert tes.posthoc_decisive_share(a, 'leak') == pytest.approx(17 / 19)
        assert tes.posthoc_decisive_share({'n': 1, 'stored': 0, 'leak': 0, 'antileak': 0, 'none': 1}, 'leak') is None

    def test_c_verdict_rules(self):
        assert tes.c_verdict({'n': 20, 'stored': 18, 'leak': 1, 'antileak': 0, 'none': 1}) == 'stored'
        assert tes.c_verdict({'n': 20, 'stored': 1, 'leak': 19, 'antileak': 0, 'none': 0}) == 'leak'
        assert tes.c_verdict({'n': 20, 'stored': 17, 'leak': 1, 'antileak': 0, 'none': 2}) == 'split'
        assert tes.c_verdict({'n': 0, 'stored': 0, 'leak': 0, 'antileak': 0, 'none': 0}) == 'no data'


class TestPriorAndMiscentering:
    """S1 and S3 on poses whose answer is known, so the tables are not pinned by the artifact alone."""

    @staticmethod
    def _pose(pitch, roll, era='modern'):
        return pd.DataFrame({'pose_source': 'npz', 'scrape_era': era, 'pose_pitch_deg': pitch,
                             'pose_roll_deg': roll})

    def test_pure_roll_at_the_side_gives_T_equal_to_roll(self):
        rolls = np.linspace(-6, 6, 101)
        pose = self._pose(np.full(101, 9.0), rolls)
        corpus = pd.DataFrame({'dbear': [90.0] * 10 + [-90.0] * 10 + [90.0] * 3,
                               'depression': [2.0] * 20 + [10.0, 20.0, 40.0]})     # every band populated
        row = tes.miscentering_table(corpus, pose)[0]
        assert row['band'] == '<5' and row['n_labels'] == 20
        assert row['T_p90_deg'] == pytest.approx(np.percentile(np.abs(rolls), 90), abs=1e-9)
        prior = tes.tilt_prior(pose, corpus['dbear'][:20])['modern']
        assert prior['abs_T_p90_deg'] == pytest.approx(np.percentile(np.abs(rolls), 90), abs=1e-9)
        assert prior['abs_pitch_p90_deg'] == pytest.approx(9.0)

    def test_pitch_ahead_and_the_p90_not_the_median(self):
        pitches = np.linspace(0, 10, 101)
        pose = self._pose(pitches, np.zeros(101))
        corpus = pd.DataFrame({'dbear': [0.0] * 5 + [90.0] * 3, 'depression': [20.0] * 5 + [2.0, 10.0, 40.0]})
        row = [r for r in tes.miscentering_table(corpus, pose) if r['band'] == '15-30'][0]
        assert row['T_p90_deg'] == pytest.approx(9.0)

    def test_prior_is_per_scrape_era(self):
        pose = pd.concat([self._pose(np.full(10, 1.0), np.zeros(10), 'xml'),
                          self._pose(np.full(10, 3.0), np.zeros(10), 'modern')], ignore_index=True)
        prior = tes.tilt_prior(pose, [0.0])
        assert prior['xml']['abs_pitch_p50_deg'] == pytest.approx(1.0)
        assert prior['modern']['abs_pitch_p50_deg'] == pytest.approx(3.0)

    def test_xml_convention_picks_the_committed_reading(self):
        rng = np.random.default_rng(4)
        yaw, tyaw, m = rng.uniform(0, 360, 200), rng.uniform(0, 360, 200), rng.uniform(0.5, 6, 200)
        p, r = tg.xml_tilt_to_pitch_roll(yaw, tyaw, m)
        overlap = pd.DataFrame({'xml_pano_yaw_deg': yaw, 'xml_tilt_yaw_deg': tyaw, 'xml_tilt_pitch_deg': m,
                                'pitch_deg': p, 'roll_deg': r})
        overlap.loc[:9, 'roll_deg'] += 3.0                       # ten re-renders
        overlap.loc[10:19, 'roll_deg'] += 0.7                    # ten small differences, under 1 deg
        s2 = tes.xml_npz_convention(overlap)
        assert s2['best'] == 'pitch=-m*cos(dir), roll=-m*sin(dir)'
        assert s2['n_vector_diff_over_1deg'] == 10 and s2['share_vector_diff_over_1deg'] == pytest.approx(0.05)
        assert s2['min_miss_of_other_readings_deg'] > 1.0


class TestMiscentering:
    def test_fraction_uses_the_v2_window_height(self):
        row = tes.miscentering_row('5-15', 10.0, t_p90_deg=2.0, pano_height=8192)
        y = 4096 + 10.0 * 8192 / 180
        height = CropRunner.crop_window_fov_deg(y, 8192) / CropRunner.CROP_ASPECT_W_OVER_H
        assert row['window_height_deg'] == pytest.approx(height)
        assert row['ceiling_fraction_of_height'] == pytest.approx(2.0 / height)

    def test_ceiling_row_is_beta_one(self):
        row = tes.miscentering_row('<5', 2.5, t_p90_deg=3.0, pano_height=8192)
        assert row['ceiling_shift_deg'] == pytest.approx(3.0)
        assert row['ceiling_shift_px_8192'] == pytest.approx(3.0 * 8192 / 180)

    def test_bands_are_the_prereg_bands(self):
        assert tes.BANDS == ('<5', '5-15', '15-30', '>30')
        assert list(tes.band_of(np.array([0.0, 4.99, 5.0, 15.0, 29.9, 30.0]))) == \
            ['<5', '<5', '5-15', '15-30', '15-30', '>30']


@pytest.fixture(scope='module')
def tree():
    with open(STUDY_PY, encoding='utf-8') as f:
        return ast.parse(f.read())


class TestExtentGold:
    def test_pairs_need_x_inside_and_y_near_the_box_bottom(self):
        """RampNet boxes are normalised (cx, cy, w, h); a PS label pairs with one when its x is inside
        the box's span and its y within one box-height of the box's bottom edge."""
        boxes = {'panos': {'P1': {'det:0': {'status': 'boxed', 'cx': 0.5, 'cy': 0.6, 'w': 0.02, 'h': 0.02},
                                  'det:1': {'status': 'skipped', 'cx': 0.2, 'cy': 0.6, 'w': 0.02, 'h': 0.02}}}}
        labels = pd.DataFrame([
            dict(label_id=1, pano_id='P1', label_type='CurbRamp', pano_x=0.505 * 1000, pano_y=0.62 * 500,
                 pano_width=1000, pano_height=500),                          # in
            dict(label_id=2, pano_id='P1', label_type='CurbRamp', pano_x=0.52 * 1000, pano_y=0.61 * 500,
                 pano_width=1000, pano_height=500),                          # x outside
            dict(label_id=3, pano_id='P1', label_type='CurbRamp', pano_x=0.5 * 1000, pano_y=0.66 * 500,
                 pano_width=1000, pano_height=500),                          # 2 box-heights below
            dict(label_id=4, pano_id='P1', label_type='NoCurbRamp', pano_x=0.5 * 1000, pano_y=0.61 * 500,
                 pano_width=1000, pano_height=500),                          # wrong type
            dict(label_id=5, pano_id='P1', label_type='CurbRamp', pano_x=0.2 * 1000, pano_y=0.61 * 500,
                 pano_width=1000, pano_height=500),                          # only an unboxed detection
        ])
        pairs = tes.extent_gold_pairs('x', boxes, labels)
        assert [p['label_uid'] for p in pairs] == ['x:1']


class TestConventions:
    def test_no_local_fmt_or_percentile(self, tree):
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert not names & {'fmt', 'num', 'percentile'}
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == 'studyfmt'
                    for a in n.names}
        assert {'fmt', 'num'} <= imported

    def test_label_uid_is_city_and_id(self):
        df = pd.DataFrame({'city': ['a', 'b'], 'label_id': [7, 7]})
        assert list(tes.label_uid(df)) == ['a:7', 'b:7']
        with pytest.raises(AssertionError):
            tes.label_uid(pd.DataFrame({'city': ['a', 'a'], 'label_id': [7, 7]}))

    def test_every_merge_validates(self, tree):
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, 'attr', None) == 'merge':
                assert any(k.arg == 'validate' for k in n.keywords), 'merge at line %d lacks validate=' % n.lineno

    def test_populations_claim_every_figure_exactly_once(self):
        summary = {'populations': {'a': {'keys': ['f1', 's1']}, 'b': {'keys': ['f2']}},
                   'f1': {}, 'f2': {}, 's1': {}, 'generated_from': {}, 'conventions': {}, 'wrong_turns': []}
        tes.check_populations(summary)
        summary['populations']['b']['keys'].append('f1')
        with pytest.raises(AssertionError):
            tes.check_populations(summary)
        del summary['populations']['b']
        with pytest.raises(AssertionError):
            tes.check_populations(summary)

    def test_artifact_is_strict_json(self, tmp_path):
        with pytest.raises(ValueError):
            tes.write_json({'x': float('nan')}, str(tmp_path / 'x.json'))


# ---- committed findings (pinned to the artifact) ------------------------------------------------

@pytest.fixture(scope='module')
def summary():
    if not os.path.exists(SUMMARY_JSON):
        pytest.skip('artifact not generated yet')
    with open(SUMMARY_JSON, encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def report():
    if not os.path.exists(REPORT_MD):
        pytest.skip('report not written yet')
    with open(REPORT_MD, encoding='utf-8') as f:
        return ' '.join(f.read().split())


class TestCommittedFindings:
    def test_populations_are_complete(self, summary):
        tes.check_populations(summary)

    def test_f1_depth_planes_are_in_the_rig_frame(self, summary):
        for arm in summary['f1_depth_frame'].values():
            assert arm['verdict'] == 'rig'
            assert 0.9 < arm['fit']['ci_p'][0] and arm['fit']['ci_p'][1] < 1.1
            assert 0.9 < arm['fit']['ci_r'][0] and arm['fit']['ci_r'][1] < 1.1

    def test_f2_arms_have_calibrations(self, summary):
        for name, arm in summary['f2_tile_frame'].items():
            assert arm['n_panos'] >= 150, name
            if name.startswith('store_top'):
                assert arm['saturated'], name          # outside the estimator's linear range
                continue
            assert not arm['saturated'] and 0.2 < arm['a_pitch'] < 1.0, name

    def test_f2_tiles_are_not_gravity_levelled(self, summary):
        """Read on the RAW slopes, in every arm: levelled tiles predict 0 whatever the calibration."""
        for name, arm in summary['f2_tile_frame'].items():
            assert arm['raw_excludes_zero'] and arm['raw_min_z'] > 5, name
            if arm['saturated']:
                assert arm['gravity_band_excluded'] is None, name
            else:
                assert arm['gravity_band_excluded'], name

    def test_c_preliminary_verdicts_favour_the_leak_window(self, summary):
        """The machine pass only (not decision-bearing): split by the pre-set rule in both arms, leak
        against antileak 37:0, and leak wins both directions."""
        judge = summary['c_adjudication']['judges']['claude-opus-5-5']
        assert not judge['decision_bearing']
        assert {a['verdict'] for a in judge['arms'].values()} == {'split'}
        assert judge['leak_vs_antileak']['antileak'] == 0 and judge['leak_vs_antileak']['p_one_sided'] < 1e-10
        assert summary['c_adjudication']['status'] == 'awaiting Jon'
        assert len(summary['c_adjudication']['exposed_in_figure']) == 8
        for arm in judge['arms'].values():
            assert arm['leak'] > 2 * (arm['stored'] + arm['antileak'])
            assert arm['antileak'] == 0
        for d in judge['by_direction'].values():
            assert d['leak'] > d['n'] / 2

    def test_s2_convention_is_the_committed_function(self, summary):
        s2 = summary['s2_xml_npz']
        assert s2['best'] == 'pitch=-m*cos(dir), roll=-m*sin(dir)'
        assert s2['median_abs_dpitch_deg'] <= 0.3 and s2['median_abs_droll_deg'] <= 0.3

    def test_s1_prior_is_wrapped(self, summary):
        for era in summary['s1_tilt_prior']['by_scrape_era'].values():
            assert era['abs_roll_p90_deg'] < 20     # the photometa census's unwrapped 359.6 cannot recur

    def test_c_counts_reconcile(self, summary):
        for judge in summary['c_adjudication']['judges'].values():
            for arm in judge['arms'].values():
                assert arm['stored'] + arm['leak'] + arm['antileak'] + arm['none'] == arm['n']


class TestReportMatchesTheArtifact:
    def test_every_quoted_number_is_in_the_markdown(self, summary, report):
        missing = [(k, v) for k, v in tes.report_numbers(summary).items() if v not in report]
        assert not missing, missing

    def test_wrong_turns_are_listed(self, summary, report):
        for turn in summary['wrong_turns']:
            head = ' '.join(turn.split()[:8])
            assert head in report, head

    def test_deviations_are_listed(self, summary, report):
        for dev in summary['deviations']:
            assert ' '.join(dev.split()) in report, dev[:60]

    def test_a_value_off_by_one_is_not_found(self, summary, report):
        """The context is what makes the check bite: bump the last digit of every quoted value and the
        fragment must no longer be in the report (a bare-substring check let 8 of 110 through)."""
        import re
        survivors = []
        for k, frag in tes.report_numbers(summary).items():
            m = list(re.finditer(r'\d', frag))
            if not m:
                continue
            i = m[-1].start()
            bumped = frag[:i] + str((int(frag[i]) + 1) % 10) + frag[i + 1:]
            if bumped in report:
                survivors.append((k, bumped))
        assert not survivors, survivors

    def test_c_is_not_headlined_past_its_rule(self, report):
        for claim in ('sits at the rig pixel, not the stored', 'slope is a lower bound', 'lower bounds, not estimates',
                      'the ceiling is the likely value'):
            assert claim not in report, claim


class TestTheArtifactIsTheCode:
    """The one place the quoted numbers meet the code that makes them (#158 review item 6: twelve
    mutants to the analysis changed the committed JSON with every other test green)."""

    def test_the_artifact_regenerates_from_committed_data(self, tmp_path):
        out = tmp_path / 'regen.json'
        r = subprocess.run([sys.executable, STUDY_PY, 'analyze', '--write', str(out)], cwd=REPO_ROOT,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]
        with open(out, encoding='utf-8') as f:
            regen = json.load(f)
        with open(SUMMARY_JSON, encoding='utf-8') as f:
            committed = json.load(f)
        assert regen == committed

    def test_f1_sign_is_the_modules_rotation_on_committed_facades(self):
        """F1's measured sign, tied to tilt_geometry's actual rotation rather than to a hand-typed pin:
        a plumb facade at rig bearing b sits at rig elevation gravity_to_rig(b, 0, pitch, roll)[1], and
        the committed Seattle facades must follow that prediction with slope 1."""
        fac = pd.read_csv(os.path.join(DATA, '2026-09-26-tilt-facades.csv.gz'), dtype={'pano_id': str})
        pose = pd.read_csv(os.path.join(DATA, '2026-09-26-tilt-pose-seattle.csv.gz'), dtype={'pano_id': str})
        f = fac[fac['source'] == 'seattle'].merge(pose[pose['npz_present'] == 1][['city', 'pano_id', 'pitch_deg', 'roll_deg']],
                                                  on=['city', 'pano_id'], how='inner', validate='many_to_one')
        f = f[np.isfinite(f['pitch_deg']) & np.isfinite(f['roll_deg'])]
        b, el = tg.artifact_normal_bearing_elevation(f[['n_x', 'n_y', 'n_z']].to_numpy(float))
        pred = np.asarray(tg.gravity_to_rig(b, np.zeros_like(b), f['pitch_deg'].to_numpy(float),
                                            f['roll_deg'].to_numpy(float))[1])
        slope = float(np.dot(el, pred) / np.dot(pred, pred))
        assert len(f) > 20000
        assert 0.99 <= slope <= 1.01, slope


def _render_poles(w, h, pitch, roll):
    ys, xs = np.mgrid[0:h, 0:w]
    b, el = tg.bearing_elevation_from_pixel(xs + 0.5, ys + 0.5, w, h)
    bg, elg = tg.rig_to_gravity(b, el, pitch, roll)
    img = np.zeros((h, w))
    for pb in np.arange(-165, 180, 30.0):
        d = np.abs(tg.wrap_deg(bg - pb)) * np.cos(np.radians(elg))
        img = np.maximum(img, np.clip(0.5 + (1.5 - d) / (360.0 / w), 0, 1))
    img[np.abs(elg) > 40] = 0
    return (40 + 180 * img).astype(np.uint8)


def test_instrument_to_analysis_calibration_end_to_end(tmp_path):
    """tilt_frame.measure_one's rows, dropped to the columns export() keeps, straight into
    tile_frame_fit: on clean rig-frame synthetic panos a ~ 1. Nothing else ties the column contract
    between the instrument and the analysis together."""
    rng = np.random.default_rng(11)
    w, h = 1440, 720
    rows, pose = [], []
    for i in range(6):
        p, r = rng.uniform(-4, 4), rng.uniform(-3, 3)
        pid = 'ab%02d' % i
        (tmp_path / 'ab').mkdir(exist_ok=True)
        Image.fromarray(_render_poles(w, h, p, r)).save(tmp_path / 'ab' / (pid + '.jpg'), quality=95)
        rec = {'city': '', 'pano_id': pid, 'pitch_deg': p, 'roll_deg': r, 'arm': 't'}
        rows += tf.measure_one((rec, str(tmp_path / 'ab' / (pid + '.jpg')), w, True, 1))
        pose.append({'city': '', 'pano_id': pid, 'pitch_deg': p, 'roll_deg': r})
    lean = pd.DataFrame(rows)
    lean['lean_deg'] = pd.to_numeric(lean['lean_deg'], errors='coerce')
    lean = lean.drop(columns=['pitch_deg', 'roll_deg', 'predicted_lean_deg'])      # as export() does
    fit = tes.tile_frame_fit(lean, pd.DataFrame(pose), n_boot=0)
    assert 0.8 <= fit['a_pitch'] <= 1.25, fit['a_pitch']
    assert 0.8 <= fit['a_roll'] <= 1.25, fit['a_roll']
