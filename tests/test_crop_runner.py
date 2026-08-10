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
import os
import sys

import pytest
import requests
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The columns of samples/labeldata.csv (the getFullLabelList.sql shape) — the minimal CSV intake surface.
CSV_COLUMNS = ['pano_id', 'source', 'pano_x', 'pano_y', 'label_type_id',
               'camera_heading', 'heading', 'pitch', 'label_id']

# A 512x256 pano with a label at y=128 predicts a 248.3 px crop — comfortably interior, so none of these
# tests depend on the (separately tracked, #47) edge/seam behaviour.
PANO_SIZE = (512, 256)
INTERIOR_Y = 128


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


def put_pano(store_dir, pano_id, size=PANO_SIZE, color=(255, 255, 255), square_at=None):
    """Write a synthetic pano JPEG into the store's <pano_id[:2]>/<pano_id>.jpg layout.

    square_at: optionally burn a 20x20 black square centred there, as a JPEG-robust landmark.
    """
    shard = os.path.join(str(store_dir), pano_id[:2])
    os.makedirs(shard, exist_ok=True)
    img = Image.new('RGB', size, color)
    if square_at is not None:
        x, y = square_at
        for dx in range(-10, 10):
            for dy in range(-10, 10):
                if 0 <= x + dx < size[0] and 0 <= y + dy < size[1]:
                    img.putpixel((x + dx, y + dy), (0, 0, 0))
    path = os.path.join(shard, pano_id + '.jpg')
    img.save(path, quality=95)
    return path


def crop_path(out_dir, label_type_id, label_id):
    return os.path.join(str(out_dir), str(label_type_id), str(label_id) + '.jpg')


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
        args = crop_runner.build_parser().parse_args(['-d', 'sidewalk-test.invalid'])
        assert args.s == '/tmp/download_dest/'
        assert args.o == '/crops/'
        assert args.mark_label is False

    def test_mark_label_is_a_flag(self, crop_runner):
        """The old MARK_LABEL=True module constant burned a dot into every crop ever produced (#48);
        marking must be an explicit opt-in."""
        args = crop_runner.build_parser().parse_args(['-d', 'x.invalid', '--mark-label'])
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
        not walk off to a 'na/nan.jpg' path."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, '1234567890')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row(pano_id='1234567890', label_id=1),
                                    label_row(pano_id='', label_id=2)])
        assert crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out)]) == 0
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

    @pytest.mark.parametrize('pano_y, pano_height, expected', [
        (4677, 8192, 503.21326713898634),   # a real Seattle sample row
        (5049, 8192, 1200.051484399708),    # near field, unclamped
        (4096, 8192, 248.32906718298392),   # the horizon
        (0, 8192, 50),                      # far field clamps to the floor
        (6000, 8192, 1500),                 # near field clamps to the ceiling
        (128, 256, 248.32906718298392),     # height-normalised: same relative y, same size
    ])
    def test_known_values(self, crop_runner, pano_y, pano_height, expected):
        assert crop_runner.predict_crop_size(pano_y, pano_height) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# The crop loop
# ---------------------------------------------------------------------------

class TestBulkExtractCrops:

    def test_counts_reconcile(self, crop_runner, tmp_path):
        """success + skipped_existing + missing_pano + errors must equal total. Pre-fix, already-existing
        crops were counted nowhere, so no combination of the printed numbers summed to the input size
        (#48 item 3)."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [label_row(label_id=1, pano_x=150),
                  label_row(label_id=2, pano_x=250),
                  label_row(label_id=3, pano_id='gonepano0001')]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 3, 'success': 2, 'skipped_existing': 0, 'missing_pano': 1,
                          'dims_mismatch': 0, 'errors': 0}

    def test_rerun_skips_existing_and_still_reconciles(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [label_row(label_id=1, pano_x=150), label_row(label_id=2, pano_x=250)]
        crop_runner.bulk_extract_crops(labels, str(store), str(out))
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 2, 'success': 0, 'skipped_existing': 2, 'missing_pano': 0,
                          'dims_mismatch': 0, 'errors': 0}

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
        labels = [label_row(label_id=1, pano_x=200), label_row(label_id=2, pano_x=260)]
        crop_runner.bulk_extract_crops(labels, str(store), str(out), mark_label=True)
        with Image.open(crop_path(out, 1, 2)) as crop2:
            w, h = crop2.size
            # Label 1's pano position, expressed in crop 2's coordinates.
            other = crop2.getpixel((w // 2 - 60, h // 2))
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
        # DownloadRunner #49 lesson: a Docker CWD dies with the container).
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
        The store is our own output, so the run must raise the ceiling rather than warn per pano (or
        silently hard-fail at 2x the threshold)."""
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 89478485)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row()], str(store), str(out))
        assert Image.MAX_IMAGE_PIXELS is None or Image.MAX_IMAGE_PIXELS >= 16384 * 8192


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
    """The pure geometry: integer window, x wraps at the seam, y clamps by shifting, size capped."""

    def test_interior_label_is_centred(self, crop_runner):
        left, top, size = crop_runner.compute_crop_box(300, 128, 248.33, 512, 256)
        assert (left, top, size) == (300 - 124, 128 - 124, 248)

    def test_x_wraps_at_the_seam(self, crop_runner):
        """x=0 and x=width are the same place in the world; the window must reach across, not stop."""
        left, top, size = crop_runner.compute_crop_box(0, 128, 248.33, 512, 256)
        assert (left, top, size) == ((0 - 124) % 512, 4, 248)

    def test_top_clamps_by_shifting(self, crop_runner):
        """The poles are NOT adjacent, so y shifts to stay inside rather than wrapping or padding."""
        left, top, size = crop_runner.compute_crop_box(300, 8, 248.33, 512, 256)
        assert top == 0
        assert size == 248

    def test_bottom_clamps_by_shifting(self, crop_runner):
        left, top, size = crop_runner.compute_crop_box(300, 250, 200.0, 512, 256)
        assert top == 256 - 200

    def test_size_caps_at_the_pano(self, crop_runner):
        """predict_crop_size can return up to 1500; a window can never exceed the image it's cut from."""
        left, top, size = crop_runner.compute_crop_box(300, 250, 1500, 512, 256)
        assert size == 256
        assert top == 0

    def test_box_is_integral_and_deterministic(self, crop_runner):
        """Pillow's float-box crop banker's-rounds each edge independently, so the same predicted size
        yielded 503- or 504-px crops depending on the centre's parity. Integers end that."""
        for x in (100, 100.5, 101, 250.25):
            left, top, size = crop_runner.compute_crop_box(x, 128, 503.21, 2048, 1024)
            assert isinstance(left, int) and isinstance(top, int) and isinstance(size, int)
            assert size == 503


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
            assert (w, h) == (248, 248)
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
        counts = crop_runner.bulk_extract_crops([label_row(pano_x=511)], str(store), str(out))
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
        (0, 128),      # left seam
        (511, 128),    # right seam
        (300, 8),      # near the top edge
        (300, 250),    # near the bottom edge
        (0, 0),        # corner: seam wrap and top clamp together
        (511, 255),    # corner: seam wrap and bottom clamp together
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

    def test_label_near_top_stays_at_its_true_position(self, crop_runner, tmp_path):
        """When the window shifts to stay inside, the label is deliberately no longer centred — the
        landmark must sit at its true (shifted) position in the crop, not be dragged to the centre."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001', square_at=(300, 8))
        crop_runner.bulk_extract_crops([label_row(pano_x=300, pano_y=8)], str(store), str(out))
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            at_label = crop.getpixel((w // 2, 8))       # top=0, so crop row == pano row
            at_centre = crop.getpixel((w // 2, h // 2))
        assert sum(at_label) < 150       # the landmark, at its true position
        assert sum(at_centre) > 600      # the centre is plain imagery, not the landmark

    def test_mark_follows_the_label_not_the_crop_centre(self, crop_runner, tmp_path):
        """--mark-label must annotate the label's true position; under an edge shift that is not the
        crop centre."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row(pano_x=300, pano_y=8)], str(store), str(out),
                                       mark_label=True)
        with Image.open(crop_path(out, 1, 1)) as crop:
            w, h = crop.size
            at_label = crop.getpixel((w // 2, 8))
            at_centre = crop.getpixel((w // 2, h // 2))
        assert sum(at_label) < 600       # the dot
        assert sum(at_centre) > 600      # not at the centre

    def test_mark_lands_at_the_centre_of_a_seam_crop(self, crop_runner, tmp_path):
        """x wrapping keeps the label horizontally centred, so the dot belongs at the centre there —
        discrimination that the mark's x is computed modulo the seam, not clamped."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([label_row(pano_x=0, pano_y=128)], str(store), str(out),
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
        put_pano(store, 'testpano0001')  # 512x256 on disk
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
        row.update(pano_width=512, pano_height=256)
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
