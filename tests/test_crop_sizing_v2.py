"""Tests for reports/scripts/crop_sizing_v2.py — the v1-vs-v2 crop-sizing study.

Two jobs, matching how the other report tests here are split:

* the study's own logic, on synthetic ramps whose answers are known by construction;
* the committed conclusions, pinned against reports/data/2026-08-19-crop-sizing-v2.json, offline —
  the gold this ran over lives in the RampNet benchmark and its panoramas are archive-anchored, so
  the JSON is the only thing CI can see.

The frozen v1 replica in the script is pinned against the report's own arithmetic rather than
against CropRunner, which now ships v2. That is deliberate: the whole point of the report is that
the two differ.
"""

import json
import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import CropRunner  # noqa: E402
import crop_sizing_v2 as csv2  # noqa: E402

SUMMARY_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-19-crop-sizing-v2.json')
REPORT_MD = os.path.join(REPO_ROOT, 'reports', '2026-08-19-crop-sizing-v2.md')


@pytest.fixture(scope='module')
def report():
    """The report with every whitespace run collapsed, so a phrase that wraps across a line still
    matches. Without it the fix for a failing prose assertion looks like "reword the report", which is
    how a check on a report's contents quietly becomes a check on its line breaks."""
    with open(REPORT_MD, encoding='utf-8') as f:
        return ' '.join(f.read().split())


@pytest.fixture(scope='module')
def summary():
    with open(SUMMARY_JSON, encoding='utf-8') as f:
        return json.load(f)


class TestTheRuleIsWhatTheReportDescribes:
    """If a constant is retuned, the report's numbers stop describing the shipped rule."""

    def test_constants(self):
        assert CropRunner.CROP_RULE_VERSION == 'v2'
        assert CropRunner.CROP_SIZE_SCALE == 2.5
        assert (CropRunner.CROP_MIN_FOV_DEG, CropRunner.CROP_MAX_FOV_DEG) == (8.0, 90.0)
        assert CropRunner.CROP_ASPECT_W_OVER_H == 1.5
        assert CropRunner.CROP_MAX_STORED_WIDTH == 1440

    def test_the_committed_summary_was_produced_by_these_constants(self, summary):
        assert summary['rule_version'] == CropRunner.CROP_RULE_VERSION
        assert summary['constants'] == {
            'scale': CropRunner.CROP_SIZE_SCALE,
            'min_fov_deg': CropRunner.CROP_MIN_FOV_DEG,
            'max_fov_deg': CropRunner.CROP_MAX_FOV_DEG,
            'aspect_w_over_h': CropRunner.CROP_ASPECT_W_OVER_H,
            'max_stored_width': CropRunner.CROP_MAX_STORED_WIDTH,
        }


class TestFrozenV1:
    """The replica the study compares against."""

    def test_matches_v2_at_the_calibration_height(self):
        """The compatibility claim from the other side: normalisation is the identity at 6656, so
        the two rules differ only by the scale constant there."""
        for y in (1000, 3328, 5000):
            # `==`, not approx: "bit-identical at the calibration height" is the claim the report,
            # the docstring and the PR all make, and normalising by 6656.0/6656.0 is a multiply by
            # exactly 1.0 on both sides. An approx here would pass for a rule that was merely close.
            assert csv2.v1_side(y, 6656) == CropRunner.predict_crop_size(y, 6656)

    def test_differs_away_from_the_calibration_height(self):
        """Discrimination for the test above — otherwise it would pass on a no-op normalisation."""
        assert csv2.v1_side(5000, 8192) != pytest.approx(
            CropRunner.predict_crop_size(5000, 8192))

    def test_there_is_one_frozen_v1_and_the_study_uses_it(self):
        """v1 was transcribed by hand into this study and into both census test modules. Three copies
        of a frozen constant agree until one is edited and nothing fails, which is the arrangement
        `studyfmt` and `region_tag_mask` exist to prevent. One definition now, in crop_rule_v1."""
        import crop_rule_v1
        for y, h in ((1000, 6656), (5000, 8192), (300, 2048)):
            assert csv2.v1_side(y, h) == float(crop_rule_v1.predict_crop_size(y, h))
        assert (csv2.v1_box(8000, 5000, 400, 16384, 8192)[:3]
                == crop_rule_v1.compute_crop_box(8000, 5000, 400, 16384, 8192))

    def test_the_square_window_shares_v2s_seam_and_shift_mechanics(self):
        """Only the SHAPE changed. A window at the seam wraps in both rules, and neither pads."""
        left, top, w, h = csv2.v1_box(0, 4096, 500, 16384, 8192)
        assert w == h == 500
        assert left == (0 - 250) % 16384
        assert 0 <= top <= 8192 - h


class TestStudyLogic:
    """score() on ramps whose answers are known by construction."""

    def _ramp(self, pano_h=6656, depression=10.0, box_w=200.0, box_h=60.0):
        pano_w = pano_h * 2
        y = pano_h / 2 + depression / 180.0 * pano_h
        return {'pano_id': 'p', 'key': 'det:0', 'pano_w': pano_w, 'pano_h': pano_h,
                'x': pano_w / 2, 'y': y, 'box_w': box_w, 'box_h': box_h,
                'box_cx': pano_w / 2, 'box_cy': y, 'depression_deg': depression}

    def test_the_two_write_paths_are_reported_separately(self):
        """CropRunner has never resized, so v1's stored file WAS the window; the 1440 upsample is
        ImageController's, on a path no code here takes. Reporting them under one key made v2 look
        like it removed an upscale this tool was doing, and made v2's own residual upsample vanish
        (it was hard-coded to 1.0 rather than computed)."""
        scored = csv2.score([self._ramp(pano_h=h) for h in (2048, 4000, 6656, 8192)])
        # What the cropper writes: v2's windows are wider, so its files are bigger, up to the cap.
        assert scored['v2']['stored_width_p50'] > scored['v1']['stored_width_p50']
        assert scored['v2']['stored_width_p90'] <= CropRunner.CROP_MAX_STORED_WIDTH
        # What ImageController would do to each: better under v2, but emphatically not eliminated.
        assert scored['v1']['imagecontroller_median_upsample'] > 2.0
        assert 1.0 < scored['v2']['imagecontroller_median_upsample']             < scored['v1']['imagecontroller_median_upsample']

    def test_a_window_at_or_over_the_cap_is_stored_at_the_cap_and_never_upsampled(self):
        near = self._ramp(pano_h=8192, depression=40.0)          # deep near field, 90 deg window
        scored = csv2.score([near])
        assert scored['v2']['stored_width_p50'] == CropRunner.CROP_MAX_STORED_WIDTH
        assert scored['v2']['imagecontroller_median_upsample'] == 1.0

    def test_a_ramp_wider_than_its_window_is_not_contained(self):
        wide = csv2.score([self._ramp(box_w=100000.0)])
        assert wide['v2']['containment'] == 0.0
        assert csv2.score([self._ramp()])['v2']['containment'] == 1.0

    def test_containment_is_positional_and_not_a_size_comparison(self):
        """The defect the first version of this study shipped. The window is centred on the label's
        stored POINT and the box on the apron's extent, so a box the right size can sit wholly
        outside the crop — and a dimensions-only check calls that contained. Here the apron fits
        comfortably by size and is displaced far enough to leave the window entirely."""
        ramp = self._ramp(box_w=200.0, box_h=60.0)
        ramp['box_cx'] = ramp['box_cx'] + 4000.0
        scored = csv2.score([ramp])
        assert scored['v2']['fits_by_size'] == 1.0, 'it would fit, if it were where the window is'
        assert scored['v2']['containment'] == 0.0, 'but it is not inside the crop that was cut'

    def test_containment_reads_x_through_the_seam(self):
        """Discrimination for the above: the fix must not turn every seam-crossing window into a
        miss. Column 0 and column pano_width are the same place in the world, and the window was cut
        through the seam, so the box has to be compared the same way."""
        ramp = self._ramp()
        ramp['x'] = 0.0                       # window straddles the seam
        ramp['box_cx'] = 0.0
        assert csv2.score([ramp])['v2']['containment'] == 1.0

    def test_a_box_pushed_off_the_top_of_the_window_is_not_contained(self):
        """The y arm, which does not wrap: the poles are not adjacent."""
        ramp = self._ramp()
        ramp['box_cy'] = ramp['box_cy'] - 3000.0
        assert csv2.score([ramp])['v2']['containment'] == 0.0

    def test_fill_is_the_ramp_against_the_window_actually_cut(self):
        """Against the integer window from compute_crop_box, not the rule's unrounded ask — the
        crop that exists is the one a viewer judges."""
        ramp = self._ramp(box_w=300.0)
        scored = csv2.score([ramp])
        window = csv2.v2_box(ramp['x'], ramp['y'], ramp['pano_w'], ramp['pano_h'])[2]
        assert isinstance(window, int)
        assert scored['v2']['fill_p50'] == pytest.approx(300.0 / window, rel=1e-9)

    def test_resolution_invariance_is_exact_for_v2(self):
        rows = csv2.resolution_invariance([1664, 2048, 6656, 8192, 16384])
        for row in rows:
            assert row['v2_spread'] == pytest.approx(1.0, abs=1e-9)
            assert row['v1_spread'] > 1.5


class TestCommittedFindings:
    """The report's conclusions, pinned. Offline — the gold itself is not in this repo."""

    def test_corpus_shape(self, summary):
        assert summary['pooled']['n'] == 658
        assert set(summary['cities']) == {'richmond', 'sao_paulo', 'annapolis', 'paterson'}
        assert min(summary['pano_heights']) == 1664
        assert max(summary['pano_heights']) == 8192

    def test_finding_1_v1_window_angle_swings_with_pano_height(self, summary):
        """The defect. v2's window is an angle; v1's is an angle that depends on pixel count."""
        by_depression = {row['depression_deg']: row for row in summary['resolution_invariance']}
        assert by_depression[5.0]['v1_spread'] == pytest.approx(4.09, abs=0.01)
        assert by_depression[10.0]['v1_spread'] == pytest.approx(3.22, abs=0.01)
        assert by_depression[20.0]['v1_spread'] == pytest.approx(1.86, abs=0.01)
        for row in summary['resolution_invariance']:
            assert row['v2_spread'] == pytest.approx(1.0, abs=1e-9)

    def test_finding_1_the_swing_leans_the_wrong_way(self, summary):
        """Bigger panorama, tighter crop — which is why the defect stayed invisible."""
        row = next(r for r in summary['resolution_invariance'] if r['depression_deg'] == 5.0)
        heights, degs = row['pano_heights'], row['v1_window_deg']
        assert degs[heights.index(min(heights))] > degs[heights.index(max(heights))]

    def test_finding_2_v1_crops_are_too_tight(self, summary):
        assert summary['too_tight_fill'] == 0.49
        pooled = summary['pooled']
        assert pooled['v1']['fill_p50'] == pytest.approx(0.84, abs=0.01)
        assert pooled['v1']['frac_clearing_too_tight'] == pytest.approx(0.109, abs=0.002)
        assert pooled['v2']['fill_p50'] == pytest.approx(0.37, abs=0.01)
        assert pooled['v2']['frac_clearing_too_tight'] == pytest.approx(0.748, abs=0.002)

    def test_finding_2_containment(self, summary):
        """Positional containment: is the apron inside the crop that was cut. The size-only reading
        rides along as `fits_by_size`, because the gap between the two IS placement error and it is
        much wider for v1 — which is why the distinction earns a second key rather than a rename."""
        pooled = summary['pooled']
        assert pooled['v1']['containment'] == pytest.approx(0.471, abs=0.002)
        assert pooled['v2']['containment'] == pytest.approx(0.944, abs=0.002)
        assert pooled['v1']['fits_by_size'] == pytest.approx(0.684, abs=0.002)
        assert pooled['v2']['fits_by_size'] == pytest.approx(0.979, abs=0.002)
        for rule in ('v1', 'v2'):
            assert pooled[rule]['containment'] < pooled[rule]['fits_by_size'], rule
        assert (pooled['v1']['fits_by_size'] - pooled['v1']['containment']
                > pooled['v2']['fits_by_size'] - pooled['v2']['containment'])

    def test_finding_2_annapolis_is_the_stated_exception(self, summary):
        """Reported rather than smoothed over: one global constant under-sizes the city whose ramps
        subtend the largest angle, and Annapolis misses the 45% per-city floor."""
        clearing = {city: s['v2']['frac_clearing_too_tight']
                    for city, s in summary['cities'].items()}
        assert clearing['annapolis'] == pytest.approx(0.43, abs=0.01)
        assert clearing['annapolis'] < 0.45
        assert min(c for city, c in clearing.items() if city != 'annapolis') > 0.70

    def test_finding_3_what_the_cropper_writes(self, summary):
        """CropRunner's own write path. It has never resized, so v1's stored file WAS the window."""
        pooled = summary['pooled']
        assert (pooled['v1']['stored_width_p50'], pooled['v2']['stored_width_p50']) == (348, 873)
        assert (pooled['v1']['stored_width_p90'], pooled['v2']['stored_width_p90']) == (807, 1440)
        assert pooled['v2']['stored_width_p90'] == CropRunner.CROP_MAX_STORED_WIDTH

    def test_finding_3_what_the_webpages_write_path_would_do(self, summary):
        """ImageController's unconditional resize to 1440 — modelled, on a path nothing in this repo
        takes today, and REDUCED rather than removed by v2. The first version of this study reported
        both paths under one key and hard-coded v2's upsample to 1.0, which is how v2 came to look
        like it removed an upscale this tool was never doing."""
        pooled = summary['pooled']
        assert pooled['v1']['imagecontroller_median_upsample'] == pytest.approx(4.14, abs=0.01)
        assert pooled['v1']['imagecontroller_frac_upsampled_over_2x'] == pytest.approx(
            0.895, abs=0.002)
        assert pooled['v2']['imagecontroller_median_upsample'] == pytest.approx(1.65, abs=0.01)
        assert pooled['v2']['imagecontroller_frac_upsampled_over_2x'] == pytest.approx(
            0.353, abs=0.002)
        assert pooled['v2']['imagecontroller_median_upsample'] > 1.0, 'reduced, not eliminated'

    def test_every_city_improves_on_every_headline_axis(self, summary):
        for city, scored in summary['cities'].items():
            assert scored['v2']['frac_clearing_too_tight'] > scored['v1']['frac_clearing_too_tight'], city
            assert scored['v2']['containment'] >= scored['v1']['containment'], city
            assert (scored['v2']['imagecontroller_frac_upsampled_over_2x']
                    <= scored['v1']['imagecontroller_frac_upsampled_over_2x']), city

    def test_the_corpus_table_is_in_the_artifact(self, summary):
        """Provider and per-city pano heights used to live only in the report's prose, which made the
        corpus table the one thing in the document nothing could check."""
        cities = summary['cities']
        assert {c: s['provider'] for c, s in cities.items()} == {
            'richmond': 'mapillary', 'annapolis': 'mapillary',
            'sao_paulo': 'gsv', 'paterson': 'gsv'}
        assert cities['annapolis']['pano_heights'] == [4000]
        assert min(cities['paterson']['pano_heights']) == 1664
        assert max(cities['paterson']['pano_heights']) == 8192
        assert sum(s['n'] for s in cities.values()) == summary['pooled']['n'] == 658

    def test_annapolis_ramps_really_are_the_widest(self, summary):
        """The stated reason one global constant under-sizes that city, as a number rather than an
        assertion — it is the load-bearing half of calling Annapolis a reported exception."""
        widths = {c: s['ramp_width_deg_p50'] for c, s in summary['cities'].items()}
        assert widths['annapolis'] == max(widths.values())
        assert widths['annapolis'] == pytest.approx(14.93, abs=0.01)
        assert widths['sao_paulo'] == pytest.approx(9.67, abs=0.01)


class TestReportMatchesTheArtifact:
    """Every number in the report's prose, transcribed from the committed summary.

    The convention this file was missing, and its absence was not hypothetical: the report claimed
    "a 2048-px pano gets a 23.0 degree window and a 16384-px pano gets 4.6", where 16384 is a pano
    WIDTH in this corpus, no panorama in it is 16384 px high, and the quoted pair implies a 5.05x
    spread one line under a table stating 4.09x. A report table is the one place in this repo where
    a plausible number has no compiler and no test.
    """

    def _has(self, report, value, spec='.2f'):
        return format(value, spec) in report

    def test_the_corpus_table(self, summary, report):
        for city, scored in summary['cities'].items():
            assert str(scored['n']) in report, city
            assert self._has(report, scored['ramp_width_deg_p50']), city
            for height in (min(scored['pano_heights']), max(scored['pano_heights'])):
                assert str(int(height)) in report, (city, height)

    def test_the_resolution_invariance_table(self, summary, report):
        for row in summary['resolution_invariance']:
            assert self._has(report, row['v1_spread']), row['depression_deg']

    def test_the_illustrative_pair_comes_from_the_five_degree_row(self, summary, report):
        """The specific sentence that was wrong. Its two windows must be the endpoints of the spread
        quoted beside them, at heights the corpus actually contains."""
        row = next(r for r in summary['resolution_invariance'] if r['depression_deg'] == 5.0)
        heights, degs = row['pano_heights'], row['v1_window_deg']
        small = degs[heights.index(min(heights))]
        large = degs[heights.index(max(heights))]
        assert self._has(report, small) and self._has(report, large)
        assert str(int(min(heights))) in report and str(int(max(heights))) in report
        assert max(degs) / min(degs) == pytest.approx(row['v1_spread'])
        assert not re.search(r'16384[- ]px (pano|panorama)', report)

    def test_the_finding_2_table(self, summary, report):
        pooled = summary['pooled']
        for rule in ('v1', 'v2'):
            assert self._has(report, pooled[rule]['fill_p50']), rule
            assert self._has(report, pooled[rule]['containment'], '.3f'), rule
            assert self._has(report, 100 * pooled[rule]['frac_clearing_too_tight'], '.1f'), rule
            assert self._has(report, pooled[rule]['window_deg_p50'], '.1f'), rule
            assert self._has(report, pooled[rule]['fits_by_size'], '.3f'), rule

    def test_the_finding_3_figures(self, summary, report):
        pooled = summary['pooled']
        for rule in ('v1', 'v2'):
            assert str(pooled[rule]['stored_width_p50']) in report, rule
            assert str(pooled[rule]['stored_width_p90']) in report, rule
            assert self._has(report, pooled[rule]['imagecontroller_median_upsample']), rule
            assert self._has(
                report, 100 * pooled[rule]['imagecontroller_frac_upsampled_over_2x'], '.1f'), rule
        for city in ('annapolis', 'richmond', 'paterson', 'sao_paulo'):
            share = summary['cities'][city]['v1']['imagecontroller_frac_upsampled_over_2x']
            assert self._has(report, 100 * share, '.1f'), city

    def test_the_figure_caption(self, summary, report):
        """The rows the caption argues from, by their own numbers rather than by row index alone."""
        examples = summary['figure_examples']
        assert len(examples) == 8
        blur = next(e for e in examples
                    if e['city'] == 'sao_paulo' and e['depression_deg'] < 10)
        assert str(blur['v1_window_px']) in report
        assert '%dx%d' % tuple(blur['v2_window_px']) in report.replace('\u00d7', 'x')
        assert self._has(report, blur['v1_window_deg'], '.1f')
        assert self._has(report, blur['v2_fill'])
        overflow = max(examples, key=lambda e: e['v1_fill'])
        assert self._has(report, overflow['v1_fill'])
        widest = max(examples, key=lambda e: e['v2_window_deg'])
        assert self._has(report, widest['v2_window_deg'], '.0f')
