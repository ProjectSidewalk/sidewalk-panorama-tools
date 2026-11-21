"""reports/2026-11-17-bayonne-panoramax-crop-check.md (#111): the study script's detectors, and the report
against its committed raw files.

Two halves, in the order .claude/rules/desk-studies.md asks for:

* **Code.** Each detector of reports/scripts/bayonne_crop_check.py on a synthetic store built here (small
  PIL-generated equirectangular images under UUID ids, cropped by the real `CropRunner.bulk_extract_crops`),
  with one planted fault per test and a clean store that reports nothing. The standalone v2 transcription
  against `CropRunner` on a grid, and a perturbed constant on either side. The visual-verdict rule on
  synthetic tallies, on both sides of each threshold.
* **The committed run.** The headline counts re-derived from the raw files (the run-1 stdout capture, the
  gzipped manifest and snapshot, `visual-verdicts.csv`), never through the summary JSON; every table row the
  script emitted appears in the report; no local path is committed.

Network-free. No imagery is committed: the synthetic store lives in `tmp_path`.
"""

import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import uuid
from collections import Counter

import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import CropRunner  # noqa: E402
import bayonne_crop_check as bcc  # noqa: E402

DATA = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-17-bayonne-panoramax-crop-check')
SUMMARY = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-17-bayonne-panoramax-crop-check.json')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-11-17-bayonne-panoramax-crop-check.md')

SOURCE_FIELD = CropRunner.PROVENANCE_FIELDS[0]
ATTRIBUTION_FIELDS = CropRunner.PROVENANCE_FIELDS[1:]


# ------------------------------------------------------------------------------------------ synthetic store

def _pano_id(n):
    """A deterministic 36-char UUID, the shape Panoramax ids have."""
    return str(uuid.UUID(int=n * 0x1000100010001 + 0x0a1b2c3d4e5f60718293a4b5c6d7e8f9))


def _label(label_id, pano_id, pano_x, pano_y, width=720, height=360, label_type='CurbRamp'):
    row = {'label_id': label_id, 'pano_id': pano_id, 'label_type': label_type, 'pano_x': pano_x,
           'pano_y': pano_y, 'pano_width': width, 'pano_height': height, SOURCE_FIELD: 'panoramax'}
    row.update({ATTRIBUTION_FIELDS[0]: 'producer_a', ATTRIBUTION_FIELDS[1]: 'attr-2.0'})
    return row


def _write_pano(store, pano_id, width=720, height=360):
    shard = os.path.join(store, pano_id[:2])
    os.makedirs(shard, exist_ok=True)
    # A gradient, so no window is mostly black and the content check never withholds one.
    image = Image.linear_gradient('L').resize((width, height)).convert('RGB')
    image = Image.eval(image, lambda v: 40 + v // 2)
    image.save(os.path.join(shard, pano_id + '.jpg'), quality=90)


def _build(tmp_path, n_panos=3):
    """A store, its ledger, the app's pano list and label list, and a crop store cut by the real cropper."""
    store = str(tmp_path / 'store')
    crops = str(tmp_path / 'crops' / bcc.CITY)
    panos, labels = [], []
    os.makedirs(store)
    with open(os.path.join(store, 'pano_id_log.csv'), 'w', newline='', encoding='utf-8') as f:
        f.write('pano_id,downloaded,fetched_at' + chr(10))      # the header the real ledger carries
        for i in range(n_panos):
            pano_id = _pano_id(i + 1)
            _write_pano(store, pano_id)
            f.write('%s,1,2026-11-17T00:00:00+00:00\n' % pano_id)
            panos.append({'pano_id': pano_id, 'has_labels': True, 'width': 720, 'height': 360,
                          'camera_pitch': 0, 'camera_roll': None, 'source': 'panoramax'})
            labels.append(_label(2 * i + 1, pano_id, 100 + 50 * i, 220))
            labels.append(_label(2 * i + 2, pano_id, 710, 200))      # crosses the seam
    counts = CropRunner.bulk_extract_crops([dict(r) for r in labels], store, crops, city=bcc.CITY)
    assert counts['success'] == len(labels), counts
    return store, crops, panos, labels


def _manifest_rows(crops):
    with open(os.path.join(crops, CropRunner.PROVENANCE_MANIFEST), newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


class TestTheCleanStoreReportsNothing:
    """Guards every detector below: on an unfaulted store each one is silent, so a hit is the plant's."""

    def test_store(self, tmp_path):
        store, _, panos, labels = _build(tmp_path)
        result = bcc.store_check(store, panos, labels)
        assert result['outside_shard'] == [] and result['frame_mismatch'] == []
        assert result['walk_equals_ledger'] and result['ledger_downloaded'] == 3
        assert result['ledger_verdicts'] == {'1': 3}, 'the header row is not a verdict'

    def test_windows(self, tmp_path):
        _, crops, _, labels = _build(tmp_path)
        rows = bcc.window_rows(labels, crops)
        assert len(rows) == len(labels)
        assert all(r['size_ok'] and r['transcription_agrees'] for r in rows)
        assert sum(r['crosses_seam'] for r in rows) == 3

    def test_manifest(self, tmp_path):
        _, crops, _, labels = _build(tmp_path)
        result = bcc.manifest_check(_manifest_rows(crops), labels, bcc.CITY)
        assert result['rows'] == len(labels) and result['unique'] and result['city_ok']
        assert result['attribution_mismatch'] == [] and result['attribution_equal'] == len(labels)
        assert result['attribution_pairs'] == 1
        assert result['not_in_manifest'] == [] and result['not_in_snapshot'] == []

    def test_dims(self):
        assert bcc.dims_coverage([_label(1, _pano_id(1), 1, 1)]) == {'usable': 1, 'total': 1, 'untested': []}


class TestEachDetectorCatchesItsPlant:

    @pytest.mark.parametrize('value', ['other_producer', ''])
    def test_a_manifest_row_whose_attribution_differs_from_or_lacks_the_served_value(self, tmp_path, value):
        """Mutant killed: `manifest_check` comparing only the source field, or skipping the empty value."""
        _, crops, _, labels = _build(tmp_path)
        rows = _manifest_rows(crops)
        rows[2][ATTRIBUTION_FIELDS[0]] = value
        result = bcc.manifest_check(rows, labels, bcc.CITY)
        assert result['attribution_mismatch'] == ['%s:%s' % (bcc.CITY, rows[2]['label_id'])]
        assert result['attribution_equal'] == len(labels) - 1

    def test_the_second_attribution_field_is_compared_too(self, tmp_path):
        """Mutant killed: comparing `ATTRIBUTION_FIELDS[:1]`."""
        _, crops, _, labels = _build(tmp_path)
        rows = _manifest_rows(crops)
        rows[0][ATTRIBUTION_FIELDS[1]] = 'other-1.0'
        assert bcc.manifest_check(rows, labels, bcc.CITY)['attribution_mismatch'] == [
            '%s:%s' % (bcc.CITY, rows[0]['label_id'])]

    def test_a_duplicated_manifest_row_is_not_unique(self, tmp_path):
        _, crops, _, labels = _build(tmp_path)
        rows = _manifest_rows(crops)
        result = bcc.manifest_check(rows + rows[:1], labels, bcc.CITY)
        assert not result['unique']

    def test_a_crop_of_the_wrong_size(self, tmp_path):
        """Mutant killed: `size_ok` computed from the window rather than read off the file."""
        _, crops, _, labels = _build(tmp_path)
        victim = labels[3]
        path = os.path.join(crops, '1', '%d.jpg' % victim['label_id'])
        with Image.open(path) as im:
            im.load()
            smaller = im.resize((im.width - 2, im.height))
        smaller.save(path)
        rows = {r['label_uid']: r for r in bcc.window_rows(labels, crops)}
        assert not rows['%s:%d' % (bcc.CITY, victim['label_id'])]['size_ok']
        assert sum(not r['size_ok'] for r in rows.values()) == 1

    def test_a_missing_crop_is_not_ok(self, tmp_path):
        _, crops, _, labels = _build(tmp_path)
        os.remove(os.path.join(crops, '1', '%d.jpg' % labels[0]['label_id']))
        rows = bcc.window_rows(labels, crops)
        assert [r['size_ok'] for r in rows].count(False) == 1 and rows[0]['file_w'] is None

    def test_a_stored_frame_differing_from_the_app_frame(self, tmp_path):
        """Mutant killed: comparing the app's two frames with each other and not with the file's header."""
        store, _, panos, labels = _build(tmp_path)
        _write_pano(store, panos[1]['pano_id'], width=700, height=350)
        result = bcc.store_check(store, panos, labels)
        assert result['frame_mismatch'] == [panos[1]['pano_id']]

    def test_the_label_frame_is_compared_too(self, tmp_path):
        """Mutant killed: comparing the file with /adminapi/panos only, never with cvMetadata's frame."""
        store, _, panos, labels = _build(tmp_path)
        for row in labels:
            if row['pano_id'] == panos[0]['pano_id']:
                row['pano_width'], row['pano_height'] = 700, 350
        assert bcc.store_check(store, panos, labels)['frame_mismatch'] == [panos[0]['pano_id']]

    def test_a_pano_outside_its_shard(self, tmp_path):
        """Mutant killed: listing `walk_store_panos` alone, which skips a misplaced file by design."""
        store, _, panos, labels = _build(tmp_path)
        pano_id = panos[2]['pano_id']
        wrong = os.path.join(store, 'zz')
        os.makedirs(wrong)
        os.replace(os.path.join(store, pano_id[:2], pano_id + '.jpg'), os.path.join(wrong, pano_id + '.jpg'))
        result = bcc.store_check(store, panos, labels)
        assert result['outside_shard'] == ['zz/%s.jpg' % pano_id]
        assert not result['walk_equals_ledger']

    def test_a_labelled_pano_the_ledger_wrote_off_is_not_downloaded(self, tmp_path):
        """Mutant killed: every ledger verdict counted as downloaded (`downloaded = set(ledger)`), which would
        make the report's "labelled panos not downloaded | 0" true whatever the ledger said."""
        store, _, panos, labels = _build(tmp_path)
        victim = panos[1]['pano_id']
        with open(os.path.join(store, 'pano_id_log.csv'), 'a', newline='', encoding='utf-8') as f:
            f.write('%s,0,2026-11-18T00:00:00+00:00\n' % victim)      # a later row wins
        result = bcc.store_check(store, panos, labels)
        assert result['labelled_not_downloaded'] == [victim]
        assert result['ledger_downloaded'] == 2 and result['ledger_verdicts'] == {'1': 2, '0': 1}
        assert not result['walk_equals_ledger'], 'the file is still on disk, the ledger says 0'

    def test_a_served_label_without_a_manifest_row_and_a_row_for_no_served_label(self, tmp_path):
        """Mutants killed: `not_in_manifest` or `not_in_snapshot` always empty (M9's drift rows read 0)."""
        _, crops, _, labels = _build(tmp_path)
        rows = _manifest_rows(crops)
        dropped = rows.pop(1)
        extra = dict(rows[0], label_id='99')
        result = bcc.manifest_check(rows + [extra], labels, bcc.CITY)
        assert result['not_in_manifest'] == ['%s:%s' % (bcc.CITY, dropped['label_id'])]
        assert result['not_in_snapshot'] == ['%s:99' % bcc.CITY]

    def test_a_window_wider_than_the_storage_cap_expects_the_downscaled_file(self, tmp_path):
        """Mutant killed: `expected_stored_size` never expecting a downscale. Bayonne never takes this branch
        (its widest window is exactly the cap), so a 7680-wide synthetic pano does, cut by the real cropper."""
        store, crops = str(tmp_path / 'store'), str(tmp_path / 'crops' / bcc.CITY)
        width, height = 7680, 3840
        pano_id = _pano_id(9)
        _write_pano(store, pano_id, width, height)
        label = _label(1, pano_id, 100, int(0.9 * height), width, height)
        counts = CropRunner.bulk_extract_crops([dict(label)], store, crops, city=bcc.CITY)
        assert counts['success'] == 1, counts
        [row] = bcc.window_rows([label], crops)
        assert row['downscaled'] and row['width'] > CropRunner.CROP_MAX_STORED_WIDTH
        assert row['expected_w'] == CropRunner.CROP_MAX_STORED_WIDTH < row['width']
        assert (row['file_w'], row['file_h']) == (row['expected_w'], row['expected_h']) and row['size_ok']

    def test_the_seam_flag_is_exactly_the_windows_that_wrap(self, tmp_path):
        """Mutant killed: a seam test off by a window width or by one. Swept across both edges, with the
        window that ends on the last column (no wrap) among them; the truth is the window's own columns."""
        width, height = 720, 360
        xs = list(range(0, 40)) + list(range(560, 720))
        labels = [_label(i, _pano_id(1), x, 200, width, height) for i, x in enumerate(xs, start=1)]
        rows = bcc.window_rows(labels, str(tmp_path / 'no-crops'))
        wraps = [(r['left'] + r['width'] - 1) % width < r['left'] for r in rows]
        assert [r['crosses_seam'] for r in rows] == wraps
        assert any(wraps) and not all(wraps)
        assert any(r['left'] + r['width'] == width for r in rows), 'the boundary case is in the sweep'

    def test_a_row_with_no_usable_dims_is_counted_untested(self):
        """Mutant killed: counting every row as usable, which makes dims_mismatch 0 vacuous for it."""
        rows = [_label(1, _pano_id(1), 1, 1), _label(2, _pano_id(1), 1, 1, width=None, height=None),
                _label(3, _pano_id(2), 1, 1, width=0, height=0)]
        assert bcc.dims_coverage(rows) == {'usable': 1, 'total': 3,
                                           'untested': ['bayonne-fr:2', 'bayonne-fr:3']}


def _band_image(width, height, white_from=None, sky_rows=0, blob=False, row_white_share=1.0):
    """A mid-grey RGB image with optional white rows: `sky_rows` at the top and every row from `white_from` to the
    bottom. `blob` paints a dark square inside the band; `row_white_share` < 1 leaves that share of each band
    row's pixels white and the rest grey."""
    image = Image.new('RGB', (width, height), (120, 120, 120))
    if sky_rows:
        image.paste((255, 255, 255), (0, 0, width, sky_rows))
    if white_from is not None:
        image.paste((255, 255, 255), (0, white_from, int(round(width * row_white_share)), height))
        if blob:
            image.paste((30, 30, 30), (10, white_from + 2, 10 + (height - white_from) // 2, height - 2))
    return image


class TestTheWhiteBand:
    """Review of PR 251, finding 2: every stored Bayonne pano is flat white from about 0.79 of its height down,
    and 11 crops carry some of it. `black_content` looks for black and cannot see it, so the study measures it."""

    def test_a_white_row_is_one_with_at_least_the_fraction_of_its_pixels_above_the_luma(self):
        flags = bcc.white_row_flags(_band_image(100, 50, white_from=40))
        assert flags.sum() == 10 and flags[40:].all() and not flags[:40].any()
        assert bcc.white_row_flags(_band_image(100, 50, white_from=40, row_white_share=0.98)).sum() == 10
        assert bcc.white_row_flags(_band_image(100, 50, white_from=40, row_white_share=0.97)).sum() == 0
        at_the_luma = Image.new('RGB', (100, 10), (bcc.WHITE_LUMA,) * 3)
        assert not bcc.white_row_flags(at_the_luma).any(), 'strictly above the luma'
        above = Image.new('RGB', (100, 10), (bcc.WHITE_LUMA + 1,) * 3)
        assert bcc.white_row_flags(above).all()

    def test_the_band_starts_at_the_first_white_row_in_the_lower_half(self):
        """A white sky at the top is not the band: six of the 40 Bayonne panos have one."""
        top, share = bcc.white_band(_band_image(200, 100, white_from=79, sky_rows=12))
        assert top == 79 and share == pytest.approx(1.0)
        assert bcc.white_band(_band_image(200, 100, white_from=79, row_white_share=0.98))[0] == 79
        assert bcc.white_band(_band_image(200, 100, white_from=79, row_white_share=0.97)) == (None, None)
        assert bcc.white_band(_band_image(200, 100, sky_rows=12)) == (None, None)
        assert bcc.white_band(_band_image(200, 100)) == (None, None)

    def test_the_band_may_start_anywhere_in_the_lower_half(self):
        """Review 2, N2: a band from just below the middle is found there, not at a later row, and white rows
        that end just above the middle are sky."""
        assert bcc.white_band(_band_image(200, 100, white_from=52))[0] == 52
        assert bcc.white_band(_band_image(200, 100, sky_rows=50)) == (None, None)

    def test_the_band_uses_the_same_luma_line_as_the_rows(self):
        """Review 2, N2: a band at exactly WHITE_LUMA is not white, one grey level above it is, and the share
        counts the same way."""
        def band(level):
            image = _band_image(200, 100, white_from=70)
            image.paste((level,) * 3, (0, 70, 200, 100))
            return image
        assert bcc.white_band(band(bcc.WHITE_LUMA)) == (None, None)
        top, share = bcc.white_band(band(bcc.WHITE_LUMA + 1))
        assert top == 70 and share == pytest.approx(1.0)
        mixed = band(bcc.WHITE_LUMA + 1)
        mixed.paste((bcc.WHITE_LUMA,) * 3, (0, 90, 2, 100))     # 2 of 200 columns, 1%: rows stay white
        assert bcc.white_band(mixed) == (70, pytest.approx(1 - 20 / (30 * 200)))

    def test_the_band_share_counts_its_pixels_from_the_top_row_down(self):
        image = _band_image(200, 100, white_from=60, blob=True)
        luma = image.convert('L')
        expected = sum(1 for y in range(60, 100) for x in range(200) if luma.getpixel((x, y)) > bcc.WHITE_LUMA)
        top, share = bcc.white_band(image)
        assert top == 60 and share == pytest.approx(expected / (40 * 200), abs=1e-4) and share < 0.95

    def test_the_store_and_crop_tables_carry_it(self, tmp_path):
        """A pano white from 0.8 of its height: its row says where the band starts, and a crop near the nadir
        counts white rows while one at the horizon counts none. Cut by the real cropper."""
        store, crops = str(tmp_path / 'store'), str(tmp_path / 'crops' / bcc.CITY)
        width, height = 720, 360
        pano_id = _pano_id(5)
        os.makedirs(os.path.join(store, pano_id[:2]))
        image = _band_image(width, height, white_from=288)
        image.paste((30, 30, 30), (400, 300, 410, 310))     # 1.4% of a row: the rows stay white, the share < 1
        image.save(os.path.join(store, pano_id[:2], pano_id + '.jpg'), quality=95)
        panos = [{'pano_id': pano_id, 'has_labels': True, 'width': width, 'height': height}]
        labels = [_label(1, pano_id, 300, 180, width, height), _label(2, pano_id, 300, 300, width, height)]
        assert CropRunner.bulk_extract_crops([dict(r) for r in labels], store, crops, city=bcc.CITY)['success'] == 2
        [pano] = bcc.store_check(store, panos, labels)['rows']
        assert abs(pano['white_band_top'] - 288) <= 1 and pano['white_band_share'] > 0.99
        with Image.open(os.path.join(store, pano_id[:2], pano_id + '.jpg')) as im:
            assert pano['white_band_share'] == round(bcc.white_band(im)[1], 4), 'four places, as panos.csv has'
        rows = {r['label_id']: r for r in bcc.window_rows(labels, crops)}
        assert rows[1]['white_rows'] == 0
        band_in_window = rows[2]['top'] + rows[2]['height'] - 288
        assert band_in_window > 0 and abs(rows[2]['white_rows'] - band_in_window) <= 2


class TestTheStandaloneTranscription:

    GRID = [(y_frac, h) for h in (1664, 2688, 2880, 3328, 6656, 8192) for y_frac in
            (0.0, 0.01, 0.2, 0.45, 0.5, 0.52, 0.55, 0.6, 0.7, 0.8, 0.9, 0.97, 0.999)]

    @pytest.mark.parametrize('y_frac, h', GRID)
    @pytest.mark.parametrize('x_frac', [0.0, 0.003, 0.5, 0.998])
    def test_it_agrees_with_the_cropper_on_a_grid(self, y_frac, h, x_frac):
        w = 2 * h
        x, y = int(x_frac * w), int(y_frac * h)
        box = CropRunner.compute_crop_box(x, y, CropRunner.crop_window_width(y, w, h), w, h)
        assert bcc.standalone_v2_window(x, y, w, h) == tuple(box)

    def test_it_does_not_import_the_rule(self):
        """Its point is to be a second derivation; reading the constants back from CropRunner would make
        the agreement above true by construction."""
        import inspect
        source = inspect.getsource(bcc.standalone_v2_window)
        assert 'CropRunner' not in source

    def _labels(self):
        return [_label(i, _pano_id(1), 37 * i, int(2880 * f), 5760, 2880)
                for i, f in enumerate((0.5, 0.55, 0.6, 0.65, 0.7), start=1)]

    def test_a_perturbed_cropper_constant_is_reported(self, monkeypatch):
        assert bcc.transcription_disagreements(self._labels()) == []
        monkeypatch.setattr(CropRunner, 'CROP_SIZE_SCALE', 2.4)
        assert len(bcc.transcription_disagreements(self._labels())) >= 3

    def test_a_perturbed_transcription_constant_is_reported(self, monkeypatch):
        monkeypatch.setattr(bcc, 'V2_SIZE_EXP', -1.19)
        assert len(bcc.transcription_disagreements(self._labels())) >= 3


def _verdicts(spec):
    """[(verdict, direction, pose_class, n), ...] -> verdict rows."""
    rows = []
    for verdict, direction, pose, n in spec:
        rows.extend({'label_uid': 'bayonne-fr:%d' % (len(rows) + i), 'verdict': verdict,
                     'direction': direction, 'pose_class': pose} for i in range(n))
    return rows


class TestTheVerdictRule:
    """Section 2.6 of the plan, fixed before the run, as a pure function."""

    def test_all_on_mark_passes(self):
        assert bcc.visual_verdict(_verdicts([('on-mark', '', 'none', 20)]))['outcome'] == 'PASS'

    def test_under_20_judgeable_is_inconclusive(self):
        rows = _verdicts([('on-mark', '', 'none', 19), ('unclear', '', 'none', 30)])
        assert bcc.visual_verdict(rows)['outcome'] == 'inconclusive'
        assert bcc.visual_verdict(_verdicts([('on-mark', '', 'none', 20), ('unclear', '', 'zero', 30)]))[
            'outcome'] == 'PASS'

    def test_no_crop_on_mark_fails(self):
        rows = _verdicts([('in-crop-off-mark', d, 'none', 5) for d in ('up', 'down', 'left', 'right')])
        assert bcc.visual_verdict(rows)['outcome'] == 'FAIL'
        rows = _verdicts([('in-crop-off-mark', d, 'none', 5) for d in ('up', 'down', 'left')] +
                         [('on-mark', '', 'none', 5)])
        assert bcc.visual_verdict(rows)['outcome'] == 'PASS'

    @pytest.mark.parametrize('not_visible, outcome', [(10, 'FAIL'), (9, 'PASS')])
    def test_not_visible_at_half_the_judgeable_fails(self, not_visible, outcome):
        rows = _verdicts([('not-visible', '', 'none', not_visible), ('on-mark', '', 'none', 20 - not_visible),
                          ('unclear', '', 'none', 15)])
        assert bcc.visual_verdict(rows)['outcome'] == outcome

    @pytest.mark.parametrize('down, up, outcome', [(8, 2, 'FAIL'), (7, 3, 'PASS'), (15, 5, 'FAIL')])
    def test_a_consistent_direction_on_unposed_panos_fails(self, down, up, outcome):
        """Three quarters is the line: 8 of 10 and 15 of 20 cross it, 7 of 10 does not."""
        n_off = down + up
        rows = _verdicts([('in-crop-off-mark', 'down', 'zero', down), ('in-crop-off-mark', 'up', 'none', up),
                          ('on-mark', '', 'none', max(n_off, 10))])
        assert bcc.visual_verdict(rows)['outcome'] == outcome

    @pytest.mark.parametrize('down_unposed, down_posed, up_posed, up_unposed, outcome', [
        (7, 0, 3, 0, 'PASS'), (8, 0, 2, 0, 'FAIL'), (5, 3, 0, 2, 'PASS'), (6, 3, 0, 1, 'FAIL')])
    def test_each_three_quarter_clause_is_its_own_line(self, down_unposed, down_posed, up_posed, up_unposed,
                                                       outcome):
        """7 of 10 down overall is under the line even when every unposed off-mark is down; 8 of 10 overall is
        not enough when the unposed ones are only 5 of 7 down."""
        rows = _verdicts([('in-crop-off-mark', 'down', 'none', down_unposed),
                          ('in-crop-off-mark', 'down', 'posed', down_posed),
                          ('in-crop-off-mark', 'up', 'posed', up_posed), ('in-crop-off-mark', 'up', 'zero', up_unposed),
                          ('on-mark', '', 'none', 10)])
        assert bcc.visual_verdict(rows)['outcome'] == outcome

    def test_under_half_off_mark_passes_whatever_the_direction(self):
        rows = _verdicts([('in-crop-off-mark', 'down', 'none', 9), ('on-mark', '', 'none', 11)])
        assert bcc.visual_verdict(rows)['outcome'] == 'PASS'
        rows = _verdicts([('in-crop-off-mark', 'down', 'none', 10), ('on-mark', '', 'none', 10)])
        assert bcc.visual_verdict(rows)['outcome'] == 'FAIL'

    def test_a_consistent_direction_only_on_posed_panos_is_a_finding_not_a_failure(self):
        """A tilt leak explains an offset on posed panos; it cannot explain one on panos with no stored tilt."""
        rows = _verdicts([('in-crop-off-mark', 'down', 'posed', 12), ('on-mark', '', 'none', 8)])
        result = bcc.visual_verdict(rows)
        assert result['outcome'] == 'PASS'
        assert result['by_pose_class']['posed']['in-crop-off-mark'] == 12

    def test_unposed_off_marks_in_another_direction_do_not_fail(self):
        rows = _verdicts([('in-crop-off-mark', 'down', 'posed', 12), ('in-crop-off-mark', 'left', 'none', 3),
                          ('on-mark', '', 'none', 15)])
        assert bcc.visual_verdict(rows)['outcome'] == 'PASS'

    def test_an_unknown_verdict_is_refused(self):
        with pytest.raises(ValueError):
            bcc.visual_verdict(_verdicts([('maybe', '', 'none', 1)]))

    @pytest.mark.parametrize('direction', ['', None, 'sideways'])
    def test_an_off_mark_verdict_without_a_direction_is_refused(self, direction):
        """Mutant killed: accepting an off-mark row with no direction, which the direction clause then cannot
        count (the row would vanish from every direction tally and still count as off-mark)."""
        rows = _verdicts([('on-mark', '', 'none', 20)]) + [
            {'label_uid': 'bayonne-fr:999', 'verdict': 'in-crop-off-mark', 'direction': direction,
             'pose_class': 'none'}]
        with pytest.raises(ValueError, match='direction'):
            bcc.visual_verdict(rows)


class TestTheSmallReaders:

    LINE = ('81 crops extracted, 0 already existed, 0 skipped because the panorama image was missing, 0 skipped '
            'on a metadata/image dimension mismatch, 0 skipped for a label position outside the image, 0 withheld '
            'for a mostly black window, 0 skipped for no pano pose, 0 errors, of 81 labels total.')

    def test_the_summary_line_parses(self):
        counts = bcc.parse_crop_summary('noise\n' + self.LINE + '\nexit=0\n')
        assert counts == {'success': 81, 'skipped_existing': 0, 'missing_pano': 0, 'dims_mismatch': 0,
                          'out_of_frame': 0, 'black_content': 0, 'no_pose': 0, 'errors': 0, 'total': 81}
        assert bcc.parse_exit('noise\n' + self.LINE + '\nexit=0\n') == 0
        assert set(counts) - {'total'} == set(CropRunner.DISJOINT_OUTCOMES)

    def test_the_last_summary_and_exit_lines_are_read(self):
        """Mutant killed: reading the first summary line of a capture that holds two."""
        resumed = self.LINE.replace('81 crops extracted, 0 already existed', '0 crops extracted, 81 already existed')
        text = self.LINE + '\nexit=1\n' + resumed + '\nexit=0\n'
        counts = bcc.parse_crop_summary(text)
        assert (counts['success'], counts['skipped_existing']) == (0, 81)
        assert bcc.parse_exit(text) == 0

    def test_a_capture_with_no_summary_is_refused(self):
        with pytest.raises(ValueError):
            bcc.parse_crop_summary('Finished.\n')
        with pytest.raises(ValueError):
            bcc.parse_exit('Finished.\n')

    def test_scrub_replaces_both_slash_forms_longest_first(self):
        text = r'C:\Users\x\scratch\b\store\bayonne-fr\ab\x.jpg and C:/Users/x/scratch/b/crops/y'
        out = bcc.scrub(text, [('C:/Users/x/scratch/b', '<scratch>'),
                               ('C:/Users/x/scratch/b/store/bayonne-fr', '<store>')])
        assert out == '<store>' + chr(92) + 'ab' + chr(92) + 'x.jpg and <scratch>/crops/y'

    @pytest.mark.parametrize('pano_id, expected', [
        (_pano_id(1), True), ('7c4c4c2f-8d96-432f-ad89-00fbdbb769f0', True),
        ('-' * 36, False), ('0' * 36, False), ('7c4c4c2f8d96432fad8900fbdbb769f0', False),
        ('7c4c4c2f-8d96-432f-ad89-00fbdbb769f', False), ('7c4c4c2f-8d96-432f-ad89-00fbdbb769f0a', False),
        ('7c4c4c2f-8d96-432f-ad8900-fbdbb769f0', False), ('7C4C4C2F-8D96-432F-AD89-00FBDBB769F0', False),
        ('ARy-clYtgyvpxmBabOJQ0g', False)])
    def test_a_uuid_is_the_8_4_4_4_12_shape(self, pano_id, expected):
        """Review of PR 251, nit 5: `[0-9a-f-]{36}` also matched 36 hyphens. Lower case only, as Panoramax and the
        store write them."""
        assert bcc.is_uuid(pano_id) is expected

    def test_pose_classes_follow_the_levelling_report(self):
        panos = [{'pano_id': 'a', 'camera_pitch': 3.1, 'camera_roll': -1.0},
                 {'pano_id': 'b', 'camera_pitch': 0, 'camera_roll': 0},
                 {'pano_id': 'c', 'camera_pitch': 0, 'camera_roll': None},
                 {'pano_id': 'd', 'camera_pitch': 0}]
        assert bcc.pose_classes(panos) == {'a': 'posed', 'b': 'zero', 'c': 'none', 'd': 'none'}

    def test_pov_residuals_recover_a_planted_per_zoom_gain(self):
        rows = []
        for i, f in enumerate((-0.4, -0.2, 0.0, 0.1, 0.3)):
            for zoom, gain in ((1, 115.0), (2, 58.0)):
                heading, camera_heading = 30.0 + i, 250.0
                azimuth = heading - camera_heading + 180 + gain * f
                rows.append({'zoom': zoom, 'heading': heading, 'camera_heading': camera_heading,
                             'canvas_x': 360 + f * 720, 'canvas_width': 720,
                             'pano_x': (azimuth % 360) / 360 * 5760, 'pano_width': 5760})
        result = bcc.pov_residuals(rows)
        assert result['1']['gain_deg_per_canvas'] == pytest.approx(115.0, abs=0.01)
        assert result['2']['gain_deg_per_canvas'] == pytest.approx(58.0, abs=0.01)
        assert result['1']['fit_residual_abs_deg']['max'] == pytest.approx(0.0, abs=1e-6)
        assert result['1']['n'] == 5
        assert result['1']['raw_abs_deg']['median'] == pytest.approx(23.0)   # |115 f| over the five f

    def test_a_fractional_zoom_joins_its_nearest_bin_at_that_bins_scale(self):
        """cvMetadata's zoom is a float (0.998, 1.07, 1.99 in Bayonne). The view halves per zoom step, so a row
        at zoom z' is put on its nearest integer zoom z with its offset scaled by 2^-(z'-z); truncating put
        0.998 in a bin of its own and 1.99 in bin 1."""
        rows = []
        for i, (zoom, f) in enumerate(((0.998, -0.3), (1.0, 0.2), (1.07, 0.35), (1.99, -0.25), (2.0, 0.1))):
            azimuth = 10.0 + i - 200.0 + 180 + 115.0 * 2 ** -(zoom - 1) * f
            rows.append({'zoom': zoom, 'heading': 10.0 + i, 'camera_heading': 200.0, 'canvas_x': 360 + f * 720,
                         'canvas_width': 720, 'pano_x': (azimuth % 360) / 360 * 5376, 'pano_width': 5376})
        result = bcc.pov_residuals(rows)
        assert sorted(result) == ['1', '2'] and (result['1']['n'], result['2']['n']) == (3, 2)
        assert result['1']['gain_deg_per_canvas'] == pytest.approx(115.0)
        assert result['2']['gain_deg_per_canvas'] == pytest.approx(57.5)
        assert result['1']['fit_residual_abs_deg']['max'] == pytest.approx(0.0, abs=1e-6)


def _summary_line(**counts):
    """CropRunner's summary line, as the captures carry it, with the given counts (the rest 0)."""
    c = dict.fromkeys(bcc.SUMMARY_KEYS, 0)
    c.update(counts)
    return ('%(success)d crops extracted, %(skipped_existing)d already existed, %(missing_pano)d skipped because the '
            'panorama image was missing, %(dims_mismatch)d skipped on a metadata/image dimension mismatch, '
            '%(out_of_frame)d skipped for a label position outside the image, %(black_content)d withheld for a '
            'mostly black window, %(no_pose)d skipped for no pano pose, %(errors)d errors, of %(total)d labels '
            'total.' % c)


def _measure_setup(tmp_path, monkeypatch):
    """A whole synthetic run for `measure --write`: the snapshot, the store, run 1's crop store, the five
    captures and run 1's manifest copy, all under tmp_path. Returns (argv, snapshot dir, summary path, labels)."""
    store, crops, panos, labels = _build(tmp_path)
    snap = tmp_path / 'snap'
    snap.mkdir()
    bodies = {'panos.json': json.dumps(panos).encode('utf-8'),
              'cvmetadata.json': json.dumps(labels).encode('utf-8')}
    for name, body in bodies.items():
        (snap / (name + '.gz')).write_bytes(gzip.compress(body, mtime=0))
    (snap / 'snapshot.json').write_text(json.dumps({
        'taken_at_utc': '2026-11-17T00:00:00+00:00', 'host': 'example.invalid', 'city': bcc.CITY, 'counts': {},
        'platform': {}, 'sha256': {k: hashlib.sha256(v).hexdigest() for k, v in bodies.items()}}), encoding='utf-8')
    with open(os.path.join(store, 'log.csv'), 'w', encoding='utf-8') as f:
        f.write('synthetic' + chr(10))
    runs = tmp_path / 'out'
    runs.mkdir()
    n = len(labels)
    for name, counts in (('download.stdout.txt', None), ('crop.stdout.txt', {'success': n}),
                         ('crop-rerun.stdout.txt', {'skipped_existing': n}), ('crop-tilt.stdout.txt', {'no_pose': n}),
                         ('crop-marked.stdout.txt', {'success': n})):
        body = 'Finished.' if counts is None else _summary_line(total=n, **counts)
        (runs / name).write_text(body + chr(10) + 'exit=0' + chr(10), encoding='utf-8')
    with open(os.path.join(crops, CropRunner.PROVENANCE_MANIFEST), 'rb') as f:
        (runs / bcc.RUN1_MANIFEST_COPY).write_bytes(f.read())
    summary_path = str(tmp_path / 'summary.json')
    monkeypatch.setattr(bcc, 'SUMMARY_PATH', summary_path)
    argv = ['measure', '--snapshot', str(snap), '--store', store, '--crops', crops,
            '--tilt-crops', str(tmp_path / 'crops-tilt' / bcc.CITY), '--marked-crops', crops, '--runs', str(runs),
            '--scratch', str(tmp_path), '--write']
    return argv, snap, summary_path, labels


class TestMeasureEndToEnd:
    """`measure --write` over a synthetic run: the table rows the report carries come out of the stores and the
    captures, so a mutant anywhere between a detector and `table_rows` changes a row here."""

    def test_the_table_rows_come_from_the_stores_and_the_captures(self, tmp_path, monkeypatch):
        argv, snap, summary_path, labels = _measure_setup(tmp_path, monkeypatch)
        assert bcc.main(argv) == 0
        with open(summary_path, encoding='utf-8') as f:
            rows = json.load(f)['table_rows']
        n = len(labels)
        for row in ('| windows crossing the seam | 3 |', '| `success` | %d of %d |' % (n, n),
                    '| run 2 `skipped_existing` | %d of %d |' % (n, n), '| tilt run `no_pose` | %d of %d |' % (n, n),
                    '| marked run `success` | %d of %d |' % (n, n), '| manifest bytes unchanged by run 2 | yes |',
                    "| labels in run 1's manifest not in the snapshot / the reverse | 0 / 0 |",
                    '| labelled panos not downloaded | 0 |', '| labelled pano ids that are 36-char UUIDs | 3 of 3 |',
                    '| labelled panos with a white band in the lower half | 0 of 3 |',
                    '| crops with white rows | 0 of %d |' % n, '| most white rows in one crop | 0 |'):
            assert row in rows, row

    def test_the_white_band_rows_come_from_the_pixels(self, tmp_path, monkeypatch):
        """One pano repainted white from 2/3 of its height, then re-cut: its first label's window (45.8 deg, rows
        190-250) takes the band's top rows, and a label near its nadir (at the 90 deg cap) takes a whole window."""
        argv, _, summary_path, labels = _measure_setup(tmp_path, monkeypatch)
        store, crops = argv[argv.index('--store') + 1], argv[argv.index('--crops') + 1]
        first = labels[0]
        pano_id = first['pano_id']
        _band_image(720, 360, white_from=240).save(os.path.join(store, pano_id[:2], pano_id + '.jpg'), quality=95)
        nadir = _label(99, pano_id, 300, 330)
        cvmetadata = os.path.join(argv[argv.index('--snapshot') + 1], 'cvmetadata.json.gz')
        with open(cvmetadata, 'wb') as f:
            f.write(gzip.compress(json.dumps(labels + [nadir]).encode('utf-8'), mtime=0))
        os.remove(os.path.join(crops, '1', '%d.jpg' % first['label_id']))
        assert CropRunner.bulk_extract_crops([dict(first), dict(nadir)], store, crops, city=bcc.CITY)['success'] == 2
        assert CropRunner.crop_window_fov_deg(float(first['pano_y']), 360) < CropRunner.CROP_MAX_FOV_DEG
        assert CropRunner.crop_window_fov_deg(330.0, 360) >= CropRunner.CROP_MAX_FOV_DEG
        # A second banded pano, lower and less white, so each min / max row has two different ends (review 2, N2).
        second = labels[2]['pano_id']
        _band_image(720, 360, white_from=270, blob=True).save(os.path.join(store, second[:2], second + '.jpg'),
                                                               quality=95)
        bcc.main(argv)
        with open(summary_path, encoding='utf-8') as f:
            rows = json.load(f)['table_rows']
        [tops] = [r for r in rows if r.startswith('| band top as a share of the frame height, min / max |')]
        assert tops == '| band top as a share of the frame height, min / max | 0.667 / 0.750 |'
        [shares] = [r for r in rows if r.startswith('| white pixels from the band top to the last row, min / max |')]
        low, high = (float(v) for v in shares.split('|')[2].split('/'))
        assert low < 0.97 and high > 0.99
        for row in ('| labelled panos with a white band in the lower half | 2 of 3 |',
                    '| crops with white rows | 2 of %d |' % (len(labels) + 1),
                    '| of those, windows at the 90 deg cap | 1 |'):
            assert row in rows, row
        assert any(r.startswith('| most white rows in one crop |') and '`bayonne-fr:99`' in r for r in rows)

    def test_the_run_1_manifest_hash_is_recorded_beside_the_stores(self, tmp_path, monkeypatch):
        """`manifest bytes unchanged by run 2` was a yes/no only the scratch root could check; snapshot.json now
        carries the sha256 of run 1's copy beside the store manifest's, so the committed files can."""
        argv, snap, _, _ = _measure_setup(tmp_path, monkeypatch)
        bcc.main(argv)
        sha = json.loads((snap / 'snapshot.json').read_text(encoding='utf-8'))['sha256']
        assert sha[bcc.RUN1_MANIFEST_COPY] == sha[CropRunner.PROVENANCE_MANIFEST]

    def test_a_manifest_that_moved_after_run_1_reads_as_moved(self, tmp_path, monkeypatch):
        argv, snap, summary_path, _ = _measure_setup(tmp_path, monkeypatch)
        copy = os.path.join(argv[argv.index('--runs') + 1], bcc.RUN1_MANIFEST_COPY)
        with open(copy, 'rb') as f:
            body = f.read()
        with open(copy, 'wb') as f:
            f.write(body[:-1])
        bcc.main(argv)
        with open(summary_path, encoding='utf-8') as f:
            assert '| manifest bytes unchanged by run 2 | no |' in json.load(f)['table_rows']
        sha = json.loads((snap / 'snapshot.json').read_text(encoding='utf-8'))['sha256']
        assert sha[bcc.RUN1_MANIFEST_COPY] != sha[CropRunner.PROVENANCE_MANIFEST]


# ------------------------------------------------------------------------------------- the committed run

def _read(name):
    with open(os.path.join(DATA, name), encoding='utf-8') as f:
        return f.read()


def _gz_bytes(name):
    with gzip.open(os.path.join(DATA, name), 'rb') as f:
        return f.read()


def _cvmetadata():
    return json.loads(_gz_bytes('cvmetadata.json.gz').decode('utf-8'))


def _manifest():
    return list(csv.DictReader(io.StringIO(_gz_bytes('crop_provenance.csv.gz').decode('utf-8'))))


def _verdict_rows():
    with open(os.path.join(DATA, 'visual-verdicts.csv'), newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _report():
    with open(REPORT, encoding='utf-8') as f:
        return f.read()


def _prose():
    """The report with every run of whitespace one space, so a phrase wrapped across lines still matches."""
    return ' '.join(_report().split())


def _summary():
    with open(SUMMARY, encoding='utf-8') as f:
        return json.load(f)


def _crops_rows():
    with open(os.path.join(DATA, 'crops.csv'), newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _read_repo(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as f:
        return f.read()


_WORDS = dict(enumerate(('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten',
                         'eleven', 'twelve')))


class TestTheCommittedRunFromItsRawFiles:

    def test_the_snapshot_hashes_are_the_uncompressed_bytes(self):
        snapshot = json.loads(_read('snapshot.json'))
        for name in ('panos.json.gz', 'cvmetadata.json.gz', 'crop_provenance.csv.gz'):
            assert hashlib.sha256(_gz_bytes(name)).hexdigest() == snapshot['sha256'][name[:-3]]

    def test_run_1_cut_every_served_label_and_nothing_else_happened(self):
        text = _read('run-crop.stdout.txt')
        counts = bcc.parse_crop_summary(text)
        total = len(_cvmetadata())
        assert counts['total'] == counts['success'] == total
        assert all(counts[k] == 0 for k in CropRunner.DISJOINT_OUTCOMES if k != 'success')
        assert bcc.parse_exit(text) == 0
        assert '| `success` | %d of %d |' % (total, total) in _report()

    def test_every_served_row_has_usable_dims(self):
        rows = _cvmetadata()
        coverage = bcc.dims_coverage(rows)
        assert coverage['usable'] == coverage['total'] == len(rows)
        assert '| rows with usable dims | %d of %d |' % (len(rows), len(rows)) in _report()

    def test_the_manifest_is_one_row_per_crop_and_carries_the_served_attribution(self):
        rows, served = _manifest(), _cvmetadata()
        uids = [(r['city'], r['label_id']) for r in rows]
        assert len(uids) == len(set(uids)) == len(served)
        result = bcc.manifest_check(rows, served, bcc.CITY)
        assert result['attribution_mismatch'] == [] and result['attribution_equal'] == len(served)
        assert result['city_ok'] and result['rule_versions'] == ['v2']
        assert result['tilt_columns'] == [tuple(CropRunner.TILT_PROVENANCE_OFF)]
        pairs = {tuple(r[f] for f in ATTRIBUTION_FIELDS) for r in served}
        assert result['attribution_pairs'] == len(pairs)

    def test_the_verdict_tallies_come_from_the_verdict_file(self):
        rows = _verdict_rows()
        uids = [r['label_uid'] for r in rows]
        assert len(uids) == len(set(uids)) == len(_cvmetadata())
        assert set(uids) == {'%s:%d' % (bcc.CITY, r['label_id']) for r in _cvmetadata()}
        result = bcc.visual_verdict(rows)
        assert _summary()['visual']['tallies'] == result['tallies']
        assert '**%s**' % result['outcome'] in _report()

    def test_the_window_fov_figures_are_the_crops_tables(self):
        with open(os.path.join(DATA, 'crops.csv'), newline='', encoding='utf-8') as f:
            fovs = sorted(float(r['fov_deg']) for r in csv.DictReader(f))
        median = fovs[len(fovs) // 2]
        assert len(fovs) % 2 == 1
        assert '| window fov min / median / max (deg) | %.1f / %.1f / %.1f |' % (fovs[0], median, fovs[-1]) in _report()

    def test_the_crops_table_is_whole(self):
        with open(os.path.join(DATA, 'crops.csv'), newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        assert len({r['label_uid'] for r in rows}) == len(rows) == len(_cvmetadata())
        assert all(r['size_ok'] == 'True' and r['transcription_agrees'] == 'True' for r in rows)

    def test_every_stored_frame_is_the_apps(self):
        with open(os.path.join(DATA, 'panos.csv'), newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == len({r['pano_id'] for r in _cvmetadata()})
        assert all((r['stored_w'], r['stored_h']) == (r['app_w'], r['app_h']) for r in rows)
        assert all(r['ledger'] == '1' for r in rows)


class TestTheProseNumbers:
    """The counts the report's prose quotes outside its tables, each from a raw file."""

    def test_the_unclear_crops_by_type_and_the_posed_ones(self):
        rows = _verdict_rows()
        unclear = Counter(r['label_type'] for r in rows if r['verdict'] == 'unclear')
        posed_unclear = sum(1 for r in rows if r['verdict'] == 'unclear' and r['pose_class'] == 'posed')
        report = _prose()
        assert ('%d SurfaceProblem labels' % unclear['SurfaceProblem'], '%d Occlusion labels' % unclear['Occlusion'],
                '%d NoSidewalk labels' % unclear['NoSidewalk']) == ('23 SurfaceProblem labels', '7 Occlusion labels',
                                                                    '3 NoSidewalk labels')
        assert all(phrase in report for phrase in ('23 SurfaceProblem labels', '7 Occlusion labels',
                                                   '3 NoSidewalk labels'))
        assert sum(unclear.values()) == 33 and posed_unclear == 18
        assert '18 of the posed crops are `unclear`' in report
        off = [r for r in rows if r['verdict'] == 'in-crop-off-mark']
        assert [(r['label_uid'], r['pose_class'], r['direction']) for r in off] == [('bayonne-fr:44', 'posed', 'up')]

    def test_the_average_pano_and_the_decoded_sizes(self):
        with open(os.path.join(DATA, 'panos.csv'), newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        average_mb = sum(int(r['bytes']) for r in rows) / len(rows) / 1e6
        assert 'the %d average %.1f MB' % (len(rows), average_mb) in _prose()
        widest = max(rows, key=lambda r: int(r['stored_w']))
        decoded = int(widest['stored_w']) * int(widest['stored_h']) * 3 / 1e6
        assert 'decodes to %.1f MB' % decoded in _prose()
        assert 'decodes to %d MB' % (12288 * 6144 * 3 // 10 ** 6) in _prose()

    def test_the_prose_names_the_second_attribution_field_and_never_its_value(self):
        """The plan's wording gate (section 6), restored in review of PR 251 (nit 6)."""
        values = {r[ATTRIBUTION_FIELDS[1]] for r in _cvmetadata()} - {'', None}
        assert values and not any(v in _report() for v in values)
        assert 'one value of the second attribution field' in _prose()

    def test_the_transcription_is_described_as_it_was_written(self):
        """It was transcribed from the v2 report and CropRunner's docstrings, and never names CropRunner in its
        source; the report once said it was written "without reading CropRunner" (review of PR 251, nit 4)."""
        prose = _prose()
        assert 'without reading `CropRunner`' not in prose
        assert "transcribed from the v2 report and `CropRunner`'s docstrings" in prose

    def test_the_grid_size_quoted(self):
        n = len(TestTheStandaloneTranscription.GRID) * 4
        assert 'a grid of %d windows' % n in _prose()

    def test_the_on_mark_share_of_the_seam_crossings_and_the_cap_windows(self):
        """Joined from crops.csv and visual-verdicts.csv on label_uid. The report, the index row and the PR body
        once said "all six" and "all ten" were on-mark; one of each is `unclear` (review of PR 251, finding 1)."""
        verdict = {r['label_uid']: r['verdict'] for r in _verdict_rows()}
        crops = _crops_rows()
        groups = {'seam': [r['label_uid'] for r in crops if r['crosses_seam'] == 'True'],
                  'cap': [r['label_uid'] for r in crops if float(r['fov_deg']) >= CropRunner.CROP_MAX_FOV_DEG]}
        on = {g: sum(verdict[u] == 'on-mark' for u in uids) for g, uids in groups.items()}
        rest = {g: [u for u in uids if verdict[u] != 'on-mark'] for g, uids in groups.items()}
        assert all(verdict[u] == 'unclear' for uids in rest.values() for u in uids), 'none is off-mark'
        assert (len(groups['seam']), len(groups['cap'])) == (6, 10)
        assert rest == {'seam': ['bayonne-fr:34'], 'cap': ['bayonne-fr:52']}
        phrase = ('including %s of the %s seam crossings and %s of the %s windows at the cap; the other two, `%s` '
                  '(seam) and `%s` (cap), are `unclear`, and none is off-mark'
                  % (_WORDS[on['seam']], _WORDS[len(groups['seam'])], _WORDS[on['cap']], _WORDS[len(groups['cap'])],
                     rest['seam'][0], rest['cap'][0]))
        assert phrase in _prose()
        index = ' '.join(_read_repo('reports', 'README.md').split())
        assert ('including %s of the %s seam crossings and %s of the %s at the 90 deg cap (one `unclear` in each)'
                % (_WORDS[on['seam']], _WORDS[len(groups['seam'])], _WORDS[on['cap']], _WORDS[len(groups['cap'])])
                in index)
        for text in (_prose(), index):
            assert 'all six seam crossings' not in text and 'all ten' not in text


def _panos_rows():
    with open(os.path.join(DATA, 'panos.csv'), newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _id_list(label_ids):
    """`bayonne-fr:7`, `13`, `14`: the report's way of listing several labels of the one city."""
    return ', '.join(['`%s:%d`' % (bcc.CITY, label_ids[0])] + ['`%d`' % i for i in label_ids[1:]])


def _missing_from_report(expected):
    report = _report()
    return [row for row in expected if row not in report]


class TestTheReportTablesFromTheirRawFiles:
    """Rows that reached the report only through the summary JSON's `table_rows` (review of PR 251, finding 3),
    each re-derived here from a committed raw file, never from the summary."""

    def test_the_window_counts_from_crops_csv(self):
        rows = _crops_rows()
        for r in rows:      # the seam flag against the window's own columns
            left, width, pano_w = int(r['left']), int(r['width']), int(r['pano_w'])
            assert (r['crosses_seam'] == 'True') == ((left + width - 1) % pano_w < left), r['label_uid']
        seam = sum(r['crosses_seam'] == 'True' for r in rows)
        at_cap = Counter(r['pano_w'] for r in rows if float(r['fov_deg']) >= CropRunner.CROP_MAX_FOV_DEG)
        expected = ['| windows crossing the seam | %d |' % seam,
                    '| windows shifted vertically | %d |' % sum(r['shifted'] == 'True' for r in rows),
                    '| windows downscaled for storage | %d |' % sum(r['downscaled'] == 'True' for r in rows),
                    '| widest window (px) | %d |' % max(int(r['width']) for r in rows)]
        expected += ['| windows at the 90 deg cap on %s-wide panos | %d |' % kv for kv in sorted(at_cap.items())]
        assert not _missing_from_report(expected)
        assert (seam, sum(at_cap.values())) == (6, 10)

    def test_the_pov_table_from_cvmetadata(self):
        pov = bcc.pov_residuals(_cvmetadata())
        assert sum(z['n'] for z in pov.values()) == len(_cvmetadata())
        expected = ['| zoom %s | %d | %s | %s | %s |' % (
            zoom, z['n'], bcc.fmt(z['gain_deg_per_canvas'], '.1f'), bcc.fmt(z['raw_abs_deg']['max'], '.1f'),
            bcc.fmt(z['fit_residual_abs_deg']['max'], '.2f')) for zoom, z in pov.items()]
        assert len(expected) == 3 and not _missing_from_report(expected)

    def test_the_other_three_runs_from_their_captures(self):
        rerun, tilt, marked = (_read('run-crop-%s.stdout.txt' % name) for name in ('rerun', 'tilt', 'marked'))
        c2, ct, cm = (bcc.parse_crop_summary(t) for t in (rerun, tilt, marked))
        assert ct['success'] == 0
        reasons = _read('run-crop-tilt.log.txt').count(bcc.NO_POSE_REASON)
        expected = ['| run 2 `skipped_existing` | %d of %d |' % (c2['skipped_existing'], c2['total']),
                    '| run 2 exit code | %d |' % bcc.parse_exit(rerun),
                    '| tilt run `no_pose` | %d of %d |' % (ct['no_pose'], ct['total']),
                    '| tilt run crops written, manifest rows | %d, %d |' % (ct['success'], ct['success']),
                    '| tilt run `crop.log` lines naming the reason | %d |' % reasons,
                    '| tilt run exit code | %d |' % bcc.parse_exit(tilt),
                    '| marked run `success` | %d of %d |' % (cm['success'], cm['total'])]
        assert not _missing_from_report(expected)
        assert reasons == len({r['pano_id'] for r in _cvmetadata()}), 'one reason line per pano'

    def test_run_2_left_the_manifest_bytes_as_run_1_wrote_them(self):
        sha = json.loads(_read('snapshot.json'))['sha256']
        committed = hashlib.sha256(_gz_bytes('crop_provenance.csv.gz')).hexdigest()
        assert sha[bcc.RUN1_MANIFEST_COPY] == sha[CropRunner.PROVENANCE_MANIFEST] == committed
        assert not _missing_from_report(['| manifest bytes unchanged by run 2 | yes |'])

    def test_the_manifest_and_marker_rows_from_their_files(self):
        manifest, served = _manifest(), _cvmetadata()
        result = bcc.manifest_check(manifest, served, bcc.CITY)
        keys = [(r['city'], r['label_id']) for r in manifest]
        marker = json.loads(_read(CropRunner.CROP_RULE_MARKER))
        expected = [
            '| manifest rows, `(city, label_id)` unique | %d, %s |' % (
                len(manifest), 'yes' if len(set(keys)) == len(keys) else 'no'),
            '| `city` is `%s` on every row | %s |' % (bcc.CITY, 'yes' if {k[0] for k in keys} == {bcc.CITY} else 'no'),
            '| distinct attribution pairs | %d |' % len({tuple(r[f] for f in ATTRIBUTION_FIELDS) for r in manifest}),
            '| source values | %s |' % ', '.join('`%s`' % v for v in sorted({r[SOURCE_FIELD] for r in manifest})),
            '| `crop_rule_version` | %s |' % ', '.join(sorted({r['crop_rule_version'] for r in manifest})),
            '| marker city, rule | %s, %s |' % (marker['city'], marker['crop_rule_version']),
            '| marker no-known-gap flag, known gaps | %s, %s |' % (
                json.dumps(marker[CropRunner.MANIFEST_NO_KNOWN_GAP]), marker[CropRunner.MANIFEST_KNOWN_GAPS]),
            "| labels in run 1's manifest not in the snapshot / the reverse | %d / %d |" % (
                len({(bcc.CITY, int(k[1])) for k in keys} - {(bcc.CITY, int(r['label_id'])) for r in served}),
                len({(bcc.CITY, int(r['label_id'])) for r in served} - {(bcc.CITY, int(k[1])) for k in keys})),
            '| source and both attribution fields non-empty and equal to the served row | %d of %d |' % (
                result['attribution_equal'], len(manifest))]
        assert not _missing_from_report(expected)

    def test_the_store_rows_from_the_ledger_and_panos_csv(self):
        ledger = {}
        with open(os.path.join(DATA, 'pano_id_log.csv'), newline='', encoding='utf-8') as f:
            for row in list(csv.reader(f))[1:]:
                ledger[row[0]] = row[1]
        labelled = {r['pano_id'] for r in _cvmetadata()}
        downloaded = sum(ledger.get(p) == '1' for p in labelled)
        rows = _panos_rows()
        assert {r['pano_id'] for r in rows} == labelled
        uuid_shape = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
        frames = Counter('%sx%s' % (r['stored_w'], r['stored_h']) for r in rows)
        same = sum((r['stored_w'], r['stored_h']) == (r['app_w'], r['app_h']) == (r['label_w'], r['label_h'])
                   for r in rows)
        expected = ['| labelled panos with ledger verdict `downloaded=1` | %d of %d |' % (downloaded, len(labelled)),
                    '| labelled panos not downloaded | %d |' % (len(labelled) - downloaded),
                    '| distinct shards over the labelled panos | %d |' % len({r['pano_id'][:2] for r in rows}),
                    '| labelled pano ids that are 36-char UUIDs | %d of %d |' % (
                        sum(bool(uuid_shape.fullmatch(r['pano_id'])) for r in rows), len(rows)),
                    '| stored bytes, labelled panos | %.1f MB |' % (sum(int(r['bytes']) for r in rows) / 1e6),
                    '| stored frame equals `/adminapi/panos` and cvMetadata | %d of %d |' % (same, len(rows))]
        expected += ['| stored frame %s | %d panos |' % kv for kv in sorted(frames.items())]
        assert not _missing_from_report(expected)

    def test_the_verdict_tables_from_the_verdict_file(self):
        rows = _verdict_rows()
        tally = Counter(r['verdict'] for r in rows)
        expected = ['| `%s` | %d |' % (v, tally[v]) for v in bcc.VERDICTS]
        expected += ['| %s | %s |' % (pc, ' | '.join(str(sum(r['pose_class'] == pc and r['verdict'] == v
                                                             for r in rows)) for v in bcc.VERDICTS))
                     for pc in bcc.POSE_CLASSES]
        expected += ['| judgeable crops | %d |' % (len(rows) - tally['unclear'])]
        assert not _missing_from_report(expected)

    def test_the_white_band_rows_and_prose_from_panos_csv_and_crops_csv(self):
        panos, crops = _panos_rows(), _crops_rows()
        banded = [r for r in panos if r['white_band_top']]
        tops = [int(r['white_band_top']) / int(r['stored_h']) for r in banded]
        shares = [float(r['white_band_share']) for r in banded]
        white = [r for r in crops if int(r['white_rows'])]
        whitest = max(white, key=lambda r: int(r['white_rows']))
        at_cap = [r for r in white if float(r['fov_deg']) >= CropRunner.CROP_MAX_FOV_DEG]
        expected = [
            '| labelled panos with a white band in the lower half | %d of %d |' % (len(banded), len(panos)),
            '| band top as a share of the frame height, min / max | %.3f / %.3f |' % (min(tops), max(tops)),
            '| white pixels from the band top to the last row, min / max | %.3f / %.3f |' % (min(shares), max(shares)),
            '| crops with white rows | %d of %d |' % (len(white), len(crops)),
            '| of those, windows at the 90 deg cap | %d |' % len(at_cap),
            '| most white rows in one crop | %s of %s (`%s`) |' % (whitest['white_rows'], whitest['file_h'],
                                                                 whitest['label_uid'])]
        assert not _missing_from_report(expected)
        all_cap = [r for r in crops if float(r['fov_deg']) >= CropRunner.CROP_MAX_FOV_DEG]
        others = [r['label_uid'] for r in white if r not in at_cap]
        assert len(at_cap) == len(all_cap) == 10 and others == ['bayonne-fr:42']
        prose = _prose()
        for phrase in ('at least %d%% of its pixels above luma %d' % (round(100 * bcc.WHITE_ROW_FRACTION),
                                                                       bcc.WHITE_LUMA),
                       'the %s windows at the cap and `%s`' % (_WORDS[len(all_cap)], others[0]),
                       'flat white from %.3f of its height down' % min(tops),
                       '%d of the %d crops carry some of it, up to %s of %s rows' % (
                           len(white), len(crops), whitest['white_rows'], whitest['file_h'])):
            assert phrase in prose, phrase
        index = ' '.join(_read_repo('reports', 'README.md').split())
        assert 'flat white from %.3f of its height down, which %d crops carry' % (min(tops), len(white)) in index

    def test_the_served_counts_run_1_and_the_remaining_rows(self):
        """The fifteen rows review 2 of PR 251 found tied to no raw file (S1). Two rows cannot be: `stored files
        outside a two-char shard` and `walk_store_panos() ids equal the ledger's 1 ids` describe the scratch store,
        which is not committed; the report says so."""
        panos, served = json.loads(_gz_bytes('panos.json.gz').decode('utf-8')), _cvmetadata()
        run1 = _read('run-crop.stdout.txt')
        counts = bcc.parse_crop_summary(run1)
        crops, manifest, verdicts = _crops_rows(), _manifest(), _verdict_rows()
        tilt = sorted({tuple(r[c] for c in CropRunner.TILT_PROVENANCE_COLUMNS) for r in manifest})
        off = Counter(r['direction'] for r in verdicts if r['verdict'] == 'in-crop-off-mark')
        expected = [
            '| panos served, all / labelled | %d / %d |' % (len(panos), sum(1 for p in panos if p.get('has_labels'))),
            '| labels served, on distinct panos | %d, on %d |' % (len(served), len({r['pano_id'] for r in served})),
            '| run 1 crop files on disk, manifest rows | %d, %d |' % (sum(1 for r in crops if r['file_sha256']),
                                                                     len(manifest)),
            '| run 1 exit code | %d |' % bcc.parse_exit(run1),
            "| crop file size equals the window's stored size | %d of %d |" % (
                sum(r['size_ok'] == 'True' for r in crops), len(crops)),
            '| standalone transcription gives the same window | %d of %d |' % (
                sum(r['transcription_agrees'] == 'True' for r in crops), len(crops)),
            '| tilt columns | %s |' % '; '.join(' / '.join(v or 'blank' for v in t) for t in tilt),
            '| off-mark directions | %s |' % ', '.join('%s %d' % (d, off[d]) for d in bcc.DIRECTIONS)]
        expected += ['| `%s` | %d |' % (k, counts[k]) for k in bcc.SUMMARY_KEYS[:-1] if k != 'success']
        assert len(expected) == 15
        assert not _missing_from_report(expected)

    def test_the_white_facts_in_the_summary_are_the_tables(self):
        """`facts` in the summary JSON is data too (review 2, N3): its white-band entries against the tables."""
        facts, crops, panos = _summary()['facts'], _crops_rows(), _panos_rows()
        white = [r for r in crops if int(r['white_rows'])]
        assert facts['white_crops'] == len(white)
        assert facts['white_rows_max'] == max(int(r['white_rows']) for r in white)
        assert facts['white_crops_at_cap'] == sum(float(r['fov_deg']) >= CropRunner.CROP_MAX_FOV_DEG for r in white)
        assert facts['white_band_panos'] == sum(1 for r in panos if r['white_band_top'])

    def test_the_spot_check_lists(self):
        rows = _crops_rows()
        seam = [int(r['label_id']) for r in rows if r['crosses_seam'] == 'True']
        cap = {w: [int(r['label_id']) for r in rows if r['pano_w'] == w
                   and float(r['fov_deg']) >= CropRunner.CROP_MAX_FOV_DEG] for w in ('5376', '5760')}
        prose = _prose()
        assert 'the %s seam crossings: %s;' % (_WORDS[len(seam)], _id_list(seam)) in prose
        assert 'the %s at the 90 deg cap: %s (5376 wide) and %s (5760 wide);' % (
            _WORDS[sum(len(v) for v in cap.values())], _id_list(cap['5376']), _id_list(cap['5760'])) in prose
        per_type = prose.split('one per type: ', 1)[1].split('## ', 1)[0]
        listed = re.findall(r'(\w+) `%s:(\d+)`' % bcc.CITY, per_type)
        types = {r['label_uid']: r['label_type'] for r in _verdict_rows()}
        assert {t for t, _ in listed} == set(types.values())
        assert all(types['%s:%s' % (bcc.CITY, i)] == t for t, i in listed)


class TestTheReportMatchesTheArtifact:

    def test_every_table_row_the_script_emitted_is_in_the_report(self):
        rows = _summary()['table_rows']
        assert len(rows) >= 20
        missing = [row for row in rows if row not in _report()]
        assert not missing, missing


class TestNoLocalPathIsCommitted:
    """Every capture had its scratchpad prefix replaced before commit (the report says by what). The UUID
    pattern of the streetlevel test is left out on purpose: Panoramax pano ids are UUIDs."""

    PATTERNS = [re.compile(p, re.I) for p in (r'AppData', r'\\Users\\', r'/Users/', r'/mnt/c/', r'/home/[^/\s]+/',
                                               r'scratchpad', r'D:[\\/]Git')]

    def _texts(self):
        for folder, _, files in os.walk(DATA):
            for name in files:
                path = os.path.join(folder, name)
                if name.endswith('.gz'):
                    with gzip.open(path, 'rb') as f:
                        yield path, f.read().decode('utf-8', errors='replace')
                else:
                    with open(path, encoding='utf-8', errors='replace') as f:
                        yield path, f.read()
        with open(SUMMARY, encoding='utf-8') as f:
            yield SUMMARY, f.read()

    def test_the_data_directory_is_not_empty(self):
        assert len(list(self._texts())) >= 10

    def test_no_file_carries_one(self):
        hits = []
        for path, text in self._texts():
            for number, line in enumerate(text.splitlines(), 1):
                if any(p.search(line) for p in self.PATTERNS):
                    hits.append('%s:%d' % (os.path.relpath(path, DATA), number))
        assert not hits, hits[:20]
