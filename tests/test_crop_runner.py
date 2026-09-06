"""Tests for CropRunner.py: importability, metadata intake, the crop loop, and the #48 robustness cluster.

CropRunner has never had a test (#57): pre-fix it runs argparse and its whole flow at import, so most of
this file fails on the pre-fix code with SystemExit(2) out of `import CropRunner` — that inability to even
import is the first defect under test. The target shape is DownloadRunner's post-#52.1 contract:
build_parser() / main(argv) / run(...) seams, an import with no side effects, and per-item error handling
that cannot take down a whole run (#48).

No network anywhere: metadata comes from -f files or stubbed sessions, panos are synthetic JPEGs written
into a tmp store with the production <pano_id[:2]>/<pano_id>.jpg sharding.
"""

import csv
import json
import logging
import logging.handlers  # not implied by `import logging`; asserted on below
import os
import subprocess
import sys

import pytest
import requests
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

RUNNER = os.path.join(REPO_ROOT, 'CropRunner.py')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The columns of samples/labeldata.csv (the getFullLabelList.sql shape) — the minimal CSV intake surface.
CSV_COLUMNS = ['pano_id', 'source', 'pano_x', 'pano_y', 'label_type_id',
               'camera_heading', 'heading', 'pitch', 'label_id']

# A 2048x1024 pano with a label at the horizon predicts a 96x64 window — comfortably interior, so none of
# these tests depend on the (separately tracked, #47) edge/seam behaviour.
#
# The pano is this size because sizing rule v2 is resolution-normalised: a window is an ANGLE, so a toy
# 512x256 pano yields a 24 px one and the 20 px landmark below would be larger than the crop it is meant
# to sit inside. Shrinking the landmark instead would have hidden the property under test; the honest fix
# is a pano whose pixels mean something. NEAR_BOTTOM_Y is under the horizon on purpose: it is where the
# window is large enough for "the label is not at the centre" to be a visible distinction rather than a
# rounding difference, which the top edge (at the 8-degree floor, a 31 px tall window) cannot show.
PANO_SIZE = (2048, 1024)
INTERIOR_Y = 512
NEAR_TOP_Y = 8
NEAR_BOTTOM_Y = 1000


def label_row(pano_id='testpano0001', pano_x=200, pano_y=INTERIOR_Y, label_type_id=1, label_id=1):
    return {'pano_id': pano_id, 'pano_x': pano_x, 'pano_y': pano_y,
            'label_type_id': label_type_id, 'label_id': label_id}


def write_labels_csv(path, rows):
    """Write rows (dicts with the label_row keys) as a labeldata.csv-shaped file."""
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            full = {'source': 'gsv', 'camera_heading': 180.0, 'heading': 90.0, 'pitch': -10.0}
            full.update(row)
            writer.writerow(full)


def put_pano(store_dir, pano_id, size=PANO_SIZE, color=(255, 255, 255), square_at=None,
             square_size=20):
    """Write a synthetic pano JPEG into the store's <pano_id[:2]>/<pano_id>.jpg layout.

    square_at: optionally burn a square_size x square_size black square centred there, as a
    JPEG-robust landmark. The size is a parameter because rule v2's window varies with the label's
    depression angle, and a landmark that comfortably fits a near-field crop can be wider than a
    far-field one.
    """
    shard = os.path.join(str(store_dir), pano_id[:2])
    os.makedirs(shard, exist_ok=True)
    img = Image.new('RGB', size, color)
    if square_at is not None:
        x, y = square_at
        half = square_size // 2
        for dx in range(-half, half):
            for dy in range(-half, half):
                if 0 <= x + dx < size[0] and 0 <= y + dy < size[1]:
                    img.putpixel((x + dx, y + dy), (0, 0, 0))
    path = os.path.join(shard, pano_id + '.jpg')
    img.save(path, quality=95)
    return path


def crop_path(out_dir, label_type_id, label_id):
    return os.path.join(str(out_dir), str(label_type_id), str(label_id) + '.jpg')


# The disjoint outcome buckets, in one place so a newly added one cannot quietly fall out of the
# invariant — which is exactly how it went stale when dims_mismatch arrived. shifted_vertically is
# deliberately absent: it annotates a success (the crop was written, just de-centred), so counting it
# as its own bucket would double-count.
DISJOINT_OUTCOMES = ('success', 'skipped_existing', 'missing_pano', 'dims_mismatch',
                     'out_of_frame', 'errors')


def reconciles(counts):
    """The #48 item-3 invariant: every input label lands in exactly one outcome bucket."""
    return sum(counts[k] for k in DISJOINT_OUTCOMES) == counts['total']


def truncate_pano(store_dir, pano_id, size=PANO_SIZE):
    """Write a JPEG with a valid header and half its body — the production corruption mode.

    Distinct from garbage bytes: this opens fine (Image.open only reads the header) and blows up later
    inside crop()/load(), so it exercises the per-label handler rather than the per-pano one.
    """
    path = put_pano(store_dir, pano_id, size=size)
    data = open(path, 'rb').read()
    with open(path, 'wb') as f:
        f.write(data[:len(data) // 2])
    return path


@pytest.fixture
def crop_runner():
    """Import CropRunner. On the pre-fix code this raises SystemExit(2) out of module-scope argparse,
    which is exactly the importability defect the refactor removes."""
    import CropRunner
    return CropRunner


@pytest.fixture(autouse=True)
def _isolate_logging_state():
    """configure_logging mutates the root logger and urllib3's level; snapshot and restore around every
    test so in-process main() calls can't leak handlers into each other (the test_download_runner
    _isolate_process_state pattern, minus the SIGTERM handling CropRunner doesn't do)."""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    urllib3_level = logging.getLogger('urllib3').level
    yield
    for handler in list(root.handlers):
        if handler not in before_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(before_level)
    logging.getLogger('urllib3').setLevel(urllib3_level)


# ---------------------------------------------------------------------------
# Importability and parser contract
# ---------------------------------------------------------------------------

class TestImportIsSideEffectFree:

    def test_import_is_side_effect_free(self, monkeypatch, tmp_path):
        """Importing the module must not parse argv, configure logging, write crop.log, or crop anything.

        Pre-fix: module-scope parse_args() raises SystemExit(2) under pytest's argv, and a bare import
        with plausible argv would run the whole flow and drop crop.log in the CWD."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, 'argv', ['CropRunner.py'])
        monkeypatch.delitem(sys.modules, 'CropRunner', raising=False)
        root_handlers_before = list(logging.getLogger().handlers)

        import CropRunner  # noqa: F401

        assert list(logging.getLogger().handlers) == root_handlers_before
        assert list(tmp_path.iterdir()) == []

    def test_module_exposes_the_extracted_seams(self, crop_runner):
        for name in ('build_parser', 'configure_logging', 'run', 'main'):
            assert callable(getattr(crop_runner, name, None)), name


class TestParser:

    def test_requires_a_metadata_source(self, crop_runner):
        with pytest.raises(SystemExit) as e:
            crop_runner.build_parser().parse_args([])
        assert e.value.code == 2

    def test_d_and_f_are_mutually_exclusive(self, crop_runner):
        with pytest.raises(SystemExit) as e:
            crop_runner.build_parser().parse_args(['-d', 'x.invalid', '-f', 'y.csv'])
        assert e.value.code == 2

    def test_bare_dash_d_is_a_parse_error(self, crop_runner):
        """Pre-fix nargs='?' let `-d` (no value) through as None, which then fell into the -f branch's
        else and requested https://None/adminapi/... — a flag with a missing value must fail at parse
        time instead."""
        with pytest.raises(SystemExit) as e:
            crop_runner.build_parser().parse_args(['-d'])
        assert e.value.code == 2

    def test_defaults(self, crop_runner):
        args = crop_runner.build_parser().parse_args(
            ['-d', 'sidewalk-test.invalid', '-s', '/panos', '-o', '/out'])
        assert args.mark_label is False

    def test_the_pano_and_crop_directories_are_required(self, crop_runner):
        """#52 item 6. -o defaulted to the filesystem ROOT (needs sudo on Linux, lands on the system drive
        on Windows) and -s to /tmp/download_dest/, a path that only means anything inside the Docker
        container - which runs DownloadRunner, not this script. A forgotten flag should name itself rather
        than quietly write an ML training corpus somewhere nobody will look for it."""
        for argv in (['-d', 'x.invalid'],
                     ['-d', 'x.invalid', '-s', '/panos'],
                     ['-d', 'x.invalid', '-o', '/out']):
            with pytest.raises(SystemExit) as e:
                crop_runner.build_parser().parse_args(argv)
            assert e.value.code == 2

    def test_mark_label_is_a_flag(self, crop_runner):
        """The old MARK_LABEL=True module constant burned a dot into every crop ever produced (#48);
        marking must be an explicit opt-in."""
        args = crop_runner.build_parser().parse_args(
            ['-d', 'x.invalid', '-s', '/panos', '-o', '/out', '--mark-label'])
        assert args.mark_label is True


# ---------------------------------------------------------------------------
# Metadata intake
# ---------------------------------------------------------------------------

class TestMetadataIntake:

    def test_unknown_extension_exits_with_a_clear_error(self, crop_runner, tmp_path, capsys):
        """Pre-fix: any -f that is neither .csv nor .json left label_infos unassigned and crashed with
        NameError two lines later (#48 item 1)."""
        bad = tmp_path / 'labels.txt'
        bad.write_text('not metadata')
        with pytest.raises(SystemExit) as e:
            crop_runner.main(['-f', str(bad), '-s', str(tmp_path), '-o', str(tmp_path / 'crops')])
        assert e.value.code not in (0, None)
        printed = capsys.readouterr()
        assert '.txt' in (printed.err + printed.out + str(e.value.code))

    def test_extension_check_is_case_insensitive(self, crop_runner, tmp_path):
        """labels.CSV is a CSV. Pre-fix the exact-match dispatch NameError'd on it."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        csv_file = tmp_path / 'labels.CSV'
        write_labels_csv(csv_file, [label_row()])
        assert crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out)]) == 0
        assert os.path.exists(crop_path(out, 1, 1))

    def test_csv_pano_ids_stay_strings(self, crop_runner, tmp_path):
        """The #46 intake bug, in its discriminating form: an all-numeric pano_id column with one blank
        cell infers float64 without a dtype pin, minting ids like '1234567890.0' whose shard paths quietly
        miss every real pano. The good row must still crop and the blank-id row must count as an error,
        not walk off to a 'na/nan.jpg' path — asserted on the counts, because 'the crop exists' alone
        would also pass with the blank row silently filed as a missing pano."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, '1234567890')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row(pano_id='1234567890', label_id=1),
                                    label_row(pano_id='', label_id=2)])
        labels = crop_runner.fetch_label_ids_csv(str(csv_file))
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 2, 'success': 1, 'skipped_existing': 0, 'missing_pano': 0,
                          'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0, 'errors': 1}
        assert os.path.exists(crop_path(out, 1, 1))

    def test_csv_missing_required_column_fails_loudly(self, crop_runner, tmp_path):
        """A header typo must not surface as a KeyError deep in the crop loop."""
        csv_file = tmp_path / 'labels.csv'
        with open(csv_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['panorama', 'pano_x', 'pano_y', 'label_type_id', 'label_id'])
            writer.writerow(['abc', 1, 2, 1, 1])
        with pytest.raises((SystemExit, ValueError)) as e:
            crop_runner.main(['-f', str(csv_file), '-s', str(tmp_path), '-o', str(tmp_path / 'crops')])
        assert 'pano_id' in str(e.value)

    def test_csv_dedupes_on_label_id(self, crop_runner, tmp_path):
        rows = [label_row(label_id=7), label_row(label_id=7, pano_x=210)]
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, rows)
        labels = crop_runner.fetch_label_ids_csv(str(csv_file))
        assert len(labels) == 1

    def test_json_path_produces_crops(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        json_file = tmp_path / 'labels.json'
        json_file.write_text(json.dumps([label_row()]))
        assert crop_runner.main(['-f', str(json_file), '-s', str(store), '-o', str(out)]) == 0
        assert os.path.exists(crop_path(out, 1, 1))

    def test_json_dedupes_on_label_id(self, crop_runner):
        labels = crop_runner.json_to_list([label_row(label_id=3), label_row(label_id=3)])
        assert len(labels) == 1


class TestServerFetch:

    def test_session_is_hardened(self, crop_runner):
        """Both schemes mounted (a redirect hop to http:// must not fall back to the retry-less default
        adapter) and no env-proxy routing — the DownloadRunner #51 session shape."""
        session = crop_runner.request_session()
        try:
            assert set(session.adapters) >= {'https://', 'http://'}
            https_adapter = session.get_adapter('https://x')
            assert https_adapter is session.get_adapter('http://x')
            assert session.trust_env is False
        finally:
            session.close()

    def test_fetch_passes_a_timeout(self, crop_runner, monkeypatch):
        """Pre-fix session.get had no timeout at all: a hung server stalled the run indefinitely."""
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return [label_row()]

        class FakeSession:
            trust_env = False

            def get(self, url, **kwargs):
                captured['url'] = url
                captured.update(kwargs)
                return FakeResponse()

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()
                return False

        monkeypatch.setattr(crop_runner, 'request_session', lambda: FakeSession())
        labels = crop_runner.fetch_cvMetadata_from_server('sidewalk-test.invalid')
        assert len(labels) == 1
        assert captured['url'] == 'https://sidewalk-test.invalid/adminapi/labels/cvMetadata'
        assert captured.get('timeout') == (30, 600)

    def test_fetch_failure_logs_the_actual_exception(self, crop_runner, monkeypatch, caplog):
        """Pre-fix bug pair (#48 item 2): a ConnectionError wasn't caught at all, and the retry handler
        logged the literal 'Retries: ' with the exception text dropped by a placeholder-less format()."""

        class FakeSession:
            trust_env = False

            def get(self, url, **kwargs):
                raise requests.exceptions.ConnectionError('boom-marker-1359')

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()
                return False

        monkeypatch.setattr(crop_runner, 'request_session', lambda: FakeSession())
        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as e:
                crop_runner.fetch_cvMetadata_from_server('sidewalk-test.invalid')
        assert e.value.code == 1
        assert 'boom-marker-1359' in caplog.text


# ---------------------------------------------------------------------------
# predict_crop_size: behaviour pins so the extraction cannot drift the formula
# ---------------------------------------------------------------------------

class TestPredictCropSize:
    """The regression itself: unchanged constants, evaluated in the space they were fit in."""

    @pytest.mark.parametrize('pano_y, pano_height, expected', [
        (3328, 6656, 248.32906718298392),   # the horizon AT the calibration height
        (2000, 6656, 107.29426765313778),   # above the horizon
        (0, 6656, 54.649733399851385),      # the top row: the smallest window the formula can ask for
        (6000, 6656, 1500),                 # near field clamps to the ceiling
    ])
    def test_is_the_old_formula_at_the_calibration_height(self, crop_runner, pano_y, pano_height,
                                                          expected):
        """At pano_height == V1_REF_HEIGHT the normalisation is the identity, so these are the
        values the pre-v2 function returned. That is the compatibility claim, pinned."""
        assert crop_runner.predict_crop_size(pano_y, pano_height) == pytest.approx(expected)

    @pytest.mark.parametrize('pano_height', [2048, 6656, 16384])
    def test_the_regressions_lower_clamp_is_now_unreachable(self, crop_runner, pano_height):
        """A consequence of normalising that is worth knowing about rather than discovering later.

        Normalisation bounds the reference y-offset at +/-V1_REF_HEIGHT/2 for EVERY pano, because the
        offset is scaled by the same factor as the pano - so the largest distance the regression can
        ever be handed is fixed, and with it the smallest size: 54.65 reference px, above the 50 px
        floor. The floor is dead code kept for fidelity to the published formula; what actually bounds
        a far-field window now is CROP_MIN_FOV_DEG, one level up in crop_window_width."""
        smallest = crop_runner.predict_crop_size(0, pano_height) * 6656.0 / pano_height
        assert smallest > crop_runner.V1_SIZE_MIN
        assert smallest == pytest.approx(54.649733399851385)

    @pytest.mark.parametrize('pano_height', [2048, 4000, 6656, 8192, 16384])
    def test_the_same_relative_y_scales_with_the_pano(self, crop_runner, pano_height):
        """The defect v2 fixes, stated as a property. The old function fed native pixels into
        constants fit on 6656-px panos, so the same ramp at the same place in the world asked for a
        different window on a bigger pano. Normalised, the window is a fixed FRACTION of the pano -
        which is the same thing as a fixed angle.

        This is the case the pre-v2 suite claimed to cover and did not: its two rows both sat exactly
        on the horizon, where the y-offset is zero and raw and normalised agree for any height, so it
        passed under a rule that has no normalisation in it at all."""
        at_horizon = crop_runner.predict_crop_size(pano_height / 2, pano_height)
        assert at_horizon == pytest.approx(248.32906718298392 * pano_height / 6656.0)

    @pytest.mark.parametrize('pano_height', [2048, 4000, 6656, 8192, 16384])
    def test_window_width_is_an_angle_independent_of_resolution(self, crop_runner, pano_height):
        """What the cropper actually cuts. A quarter-degree below the horizon subtends the same angle
        whatever the pano's pixel count, so the window must too."""
        y = pano_height / 2 + 10.0 / 180.0 * pano_height          # 10 degrees below the horizon
        deg = crop_runner.crop_window_width(y, 2 * pano_height, pano_height) / pano_height * 180.0
        assert deg == pytest.approx(25.0, abs=0.05)

    def test_scale_and_clamps_are_applied_on_top_of_the_regression(self, crop_runner):
        h = 8192
        near = h / 2 + 40.0 / 180.0 * h                            # deep near field
        far = 0                                                    # far above the horizon
        mid = h / 2 + 10.0 / 180.0 * h

        assert crop_runner.crop_window_width(mid, 2 * h, h) == pytest.approx(
            crop_runner.predict_crop_size(mid, h) * crop_runner.CROP_SIZE_SCALE)
        assert crop_runner.crop_window_width(near, 2 * h, h) / h * 180.0 == pytest.approx(
            crop_runner.CROP_MAX_FOV_DEG)
        assert crop_runner.crop_window_width(far, 2 * h, h) / h * 180.0 == pytest.approx(
            crop_runner.CROP_MIN_FOV_DEG)


class TestTheRuleMarker:
    """A crop store is derived data with no other provenance, and existing crops are never re-cut.

    So a mixed store is the ORDINARY consequence of changing the rule, not an edge case: run v2 over a
    directory cut under v1 and the new crops are 3:2 while the old ones stay square, and a consumer
    training on the whole directory sees one consistent-looking set. The version therefore has to land
    in the store. It used to be a line of stdout, which is where a cron run's output goes to be lost.
    """

    def test_a_fresh_store_records_the_rule_and_its_constants(self, crop_runner, tmp_path):
        assert crop_runner.write_rule_marker(str(tmp_path)) is None
        with open(tmp_path / crop_runner.CROP_RULE_MARKER, encoding='utf-8') as f:
            marker = json.load(f)
        assert marker['crop_rule_version'] == crop_runner.CROP_RULE_VERSION
        # The constants too, so a store cut under a retuned v2 is distinguishable from this one.
        assert marker['crop_size_scale'] == crop_runner.CROP_SIZE_SCALE
        assert marker['crop_max_fov_deg'] == crop_runner.CROP_MAX_FOV_DEG
        assert marker['crop_max_stored_width'] == crop_runner.CROP_MAX_STORED_WIDTH

    def test_a_crop_run_writes_it_before_cutting_anything(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        assert (out / crop_runner.CROP_RULE_MARKER).is_file()

    def test_it_is_written_even_when_every_label_fails(self, crop_runner, tmp_path):
        """Before cutting, not after: a run that dies or crops nothing still has to leave the store
        saying what it holds."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        counts = crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        assert counts['missing_pano'] == 1 and counts['success'] == 0
        assert (out / crop_runner.CROP_RULE_MARKER).is_file()

    def test_a_store_cut_under_another_rule_is_reported_not_refused(self, crop_runner, tmp_path,
                                                                    caplog):
        """Warn, don't fail. A deliberately topped-up store is a real thing and refusing to run would
        strand it; what must not happen is that the mixing goes unrecorded."""
        os.makedirs(tmp_path, exist_ok=True)
        with open(tmp_path / crop_runner.CROP_RULE_MARKER, 'w', encoding='utf-8') as f:
            json.dump({'crop_rule_version': 'v1'}, f)
        with caplog.at_level(logging.WARNING):
            previous = crop_runner.write_rule_marker(str(tmp_path))
        assert previous == 'v1'
        assert 'v1' in caplog.text and crop_runner.CROP_RULE_VERSION in caplog.text
        with open(tmp_path / crop_runner.CROP_RULE_MARKER, encoding='utf-8') as f:
            marker = json.load(f)
        assert marker['crop_rule_version'] == crop_runner.CROP_RULE_VERSION
        assert marker['previous_crop_rule_version'] == 'v1'

    def test_rewriting_the_same_rule_is_silent(self, crop_runner, tmp_path, caplog):
        """Discrimination: the warning must be about a CHANGE, not about the marker existing — every
        run after the first would otherwise cry wolf."""
        crop_runner.write_rule_marker(str(tmp_path))
        with caplog.at_level(logging.WARNING):
            assert crop_runner.write_rule_marker(str(tmp_path)) == crop_runner.CROP_RULE_VERSION
        assert 'sizing rule' not in caplog.text

    def test_an_unreadable_marker_does_not_stop_the_run(self, crop_runner, tmp_path):
        """It is provenance, not a lock. A truncated or hand-edited marker is rewritten."""
        with open(tmp_path / crop_runner.CROP_RULE_MARKER, 'w', encoding='utf-8') as f:
            f.write('{not json')
        assert crop_runner.write_rule_marker(str(tmp_path)) is None
        with open(tmp_path / crop_runner.CROP_RULE_MARKER, encoding='utf-8') as f:
            assert json.load(f)['crop_rule_version'] == crop_runner.CROP_RULE_VERSION


class TestStorageSize:
    """min(window, 1440): a ceiling, not a target."""

    def test_a_wide_window_is_downscaled_to_the_cap(self, crop_runner):
        out = crop_runner.downscale_for_storage(Image.new('RGB', (3000, 2000)))
        assert out.size == (crop_runner.CROP_MAX_STORED_WIDTH, 960)

    def test_a_narrow_window_is_left_alone_rather_than_upscaled(self, crop_runner):
        """The whole point. The ramp carries a fixed number of source pixels; stretching them to fill
        a 1440-px file adds bytes and blur, not detail."""
        crop = Image.new('RGB', (300, 200))
        assert crop_runner.downscale_for_storage(crop) is crop

    def test_the_cap_preserves_the_aspect(self, crop_runner):
        out = crop_runner.downscale_for_storage(Image.new('RGB', (2880, 1920)))
        assert out.size == (1440, 960)


# ---------------------------------------------------------------------------
# The crop loop
# ---------------------------------------------------------------------------

class TestBulkExtractCrops:

    def test_counts_reconcile(self, crop_runner, tmp_path):
        """The disjoint outcomes must equal total. Pre-fix, already-existing crops were counted
        nowhere, so no combination of the printed numbers summed to the input size (#48 item 3)."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [label_row(label_id=1, pano_x=150),
                  label_row(label_id=2, pano_x=250),
                  label_row(label_id=3, pano_id='gonepano0001')]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 3, 'success': 2, 'skipped_existing': 0, 'missing_pano': 1,
                          'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0,
                          'errors': 0}

    def test_rerun_skips_existing_and_still_reconciles(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [label_row(label_id=1, pano_x=150), label_row(label_id=2, pano_x=250)]
        crop_runner.bulk_extract_crops(labels, str(store), str(out))
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 2, 'success': 0, 'skipped_existing': 2, 'missing_pano': 0,
                          'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0,
                          'errors': 0}

    def test_every_outcome_is_accounted_for_exactly_once(self, crop_runner, tmp_path):
        """One label per disjoint outcome, all in one run: the documented invariant is that they
        sum to total. It went stale the moment dims_mismatch was added without being added to the
        sum, so it is now asserted from the counts dict itself rather than trusted to a docstring.

        shifted_vertically is deliberately NOT in the sum - it annotates a success (the crop was
        written, just de-centred), so counting it as its own bucket would double-count."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')          # 2048x1024
        crop_runner.bulk_extract_crops([label_row(label_id=1)], str(store), str(out))  # pre-exists

        labels = [
            label_row(label_id=1),                                    # skipped_existing
            label_row(label_id=2, pano_x=250),                        # success
            label_row(label_id=3, pano_id='gonepano0001'),            # missing_pano
            dict(label_row(label_id=4), pano_width=1024, pano_height=512),   # dims_mismatch
            label_row(label_id=5, pano_y=-720),                       # out_of_frame
            label_row(label_id=6, pano_x='not-a-number'),             # errors
            label_row(label_id=7, pano_y=NEAR_TOP_Y),                 # success, shifted
        ]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))

        assert sum(counts[k] for k in DISJOINT_OUTCOMES) == counts['total'] == 7
        assert counts == {'total': 7, 'success': 2, 'skipped_existing': 1, 'missing_pano': 1,
                          'dims_mismatch': 1, 'out_of_frame': 1, 'shifted_vertically': 1,
                          'errors': 1}

    def test_a_corrupt_pano_does_not_kill_the_run(self, crop_runner, tmp_path, caplog):
        """Pre-fix: nothing wrapped make_single_crop, so one truncated JPEG raised out of
        bulk_extract_crops and lost the whole job (#48 item 4)."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        shard = store / 'ba'
        shard.mkdir(parents=True)
        (shard / 'badpano00001.jpg').write_bytes(b'this is not a jpeg')
        put_pano(store, 'testpano0001')
        labels = [label_row(label_id=1, pano_id='badpano00001'),
                  label_row(label_id=2, pano_id='testpano0001')]
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['errors'] == 1
        assert counts['success'] == 1
        assert os.path.exists(crop_path(out, 1, 2))
        assert 'badpano00001' in caplog.text

    def test_a_malformed_row_is_an_error_not_a_crash(self, crop_runner, tmp_path):
        """A row with a missing key or non-numeric coordinate is one bad label, not a dead run."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [{'pano_id': 'testpano0001', 'label_id': 1},           # no coordinates at all
                  label_row(label_id=2, pano_x='not-a-number'),
                  label_row(label_id=3, pano_x=float('nan')),
                  label_row(label_id=4, pano_id=float('nan')),          # a blank CSV cell arrives as nan
                  label_row(label_id=5)]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['errors'] == 4
        assert counts['success'] == 1
        assert os.path.exists(crop_path(out, 1, 5))

    def test_crops_are_written_atomically(self, crop_runner, tmp_path, monkeypatch):
        """A crash mid-save must not leave a truncated .jpg that the next run's exists() check treats as
        done — the downloaders' atomic_output_path contract, which the pre-fix direct save() lacked."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')

        real_save = Image.Image.save

        def failing_save(self, fp, *args, **kwargs):
            real_save(self, fp, *args, **kwargs)  # write the bytes, then die before the rename
            raise OSError('disk full marker')

        monkeypatch.setattr(Image.Image, 'save', failing_save)
        counts = crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        assert counts['errors'] == 1
        type_dir = out / '1'
        leftovers = list(type_dir.iterdir()) if type_dir.exists() else []
        assert leftovers == []  # neither the final .jpg nor a .part stub

    def test_no_part_files_survive_a_successful_run(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        assert not [p for p in out.rglob('*.part')]

    def test_pano_is_decoded_once_for_all_its_labels(self, crop_runner, tmp_path, monkeypatch):
        """Discrimination for the decode-once change: a 16384x8192 pano is ~250 MB decoded, and the
        pre-fix loop re-opened it once per label."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        opened = []
        real_open = Image.open

        def counting_open(fp, *args, **kwargs):
            opened.append(fp)
            return real_open(fp, *args, **kwargs)

        monkeypatch.setattr(crop_runner.Image, 'open', counting_open)
        labels = [label_row(label_id=i, pano_x=100 + 40 * i) for i in range(1, 4)]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['success'] == 3
        assert len(opened) == 1

    def test_crop_is_centred_on_the_label(self, crop_runner, tmp_path):
        """A landmark square at the label position must land at the crop's centre."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001', square_at=(200, INTERIOR_Y))
        crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            r, g, b = crop.getpixel((w // 2, h // 2))
            corner = crop.getpixel((5, 5))
        assert r + g + b < 150            # the landmark: near-black
        assert sum(corner) > 600          # away from it: near-white

    def test_a_whole_failed_pano_is_counted_per_label(self, crop_runner, tmp_path):
        """The grouping refactor moved accounting from per-row to per-pano, so both whole-pano outcomes
        now add len(labels). Every other test here puts exactly one label on the failing pano, which
        cannot tell `+= len(labels)` from `+= 1`; this one can, and it is the reconciliation invariant
        (#48 item 3) that would break."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'goodpano0001')
        shard = store / 'ba'
        shard.mkdir(parents=True, exist_ok=True)
        (shard / 'badpano00001.jpg').write_bytes(b'this is not a jpeg')
        labels = ([label_row(pano_id='gonepano0001', label_id=i) for i in (1, 2, 3)]
                  + [label_row(pano_id='badpano00001', label_id=i) for i in (4, 5)]
                  + [label_row(pano_id='goodpano0001', label_id=6)])
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 6, 'success': 1, 'skipped_existing': 0, 'missing_pano': 3,
                          'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0, 'errors': 2}
        assert reconciles(counts)

    def test_a_truncated_pano_is_one_error_per_label(self, crop_runner, tmp_path, caplog):
        """The corruption #48 actually describes: a JPEG whose header is intact and whose body is cut
        short. Image.open succeeds on it (it only reads the header), so this fails in crop()/load() and
        is caught by the per-label handler — a different branch from the garbage-bytes case above, which
        never reaches it."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        truncate_pano(store, 'cutpano00001')
        put_pano(store, 'goodpano0001')
        labels = [label_row(pano_id='cutpano00001', label_id=1),
                  label_row(pano_id='cutpano00001', label_id=2),
                  label_row(pano_id='goodpano0001', label_id=3)]
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 3, 'success': 1, 'skipped_existing': 0, 'missing_pano': 0,
                          'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0, 'errors': 2}
        assert 'Failed to crop label' in caplog.text
        # A failed crop must leave nothing behind: the crop file is the resume marker, so a stub here
        # would be read as done on the next run.
        assert not os.path.exists(crop_path(out, 1, 1))

    def test_a_non_finite_coordinate_is_a_row_error_not_a_crop_error(self, crop_runner, tmp_path, caplog):
        """Discrimination for the isfinite guard: without it a NaN coordinate still lands in `errors`,
        just via a crop failure instead, so the counts alone cannot tell the two apart. The
        classification is what matters — a row that never had a usable position is bad metadata, not a
        bad pano, and the operator reads the log to tell those apart."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops([label_row(label_id=1, pano_y=float('nan'))],
                                                    str(store), str(out))
        assert counts['errors'] == 1
        assert 'Skipping malformed label row' in caplog.text
        assert 'Failed to crop label' not in caplog.text

    def test_a_blank_pano_id_string_is_an_error(self, crop_runner, tmp_path):
        """A JSON payload can carry '' (or a padded id) where the CSV intake would carry NaN. It must be
        a counted error, not a missing pano: '' shards to the store root and would be reported forever
        as an image we are still waiting on."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        store.mkdir(parents=True)
        counts = crop_runner.bulk_extract_crops(
            [label_row(pano_id='', label_id=1), label_row(pano_id='   ', label_id=2)], str(store), str(out))
        assert counts == {'total': 2, 'success': 0, 'skipped_existing': 0, 'missing_pano': 0,
                          'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0, 'errors': 2}

    def test_an_unusable_output_directory_is_one_error_not_a_dead_run(self, crop_runner, tmp_path):
        """os.makedirs sat outside the try, so an OSError on the output side — a full store, a read-only
        mount, an sshfs drop, the conditions atomic_output_path itself cites — raised straight out of
        bulk_extract_crops. That is #48 item 4 again on the write side: the remaining labels never ran
        and the counts dict never came back."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        out.mkdir(parents=True)
        (out / '1').write_text('a FILE where the label-type DIR should go')
        labels = [label_row(label_id=1, label_type_id=1), label_row(label_id=2, label_type_id=2)]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 2, 'success': 1, 'skipped_existing': 0, 'missing_pano': 0,
                          'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0, 'errors': 1}
        assert os.path.exists(crop_path(out, 2, 2))

    def test_the_label_type_directory_is_made_once_per_type(self, crop_runner, tmp_path, monkeypatch):
        """exist_ok=True still costs a stat, and the loop ran it per label. Over sshfs that is a network
        round trip for each of a city's ~400k labels."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        out.mkdir(parents=True)  # so os.makedirs never recurses to create the parent and inflate the count
        made = []
        real_makedirs = os.makedirs

        def counting_makedirs(path, *args, **kwargs):
            made.append(path)
            return real_makedirs(path, *args, **kwargs)

        monkeypatch.setattr(crop_runner.os, 'makedirs', counting_makedirs)
        labels = [label_row(label_id=1, label_type_id=1, pano_x=140),
                  label_row(label_id=2, label_type_id=1, pano_x=180),
                  label_row(label_id=3, label_type_id=2, pano_x=220)]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['success'] == 3
        # The crop root first (the rule marker goes in it before any crop is cut), then one directory
        # per label TYPE, not one per label.
        assert made == [str(out), str(out / '1'), str(out / '2')]

    def test_the_shared_pano_is_closed_when_its_labels_are_done(self, crop_runner, tmp_path, monkeypatch):
        """The other half of decode-once: holding one ~250 MB decoded pano is the point, holding every
        pano in a city is a leak. Nothing pinned the close, so `with pano:` could be dropped silently."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        put_pano(store, 'otherpano001')
        opened, closed = [], []
        real_open, real_close = Image.open, Image.Image.close

        def recording_open(fp, *args, **kwargs):
            img = real_open(fp, *args, **kwargs)
            opened.append(img)
            return img

        def recording_close(self):
            closed.append(self)
            return real_close(self)

        monkeypatch.setattr(crop_runner.Image, 'open', recording_open)
        monkeypatch.setattr(Image.Image, 'close', recording_close)
        crop_runner.bulk_extract_crops([label_row(pano_id='testpano0001', label_id=1),
                                        label_row(pano_id='otherpano001', label_id=2)], str(store), str(out))
        assert len(opened) == 2
        assert all(any(c is img for c in closed) for img in opened)

    def test_bulk_extract_does_not_mutate_the_pil_global(self, crop_runner, tmp_path, monkeypatch):
        """The decompression-bomb ceiling is process-level policy and belongs in main(). This module is
        importable now — that is the whole point of #52.1 — so a library caller's PIL globals are not
        ours to rewrite as a side effect of extracting some crops."""
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 89478485)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        assert Image.MAX_IMAGE_PIXELS == 89478485


class TestMarkLabel:

    def test_marking_is_off_by_default(self, crop_runner, tmp_path):
        """#48 item 5: every crop the tool ever produced carried a burned-in dot because MARK_LABEL
        defaulted to True at module scope. Default must be clean pixels."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            centre = crop.getpixel((w // 2, h // 2))
        assert sum(centre) > 600  # untouched white pano

    def test_mark_label_draws_a_centre_dot(self, crop_runner, tmp_path):
        """Discrimination for the test above: the flag must actually do something, or 'off by default'
        would also pass with marking deleted outright."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row()], str(store), str(out), mark_label=True)
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            centre = crop.getpixel((w // 2, h // 2))
        assert sum(centre) < 600  # the dot

    def test_marks_do_not_leak_between_labels_on_one_pano(self, crop_runner, tmp_path):
        """Two nearby labels on one pano: each crop covers the other's position, so if marking drew on
        the shared pano image (as the decode-once refactor would tempt), label 1's dot would appear
        inside label 2's crop. It must not."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [label_row(label_id=1, pano_x=200), label_row(label_id=2, pano_x=230)]
        crop_runner.bulk_extract_crops(labels, str(store), str(out), mark_label=True)
        with Image.open(crop_path(out, 1, 2)) as crop2:
            w, h = crop2.size
            # Label 1's pano position, expressed in crop 2's coordinates.
            other = crop2.getpixel((w // 2 - 30, h // 2))
            centre = crop2.getpixel((w // 2, h // 2))
        assert sum(centre) < 600   # crop 2's own mark is present
        assert sum(other) > 600    # crop 2 does not contain crop 1's mark


# ---------------------------------------------------------------------------
# main(): process-level wiring
# ---------------------------------------------------------------------------

class TestMain:

    def test_end_to_end_csv_run(self, crop_runner, tmp_path, monkeypatch):
        cwd = tmp_path / 'elsewhere'
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row()])

        assert crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out)]) == 0

        assert os.path.exists(crop_path(out, 1, 1))
        # crop.log lives with the crops, not in whatever CWD the process happened to have (the
        # DownloadRunner #49 lesson: a cron CWD is nowhere anyone looks).
        assert os.path.exists(os.path.join(str(out), 'crop.log'))
        assert not os.path.exists(os.path.join(str(cwd), 'crop.log'))

    def test_mark_label_flag_reaches_the_crop(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row()])
        crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out), '--mark-label'])
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            assert sum(crop.getpixel((w // 2, h // 2))) < 600

    def test_configure_logging_caps_urllib3(self, crop_runner, tmp_path):
        crop_runner.configure_logging(str(tmp_path / 'crop.log'))
        assert logging.getLogger('urllib3').level == logging.WARNING
        assert logging.getLogger().handlers  # a handler was installed

    def test_decompression_bomb_ceiling_covers_modern_panos(self, crop_runner, tmp_path, monkeypatch):
        """A 16384x8192 pano is 134 MP — over Pillow's 89 MP default DecompressionBombWarning threshold.
        The store is our own output, so a run must raise the ceiling rather than warn per pano (or
        silently hard-fail at 2x the threshold). Driven through main(), which is where process-level
        policy belongs; see test_bulk_extract_does_not_mutate_the_pil_global for the other half."""
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 89478485)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row()])
        crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out)])
        assert Image.MAX_IMAGE_PIXELS is None or Image.MAX_IMAGE_PIXELS >= 16384 * 8192

    def test_errored_labels_make_the_exit_code_nonzero(self, crop_runner, tmp_path):
        """A run that cropped nothing at all exited 0, so nothing downstream of cron could tell it from
        a clean one. `errors` is the signal: it only ever counts things that should not have happened."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        truncate_pano(store, 'cutpano00001')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row(pano_id='cutpano00001')])
        assert crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out)]) == 1

    def test_missing_panos_alone_still_exit_zero(self, crop_runner, tmp_path):
        """Discrimination for the test above, and the reason the exit code keys on `errors` rather than
        on 'did every label produce a crop': the pano store is scraped separately and legitimately lags
        the label list, so labels waiting on an undownloaded pano are the normal state of a fresh city,
        not a failure. Note dims_mismatch and out_of_frame (#47) are deliberately in the same camp: they
        are metadata the run refused to trust, not work it got wrong."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        store.mkdir(parents=True)
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row(pano_id='gonepano0001')])
        assert crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out)]) == 0

    def test_running_as_a_script_crops(self, crop_runner, tmp_path):
        """`python3 CropRunner.py ...` end to end, in a real subprocess. Every other test here calls
        main(argv) in-process, which never executes the __main__ guard itself — so a typo in that one
        line would ship green (test_download_runner drives the real script through runpy for the same
        reason). No network: -f plus a synthetic store."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row()])
        proc = subprocess.run([sys.executable, RUNNER, '-f', str(csv_file), '-s', str(store),
                               '-o', str(out)], capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert os.path.exists(crop_path(out, 1, 1))


# ---------------------------------------------------------------------------
# Crop geometry (#47): the seam wraps, the poles clamp, nothing is ever black
# ---------------------------------------------------------------------------

def put_striped_pano(store_dir, pano_id, size=PANO_SIZE):
    """A white pano with a saturated red block on its left edge (x < 32) and a saturated blue block on
    its right edge (x >= width-32): on an equirectangular pano those two blocks are adjacent in the
    world, which is what makes seam behaviour visible after JPEG compression."""
    shard = os.path.join(str(store_dir), pano_id[:2])
    os.makedirs(shard, exist_ok=True)
    img = Image.new('RGB', size, (255, 255, 255))
    w, h = size
    for x in range(0, 32):
        for y in range(h):
            img.putpixel((x, y), (255, 0, 0))
    for x in range(w - 32, w):
        for y in range(h):
            img.putpixel((x, y), (0, 0, 255))
    path = os.path.join(shard, pano_id + '.jpg')
    img.save(path, quality=95)
    return path


def darkest_pixel_sum(crop):
    """min over pixels of r+g+b — 0 for pure zero-padding, high for any real imagery here."""
    import numpy as np
    arr = np.asarray(crop.convert('RGB'), dtype=int)
    return int(arr.sum(axis=2).min())


class TestComputeCropBox:
    """The pure geometry: integer 3:2 window, x wraps at the seam, y clamps by shifting, size capped."""

    def test_interior_label_is_centred(self, crop_runner):
        box = crop_runner.compute_crop_box(300, 128, 248.33, 512, 256)
        assert (box.left, box.top, box.width, box.height) == (176, 46, 248, 165)
        assert box.shifted is False

    def test_the_window_is_three_by_two(self, crop_runner):
        """Not square. A square window is stretched 1.5x by ImageController on write, and curb-ramp
        aprons run ~3:1 in equirectangular pixels, so its extra height is sky and road."""
        for requested in (120, 248.33, 900):
            box = crop_runner.compute_crop_box(1000, 512, requested, 2048, 1024)
            assert box.width / box.height == pytest.approx(crop_runner.CROP_ASPECT_W_OVER_H, abs=0.01)

    def test_x_wraps_at_the_seam(self, crop_runner):
        """x=0 and x=width are the same place in the world; the window must reach across, not stop."""
        box = crop_runner.compute_crop_box(0, 128, 248.33, 512, 256)
        assert (box.left, box.top, box.width) == ((0 - 124) % 512, 46, 248)

    def test_top_clamps_by_shifting(self, crop_runner):
        """The poles are NOT adjacent, so y shifts to stay inside rather than wrapping or padding."""
        box = crop_runner.compute_crop_box(300, 8, 248.33, 512, 256)
        assert box.top == 0
        assert (box.width, box.height) == (248, 165)

    def test_bottom_clamps_by_shifting(self, crop_runner):
        box = crop_runner.compute_crop_box(300, 250, 200.0, 512, 256)
        assert box.top == 256 - 133

    def test_size_caps_at_the_pano(self, crop_runner):
        """crop_window_width can ask for up to 90 degrees; a window can never exceed the image it is
        cut from. On a 2:1 pano the binding term is pano_height * 1.5, not pano_width: capping at the
        width alone would leave a 512-wide window needing 341 rows out of 256."""
        box = crop_runner.compute_crop_box(300, 250, 1500, 512, 256)
        assert (box.width, box.height) == (384, 256)
        assert box.top == 0

    def test_size_caps_at_the_narrow_axis_of_a_portrait_pano(self, crop_runner):
        """The cap is min(requested, pano_width, pano_height * 1.5), and the WIDTH term is
        load-bearing, not decoration: drop it and a window wider than the pano makes extract_crop's
        second segment read past the far edge, where Pillow zero-fills - reintroducing #47's black
        exactly where the fix claims to have removed it. Every other pano in this file is landscape,
        so nothing else discriminates the width term."""
        box = crop_runner.compute_crop_box(100, 300, 400, 200, 600)
        assert box.width == 200
        assert 0 <= box.left < 200
        assert 0 <= box.top <= 600 - box.height

    def test_box_reports_whether_it_shifted(self, crop_runner):
        """The shift is reported by the geometry itself rather than re-derived by the caller: a
        second copy of round(pano_y - height / 2) elsewhere is free to drift out of step with this
        one, and a de-centred crop that quietly stops being announced is exactly the class of
        silence this PR exists to remove."""
        assert crop_runner.compute_crop_box(300, 128, 248.33, 512, 256).shifted is False
        assert crop_runner.compute_crop_box(300, 8, 248.33, 512, 256).shifted is True
        assert crop_runner.compute_crop_box(300, 250, 200.0, 512, 256).shifted is True

    def test_box_is_integral_and_deterministic(self, crop_runner):
        """Pillow's float-box crop banker's-rounds each edge independently, so the same predicted size
        yielded 503- or 504-px crops depending on the centre's parity. Integers end that.

        `left` is pinned too, not just the dimensions: this is the only case exercising a fractional
        centre, so without it a truncating int(...) in place of int(round(...)) would move every such
        window a pixel with nothing to notice. Expected values are round-half-to-even on
        pano_x - 251.5, matching Python's round()."""
        for x, raw_left in ((100, -152), (100.5, -151), (101, -150), (250.25, -1)):
            box = crop_runner.compute_crop_box(x, 512, 503.21, 2048, 1024)
            assert isinstance(box.left, int) and isinstance(box.top, int)
            assert isinstance(box.width, int) and isinstance(box.height, int)
            assert (box.width, box.height) == (503, 335)
            assert box.left == raw_left % 2048


class TestSeamCrops:

    def test_seam_crop_wraps_instead_of_black_padding(self, crop_runner, tmp_path):
        """The #47 discriminator. A label at x=0: the crop's left half must come from the pano's RIGHT
        edge (blue), its right half from the left edge (red), with no synthetic black anywhere.
        Pre-fix Pillow zero-filled the out-of-range half; a clamp-only fix would show no blue."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_striped_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row(pano_x=0)], str(store), str(out))
        assert counts['success'] == 1
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            assert (w, h) == (96, 64)
            just_left_of_centre = crop.getpixel((w // 2 - 10, h // 2))
            just_right_of_centre = crop.getpixel((w // 2 + 10, h // 2))
            no_black = darkest_pixel_sum(crop)
        r, g, b = just_left_of_centre
        assert b > 150 and r < 120          # imagery from the pano's right edge, wrapped in
        r, g, b = just_right_of_centre
        assert r > 150 and b < 120          # imagery from the pano's left edge
        assert no_black > 60                # zero-padding would be exactly 0

    def test_far_seam_crop_wraps_too(self, crop_runner, tmp_path):
        """Same property approached from x = width-1."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_striped_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row(pano_x=2047)], str(store), str(out))
        assert counts['success'] == 1
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            left_px = crop.getpixel((w // 2 - 10, h // 2))
            right_px = crop.getpixel((w // 2 + 10, h // 2))
            no_black = darkest_pixel_sum(crop)
        assert left_px[2] > 150 and left_px[0] < 120     # blue: the right-edge block itself
        assert right_px[0] > 150 and right_px[2] < 120   # red: wrapped around to the left edge
        assert no_black > 60

    @pytest.mark.parametrize('pano_x, pano_y', [
        (0, INTERIOR_Y),        # left seam
        (2047, INTERIOR_Y),     # right seam
        (300, NEAR_TOP_Y),      # near the top edge
        (300, NEAR_BOTTOM_Y),   # near the bottom edge, where the window is widest
        (0, 0),                 # corner: seam wrap and top clamp together
        (2047, 1023),           # corner: seam wrap and bottom clamp together
    ])
    def test_no_crop_ever_contains_synthetic_black(self, crop_runner, tmp_path, pano_x, pano_y):
        """The general #47 property on an all-white pano: whatever the label position, the crop is real
        imagery edge to edge."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row(pano_x=pano_x, pano_y=pano_y)],
                                                str(store), str(out))
        assert counts['success'] == 1
        with Image.open(crop_path(out, 1, 1)) as crop:
            assert darkest_pixel_sum(crop) > 600  # plain white everywhere; padding would be 0


class TestEdgeClampBehaviour:

    def test_label_near_an_edge_stays_at_its_true_position(self, crop_runner, tmp_path):
        """When the window shifts to stay inside, the label is deliberately no longer centred — the
        landmark must sit at its true (shifted) position in the crop, not be dragged to the centre.

        Taken at the BOTTOM edge rather than the top: rule v2 sizes by depression angle, so a
        near-field label gets a wide window in which "at the label" and "at the centre" are far apart,
        while a top-edge label sits at the 8-degree floor and its 31-row window cannot separate the
        two at all."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001', square_at=(300, NEAR_BOTTOM_Y))
        crop_runner.bulk_extract_crops([label_row(pano_x=300, pano_y=NEAR_BOTTOM_Y)],
                                       str(store), str(out))
        box = crop_runner.compute_crop_box(300, NEAR_BOTTOM_Y,
                                           crop_runner.crop_window_width(NEAR_BOTTOM_Y, 2048, 1024),
                                           2048, 1024)
        assert box.shifted is True
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            at_label = crop.getpixel((w // 2, NEAR_BOTTOM_Y - box.top))
            at_centre = crop.getpixel((w // 2, h // 2))
        assert sum(at_label) < 150       # the landmark, at its true position
        assert sum(at_centre) > 600      # the centre is plain imagery, not the landmark

    def test_mark_follows_the_label_not_the_crop_centre(self, crop_runner, tmp_path):
        """--mark-label must annotate the label's true position; under an edge shift that is not the
        crop centre. At the bottom edge, for the reason given above."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row(pano_x=300, pano_y=NEAR_BOTTOM_Y)],
                                       str(store), str(out), mark_label=True)
        box = crop_runner.compute_crop_box(300, NEAR_BOTTOM_Y,
                                           crop_runner.crop_window_width(NEAR_BOTTOM_Y, 2048, 1024),
                                           2048, 1024)
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            at_label = crop.getpixel((w // 2, NEAR_BOTTOM_Y - box.top))
            at_centre = crop.getpixel((w // 2, h // 2))
        assert sum(at_label) < 600       # the dot
        assert sum(at_centre) > 600      # not at the centre

    def test_mark_lands_at_the_centre_of_a_seam_crop(self, crop_runner, tmp_path):
        """x wrapping keeps the label horizontally centred, so the dot belongs at the centre there —
        discrimination that the mark's x is computed modulo the seam, not clamped."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row(pano_x=0, pano_y=INTERIOR_Y)], str(store), str(out),
                                       mark_label=True)
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            assert sum(crop.getpixel((w // 2, h // 2))) < 600


class TestDimsReconciliation:
    """Stored pano_x/pano_y are pixels in the metadata's pano_width x pano_height frame. If the image
    on disk has different dimensions, every crop from it is silently mis-centred (the Mapillary path
    saves whatever thumb_original_url serves; GSV re-serves old ids at new resolutions). Metadata dims,
    when present, must be reconciled against the file — a loud skip, not silent poison."""

    def test_mismatched_dims_are_a_loud_skip(self, crop_runner, tmp_path, caplog):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')  # 2048x1024 on disk
        row = label_row()
        row.update(pano_width=1024, pano_height=512)
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops([row], str(store), str(out))
        assert counts['dims_mismatch'] == 1
        assert counts['success'] == 0
        assert not os.path.exists(crop_path(out, 1, 1))
        assert '1024' in caplog.text and '512' in caplog.text

    def test_matching_dims_proceed(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        row = label_row()
        row.update(pano_width=2048, pano_height=1024)
        counts = crop_runner.bulk_extract_crops([row], str(store), str(out))
        assert counts['success'] == 1
        assert counts['dims_mismatch'] == 0

    def test_old_csv_width_height_keys_are_honoured(self, crop_runner, tmp_path):
        """metadata-seattle.csv calls the same fields width/height."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        row = label_row()
        row.update(width=1024, height=512)
        counts = crop_runner.bulk_extract_crops([row], str(store), str(out))
        assert counts['dims_mismatch'] == 1

    def test_rows_without_dims_metadata_proceed(self, crop_runner, tmp_path):
        """labeldata.csv carries no pano dims; absence of the metadata is not a mismatch."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        assert counts['success'] == 1

    def test_nan_dims_metadata_proceeds(self, crop_runner, tmp_path):
        """cvMetadata serves null dims for third-party photospheres; a NaN is absent, not mismatched."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        row = label_row()
        row.update(pano_width=float('nan'), pano_height=float('nan'))
        counts = crop_runner.bulk_extract_crops([row], str(store), str(out))
        assert counts['success'] == 1


class TestCoordinateBounds:
    """Stored pano_x/pano_y must land inside the frame they are expressed in — but only y can be
    checked, and that asymmetry is the whole point.

    x needs no check and must not get one: column 0 and column pano_width are the same place in the
    world, so the seam modulo is the CORRECT reading of any finite x. Two CDMX labels in the census
    corpus store pano_x == pano_width exactly and crop perfectly.

    y has no such escape. The poles are not adjacent, so an out-of-frame y is clamped to a pole and
    the crop becomes clean imagery of a place the label is not in — worse than the black bar it
    replaced, because nothing downstream can see it. The census found exactly two such rows in
    438,410 labels, and both are the corrupt negative-pano_y rows a consumer is told to exclude.
    """

    # The two production rows, from reports/data/2026-08-10-crop-geometry-census.json.
    PRODUCTION_OUT_OF_FRAME = [(231546, 845, -720, 13312, 6656),
                               (233419, 12327, -355, 16384, 8192)]

    def test_label_above_the_top_edge_is_a_loud_skip(self, crop_runner, tmp_path, caplog):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops([label_row(pano_y=-720)], str(store), str(out))
        assert counts['out_of_frame'] == 1
        assert counts['success'] == 0
        assert not os.path.exists(crop_path(out, 1, 1))
        assert '-720' in caplog.text

    def test_label_below_the_bottom_edge_is_a_loud_skip(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row(pano_y=1100)], str(store), str(out))
        assert counts['out_of_frame'] == 1

    def test_pano_y_equal_to_the_height_is_out_of_frame(self, crop_runner, tmp_path):
        """Row indices run [0, height); pano_y == height is one row past the last one."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row(pano_y=1024)], str(store), str(out))
        assert counts['out_of_frame'] == 1

    def test_the_last_real_row_is_still_in_frame(self, crop_runner, tmp_path):
        """Discrimination for the test above: the check must be < height, not <= height - 1 - k."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row(pano_y=1023)], str(store), str(out))
        assert counts['success'] == 1
        assert counts['out_of_frame'] == 0

    def test_x_at_the_seam_boundary_is_not_rejected(self, crop_runner, tmp_path):
        """The discriminator against an over-broad bounds check. pano_x == pano_width is the same
        world column as pano_x == 0, so it must crop — and produce the identical crop."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_striped_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops(
            [label_row(label_id=1, pano_x=0), label_row(label_id=2, pano_x=2048)],
            str(store), str(out))
        assert counts['success'] == 2
        assert counts['out_of_frame'] == 0
        with Image.open(crop_path(out, 1, 1)) as a, Image.open(crop_path(out, 1, 2)) as b:
            assert a.convert('RGB').tobytes() == b.convert('RGB').tobytes()

    def test_negative_x_is_not_rejected_either(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row(pano_x=-3)], str(store), str(out))
        assert counts['success'] == 1
        assert counts['out_of_frame'] == 0

    @pytest.mark.parametrize('label_id, pano_x, pano_y, pano_width, pano_height',
                             PRODUCTION_OUT_OF_FRAME)
    def test_the_production_rows_would_be_skipped(self, crop_runner, label_id, pano_x, pano_y,
                                                 pano_width, pano_height):
        """The two real rows, at their real pano dimensions. Pre-check they clamped to a pole and
        produced a clean crop of the wrong place; the geometry still would, which is why the
        rejection has to happen before the crop, not inside it."""
        width = crop_runner.crop_window_width(pano_y, pano_width, pano_height)
        box = crop_runner.compute_crop_box(pano_x, pano_y, width, pano_width, pano_height)
        assert box.shifted is True
        assert not 0 <= pano_y - box.top < box.height  # the label is not inside its own crop
        assert not 0 <= pano_y < pano_height          # ... because the row is out of frame

    def test_a_shifted_crop_is_counted_but_still_succeeds(self, crop_runner, tmp_path):
        """A label legitimately close to a pole is de-centred, not rejected: the crop is real
        imagery containing the label. It is counted so the de-centring is visible to a consumer
        (it is a covariate for the #54 placement work), and counted as an annotation on the
        success rather than as its own bucket."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row(pano_y=NEAR_TOP_Y)], str(store), str(out))
        assert counts['success'] == 1
        assert counts['shifted_vertically'] == 1
        assert counts['out_of_frame'] == 0

    def test_an_interior_crop_is_not_counted_as_shifted(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        counts = crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        assert counts['success'] == 1
        assert counts['shifted_vertically'] == 0


# ---------------------------------------------------------------------------
# Process setup and the single-crop entry point
# ---------------------------------------------------------------------------

class TestAnUnopenableLogFallsBackToStderr:
    """crop.log is the run's only record under cron. Not being able to open it must cost the log, not the
    run - a store whose crops are already cut is exactly when the log is least worth failing over.

    DownloadRunner's twin of this is already covered by its subprocess test; CropRunner's had nothing.
    """

    def test_the_run_falls_back_to_a_stream_handler(self, crop_runner, tmp_path, caplog):
        # A directory cannot be opened as a file on any OS, which is how test_download_runner provokes the
        # same branch without needing permissions that differ between platforms.
        log_path = tmp_path / 'crop.log'
        log_path.mkdir()
        root = logging.getLogger()
        before = list(root.handlers)

        crop_runner.configure_logging(str(log_path))

        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        assert isinstance(added[0], logging.StreamHandler)
        assert not isinstance(added[0], logging.handlers.RotatingFileHandler)
        assert list(log_path.iterdir()) == [], 'nothing should have been written into the directory'

    def test_the_operator_is_told_where_the_log_went(self, crop_runner, tmp_path, caplog):
        """Silently redirecting to stderr is worse than the failure: under cron stderr goes to mail and then
        nowhere, so a run whose log 'just stopped appearing' has no explanation on disk."""
        log_path = tmp_path / 'crop.log'
        log_path.mkdir()

        with caplog.at_level(logging.WARNING):
            crop_runner.configure_logging(str(log_path))

        assert 'logging to stderr' in caplog.text
        assert str(log_path) in caplog.text

    def test_a_writable_path_still_gets_a_rotating_file_handler(self, crop_runner, tmp_path):
        """Guard the guard: the fallback must be a fallback, not what every run gets."""
        root = logging.getLogger()
        before = list(root.handlers)

        crop_runner.configure_logging(str(tmp_path / 'crop.log'))

        added = [h for h in root.handlers if h not in before]
        assert [type(h) for h in added] == [logging.handlers.RotatingFileHandler]


class TestMetadataSourceDispatch:
    """load_label_metadata's -d arm. The .csv, .json and unknown-extension arms are pinned elsewhere in this
    file; the server arm is the one nothing reached."""

    def test_no_file_means_the_server(self, crop_runner, monkeypatch):
        asked = []
        monkeypatch.setattr(crop_runner, 'fetch_cvMetadata_from_server',
                            lambda fqdn: asked.append(fqdn) or [{'label_id': 1}])
        for name in ('fetch_label_ids_csv', 'fetch_cvMetadata_from_file'):
            monkeypatch.setattr(crop_runner, name, lambda *a, **k: pytest.fail('wrong intake: ' + name))

        rows = crop_runner.load_label_metadata('sidewalk-test.invalid', None)

        assert asked == ['sidewalk-test.invalid']
        assert rows == [{'label_id': 1}]


class TestMakeSingleCropAcceptsAPath:
    """make_single_crop's path form, kept for one-off use outside the pano-grouped bulk loop.

    bulk_extract_crops always hands it an already-open Image, so the two lines that open a path and the one
    that closes it were never executed by anything.
    """

    def test_a_path_and_an_open_image_produce_the_same_crop(self, crop_runner, tmp_path):
        store = tmp_path / 'store'
        put_pano(store, 'testpano0001', square_at=(200, INTERIOR_Y))
        pano_path = os.path.join(str(store), 'te', 'testpano0001.jpg')

        from_path = str(tmp_path / 'from_path.jpg')
        from_image = str(tmp_path / 'from_image.jpg')
        box_a = crop_runner.make_single_crop(pano_path, 200, INTERIOR_Y, from_path)
        with Image.open(pano_path) as pano:
            box_b = crop_runner.make_single_crop(pano, 200, INTERIOR_Y, from_image)

        assert box_a == box_b
        assert open(from_path, 'rb').read() == open(from_image, 'rb').read()

    def test_an_image_opened_from_a_path_is_closed_again(self, crop_runner, tmp_path, monkeypatch):
        """A 16384x8192 pano is a few hundred MB decoded. Leaking one per call is survivable in a one-off
        script and is not survivable in a loop, which is precisely what this form invites."""
        store = tmp_path / 'store'
        put_pano(store, 'testpano0001')
        pano_path = os.path.join(str(store), 'te', 'testpano0001.jpg')

        # A proxy that records close(), rather than inspecting the image afterwards: PIL drops `.fp` as soon
        # as the pixels are loaded, which crop() forces, so `fp is None` is true whether or not anything
        # ever called close(). It reads like a leak check and is not one.
        class RecordingImage:
            def __init__(self, image):
                self._image = image
                self.closed = False

            def __getattr__(self, name):
                return getattr(self._image, name)

            def close(self):
                self.closed = True
                self._image.close()

        opened = []
        real_open = Image.open

        def recording_open(fp, *args, **kwargs):
            proxy = RecordingImage(real_open(fp, *args, **kwargs))
            opened.append(proxy)
            return proxy

        monkeypatch.setattr(Image, 'open', recording_open)
        crop_runner.make_single_crop(pano_path, 200, INTERIOR_Y, str(tmp_path / 'crop.jpg'))

        assert len(opened) == 1
        assert opened[0].closed, 'the pano this call opened was left open'

    def test_an_image_handed_in_is_left_open_for_its_other_labels(self, crop_runner, tmp_path):
        """The other half of the same branch: the bulk loop decodes each pano once and cuts every label on
        it, so closing a caller's image would break the label after this one."""
        store = tmp_path / 'store'
        put_pano(store, 'testpano0001')
        pano_path = os.path.join(str(store), 'te', 'testpano0001.jpg')

        with Image.open(pano_path) as pano:
            crop_runner.make_single_crop(pano, 200, INTERIOR_Y, str(tmp_path / 'first.jpg'))
            # Still usable: this is what the second label on the same pano depends on.
            crop_runner.make_single_crop(pano, 400, INTERIOR_Y, str(tmp_path / 'second.jpg'))

        assert os.path.exists(str(tmp_path / 'second.jpg'))


# ---------------------------------------------------------------------------
# #78: the crop window has one derivation, and registration is measured rather than captioned
# ---------------------------------------------------------------------------

def plant(size, at, planted=(255, 0, 0), base=(0, 0, 255)):
    """A pano of `size` in `base`, with exactly one pixel of `planted` at `at`.

    One pixel, and a colour that appears nowhere else, so "the label is at (px, py)" is decided by the
    raster rather than by a tolerance: the tests below assert both that the computed position holds the
    planted colour AND that the crop contains exactly one such pixel, which a mapping that is right by
    coincidence on a uniform image cannot satisfy. In memory and never through JPEG — quantisation
    smears a single pixel across its block, and the quantity under test is exact.
    """
    img = Image.new('RGB', size, base)
    img.putpixel(at, planted)
    return img


class TestEquirectUnits:
    """1.0 of width is 360 deg, 1.0 of height is 180 deg. Written once, and pinned here (#78)."""

    @pytest.mark.parametrize('pano_width', [1024, 2048, 13312, 16384])
    def test_a_full_turn_of_azimuth_is_the_whole_width(self, crop_runner, pano_width):
        """The absolute anchor. No factor of two survives an equality against the frame itself."""
        assert crop_runner.azimuth_deg_to_px(360.0, pano_width) == pano_width
        assert crop_runner.azimuth_deg_to_px(180.0, pano_width) == pano_width / 2

    @pytest.mark.parametrize('pano_height', [1024, 1664, 6656, 8192])
    def test_pole_to_pole_of_elevation_is_the_whole_height(self, crop_runner, pano_height):
        assert crop_runner.elevation_deg_to_px(180.0, pano_height) == pano_height
        assert crop_runner.elevation_deg_to_px(90.0, pano_height) == pano_height / 2

    def test_the_two_axes_are_the_same_only_because_panos_are_two_to_one(self, crop_runner):
        """The pin that kills a swapped axis.

        On a 2:1 pano the two conversions agree, which is the whole reason the confusion is invisible
        in production. On a square one they differ by exactly 2 — the factor #78's live instance lost.
        A module that wrote `pano_width / 180` anywhere passes the first assertion and fails the second.
        """
        assert (crop_runner.azimuth_deg_to_px(30.0, 2048)
                == crop_runner.elevation_deg_to_px(30.0, 1024))

        side = 1024
        assert (crop_runner.elevation_deg_to_px(30.0, side)
                == 2 * crop_runner.azimuth_deg_to_px(30.0, side))

    @pytest.mark.parametrize('deg', [0.0, 8.0, 25.0, 90.0, 179.0])
    def test_each_axis_round_trips(self, crop_runner, deg):
        w, h = 13312, 6656
        assert crop_runner.azimuth_px_to_deg(
            crop_runner.azimuth_deg_to_px(deg, w), w) == pytest.approx(deg)
        assert crop_runner.elevation_px_to_deg(
            crop_runner.elevation_deg_to_px(deg, h), h) == pytest.approx(deg)

    def test_the_module_states_the_anisotropy_in_exactly_one_place(self, crop_runner):
        """#78's actual proposal, as a test: the correct derivation has to be the only reachable one.

        Careful people wrote both halves of the bug the issue records, so "be careful with aspect
        ratios" is not a fix. What is a fix is that 360 and 180 appear in this module ONLY inside the
        four unit functions — a call site cannot introduce a factor of two because there is nothing
        there to get wrong. Read off the token stream rather than the text, so prose in a docstring
        saying "360 deg" is not mistaken for arithmetic, and a comment cannot satisfy it either.
        """
        import ast
        import io
        import tokenize

        with open(crop_runner.__file__, encoding='utf-8') as f:
            source = f.read()
        allowed = {'azimuth_deg_to_px', 'azimuth_px_to_deg',
                   'elevation_deg_to_px', 'elevation_px_to_deg'}
        spans = [(n.lineno, n.end_lineno) for n in ast.parse(source).body
                 if isinstance(n, ast.FunctionDef) and n.name in allowed]
        assert len(spans) == len(allowed), 'the unit primitives moved or were renamed'

        offenders = []
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.NUMBER and tok.string.replace('_', '') in (
                    '180', '360', '180.0', '360.0'):
                if not any(lo <= tok.start[0] <= hi for lo, hi in spans):
                    offenders.append((tok.start[0], tok.string))
        assert offenders == [], (
            'the equirectangular conversion is written outside the unit primitives at %s — route it '
            'through azimuth_*/elevation_* instead' % offenders)


class TestRegistration:
    """Where the labelled pixel lands in the crop, measured against the raster (#78).

    The failure this guards against is a picture captioned as covering a window that does not: nothing
    throws, the crop looks reasonable, and the surrounding correct machinery lends it credibility. So
    every assertion here reads a pixel out of the cut image; none compares one derivation to another.
    """

    @pytest.mark.parametrize('label', ['interior', 'near-top', 'near-bottom',
                                       'seam-left', 'seam-right', 'first-column'])
    def test_the_planted_pixel_is_where_the_geometry_says(self, crop_runner, label):
        w, h = PANO_SIZE
        x, y = {'interior': (1000, INTERIOR_Y),
                'near-top': (1000, NEAR_TOP_Y),
                'near-bottom': (1000, NEAR_BOTTOM_Y),
                'seam-left': (5, INTERIOR_Y),
                'seam-right': (w - 5, INTERIOR_Y),
                'first-column': (0, INTERIOR_Y)}[label]

        pano = plant((w, h), (x, y))
        box = crop_runner.compute_crop_box(x, y, crop_runner.crop_window_width(y, w, h), w, h)
        crop = crop_runner.extract_crop(pano, box.left, box.top, box.width, box.height)

        px, py = crop_runner.label_position_in_crop(x, y, box, w)
        assert crop.getpixel((int(px), int(py))) == (255, 0, 0)
        assert (1, (255, 0, 0)) in crop.getcolors(maxcolors=1 << 24), \
            'the landmark must be unique in the crop, or this assertion proves nothing'

    def test_a_shifted_crop_puts_the_label_off_centre_and_the_mapping_follows(self, crop_runner):
        """The case a "the label is at the crop's centre" shortcut gets wrong, and the reason
        label_position_in_crop takes a CropBox rather than recomputing the window from pano_y."""
        w, h = PANO_SIZE
        x, y = 1000, NEAR_BOTTOM_Y
        box = crop_runner.compute_crop_box(x, y, crop_runner.crop_window_width(y, w, h), w, h)
        assert box.shifted, 'this fixture is only meaningful on a window that had to move'

        px, py = crop_runner.label_position_in_crop(x, y, box, w)
        assert py != pytest.approx(box.height / 2)
        crop = crop_runner.extract_crop(plant((w, h), (x, y)), box.left, box.top,
                                        box.width, box.height)
        assert crop.getpixel((int(px), int(py))) == (255, 0, 0)

    def test_the_seam_is_carried_rather_than_producing_a_negative_offset(self, crop_runner):
        """A window that starts near the far edge and wraps: the naive `pano_x - box.left` is negative
        by nearly a whole pano width, which as an index reads from the wrong end of the crop."""
        w, h = PANO_SIZE
        x, y = 3, INTERIOR_Y
        box = crop_runner.compute_crop_box(x, y, crop_runner.crop_window_width(y, w, h), w, h)
        assert box.left + box.width > w, 'this fixture needs a window that crosses the seam'

        px, _ = crop_runner.label_position_in_crop(x, y, box, w)
        assert 0 <= px < box.width
        assert x - box.left < 0, 'without the modulo this case would index from the wrong end'

    def test_the_position_scales_into_the_stored_file(self, crop_runner, monkeypatch):
        """Registration has to survive downscale_for_storage, because the stored crop is what every
        consumer holds. Measured through the resize with a landmark large enough to survive it — the
        quantity under test is the position, not LANCZOS's treatment of one pixel."""
        monkeypatch.setattr(crop_runner, 'CROP_MAX_STORED_WIDTH', 64)
        w, h = PANO_SIZE
        x, y = 1000, NEAR_BOTTOM_Y

        pano = Image.new('RGB', (w, h), (0, 0, 255))
        for dx in range(-12, 12):
            for dy in range(-12, 12):
                pano.putpixel((x + dx, y + dy), (255, 0, 0))

        box = crop_runner.compute_crop_box(x, y, crop_runner.crop_window_width(y, w, h), w, h)
        stored = crop_runner.downscale_for_storage(
            crop_runner.extract_crop(pano, box.left, box.top, box.width, box.height))
        assert stored.size[0] == 64, 'this fixture needs the storage cap to actually bind'

        px, py = crop_runner.label_position_in_crop(x, y, box, w,
                                                    scale=stored.size[0] / box.width)
        r, g, b = stored.getpixel((int(px), int(py)))
        assert r > b, 'the label does not land inside the landmark once the crop is stored'

    @pytest.mark.parametrize('pano_y', [NEAR_TOP_Y, INTERIOR_Y, 700, NEAR_BOTTOM_Y])
    def test_two_views_of_one_window_share_an_aspect(self, crop_runner, monkeypatch, pano_y):
        """The issue's second registration ask. The live instance was a depth panel captioned "the same
        window" as the photo beside it at an aspect of 2.018 against 1.011 — the caption was the only
        thing asserting it. Here the cut window and the stored file are those two views, and the
        tolerance is one pixel of rounding, so a factor of two cannot hide inside it."""
        monkeypatch.setattr(crop_runner, 'CROP_MAX_STORED_WIDTH', 40)
        w, h = PANO_SIZE
        box = crop_runner.compute_crop_box(1000, pano_y, crop_runner.crop_window_width(pano_y, w, h),
                                           w, h)
        assert box.height == round(box.width / crop_runner.CROP_ASPECT_W_OVER_H)

        stored = crop_runner.downscale_for_storage(Image.new('RGB', (box.width, box.height)))
        assert stored.size[0] == 40, 'this fixture needs the storage cap to actually bind'
        assert stored.size[0] / stored.size[1] == pytest.approx(box.width / box.height,
                                                               rel=1.0 / stored.size[1])


class TestTheTwoWindowDerivationsAgree:
    """CropRunner and the gold-annotation instrument derive the same window independently, and #78's
    item 3 says that if the implementations are to stay several, something has to assert they agree.

    They stay several on purpose: reports/scripts/annotation_tiles.py may not import the cropper
    (prereg Amendment 1(e) — a gold standard sharing a transform with the thing under study measures
    zero by construction), and its module docstring says so. This class compares; it does not couple.
    If the two ever disagree, that is the finding.
    """

    @pytest.fixture
    def tiles(self):
        scripts = os.path.join(REPO_ROOT, 'reports', 'scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import annotation_tiles
        return annotation_tiles

    @pytest.mark.parametrize('pano_x', [0, 5, 1000, 2043, 2047])
    @pytest.mark.parametrize('width', [96, 300, 600])
    def test_the_left_edge_lands_in_the_same_place(self, crop_runner, tiles, pano_x, width):
        """Matched on width, since that is all `left` depends on. Includes both sides of the seam,
        where the two normalisations could differ and a negative or out-of-range origin would show."""
        w, h = PANO_SIZE
        fov = crop_runner.azimuth_px_to_deg(width, w)
        window = tiles.tile_window(pano_x, INTERIOR_Y, w, h, 0, 0, fov_deg=fov)
        assert window.width == width, 'the fixture failed to match the two window widths'

        box = crop_runner.compute_crop_box(pano_x, INTERIOR_Y, width, w, h)
        assert box.left == window.left

    @pytest.mark.parametrize('pano_y', [0, NEAR_TOP_Y, INTERIOR_Y, NEAR_BOTTOM_Y, 1023])
    @pytest.mark.parametrize('width', [96, 300, 600])
    def test_the_vertical_clamp_agrees_including_which_windows_moved(self, crop_runner, tiles,
                                                                    pano_y, width):
        """Matched on height, which is what the pole clamp depends on. Both slide a window that would
        run off a pole back inside rather than padding it, and both report that they did — so
        `shifted` is compared too, not just where the window ended up."""
        w, h = PANO_SIZE
        box = crop_runner.compute_crop_box(1000, pano_y, width, w, h)
        assert box.height % 2 == 0, 'the fixture needs an even height to match tile_extent_px'

        fov = crop_runner.elevation_px_to_deg(box.height, h)
        window = tiles.tile_window(1000, pano_y, w, h, 0, 0, fov_deg=fov)
        assert window.height == box.height, 'the fixture failed to match the two window heights'

        assert (box.top, box.shifted) == (window.top, window.shifted)

    @pytest.mark.parametrize('pano_x', [0, 3, 1000, 2045])
    @pytest.mark.parametrize('pano_y', [NEAR_TOP_Y, INTERIOR_Y, NEAR_BOTTOM_Y])
    def test_the_two_registrations_agree(self, crop_runner, tiles, pano_x, pano_y):
        """The mapping itself: pano pixel -> window pixel, by two implementations that share no code.
        `pano_to_tile` rounds to integers because an annotation is a click; label_position_in_crop
        returns floats because a measurement should not. Compared after the same rounding."""
        w, h = PANO_SIZE
        box = crop_runner.compute_crop_box(pano_x, pano_y, crop_runner.crop_window_width(pano_y, w, h),
                                           w, h)
        window = tiles.TileWindow(left=box.left, top=box.top, width=box.width, height=box.height,
                                  shifted=box.shifted, wraps=box.left + box.width > w)

        px, py = crop_runner.label_position_in_crop(pano_x, pano_y, box, w)
        assert (int(round(px)), int(round(py))) == tiles.pano_to_tile(window, pano_x, pano_y, w)


class TestTheWindowWidthIsAnAzimuthalSpan:
    """crop_window_width returns a WIDTH, so its pixels are azimuth pixels (#106 review).

    The rule's angle is derived through the elevation conversion, because predict_crop_size is a
    height-normalised length; the window that angle sizes is horizontal, so turning it back into pixels
    goes through the azimuth conversion against pano_width. On a 2:1 pano the two forms are the same
    number to the bit, which is why the elevation form served as the width unnoticed and why putting
    the axis right changed no crop. A non-2:1 pano is the only thing that tells them apart, so that is
    what the discriminating test uses.
    """

    @pytest.mark.parametrize('pano_height', [1024, 1664, 6656, 8192])
    @pytest.mark.parametrize('rel_y', [0.0, 0.5, 0.6, 0.999])
    def test_the_width_spans_the_rules_angle_in_azimuth(self, crop_runner, pano_height, rel_y):
        w, y = 2 * pano_height, rel_y * pano_height
        fov = crop_runner.crop_window_fov_deg(y, pano_height)
        width = crop_runner.crop_window_width(y, w, pano_height)
        assert crop_runner.azimuth_px_to_deg(width, w) == pytest.approx(fov)
        # Bit-identical to the elevation form the rule was measured under, on the 2:1 panos it was
        # measured on: no crop this repo has ever cut changes size.
        assert width == crop_runner.elevation_deg_to_px(fov, pano_height)

    def test_only_a_non_two_to_one_pano_tells_the_axes_apart(self, crop_runner):
        """The discriminating case. A square pano has half the azimuth pixels per degree that a 2:1
        one has, so a width converted on the wrong axis comes out exactly 2x — the #78 factor. This
        is the assertion a revert to `elevation_deg_to_px(fov, pano_height)` fails."""
        side = 1024
        fov = crop_runner.crop_window_fov_deg(INTERIOR_Y, side)
        width = crop_runner.crop_window_width(INTERIOR_Y, side, side)
        assert width == crop_runner.azimuth_deg_to_px(fov, side)
        assert width == crop_runner.elevation_deg_to_px(fov, side) / 2
