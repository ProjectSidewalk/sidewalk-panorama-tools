"""The pano-level black-band check (#179): the primitive in downloaders/common.py, the GSV downloader's refusal
to save a stitch with a deep edge band, and scan_black_bands.py, the offline sweep of the existing store.

The fixtures under tests/fixtures/black_bands/ are real-shaped JPEGs (textured imagery, Pillow's default
quality, bands written as exact RGB 0 before encoding); make_fixtures.py there regenerates them and its
docstring lists what each one is. The threshold-edge tests use in-memory frames instead, because only there
can a band depth be set to the exact line.
"""

import csv
import hashlib
import os
import shutil

import pytest
from PIL import Image

import refetch_panos
import scan_black_bands
from downloaders import common, gsv
from downloaders.common import DownloadResult

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'black_bands')
GREY = (128, 128, 128)

# One reduced line of a 1024x512 fixture at EDGE_BAND_SCALE 8 is 1/64 of the height and 1/128 of the width:
# the detector's resolution, and so the tolerance on a from-file measurement.
ONE_LINE_TALL = 1 / 64.0


def fixture(name):
    return os.path.join(FIXTURES, name)


def framed(width, height, bottom=0, right=0, colour=GREY):
    """An in-memory stitch: imagery, with exact-black bands of `bottom` rows and `right` columns at the edge -
    exactly what the stitcher's black canvas leaves where no tile was pasted."""
    image = Image.new('RGB', (width, height), (0, 0, 0))
    image.paste(Image.new('RGB', (width - right, height - bottom), colour), (0, 0))
    return image


# --- The primitive -------------------------------------------------------------------------------------------

class TestTheBandDetectorOnRealShapedJpegs:
    """The acceptance list in #179: the D4 shape, thin bottom bands at 10% and 15%, a dark but non-black scene,
    and a legitimate nadir cap. Read through the file path, which is the one the sweep uses (DCT draft)."""

    def test_the_d4_shape_reports_both_bands(self):
        bands = common.edge_black_bands_from_file(fixture('d4.jpg'))

        assert bands.bottom == pytest.approx(0.1875, abs=ONE_LINE_TALL)
        assert bands.right == pytest.approx(0.1875, abs=ONE_LINE_TALL / 2)

    @pytest.mark.parametrize('name, depth', [('thin_bottom_10.jpg', 51 / 512.0), ('thin_bottom_15.jpg', 77 / 512.0)])
    def test_a_thin_bottom_band_is_measured_to_within_one_jpeg_block(self, name, depth):
        """Neither band is aligned to the 8-row JPEG block, so the block that straddles the edge rings and is
        not counted: the measurement may fall short by up to one block, never long."""
        bands = common.edge_black_bands_from_file(fixture(name))

        assert depth - ONE_LINE_TALL <= bands.bottom <= depth
        assert bands.right == 0.0

    @pytest.mark.parametrize('name', ['thin_bottom_10.jpg', 'thin_bottom_15.jpg', 'd4.jpg'])
    def test_every_band_the_cropper_cannot_withhold_is_deep(self, name):
        """The point of #179: a band thinner than H/6 is written as success by the crop-level check, and the
        D4 shape outside its bands is invisible to it. All three are over the pano-level limit."""
        assert common.deep_edge_bands(common.edge_black_bands_from_file(fixture(name)))

    @pytest.mark.parametrize('name', ['clean.jpg', 'dark_scene.jpg', 'near_black_nadir.jpg'])
    def test_imagery_dark_imagery_and_a_near_black_nadir_have_no_band(self, name):
        """Exact zero, the definition common.black_fraction uses: JPEG noise keeps a night scene and a
        near-black (RGB 2) nadir off 0, so none of them is a band at all."""
        assert common.edge_black_bands_from_file(fixture(name)) == common.EdgeBands(0.0, 0.0)

    def test_a_legitimate_black_nadir_cap_is_measured_but_not_deep(self):
        """A camera's own black nadir cap IS a bottom band - the shapes cannot be told apart, only the depths
        - so it is reported, and the limit is what keeps it from being flagged."""
        bands = common.edge_black_bands_from_file(fixture('nadir_cap_3.jpg'))

        assert 0.0 < bands.bottom <= 15 / 512.0
        assert common.deep_edge_bands(bands) == ()

    def test_the_file_path_and_the_in_memory_path_agree(self):
        """The downloader judges the stitch in memory, the sweep judges the stored file; one primitive.

        Measured: d4.jpg is (0.1875, 0.1875) from the file and (0.171875, 0.1796875) from a full RGB decode -
        one line short each way, because the full decode's chroma upsampling bleeds colour from the imagery
        into the first black block, which turns its luma off 0. The draft path reads luma only, which is why
        the sweep uses it. The stitch is never JPEG-encoded before it is judged, so it has no such edge."""
        with Image.open(fixture('d4.jpg')) as image:
            in_memory = common.edge_black_bands(image)
        from_file = common.edge_black_bands_from_file(fixture('d4.jpg'))

        assert from_file.bottom - 2 * ONE_LINE_TALL <= in_memory.bottom <= from_file.bottom
        assert from_file.right - ONE_LINE_TALL <= in_memory.right <= from_file.right

    def test_an_unreadable_file_raises_value_error(self, tmp_path):
        bad = tmp_path / 'xx.jpg'
        bad.write_bytes(b'not a jpeg')

        with pytest.raises(ValueError):
            common.edge_black_bands_from_file(str(bad))

    def test_a_readable_header_over_a_truncated_scan_raises_value_error(self, tmp_path):
        """The shape a crashed pre-atomic writer left: the header parses, the scan stops half way."""
        with open(fixture('d4.jpg'), 'rb') as f:
            data = f.read()
        cut = tmp_path / 'xx.jpg'
        cut.write_bytes(data[:len(data) // 2])
        assert common.jpeg_dimensions(str(cut)) == (1024, 512)

        with pytest.raises(ValueError):
            common.edge_black_bands_from_file(str(cut))


class TestTheBandDetectorsEdges:
    """Exact in-memory frames, so every threshold can be put on its line and either side of it."""

    def test_a_stitch_shaped_band_is_measured_exactly(self):
        bands = common.edge_black_bands(framed(1024, 512, bottom=96, right=192))

        assert bands == common.EdgeBands(96 / 512.0, 192 / 1024.0)

    def test_only_a_band_touching_the_edge_counts(self):
        """A black stripe one imagery row-block above the bottom edge is not an edge band: the count is
        contiguous from the frame edge, not a tally of black lines."""
        image = framed(800, 400)
        image.paste(Image.new('RGB', (800, 80), (0, 0, 0)), (0, 312))      # rows 312..391, imagery below
        image.paste(Image.new('RGB', (80, 400), (0, 0, 0)), (712, 0))       # cols 712..791, imagery right

        assert common.edge_black_bands(image) == common.EdgeBands(0.0, 0.0)

    @pytest.mark.parametrize('grey_blocks, counted', [(2, True), (3, False)])
    def test_a_line_needs_at_least_the_line_minimum_black(self, grey_blocks, counted):
        """800 px wide reduces to 100 px at scale 8, so N grey 8x8 blocks in a band row leave (100-N)% black:
        98% is a band line (EDGE_BAND_LINE_MIN_BLACK, inclusive), 97% is not."""
        assert common.EDGE_BAND_LINE_MIN_BLACK == 0.98
        assert common.EDGE_BAND_SCALE == 8
        image = framed(800, 400, bottom=80)
        for row in range(320, 400, 8):
            for block in range(grey_blocks):
                image.paste(Image.new('RGB', (8, 8), GREY), (block * 16, row))

        assert common.edge_black_bands(image).bottom == (80 / 400.0 if counted else 0.0)

    @pytest.mark.parametrize('grey_blocks, counted', [(2, True), (3, False)])
    def test_the_line_minimum_holds_for_columns_too(self, grey_blocks, counted):
        image = framed(800, 800, right=80)
        for col in range(720, 800, 8):
            for block in range(grey_blocks):
                image.paste(Image.new('RGB', (8, 8), GREY), (col, block * 16))

        assert common.edge_black_bands(image).right == (80 / 800.0 if counted else 0.0)

    @pytest.mark.parametrize('rows, deep', [(80, False), (88, True)])
    def test_the_depth_limit_is_strict(self, rows, deep):
        """1600 tall reduces to 200 lines: 80 rows is 10 lines, exactly EDGE_BAND_MAX_FRACTION (0.05), and is
        NOT deep; 88 rows is 11 lines and is."""
        assert common.EDGE_BAND_MAX_FRACTION == 0.05
        bands = common.edge_black_bands(framed(3200, 1600, bottom=rows))

        assert bands.bottom == rows / 1600.0
        assert (common.deep_edge_bands(bands) == ('bottom',)) is deep

    @pytest.mark.parametrize('cols, deep', [(160, False), (176, True)])
    def test_the_depth_limit_is_strict_for_the_right_band(self, cols, deep):
        bands = common.edge_black_bands(framed(3200, 1600, right=cols))

        assert bands.right == cols / 3200.0
        assert (common.deep_edge_bands(bands) == ('right',)) is deep

    def test_deep_edge_bands_names_both_sides_and_takes_a_limit(self):
        assert common.deep_edge_bands(common.EdgeBands(0.2, 0.2)) == ('bottom', 'right')
        assert common.deep_edge_bands(common.EdgeBands(0.2, 0.2), limit=0.25) == ()
        assert common.deep_edge_bands(common.EdgeBands(0.01, 0.0), limit=0.0) == ('bottom',)

    def test_an_all_black_frame_is_one_band_of_the_whole_height(self):
        assert common.edge_black_bands(Image.new('RGB', (64, 32), (0, 0, 0))) == common.EdgeBands(1.0, 1.0)

    def test_the_detector_reuses_black_fraction(self, monkeypatch):
        """One definition of black in this repo (exact luma 0, common.black_fraction). A second, inline
        definition is exactly what the brief for #179 ruled out."""
        calls = []
        real = common.black_fraction

        def spy(image):
            calls.append(image.size)
            return real(image)

        monkeypatch.setattr(common, 'black_fraction', spy)
        common.edge_black_bands(framed(64, 32, bottom=8))

        assert calls


# --- The downloader's refusal --------------------------------------------------------------------------------

PANO_ID = 'bandPanoAAAAAAAAAAAAAA'


def download_with_stitch(monkeypatch, tmp_path, image):
    monkeypatch.setattr(gsv, 'resolve_frame',
                        lambda info: gsv.ResolvedFrame(image.width, image.height, 5, True, image.size, 'photometa'))
    monkeypatch.setattr(gsv, 'fetch_pano_image', lambda *a: gsv.StitchedPano(image, 0, False))
    return gsv.download_single_pano(str(tmp_path), {'pano_id': PANO_ID, 'width': image.width,
                                                    'height': image.height})


class TestTheDownloaderRefusesADeepEdgeBand:
    """#179 item 2: checked on the stitch before the atomic save, refused unledgered and loud on both channels.

    Raised as a FrameDisagreementError subclass, so the image loop already treats it as #74's frame refusal:
    counted, not ledgered, retried next run, and an ANSWER for images-no-success - Google served tiles, the
    frame is what is wrong."""

    @pytest.mark.parametrize('bottom, right', [(96, 192), (52, 0), (0, 64)])
    def test_a_deep_band_is_refused_and_nothing_is_written(self, monkeypatch, tmp_path, capsys, bottom, right):
        with pytest.raises(gsv.EdgeBandError) as refused:
            download_with_stitch(monkeypatch, tmp_path, framed(1024, 512, bottom=bottom, right=right))

        assert isinstance(refused.value, gsv.FrameDisagreementError)
        assert 'frame disagreement' in str(refused.value) and 'black band' in str(refused.value)
        assert PANO_ID in str(refused.value)
        shard = tmp_path / PANO_ID[:2]
        assert not shard.exists() or os.listdir(str(shard)) == [], 'no .jpg and no .part'
        out = capsys.readouterr().out
        assert 'IMAGEDOWNLOAD: WARNING' in out and PANO_ID in out and 'retried next run' in out

    def test_a_band_at_the_limit_is_saved(self, monkeypatch, tmp_path):
        """512 * 0.05 = 25.6 rows; 24 rows (three whole 8-row lines) is under it and is real imagery to us."""
        assert download_with_stitch(monkeypatch, tmp_path, framed(1024, 512, bottom=24)) == DownloadResult.success
        assert (tmp_path / PANO_ID[:2] / (PANO_ID + '.jpg')).is_file()

    def test_the_limit_is_read_at_call_time(self, monkeypatch, tmp_path):
        monkeypatch.setattr(common, 'EDGE_BAND_MAX_FRACTION', 0.25)

        assert download_with_stitch(monkeypatch, tmp_path, framed(1024, 512, bottom=96)) == DownloadResult.success

    def test_the_image_loop_does_not_ledger_it_and_logs_it(self, monkeypatch, tmp_path):
        """Through DownloadRunner's real image loop: no pano_id_log row, an ERROR in scrape.log carrying the
        refusal's text."""
        from test_download_runner import call_main_scripted
        error = gsv.EdgeBandError('pano bandPano: frame disagreement: the stitch has a black band')

        storage, code = call_main_scripted(monkeypatch, tmp_path, {'bandPano': error})

        with open(str(storage / 'pano_id_log.csv')) as f:
            assert 'bandPano' not in f.read()
        with open(str(storage / 'scrape.log')) as f:
            assert 'black band' in f.read()
        assert code == 0


# --- The sweep ------------------------------------------------------------------------------------------------

STORE_PANOS = {
    'd4PanoAAAAAAAAAAAAAAAA': 'd4.jpg',
    'thPanoAAAAAAAAAAAAAAAA': 'thin_bottom_10.jpg',
    'tfPanoAAAAAAAAAAAAAAAA': 'thin_bottom_15.jpg',
    'clPanoAAAAAAAAAAAAAAAA': 'clean.jpg',
    'dkPanoAAAAAAAAAAAAAAAA': 'dark_scene.jpg',
    'ncPanoAAAAAAAAAAAAAAAA': 'nadir_cap_3.jpg',
}
FLAGGED = {'d4PanoAAAAAAAAAAAAAAAA', 'thPanoAAAAAAAAAAAAAAAA', 'tfPanoAAAAAAAAAAAAAAAA'}


def build_store(root):
    root.mkdir(exist_ok=True)
    for pano_id, name in STORE_PANOS.items():
        shard = root / pano_id[:2]
        shard.mkdir(exist_ok=True)
        shutil.copy(fixture(name), str(shard / (pano_id + '.jpg')))
    # Not panoramas: a display copy beside one (walk_store_panos must exclude it) and a crops tree.
    shutil.copy(fixture('d4.jpg'), str(root / 'd4' / 'd4PanoAAAAAAAAAAAAAAAA.w8192.jpg'))
    (root / 'crops').mkdir()
    shutil.copy(fixture('d4.jpg'), str(root / 'crops' / 'x.jpg'))
    return root


def digests(root):
    out = {}
    for dirpath, _dirs, files in os.walk(str(root)):
        for name in files:
            path = os.path.join(dirpath, name)
            if name.endswith('.jpg'):
                with open(path, 'rb') as f:
                    out[path] = (hashlib.sha256(f.read()).hexdigest(), os.stat(path).st_mtime_ns)
    return out


def read_rows(path):
    with open(str(path), newline='') as f:
        return list(csv.DictReader(f))


class TestTheSweep:

    def test_it_lists_exactly_the_banded_panos_in_a_worklist_refetch_accepts(self, tmp_path):
        store = build_store(tmp_path)

        summary = scan_black_bands.scan_store(str(store))

        assert summary == scan_black_bands.Summary(scanned=6, clean=3, banded=3, current=0, failed=0,
                                                   unreached=0)
        worklist = store / scan_black_bands.WORKLIST_FILENAME
        rows = read_rows(worklist)
        assert {r['pano_id'] for r in rows} == FLAGGED
        d4 = next(r for r in rows if r['pano_id'].startswith('d4'))
        assert d4['side'] == 'bottom+right'
        assert float(d4['bottom_band']) == pytest.approx(0.1875, abs=ONE_LINE_TALL)
        assert (d4['width'], d4['height']) == ('1024', '512')
        records = refetch_panos.read_worklist(str(worklist))
        assert {r['pano_id'] for r in records} == FLAGGED
        assert all((r['width'], r['height']) == (1024, 512) for r in records), \
            'the stored frame, so refetch reads no dims_changed'

    def test_it_never_touches_a_stored_file(self, tmp_path):
        store = build_store(tmp_path)
        before = digests(store)

        scan_black_bands.scan_store(str(store))
        scan_black_bands.scan_store(str(store))

        after = digests(store)
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        assert not changed, ['%s: %r -> %r' % (p, before.get(p), after.get(p)) for p in changed]

    def test_the_ledger_records_every_pano_so_it_is_the_calibration_data(self, tmp_path):
        store = build_store(tmp_path)

        scan_black_bands.scan_store(str(store))

        rows = read_rows(store / scan_black_bands.LEDGER_FILENAME)
        assert {r['pano_id'] for r in rows} == set(STORE_PANOS)
        cap = next(r for r in rows if r['pano_id'].startswith('nc'))
        assert 0.0 < float(cap['bottom_band']) < common.EDGE_BAND_MAX_FRACTION

    def test_a_rerun_decodes_nothing_and_writes_the_same_worklist(self, tmp_path, monkeypatch):
        store = build_store(tmp_path)
        scan_black_bands.scan_store(str(store))
        first = (store / scan_black_bands.WORKLIST_FILENAME).read_bytes()

        def no_decode(path):
            raise AssertionError('a finished store must cost no decode: %s' % path)

        monkeypatch.setattr(common, 'edge_black_bands_from_file', no_decode)
        summary = scan_black_bands.scan_store(str(store))

        assert summary.current == 6 and summary.scanned == 6
        assert (store / scan_black_bands.WORKLIST_FILENAME).read_bytes() == first

    def test_a_changed_pano_is_scanned_again(self, tmp_path):
        """Keyed on size and mtime as well as id, so a pano a repair pass swapped is judged afresh."""
        store = build_store(tmp_path)
        scan_black_bands.scan_store(str(store))
        swapped = store / 'd4' / 'd4PanoAAAAAAAAAAAAAAAA.jpg'
        shutil.copy(fixture('clean.jpg'), str(swapped))

        summary = scan_black_bands.scan_store(str(store))

        assert summary.current == 5 and summary.clean == 1
        assert {r['pano_id'] for r in read_rows(store / scan_black_bands.WORKLIST_FILENAME)} \
            == FLAGGED - {'d4PanoAAAAAAAAAAAAAAAA'}

    def test_a_budget_leaves_the_rest_unreached_and_the_next_run_finishes(self, tmp_path, monkeypatch):
        store = build_store(tmp_path)
        clock = iter([0.0] + [0.0] * 2 + [1e9] * 100)
        monkeypatch.setattr(scan_black_bands.time, 'monotonic', lambda: next(clock))

        first = scan_black_bands.scan_store(str(store), max_runtime_minutes=1)

        assert first.scanned == 2 and first.unreached == 4
        monkeypatch.undo()
        second = scan_black_bands.scan_store(str(store))
        assert second.current == 2 and second.scanned == 6
        assert {r['pano_id'] for r in read_rows(store / scan_black_bands.WORKLIST_FILENAME)} == FLAGGED

    def test_progress_survives_a_kill(self, tmp_path, monkeypatch):
        """Each verdict is on disk as it lands, so a run killed half way - SIGKILL included, which runs no
        `finally` and closes no file - keeps what it did. Read from disk WHILE the run is still going."""
        store = build_store(tmp_path)
        real = common.edge_black_bands_from_file
        seen = []
        on_disk_at_the_third = []

        def killed_on_the_third(path):
            if len(seen) == 2:
                on_disk_at_the_third.append(len(read_rows(store / scan_black_bands.LEDGER_FILENAME)))
                raise KeyboardInterrupt
            seen.append(path)
            return real(path)

        monkeypatch.setattr(common, 'edge_black_bands_from_file', killed_on_the_third)
        with pytest.raises(KeyboardInterrupt):
            scan_black_bands.scan_store(str(store))

        assert on_disk_at_the_third == [2]
        assert len(read_rows(store / scan_black_bands.LEDGER_FILENAME)) == 2

    @pytest.mark.parametrize('elapsed, scanned', [(59.999, 6), (60.0, 0)])
    def test_the_budget_is_spent_at_exactly_its_length(self, tmp_path, monkeypatch, elapsed, scanned):
        store = build_store(tmp_path)
        clock = iter([0.0] + [elapsed] * 100)
        monkeypatch.setattr(scan_black_bands.time, 'monotonic', lambda: next(clock))

        summary = scan_black_bands.scan_store(str(store), max_runtime_minutes=1)

        assert (summary.scanned, summary.unreached) == (scanned, 6 - scanned)

    def test_an_unreadable_pano_is_failed_unledgered_and_retried(self, tmp_path, capsys):
        store = build_store(tmp_path)
        (store / 'zz').mkdir()
        (store / 'zz' / 'zzBroken.jpg').write_bytes(b'\xff\xd8 truncated')

        first = scan_black_bands.scan_store(str(store))
        second = scan_black_bands.scan_store(str(store))

        assert first.failed == 1 and second.failed == 1
        assert 'zzBroken' not in (store / scan_black_bands.LEDGER_FILENAME).read_text()
        assert 'FAILED' in capsys.readouterr().out

    @pytest.mark.parametrize('min_band, expected', [
        (None, FLAGGED),
        (0.0, FLAGGED | {'ncPanoAAAAAAAAAAAAAAAA'}),
        (0.12, {'d4PanoAAAAAAAAAAAAAAAA', 'tfPanoAAAAAAAAAAAAAAAA'}),
    ])
    def test_min_band_sets_the_worklist_cut(self, tmp_path, min_band, expected):
        """--min-band 0 lists every pano with any band at all: the calibration view of the store."""
        store = build_store(tmp_path)

        summary = scan_black_bands.scan_store(str(store), min_band=min_band)

        assert {r['pano_id'] for r in read_rows(store / scan_black_bands.WORKLIST_FILENAME)} == expected
        assert summary.banded == len(expected)

    def test_the_counts_reconcile(self, tmp_path):
        store = build_store(tmp_path)
        (store / 'zz').mkdir()
        (store / 'zz' / 'zzBroken.jpg').write_bytes(b'junk')
        scan_black_bands.scan_store(str(store))
        shutil.copy(fixture('d4.jpg'), str(store / 'cl' / 'clPanoAAAAAAAAAAAAAAAA.jpg'))

        s = scan_black_bands.scan_store(str(store))

        assert s.scanned == s.clean + s.banded + s.current + s.failed
        assert (s.scanned, s.clean, s.banded, s.current, s.failed) == (7, 0, 1, 5, 1)


class TestTheSweepsCommandLine:

    def test_main_prints_one_summary_line_and_exits_zero(self, tmp_path, capsys):
        store = build_store(tmp_path)

        code = scan_black_bands.main([str(store)])

        out = capsys.readouterr().out
        assert code == 0
        assert 'Examined 6 panorama(s): 3 with an edge band over 5.0%' in out
        assert 'black_band_worklist.csv' in out

    def test_main_exits_one_when_a_pano_failed(self, tmp_path):
        store = build_store(tmp_path)
        (store / 'zz').mkdir()
        (store / 'zz' / 'zzBroken.jpg').write_bytes(b'junk')

        assert scan_black_bands.main([str(store)]) == 1

    def test_explicit_output_paths(self, tmp_path):
        store = build_store(tmp_path / 'store')
        out = tmp_path / 'out'
        out.mkdir()

        scan_black_bands.main([str(store), '--ledger', str(out / 'l.csv'), '--worklist', str(out / 'w.csv')])

        assert {r['pano_id'] for r in read_rows(out / 'w.csv')} == FLAGGED
        assert not (store / scan_black_bands.LEDGER_FILENAME).exists()

    def test_min_band_zero_through_main_is_the_calibration_view(self, tmp_path, capsys):
        store = build_store(tmp_path)

        assert scan_black_bands.main([str(store), '--min-band', '0']) == 0

        assert 'with an edge band over 0.0%' in capsys.readouterr().out
        assert len(read_rows(store / scan_black_bands.WORKLIST_FILENAME)) == 4

    @pytest.mark.parametrize('value', ['-0.1', '1.5', 'x'])
    def test_min_band_must_be_a_fraction(self, tmp_path, value):
        with pytest.raises(SystemExit):
            scan_black_bands.main([str(tmp_path), '--min-band', value])
