"""sidewalk_panorama_tools.ps_math: the front end's click and POV math, once (#222 step 6, docs/geometry.md).

The functions moved here verbatim from reports/scripts/pov_replay.py, which re-exports them (tests/test_pov_replay.py
keeps pinning them under those names and asserts they are this module's objects). This file pins the package module itself, the new inverse, and a
cross-implementation check against a line-by-line transcription of SidewalkWebpage's UtilitiesPanomarker.js at
tag v7.19.10 (public/javascripts/common/UtilitiesPanomarker.js, read with `gh api` on 2026-11-05).
"""

import math

import numpy as np
import pytest

from sidewalk_panorama_tools import ps_math as m


# ------------------------------------------------------------------ UtilitiesPanomarker.js @ v7.19.10, transcribed

def js_get3d_fov(zoom):
    return 126.5 - zoom * 36.75 if zoom <= 2 else 195.93 / math.pow(1.92, zoom)


def js_calculate_pov_if_centered(pov, canvas_x, canvas_y, canvas_width, canvas_height):
    """calculatePovIfCentered, statement for statement (scalar Math.*, not the port's vectorised form)."""
    fov = js_get3d_fov(pov['zoom']) * math.pi / 180.0
    h0 = pov['heading'] * math.pi / 180.0
    p0 = pov['pitch'] * math.pi / 180.0
    f = 0.5 * canvas_width / math.tan(0.5 * fov)
    x0, y0, z0 = f * math.cos(p0) * math.sin(h0), f * math.cos(p0) * math.cos(h0), f * math.sin(p0)
    du = canvas_x - canvas_width / 2
    dv = canvas_height / 2 - canvas_y
    sgn = 1 if math.cos(p0) >= 0 else -1
    ux, uy, uz = sgn * math.cos(h0), -sgn * math.sin(h0), 0
    vx, vy, vz = -math.sin(p0) * math.sin(h0), -math.sin(p0) * math.cos(h0), math.cos(p0)
    x, y, z = x0 + du * ux + dv * vx, y0 + du * uy + dv * vy, z0 + du * uz + dv * vz
    r = math.sqrt(x * x + y * y + z * z)
    return math.atan2(x, y) * 180.0 / math.pi, math.asin(z / r) * 180.0 / math.pi


def js_calculate_pano_xy_from_pov(pov, camera_heading, pano_width, pano_height):
    """calculatePanoXYFromPov as the v7.19.10 client runs it: unrounded (evolution 179 rounds server-side)."""
    pano_y = pano_height / 2 - (pano_height / 2) * (pov['pitch'] / 90)
    heading_pixel_zero = camera_heading - 180
    heading = (pov['heading'] + 360) % 360
    heading_pixel_zero = (heading_pixel_zero + 360) % 360
    pano_x = pano_width * (heading - heading_pixel_zero) / 360
    if pano_x < 0:
        pano_x += pano_width
    return pano_x, pano_y


class TestAgainstTheFrontEnd:

    @pytest.mark.parametrize('zoom, expected', [(1, 89.75), (2, 53.0), (3, 27.68198649088542)])
    def test_get_3d_fov_at_the_three_zooms(self, zoom, expected):
        """89.75 and 53 are the JS's linear branch; 27.68 its "determined experimentally" one."""
        assert m.get_3d_fov(zoom) == pytest.approx(expected) == js_get3d_fov(zoom)

    def test_the_right_canvas_edge_is_half_the_fov(self):
        """An analytic case of the JS: level view, zoom 1, click on the right edge of a 720x480 canvas."""
        heading, pitch = m.pov_if_centered(720, 240, 0.0, 0.0, 1)
        assert heading == pytest.approx(89.75 / 2) and pitch == pytest.approx(0.0, abs=1e-12)

    def test_a_concrete_pano_xy_case(self):
        """pov (90, -30) on a 16384x8192 pano whose camera faces north: the JS gives x 12288 and y 5461.33;
        the port rounds y to 5461 (evolution 179's round-then-wrap)."""
        assert js_calculate_pano_xy_from_pov({'heading': 90, 'pitch': -30}, 0, 16384, 8192) == \
            pytest.approx((12288, 5461.333333333333))
        assert tuple(int(v) for v in m.pano_xy_from_pov(90, -30, 0, 16384, 8192)) == (12288, 5461)

    @pytest.mark.parametrize('heading', [-170.0, -45.5, 0.0, 33.3, 179.0])
    @pytest.mark.parametrize('pitch', [-35.0, -5.0, 0.0, 12.0])
    @pytest.mark.parametrize('zoom', [1, 2, 3])
    def test_pov_if_centered_is_the_js_on_a_grid(self, heading, pitch, zoom):
        for canvas_x, canvas_y in [(0, 0), (451, 142), (360, 240), (719, 479), (100, 400)]:
            js = js_calculate_pov_if_centered({'heading': heading, 'pitch': pitch, 'zoom': zoom},
                                              canvas_x, canvas_y, 720, 480)
            port = m.pov_if_centered(canvas_x, canvas_y, heading, pitch, zoom)
            assert float(port[0]) == pytest.approx(js[0], abs=1e-9)
            assert float(port[1]) == pytest.approx(js[1], abs=1e-9)

    @pytest.mark.parametrize('camera_heading', [-179.5, -20.0, 0.0, 95.25, 180.0])
    def test_pano_xy_is_the_js_rounded(self, camera_heading):
        """The port is the JS plus rounding: within half a pixel of it, modulo the width."""
        width, height = 16384, 8192
        for pov_heading in np.linspace(-179.9, 180.0, 37):
            for pov_pitch in (-60.0, -12.5, 0.0, 30.0):
                jx, jy = js_calculate_pano_xy_from_pov({'heading': pov_heading, 'pitch': pov_pitch},
                                                       camera_heading, width, height)
                px, py = m.pano_xy_from_pov(pov_heading, pov_pitch, camera_heading, width, height)
                dx = (float(px) - jx + width / 2) % width - width / 2
                assert abs(dx) <= 0.5 + 1e-9 and abs(float(py) - jy) <= 0.5 + 1e-9


class TestTheForwardMap:

    def test_pov_if_centered_at_the_canvas_centre_is_the_camera(self):
        for heading, pitch in [(123.4, -10.5), (-90.0, 20.0), (0.0, 0.0)]:
            h, p = m.pov_if_centered(m.CANVAS_W / 2, m.CANVAS_H / 2, heading, pitch, 2)
            assert float(h) == pytest.approx(heading) and float(p) == pytest.approx(pitch)

    def test_the_camera_heading_lands_on_the_centre_column_and_pitch_zero_on_the_centre_row(self):
        for camera_heading in (-135.0, 0.0, 47.5, 180.0):
            x, y = m.pano_xy_from_pov(camera_heading, 0.0, camera_heading, 16384, 8192)
            assert (int(x), int(y)) == (8192, 4096)

    def test_depression_is_minus_pitch(self):
        _, pitch = m.pov_if_centered(360, 400, 0.0, 0.0, 1)
        assert float(m.exact_depression_deg(360, 400, 0.0, 0.0, 1)) == pytest.approx(-float(pitch))
        assert float(m.depression_from_pano_y(4096 + 1024, 8192)) == pytest.approx(22.5)


class TestTheInverse:

    WIDTH, HEIGHT = 16384, 8192

    def test_the_centre_pixel_looks_along_the_camera(self):
        h, p = m.pov_from_pano_xy(8192, 4096, 100.0, self.WIDTH, self.HEIGHT)
        assert (float(h), float(p)) == (100.0, 0.0)

    def test_heading_is_in_the_half_open_interval(self):
        h, _ = m.pov_from_pano_xy(np.arange(0, self.WIDTH, 512), 4096, 180.0, self.WIDTH, self.HEIGHT)
        assert np.all(h > -180.0) and np.all(h <= 180.0)
        h0, _ = m.pov_from_pano_xy(0, 4096, 0.0, self.WIDTH, self.HEIGHT)
        assert float(h0) == 180.0, 'column zero of a north-facing pano looks south, written +180, never -180'

    @pytest.mark.parametrize('camera_heading', [-179.0, -33.3, 0.0, 90.0, 180.0])
    def test_pixel_to_pov_to_pixel_is_exact(self, camera_heading):
        xs = np.array([0, 1, 2, 8191, 8192, 12000, self.WIDTH - 2, self.WIDTH - 1])
        ys = np.array([0, 1, 1000, 4095, 4096, 4097, 7000, self.HEIGHT - 1])
        gx, gy = np.meshgrid(xs, ys)
        h, p = m.pov_from_pano_xy(gx, gy, camera_heading, self.WIDTH, self.HEIGHT)
        x, y = m.pano_xy_from_pov(h, p, camera_heading, self.WIDTH, self.HEIGHT)
        assert np.array_equal(x, gx % self.WIDTH) and np.array_equal(y, gy)

    @pytest.mark.parametrize('camera_heading', [-179.0, 0.0, 77.7, 180.0])
    def test_pov_to_pixel_to_pov_is_within_one_pixel_including_the_seam_and_both_hemispheres(self, camera_heading):
        seam = camera_heading - 180.0                      # the bearing of column zero
        headings = np.concatenate([np.linspace(-179.99, 180.0, 61),
                                   seam + np.array([-0.01, -0.005, 0.0, 0.005, 0.01])])
        headings = 180.0 - np.mod(180.0 - headings, 360.0)
        pitches = np.array([-89.0, -45.0, -0.01, 0.0, 0.01, 30.0, 89.0])
        gh, gp = np.meshgrid(headings, pitches)
        x, y = m.pano_xy_from_pov(gh, gp, camera_heading, self.WIDTH, self.HEIGHT)
        h, p = m.pov_from_pano_xy(x, y, camera_heading, self.WIDTH, self.HEIGHT)
        per_px = 360.0 / self.WIDTH
        dh = (h - gh + 180.0) % 360.0 - 180.0
        assert np.max(np.abs(dh)) <= per_px + 1e-9
        assert np.max(np.abs(p - gp)) <= 180.0 / self.HEIGHT + 1e-9
        assert set(np.unique(x)) & {0, self.WIDTH - 1}, 'the grid must actually reach both seam columns'


class TestTheDistance:

    def test_monotone_non_increasing_in_depression_and_capped(self):
        deps = np.linspace(-10.0, 89.0, 400)
        d = m.predict_blend_distance(deps)
        assert np.all(np.diff(d) <= 1e-12)
        assert np.all(d <= m.DIST_CAP_M) and np.all(d >= 0.0)
        assert float(m.predict_blend_distance(0.0)) == pytest.approx(23.848261259830384)

    def test_the_cap_binds_with_a_smaller_cap(self):
        assert float(m.predict_blend_distance(0.0, cap_m=10.0)) == 10.0

