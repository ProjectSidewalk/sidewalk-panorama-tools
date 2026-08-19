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
            assert csv2.v1_side(y, 6656) == pytest.approx(
                CropRunner.predict_crop_size(y, 6656))

    def test_differs_away_from_the_calibration_height(self):
        """Discrimination for the test above — otherwise it would pass on a no-op normalisation."""
        assert csv2.v1_side(5000, 8192) != pytest.approx(
            CropRunner.predict_crop_size(5000, 8192))

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

    def test_v2_never_upsamples_and_v1_does(self):
        scored = csv2.score([self._ramp(pano_h=h) for h in (2048, 4000, 6656, 8192)])
        assert scored['v2']['median_upsample'] == 1.0
        assert scored['v2']['frac_upsampled_over_2x'] == 0.0
        assert scored['v1']['median_upsample'] > 2.0

    def test_a_ramp_wider_than_its_window_is_not_contained(self):
        wide = csv2.score([self._ramp(box_w=100000.0)])
        assert wide['v2']['containment'] == 0.0
        assert csv2.score([self._ramp()])['v2']['containment'] == 1.0

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
        assert summary['pooled']['v1']['containment'] == pytest.approx(0.684, abs=0.002)
        assert summary['pooled']['v2']['containment'] == pytest.approx(0.979, abs=0.002)

    def test_finding_2_annapolis_is_the_stated_exception(self, summary):
        """Reported rather than smoothed over: one global constant under-sizes the city whose ramps
        subtend the largest angle, and Annapolis misses the 45% per-city floor."""
        clearing = {city: s['v2']['frac_clearing_too_tight']
                    for city, s in summary['cities'].items()}
        assert clearing['annapolis'] == pytest.approx(0.43, abs=0.01)
        assert clearing['annapolis'] < 0.45
        assert min(c for city, c in clearing.items() if city != 'annapolis') > 0.70

    def test_finding_3_the_stored_file_was_mostly_invented_pixels(self, summary):
        pooled = summary['pooled']
        assert pooled['v1']['median_upsample'] == pytest.approx(4.14, abs=0.01)
        assert pooled['v1']['frac_upsampled_over_2x'] == pytest.approx(0.90, abs=0.01)
        assert pooled['v2']['median_upsample'] == 1.0
        assert pooled['v2']['frac_upsampled_over_2x'] == 0.0

    def test_every_city_improves_on_every_headline_axis(self, summary):
        for city, scored in summary['cities'].items():
            assert scored['v2']['frac_clearing_too_tight'] > scored['v1']['frac_clearing_too_tight'], city
            assert scored['v2']['containment'] >= scored['v1']['containment'], city
            assert scored['v2']['frac_upsampled_over_2x'] <= scored['v1']['frac_upsampled_over_2x'], city
