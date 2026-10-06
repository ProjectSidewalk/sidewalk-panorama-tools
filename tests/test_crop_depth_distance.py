"""Tests for sizing rule v3-depth (#180): v3 with its distance read from the pano's .depth.npz.

`--sizing-rule v3-depth` is v3 (`geometric_window_fov_deg`, the 3:2 window, the clamps, the storage cap) with
one step replaced: the distance comes from the depth artifact, sampled at the label's RIG pixel, and falls
back to v3's own blend distance whenever the artifact cannot be trusted there. Every fallback is counted by
reason, and the counts reconcile with the crops cut.

Every artifact here is written by the real writer, `gsv._write_depth_artifact`, from a scene of planes laid
out in the artifact's own frame (x left, y back, z down: docs/depth.md, pano_pose's module docstring). The
raster is derived from those planes by `gsv._compute_depth_raster`, the production derivation, so a test
that passes here passes on the format the depth phase actually writes. A ground plane at camera height h is
the known geometry: the distance at depression d must be h / tan(d).

The rig pixel is the #54 point (#158, #193): a GSV label's stored pano_x/pano_y is gravity-levelled, while
the depth planes are in the rig frame of the npz's own pose. So the lookup goes through
pano_pose.corrected_pixel; on a tilted pano a lookup at the stored pixel lands rows away, which the tilted
known-geometry tests are built to catch.
"""

import json
import logging
import math
import os

import numpy as np
import pytest

import pano_pose
from conftest import make_pano
from downloaders import gsv
# crop_runner and the autouse logging isolation are fixtures: importing them into this module's namespace
# is what makes pytest apply them here.
from test_crop_runner import (  # noqa: F401
    _isolate_logging_state, crop_path, crop_runner, label_row, put_pano, reconciles)

CITY = 'seattle-wa'
W, H = 2048, 1024           # the pano (test_crop_runner's PANO_SIZE)
GH, GW = 256, 512           # the depth raster: the production size
CAMERA_H = 2.5              # deliberately not V3_CAMERA_HEIGHT_M, so depth and blend disagree


# ---------------------------------------------------------------------------
# Scenes, written by the real writer
# ---------------------------------------------------------------------------

def artifact_rays(gh, gw):
    """Unit ray of every depth cell, in the artifact frame (= -RFU), through pano_pose's pixel convention
    (column c's centre at c + 0.5). tests/test_tilt_geometry.py pins that convention to the writer's ray
    formula; the known-geometry tests below would fail if the two disagreed, since the raster itself is
    computed by gsv._compute_depth_raster from its own rays."""
    bearing = (np.arange(gw) + 0.5) / gw * 360.0 - 180.0
    elevation = 90.0 - (np.arange(gh) + 0.5) / gh * 180.0
    b, el = np.meshgrid(bearing, elevation)
    return -pano_pose.direction_rfu(b, el)


def ground(height=CAMERA_H, pitch=0.0, roll=0.0, mask=None):
    """A gravity-level ground plane `height` below the camera, for a rig at (pitch, roll): normal
    R @ up (gravity's up, in rig RFU), offset +height, in the artifact frame. Points p_art with
    p_art . n = height are p_grav . up = -height (art = -RFU)."""
    up = pano_pose.rig_from_gravity(pitch, roll) @ np.array([0.0, 0.0, 1.0])
    return up, float(height), mask


def facade(distance, pitch=0.0, roll=0.0, mask=None):
    """A gravity-vertical wall `distance` ahead of the camera, facing it."""
    forward = pano_pose.rig_from_gravity(pitch, roll) @ np.array([0.0, 1.0, 0.0])
    return forward, -float(distance), mask


def write_scene(storage, pano_id, planes, pitch=0.0, roll=0.0, shape=(GH, GW)):
    """Write <pano_id[:2]>/<pano_id>.depth.npz for a scene of planes, through gsv._write_depth_artifact.

    Each pixel takes the nearest plane its ray hits in front of the camera (t > 0), restricted to the
    plane's mask when it has one; no hit is index 0, which the writer stores as -1. `pitch`/`roll` are
    degrees (None for a pano Google sent no pose for), stored as the writer stores them: radians."""
    gh, gw = shape
    rays = artifact_rays(gh, gw)
    best = np.full((gh, gw), np.inf)
    indices = np.zeros((gh, gw), dtype=np.uint8)
    for i, (normal, offset, mask) in enumerate(planes, start=1):
        with np.errstate(divide='ignore', invalid='ignore'):
            t = offset / (rays @ np.asarray(normal, dtype=float))
        hit = np.isfinite(t) & (t > 0) & (t < best)
        if mask is not None:
            hit &= mask
        best = np.where(hit, t, best)
        indices = np.where(hit, i, indices).astype(np.uint8)
    normals = np.vstack([np.zeros(3)] + [np.asarray(n, dtype=float) for n, _, _ in planes]).astype(np.float32)
    offsets = np.array([0.0] + [d for _, d, _ in planes], dtype=np.float32)
    planes_bundle = gsv.DepthPlanes(indices, normals, offsets)
    raster = gsv._compute_depth_raster(planes_bundle)
    # make_pano takes streetlevel's x-mirrored raster; the writer un-mirrors it (#58).
    pano = make_pano(raster[:, ::-1].astype(np.float64), heading=0.0,
                     pitch=None if pitch is None else math.radians(pitch),
                     roll=None if roll is None else math.radians(roll), planes=planes_bundle)
    gsv._write_depth_artifact(str(storage), pano_id, pano, planes_bundle)
    return os.path.join(str(storage), pano_id[:2], pano_id + gsv.DEPTH_ARTIFACT_SUFFIX)


def resave(path, drop=(), **replace):
    """Rewrite a writer-made artifact with members dropped or replaced: an older or a damaged format."""
    with np.load(path) as art:
        members = {key: art[key] for key in art.files if key not in drop}
    members.update(replace)
    with open(path, 'wb') as f:
        np.savez(f, **members)


def cell_centre_pixel(row, col, gh=GH, gw=GW):
    """The pano pixel at the centre of depth cell (row, col)."""
    return (col + 0.5) * W / gw, (row + 0.5) * H / gh


def stored_pixel(bearing_deg, depression_deg):
    """The stored (gravity-levelled) pano pixel of a direction."""
    x, y = pano_pose.pixel_from_bearing_elevation(bearing_deg, -depression_deg, W, H)
    return float(x), float(y)


def cell_mask(cells, shape=(GH, GW)):
    mask = np.zeros(shape, dtype=bool)
    for r, c in cells:
        mask[r, c % shape[1]] = True
    return mask


def estimate_for(crop_runner, path, x, y):
    return crop_runner.depth_backed_distance(x, y, W, H, path)


def blend_for(crop_runner, y):
    return crop_runner.blend_distance_m(crop_runner.label_depression_deg(y, H))


# ---------------------------------------------------------------------------
# Known geometry: a ground plane at camera height h gives h / tan(depression)
# ---------------------------------------------------------------------------

class TestKnownGeometry:
    @pytest.mark.parametrize('row', [140, 150, 170, 200, 230])
    def test_a_level_ground_plane_is_h_over_tan_depression(self, crop_runner, tmp_path, row):
        """At a cell centre on a level rig the 3x3 median is the centre row's value exactly, so the
        only error left is the artifact's float32."""
        path = write_scene(tmp_path, 'abground0001', [ground()])
        x, y = cell_centre_pixel(row, 77)
        depression = crop_runner.label_depression_deg(y, H)
        est = estimate_for(crop_runner, path, x, y)
        assert est.source == crop_runner.DISTANCE_SOURCE_DEPTH and est.reason is None
        assert est.distance_m == pytest.approx(CAMERA_H / math.tan(math.radians(depression)), rel=1e-5)

    def test_it_is_the_ground_distance_not_the_slant_range(self, crop_runner, tmp_path):
        """The blend it stands in for is a ground distance (V3_CONTEXT_WIDTH_M was fitted on it), so the
        artifact's ray length is projected to the ground: h / sin(d) would be ~6% long at 20 degrees."""
        path = write_scene(tmp_path, 'abground0001', [ground()])
        x, y = cell_centre_pixel(185, 10)
        d = math.radians(crop_runner.label_depression_deg(y, H))
        est = estimate_for(crop_runner, path, x, y)
        assert est.distance_m == pytest.approx(CAMERA_H / math.tan(d), rel=1e-5)
        assert abs(est.distance_m - CAMERA_H / math.sin(d)) > 0.05 * est.distance_m

    def test_it_differs_from_the_blend_where_the_camera_height_does(self, crop_runner, tmp_path):
        path = write_scene(tmp_path, 'abground0001', [ground()])
        x, y = cell_centre_pixel(200, 300)
        assert estimate_for(crop_runner, path, x, y).distance_m == pytest.approx(
            blend_for(crop_runner, y) * CAMERA_H / crop_runner.V3_CAMERA_HEIGHT_M, rel=1e-5)


class TestTheLookupIsAtTheRigPixel:
    """The depth planes are in the rig frame of the npz's own pose (#54 F1); the stored label pixel is
    gravity-levelled (endpoint C, 79 : 0). Sampling at the stored pixel is off by T(b) in elevation."""

    SHAPE = (512, 1024)     # finer than production, so a 2-3 degree tilt is many cells, not one or two

    @pytest.mark.parametrize('bearing, pitch, roll', [(0.0, 3.0, -2.0), (90.0, 3.0, -2.0),
                                                      (-135.0, -2.5, -1.5)])
    @pytest.mark.parametrize('depression', [15.0, 25.0])
    def test_a_tilted_rig_still_gives_h_over_tan_depression(self, crop_runner, tmp_path, bearing, pitch,
                                                            roll, depression):
        path = write_scene(tmp_path, 'abtilted0001', [ground(pitch=pitch, roll=roll)], pitch=pitch, roll=roll,
                           shape=self.SHAPE)
        x, y = stored_pixel(bearing, depression)
        expected = CAMERA_H / math.tan(math.radians(depression))
        est = estimate_for(crop_runner, path, x, y)
        assert est.source == crop_runner.DISTANCE_SOURCE_DEPTH
        # Half a 0.35-degree cell of quantisation is about 1% here.
        assert est.distance_m == pytest.approx(expected, rel=0.015)

        # The test's own power: the same artifact sampled at the STORED pixel is far outside that band.
        with np.load(path) as art:
            grid = art['depth']
        gh, gw = grid.shape
        stored_cell = grid[min(int(y / H * gh), gh - 1), int(x / W * gw) % gw]
        stored_distance = stored_cell * math.cos(math.radians(depression))
        assert abs(stored_distance - expected) > 0.05 * expected

    def test_the_lookup_pixel_is_pano_pose_corrected_pixel_with_the_npz_beta(self, crop_runner, tmp_path,
                                                                             monkeypatch):
        path = write_scene(tmp_path, 'abtilted0001', [ground(pitch=3.0, roll=-2.0)], pitch=3.0, roll=-2.0)
        x, y = stored_pixel(40.0, 20.0)
        for beta in (1.0, 0.5):
            monkeypatch.setitem(crop_runner.TILT_BETA_BY_POSE_SOURCE, pano_pose.POSE_SOURCE_NPZ, beta)
            est = estimate_for(crop_runner, path, x, y)
            assert (est.lookup_x, est.lookup_y) == pytest.approx(
                pano_pose.corrected_pixel(x, y, W, H, 3.0, -2.0, beta), abs=1e-9)

    def test_the_xml_pose_is_never_the_lookups(self, crop_runner, tmp_path):
        """The grid is in the frame of the capture the npz describes; a 2019-22 .xml beside the JPEG poses
        the JPEG, not the grid, so it must not move the lookup."""
        path = write_scene(tmp_path, 'abtilted0001', [ground(pitch=3.0, roll=-2.0)], pitch=3.0, roll=-2.0)
        with open(path[:-len(gsv.DEPTH_ARTIFACT_SUFFIX)] + '.xml', 'w', encoding='utf-8') as f:
            f.write('<panorama><projection_properties pano_yaw_deg="10" tilt_yaw_deg="200" '
                    'tilt_pitch_deg="9"/></panorama>')
        x, y = stored_pixel(0.0, 20.0)
        est = estimate_for(crop_runner, path, x, y)
        assert (est.lookup_x, est.lookup_y) == pytest.approx(
            pano_pose.corrected_pixel(x, y, W, H, 3.0, -2.0, 1.0), abs=1e-9)


# ---------------------------------------------------------------------------
# The fallbacks: each falls back to v3's own blend distance, with its reason
# ---------------------------------------------------------------------------

def assert_fallback(crop_runner, est, y, reason):
    assert est.source == crop_runner.DISTANCE_SOURCE_BLEND
    assert est.reason == reason
    assert reason in crop_runner.DEPTH_FALLBACK_REASONS
    assert est.distance_m == blend_for(crop_runner, y)


class TestFallbacks:
    def test_no_artifact(self, crop_runner, tmp_path):
        x, y = cell_centre_pixel(200, 5)
        est = estimate_for(crop_runner, str(tmp_path / 'ab' / 'abmissing001.depth.npz'), x, y)
        assert_fallback(crop_runner, est, y, 'no_artifact')
        assert est.lookup_x is None and est.lookup_y is None

    def test_no_path_at_all_is_no_artifact(self, crop_runner):
        x, y = cell_centre_pixel(200, 5)
        assert_fallback(crop_runner, estimate_for(crop_runner, None, x, y), y, 'no_artifact')

    def test_garbage_bytes_are_unreadable(self, crop_runner, tmp_path):
        path = tmp_path / 'abbad0000001.depth.npz'
        path.write_bytes(b'PK\x03\x04truncated')
        x, y = cell_centre_pixel(200, 5)
        assert_fallback(crop_runner, estimate_for(crop_runner, str(path), x, y), y, 'unreadable')

    @pytest.mark.parametrize('damage', ['indices-shape', 'index-past-planes', 'depth-1d', 'normals-shape',
                                        'no-depth'])
    def test_a_malformed_v3_artifact_is_unreadable(self, crop_runner, tmp_path, damage):
        path = write_scene(tmp_path, 'abground0001', [ground()])
        if damage == 'indices-shape':
            resave(path, plane_indices=np.ones((GH, GW - 1), dtype=np.uint8))
        elif damage == 'index-past-planes':
            resave(path, plane_indices=np.full((GH, GW), 7, dtype=np.uint8))
        elif damage == 'depth-1d':
            resave(path, depth=np.ones(GW, dtype=np.float32))
        elif damage == 'normals-shape':
            resave(path, planes_n=np.ones((2, 2), dtype=np.float32))
        else:
            resave(path, drop=('depth',))
        x, y = cell_centre_pixel(200, 5)
        assert_fallback(crop_runner, estimate_for(crop_runner, path, x, y), y, 'unreadable')

    def test_a_pre_v2_artifact_is_old_format(self, crop_runner, tmp_path):
        """No format_version: x-mirrored (#58), so its columns are the wrong ones."""
        path = write_scene(tmp_path, 'abground0001', [ground()])
        resave(path, drop=('format_version', 'plane_indices', 'planes_n', 'planes_d'))
        x, y = cell_centre_pixel(200, 5)
        assert_fallback(crop_runner, estimate_for(crop_runner, path, x, y), y, 'old_format')

    def test_a_v2_artifact_is_old_format(self, crop_runner, tmp_path):
        """v2 has no plane fields, so the facade test cannot run; the depth source requires v3 (#180's
        open point 2, decided this way - the PR's decision list)."""
        path = write_scene(tmp_path, 'abground0001', [ground()])
        resave(path, drop=('plane_indices', 'planes_n', 'planes_d'), format_version=np.int64(2))
        x, y = cell_centre_pixel(200, 5)
        assert_fallback(crop_runner, estimate_for(crop_runner, path, x, y), y, 'old_format')

    @pytest.mark.parametrize('pitch, roll', [(None, 0.0), (0.0, None)])
    def test_an_artifact_without_a_pose_is_no_pose(self, crop_runner, tmp_path, pitch, roll):
        """No pose, no rig pixel: the lookup is never made at the stored pixel instead (#180's
        2026-10-05 comment)."""
        path = write_scene(tmp_path, 'abground0001', [ground()], pitch=pitch, roll=roll)
        x, y = cell_centre_pixel(200, 5)
        assert_fallback(crop_runner, estimate_for(crop_runner, path, x, y), y, 'no_pose')

    def test_above_the_horizon_with_no_plane_is_sky(self, crop_runner, tmp_path):
        path = write_scene(tmp_path, 'abground0001', [ground()])
        x, y = cell_centre_pixel(100, 5)
        assert_fallback(crop_runner, estimate_for(crop_runner, path, x, y), y, 'sky')

    def test_below_the_horizon_with_no_plane_is_no_plane(self, crop_runner, tmp_path):
        """-1 is finite and means 'no plane' (#180 open point 1): it must not count towards the five."""
        row, col = 200, 40
        hole = np.ones((GH, GW), dtype=bool)
        hole[row - 2:row + 3, col - 2:col + 3] = False
        path = write_scene(tmp_path, 'abground0001', [ground(mask=hole)])
        x, y = cell_centre_pixel(row, col)
        assert_fallback(crop_runner, estimate_for(crop_runner, path, x, y), y, 'no_plane')

    @pytest.mark.parametrize('valid, source', [(4, 'blend'), (5, 'depth')])
    def test_five_valid_cells_is_the_threshold(self, crop_runner, tmp_path, valid, source):
        """Centre cell empty on purpose: the plane is then the neighbourhood's most common one."""
        row, col = 200, 40
        ring = [(row - 1, col - 1), (row - 1, col), (row - 1, col + 1), (row, col - 1), (row, col + 1),
                (row + 1, col - 1), (row + 1, col), (row + 1, col + 1)]
        path = write_scene(tmp_path, 'abground0001', [ground(mask=cell_mask(ring[:valid]))])
        x, y = cell_centre_pixel(row, col)
        est = estimate_for(crop_runner, path, x, y)
        assert est.source == source
        if source == 'blend':
            assert_fallback(crop_runner, est, y, 'no_plane')

    def test_a_facade_is_facade(self, crop_runner, tmp_path):
        """A wall 8 m ahead hides the ground the label's ray would reach 28 m out."""
        path = write_scene(tmp_path, 'abfacade0001', [ground(), facade(8.0)])
        x, y = stored_pixel(0.0, 5.0)
        assert_fallback(crop_runner, estimate_for(crop_runner, path, x, y), y, 'facade')

    def test_a_steep_but_not_facade_plane_is_depth(self, crop_runner, tmp_path):
        """The threshold is |n_z|/|n| < 0.7: a ground plane tilted 40 degrees (0.766) is still ground."""
        normal = pano_pose.rig_from_gravity(40.0, 0.0) @ np.array([0.0, 0.0, 1.0])
        path = write_scene(tmp_path, 'absteep00001', [(normal, CAMERA_H, None)])
        x, y = stored_pixel(0.0, 30.0)         # the side of the slope the ray meets
        assert estimate_for(crop_runner, path, x, y).source == 'depth'

    def test_too_far_is_out_of_range(self, crop_runner, tmp_path):
        path = write_scene(tmp_path, 'abground0001', [ground()])
        x, y = stored_pixel(0.0, 1.0)            # 2.5 / tan(1 deg) = 143 m > V3_DIST_CAP_M
        assert_fallback(crop_runner, estimate_for(crop_runner, path, x, y), y, 'out_of_range')

    def test_too_near_is_out_of_range(self, crop_runner, tmp_path):
        path = write_scene(tmp_path, 'abground0001', [ground(height=0.2)])
        x, y = stored_pixel(0.0, 45.0)           # 0.2 m < V3_DEPTH_MIN_M
        assert_fallback(crop_runner, estimate_for(crop_runner, path, x, y), y, 'out_of_range')

    def test_the_range_limits_are_inclusive(self, crop_runner, tmp_path, monkeypatch):
        path = write_scene(tmp_path, 'abground0001', [ground()])
        x, y = cell_centre_pixel(200, 5)
        d = estimate_for(crop_runner, path, x, y).distance_m
        monkeypatch.setattr(crop_runner, 'V3_DEPTH_MIN_M', d)
        assert estimate_for(crop_runner, path, x, y).source == 'depth'
        monkeypatch.setattr(crop_runner, 'V3_DEPTH_MIN_M', 0.5)
        monkeypatch.setattr(crop_runner, 'V3_DIST_CAP_M', d)
        assert estimate_for(crop_runner, path, x, y).source == 'depth'
        monkeypatch.setattr(crop_runner, 'V3_DIST_CAP_M', d * 0.999)
        assert estimate_for(crop_runner, path, x, y).reason == 'out_of_range'

    @pytest.mark.parametrize('col', [0, GW - 1])
    def test_the_neighbourhood_wraps_at_the_seam(self, crop_runner, tmp_path, col):
        """Only columns GW-1 and 0 hold ground: six valid cells with the wrap, three without."""
        row = 200
        mask = np.zeros((GH, GW), dtype=bool)
        mask[:, [GW - 1, 0]] = True
        path = write_scene(tmp_path, 'abseam000001', [ground(mask=mask)])
        x, y = cell_centre_pixel(row, col)
        est = estimate_for(crop_runner, path, x, y)
        assert est.source == 'depth'
        assert est.distance_m == pytest.approx(
            CAMERA_H / math.tan(math.radians(crop_runner.label_depression_deg(y, H))), rel=1e-5)

    def test_rows_do_not_wrap(self, crop_runner, tmp_path):
        """The poles are not adjacent: at the bottom row the neighbourhood is 2x3, never the zenith row.
        Two floor cells under the label plus a ceiling over the whole zenith row are five cells only if
        the rows wrap."""
        row, col = GH - 1, 30
        zenith = np.zeros((GH, GW), dtype=bool)
        zenith[0, :] = True
        floor = ground(mask=cell_mask([(row, col - 1), (row, col)]))
        ceiling = (np.array([0.0, 0.0, 1.0]), -5.0, zenith)
        path = write_scene(tmp_path, 'abpole000001', [floor, ceiling])
        x, y = cell_centre_pixel(row, col)
        assert estimate_for(crop_runner, path, x, y).reason == 'no_plane'

    def test_every_reason_is_listed_once(self, crop_runner):
        reasons = crop_runner.DEPTH_FALLBACK_REASONS
        assert len(set(reasons)) == len(reasons)
        assert set(reasons) == {'no_artifact', 'unreadable', 'old_format', 'no_pose', 'sky', 'no_plane',
                                'facade', 'out_of_range'}
        assert crop_runner.DISTANCE_SOURCE_COUNTS == ('distance_depth',) + tuple(
            'distance_blend_' + reason for reason in reasons)


# ---------------------------------------------------------------------------
# The composition
# ---------------------------------------------------------------------------

class TestTheComposition:
    def test_v3_depth_is_the_geometric_window_at_the_distance_given(self, crop_runner):
        for d in (0.3, 2.0, 6.5, 23.0, 49.0):
            assert crop_runner.crop_window_fov_deg(700, H, 'v3-depth', distance_m=d) == \
                crop_runner.geometric_window_fov_deg(d)

    def test_v3_depth_without_a_distance_is_refused(self, crop_runner):
        with pytest.raises(ValueError):
            crop_runner.crop_window_fov_deg(700, H, 'v3-depth')

    @pytest.mark.parametrize('rule', ['v2', 'v3'])
    def test_the_other_rules_refuse_a_distance(self, crop_runner, rule):
        """Rather than ignore it: a caller passing a distance to v3 thinks it is cutting with it."""
        with pytest.raises(ValueError):
            crop_runner.crop_window_fov_deg(700, H, rule, distance_m=5.0)

    def test_the_blend_fallback_is_exactly_v3(self, crop_runner):
        for y in (0, 300, 512, 600, 700, 900, 1023):
            assert crop_runner.crop_window_width(y, W, H, 'v3-depth', distance_m=blend_for(crop_runner, y)) == \
                crop_runner.crop_window_width(y, W, H, 'v3')

    def test_the_rule_is_selectable_and_not_the_default(self, crop_runner):
        assert crop_runner.CROP_RULE_VERSION == 'v2'
        assert 'v3-depth' in crop_runner.CROP_RULE_VERSIONS
        args = crop_runner.build_parser().parse_args(
            ['--city', CITY, '-f', 'x.csv', '-s', 's', '-o', 'o', '--sizing-rule', 'v3-depth'])
        assert args.sizing_rule == 'v3-depth'


# ---------------------------------------------------------------------------
# In the crop loop
# ---------------------------------------------------------------------------

def run(crop_runner, labels, store, out, **kwargs):
    return crop_runner.bulk_extract_crops(labels, str(store), str(out), city=CITY, **kwargs)


@pytest.fixture
def widths(crop_runner, monkeypatch):
    """The width of every window extract_crop cuts, in order."""
    cut = []
    original = crop_runner.extract_crop

    def spy(pano, left, top, width, height):
        cut.append(width)
        return original(pano, left, top, width, height)

    monkeypatch.setattr(crop_runner, 'extract_crop', spy)
    return cut


def distance_counts(crop_runner, counts):
    return {key: counts[key] for key in crop_runner.DISTANCE_SOURCE_COUNTS if counts[key]}


def mixed_store(tmp_path):
    """Four panos: a ground artifact (2 labels below the horizon, 1 above), none, garbage, a facade."""
    store = tmp_path / 'store'
    put_pano(store, 'abground0001')
    write_scene(store, 'abground0001', [ground()])
    put_pano(store, 'cdnone000001')
    put_pano(store, 'efbad0000001')
    (store / 'ef' / 'efbad0000001.depth.npz').write_bytes(b'not a zip')
    put_pano(store, 'ghfacade0001')
    write_scene(store, 'ghfacade0001', [ground(), facade(8.0)])
    below_a, below_b = stored_pixel(0.0, 20.0), stored_pixel(60.0, 30.0)
    above = stored_pixel(10.0, -10.0)
    labels = [label_row(pano_id='abground0001', pano_x=below_a[0], pano_y=below_a[1], label_id=1),
              label_row(pano_id='abground0001', pano_x=below_b[0], pano_y=below_b[1], label_id=2),
              label_row(pano_id='abground0001', pano_x=above[0], pano_y=above[1], label_id=3),
              label_row(pano_id='cdnone000001', pano_x=500, pano_y=700, label_id=4),
              label_row(pano_id='efbad0000001', pano_x=500, pano_y=700, label_id=5),
              label_row(pano_id='ghfacade0001', pano_x=stored_pixel(0.0, 5.0)[0],
                        pano_y=stored_pixel(0.0, 5.0)[1], label_id=6)]
    return store, labels


class TestInTheCropLoop:
    def test_the_window_is_sized_from_the_depth_distance(self, crop_runner, tmp_path, widths):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'abground0001')
        write_scene(store, 'abground0001', [ground()])
        x, y = stored_pixel(0.0, 20.0)
        counts = run(crop_runner, [label_row(pano_id='abground0001', pano_x=x, pano_y=y)], store, out,
                     sizing_rule='v3-depth')
        assert counts['success'] == 1 and counts['distance_depth'] == 1
        d = crop_runner.depth_backed_distance(x, y, W, H, str(store / 'ab' / 'abground0001.depth.npz'))
        expected = crop_runner.compute_crop_box(
            x, y, crop_runner.crop_window_width(y, W, H, 'v3-depth', distance_m=d.distance_m), W, H)
        v3 = crop_runner.compute_crop_box(x, y, crop_runner.crop_window_width(y, W, H, 'v3'), W, H)
        assert widths == [expected.width]
        assert expected.width != v3.width

    def test_every_fallback_is_counted_and_the_counts_reconcile(self, crop_runner, tmp_path, capsys):
        store, labels = mixed_store(tmp_path)
        counts = run(crop_runner, labels, store, tmp_path / 'crops', sizing_rule='v3-depth')
        assert reconciles(counts) and counts['success'] == 6
        assert distance_counts(crop_runner, counts) == {
            'distance_depth': 2, 'distance_blend_sky': 1, 'distance_blend_no_artifact': 1,
            'distance_blend_unreadable': 1, 'distance_blend_facade': 1}
        assert sum(counts[key] for key in crop_runner.DISTANCE_SOURCE_COUNTS) == counts['success']
        printed = capsys.readouterr().out
        assert ('Distance source (v3-depth): 2 crops sized from the depth artifact, 4 from the blend '
                'fallback (no_artifact 1, unreadable 1, sky 1, facade 1).') in printed

    def test_the_summary_is_on_both_channels(self, crop_runner, tmp_path, caplog):
        store, labels = mixed_store(tmp_path)
        with caplog.at_level(logging.INFO):
            run(crop_runner, labels, store, tmp_path / 'crops', sizing_rule='v3-depth')
        assert any(r.getMessage().startswith('Distance source (v3-depth): 2 crops') for r in caplog.records)

    def test_each_crop_logs_its_distance_source(self, crop_runner, tmp_path, caplog):
        store, labels = mixed_store(tmp_path)
        with caplog.at_level(logging.INFO):
            run(crop_runner, labels, store, tmp_path / 'crops', sizing_rule='v3-depth')
        lines = [r.getMessage() for r in caplog.records if ' distance ' in r.getMessage()]
        assert len(lines) == 6
        assert any(line.startswith('1.jpg abground0001 distance ') and line.endswith(' from depth')
                   for line in lines)
        assert any(line.startswith('4.jpg cdnone000001 distance ') and line.endswith(' from blend (no_artifact)')
                   for line in lines)

    def test_a_skipped_or_failed_label_is_not_counted_as_a_source(self, crop_runner, tmp_path, monkeypatch):
        """The source counts annotate a success, so a label the run did not write adds to none of them."""
        store, labels = mixed_store(tmp_path)
        out = tmp_path / 'crops'
        run(crop_runner, labels[:1], store, out, sizing_rule='v3-depth')       # label 1 now on disk
        original = crop_runner.make_single_crop

        def fail_for_label_2(pano, x, y, path, **kwargs):
            if path.endswith(os.sep + '2.jpg'):
                raise OSError('disk full')
            return original(pano, x, y, path, **kwargs)

        monkeypatch.setattr(crop_runner, 'make_single_crop', fail_for_label_2)
        counts = run(crop_runner, labels, store, out, sizing_rule='v3-depth')
        assert counts['skipped_existing'] == 1 and counts['errors'] == 1 and counts['success'] == 4
        assert sum(counts[key] for key in crop_runner.DISTANCE_SOURCE_COUNTS) == counts['success']
        assert counts['distance_depth'] == 0

    def test_the_artifact_is_read_once_per_pano_and_never_for_a_finished_store(self, crop_runner, tmp_path,
                                                                               monkeypatch):
        store, labels = mixed_store(tmp_path)
        out = tmp_path / 'crops'
        calls = []
        original = crop_runner.load_depth_grid

        def spy(path):
            calls.append(os.path.basename(path))
            return original(path)

        monkeypatch.setattr(crop_runner, 'load_depth_grid', spy)
        run(crop_runner, labels, store, out, sizing_rule='v3-depth')
        assert sorted(calls) == ['abground0001.depth.npz', 'cdnone000001.depth.npz', 'efbad0000001.depth.npz',
                                 'ghfacade0001.depth.npz']
        calls.clear()
        counts = run(crop_runner, labels, store, out, sizing_rule='v3-depth')
        assert counts['skipped_existing'] == 6 and calls == []

    def test_a_raise_in_the_lookup_is_a_counted_error_and_the_run_goes_on(self, crop_runner, tmp_path,
                                                                         monkeypatch):
        store, labels = mixed_store(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError('bug')

        monkeypatch.setattr(crop_runner, 'label_distance', boom)
        counts = run(crop_runner, labels, store, tmp_path / 'crops', sizing_rule='v3-depth')
        assert counts['errors'] == 6 and counts['success'] == 0 and reconciles(counts)
        assert sum(counts[key] for key in crop_runner.DISTANCE_SOURCE_COUNTS) == 0

    def test_under_tilt_correction_the_lookup_is_still_the_npz_rig_pixel(self, crop_runner, tmp_path,
                                                                         monkeypatch):
        """The correction moves the window's centre; the depth lookup is its own business."""
        store = tmp_path / 'store'
        put_pano(store, 'abtilted0001')
        write_scene(store, 'abtilted0001', [ground(pitch=3.0, roll=-2.0)], pitch=3.0, roll=-2.0)
        x, y = stored_pixel(0.0, 20.0)
        seen = []
        original = crop_runner.label_distance

        def spy(*args, **kwargs):
            est = original(*args, **kwargs)
            seen.append(est)
            return est

        monkeypatch.setattr(crop_runner, 'label_distance', spy)
        counts = run(crop_runner, [label_row(pano_id='abtilted0001', pano_x=x, pano_y=y)], store,
                     tmp_path / 'crops', sizing_rule='v3-depth', tilt_correction=True)
        assert counts['success'] == 1 and counts['distance_depth'] == 1
        assert (seen[0].lookup_x, seen[0].lookup_y) == pytest.approx(
            pano_pose.corrected_pixel(x, y, W, H, 3.0, -2.0, 1.0), abs=1e-9)

    def test_the_marker_records_the_rule_and_the_lookup(self, crop_runner, tmp_path):
        store, labels = mixed_store(tmp_path)
        out = tmp_path / 'crops'
        run(crop_runner, labels, store, out, sizing_rule='v3-depth')
        marker = json.loads((out / crop_runner.CROP_RULE_MARKER).read_text(encoding='utf-8'))
        assert marker['crop_rule_version'] == 'v3-depth'
        assert marker['distance_estimator'] == crop_runner.CROP_RULE_DISTANCE_ESTIMATOR['v3-depth']
        assert marker['v3_depth_lookup_pixel'] == 'rig'
        assert marker['v3_depth_lookup_beta'] == crop_runner.TILT_BETA_BY_POSE_SOURCE['npz']
        assert marker['v3_depth_min_valid_cells'] == 5
        assert marker['v3_depth_min_m'] == 0.5
        assert marker['v3_depth_min_ground_verticality'] == 0.7
        assert marker['v3_depth_min_format_version'] == 3
        seen = marker['constants_seen']['v3-depth']
        for key in crop_runner.RULE_MARKER_CONSTANT_KEYS['v3-depth']:
            assert seen[key] == [marker[key]]
        assert set(crop_runner.RULE_MARKER_CONSTANT_KEYS['v3']) < set(
            crop_runner.RULE_MARKER_CONSTANT_KEYS['v3-depth'])

    def test_a_v3_store_topped_up_under_v3_depth_warns_it_is_mixed(self, crop_runner, tmp_path, capsys):
        store, labels = mixed_store(tmp_path)
        out = tmp_path / 'crops'
        run(crop_runner, labels[:2], store, out, sizing_rule='v3')
        capsys.readouterr()
        run(crop_runner, labels, store, out, sizing_rule='v3-depth')
        assert 'was cut under sizing rule v3 and this run uses v3-depth' in capsys.readouterr().out

    def test_a_changed_lookup_beta_warns_under_the_same_rule(self, crop_runner, tmp_path, capsys, monkeypatch):
        store, labels = mixed_store(tmp_path)
        out = tmp_path / 'crops'
        run(crop_runner, labels[:2], store, out, sizing_rule='v3-depth')
        monkeypatch.setitem(crop_runner.TILT_BETA_BY_POSE_SOURCE, pano_pose.POSE_SOURCE_NPZ, 0.9)
        capsys.readouterr()
        run(crop_runner, labels, store, out, sizing_rule='v3-depth')
        assert 'v3_depth_lookup_beta=1.0 and this run uses 0.9' in capsys.readouterr().out

    def test_main_accepts_the_rule(self, crop_runner, tmp_path):
        store, labels = mixed_store(tmp_path)
        metadata = tmp_path / 'labels.json'
        metadata.write_text(json.dumps(labels), encoding='utf-8')
        out = tmp_path / 'crops'
        assert crop_runner.main(['-f', str(metadata), '-s', str(store), '-o', str(out), '--city', CITY,
                                 '--sizing-rule', 'v3-depth']) == 0
        marker = json.loads((out / CITY / crop_runner.CROP_RULE_MARKER).read_text(encoding='utf-8'))
        assert marker['crop_rule_version'] == 'v3-depth'


# ---------------------------------------------------------------------------
# Off is identical: the default output does not move
# ---------------------------------------------------------------------------

def crop_bytes(out, label_ids):
    return [open(crop_path(out, 1, i), 'rb').read() for i in label_ids]


class TestOffIsIdentical:
    @pytest.mark.parametrize('rule', ['v2', 'v3'])
    def test_the_other_rules_never_read_an_artifact(self, crop_runner, tmp_path, monkeypatch, rule):
        """v2 and v3 cut the same bytes with a depth artifact beside the pano as without one, and never
        look at it - the loader and the lookup are poisoned for the artifact run."""
        results = []
        for with_artifact in (False, True):
            store, labels = mixed_store(tmp_path / str(with_artifact))
            if not with_artifact:
                for npz in store.rglob('*.depth.npz'):
                    npz.unlink()
            else:
                monkeypatch.setattr(crop_runner, 'load_depth_grid',
                                    lambda path: pytest.fail('artifact read under %s' % rule))
                monkeypatch.setattr(crop_runner, 'label_distance',
                                    lambda *a, **k: pytest.fail('distance looked up under %s' % rule))
            out = tmp_path / str(with_artifact) / 'crops'
            counts = run(crop_runner, labels, store, out, sizing_rule=rule)
            assert counts['success'] == 6
            assert not set(crop_runner.DISTANCE_SOURCE_COUNTS) & set(counts)
            results.append(crop_bytes(out, range(1, 7)))
        assert results[0] == results[1]

    @pytest.mark.parametrize('rule', ['v2', 'v3'])
    def test_the_other_rules_make_the_call_they_always_made(self, crop_runner, tmp_path, monkeypatch, rule):
        store, labels = mixed_store(tmp_path)
        calls = []
        original = crop_runner.make_single_crop

        def spy(*args, **kwargs):
            calls.append(sorted(kwargs))
            return original(*args, **kwargs)

        monkeypatch.setattr(crop_runner, 'make_single_crop', spy)
        run(crop_runner, labels, store, tmp_path / 'crops', sizing_rule=rule)
        assert calls == [['draw_mark', 'sizing_rule']] * 6

    def test_v3_depth_with_no_artifact_cuts_v3s_bytes(self, crop_runner, tmp_path):
        """Every fallback is v3's own blend distance, so a store with no artifacts at all - a Mapillary
        city - cuts exactly the crops v3 cuts."""
        results = []
        for rule in ('v3', 'v3-depth'):
            store, labels = mixed_store(tmp_path / rule)
            for npz in store.rglob('*.depth.npz'):
                npz.unlink()
            out = tmp_path / rule / 'crops'
            counts = run(crop_runner, labels, store, out, sizing_rule=rule)
            assert counts['success'] == 6
            results.append(crop_bytes(out, range(1, 7)))
        assert results[0] == results[1]

    def test_v3_depth_falling_back_for_every_reason_cuts_v3s_bytes(self, crop_runner, tmp_path):
        """And so does a store whose artifacts are all refused: only the depth-sourced crops differ."""
        results = []
        for rule in ('v3', 'v3-depth'):
            store, labels = mixed_store(tmp_path / rule)
            out = tmp_path / rule / 'crops'
            run(crop_runner, labels, store, out, sizing_rule=rule)
            results.append(crop_bytes(out, range(1, 7)))
        v3, v3_depth = results
        # Labels 1 and 2 are sized from depth (camera height 2.5 m, not 2.34 m); 3-6 fall back.
        assert v3[2:] == v3_depth[2:]
        assert v3[0] != v3_depth[0] and v3[1] != v3_depth[1]

    def test_the_default_run_prints_no_distance_line(self, crop_runner, tmp_path, capsys):
        store, labels = mixed_store(tmp_path)
        run(crop_runner, labels, store, tmp_path / 'crops')
        assert 'Distance source' not in capsys.readouterr().out
