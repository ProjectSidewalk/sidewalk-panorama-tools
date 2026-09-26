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
import random
import statistics
import subprocess
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
FIGURE = os.path.join(REPO_ROOT, 'reports', 'figures', '2026-09-26-crop-sizing-v3-examples.jpg')


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
        # Deliberately NOT in depression order: the gold is unordered, and a fit handed sorted input
        # cannot show that it returns its answer in the caller's order.
        ramps = [ramp(depression=d, box_w=40.0 * (1 + d) ** 0.7) for d in (13, 0.5, 40, 4, 27, 2, 19, 8)]
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
        """Fed out of order: (x, y) pairs (2, 3), (1, 1), (3, 0), (2, 1) - the answer comes back per input."""
        fit = csv3._isotonic_fit([2, 1, 3, 2], [3.0, 1.0, 0.0, 1.0])
        assert list(fit) == pytest.approx([4.0 / 3, 1.0, 4.0 / 3, 4.0 / 3])

    def test_isotonic_fit_is_returned_in_input_order(self):
        """#157 review item 3 (kills S4: a PAVA that forgets to un-sort). Every isotonic test used to feed
        x already sorted, so an implementation returning the sorted-order fit passed all of them."""
        rng = random.Random(0)
        x = [rng.uniform(0, 40) for _ in range(30)]
        y = [math.log(1 + xi) + rng.gauss(0, 0.3) for xi in x]
        fit = csv3._isotonic_fit(x, y)
        order = sorted(range(len(x)), key=lambda i: x[i])
        sorted_fit = csv3._isotonic_fit([x[i] for i in order], [y[i] for i in order])
        for rank, i in enumerate(order):
            assert fit[i] == pytest.approx(sorted_fit[rank])
        assert all(fit[a] <= fit[b] + 1e-12 for a, b in zip(order, order[1:]))

    def test_isotonic_fit_gives_tied_x_one_value(self):
        """Kills S5 (tie pooling removed): the pools-ties case above gives the same fit either way."""
        assert list(csv3._isotonic_fit([1, 1], [0.0, 1.0])) == pytest.approx([0.5, 0.5])

    def test_the_dispersion_metrics_are_what_they_say(self):
        """Kills S10 (p90/p50 for p90/p10) and S14 (pstdev for stdev): fill_log_sd and fill_p90_over_p10
        are the headline metrics, and only the regenerated artifact pinned them. Fills are planted
        exactly by fixing the box width."""
        fills = [0.1, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.6, 0.8, 0.9, 1.2]
        ramps = [ramp(box_w=1000.0 * f) for f in fills]
        boxes = [CropRunner.CropBox(0, 0, 1000, 667, False) for _ in ramps]
        scored = csv3._score_boxes(ramps, boxes)
        assert scored['fill_log_sd'] == pytest.approx(statistics.stdev([math.log(f) for f in fills]),
                                                      rel=1e-12)
        assert scored['fill_p90_over_p10'] == pytest.approx(0.9 / 0.2, rel=1e-12)

    def test_window_change_counts_narrower_windows_too(self):
        """Kills S8 (counting only windows that widen): the other window_change test has every ratio 1."""
        dep = 12.0
        v2 = csv3.v2_fov_at(dep)
        width = 2 * CropRunner.blend_distance_m(dep) * math.tan(math.radians(0.8 * v2) / 2)
        change = csv3.window_change([ramp(depression=dep) for _ in range(4)], width)
        assert change['ratio_p50'] == pytest.approx(0.8)
        assert change['frac_over_10pct'] == 1.0 and change['frac_over_20pct'] == 0.0

    def test_matched_width_takes_the_first_grid_value_on_a_tie(self):
        """Kills S2 (last on a tie): every width caps a 60-degree ramp at 90 degrees, so every fill ties
        and the docstring's "first" is the only thing deciding."""
        ramps = [ramp(depression=60.0)]
        grid = [4.0, 5.0, 6.0]
        assert len({row['fill_p50'] for row in csv3.context_width_sweep(ramps, grid)}) == 1
        assert csv3.matched_context_width(ramps, 0.0, grid) == 4.0

    def test_option_a_is_matched_on_the_nearest_fill(self):
        """Kills S3 (the matched-scale objective without abs): option A's matched scale anchors D7."""
        ramps = [ramp(depression=d, box_w=150.0 + 10 * d) for d in (2.0, 6.0, 11.0, 17.0, 24.0)]
        k0 = 2.2
        target = csv3.score_fov_fn(ramps, lambda dep: csv3.rule_a_fov_at(dep, k0))['fill_p50']
        block = csv3.rule_a_block({'c': ramps}, ramps, target)
        assert block['matched_scale'] == k0
        assert block['pooled_matched']['fill_p50'] == target

    def test_production_ratio_is_v3_over_v2(self, tmp_path, monkeypatch):
        """Kills S9 (v2/v3): production_block is the x1.13 / x0.74 headline, with no synthetic test."""
        census = {'by_label_type': {'CurbRamp': {'n': 7, 'depression_deg': {
            'p10': 3.0, 'p50': 14.8, 'p90': 28.0, 'p99': 50.0}}}}
        path = tmp_path / 'census.json'
        path.write_text(json.dumps(census), encoding='utf-8')
        monkeypatch.setattr(csv3, 'REPO_ROOT', str(tmp_path))
        block = csv3.production_block(5.8, path=str(path))
        for row in block['rows']:
            assert row['ratio'] == pytest.approx(csv3.v3_fov_at(row['depression_deg'], 5.8)
                                                 / csv3.v2_fov_at(row['depression_deg']))
        assert block['n'] == 7

    def test_city_block_scores_v3_at_the_width_it_is_given(self):
        """Kills S11 (v3 scored at a nudged width): at the shipped width city_block's v3 is the rule."""
        ramps = [ramp(depression=d, box_w=120 + 9 * d) for d in (1.0, 4.0, 9.0, 15.0, 22.0, 31.0)]
        block = csv3.city_block(ramps, CropRunner.V3_CONTEXT_WIDTH_M)
        assert block['v3'] == csv3.score_rule(ramps, 'v3')

    def test_build_summary_wires_the_criteria_and_the_geometry(self, monkeypatch):
        """Kills S12 (band-centre on the v2 target) and S16 (cap onset at a fixed 6.0 m): both are
        computed in build_summary, and only the committed JSON - regenerated by this code - pinned them."""
        monkeypatch.setattr(csv3, 'production_block', lambda w: {})
        monkeypatch.setattr(csv3, 'rampnet_commit', lambda p: None)
        ramps = [ramp(depression=d, box_w=100 + 12 * d) for d in (1.0, 3.0, 6.0, 10.0, 15.0, 21.0, 28.0)]
        s = csv3.build_summary({'c': ramps}, {'c': '/x'}, [])
        matched = s['selection']['matched_context_width_m']
        assert s['selection']['band_centre_width_m'] == csv3.matched_context_width(
            ramps, csv3.BAND_CENTRE_FILL, csv3.GRID_M)
        assert s['rule_geometry']['v3_cap_onset_deg'] == csv3.cap_onset_deg(
            lambda d: csv3.v3_fov_at(d, matched))

    def test_distance_exponent_keeps_a_small_positive_legacy_distance(self):
        """Kills S15 (d > 0.5 for d > 0): only d <= 0 has no log; a ramp just short of the legacy zero
        crossing still counts."""
        crossing = csv3.legacy_zero_crossing_deg()
        ramps = [ramp(depression=d, box_w=100 + d) for d in (5.0, 15.0, crossing - 0.05)]
        assert 0 < csv3.legacy_distance_m(crossing - 0.05) < 0.5
        assert csv3.distance_exponent(ramps, 'legacy')['n'] == 3

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

    def test_option_a_scale_is_a_multiplier_on_the_angle_below_the_cap(self):
        dep = 12.0
        assert csv3.rule_a_fov_at(dep, 2.0) == pytest.approx(csv3.rule_a_fov_at(dep, 1.0) * 2.0)

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
        assert_every_key_is_claimed_once(summary)
        assert summary['populations']['gold']['n'] == 5
        assert summary['populations']['clamp_census']['covers'] == ['production']

    def test_a_mixed_block_is_not_named_for_one_provider(self):
        """#157 review item 10: the pooled block said 'gsv' with 430 of its 658 ramps Mapillary."""
        ramps = [ramp(), ramp(), dict(ramp(), pano_id='1234567890')]
        block = csv3.city_block(ramps, 5.8)
        assert block['provider'] == 'mixed'
        assert block['provider_counts'] == {'gsv': 2, 'mapillary': 1}
        assert csv3.city_block(ramps[:2], 5.8)['provider'] == 'gsv'
        assert csv3.city_block(ramps[2:], 5.8)['provider'] == 'mapillary'

    def test_meta_records_bundles_relative_to_their_checkout(self, tmp_path):
        """#157 review item 11: the artifact baked in D:/Git/RampNet, so it reproduced on one machine."""
        root = tmp_path / 'RampNet'
        bundle = root / 'benchmark' / 'richmond'
        bundle.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        assert csv3.bundle_spec(str(bundle)) == 'benchmark/richmond'
        command = csv3.canonical_command(
            ['--bundle', 'richmond=%s' % bundle, '--write',
             os.path.join(REPO_ROOT, 'reports', 'data', 'x.json')], {'richmond': str(bundle)})
        assert command == ('python reports/scripts/crop_sizing_v3.py --bundle '
                           'richmond=<RampNet>/benchmark/richmond --write reports/data/x.json')
        assert str(tmp_path) not in command

    def test_a_bundle_outside_a_checkout_is_recorded_as_given(self, tmp_path):
        bundle = tmp_path / 'loose'
        bundle.mkdir()
        assert csv3.bundle_spec(str(bundle)) == str(bundle).replace('\\', '/')

    def test_the_option_a_table_is_computed_from_the_artifact(self):
        """#157 review item 2: the report's A-vs-v3 table is generated, so it is asserted, not typed."""
        def scored(fill, sd, spread, clear, cont, r2=None):
            out = {'n': 10, 'fill_p50': fill, 'fill_log_sd': sd, 'fill_p90_over_p10': spread,
                   'frac_clearing_too_tight': clear, 'containment': cont}
            return out if r2 is None else dict(out, r2=r2)
        fake = {'rule_a_blend_powerlaw': {
                    'cities_matched': {'annapolis': scored(0.5, 0.43, 2.95, 0.4885, 0.84, r2=0.522)},
                    'pooled_matched': scored(0.368, 0.402, 2.73, 0.7736, 0.944, r2=0.604)},
                'cities': {'annapolis': {'v3': scored(0.513, 0.427, 2.81, 0.4427, 0.855),
                                         'r2': {'v3': 0.530}}},
                'pooled': {'v3': scored(0.370, 0.406, 2.78, 0.7523, 0.944), 'r2': {'v3': 0.612}}}
        lines = csv3.rule_a_table(fake)
        assert lines[2] == ('| annapolis | 10 | 0.500 / 0.513 | 0.430 / 0.427 | 2.95 / 2.81 | 0.522 / 0.530 '
                            '| 48.9% / 44.3% | 0.840 / 0.855 |')
        assert lines[3].startswith('| **pooled** | **10** | **0.368 / 0.370** |')
        assert len(lines) == 4

    def test_the_figure_is_labelled_with_the_rules_it_shows(self, tmp_path, monkeypatch):
        """#157 review item 13: render_examples hard-coded a v1/v2 caption. With rules=('v2','v3') the
        panels are the v2 and v3 windows and the caption names those; the default stays v1/v2."""
        from PIL import Image, ImageDraw
        import crop_sizing_v2
        pano = tmp_path / 'p.jpg'
        Image.new('RGB', (2048, 1024), (90, 90, 90)).save(pano)
        example = dict(ramp(depression=12.0, pano_h=1024), city='c', pano_id='abcdefghijklmnop',
                       pano_path=str(pano), v1=(900, 500, 200, 200), v2=(900, 520, 180, 120),
                       v3=(880, 510, 240, 160))
        captions = []
        real_text = ImageDraw.ImageDraw.text
        monkeypatch.setattr(ImageDraw.ImageDraw, 'text',
                            lambda self, xy, text, *a, **k: (captions.append(text),
                                                             real_text(self, xy, text, *a, **k)))
        size = crop_sizing_v2.render_examples([example], str(tmp_path / 'f.jpg'), rules=('v2', 'v3'))
        assert size[0] > 0 and (tmp_path / 'f.jpg').is_file()
        assert 'v2: 180x120 px' in captions[-1] and 'v3: 240x160 px' in captions[-1]
        assert 'v1' not in captions[-1]
        assert 'fill %.2f' % (example['box_w'] / 240) in captions[-1]
        crop_sizing_v2.render_examples([example], str(tmp_path / 'g.jpg'))
        assert 'v1: 200 px square' in captions[-1] and 'v2: 180x120 px' in captions[-1]

    def test_the_generic_caption_takes_the_angle_on_the_azimuth_axis(self, tmp_path, monkeypatch):
        """A window width is horizontal, so its angle is azimuthal. The test above uses a 2:1 pano,
        where the two axes agree; a square one tells them apart (#157 final review)."""
        from PIL import Image, ImageDraw
        import crop_sizing_v2
        pano = tmp_path / 'p.jpg'
        Image.new('RGB', (2048, 2048), (90, 90, 90)).save(pano)
        example = dict(ramp(depression=12.0, pano_h=2048), pano_w=2048, city='c',
                       pano_id='abcdefghijklmnop', pano_path=str(pano), v2=(900, 900, 180, 120),
                       v3=(880, 900, 240, 160))
        captions = []
        real_text = ImageDraw.ImageDraw.text
        monkeypatch.setattr(ImageDraw.ImageDraw, 'text',
                            lambda self, xy, text, *a, **k: (captions.append(text),
                                                             real_text(self, xy, text, *a, **k)))
        crop_sizing_v2.render_examples([example], str(tmp_path / 'f.jpg'), rules=('v2', 'v3'))
        assert '%.1f deg' % CropRunner.azimuth_px_to_deg(240, 2048) in captions[-1]

    def test_figure_examples_put_the_v3_window_beside_the_v2_one(self, monkeypatch):
        """Without this only the RAMPNET_ROOT-gated reproduction could see a v3 slot filled with the v2
        window, and CI does not set RAMPNET_ROOT."""
        example = dict(ramp(depression=4.0, box_w=300.0), city='c')
        monkeypatch.setattr(csv3, 'pick_examples', lambda by_city, bundles: [dict(example)])
        out = csv3.figure_examples({}, {})
        assert out[0]['v3'] == tuple(csv3.window_for(example, rule='v3'))[:4]
        assert out[0]['v3'] != out[0]['v2']

    def test_the_figure_examples_key_is_claimed(self, monkeypatch):
        """populations() was otherwise checked only against the committed JSON it produced, and the
        synthetic main() run above writes no figure, so has no figure_examples key to claim."""
        monkeypatch.setattr(csv3, 'production_block', lambda w: {})
        monkeypatch.setattr(csv3, 'rampnet_commit', lambda p: None)
        ramps = [dict(ramp(depression=d, box_w=100 + 12 * d), city='c')
                 for d in (1.0, 3.0, 6.0, 10.0, 15.0, 21.0, 28.0)]
        summary = csv3.build_summary({'c': ramps}, {'c': '/x'}, [], examples=[
            dict(ramps[0], v2=(0, 0, 200, 133), v3=(0, 0, 220, 147))])
        assert 'figure_examples' in summary['populations']['gold']['covers']
        assert_every_key_is_claimed_once(summary)

    def test_canonical_command_relativises_the_figure_too(self):
        command = csv3.canonical_command(
            ['--figure', os.path.join(csv3.REPO_ROOT, 'reports', 'figures', 'f.jpg')], {})
        assert command.endswith('--figure reports/figures/f.jpg')

    def test_canonical_command_survives_an_output_on_another_drive(self, monkeypatch):
        """On Windows commonpath raises ValueError for paths on two drives (--write D:/x.json from a
        checkout on C:), and it raised inside build_summary - after the whole study, before anything was
        written. Such a path is outside the repo, so it is recorded as given (#157 final review)."""
        def across_drives(paths):
            raise ValueError("Paths don't have the same drive")
        monkeypatch.setattr(csv3.os.path, 'commonpath', across_drives)
        assert csv3.canonical_command(['--write', 'Z:/out/x.json', '--figure', 'Z:/out/f.jpg'], {}) == (
            'python reports/scripts/crop_sizing_v3.py --write Z:/out/x.json --figure Z:/out/f.jpg')


def assert_every_key_is_claimed_once(summary):
    """Every top-level key is computed on exactly one named population, or declared to be on none."""
    pops = summary['populations']
    claimed = pops['gold']['covers'] + pops['clamp_census']['covers'] + pops['no_population']
    assert len(claimed) == len(set(claimed)), claimed
    assert set(claimed) == set(summary) - {'meta', 'populations'}


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
        assert summary['populations']['gold']['n'] == summary['pooled']['n'] == 658
        assert set(summary['cities']) == CITIES
        for city in CITIES:
            assert summary['cities'][city]['n'] == v2_summary['cities'][city]['n'], city

    def test_v2_replicates_exactly(self, summary, v2_summary):
        """The v2 rows here are recomputed, not copied - and equal the v2 artifact's to the bit, on EVERY
        key the two artifacts share (the v2 artifact has no log-sd or p90/p10). The window_deg_* keys
        agree only because every gold pano is 2:1: the v2 study takes the angle on the elevation axis
        (w / pano_h), this one on the azimuth axis, as a width must be."""
        for name, block in _blocks(summary).items():
            ref = v2_summary['pooled'] if name == 'pooled' else v2_summary['cities'][name]
            shared = set(block['v2']) & set(ref['v2'])
            assert {'fill_p10', 'fill_p50', 'fill_p90', 'containment', 'frac_clearing_too_tight',
                    'fits_by_size', 'window_deg_p50', 'stored_width_p50', 'stored_width_p90'} <= shared
            for key in sorted(shared):
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

    def test_v2s_cap_binds_before_the_legacy_line_reaches_zero(self, summary):
        """#157 review item 5: past 26.55 deg the 90-degree cap sizes every v2 window, so the 2013 line's
        zero at 35.15 deg (and the 1500-px clamp behind it) is a v1 fact, not a v2 one."""
        geometry = summary['rule_geometry']
        assert geometry['v2_cap_onset_deg'] < geometry['legacy_zero_crossing_deg']
        assert csv3.v2_fov_at(geometry['v2_cap_onset_deg'] + 1.0) == CropRunner.CROP_MAX_FOV_DEG

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

    def test_option_a_is_compared_like_for_like_and_the_trade_is_real(self, summary):
        """Option A (blend distance into v2's power law) at its own median-matched scale. It tracks
        the apron less well than v3 (R-squared, every city) but its fill is slightly LESS dispersed
        pooled - a genuine trade the report states rather than a clean win, pinned so a re-run that
        changes the sign of either half has to change the prose too."""
        rule_a = summary['rule_a_blend_powerlaw']
        matched = rule_a['pooled_matched']
        assert matched['n'] == 658
        assert matched['fill_p50'] == pytest.approx(summary['pooled']['v2']['fill_p50'], abs=0.01)
        assert matched['r2'] < summary['pooled']['r2']['v3']
        for city, block in rule_a['cities_matched'].items():
            assert block['r2'] < summary['cities'][city]['r2']['v3'], city
        assert matched['fill_log_sd'] < summary['pooled']['v3']['fill_log_sd']
        assert matched['fill_log_sd'] < summary['pooled']['v2']['fill_log_sd']

    def test_every_key_names_its_population(self, summary):
        assert_every_key_is_claimed_once(summary)
        assert summary['populations']['clamp_census']['n'] == summary['production']['n']
        assert summary['production']['n'] != summary['populations']['gold']['n']

    def test_the_pooled_block_is_mixed_provider(self, summary):
        pooled = summary['pooled']
        assert pooled['provider'] == 'mixed'
        assert sum(pooled['provider_counts'].values()) == 658
        by_city = {}
        for block in summary['cities'].values():
            by_city[block['provider']] = by_city.get(block['provider'], 0) + block['n']
        assert pooled['provider_counts'] == by_city

    def test_the_run_is_recorded_without_a_machines_paths(self, summary):
        meta = summary['meta']
        assert len(meta['rampnet_commit']) == 40
        assert meta['bundles'] == {c: 'benchmark/%s' % c for c in CITIES}
        assert '--write reports/data/2026-09-26-crop-sizing-v3.json' in meta['generated_by']
        assert '--figure reports/figures/2026-09-26-crop-sizing-v3-examples.jpg' in meta['generated_by']
        for city in CITIES:
            assert '%s=<RampNet>/benchmark/%s' % (city, city) in meta['generated_by']
        assert ':/' not in json.dumps(meta) and ':\\\\' not in json.dumps(meta)

    def test_the_figure_is_committed_and_small(self, summary):
        """#157 review item 13: the v2/v3 twin of the v2 examples sheet, on the v2 sheet's own picks."""
        assert os.path.getsize(FIGURE) < 1.5e6
        examples = summary['figure_examples']
        assert len(examples) == 8
        assert {e['city'] for e in examples} == CITIES
        with open(V2_SUMMARY_JSON, encoding='utf-8') as f:
            v2_examples = json.load(f)['figure_examples']
        assert [(e['pano_id'], e['key']) for e in examples] == [(e['pano_id'], e['key']) for e in v2_examples]
        for mine, theirs in zip(examples, v2_examples):
            assert mine['v2_window_px'] == theirs['v2_window_px']


@pytest.mark.skipif(not os.environ.get('RAMPNET_ROOT'),
                    reason='set RAMPNET_ROOT to a RampNet checkout to re-run the study from source')
def test_the_committed_artifact_reproduces_from_source(tmp_path, summary):
    """The documented command, run against a RampNet checkout at meta.rampnet_commit, reproduces the
    committed JSON - everything but generated_by, which names this run's output paths."""
    root = os.environ['RAMPNET_ROOT']
    if csv3.rampnet_commit(root) != summary['meta']['rampnet_commit']:
        pytest.skip('RAMPNET_ROOT is not at the committed rampnet_commit')
    out = tmp_path / 'v3.json'
    argv = []
    for city in ('richmond', 'sao_paulo', 'paterson', 'annapolis'):
        argv += ['--bundle', '%s=%s' % (city, os.path.join(root, 'benchmark', city))]
    assert csv3.main(argv + ['--write', str(out), '--figure', str(tmp_path / 'f.jpg')]) == 0
    with open(out, encoding='utf-8') as f:
        fresh = json.load(f)
    committed = json.loads(json.dumps(summary))
    for artifact in (fresh, committed):
        del artifact['meta']['generated_by']
    assert fresh == committed


@pytest.fixture(scope='module')
def report():
    """The report with whitespace runs collapsed (so a wrapped phrase still matches) and the typographic
    minus read as ASCII, so '-1.28' is found where the table prints it as a proper minus sign."""
    with open(REPORT_MD, encoding='utf-8') as f:
        return ' '.join(f.read().split()).replace('−', '-')


def _has(report, value, spec='.3f'):
    return format(value, spec) in report


class TestReportMatchesTheArtifact:
    """Every number in the report's prose and tables, transcribed from the committed summary.

    The desk-study convention: a report table is the one place in this repo where a plausible number has
    no compiler and no test, and two counts in an earlier report were wrong by 2x and 6x unnoticed.
    """

    def test_the_reproduce_block_names_the_run(self, summary, report):
        assert summary['meta']['rampnet_commit'] in report
        assert 'reports/data/2026-09-26-crop-sizing-v3.json' in report

    def test_the_selection(self, summary, report):
        sel = summary['selection']
        # In their sentences (#157 review item 9): 0.371 is also a table cell and 6.0 m recurs in the
        # prose, so a bare search passes a report that got either wrong.
        assert "nearest rule v2's (%.3f)" % sel['target_fill_p50'] in report
        assert 'The answer is **%.1f m**' % sel['matched_context_width_m'] in report
        assert 'gives **%.1f m**' % sel['band_centre_width_m'] in report
        assert '%.1f to %.1f m' % (sel['grid_min_m'], sel['grid_max_m']) in report
        assert '%.1f m grid' % sel['grid_step_m'] in report

    def test_the_distance_table(self, summary, report):
        for row in summary['distance_table']:
            dep = '%g°' % row['depression_deg']
            line = '| %s | %.2f m | %.2f m | %.1f° | %.1f° |' % (
                dep, row['legacy_m'], row['blend_m'], row['v2_window_deg'], row['v3_window_deg'])
            assert line in report, line

    def test_the_geometry(self, summary, report):
        g = summary['rule_geometry']
        for key in ('legacy_zero_crossing_deg', 'v2_cap_onset_deg', 'v3_cap_onset_deg',
                    'v3_horizon_window_deg'):
            assert _has(report, g[key], '.2f'), key

    def test_the_exponent_table(self, summary, report):
        for name, block in dict(summary['cities'], pooled=summary['pooled']).items():
            legacy, blend = block['exponent']['legacy'], block['exponent']['blend']
            cells = ['%.2f' % legacy['slope'], '%.3f' % legacy['r2'], str(legacy['n']),
                     '%.2f' % blend['slope'], '%.3f' % blend['r2'], str(blend['n'])]
            if name == 'pooled':
                cells = ['**%s**' % c for c in cells]
            line = '| %s | %s |' % ('**pooled**' if name == 'pooled' else name, ' | '.join(cells))
            assert line in report, line
        excluded = summary['pooled']['exponent']['blend']['n'] - summary['pooled']['exponent']['legacy']['n']
        assert '(%d pooled)' % excluded in report

    def test_the_finding_2_table(self, summary, report):
        for name, block in dict(summary['cities'], pooled=summary['pooled']).items():
            v2, v3, r2 = block['v2'], block['v3'], block['r2']
            cells = [str(block['n']),
                     '%.3f / %.3f' % (v2['fill_p50'], v3['fill_p50']),
                     '%.3f / %.3f' % (v2['fill_log_sd'], v3['fill_log_sd']),
                     '%.2f / %.2f' % (v2['fill_p90_over_p10'], v3['fill_p90_over_p10']),
                     '%.3f / %.3f' % (r2['v2'], r2['v3']),
                     '%.3f' % r2['ceiling']['isotonic_r2'], '%.3f' % r2['ceiling']['parametric_r2']]
            if name == 'pooled':
                cells = ['**%s**' % c for c in cells]
            line = '| %s | %s |' % ('**pooled**' if name == 'pooled' else name, ' | '.join(cells))
            assert line in report, line

    def test_the_checks(self, summary, report):
        pooled = summary['pooled']
        assert ('%.1f%% / %.1f%%' % (100 * pooled['v2']['frac_clearing_too_tight'],
                                    100 * pooled['v3']['frac_clearing_too_tight'])) in report
        assert '%.3f / %.3f' % (pooled['v2']['containment'], pooled['v3']['containment']) in report
        assert '%.1f° / %.1f°' % (pooled['v2']['window_deg_p50'], pooled['v3']['window_deg_p50']) in report
        assert '%d / %d' % (pooled['v2']['stored_width_p50'], pooled['v3']['stored_width_p50']) in report
        sp = summary['cities']['sao_paulo']
        assert '%.3f / %.3f' % (sp['v2']['containment'], sp['v3']['containment']) in report

    def test_option_a(self, summary, report):
        a = summary['rule_a_blend_powerlaw']
        m = a['pooled_matched']
        # In their sentences, not bare: 0.402 is also Richmond's v3 log-sd, so a bare search passes
        # for a report that got option A's number wrong.
        assert '(**×%.1f**, fill p50 **%.3f**)' % (a['matched_scale'], m['fill_p50']) in report
        assert 'log-sd **%.3f** against' % m['fill_log_sd'] in report
        assert 'p90/p10 **%.2f** against' % m['fill_p90_over_p10'] in report
        assert 'pooled R² **%.3f** against' % m['r2'] in report
        for city, label in (('sao_paulo', 'São Paulo'), ('paterson', 'Paterson'),
                            ('richmond', 'Richmond'), ('annapolis', 'Annapolis')):
            assert '%s **%.3f**' % (label, a['cities_matched'][city]['r2']) in report, city
        assert _has(report, a['pooled_at_v2_scale']['fill_p50'])

    def test_the_option_a_table(self, summary, report):
        """#157 review item 2: the per-city A-vs-v3 table is computed by the script, not typed."""
        for line in csv3.rule_a_table(summary):
            assert ' '.join(line.split()) in report, line

    def test_option_a_clears_annapolis_and_the_report_says_so(self, summary, report):
        """The evidence the first draft left out, pinned in its sentences - in §4 and in §6."""
        a = summary['rule_a_blend_powerlaw']
        ann_a, ann_v3 = a['cities_matched']['annapolis'], summary['cities']['annapolis']['v3']
        assert ann_a['frac_clearing_too_tight'] >= 0.45 > ann_v3['frac_clearing_too_tight']
        assert '**Annapolis, %.1f%% against %.1f%%**' % (100 * ann_a['frac_clearing_too_tight'],
                                                        100 * ann_v3['frac_clearing_too_tight']) in report
        assert 'at containment **%.3f** against v3\'s %.3f' % (ann_a['containment'],
                                                             ann_v3['containment']) in report
        assert 'clear it (%.1f%%, at containment %.3f against v3\'s %.3f; §4)' % (
            100 * ann_a['frac_clearing_too_tight'], ann_a['containment'], ann_v3['containment']) in report
        m, v3 = a['pooled_matched'], summary['pooled']['v3']
        assert '(**%.1f%%** against %.1f%%)' % (100 * m['frac_clearing_too_tight'],
                                               100 * v3['frac_clearing_too_tight']) in report
        lower = [c for c, b in a['cities_matched'].items()
                 if b['fill_log_sd'] < summary['cities'][c]['v3']['fill_log_sd']]
        assert sorted(lower) == ['paterson', 'richmond', 'sao_paulo']
        assert "A's fill log-sd is lower in three of four (all but Annapolis)" in report
        spread = [c for c, b in a['cities_matched'].items()
                  if b['fill_p90_over_p10'] < summary['cities'][c]['v3']['fill_p90_over_p10']]
        assert len(spread) == 2 and 'p90/p10 splits two and two' in report
        assert all(b['frac_clearing_too_tight'] > summary['cities'][c]['v3']['frac_clearing_too_tight']
                   for c, b in a['cities_matched'].items())
        assert 'A clears more in all four' in report
        assert 'any more than one global scale could' not in report

    def test_the_v2_cap_sentence(self, summary, report):
        g = summary['rule_geometry']
        assert '**0 m at %.2f°**' % g['legacy_zero_crossing_deg'] in report
        assert 'the 90° cap already binds from **%.2f°**' % g['v2_cap_onset_deg'] in report
        assert 'the 1500-px clamp, not a distance, sizes every v2 crop' not in report

    def test_the_figure_is_in_the_report(self, report):
        assert '(figures/2026-09-26-crop-sizing-v3-examples.jpg)' in report

    def test_what_moves(self, summary, report):
        wc = summary['window_change']
        for key in ('ratio_p10', 'ratio_p50', 'ratio_p90'):
            assert '×%.3f' % wc[key] in report, key
        assert '**%.1f%%** of ramps move by more than 10%% and **%.1f%%** by more than 20%%' % (
            100 * wc['frac_over_10pct'], 100 * wc['frac_over_20pct']) in report
        assert 'moves %.1f%% of windows by more than 10%%' % (100 * wc['frac_over_10pct']) in report
        names = {'<2': '< 2°', '>=40': '≥ 40°'}
        for band in wc['bands']:
            label = names.get(band['band'], band['band'].replace('-', '–') + '°')
            line = '| %s | %d | ×%.2f |' % (label, band['n'], band['ratio_p50'])
            assert line in report, line

    def test_the_production_table(self, summary, report):
        prod = summary['production']
        assert '{:,}'.format(prod['n']) in report
        for row in prod['rows']:
            line = '| %s | %.1f° | %.1f° | %.1f° | ×%.2f |' % (
                row['percentile'], row['depression_deg'], row['v2_window_deg'], row['v3_window_deg'],
                row['ratio'])
            assert line in report, line

    def test_annapolis(self, summary, report):
        ann = summary['cities']['annapolis']
        assert '%.1f%% to **%.1f%%**' % (100 * ann['v2']['frac_clearing_too_tight'],
                                          100 * ann['v3']['frac_clearing_too_tight']) in report

    def test_the_cap_onset_and_camera_height(self, summary, report):
        assert _has(report, summary['constants']['v3']['camera_height_m'], '.4f')
        assert '%g°' % summary['constants']['v3']['blend_deg'] in report

    def test_the_byte_identical_table_size_is_the_tests(self, report):
        with open(os.path.join(REPO_ROOT, 'tests', 'test_crop_runner.py'), encoding='utf-8') as f:
            source = f.read()
        block = source[source.index('MASTER_V2_WINDOWS = ['):]
        block = block[:block.index('\n]')]
        rows = sum(1 for line in block.splitlines() if line.strip().startswith('(('))
        assert rows >= 12
        assert '%d-row table' % rows in report
