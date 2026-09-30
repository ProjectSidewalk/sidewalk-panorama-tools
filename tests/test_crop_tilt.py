"""Tests for CropRunner's opt-in tilt correction (#191, following the #54 study).

Under --tilt-correction each label's crop is centred on the rig pixel of the stored tiles - the stored
(gravity-levelled) pano_x/pano_y moved by beta times the rig transform, beta per label era - with the pose
read from the pano's own .xml (a 2019-22 scrape) or else its .depth.npz, and a pano with neither counted
no_pose, never guessed. Without the flag nothing changes.

Registration is asserted the #78 way: a unique pixel is planted at the rig position in a lossless synthetic
pano, the real loop cuts it, and the pixel is read back out of the cut window where label_position_in_crop
says it is. The stored point handed in is derived from the planted one by the INVERSE transform
(gravity_pixel_from_rig_pixel), so the test is a round trip through the geometry, not one derivation
compared with another.
"""

import csv
import json
import logging
import os

import numpy as np
import pytest
from PIL import Image

import pano_pose
# crop_runner and the autouse logging isolation are fixtures: importing them into this module's namespace
# is what makes pytest apply them here.
from test_crop_runner import (  # noqa: F401
    STALE_KEPT_SUMMARY, _isolate_logging_state, crop_path, crop_runner, label_row, put_pano, reconciles)

CITY = 'seattle-wa'
W, H = 2048, 1024
PITCH, ROLL = 3.0, -2.0
PLANTED = (255, 0, 0)
BASE = (0, 0, 255)
BOUNDARY_MS = 1680048000000  # pano_pose.EVO179_UTC in epoch ms


def put_lossless_pano(store, pano_id, planted_at=None, size=(W, H)):
    """A pano at <id[:2]>/<id>.jpg holding PNG bytes: Image.open reads it by content, so a single planted
    pixel survives exactly (JPEG would smear it across its block)."""
    shard = os.path.join(str(store), pano_id[:2])
    os.makedirs(shard, exist_ok=True)
    img = Image.new('RGB', size, BASE)
    if planted_at is not None:
        img.putpixel(planted_at, PLANTED)
    path = os.path.join(shard, pano_id + '.jpg')
    img.save(path, format='PNG')
    return path


def write_npz_pose(jpg_path, pitch_deg=PITCH, roll_deg=ROLL):
    path = jpg_path[:-4] + '.depth.npz'
    with open(path, 'wb') as f:
        np.savez(f, pitch=np.float64(np.radians(pitch_deg)), roll=np.float64(np.radians(roll_deg)),
                 heading=np.float64(0.0), format_version=np.int64(3))
    return path


def write_xml_pose(jpg_path, pitch_deg=PITCH, roll_deg=ROLL, drop=None):
    yaw, tilt_yaw, tilt_pitch = pano_pose.pitch_roll_to_xml_tilt(200.0, pitch_deg, roll_deg)
    attrs = {'pano_yaw_deg': float(yaw), 'tilt_yaw_deg': float(tilt_yaw), 'tilt_pitch_deg': float(tilt_pitch)}
    if drop:
        del attrs[drop]
    path = jpg_path[:-4] + '.xml'
    with open(path, 'w', encoding='utf-8') as f:
        f.write('<panorama><projection_properties projection_type="spherical" %s/></panorama>'
                % ' '.join('%s="%r"' % item for item in attrs.items()))
    return path


def write_pose(jpg_path, source, **kwargs):
    return (write_xml_pose if source == 'xml' else write_npz_pose)(jpg_path, **kwargs)


def run(crop_runner, labels, store, out, **kwargs):
    return crop_runner.bulk_extract_crops(labels, str(store), str(out), city=CITY, **kwargs)


@pytest.fixture
def windows(crop_runner, monkeypatch):
    """Every window extract_crop cuts, as (CropBox-shaped args, the exact window image)."""
    cut = []
    original = crop_runner.extract_crop

    def spy(pano, left, top, width, height):
        window = original(pano, left, top, width, height)
        cut.append((crop_runner.CropBox(left, top, width, height, False), window.copy()))
        return window

    monkeypatch.setattr(crop_runner, 'extract_crop', spy)
    return cut


def planted_position(window):
    """(x, y) of the one PLANTED pixel in a window."""
    pixels = np.asarray(window.convert('RGB'))
    ys, xs = np.nonzero(np.all(pixels == PLANTED, axis=-1))
    assert len(xs) == 1
    return int(xs[0]), int(ys[0])


def stored_point_for(rig_x, rig_y, pitch=PITCH, roll=ROLL):
    """The gravity-frame (stored) point whose rig pixel is the centre of pixel (rig_x, rig_y)."""
    x, y = pano_pose.gravity_pixel_from_rig_pixel(rig_x + 0.5, rig_y + 0.5, W, H, pitch, roll)
    return float(x), float(y)


# ---------------------------------------------------------------------------
# Off is identical
# ---------------------------------------------------------------------------

class TestOffIsIdentical:
    def test_a_pose_beside_the_pano_is_never_read_without_the_flag(self, crop_runner, tmp_path, monkeypatch,
                                                                    capsys):
        """The same store cut with and without pose files beside it, flag off: byte-identical crops, identical
        counts and summary, and the pose reader never called."""
        labels = [label_row(pano_x=300, pano_y=y, label_id=i) for i, y in enumerate((100, 512, 900), 1)]
        results = []
        for with_pose in (False, True):
            store, out = tmp_path / ('s%d' % with_pose), tmp_path / ('o%d' % with_pose)
            jpg = put_pano(store, 'testpano0001')
            if with_pose:
                write_npz_pose(jpg)
                write_xml_pose(jpg)
                monkeypatch.setattr(pano_pose, 'resolve_pano_pose',
                                    lambda path: pytest.fail('pose looked up without --tilt-correction'))
            capsys.readouterr()
            counts = run(crop_runner, labels, store, out)
            printed = capsys.readouterr().out.replace(str(store), '<s>').replace(str(out), '<o>')
            crops = [open(crop_path(out, 1, i), 'rb').read() for i in (1, 2, 3)]
            results.append((counts, printed, crops))
        assert results[0] == results[1]
        counts, printed, _ = results[0]
        assert counts['no_pose'] == 0 and counts['success'] == 3
        assert '0 skipped for no pano pose' in printed
        assert 'Tilt correction' not in printed

    def test_the_window_is_the_stored_points_without_the_flag(self, crop_runner, tmp_path, windows):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_pano(store, 'testpano0001'))
        run(crop_runner, [label_row(pano_x=700, pano_y=400)], store, out)
        box = windows[0][0]
        expected = crop_runner.compute_crop_box(700, 400, crop_runner.crop_window_width(400, W, H), W, H)
        assert (box.left, box.top, box.width, box.height) == expected[:4]

    def test_the_marker_records_the_correction_off(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        run(crop_runner, [label_row()], store, out)
        marker = json.loads((out / crop_runner.CROP_RULE_MARKER).read_text(encoding='utf-8'))
        assert marker['tilt_correction'] == 'off'
        for key in crop_runner.TILT_BETA_MARKER_KEYS.values():
            assert marker[key] == 0.0
        assert crop_runner.build_parser().parse_args(
            ['--city', CITY, '-f', 'x.csv', '-s', 's', '-o', 'o']).tilt_correction is False


# ---------------------------------------------------------------------------
# Registration: the planted rig pixel is where the geometry says, in the cut window
# ---------------------------------------------------------------------------

REGISTRATION_POINTS = {
    'horizon': (1100, 512),
    'below-horizon': (600, 800),
    'near-top': (1000, 12),
    'near-bottom': (1500, 1010),
    'seam-left': (3, 520),
    'seam-right': (W - 4, 500),
}


class TestRegistration:
    @pytest.mark.parametrize('source', ['xml', 'npz'])
    @pytest.mark.parametrize('where', sorted(REGISTRATION_POINTS))
    def test_the_planted_rig_pixel_is_where_the_geometry_says(self, crop_runner, tmp_path, windows, source,
                                                             where):
        rig_x, rig_y = REGISTRATION_POINTS[where]
        store, out = tmp_path / 'store', tmp_path / 'crops'
        jpg = put_lossless_pano(store, 'testpano0001', planted_at=(rig_x, rig_y))
        write_pose(jpg, source)
        stored_x, stored_y = stored_point_for(rig_x, rig_y)
        counts = run(crop_runner, [label_row(pano_x=stored_x, pano_y=stored_y)], store, out,
                     tilt_correction=True)
        assert counts['success'] == 1

        (box, window), = windows
        px, py = crop_runner.label_position_in_crop(rig_x + 0.5, rig_y + 0.5, box, W)
        assert window.getpixel((int(px), int(py))) == PLANTED
        assert (1, PLANTED) in window.getcolors(maxcolors=1 << 24), \
            'the landmark must be unique in the window, or this assertion proves nothing'
        # And the window is centred on it - read off the raster, since the mapping above would also find
        # the pixel in a window that merely contains it (a window left at the stored point does).
        found_x, found_y = planted_position(window)
        assert abs(found_x + 0.5 - box.width / 2) <= 1
        if 0 < box.top < H - box.height:
            assert abs(found_y + 0.5 - box.height / 2) <= 1

    def test_the_correction_moved_the_window_off_the_stored_point(self, crop_runner, tmp_path, windows):
        """The fixture is only meaningful if the stored and the rig pixel are far enough apart to tell."""
        rig_x, rig_y = REGISTRATION_POINTS['below-horizon']
        stored_x, stored_y = stored_point_for(rig_x, rig_y)
        assert abs(stored_y - rig_y) > 10
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_lossless_pano(store, 'testpano0001', planted_at=(rig_x, rig_y)))
        run(crop_runner, [label_row(pano_x=stored_x, pano_y=stored_y)], store, out, tilt_correction=True)
        (box, _), = windows
        uncorrected = crop_runner.compute_crop_box(
            stored_x, stored_y, crop_runner.crop_window_width(stored_y, W, H), W, H)
        assert box.top != uncorrected.top

    def test_mark_label_dots_the_planted_pixel(self, crop_runner, tmp_path, windows):
        rig_x, rig_y = REGISTRATION_POINTS['below-horizon']
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_lossless_pano(store, 'testpano0001', planted_at=(rig_x, rig_y)))
        stored_x, stored_y = stored_point_for(rig_x, rig_y)
        run(crop_runner, [label_row(pano_x=stored_x, pano_y=stored_y)], store, out, tilt_correction=True,
            mark_label=True)
        (box, window), = windows
        mark_x, mark_y = crop_runner.label_position_in_crop(rig_x + 0.5, rig_y + 0.5, box, W)
        assert window.getpixel((int(mark_x), int(mark_y))) == PLANTED
        with Image.open(crop_path(out, 1, 1)) as stored:
            scale = stored.size[0] / box.width
            r, g, b = stored.convert('RGB').getpixel((int(mark_x * scale), int(mark_y * scale)))
        # The dot is (128, 0, 0) drawn over a blue pano; JPEG moves it a little, not to blue.
        assert r > 90 and b < 60, (r, g, b)


# ---------------------------------------------------------------------------
# Sizing stays at the stored y
# ---------------------------------------------------------------------------

class TestSizingStaysAtTheStoredY:
    @pytest.mark.parametrize('rule', ['v2', 'v3'])
    def test_the_corrected_window_is_as_wide_as_the_uncorrected_one(self, crop_runner, tmp_path, windows, rule):
        stored_x, stored_y = 1024.0, 620.0
        corrected_y = pano_pose.corrected_pixel(stored_x, stored_y, W, H, PITCH, ROLL, 1.0)[1]
        assert (round(crop_runner.crop_window_width(stored_y, W, H, rule))
                != round(crop_runner.crop_window_width(corrected_y, W, H, rule))), \
            'this fixture needs a y where sizing at the corrected point would give another width'
        for flag in (False, True):
            store, out = tmp_path / ('s%d' % flag), tmp_path / ('o%d' % flag)
            write_npz_pose(put_pano(store, 'testpano0001'))
            run(crop_runner, [label_row(pano_x=stored_x, pano_y=stored_y)], store, out, sizing_rule=rule,
                tilt_correction=flag)
        (off, _), (on, _) = windows
        assert on.width == off.width
        assert on.top != off.top


# ---------------------------------------------------------------------------
# no_pose
# ---------------------------------------------------------------------------

def no_pose_store(tmp_path, shape):
    store = tmp_path / 'store'
    jpg = put_pano(store, 'testpano0001')
    if shape == 'xml-incomplete':
        write_xml_pose(jpg, drop='tilt_pitch_deg')
        write_npz_pose(jpg)          # present, and must not be fallen through to
    elif shape == 'npz-nan':
        write_npz_pose(jpg, pitch_deg=float('nan'))
    return store


class TestNoPose:
    @pytest.mark.parametrize('shape, reason', [('absent', 'no .xml and no .depth.npz'),
                                               ('xml-incomplete', 'tilt_pitch_deg'),
                                               ('npz-nan', 'not finite')])
    def test_counted_logged_and_not_an_error(self, crop_runner, tmp_path, caplog, capsys, shape, reason):
        store, out = no_pose_store(tmp_path, shape), tmp_path / 'crops'
        with caplog.at_level(logging.WARNING):
            counts = run(crop_runner, [label_row(label_id=1), label_row(label_id=2)], store, out,
                         tilt_correction=True)
        assert counts['no_pose'] == 2 and counts['errors'] == 0 and counts['success'] == 0
        assert reconciles(counts)
        assert not os.path.exists(crop_path(out, 1, 1))
        lines = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith('Skipped') and 'no pose for the tilt correction (' in r.getMessage()]
        assert len(lines) == 1 and reason in lines[0], 'one crop.log line per pano, naming why'
        printed = capsys.readouterr().out
        summary = '2 labels were skipped because their pano has no pose for the tilt correction (no_pose)'
        assert summary in printed and summary in caplog.text
        assert '2 skipped for no pano pose' in printed

    def test_main_exits_zero(self, crop_runner, tmp_path):
        store, out = no_pose_store(tmp_path, 'absent'), tmp_path / 'crops'
        labels = tmp_path / 'labels.csv'
        with open(str(labels), 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['pano_id', 'pano_x', 'pano_y', 'label_type_id', 'label_id'])
            writer.writeheader()
            writer.writerow(label_row())
        assert crop_runner.main(['--city', CITY, '-f', str(labels), '-s', str(store), '-o', str(out),
                                 '--tilt-correction']) == 0
        marker = json.loads((out / CITY / crop_runner.CROP_RULE_MARKER).read_text(encoding='utf-8'))
        assert marker['tilt_correction'] == 'on'

    def test_under_force_a_crop_on_disk_is_stale_kept(self, crop_runner, tmp_path, capsys):
        store, out = no_pose_store(tmp_path, 'absent'), tmp_path / 'crops'
        run(crop_runner, [label_row()], store, out)
        before = open(crop_path(out, 1, 1), 'rb').read()
        capsys.readouterr()
        counts = run(crop_runner, [label_row(), label_row(label_id=2)], store, out, tilt_correction=True,
                     force=True)
        assert counts['no_pose'] == 2 and counts['stale_kept'] == 1 and reconciles(counts)
        assert open(crop_path(out, 1, 1), 'rb').read() == before
        printed = capsys.readouterr().out
        assert STALE_KEPT_SUMMARY % 1 in printed
        assert '1 of them were on a pano with no pose for the tilt correction (no_pose)' in printed

    def test_every_disjoint_outcome_once_and_the_key_set_exactly(self, crop_runner, tmp_path, monkeypatch):
        """The twin of test_crop_content's every-bucket test, with the flag on, so no_pose is reached too;
        the black_content label sits on a pano with a black band."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_pano(store, 'testpano0001'), pitch_deg=0.0, roll_deg=0.0)
        put_pano(store, 'noposepano01')
        band = put_pano(store, 'bandpano0001')
        img = Image.open(band).convert('RGB')
        img.paste((0, 0, 0), (0, 600, W, H))
        img.save(band, quality=95)
        write_npz_pose(band, pitch_deg=0.0, roll_deg=0.0)
        run(crop_runner, [label_row(label_id=9)], store, out, tilt_correction=True)

        labels = [label_row(label_id=1),                                          # success
                  label_row(label_id=9),                                          # skipped_existing
                  label_row(pano_id='gonepano0001', label_id=2),                  # missing_pano
                  dict(label_row(label_id=3), pano_width=4096, pano_height=2048),  # dims_mismatch
                  label_row(pano_y=5000, label_id=4),                             # out_of_frame
                  label_row(pano_id='bandpano0001', pano_y=900, label_id=5),      # black_content
                  label_row(pano_id='noposepano01', label_id=7),                  # no_pose
                  label_row(pano_x='not-a-number', label_id=6)]                   # errors
        counts = run(crop_runner, labels, store, out, tilt_correction=True)
        assert set(counts) == ({'total'} | set(crop_runner.DISJOINT_OUTCOMES)
                               | set(crop_runner.COUNT_ANNOTATIONS))
        assert 'no_pose' in crop_runner.DISJOINT_OUTCOMES and 'no_pose' not in crop_runner.COUNT_ANNOTATIONS
        for bucket in crop_runner.DISJOINT_OUTCOMES:
            assert counts[bucket] == 1, bucket
        assert counts['total'] == 8


# ---------------------------------------------------------------------------
# The out_of_frame preflight reads the corrected y
# ---------------------------------------------------------------------------

class TestTheFramePreflightReadsTheCorrectedY:
    """The exact rotation maps every direction to a y in [0, h] (a point past a pole comes back over it, at
    the opposite bearing), so a corrected y outside the image is reachable only at the nadir row. The
    preflight is pinned by standing in for the transform."""

    def test_a_stored_y_inside_whose_corrected_y_is_outside_is_out_of_frame(self, crop_runner, tmp_path,
                                                                            monkeypatch, caplog):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_pano(store, 'testpano0001'))
        monkeypatch.setattr(pano_pose, 'corrected_pixel', lambda x, y, w, h, p, r, beta: (x, float(h)))
        with caplog.at_level(logging.WARNING):
            counts = run(crop_runner, [label_row(pano_y=1000)], store, out, tilt_correction=True)
        assert counts['out_of_frame'] == 1 and counts['success'] == 0 and reconciles(counts)
        assert 'pano_y 1000.0 (tilt-corrected to 1024.0) is outside' in caplog.text

    def test_a_stored_y_outside_is_out_of_frame_whatever_the_correction_says(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_pano(store, 'testpano0001'))
        counts = run(crop_runner, [label_row(pano_y=-3)], store, out, tilt_correction=True)
        assert counts['out_of_frame'] == 1 and reconciles(counts)


# ---------------------------------------------------------------------------
# Beta per era
# ---------------------------------------------------------------------------

class TestBetaPerEra:
    def test_each_era_gets_its_own_beta(self, crop_runner, tmp_path, windows, monkeypatch):
        monkeypatch.setattr(crop_runner, 'TILT_BETA_BY_ERA', {'post179': 1.0, 'legacy+mid': 0.0, 'unknown': 0.5})
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_pano(store, 'testpano0001'))
        x, y = 1024.0, 700.0
        labels = [dict(label_row(pano_x=x, pano_y=y, label_id=1), time_created=BOUNDARY_MS),
                  dict(label_row(pano_x=x, pano_y=y, label_id=2), time_created=BOUNDARY_MS - 1),
                  label_row(pano_x=x, pano_y=y, label_id=3)]
        run(crop_runner, labels, store, out, tilt_correction=True)
        width = crop_runner.crop_window_width(y, W, H)
        for (box, _), beta in zip(windows, (1.0, 0.0, 0.5)):
            cx, cy = pano_pose.corrected_pixel(x, y, W, H, PITCH, ROLL, beta)
            assert box[:4] == crop_runner.compute_crop_box(cx, cy, width, W, H)[:4], beta

    def test_the_tally_line_with_a_time_created_column(self, crop_runner, tmp_path, capsys):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_pano(store, 'testpano0001'))
        path = tmp_path / 'labels.csv'
        with open(str(path), 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['pano_id', 'pano_x', 'pano_y', 'label_type_id', 'label_id',
                                                   'time_created'])
            writer.writeheader()
            for i, stamp in enumerate((str(BOUNDARY_MS), '2023-03-29T00:00:00Z', '1500000000000', ''), 1):
                writer.writerow(dict(label_row(label_id=i), time_created=stamp))
        crop_runner.run(None, str(path), str(store), str(out), city=CITY, tilt_correction=True)
        printed = capsys.readouterr().out
        assert ('Tilt correction on (recorded in crop_rule.json): 1 labels legacy+mid (beta 1.0), '
                '2 labels post179 (beta 1.0), 1 labels unknown (beta 1.0).') in printed
        assert 'No label carried a readable' not in printed

    def test_the_tally_line_without_one_says_every_label_is_unknown(self, crop_runner, tmp_path, capsys):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_pano(store, 'testpano0001'))
        run(crop_runner, [label_row(label_id=1), label_row(label_id=2)], store, out, tilt_correction=True)
        printed = capsys.readouterr().out
        assert '0 labels legacy+mid (beta 1.0), 0 labels post179 (beta 1.0), 2 labels unknown (beta 1.0).' in printed
        assert 'No label carried a readable time_created' in printed


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------

def read_marker(crop_runner, out):
    return json.loads((out / crop_runner.CROP_RULE_MARKER).read_text(encoding='utf-8'))


class TestTheMarker:
    def test_on_records_the_tables_values(self, crop_runner, tmp_path, monkeypatch):
        monkeypatch.setattr(crop_runner, 'TILT_BETA_BY_ERA', {'post179': 0.936, 'legacy+mid': 1.0, 'unknown': 0.9})
        crop_runner.write_rule_marker(str(tmp_path), tilt_correction=True)
        marker = read_marker(crop_runner, tmp_path)
        assert marker['tilt_correction'] == 'on'
        assert (marker['tilt_beta_post179'], marker['tilt_beta_legacy_mid'], marker['tilt_beta_unknown_era']) \
            == (0.936, 1.0, 0.9)
        recorded, unreadable = crop_runner._read_rule_marker(str(tmp_path / crop_runner.CROP_RULE_MARKER))
        assert not unreadable and recorded['tilt_correction'] == 'on'

    @pytest.mark.parametrize('rule', ['v2', 'v3'])
    def test_off_then_on_warns_on_both_channels_naming_the_key(self, crop_runner, tmp_path, caplog, capsys, rule):
        crop_runner.write_rule_marker(str(tmp_path), sizing_rule=rule)
        capsys.readouterr()
        with caplog.at_level(logging.WARNING):
            crop_runner.write_rule_marker(str(tmp_path), sizing_rule=rule, tilt_correction=True)
        printed = capsys.readouterr().out
        for channel in (printed, caplog.text):
            assert 'tilt_beta_post179=0.0 and this run uses 1.0' in channel
        seen = read_marker(crop_runner, tmp_path)['constants_seen'][rule]['tilt_beta_post179']
        assert seen == [0.0, 1.0]

    def test_the_history_survives_force(self, crop_runner, tmp_path, caplog):
        crop_runner.write_rule_marker(str(tmp_path))
        crop_runner.write_rule_marker(str(tmp_path), tilt_correction=True, force=True)
        with caplog.at_level(logging.WARNING):
            crop_runner.write_rule_marker(str(tmp_path), tilt_correction=True)
        assert 'tilt_beta_post179=0.0 and this run uses 1.0' in caplog.text

    def test_a_marker_from_before_the_keys_is_silent(self, crop_runner, tmp_path, caplog):
        crop_runner.write_rule_marker(str(tmp_path))
        path = tmp_path / crop_runner.CROP_RULE_MARKER
        marker = json.loads(path.read_text(encoding='utf-8'))
        for key in list(crop_runner.TILT_BETA_MARKER_KEYS.values()) + ['tilt_correction']:
            del marker[key]
        for keys in marker['constants_seen'].values():
            for key in crop_runner.TILT_BETA_MARKER_KEYS.values():
                keys.pop(key, None)
        path.write_text(json.dumps(marker), encoding='utf-8')
        with caplog.at_level(logging.WARNING):
            crop_runner.write_rule_marker(str(tmp_path), tilt_correction=True)
        assert 'this run uses' not in caplog.text
        assert read_marker(crop_runner, tmp_path)['tilt_correction'] == 'on'

    def test_a_non_string_tilt_correction_is_unreadable(self, crop_runner, tmp_path):
        crop_runner.write_rule_marker(str(tmp_path))
        path = tmp_path / crop_runner.CROP_RULE_MARKER
        marker = json.loads(path.read_text(encoding='utf-8'))
        marker['tilt_correction'] = ['on']
        path.write_text(json.dumps(marker), encoding='utf-8')
        assert crop_runner._read_rule_marker(str(path)) == ({}, True)

    def test_the_run_writes_the_marker_with_the_flag(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        write_npz_pose(put_pano(store, 'testpano0001'))
        run(crop_runner, [label_row()], store, out, tilt_correction=True)
        marker = read_marker(crop_runner, out)
        assert marker['tilt_correction'] == 'on' and marker['tilt_beta_unknown_era'] == 1.0
