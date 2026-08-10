"""Pins for reports/scripts/pov_replay.py — the front-end replay math ported from label-latlng-estimation.

The sibling repo carries the deep fidelity evidence (pano_y replays exactly for every row with a
replay target; post-2021 pano_x exactly in all six non-DC cities — label-latlng-estimation
reports/2026-08-06-pov-inversion.md). These tests pin the PORT: the constants, the sign conventions,
and the shipped distance calibration, so a transcription slip here fails CI rather than silently
skewing every study number built on it.
"""

import os
import sys

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import pov_replay  # noqa: E402


class TestViewerFov:

    def test_the_get3dfov_values(self):
        """UtilitiesPanomarker's per-zoom FOV: the two branches meet the JS's constants exactly."""
        assert pov_replay.get_3d_fov(1) == pytest.approx(89.75)
        assert pov_replay.get_3d_fov(2) == pytest.approx(53.0)
        assert pov_replay.get_3d_fov(3) == pytest.approx(27.68198649088542)


class TestPovInversion:

    def test_a_canvas_centre_click_is_the_view_pov(self):
        """du = dv = 0 must return (heading, pitch) untouched — the projection's fixed point."""
        pov_heading, pov_pitch = pov_replay.pov_if_centered(360, 240, 123.4, -10.5, 1)
        assert float(pov_heading) == pytest.approx(123.4)
        assert float(pov_pitch) == pytest.approx(-10.5)

    def test_click_right_of_centre_moves_the_heading_clockwise(self):
        """Sign convention: canvas x grows rightward, heading grows clockwise from north."""
        pov_heading, _ = pov_replay.pov_if_centered(500, 240, 0.0, 0.0, 1)
        assert float(pov_heading) > 0

    def test_click_below_centre_is_a_depression(self):
        """Canvas y grows downward; depression is positive down."""
        dep = pov_replay.exact_depression_deg(360, 400, 0.0, 0.0, 1)
        assert float(dep) > 0

    def test_pano_mapping_round_trip(self):
        """The full replay of a centre click at heading 123.4, pitch -10.5, camera_heading 100 on an
        8192x4096 pano — values verified against the module at port time; a heading-centred raster
        puts the camera_heading at the centre column."""
        pov_heading, pov_pitch = pov_replay.pov_if_centered(360, 240, 123.4, -10.5, 1)
        pano_x, pano_y = pov_replay.pano_xy_from_pov(pov_heading, pov_pitch, 100.0, 8192, 4096)
        assert (int(pano_x), int(pano_y)) == (4628, 2287)
        # 23.4 deg clockwise of the centre column: 4096 + round(8192 * 23.4 / 360) = 4628.
        assert int(pano_x) == 4096 + round(8192 * 23.4 / 360)

    def test_depression_from_pano_y_inverts_the_y_mapping(self):
        """pano_y -> depression is the exact inverse of the linear elevation mapping, up to the
        round() the front end applies on write (half a pixel = 180/height/2 degrees)."""
        dep = pov_replay.depression_from_pano_y(2287, 4096)
        assert float(dep) == pytest.approx(10.5, abs=180.0 / 4096 / 2)


class TestBlendDistance:
    """The shipped calibration from label-latlng-estimation #3 Stage 4 (final_coefficients)."""

    def test_the_flat_camera_height_is_the_shipped_one(self):
        assert pov_replay.BLEND_CAMERA_HEIGHT_M == pytest.approx(2.341219672825709, abs=1e-12)

    def test_cotangent_regime_at_the_blend_point(self):
        """At exactly 11.25 deg the cotangent and the tail agree (C1 continuity): h / tan(11.25)."""
        assert float(pov_replay.predict_blend_distance(11.25)) == pytest.approx(11.770106120938644)

    def test_cotangent_regime_at_45_degrees(self):
        """h / tan(45) == h."""
        assert float(pov_replay.predict_blend_distance(45.0)) == pytest.approx(2.3412196728257095)

    def test_the_horizon_is_bounded_not_divergent(self):
        """The structural maximum equals the sibling repo's documented max_answer_m to the digit —
        the whole point of the blend over a raw cotangent."""
        assert float(pov_replay.predict_blend_distance(0.0)) == pytest.approx(23.848261259830384)

    def test_above_the_horizon_answers_the_horizon(self):
        assert float(pov_replay.predict_blend_distance(-5.0)) == float(pov_replay.predict_blend_distance(0.0))

    def test_monotone_decreasing_through_the_blend(self):
        deps = np.array([0.5, 2.0, 5.0, 11.24, 11.26, 20.0, 60.0])
        dists = pov_replay.predict_blend_distance(deps)
        assert np.all(np.diff(dists) < 0)
