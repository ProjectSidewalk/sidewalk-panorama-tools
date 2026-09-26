"""Tests for reports/scripts/tilt_geometry.py — the #54 tilt study's one definition of every frame.

Every convention the study rests on is pinned here against something *outside* the module: the
depth artifact's ray formula as docs/depth.md and downloaders/gsv.py state it, the stored-pixel
projection pov_replay reproduces bit-for-bit, and the four operational sign pins. The committed
xml<->npz overlap table pins the one fitted conversion.
"""

import gzip
import csv
import os
import sys

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import tilt_geometry as tg  # noqa: E402
import pov_replay  # noqa: E402

OVERLAP_CSV = os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-26-tilt-xml-npz-overlap.csv.gz')


class TestWrap:
    def test_photometa_roll_wraps(self):
        """photometa serves roll in [0, 360); the 2026-08-09 census's |roll| p90 of 359.6 deg was
        exactly this wrap missing."""
        assert tg.wrap_deg(359.6) == pytest.approx(-0.4)
        assert tg.wrap_deg(180.0) == pytest.approx(180.0)
        assert tg.wrap_deg(-180.0) == pytest.approx(180.0)
        assert tg.wrap_deg(0.5) == pytest.approx(0.5)
        np.testing.assert_allclose(tg.wrap_deg(np.array([540.0, -359.0])), [180.0, 1.0])


class TestDirections:
    def test_round_trip(self):
        rng = np.random.default_rng(1)
        b = rng.uniform(-179.9, 180, 500)
        el = rng.uniform(-89, 89, 500)
        b2, el2 = tg.bearing_elevation(tg.direction_rfu(b, el))
        np.testing.assert_allclose(b2, b, atol=1e-9)
        np.testing.assert_allclose(el2, el, atol=1e-9)

    def test_forward_is_plus_y_right_is_plus_x(self):
        np.testing.assert_allclose(tg.direction_rfu(0, 0), [0, 1, 0], atol=1e-12)
        np.testing.assert_allclose(tg.direction_rfu(90, 0), [1, 0, 0], atol=1e-12)
        np.testing.assert_allclose(tg.direction_rfu(0, 90), [0, 0, 1], atol=1e-12)


class TestArtifactFrame:
    def test_artifact_axes_reproduce_the_documented_ray_formula(self):
        """docs/depth.md / gsv._write_depth_artifact define the plane frame by
        v(r, c) = (sin t cos f, sin t sin f, cos t), t = (h-r-0.5)/h*pi, f = (w-c-0.5)/w*2pi + pi/2.
        Mapping that ray through artifact_normal_bearing_elevation must land on the pixel-centre
        inverse of the stored-pixel projection, at every column and row. A sign slip on x or y fails
        at every column; on z at every row."""
        w, h = 512, 256
        r, c = np.meshgrid(np.arange(0, h, 7), np.arange(w), indexing='ij')
        theta = (h - r - 0.5) / h * np.pi
        phi = (w - c - 0.5) / w * 2 * np.pi + np.pi / 2
        v = np.stack([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)], axis=-1)
        b, el = tg.artifact_normal_bearing_elevation(v)
        b_exp, el_exp = tg.bearing_elevation_from_pixel(c + 0.5, r + 0.5, w, h)
        np.testing.assert_allclose(tg.wrap_deg(b - b_exp), 0, atol=1e-9)
        np.testing.assert_allclose(el, el_exp, atol=1e-9)

    def test_zero_norm_is_nan(self):
        b, el = tg.artifact_normal_bearing_elevation(np.zeros(3))
        assert np.isnan(b) and np.isnan(el)


class TestPose:
    @pytest.mark.parametrize('b, p, r, expected', [
        (0, 3.0, 0, (0, 3.0)),
        (180, 3.0, 0, (180, -3.0)),
        (-90, 0, 2.0, (-90, -2.0)),
        (90, 0, 2.0, (90, 2.0)),
    ])
    def test_sign_pins(self, b, p, r, expected):
        br, er = tg.gravity_to_rig(b, 0.0, p, r)
        assert tg.wrap_deg(br - expected[0]) == pytest.approx(0, abs=1e-9)
        assert er == pytest.approx(expected[1], abs=1e-9)

    def test_small_angle_agreement(self):
        rng = np.random.default_rng(2)
        b = rng.uniform(-180, 180, 2000)
        el = rng.uniform(-45, 45, 2000)
        p = rng.uniform(-3, 3, 2000)
        r = rng.uniform(-3, 3, 2000)
        _, er = tg.gravity_to_rig(b, el, p, r)
        # first order: el_rig = el + T(b) (measured sign); the exact rotation departs by O(tilt^2 tan el)
        assert np.max(np.abs(er - (el + tg.tilt_term_deg(b, p, r)))) < 0.2
        _, er0 = tg.gravity_to_rig(b, 0 * el, p, r)
        assert np.max(np.abs(er0 - tg.tilt_term_deg(b, p, r))) < 0.02

    def test_order_is_pitch_then_roll(self):
        """At (10, 10) the two orders differ measurably; pin which one the module uses."""
        R = tg.rig_from_gravity(10.0, 10.0)
        p, r = np.radians(10.0), np.radians(10.0)
        rx = np.array([[1, 0, 0], [0, np.cos(-p), -np.sin(-p)], [0, np.sin(-p), np.cos(-p)]])
        ry = np.array([[np.cos(r), 0, np.sin(r)], [0, 1, 0], [-np.sin(r), 0, np.cos(r)]])
        np.testing.assert_allclose(R, (rx @ ry).T, atol=1e-12)
        assert np.max(np.abs(R - (ry @ rx).T)) > 0.01

    def test_inverse(self):
        rng = np.random.default_rng(3)
        b = rng.uniform(-180, 180, 300)
        el = rng.uniform(-80, 80, 300)
        p = rng.uniform(-8, 8, 300)
        r = rng.uniform(-8, 8, 300)
        b2, el2 = tg.rig_to_gravity(*tg.gravity_to_rig(b, el, p, r), p, r)
        np.testing.assert_allclose(tg.wrap_deg(b2 - b), 0, atol=1e-9)
        np.testing.assert_allclose(el2, el, atol=1e-9)

    def test_rotation_is_orthonormal(self):
        R = tg.rig_from_gravity(4.0, -2.5)
        np.testing.assert_allclose(R @ R.T, np.eye(3), atol=1e-12)
        assert np.linalg.det(R) == pytest.approx(1.0)


class TestPixels:
    def test_matches_pov_replay_at_zero_tilt(self):
        rng = np.random.default_rng(4)
        n = 500
        w, h = 16384, 8192
        cam = rng.uniform(0, 360, n)
        heading = rng.uniform(0, 360, n)
        pitch = rng.uniform(-60, 30, n)
        px, py = pov_replay.pano_xy_from_pov(heading, pitch, cam, w, h)
        x, y = tg.pixel_from_bearing_elevation(tg.wrap_deg(heading - cam), pitch, w, h)
        dx = (x - px + w / 2) % w - w / 2
        assert np.max(np.abs(dx)) <= 0.5 + 1e-6
        assert np.max(np.abs(y - py)) <= 0.5 + 1e-6

    def test_pixel_round_trip(self):
        x = np.array([0.0, 100.5, 8192.0, 16000.25])
        y = np.array([10.0, 4096.0, 6000.0, 8000.0])
        b, el = tg.bearing_elevation_from_pixel(x, y, 16384, 8192)
        x2, y2 = tg.pixel_from_bearing_elevation(b, el, 16384, 8192)
        np.testing.assert_allclose(x2, x, atol=1e-6)
        np.testing.assert_allclose(y2, y, atol=1e-6)

    def test_rig_pixel_moves_by_T_over_180(self):
        w, h = 16384, 8192
        x = np.array([8192.0, 4096.0, 12288.0, 0.0, 10000.0])
        y = np.full(5, h / 2)
        p, r = 2.0, -1.5
        xr, yr = tg.rig_pixel_from_gravity_pixel(x, y, w, h, p, r)
        b, _ = tg.bearing_elevation_from_pixel(x, y, w, h)
        np.testing.assert_allclose(yr - y, -tg.tilt_term_deg(b, p, r) * h / 180, atol=0.5)
        xg, yg = tg.gravity_pixel_from_rig_pixel(xr, yr, w, h, p, r)
        np.testing.assert_allclose(yg, y, atol=1e-6)


class TestVerticalLean:
    def test_is_the_bearing_derivative_of_the_horizon(self):
        p, r = 3.0, -2.0
        b = np.linspace(-170, 170, 35)
        db = 1e-4
        _, e1 = tg.gravity_to_rig(b + db, 0 * b, p, r)
        _, e0 = tg.gravity_to_rig(b - db, 0 * b, p, r)
        slope_per_rad = (e1 - e0) / np.radians(2 * db)
        np.testing.assert_allclose(slope_per_rad, tg.vertical_lean_deg(b, p, r), atol=0.02)


class TestXmlConversion:
    def test_synthetic_round_trip(self):
        rng = np.random.default_rng(5)
        p = rng.uniform(-6, 6, 200)
        r = rng.uniform(-6, 6, 200)
        yaw = rng.uniform(0, 360, 200)
        triple = tg.pitch_roll_to_xml_tilt(yaw, p, r)
        p2, r2 = tg.xml_tilt_to_pitch_roll(*triple)
        np.testing.assert_allclose(p2, p, atol=1e-9)
        np.testing.assert_allclose(r2, r, atol=1e-9)

    def test_planning_example(self):
        """_03OWjj8xH_WkWLSZHZ0mg.xml: pano_yaw 269.22998, tilt_yaw 110.909996, tilt_pitch 4.73."""
        p, r = tg.xml_tilt_to_pitch_roll(269.22998, 110.909996, 4.73)
        assert np.hypot(p, r) == pytest.approx(4.73)

    @pytest.mark.skipif(not os.path.exists(OVERLAP_CSV), reason='overlap table not committed yet')
    def test_matches_the_committed_overlap(self):
        with gzip.open(OVERLAP_CSV, 'rt', newline='') as f:
            rows = list(csv.DictReader(f))
        assert len(rows) >= 100
        yaw = np.array([float(r['xml_pano_yaw_deg']) for r in rows])
        tyaw = np.array([float(r['xml_tilt_yaw_deg']) for r in rows])
        tp = np.array([float(r['xml_tilt_pitch_deg']) for r in rows])
        p, r = tg.xml_tilt_to_pitch_roll(yaw, tyaw, tp)
        dp = np.abs(p - np.array([float(x['pitch_deg']) for x in rows]))
        dr = np.abs(r - np.array([float(x['roll_deg']) for x in rows]))
        assert np.median(dp) <= 0.3
        assert np.median(dr) <= 0.3
