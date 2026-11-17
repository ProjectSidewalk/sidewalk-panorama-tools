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

    def test_a_row_with_no_usable_dims_is_counted_untested(self):
        """Mutant killed: counting every row as usable, which makes dims_mismatch 0 vacuous for it."""
        rows = [_label(1, _pano_id(1), 1, 1), _label(2, _pano_id(1), 1, 1, width=None, height=None),
                _label(3, _pano_id(2), 1, 1, width=0, height=0)]
        assert bcc.dims_coverage(rows) == {'usable': 1, 'total': 3,
                                           'untested': ['bayonne-fr:2', 'bayonne-fr:3']}


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

    def test_the_grid_size_quoted(self):
        n = len(TestTheStandaloneTranscription.GRID) * 4
        assert 'a grid of %d windows' % n in _prose()


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
