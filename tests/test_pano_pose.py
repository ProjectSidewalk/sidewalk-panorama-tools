"""Tests for pano_pose.py - the production home of the #54 study's frame geometry, plus what the cropper's
tilt correction (#191) adds to it: beta, the pose readers, the scrape-era rule and the label's era.

The geometry's conventions are pinned against things outside the module by tests/test_tilt_geometry.py,
which now runs through reports/scripts/tilt_geometry.py's re-export. What is pinned here is that the move
changed nothing (literals computed from origin/master's tilt_geometry before the move), that the shim cannot
drift, and the cropper-side behaviour.
"""

import datetime
import os
import sys
import zipfile

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import pano_pose  # noqa: E402
import tilt_geometry  # noqa: E402
from downloaders import gsv  # noqa: E402

# rig_pixel_from_gravity_pixel(x, y, w, h, pitch, roll) as `git show origin/master:reports/scripts/
# tilt_geometry.py` computed it before the move (2026-09-29): the seam, both poles, a non-2:1 pano, and a
# roll wrapped from photometa's 359.6.
RIG_PIXEL_BEFORE_THE_MOVE = [
    ((0.0, 512.0, 2048, 1024, 2.0, -1.5), (0.29795644458499737, 523.3738773139866)),
    ((1023.5, 300.0, 2048, 1024, -3.0, 4.0), (1008.0422278530299, 317.64081972928483)),
    ((2047.9, 700.0, 2048, 1024, 5.0, -0.39999999999997726), (1.6743919980886859, 728.4389698001295)),
    ((1.0, 1.0, 2048, 1024, 4.0, 2.0), (145.37472134944917, 26.336677901628775)),
    ((1000.0, 1023.0, 2048, 1024, -2.5, -3.0), (297.63879227780205, 1002.3545120263921)),
    ((8000.0, 4096.0, 16384, 8192, 1.2, 0.7), (7999.308630606972, 4043.8827655271357)),
    ((13000.0, 6000.0, 13312, 6656, -0.8, 2.2), (12777.759230877655, 5954.803668177219)),
    ((500.0, 700.0, 1024, 1024, 6.0, -4.0), (494.9555580793938, 663.9765319036892)),
]

XML_TILT_BEFORE_THE_MOVE = [
    ((269.22998, 110.909996, 4.73), (4.395406789660389, 1.7473692092420987)),
    ((0.0, 90.0, 2.0), (-1.2246467991473532e-16, -2.0)),
    ((359.6, 10.0, 1.5), (-1.4753572062200788, -0.2707787178758394)),
    ((120.0, 120.0, 3.0), (-3.0, -0.0)),
]

GEOMETRY_NAMES = ('wrap_deg', 'direction_rfu', 'bearing_elevation', 'rig_from_gravity', 'gravity_to_rig',
                  'rig_to_gravity', 'tilt_term_deg', 'vertical_lean_deg', 'pixel_from_bearing_elevation',
                  'bearing_elevation_from_pixel', 'rig_pixel_from_gravity_pixel', 'gravity_pixel_from_rig_pixel',
                  'artifact_normal_bearing_elevation', 'xml_tilt_to_pitch_roll', 'pitch_roll_to_xml_tilt')

XML = ('<?xml version="1.0" encoding="UTF-8" ?><panorama><data_properties image_width="2048" '
       'image_height="1024" image_date="2018-10" pano_id="p"></data_properties>'
       '<projection_properties projection_type="spherical" {attrs}/></panorama>')


def write_xml(path, pano_yaw='269.22998', tilt_yaw='110.909996', tilt_pitch='4.73'):
    attrs = ' '.join('%s="%s"' % (k, v) for k, v in (('pano_yaw_deg', pano_yaw), ('tilt_yaw_deg', tilt_yaw),
                                                      ('tilt_pitch_deg', tilt_pitch)) if v is not None)
    with open(str(path), 'w', encoding='utf-8') as f:
        f.write(XML.format(attrs=attrs))


def write_npz(path, pitch_rad=0.05, roll_rad=0.02, **extra):
    fields = dict(extra)
    if pitch_rad is not None:
        fields['pitch'] = np.float64(pitch_rad)
    if roll_rad is not None:
        fields['roll'] = np.float64(roll_rad)
    fields.setdefault('heading', np.float64(1.0))
    fields.setdefault('format_version', np.int64(3))
    with open(str(path), 'wb') as f:
        np.savez(f, **fields)


class TestTheMoveChangedNothing:
    @pytest.mark.parametrize('args, expected', RIG_PIXEL_BEFORE_THE_MOVE)
    def test_rig_pixel_matches_the_pre_move_module(self, args, expected):
        x, y = pano_pose.rig_pixel_from_gravity_pixel(*args)
        assert (float(x), float(y)) == pytest.approx(expected, abs=1e-9)

    @pytest.mark.parametrize('args, expected', XML_TILT_BEFORE_THE_MOVE)
    def test_xml_conversion_matches_the_pre_move_module(self, args, expected):
        p, r = pano_pose.xml_tilt_to_pitch_roll(*args)
        assert (float(p), float(r)) == pytest.approx(expected, abs=1e-12)

    def test_the_wrapped_roll_row_is_photometas_359_6(self):
        assert RIG_PIXEL_BEFORE_THE_MOVE[2][0][5] == pytest.approx(pano_pose.wrap_deg(359.6))

    @pytest.mark.parametrize('name', GEOMETRY_NAMES)
    def test_tilt_geometry_is_a_re_export_not_a_copy(self, name):
        assert getattr(tilt_geometry, name) is getattr(pano_pose, name)

    def test_tilt_geometry_defines_nothing_of_its_own(self):
        own = [n for n, v in vars(tilt_geometry).items()
               if callable(v) and getattr(v, '__module__', None) == 'tilt_geometry']
        assert own == []


class TestBeta:
    GRID = [(x, y) for x in (0.0, 5.0, 700.0, 1500.0, 2047.5) for y in (3.0, 200.0, 512.0, 900.0, 1020.0)]

    @pytest.mark.parametrize('x, y', GRID)
    def test_beta_zero_is_the_identity(self, x, y):
        assert pano_pose.corrected_pixel(x, y, 2048, 1024, 4.0, -3.0, 0.0) == pytest.approx((x, y), abs=1e-9)

    def test_beta_zero_wraps_x_like_the_transform_does(self):
        assert pano_pose.corrected_pixel(2048.0, 10.0, 2048, 1024, 4.0, -3.0, 0.0) == (0.0, 10.0)

    @pytest.mark.parametrize('x, y', GRID)
    def test_beta_one_is_the_full_transform(self, x, y):
        full = pano_pose.rig_pixel_from_gravity_pixel(x, y, 2048, 1024, 4.0, -3.0)
        assert pano_pose.corrected_pixel(x, y, 2048, 1024, 4.0, -3.0, 1.0) == pytest.approx(
            (float(full[0]), float(full[1])), abs=1e-9)

    def test_beta_scales_the_pose_not_the_pixel_move(self):
        """The choice this module makes, pinned: beta 0.5 is the transform at half the pose. At a large tilt
        and off the horizon the pose-scaled and the interpolated answers differ at second order, so a
        mutant that interpolates the pixels fails the equality while agreeing to first order."""
        x, y, w, h, p, r = 300.0, 150.0, 2048, 1024, 20.0, 15.0
        half = pano_pose.corrected_pixel(x, y, w, h, p, r, 0.5)
        expected = pano_pose.rig_pixel_from_gravity_pixel(x, y, w, h, 0.5 * p, 0.5 * r)
        assert half == pytest.approx((float(expected[0]), float(expected[1])), abs=1e-9)
        full = pano_pose.corrected_pixel(x, y, w, h, p, r, 1.0)
        interpolated = (x + 0.5 * (full[0] - x), y + 0.5 * (full[1] - y))
        assert abs(half[1] - interpolated[1]) > 0.5 or abs(half[0] - interpolated[0]) > 0.5

    @pytest.mark.parametrize('y', [300.0, 700.0])
    def test_half_beta_moves_about_half_as_far_the_same_way(self, y):
        """Off the horizon, where x moves at first order too (on it, x moves only at second)."""
        x, w, h = 1300.0, 2048, 1024
        full = pano_pose.corrected_pixel(x, y, w, h, 3.0, 2.0, 1.0)
        half = pano_pose.corrected_pixel(x, y, w, h, 3.0, 2.0, 0.5)
        for axis, start in ((0, x), (1, y)):
            assert np.sign(half[axis] - start) == np.sign(full[axis] - start)
            assert (half[axis] - start) == pytest.approx(0.5 * (full[axis] - start), rel=0.02)

    @pytest.mark.parametrize('beta', [0.25, 0.936, 1.0, 1.5])
    @pytest.mark.parametrize('x', [0.0, 512.0, 1024.0, 1600.0])
    def test_first_order_y_move_is_minus_beta_T_h_over_180(self, beta, x):
        w, h, p, r = 2048, 1024, 0.6, -0.4
        b = x / w * 360.0 - 180.0
        _, cy = pano_pose.corrected_pixel(x, 512.0, w, h, p, r, beta)
        predicted = -beta * pano_pose.tilt_term_deg(b, p, r) * h / 180.0
        assert cy - 512.0 == pytest.approx(predicted, abs=0.02)


class TestPoseReaders:
    def test_an_xml_triple_round_trips(self, tmp_path):
        yaw, tilt_yaw, tilt_pitch = pano_pose.pitch_roll_to_xml_tilt(123.0, 2.5, -1.25)
        path = tmp_path / 'p.xml'
        write_xml(path, repr(float(yaw)), repr(float(tilt_yaw)), repr(float(tilt_pitch)))
        pose, reason = pano_pose.pose_from_xml(str(path))
        assert reason is None
        assert (pose.pitch_deg, pose.roll_deg) == pytest.approx((2.5, -1.25), abs=1e-9)
        assert pose.source == 'xml'

    @pytest.mark.parametrize('missing', ['pano_yaw', 'tilt_yaw', 'tilt_pitch'])
    def test_an_xml_missing_a_field_is_no_pose(self, tmp_path, missing):
        path = tmp_path / 'p.xml'
        write_xml(path, **{missing: None})
        pose, reason = pano_pose.pose_from_xml(str(path))
        assert pose is None and missing in reason

    @pytest.mark.parametrize('value', ['', 'abc', 'nan', 'inf'])
    def test_an_xml_with_an_unusable_value_is_no_pose(self, tmp_path, value):
        path = tmp_path / 'p.xml'
        write_xml(path, tilt_pitch=value)
        pose, reason = pano_pose.pose_from_xml(str(path))
        assert pose is None and 'tilt_pitch_deg' in reason

    def test_an_xml_without_projection_properties_is_no_pose(self, tmp_path):
        path = tmp_path / 'p.xml'
        path.write_text('<panorama><data_properties/></panorama>')
        pose, reason = pano_pose.pose_from_xml(str(path))
        assert pose is None and 'projection_properties' in reason

    def test_an_unparseable_xml_is_no_pose(self, tmp_path):
        path = tmp_path / 'p.xml'
        path.write_text('<panorama><projection')
        pose, reason = pano_pose.pose_from_xml(str(path))
        assert pose is None and 'unreadable' in reason

    def test_an_xml_naming_an_unknown_encoding_is_no_pose(self, tmp_path):
        """ET.parse raises LookupError, not ParseError, for a declaration naming an encoding Python does not
        know; uncaught, it ended the whole crop run at that pano (#193 review). Fails with the reader's
        except narrowed back to (OSError, ParseError)."""
        path = tmp_path / 'p.xml'
        path.write_text('<?xml version="1.0" encoding="bogus"?><panorama/>', encoding='ascii')
        pose, reason = pano_pose.pose_from_xml(str(path))
        assert pose is None and 'unreadable' in reason and 'LookupError' in reason

    def test_npz_radians_become_wrapped_degrees(self, tmp_path):
        path = tmp_path / 'p.depth.npz'
        write_npz(path, pitch_rad=np.radians(2.0), roll_rad=np.radians(359.6))
        pose, reason = pano_pose.pose_from_depth_artifact(str(path))
        assert reason is None
        assert (pose.pitch_deg, pose.roll_deg) == pytest.approx((2.0, -0.4), abs=1e-9)
        assert pose.source == 'npz'

    @pytest.mark.parametrize('pitch, roll', [(float('nan'), 0.01), (0.01, float('nan'))])
    def test_a_nan_npz_is_no_pose(self, tmp_path, pitch, roll):
        path = tmp_path / 'p.depth.npz'
        write_npz(path, pitch_rad=pitch, roll_rad=roll)
        pose, reason = pano_pose.pose_from_depth_artifact(str(path))
        assert pose is None and 'not finite' in reason

    @pytest.mark.parametrize('drop', ['pitch', 'roll'])
    def test_an_npz_missing_a_key_is_no_pose(self, tmp_path, drop):
        path = tmp_path / 'p.depth.npz'
        write_npz(path, **{drop + '_rad': None})
        pose, reason = pano_pose.pose_from_depth_artifact(str(path))
        assert pose is None and drop in reason

    def test_a_corrupt_npz_is_no_pose(self, tmp_path):
        path = tmp_path / 'p.depth.npz'
        path.write_bytes(b'PK\x03\x04 not really a zip')
        pose, reason = pano_pose.pose_from_depth_artifact(str(path))
        assert pose is None and 'unreadable' in reason

    def test_a_non_scalar_pitch_is_no_pose(self, tmp_path):
        path = tmp_path / 'p.depth.npz'
        with open(str(path), 'wb') as f:
            np.savez(f, pitch=np.zeros(3), roll=np.float64(0.0))
        pose, reason = pano_pose.pose_from_depth_artifact(str(path))
        assert pose is None and 'unreadable' in reason

    def test_only_the_two_scalars_are_read(self, tmp_path):
        """A depth raster whose member is corrupt must not matter: the reader never touches it."""
        path = tmp_path / 'p.depth.npz'
        write_npz(path, depth=np.zeros((4, 8), dtype=np.float32))
        with zipfile.ZipFile(str(path)) as z:
            members = {name: z.read(name) for name in z.namelist()}
        members['depth.npy'] = b'garbage'
        with zipfile.ZipFile(str(path), 'w') as z:
            for name, data in members.items():
                z.writestr(name, data)
        pose, reason = pano_pose.pose_from_depth_artifact(str(path))
        assert reason is None and pose.source == 'npz'

    def test_a_missing_file_is_no_pose(self, tmp_path):
        assert pano_pose.pose_from_depth_artifact(str(tmp_path / 'none.depth.npz'))[0] is None
        assert pano_pose.pose_from_xml(str(tmp_path / 'none.xml'))[0] is None


class TestTheScrapeEraRule:
    def jpg(self, tmp_path):
        path = tmp_path / 'ab' / 'abPano.jpg'
        path.parent.mkdir()
        path.write_bytes(b'')
        return path

    def test_the_npz_suffix_is_gsvs(self, tmp_path):
        jpg = self.jpg(tmp_path)
        write_npz(str(jpg)[:-4] + gsv.DEPTH_ARTIFACT_SUFFIX)
        pose, _ = pano_pose.resolve_pano_pose(str(jpg))
        assert pose.source == 'npz'

    def test_the_xml_wins_over_an_npz_beside_it(self, tmp_path):
        jpg = self.jpg(tmp_path)
        write_xml(str(jpg)[:-4] + '.xml')
        write_npz(str(jpg)[:-4] + gsv.DEPTH_ARTIFACT_SUFFIX, pitch_rad=0.3, roll_rad=0.3)
        pose, _ = pano_pose.resolve_pano_pose(str(jpg))
        assert pose.source == 'xml'
        assert (pose.pitch_deg, pose.roll_deg) == pytest.approx((4.395406789660389, 1.7473692092420987))

    def test_an_incomplete_xml_never_falls_through_to_the_npz(self, tmp_path):
        jpg = self.jpg(tmp_path)
        write_xml(str(jpg)[:-4] + '.xml', tilt_pitch=None)
        write_npz(str(jpg)[:-4] + gsv.DEPTH_ARTIFACT_SUFFIX)
        pose, reason = pano_pose.resolve_pano_pose(str(jpg))
        assert pose is None and 'xml' in reason

    def test_a_suffix_handed_in_needs_no_import(self, tmp_path, monkeypatch):
        """CropRunner hands gsv's suffix in from its own module-scope import, so resolving a pose can never
        fail per pano on an import (#193 review). Fails if the import is done whether or not one is given."""
        jpg = self.jpg(tmp_path)
        write_npz(str(jpg)[:-4] + gsv.DEPTH_ARTIFACT_SUFFIX)
        monkeypatch.setitem(sys.modules, 'downloaders.gsv', None)
        pose, _ = pano_pose.resolve_pano_pose(str(jpg), depth_suffix=gsv.DEPTH_ARTIFACT_SUFFIX)
        assert pose.source == 'npz'
        with pytest.raises(ImportError):
            pano_pose.resolve_pano_pose(str(jpg))

    def test_neither_is_no_pose(self, tmp_path):
        pose, reason = pano_pose.resolve_pano_pose(str(self.jpg(tmp_path)))
        assert pose is None and '.xml' in reason and gsv.DEPTH_ARTIFACT_SUFFIX in reason


BOUNDARY_MS = 1680048000000  # 2023-03-29T00:00:00Z


class TestLabelEra:
    def test_the_boundary_constant_is_that_instant(self):
        assert datetime.datetime.fromtimestamp(BOUNDARY_MS / 1000, tz=datetime.timezone.utc) == \
            datetime.datetime.fromisoformat(pano_pose.EVO179_UTC)

    @pytest.mark.parametrize('value, era', [
        (BOUNDARY_MS, 'post179'),
        (BOUNDARY_MS - 1, 'legacy+mid'),
        (BOUNDARY_MS + 1, 'post179'),
        (float(BOUNDARY_MS), 'post179'),
        (str(BOUNDARY_MS - 1), 'legacy+mid'),
        (' %d ' % BOUNDARY_MS, 'post179'),
        ('%d.0' % (BOUNDARY_MS - 1000), 'legacy+mid'),
        (1500000000000, 'legacy+mid'),
        ('2023-03-29T00:00:00Z', 'post179'),
        ('2023-03-28T23:59:59Z', 'legacy+mid'),
        ('2023-03-28T23:59:59', 'legacy+mid'),
        ('2023-03-29T01:00:00+02:00', 'legacy+mid'),
        ('2023-03-29 00:00:00+00:00', 'post179'),
        ('2024-01-01', 'post179'),
    ])
    def test_either_side_of_the_boundary(self, value, era):
        assert pano_pose.label_era(value) == era

    @pytest.mark.parametrize('value', [None, '', '   ', float('nan'), float('inf'), 'garbage', 'nan', True,
                                       '1.2.3', [BOUNDARY_MS], 1e30])
    def test_anything_unreadable_is_unknown(self, value):
        assert pano_pose.label_era(value) == 'unknown'

    def test_agrees_with_the_studys_boundary(self):
        rawlabels = pytest.importorskip('rawlabels')
        assert datetime.datetime.fromisoformat(pano_pose.EVO179_UTC) == rawlabels.EVO179.to_pydatetime()
