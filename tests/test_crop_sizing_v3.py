"""Tests for reports/scripts/crop_sizing_v3.py - the v2-vs-v3 crop-sizing study (#32).

Split the way tests/test_crop_sizing_v2.py is:

* the study's own logic, on synthetic ramps whose answers are known by construction - because a pin
  against the committed artifact proves nothing about the code that produced it;
* the committed conclusions, pinned against reports/data/2026-09-26-crop-sizing-v3.json, offline - the
  gold lives in the RampNet benchmark, so the JSON is the only thing CI can see;
* every number in the report's prose, transcribed from that artifact.
"""

import json
import math
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import CropRunner  # noqa: E402
import crop_sizing_v3 as csv3  # noqa: E402

SUMMARY_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-26-crop-sizing-v3.json')
V2_SUMMARY_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-19-crop-sizing-v2.json')
REPORT_MD = os.path.join(REPO_ROOT, 'reports', '2026-09-26-crop-sizing-v3.md')


def ramp(depression=10.0, box_w=200.0, box_h=60.0, pano_h=6656, x=None):
    """A gold ramp at a known depression, its apron centred on the label."""
    pano_w = pano_h * 2
    y = pano_h / 2 + CropRunner.elevation_deg_to_px(depression, pano_h)
    x = pano_w / 2 if x is None else x
    return {'pano_id': 'p', 'key': 'det:0', 'pano_w': pano_w, 'pano_h': pano_h, 'x': x, 'y': y,
            'box_w': box_w, 'box_h': box_h, 'box_cx': x, 'box_cy': y, 'depression_deg': depression}


class TestStudyLogic:

    def test_fill_is_the_apron_against_the_window_actually_cut(self):
        r = ramp(depression=12.0, box_w=300.0)
        scored = csv3.score_context_width([r], 5.0)
        box = csv3.window_for(r, width_m=5.0)
        assert isinstance(box.width, int)
        assert scored['fill_p50'] == pytest.approx(300.0 / box.width, rel=1e-12)

    def test_the_explicit_width_path_is_the_shipped_rule_at_the_shipped_width(self):
        """score_rule('v3') goes through crop_window_width(sizing_rule='v3'); score_context_width goes
        through the explicit-width seam. At the module's width they must be the same crops."""
        ramps = [ramp(depression=d, pano_h=h) for d in (0.5, 6.0, 14.0, 25.0, 50.0) for h in (1664, 8192)]
        assert csv3.score_rule(ramps, 'v3') == csv3.score_context_width(ramps, CropRunner.V3_CONTEXT_WIDTH_M)

    def test_a_wider_context_never_raises_the_median_fill(self):
        ramps = [ramp(depression=d) for d in (1.0, 5.0, 9.0, 13.0, 18.0, 24.0)]
        p50s = [row['fill_p50'] for row in csv3.context_width_sweep(ramps, [4.0, 5.0, 6.0, 7.0, 8.0])]
        assert all(a >= b for a, b in zip(p50s, p50s[1:]))
        assert p50s[0] > p50s[-1]

    def test_matched_width_is_the_grid_value_nearest_the_target(self):
        ramps = [ramp(depression=d) for d in (3.0, 8.0, 12.0, 16.0, 21.0)]
        grid = [4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0]
        sweep = csv3.context_width_sweep(ramps, grid)
        target = sweep[3]['fill_p50']
        assert csv3.matched_context_width(ramps, target, grid) == 5.5
        assert csv3.matched_context_width(ramps, target + 1e-6, grid) == 5.5

    def test_matched_width_of_nothing_is_undefined(self):
        assert csv3.matched_context_width([], 0.37, csv3.GRID_M) is None

    def test_the_grid_is_what_the_report_says(self):
        assert csv3.GRID_M[0] == 4.0 and csv3.GRID_M[-1] == 8.0
        assert len(csv3.GRID_M) == 41 and csv3.GRID_STEP_M == 0.1

    def test_extent_fit_is_one_when_the_aprons_are_a_power_of_the_window(self):
        """Aprons exactly 0.4 x window^1.3 - log-linear in the window, so R-squared is 1."""
        ramps = []
        for d in (2.0, 7.0, 12.0, 17.0, 22.0, 30.0):
            r = ramp(depression=d)
            window_deg = CropRunner.azimuth_px_to_deg(csv3.window_for(r, width_m=5.0).width, r['pano_w'])
            r['box_w'] = CropRunner.azimuth_deg_to_px(0.4 * window_deg ** 1.3, r['pano_w'])
            ramps.append(r)
        assert csv3.extent_fit(ramps, width_m=5.0)['r2'] == pytest.approx(1.0, abs=1e-12)

    def test_extent_fit_is_undefined_for_a_constant_apron(self):
        ramps = [ramp(depression=d, box_w=250.0) for d in (2.0, 9.0, 20.0)]
        assert csv3.extent_fit(ramps, rule='v2')['r2'] is None

    def test_the_isotonic_ceiling_is_one_on_any_monotone_law(self):
        ramps = [ramp(depression=d, box_w=40.0 * (1 + d) ** 0.7) for d in (0.5, 2, 4, 8, 13, 19, 27, 40)]
        ceiling = csv3.depression_only_ceiling(ramps)
        assert ceiling['isotonic_r2'] == pytest.approx(1.0, abs=1e-12)
        assert ceiling['n_params'] == 3

    def test_the_parametric_fit_is_one_on_an_exact_log_dep_law(self):
        ramps = [ramp(depression=d, box_w=30.0 * d ** 0.6 * math.exp(0.01 * d))
                 for d in (0.5, 2, 4, 8, 13, 19, 27, 40)]
        assert csv3.depression_only_ceiling(ramps)['parametric_r2'] == pytest.approx(1.0, abs=1e-9)

    def test_the_isotonic_ceiling_bounds_a_monotone_rule(self):
        """The property that makes it a ceiling: no monotone depression-only window beats it."""
        widths = [90, 150, 120, 260, 230, 400, 380, 700, 650, 1200]
        deps = [1, 3, 5, 8, 11, 15, 19, 24, 31, 42]
        ramps = [ramp(depression=d, box_w=w) for d, w in zip(deps, widths)]
        ceiling = csv3.depression_only_ceiling(ramps)['isotonic_r2']
        for rule in ('v2', 'v3'):
            assert csv3.extent_fit(ramps, rule=rule)['r2'] <= ceiling + 1e-12

    def test_isotonic_pools_violators_and_ties(self):
        fit = csv3._isotonic_fit([1, 2, 2, 3], [1.0, 3.0, 1.0, 0.0])
        assert list(fit) == pytest.approx([1.0, 4.0 / 3, 4.0 / 3, 4.0 / 3])

    def test_legacy_distance_is_the_production_line(self):
        """legacy_distance_m re-evaluates _reference_crop_size's first step; it must be that line."""
        for dep in (0.0, 5.0, 20.0):
            ref_offset = -CropRunner.elevation_deg_to_px(dep, CropRunner.V1_REF_HEIGHT)
            assert csv3.legacy_distance_m(dep) == pytest.approx(
                CropRunner.V1_DIST_INTERCEPT + CropRunner.V1_DIST_SLOPE * ref_offset, rel=1e-12)
        size_at = CropRunner._reference_crop_size(
            -CropRunner.elevation_deg_to_px(10.0, CropRunner.V1_REF_HEIGHT))
        assert size_at == pytest.approx(CropRunner.V1_SIZE_COEF * csv3.legacy_distance_m(10.0)
                                        ** CropRunner.V1_SIZE_EXP)

    def test_the_legacy_line_reaches_zero_at_35_1_degrees(self):
        crossing = csv3.legacy_zero_crossing_deg()
        assert crossing == pytest.approx(35.1, abs=0.05)
        assert csv3.legacy_distance_m(crossing - 0.01) > 0
        assert csv3.legacy_distance_m(crossing + 0.01) == 0.0

    def test_distance_exponent_recovers_a_planted_slope(self):
        ramps = []
        for d in (1.0, 4.0, 9.0, 14.0, 20.0, 28.0):
            r = ramp(depression=d)
            r['box_w'] = CropRunner.azimuth_deg_to_px(50.0 * CropRunner.blend_distance_m(d) ** -1.0,
                                                      r['pano_w'])
            ramps.append(r)
        got = csv3.distance_exponent(ramps, 'blend')
        assert got['slope'] == pytest.approx(-1.0, abs=1e-9)
        assert got['r2'] == pytest.approx(1.0, abs=1e-12)

    def test_distance_exponent_drops_ramps_past_the_legacy_zero(self):
        ramps = [ramp(depression=d, box_w=100 + d) for d in (5.0, 15.0, 40.0, 60.0)]
        assert csv3.distance_exponent(ramps, 'legacy')['n'] == 2
        assert csv3.distance_exponent(ramps, 'blend')['n'] == 4

    def test_window_change_is_one_where_v3_reproduces_v2(self):
        """Choose W so v3's angle equals v2's at one depression: every ratio there is exactly 1."""
        dep = 12.0
        v2 = csv3.v2_fov_at(dep)
        width = 2 * CropRunner.blend_distance_m(dep) * math.tan(math.radians(v2) / 2)
        change = csv3.window_change([ramp(depression=dep) for _ in range(5)], width)
        assert change['ratio_p50'] == pytest.approx(1.0, abs=1e-12)
        assert change['frac_over_10pct'] == 0.0
        band = next(b for b in change['bands'] if b['band'] == '10-15')
        assert band['n'] == 5 and band['ratio_p50'] == pytest.approx(1.0, abs=1e-12)

    def test_the_bands_partition_the_ramps(self):
        ramps = [ramp(depression=d) for d in (-3.0, 0.0, 2.5, 4.9, 5.5, 14.0, 19.9, 29.0, 35.0, 40.5, 70.0)]
        change = csv3.window_change(ramps, 5.8)
        assert sum(b['n'] for b in change['bands']) == len(ramps)
        by = {b['band']: b['n'] for b in change['bands']}
        assert by['<2'] == 2 and by['2-5'] == 2 and by['>=40'] == 2

    def test_cap_onset_finds_the_v2_boundary(self):
        onset = csv3.cap_onset_deg(csv3.v2_fov_at)
        assert csv3.v2_fov_at(onset) == CropRunner.CROP_MAX_FOV_DEG
        assert csv3.v2_fov_at(onset - 1e-4) < CropRunner.CROP_MAX_FOV_DEG

    def test_the_rejected_alternative_is_v2_with_the_blend_distance(self):
        """Option A at a depression where both distances agree must equal v2 there."""
        # The two estimators cross between 5 deg (legacy shorter) and 10 deg (legacy longer).
        lo, hi = 5.0, 10.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if csv3.legacy_distance_m(mid) < CropRunner.blend_distance_m(mid):
                lo = mid
            else:
                hi = mid
        assert csv3.rule_a_fov_at(lo) == pytest.approx(csv3.v2_fov_at(lo), rel=1e-6)

    def test_population_covers_every_top_level_key(self, tmp_path):
        bundle = tmp_path / 'city'
        bundle.mkdir()
        boxes = {'panos': {}}
        with open(bundle / 'records.jsonl', 'w', encoding='utf-8') as f:
            for i, d in enumerate((2.0, 8.0, 14.0, 22.0, 33.0)):
                pid = 'pano%02d' % i
                f.write(json.dumps({'pano': {'panorama_id': pid, 'width': 4096, 'height': 2048}}) + '\n')
                yv = 0.5 + d / 180.0 / 1.0 * 1.0
                boxes['panos'][pid] = {'det:0': {'status': 'boxed', 'point': {'x': 0.5, 'y': yv},
                                                 'w': 0.01 + 0.002 * i, 'h': 0.01, 'cx': 0.5, 'cy': yv}}
        with open(bundle / 'boxes.json', 'w', encoding='utf-8') as f:
            json.dump(boxes, f)
        out = tmp_path / 'summary.json'
        assert csv3.main(['--bundle', 'city=%s' % bundle, '--write', str(out)]) == 0
        with open(out, encoding='utf-8') as f:
            summary = json.load(f)
        assert set(summary['population']['covers']) == set(summary) - {'meta', 'population'}
        assert summary['population']['n'] == 5


@pytest.fixture(scope='module')
def summary():
    with open(SUMMARY_JSON, encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def v2_summary():
    with open(V2_SUMMARY_JSON, encoding='utf-8') as f:
        return json.load(f)


CITIES = {'richmond', 'sao_paulo', 'annapolis', 'paterson'}


def _blocks(summary):
    """Every city block plus the pooled one, by name."""
    return dict(summary['cities'], pooled=summary['pooled'])


class TestTheRuleIsWhatTheReportDescribes:
    """If a constant is retuned, the committed numbers stop describing the shipped rule."""

    def test_the_v3_constants_are_the_shipped_ones(self, summary):
        assert summary['constants']['v3'] == {
            'camera_height_m': CropRunner.V3_CAMERA_HEIGHT_M, 'blend_deg': CropRunner.V3_BLEND_DEG,
            'dist_cap_m': CropRunner.V3_DIST_CAP_M, 'context_width_m': CropRunner.V3_CONTEXT_WIDTH_M}

    def test_the_shipped_width_is_the_fitted_one(self, summary):
        assert summary['selection']['matched_context_width_m'] == CropRunner.V3_CONTEXT_WIDTH_M

    def test_the_v2_constants_are_the_v2_studys(self, summary, v2_summary):
        """Cross-artifact: v2 here is the same rule the v2 report scored."""
        assert summary['constants']['v2'] == v2_summary['constants']

    def test_the_default_is_still_v2(self):
        assert CropRunner.CROP_RULE_VERSION == 'v2'


class TestCommittedFindings:
    """The report's conclusions, pinned. Offline - the gold itself is not in this repo."""

    def test_same_gold_as_the_v2_study(self, summary, v2_summary):
        assert summary['population']['n'] == summary['pooled']['n'] == 658
        assert set(summary['cities']) == CITIES
        for city in CITIES:
            assert summary['cities'][city]['n'] == v2_summary['cities'][city]['n'], city

    def test_v2_replicates_exactly(self, summary, v2_summary):
        """The v2 rows here are recomputed, not copied - and equal the v2 artifact's to the bit."""
        for name, block in _blocks(summary).items():
            ref = v2_summary['pooled'] if name == 'pooled' else v2_summary['cities'][name]
            for key in ('fill_p50', 'containment', 'frac_clearing_too_tight', 'fits_by_size',
                        'stored_width_p50', 'stored_width_p90'):
                assert block['v2'][key] == ref['v2'][key], (name, key)

    def test_v3_is_compared_at_v2s_median_fill(self, summary):
        pooled = summary['pooled']
        assert summary['selection']['target_fill_p50'] == pooled['v2']['fill_p50']
        assert pooled['v3']['fill_p50'] == pytest.approx(pooled['v2']['fill_p50'], abs=0.01)

    def test_the_two_criteria_disagree_by_two_steps_and_the_report_says_so(self, summary):
        sel = summary['selection']
        assert sel['matched_context_width_m'] == 5.8
        assert sel['band_centre_width_m'] == 6.0

    def test_finding_2_dispersion_falls_everywhere(self, summary):
        """The non-monotone metrics - the ones a bigger window cannot buy."""
        for name, block in _blocks(summary).items():
            assert block['v3']['fill_log_sd'] < block['v2']['fill_log_sd'], name
            assert block['v3']['fill_p90_over_p10'] < block['v2']['fill_p90_over_p10'], name

    def test_finding_2_the_window_tracks_the_apron_better_everywhere(self, summary):
        for name, block in _blocks(summary).items():
            assert block['r2']['v3'] > block['r2']['v2'], name

    def test_finding_2_neither_rule_exceeds_the_monotone_ceiling(self, summary):
        """The isotonic fit bounds every monotone depression-only rule, so this is a check on the
        arithmetic as much as a finding - and the parametric fit is NOT a bound, which the pooled
        row shows."""
        for name, block in _blocks(summary).items():
            ceiling = block['r2']['ceiling']['isotonic_r2']
            assert block['r2']['v2'] <= ceiling and block['r2']['v3'] <= ceiling, name
        pooled = summary['pooled']['r2']
        assert pooled['v3'] > pooled['ceiling']['parametric_r2']

    def test_finding_1_the_blend_distance_is_the_better_distance(self, summary):
        """Against the apron's angular width the blend distance explains more, and its log-log slope
        sits nearer the geometric -1 than the legacy line's, in every city and pooled."""
        for name, block in _blocks(summary).items():
            legacy, blend = block['exponent']['legacy'], block['exponent']['blend']
            assert blend['r2'] > legacy['r2'], name
            assert abs(blend['slope'] + 1) < abs(legacy['slope'] + 1), name
        pooled = summary['pooled']['exponent']
        assert -1.25 <= pooled['blend']['slope'] <= -0.95
        assert pooled['legacy']['n'] < pooled['blend']['n'] == 658

    def test_finding_1_the_legacy_line_is_not_distance_shaped(self, summary):
        geometry = summary['rule_geometry']
        assert geometry['legacy_zero_crossing_deg'] == pytest.approx(35.15, abs=0.01)
        at = {row['depression_deg']: row for row in summary['distance_table']}
        assert at[45.0]['legacy_m'] == 0.0
        assert at[45.0]['blend_m'] == pytest.approx(CropRunner.V3_CAMERA_HEIGHT_M)

    def test_the_clamps_under_v3(self, summary):
        geometry = summary['rule_geometry']
        assert geometry['v3_cap_onset_deg'] > geometry['v2_cap_onset_deg']
        assert geometry['v3_horizon_window_deg'] > geometry['min_fov_deg'], 'the floor is unreachable'

    def test_finding_3_what_moves(self, summary):
        change = summary['window_change']
        assert 0.3 < change['frac_over_10pct'] < 0.5
        assert change['frac_over_20pct'] < 0.05
        assert change['ratio_p10'] < 1 < change['ratio_p90']
        assert sum(b['n'] for b in change['bands']) == 658
        bands = {b['band']: b for b in change['bands']}
        assert bands['2-5']['ratio_p50'] < 1 < bands['15-20']['ratio_p50']

    def test_finding_3_production_median_gets_wider(self, summary):
        rows = {row['percentile']: row for row in summary['production']['rows']}
        assert rows['p50']['ratio'] > 1.05
        assert rows['p90']['ratio'] < 0.8

    def test_what_this_does_not_fix_annapolis(self, summary):
        """The honest exception survives the swap: an extent problem, not a distance problem."""
        assert summary['cities']['annapolis']['v3']['frac_clearing_too_tight'] < 0.45

    def test_the_rejected_alternative_is_a_row(self, summary):
        rule_a = summary['rule_a_blend_powerlaw']
        assert rule_a['n'] == 658
        assert rule_a['r2'] < summary['pooled']['r2']['v3']

    def test_population_covers_every_top_level_key(self, summary):
        assert set(summary['population']['covers']) == set(summary) - {'meta', 'population'}

    def test_the_run_is_recorded(self, summary):
        meta = summary['meta']
        assert len(meta['rampnet_commit']) == 40
        assert set(meta['bundles']) == CITIES
        assert '--write reports/data/2026-09-26-crop-sizing-v3.json' in meta['generated_by']
