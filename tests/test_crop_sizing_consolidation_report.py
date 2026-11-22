"""Tests for reports/scripts/crop_sizing_consolidation.py - the #186 consolidation of the crop-sizing evidence.

Split the way the v2 and v3 study tests are:

* the study's own logic, on synthetic ramps whose answers are known by construction (a pin against the
  committed artifact proves nothing about the code that produced it);
* the committed gold extract, and the reproduction of the v2, v3 and RampNet figures from it;
* the committed artifact regenerated from the committed inputs, which CI can do because the gold is
  committed here (`reports/data/2026-11-22-crop-sizing-consolidation/`);
* every number in the report's prose, transcribed from that artifact.

The criteria this study answers were committed before any of it ran:
`reports/plans/2026-11-22-issue-186-crop-sizing-consolidation-plan.md`.
"""

import copy
import csv
import gzip
import hashlib
import io
import json
import math
import os
import random
import re
import sys

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import CropRunner  # noqa: E402
import pano_pose  # noqa: E402
import crop_sizing_consolidation as cs  # noqa: E402
import crop_sizing_v3 as csv3  # noqa: E402

DATA_DIR = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-22-crop-sizing-consolidation')
SUMMARY_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-22-crop-sizing-consolidation.json')
REPORT_MD = os.path.join(REPO_ROOT, 'reports', '2026-11-22-crop-sizing-consolidation.md')
V2_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-19-crop-sizing-v2.json')
V3_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-26-crop-sizing-v3.json')
CITIES = ('richmond', 'sao_paulo', 'paterson', 'annapolis')


# A local absolute path: a drive letter (not inside a word, so https:// is not one) followed by one or two
# slashes or backslashes and a letter, or a POSIX home, or the user name. Built without writing a path out.
_LOCAL_PATH = re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:(?:' + re.escape(chr(92)) + r'{1,2}|/)[A-Za-z]'
                         r'|/Users/|/home/|jonf')


def _json_strings(obj):
    """Every string (key or value) in a decoded JSON document."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _json_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _json_strings(v)
    elif isinstance(obj, str):
        yield obj


def ramp(depression=10.0, bearing=0.0, pano_h=4096, box_w=200.0, box_h=60.0, pitch=0.0, roll=0.0,
         city='c', pano_id='p', key='det:0'):
    """A gold ramp at a known depression and bearing, its apron centred on the point."""
    pano_w = pano_h * 2
    x = (bearing + 180.0) / 360.0 * pano_w
    y = pano_h / 2 + CropRunner.elevation_deg_to_px(depression, pano_h)
    return {'city': city, 'pano_id': pano_id, 'key': key, 'kind': key.split(':')[0], 'provider': 'gsv',
            'pano_w': pano_w, 'pano_h': pano_h, 'x': x, 'y': y, 'box_w': box_w, 'box_h': box_h,
            'box_cx': x, 'box_cy': y, 'depression_deg': depression, 'pitch': pitch, 'roll': roll}


class TestStudyLogic:

    # -- the tilt model --------------------------------------------------------------------------

    @pytest.mark.parametrize('bearing,pitch,roll,sign', [(0.0, 2.0, 0.0, +1), (90.0, 0.0, 2.0, +1),
                                                          (180.0, 2.0, 0.0, -1), (-90.0, 0.0, 2.0, -1)])
    def test_the_modelled_stored_point_is_T_b_below_the_rig_pixel(self, bearing, pitch, roll, sign):
        """Derived from T(b) = pitch cos b + roll sin b alone (tilt.md), not from pano_pose: a gravity-
        horizontal direction sits T(b) ABOVE its rig row, so the gravity-frame pixel g is beta*T(b) of
        elevation BELOW the rig pixel r. b = 0, pitch +2, beta 0.95 gives g.y - r.y = +0.95*2*h/180. A
        swapped axis (pitch read at b = 90) or a flipped sign fails at least one row."""
        r = ramp(depression=10.0, bearing=bearing, pitch=pitch, roll=roll)
        gx, gy = cs.modelled_stored_point(r, beta=0.95)
        expected = sign * 0.95 * 2.0 * r['pano_h'] / 180.0
        assert gy - r['y'] == pytest.approx(expected, rel=0.02)

    def test_the_negated_pose_moves_the_point_the_other_way(self):
        r = ramp(depression=8.0, bearing=0.0, pitch=2.0)
        _, gy = cs.modelled_stored_point(r, beta=0.95)
        _, gneg = cs.modelled_stored_point(r, beta=0.95, sign=-1.0)
        assert gy - r['y'] == pytest.approx(r['y'] - gneg, rel=0.01)
        assert gy > r['y'] > gneg

    def test_the_multiplier_scales_the_pose(self):
        r = ramp(depression=8.0, bearing=0.0, pitch=1.0)
        _, g1 = cs.modelled_stored_point(r, beta=0.95)
        _, g3 = cs.modelled_stored_point(r, beta=0.95, multiplier=3.0)
        assert g3 - r['y'] == pytest.approx(3 * (g1 - r['y']), rel=0.01)

    def test_the_correction_returns_the_modelled_point_to_the_rig_pixel(self):
        """The round trip through production's own correction: corrected_pixel(g) = r. An identity of
        the inverse pair, so it is here beside the T(b) case above, not instead of it. Production's pose
        readers hand corrected_pixel a wrapped roll (pose_from_depth_artifact), so this does too."""
        rng = random.Random(3)
        for _ in range(25):
            r = ramp(depression=rng.uniform(-5, 40), bearing=rng.uniform(-180, 180),
                     pitch=rng.uniform(-4, 4), roll=rng.choice([rng.uniform(-4, 4), 359.2, 0.7]))
            gx, gy = cs.modelled_stored_point(r, beta=0.95)
            cx, cy = pano_pose.corrected_pixel(gx, gy, r['pano_w'], r['pano_h'], r['pitch'],
                                               pano_pose.wrap_deg(r['roll']), 0.95)
            assert (cx - r['x']) % r['pano_w'] == pytest.approx(0.0, abs=1e-6) or \
                (cx - r['x']) % r['pano_w'] == pytest.approx(r['pano_w'], abs=1e-6)
            assert cy == pytest.approx(r['y'], abs=1e-6)

    def test_roll_is_wrapped_before_beta_scales_it(self):
        """A raw record roll of 359.96 is -0.04 deg. Rotating by 359.96 is the same rotation, but
        0.95 * 359.96 is not 0.95 * -0.04, so an unwrapped roll moves g by about 18 degrees."""
        raw = ramp(depression=10.0, bearing=90.0, roll=359.96)
        wrapped = ramp(depression=10.0, bearing=90.0, roll=-0.04)
        assert cs.modelled_stored_point(raw, beta=0.95) == pytest.approx(
            cs.modelled_stored_point(wrapped, beta=0.95), abs=1e-6)

    def test_sizing_at_g_changes_the_window_on_a_posed_ramp(self):
        """Kills "size every configuration at r.y": with T != 0 and the window unclamped, S-grav's
        window differs from S-rig's, under both rules."""
        r = ramp(depression=12.0, bearing=0.0, pitch=3.0)
        for spec in (('v2', 2.5), ('v3', 5.8)):
            rig = cs.tilt_boxes([r], spec, 'S-rig')[0]
            grav = cs.tilt_boxes([r], spec, 'S-grav')[0]
            assert grav.width != rig.width, spec
            # Below the camera, a lower point is nearer, so its window is wider.
            assert grav.width > rig.width, spec

    def test_s_rig_is_the_rule_as_the_v3_study_cut_it(self):
        r = ramp(depression=12.0, bearing=30.0, pitch=3.0, roll=-1.0)
        assert cs.tilt_boxes([r], ('v2', 2.5), 'S-rig')[0] == csv3.window_for(r, rule='v2')
        assert cs.tilt_boxes([r], ('v3', 5.8), 'S-rig')[0] == csv3.window_for(r, width_m=5.8)

    def test_off_and_s_grav_have_identical_size_metrics(self):
        """Only the centre differs between the two (the flag never re-sizes), so every size metric
        agrees; containment is the only thing that may not."""
        rng = random.Random(5)
        ramps = [ramp(depression=rng.uniform(1, 30), bearing=rng.uniform(-180, 180), pitch=rng.uniform(-3, 3),
                      roll=rng.uniform(-3, 3), box_w=rng.uniform(80, 500), pano_id='p%d' % i)
                 for i in range(30)]
        for spec in (('v2', 2.5), ('v3', 5.8)):
            off = cs.point_metrics(ramps, cs.tilt_boxes(ramps, spec, 'Off'))
            grav = cs.point_metrics(ramps, cs.tilt_boxes(ramps, spec, 'S-grav'))
            for key in ('fill_p50', 'fill_log_sd', 'fill_p90_over_p10', 'r2', 'window_deg_p50',
                        'frac_clearing_too_tight', 'vfill_p50'):
                assert off[key] == grav[key], (spec, key)

    def test_off_centres_on_the_modelled_point(self):
        r = ramp(depression=12.0, bearing=0.0, pitch=3.0)
        gx, gy = cs.modelled_stored_point(r, beta=0.95)
        off = cs.tilt_boxes([r], ('v2', 2.5), 'Off')[0]
        grav = cs.tilt_boxes([r], ('v2', 2.5), 'S-grav')[0]
        assert off.top == int(round(gy - off.height / 2))
        assert grav.top == int(round(r['y'] - grav.height / 2))

    def test_the_click_noise_row_is_seeded_and_sized_in_elevation(self):
        rng = random.Random(9)
        ramps = [ramp(depression=rng.uniform(2, 20), pitch=1.0, pano_id='p%d' % i) for i in range(40)]
        a = cs.tilt_boxes(ramps, ('v3', 5.8), 'S-grav-noise', noise_seed=7)
        b = cs.tilt_boxes(ramps, ('v3', 5.8), 'S-grav-noise', noise_seed=7)
        clean = cs.tilt_boxes(ramps, ('v3', 5.8), 'S-grav')
        assert a == b
        assert a != clean
        sizes = cs.config_size_ys(ramps, 'S-grav-noise', noise_seed=7)
        clean_y = cs.config_size_ys(ramps, 'S-grav')
        noise_deg = [CropRunner.elevation_px_to_deg(s - c, r['pano_h']) for s, c, r in zip(sizes, clean_y, ramps)]
        assert np.std(noise_deg) == pytest.approx(cs.CLICK_NOISE_DEG, rel=0.35)

    def test_a_ramp_without_a_pose_has_no_tilt_configuration(self):
        r = dict(ramp(), pitch=None, roll=None, provider='mapillary')
        with pytest.raises(ValueError):
            cs.tilt_boxes([r], ('v2', 2.5), 'S-grav')

    # -- the rules -------------------------------------------------------------------------------

    def test_v2_at_its_shipped_scale_is_croprunners_v2(self):
        for dep in (0.0, 3.0, 9.0, 18.0, 30.0, 60.0):
            for h in (2048, 6656, 8192):
                y = h / 2 + CropRunner.elevation_deg_to_px(dep, h)
                assert cs.rule_fov_deg(('v2', CropRunner.CROP_SIZE_SCALE), y, h) == \
                    CropRunner.crop_window_fov_deg(y, h, sizing_rule='v2')

    def test_v2_scale_multiplies_the_angle_below_the_clamps(self):
        h = 6656
        y = h / 2 + CropRunner.elevation_deg_to_px(9.0, h)
        assert cs.rule_fov_deg(('v2', 3.0), y, h) == pytest.approx(cs.rule_fov_deg(('v2', 1.5), y, h) * 2)

    def test_v3_and_option_a_are_the_v3_studys(self):
        h = 8192
        for dep in (1.0, 8.0, 15.0, 33.0):
            y = h / 2 + CropRunner.elevation_deg_to_px(dep, h)
            at = CropRunner.label_depression_deg(y, h)   # the depression the v3 study reads off y
            assert cs.rule_fov_deg(('v3', 6.0), y, h) == csv3.v3_fov_at(at, 6.0)
            assert cs.rule_fov_deg(('A', 2.2), y, h) == csv3.rule_a_fov_at(at, 2.2)

    def test_a_fill_computed_by_hand(self):
        """One fill, from the formulas in CropRunner's docstrings rather than its functions: v2 at
        depression 10 deg on a 4096-high pano."""
        r = ramp(depression=10.0, pano_h=4096, box_w=300.0)
        h = 4096
        ref_offset = -10.0 / 180.0 * 6656.0
        distance = 19.80546390 + 0.01523952 * ref_offset
        size_ref = 8725.6 * distance ** -1.192
        deg = size_ref * (h / 6656.0) * 2.5 / h * 180.0
        width = int(round(deg / 360.0 * (2 * h)))
        box = cs.tilt_boxes([r], ('v2', 2.5), 'S-rig')[0]
        assert box.width == width
        assert cs.point_metrics([r], [box])['fill_p50'] == pytest.approx(300.0 / width, rel=1e-12)

    def test_the_square_rules_are_rampnets(self):
        """RampNet crop_window_eval.py's side functions, restated: v1-norm is the 2013 regression in its
        reference space scaled back, geo-v1.5 the flat-ground width over the 0.125 target ratio."""
        w, h = 8192, 4096
        y = h / 2 + CropRunner.elevation_deg_to_px(12.0, h)
        assert cs.square_side(('v1-norm', 1.0), y, w, h) == pytest.approx(CropRunner.predict_crop_size(y, h))
        assert cs.square_side(('v1-norm', 3.5), y, w, h) == pytest.approx(3.5 * CropRunner.predict_crop_size(y, h))
        d = 2.5 / math.tan(math.radians(12.0))
        assert cs.square_side(('geo', 2.5), y, w, h) == pytest.approx((1.5 / d) * (w / (2 * math.pi)) / 0.125)
        near = h / 2 + CropRunner.elevation_deg_to_px(80.0, h)
        assert cs.square_side(('geo', 2.5), near, w, h) == pytest.approx((1.5 / 2.0) * (w / (2 * math.pi)) / 0.125)
        assert cs.square_side(('geo', 1.7), y, w, h) > cs.square_side(('geo', 2.5), y, w, h)

    def test_the_square_window_is_rampnets_crop_window(self):
        left, top, size, shifted = cs.square_window(10.0, 50.0, 300.4, 1000, 500)
        assert (left, top, size, shifted) == ((10 - 150) % 1000, 0, 300, True)

    # -- metrics and the bootstrap ---------------------------------------------------------------

    def test_weighted_metrics_at_unit_weights_are_the_point_metrics(self):
        """The bootstrap's vectorised metrics and the point estimates are two code paths; at unit weights
        they must agree, so neither can drift from the other."""
        rng = random.Random(11)
        ramps = [ramp(depression=rng.uniform(0.5, 35), box_w=rng.uniform(50, 900), box_h=rng.uniform(20, 300),
                      bearing=rng.uniform(-180, 180), pano_id='p%d' % i) for i in range(41)]
        for spec in (('v2', 2.5), ('v3', 5.8), ('A', 2.2)):
            boxes = cs.tilt_boxes(ramps, spec, 'S-rig')
            point = cs.point_metrics(ramps, boxes)
            arrays = cs.ramp_arrays(ramps, boxes)
            vec = cs.weighted_metrics(arrays, np.ones((1, len(ramps))))
            for key in cs.BOOT_METRICS:
                assert vec[key][0] == pytest.approx(point[key], rel=1e-12, abs=1e-15), (spec, key)

    def test_the_weighted_percentile_is_nearest_rank_on_the_expanded_list(self):
        rng = np.random.default_rng(0)
        x = rng.normal(size=17)
        W = rng.integers(0, 4, size=(50, 17))
        W[W.sum(1) == 0, 0] = 1
        for q in (10, 50, 90, 2.5, 97.5):
            got = cs.weighted_percentile(x, W, q)
            for b in range(len(W)):
                expanded = np.repeat(x, W[b])
                assert got[b] == cs.pct(list(expanded), q)

    def test_the_bootstrap_resamples_panos_within_city(self):
        """Synthetic panos with UNEQUAL ramp counts, so resampling ramps instead of panos is caught: every
        ramp of a pano carries its pano's draw count, each city draws as many panos as it has, and the
        replicate size varies."""
        ramps = []
        for city, counts in (('a', (5, 1, 2)), ('b', (3, 1))):
            for i, c in enumerate(counts):
                ramps += [dict(ramp(city=city, pano_id='%s%d' % (city, i)), key='det:%d' % k) for k in range(c)]
        W = cs.pano_bootstrap_weights(ramps, 400, seed=1)
        assert W.shape == (400, len(ramps))
        panos = {}
        for j, r in enumerate(ramps):
            panos.setdefault((r['city'], r['pano_id']), []).append(j)
        for cols in panos.values():
            assert (W[:, cols] == W[:, cols[:1]]).all()
        first = {key: cols[0] for key, cols in panos.items()}
        for city, n_panos in (('a', 3), ('b', 2)):
            draws = sum(W[:, first[k]] for k in first if k[0] == city)
            assert (draws == n_panos).all()
        assert len(set(W.sum(1).tolist())) > 1
        assert (W == cs.pano_bootstrap_weights(ramps, 400, seed=1)).all()

    def test_the_weighted_isotonic_ceiling_is_the_v3_studys_at_unit_weights_and_counts_copies(self):
        """The ceiling's replicates use weighted_isotonic_r2; at unit weights it must be crop_sizing_v3's
        depression_only_ceiling, and a weight of 2 must equal the ramp listed twice."""
        rng = random.Random(8)
        ramps = [ramp(depression=rng.choice([2.0, 5.0, 5.0, 9.0, 14.0, 20.0, 31.0]) + rng.uniform(0, 1),
                      box_w=rng.uniform(60, 900), pano_id='p%d' % i) for i in range(40)]
        deps = np.array([csv3.depression(r) for r in ramps])
        logs = np.log([csv3.apron_deg(r) for r in ramps])
        assert cs.weighted_isotonic_r2(deps, logs, np.ones(40)) == pytest.approx(
            csv3.depression_only_ceiling(ramps)['isotonic_r2'], rel=1e-12)
        w = np.ones(40)
        w[3] = 2
        doubled = ramps + [ramps[3]]
        assert cs.weighted_isotonic_r2(deps, logs, w) == pytest.approx(
            csv3.depression_only_ceiling(doubled)['isotonic_r2'], rel=1e-12)

    def test_the_interval_is_the_nearest_rank_2_5_and_97_5_percentiles(self):
        values = list(range(201))
        assert cs.nearest_rank_interval(values) == (5, 195)
        assert cs.nearest_rank_interval([3.0, float('nan'), 1.0, 2.0]) == (1.0, 3.0)

    # -- the verdicts ----------------------------------------------------------------------------

    def test_criterion_2_needs_the_interval_and_three_cities(self):
        better = 'lower'
        assert cs.paired_verdict(-0.02, (-0.03, -0.01), [-0.01, -0.02, -0.03, 0.01], better, 3) == 'first'
        assert cs.paired_verdict(-0.02, (-0.03, -0.01), [-0.01, -0.02, 0.01, 0.01], better, 3) == 'none'
        assert cs.paired_verdict(-0.02, (-0.03, 0.001), [-0.01, -0.02, -0.03, -0.01], better, 3) == 'none'
        assert cs.paired_verdict(0.02, (0.01, 0.03), [0.01, 0.02, 0.03, 0.0], better, 3) == 'second'
        # A city at exactly 0 does not agree.
        assert cs.paired_verdict(0.02, (0.01, 0.03), [0.01, 0.02, 0.0, 0.0], better, 3) == 'none'
        # Higher is better (R²): the same numbers read the other way.
        assert cs.paired_verdict(0.02, (0.01, 0.03), [0.01, 0.02, 0.03, -0.01], 'higher', 3) == 'first'

    def test_the_tilt_verdict_combines_the_two_metrics(self):
        assert cs.tilt_rule_verdict({'fill_log_sd': 'first', 'r2': 'none'}) == 'g.y better'
        assert cs.tilt_rule_verdict({'fill_log_sd': 'none', 'r2': 'second'}) == 'r.y better'
        assert cs.tilt_rule_verdict({'fill_log_sd': 'first', 'r2': 'second'}) == 'not distinguishable'
        assert cs.tilt_rule_verdict({'fill_log_sd': 'none', 'r2': 'none'}) == 'not distinguishable'

    # -- selection ---------------------------------------------------------------------------------

    def test_the_selected_constant_is_the_nearest_grid_value_and_the_first_on_a_tie(self):
        ramps = [ramp(depression=d, box_w=150 + 8 * d, pano_id='p%d' % i) for i, d in enumerate((2, 6, 11, 17, 24))]
        grid = [4.0, 5.0, 6.0, 7.0]
        fills = [cs.point_metrics(ramps, [cs.cut_box(r, ('v3', w)) for r in ramps])['fill_p50'] for w in grid]
        assert cs.select_constant(ramps, 'v3', grid, fills[2]) == 6.0
        assert cs.select_constant(ramps, 'v3', grid, fills[2] + 1e-9) == 6.0
        capped = [ramp(depression=60.0)]
        assert cs.select_constant(capped, 'v3', grid, 0.0) == 4.0

    def test_leave_one_city_out_chooses_on_the_other_cities_only(self):
        """The held-out city is given aprons ten times wider: if it leaked into the choice the constant
        would move."""
        ramps = []
        for city, scale in (('a', 1.0), ('b', 1.0), ('c', 1.0), ('h', 10.0)):
            for i, d in enumerate((2.0, 5.0, 9.0, 14.0, 21.0)):
                ramps.append(ramp(depression=d, box_w=scale * (120 + 9 * d), city=city, pano_id='%s%d' % (city, i)))
        others = [r for r in ramps if r['city'] != 'h']
        grid = cs.WIDE_WIDTH_GRID
        assert cs.loco_constant(ramps, 'h', 'v3', grid, 'band') == cs.select_constant(others, 'v3', grid, cs.BAND_CENTRE_FILL)
        target = cs.point_metrics(others, [cs.cut_box(r, ('v2', 2.5)) for r in others])['fill_p50']
        assert cs.loco_constant(ramps, 'h', 'v3', grid, 'match_v2') == cs.select_constant(others, 'v3', grid, target)

    # -- power -------------------------------------------------------------------------------------

    def test_the_power_step_runs_without_apron_extents(self):
        """The power simulation must never read the gold's apron: it gets ramps with every box field
        removed, and still runs."""
        rng = random.Random(2)
        ramps = []
        for city in ('sao_paulo', 'paterson'):
            for i in range(6):
                r = ramp(depression=rng.uniform(2, 25), bearing=rng.uniform(-180, 180), pitch=rng.uniform(-3, 3),
                         roll=rng.uniform(-3, 3), city=city, pano_id='%s%d' % (city, i % 3))
                for field in ('box_w', 'box_h', 'box_cx', 'box_cy'):
                    del r[field]
                ramps.append(r)
        out = cs.power_simulation(ramps, ('v2', 2.5), {'sao_paulo': 0.3, 'paterson': 0.3}, multipliers=(1.0, 8.0),
                                  n_sims=4, n_boot=30, seed=0)
        assert [row['multiplier'] for row in out['rows']] == [1.0, 8.0]
        assert all(0.0 <= row['power'] <= 1.0 for row in out['rows'])

    def test_power_rises_with_the_tilt(self):
        """With no scatter at all the truth is exactly the window at g.y, so a large tilt is always seen;
        with a zero multiplier there is nothing to see."""
        rng = random.Random(4)
        ramps = []
        for city in ('sao_paulo', 'paterson'):
            for i in range(24):
                ramps.append(ramp(depression=rng.uniform(2, 22), bearing=rng.uniform(-180, 180),
                                  pitch=rng.uniform(-3, 3), roll=rng.uniform(-3, 3), city=city,
                                  pano_id='%s%d' % (city, i % 8)))
        out = cs.power_simulation(ramps, ('v3', 5.8), {'sao_paulo': 0.05, 'paterson': 0.05},
                                  multipliers=(0.0, 4.0), n_sims=6, n_boot=60, seed=1)
        power = {row['multiplier']: row['power'] for row in out['rows']}
        assert power[0.0] == 0.0
        assert power[4.0] == 1.0
        assert out['mde_multiplier'] == 4.0


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture(scope='module')
def gold_rows():
    return cs.read_gold(os.path.join(DATA_DIR, cs.GOLD_CSV))


@pytest.fixture(scope='module')
def gold(gold_rows):
    return cs.boxed_ramps(gold_rows)


@pytest.fixture(scope='module')
def provenance():
    with open(os.path.join(DATA_DIR, cs.PROVENANCE_JSON), encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def v2_ref():
    with open(V2_JSON, encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def v3_ref():
    with open(V3_JSON, encoding='utf-8') as f:
        return json.load(f)


class TestTheGoldExtract:
    """The committed copy of RampNet's extent gold: what it holds, and that it is the file provenance names."""

    def test_the_counts(self, gold_rows, gold):
        assert len(gold_rows) == 705
        assert len(gold) == 658
        assert sum(1 for r in gold_rows if r['status'] == 'cant') == 47
        by_city = {c: sum(1 for r in gold if r['city'] == c) for c in CITIES}
        assert by_city == {'richmond': 299, 'sao_paulo': 119, 'paterson': 109, 'annapolis': 131}
        assert sum(1 for r in gold if r['kind'] == 'det') == 482
        assert sum(1 for r in gold if r['kind'] == 'missed') == 176

    def test_the_key_is_city_pano_key_and_unique(self, gold_rows):
        keys = [(r['city'], r['pano_id'], r['key']) for r in gold_rows]
        assert len(keys) == len(set(keys))
        assert 'label_id' not in gold_rows[0]

    def test_the_gsv_part_is_posed_and_the_mapillary_part_is_not(self, gold):
        gsv = [r for r in gold if r['provider'] == 'gsv']
        assert len(gsv) == 228 and len({r['pano_id'] for r in gsv}) == 70
        assert {r['city'] for r in gsv} == {'sao_paulo', 'paterson'}
        assert all(r['pitch'] is not None and r['roll'] is not None for r in gsv)
        assert all(r['pitch'] is None and r['roll'] is None for r in gold if r['provider'] == 'mapillary')
        assert max(r['roll'] for r in gsv) > 359, 'raw record rolls reach 359.96, which is why they are wrapped'

    def test_mapillary_ids_stay_strings(self, gold_rows):
        richmond = [r['pano_id'] for r in gold_rows if r['city'] == 'richmond']
        assert all(isinstance(p, str) and p.isdigit() for p in richmond)

    def test_no_attribution_field_is_copied(self, gold_rows):
        assert tuple(gold_rows[0]) == cs.GOLD_COLUMNS
        with open(os.path.join(DATA_DIR, cs.GOLD_CSV), encoding='utf-8') as f:
            assert '\u00a9' not in f.read()

    def test_the_hashes_match_provenance(self, provenance):
        assert provenance['extracts'], 'provenance lists no extract'
        for name, entry in provenance['extracts'].items():
            assert _sha(os.path.join(DATA_DIR, name)) == entry['sha256'], name
        assert len(provenance['rampnet_commit']) == 40
        assert provenance['labeler_commit'] == cs.LABELER_COMMIT
        assert provenance['gold_rows'] == 705

    def test_the_files_are_byte_stable_on_checkout(self):
        """Hashed files must not be normalised by git: .gitattributes marks the folder -text."""
        import subprocess
        out = subprocess.run(['git', '-C', REPO_ROOT, 'check-attr', 'text', '--',
                              'reports/data/2026-11-22-crop-sizing-consolidation/gold.csv'],
                             capture_output=True, text=True, check=True).stdout
        assert out.strip().endswith(': text: unset'), out

    def test_no_committed_file_of_this_study_names_a_machine(self):
        """No local absolute path in the extract, the script, the report or the criteria. Checked on the raw text
        AND on every decoded JSON string, because JSON doubles a backslash and a plain substring search for a
        Windows path then cannot match (review 1 of PR #254, finding 1). Any drive-letter path counts."""
        files = [os.path.join(root, name) for root, _, names in os.walk(DATA_DIR) for name in names]
        assert len(files) >= 9, 'the extract folder is missing or nearly empty'
        files += [os.path.join(SCRIPTS, 'crop_sizing_consolidation.py'), REPORT_MD,
                  os.path.join(REPO_ROOT, cs.CRITERIA_FILE)]
        for path in files:
            opener = gzip.open if path.endswith('.gz') else open
            with opener(path, 'rt', encoding='utf-8') as f:
                text = f.read()
            texts = [text]
            if path.endswith('.json'):
                texts += list(_json_strings(json.loads(text)))
            for t in texts:
                assert not _LOCAL_PATH.search(t), (path, _LOCAL_PATH.search(t).group(0))

    def test_the_path_check_sees_a_json_escaped_path(self):
        """Guards the guard: the path a JSON file stores with doubled backslashes is still found."""
        path = 'E:' + chr(92) + 'Work' + chr(92) + 'x'
        text = json.dumps({'from': path})
        assert chr(92) * 2 in text
        assert any(_LOCAL_PATH.search(t) for t in [text] + list(_json_strings(json.loads(text))))
        assert _LOCAL_PATH.search('see /home/someone/x') and not _LOCAL_PATH.search('<RampNet>' + chr(92) + 'benchmark')

    def test_the_plan_snapshot_is_the_labeler_file_with_one_path_redacted(self, provenance):
        assert [(r['pattern'], r['to']) for r in provenance['snapshot_redactions']] == list(cs.SNAPSHOT_REDACTIONS)
        assert all(len(r['removed_sha256']) == 64 for r in provenance['snapshot_redactions'])
        with open(os.path.join(DATA_DIR, cs.PLAN_SNAPSHOT), encoding='utf-8') as f:
            text = f.read()
        assert cs.SNAPSHOT_REDACTIONS[0][1] in text
        assert 'v1-norm' in text


class TestItReproducesTheEarlierReports:
    """The new scorer is the old scorer, on the committed gold, before any new column is trusted. Expected
    keys are listed explicitly, so a renamed key fails rather than silently dropping out of a comparison."""

    V2_KEYS = ('fill_p10', 'fill_p50', 'fill_p90', 'frac_clearing_too_tight', 'containment', 'fits_by_size',
               'stored_width_p50', 'stored_width_p90', 'window_deg_p10', 'window_deg_p50', 'window_deg_p90')
    V3_KEYS = V2_KEYS + ('fill_log_sd', 'fill_p90_over_p10')

    @staticmethod
    def _scored(ramps, spec):
        return cs.point_metrics(ramps, [cs.cut_box(r, spec) for r in ramps])

    @staticmethod
    def _blocks(gold):
        out = {c: [r for r in gold if r['city'] == c] for c in CITIES}
        out['pooled'] = gold
        return out

    def test_v2_equals_the_v2_artifact(self, gold, v2_ref):
        for name, ramps in self._blocks(gold).items():
            ref = v2_ref['pooled'] if name == 'pooled' else v2_ref['cities'][name]
            mine = self._scored(ramps, ('v2', 2.5))
            assert mine['n'] == ref['n']
            for key in self.V2_KEYS:
                assert mine[key] == pytest.approx(ref['v2'][key], rel=1e-12), (name, key)

    def test_v2_and_v3_equal_the_v3_artifact(self, gold, v3_ref):
        for name, ramps in self._blocks(gold).items():
            ref = v3_ref['pooled'] if name == 'pooled' else v3_ref['cities'][name]
            for rule, spec in (('v2', ('v2', 2.5)), ('v3', ('v3', 5.8))):
                mine = self._scored(ramps, spec)
                for key in self.V3_KEYS:
                    assert mine[key] == pytest.approx(ref[rule][key], rel=1e-12), (name, rule, key)
                assert mine['r2'] == pytest.approx(ref['r2'][rule], rel=1e-9), (name, rule)
            assert csv3.depression_only_ceiling(ramps)['isotonic_r2'] == pytest.approx(
                ref['r2']['ceiling']['isotonic_r2'], rel=1e-9)

    def test_option_a_equals_the_v3_artifact(self, gold, v3_ref):
        a = v3_ref['rule_a_blend_powerlaw']
        assert cs.select_constant(gold, 'A', csv3.RULE_A_SCALE_GRID, cs.v2_fill_p50(gold)) == a['matched_scale'] == 2.2
        mine = self._scored(gold, ('A', a['matched_scale']))
        for key in ('fill_p50', 'fill_log_sd', 'fill_p90_over_p10', 'frac_clearing_too_tight', 'containment'):
            assert mine[key] == pytest.approx(a['pooled_matched'][key], rel=1e-12), key
        assert mine['r2'] == pytest.approx(a['pooled_matched']['r2'], rel=1e-9)

    def test_the_selected_widths_are_the_v3_studys(self, gold, v3_ref):
        sel = v3_ref['selection']
        assert cs.select_constant(gold, 'v3', csv3.GRID_M, cs.v2_fill_p50(gold)) == sel['matched_context_width_m']
        assert cs.select_constant(gold, 'v3', csv3.GRID_M, cs.BAND_CENTRE_FILL) == sel['band_centre_width_m']

    @pytest.mark.parametrize('city', CITIES)
    def test_the_square_rules_equal_rampnets_committed_summaries(self, gold, city):
        """Read from the copied crop_window_eval_<city>.json, never typed in here: detection mode, v1-norm
        and geo-v1.5 at RampNet's own constants, and v1-norm's rescale sweep at x3.5."""
        with open(os.path.join(DATA_DIR, cs.EVAL_JSON % city), encoding='utf-8') as f:
            ref = json.load(f)
        assert ref['inputs']['cam_height_m'] == 2.5
        ramps = [r for r in gold if r['city'] == city]
        checked = 0
        for rule, spec in (('v1-norm', ('v1-norm', 1.0)), ('geo-v1.5', ('geo', 2.5))):
            mine = cs.rampnet_summary(ramps, spec)
            theirs = ref['summary']['detection'][rule]
            for key in cs.RAMPNET_SUMMARY_KEYS:
                assert mine[key] == pytest.approx(theirs[key], rel=1e-9), (rule, key)
                checked += 1
        row = next(r for r in ref['rescale_sweep']['v1-norm'] if r['k'] == 3.5)
        mine = cs.rampnet_sweep_row(ramps, ('v1-norm', 1.0), 3.5)
        for key in cs.RAMPNET_SWEEP_KEYS:
            assert mine[key] == pytest.approx(row[key], rel=1e-9), key
            checked += 1
        assert checked == 2 * len(cs.RAMPNET_SUMMARY_KEYS) + len(cs.RAMPNET_SWEEP_KEYS)


@pytest.mark.skipif(not (os.environ.get('RAMPNET_ROOT') and os.environ.get('AUTO_LABELER_ROOT')),
                    reason='set RAMPNET_ROOT and AUTO_LABELER_ROOT to checkouts to regenerate the extract')
def test_the_extracts_reproduce_from_rampnet(provenance):
    """`extract` against a RampNet checkout at provenance's commit (and an auto-labeler checkout holding the
    snapshot's commit) rebuilds every committed extract file byte for byte. Reads committed blobs only."""
    import subprocess
    root = os.environ['RAMPNET_ROOT']
    head = subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    if head != provenance['rampnet_commit']:
        pytest.skip('RAMPNET_ROOT is at %s, not the committed %s' % (head[:7], provenance['rampnet_commit'][:7]))
    files = cs.build_extract(root, os.environ['AUTO_LABELER_ROOT'])
    assert set(files) == set(provenance['extracts']) | {cs.PROVENANCE_JSON}
    for name, data in files.items():
        with open(os.path.join(DATA_DIR, name), 'rb') as f:
            assert f.read() == data, name


# ---------------------------------------------------------------------------------------------------------
# The committed artifact, regenerated from the committed inputs. The full run measured 33 s on a Windows dev box
# (2026-11-22), under the plan's 60 s line, so CI regenerates every block, intervals included.

@pytest.fixture(scope='module')
def summary():
    with open(SUMMARY_JSON, encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def regenerated(gold, gold_rows):
    fresh, boxes_by_rule, gsv_ramps = cs.build_summary(gold, gold_rows)
    raw, n = cs.per_ramp_rows(gold, boxes_by_rule, gsv_ramps, cs.tilt_boxes_for_rows(gsv_ramps))
    return fresh, raw, n


# Every key whose value depends on the bootstrap replicates (or, for power, on the simulation).
INTERVAL_KEYS = {'rule_intervals', 'ceiling_intervals', 'interval', 'clearing_interval', 'fill_p50_interval',
                 'width_intervals', 'verdict', 'control_verdict', 'misses_floor', 'differs', 'per_city_supported',
                 'p2_5', 'p50', 'p97_5', 'share_5_8', 'share_6_0', 'undefined_replicates', 'power'}


def _split(obj, keep_interval):
    """The point-estimate part of an artifact (keep_interval False) or its interval part (True)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in INTERVAL_KEYS:
                if keep_interval:
                    out[k] = v
            else:
                sub = _split(v, keep_interval)
                if not keep_interval or sub not in ({}, [], None):
                    out[k] = sub
        return out
    if isinstance(obj, list):
        return [_split(v, keep_interval) for v in obj] if not keep_interval else \
            [s for s in (_split(v, True) for v in obj) if s not in ({}, [], None)]
    return obj if not keep_interval else None


def _approx_equal(a, b, path=''):
    """Exact for strings, ints and bools; floats to 1e-9 relative, since CI's numpy is not this box's."""
    if isinstance(a, dict):
        assert isinstance(b, dict) and set(a) == set(b), (path, sorted(set(a) ^ set(b or {})))
        for k in a:
            _approx_equal(a[k], b[k], path + '.' + str(k))
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _approx_equal(x, y, '%s[%d]' % (path, i))
    elif isinstance(a, float) or isinstance(b, float):
        assert b == pytest.approx(a, rel=1e-9, abs=1e-12), path
    else:
        assert a == b, path


class TestTheArtifactRegenerates:

    def test_the_point_estimates_reproduce_from_committed_inputs(self, summary, regenerated):
        fresh = regenerated[0]
        committed = {k: v for k, v in summary.items() if k != 'meta'}
        point = _split(committed, False)
        assert point['rules'] and point['tilt'] and point['loco'], 'the split dropped a block'
        _approx_equal(point, _split(fresh, False))

    def test_the_intervals_reproduce_from_committed_inputs(self, summary, regenerated):
        fresh = regenerated[0]
        committed = {k: v for k, v in summary.items() if k != 'meta'}
        intervals = _split(committed, True)
        assert intervals['rule_intervals'] and intervals['paired'] and intervals['power']
        _approx_equal(intervals, _split(fresh, True))

    def test_the_per_ramp_rows_reproduce(self, summary, regenerated):
        raw, n = regenerated[1], regenerated[2]
        assert summary['meta']['per_ramp_rows'] == n
        assert hashlib.sha256(raw).hexdigest() == summary['meta']['per_ramp_csv_sha256']
        with gzip.open(os.path.join(DATA_DIR, cs.PER_RAMP_CSV), 'rb') as f:
            assert f.read() == raw

    def test_the_power_block_is_the_one_committed_first(self, summary):
        with open(os.path.join(DATA_DIR, cs.POWER_FIRST_JSON), encoding='utf-8') as f:
            first = json.load(f)
        assert first['power'] == summary['power']
        assert first['criteria_commit'] == cs.CRITERIA_COMMIT == summary['meta']['criteria_commit']

    def test_the_inputs_are_the_committed_extract(self, summary, provenance):
        for name, sha in summary['meta']['inputs_sha256'].items():
            assert provenance['extracts'][name]['sha256'] == sha, name
        assert 'study --write reports/data/2026-11-22-crop-sizing-consolidation.json' in summary['meta']['generated_by']

    def test_the_split_is_not_vacuous(self):
        sample = {'a': 1.0, 'interval': [0, 1], 'b': {'verdict': 'x', 'c': 2}}
        assert _split(sample, False) == {'a': 1.0, 'b': {'c': 2}}
        assert _split(sample, True) == {'interval': [0, 1], 'b': {'verdict': 'x'}}


def assert_every_key_is_claimed_once(summary):
    pops = summary['populations']
    claimed = (pops['gold']['covers'] + pops['gold_gsv']['covers'] + pops['mapillary_no_pose']['covers']
               + pops['clamp_census']['covers'] + pops['no_population'])
    assert len(claimed) == len(set(claimed)), claimed
    assert set(claimed) == set(summary) - {'meta', 'populations'}


class TestEveryFigureIsClaimedByOnePopulation:

    def test_the_committed_artifact(self, summary):
        assert_every_key_is_claimed_once(summary)
        pops = summary['populations']
        assert pops['gold']['n'] == 658 and pops['gold_gsv']['n'] == 228 and pops['mapillary_no_pose']['n'] == 430
        assert pops['gold']['n'] == pops['gold_gsv']['n'] + pops['mapillary_no_pose']['n']

    def test_a_new_key_must_be_claimed(self, summary):
        """The claim is computed from the summary, so a key added without a population fails."""
        extra = dict(summary, unclaimed_block={'x': 1})
        extra['populations'] = cs.populations(extra)
        with pytest.raises(AssertionError):
            assert_every_key_is_claimed_once(extra)


# ---------------------------------------------------------------------------------------------------------
# Every number in the report, transcribed from a committed file.

@pytest.fixture(scope='module')
def report():
    """The report with whitespace runs collapsed, so a wrapped phrase still matches."""
    with open(REPORT_MD, encoding='utf-8') as f:
        return ' '.join(f.read().split())


def _neg(text):
    return text.replace('-', '−')


def _eval_json(city):
    with open(os.path.join(DATA_DIR, cs.EVAL_JSON % city), encoding='utf-8') as f:
        return json.load(f)


def _smallest_scale(city, containment):
    return min(r['k'] for r in _eval_json(city)['rescale_sweep']['v1-norm'] if r['containment'] >= containment)


class TestReportMatchesTheArtifact:

    def test_every_table_is_the_scripts(self, summary, report):
        """The tables are printed by `tables` from the artifact; each line must appear in the report."""
        checked = 0
        for table in cs.REPORT_TABLES:
            for line in table(summary):
                assert ' '.join(line.split()) in report, line
                checked += 1
        assert checked > 100

    def test_the_gold(self, summary, report):
        g = summary['gold']
        assert '%d items, %d boxed' % (g['n_items'], g['n_boxed']) in report
        assert 'on the %d GSV ramps' % summary['tilt']['n_ramps'] in report
        assert 'The %d Mapillary ramps have no pose' % summary['no_pose']['n'] in report
        assert 'Its %d ramps on %d panos' % (g['by_city']['annapolis'], g['panos_by_city']['annapolis']) in report

    def test_the_reproduction(self, summary, report):
        rep = summary['reproduction']
        assert "option A's matched scale (×%.1f)" % rep['option_a_matched_scale'] in report
        assert "%.1f m matched to v2's pooled fill p50 of %.3f, %.1f m at the band centre" % (
            rep['v3_matched_width_m'], rep['v2_pooled_fill_p50'], rep['v3_band_centre_width_m']) in report
        n_values = len(CITIES) * (2 * len(cs.RAMPNET_SUMMARY_KEYS) + len(cs.RAMPNET_SWEEP_KEYS))
        assert 'in all four cities, %d values in all' % n_values in report

    def test_rampnets_rounds_from_the_copied_summaries(self, report):
        names = {'sao_paulo': 'São Paulo', 'paterson': 'Paterson', 'richmond': 'Richmond', 'annapolis': 'Annapolis'}
        order = ('sao_paulo', 'paterson', 'richmond', 'annapolis')
        parts = ['×%.1f in %s' % (_smallest_scale(c, 0.985), names[c]) for c in order]
        assert '%s, %s, %s and %s' % tuple(parts) in report
        det = _eval_json('richmond')['summary']['detection']
        assert 'geo-v1.5 containment %.3f and an apron aspect of %.2f:1' % (
            det['geo-v1.5']['containment'], det['v1-norm']['box_aspect_p50']) in report
        assert _smallest_scale('richmond', 0.995) == 3.5
        assert 'the square scale RampNet needed to contain 99.5% of Richmond' in report

    def test_e14_from_the_copied_validation_summary(self, report):
        with open(os.path.join(DATA_DIR, cs.TILT_SUMMARY), encoding='utf-8') as f:
            v = json.load(f)
        s = lambda block: (block['sign_test_accepted']['pitch']['slope'], block['sign_test_accepted']['roll']['slope'])  # noqa: E731
        p, r = s(v['all'])
        pp, pr = s(v['by_split_all']['paterson'])
        sp, sr = s(v['by_split_all']['sao_paulo'])
        assert ('slopes %s (pitch) and %s (roll), MAE %s°, on %d accepted panos; Paterson %s / %s and São Paulo '
                '%s / %s' % (p, r, v['all']['accepted']['mae_deg'], v['all']['accepted']['n'], pp, pr, sp, sr)) in report

    def test_the_tagger_maps_from_the_pilot(self, report):
        maps = {}
        with open(os.path.join(DATA_DIR, 'pilot', 'probe_ap_repro.out.txt'), encoding='utf-8') as f:
            for line in f:
                arm = line.split()[0]
                maps[arm] = line.split("'logit': ")[1].split(',')[0]
        assert 'control %s, fov25 %s, fov50 %s, fov90 %s' % (
            maps['control'], maps['fov25'], maps['fov50'], maps['fov90']) in report

    def test_the_judge_rounds_from_the_snapshot(self, report):
        with open(os.path.join(DATA_DIR, cs.PLAN_SNAPSHOT), encoding='utf-8') as f:
            snap = f.read()
        for token in ('132 comparisons', '112 comparisons', '144 crops', '**0.49**', '0.46–0.54', '68%', '74%'):
            assert token in snap, token
        assert ('132 and 112 forced choices, 144 absolute judgements, "too tight" above fill 0.49 (95% CI '
                '0.46-0.54), 68% clearing at ×2.5') in report
        assert '74% at 1:1, 3:2 and 2:1 alike' in report

    def test_the_pilot_disclosure_from_the_pilot_output(self, report):
        rows = {}
        with open(os.path.join(DATA_DIR, 'pilot', 'probe_tilt.out.txt'), encoding='utf-8') as f:
            for line in f:
                city = line.split()[0]
                dy = line.split('p50/p90/max [')[1].split(']')[0].split()
                shares = line.split('share moved >10% v2 ')[1].split(' v3 ')
                rows[city] = (dy[0], dy[1], round(100 * float(shares[0])), round(100 * float(shares[1])))
        sp, pa = rows['sao_paulo'], rows['paterson']
        assert ('|dy| p50 %s° / p90 %s° in São Paulo and %s° / %s° in Paterson' % (sp[0], sp[1], pa[0], pa[1])) in report
        assert ('%d%% (v2) and %d%% (v3) in São Paulo, %d%% and %d%% in Paterson' % (sp[2], sp[3], pa[2], pa[3])) in report
        assert "São Paulo's |dy| p90 reads %.3f° in the artifact against the pilot's %s°" % (
            summary_shift_p90(), sp[1]) in report

    def test_the_tilt_prose(self, summary, report):
        t = summary['tilt']
        assert ("R² %.3f of the GSV aprons' log width at r.y and %.3f at g.y"
                % (t['isotonic_ceiling']['at_r_y'], t['isotonic_ceiling']['at_g_y'])) in report
        v2, v3 = t['rules']['v2'], t['rules']['v3']
        assert 'Criterion 6 reads **%s** for v2 and **%s** for v3' % (v2['verdict'], v3['verdict']) in report
        assert v2['control_verdict'] == v3['control_verdict'] == 'not distinguishable'
        assert ('moves %.1f%% of v2 windows and %.1f%% of v3 windows' % (
            100 * v2['share_window_moves_over_10pct'], 100 * v3['share_window_moves_over_10pct'])) in report
        off2, off3 = v2['off_minus_grav_containment'], v3['off_minus_grav_containment']
        assert 'containment by %s [%s, %s] under v2 and by %.3f under v3' % (
            _neg('%+.4f' % off2['pooled']), _neg('%.4f' % off2['interval'][0]), '%.4f' % off2['interval'][1],
            off3['pooled']) in report
        sel = summary['selection_at_gy']
        assert 'gives %.1f m matched and %.1f m at the band centre' % (
            sel['matched_context_width_m'], sel['band_centre_width_m']) in report
        assert sel['matched_context_width_m'] == sel['at_r_y']['matched_context_width_m']

    def test_the_outcome_matches_the_verdicts(self, summary, report):
        p = summary['paired']
        assert all(p[name]['metrics'][m]['verdict'] == 'first' for name in ('v3_minus_v2', 'A_minus_v2')
                   for m in cs.DISCRIMINATING)
        assert all(p[name]['metrics'][m]['out_of_sample']['holds'] for name in ('v3_minus_v2', 'A_minus_v2')
                   for m in cs.DISCRIMINATING)
        d7 = p['v3_minus_A']['metrics']
        assert d7['r2']['verdict'] == 'first' and d7['fill_log_sd']['verdict'] == d7['fill_p90_over_p10']['verdict'] == 'none'
        assert not any(row['misses_floor'] for held in summary['loco'].values() for row in held['rows'].values())
        assert 'no held-out city misses the 45% floor' in report
        assert summary['d6']['band']['mapillary_minus_gsv']['differs'] and summary['d6']['per_city_supported']
        assert not summary['d6']['match_v2']['mapillary_minus_gsv']['differs']

    def test_shape_and_camera_and_s4(self, summary, report):
        sh = summary['shape']
        c = sh['apron_aspect_by_city_p50']
        assert '%.3f pooled (Richmond %.3f, São Paulo %.3f, Paterson %.3f, Annapolis %.3f)' % (
            sh['apron_aspect_w_over_h_p50'], c['richmond'], c['sao_paulo'], c['paterson'], c['annapolis']) in report
        r = summary['rules']
        assert 'vertical fill p50 is %.3f under v2 ×2.5 and %.3f under v1-norm ×3.5' % (
            r['v2_x2.5']['strata']['pooled']['vfill_p50'], r['v1norm_x3.5_square']['strata']['pooled']['vfill_p50']) in report
        assert 'every v3 window to %.1e°' % summary['camera_heights']['w_over_h_identity_max_abs_deg'] in report
        with open(os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-26-tilt-s4-extent-gold.json'), encoding='utf-8') as f:
            s4 = json.load(f)
        assert 'S4 had %d pairs where its design needed %d' % (s4['n_pairs'], s4['min_pairs_to_analyse']) in report

    def test_the_decisions_quote_the_artifact(self, summary, report):
        r = summary['rules']
        assert 'option A clears more (%.1f%% against %.1f%% pooled)' % (
            100 * r['A_x2.2']['strata']['pooled']['frac_clearing_too_tight'],
            100 * r['v3_5.8m']['strata']['pooled']['frac_clearing_too_tight']) in report
        ann = summary['loco']['annapolis']['rows']['A_match_v2']
        assert 'Option A clears it best (%.1f%% held out)' % (100 * ann['clearing']) in report
        best = max(summary['loco']['annapolis']['rows'].values(), key=lambda row: row['clearing'])
        assert best is ann

    def test_the_criteria_commit_is_named(self, summary, report):
        assert 'commit `%s`' % cs.CRITERIA_COMMIT[:7] in report
        assert summary['meta']['criteria_file'] in report.replace('](plans/', '](reports/plans/') or \
            os.path.basename(summary['meta']['criteria_file']) in report

    def test_the_report_is_indexed_with_its_outcome(self):
        with open(os.path.join(REPO_ROOT, 'reports', 'README.md'), encoding='utf-8') as f:
            assert '(2026-11-22-crop-sizing-consolidation.md)' in f.read()


def summary_shift_p90():
    with open(SUMMARY_JSON, encoding='utf-8') as f:
        return json.load(f)['tilt']['shift']['sao_paulo']['abs_dy_deg_p90']
