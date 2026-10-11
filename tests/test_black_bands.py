"""The pano-level black-band check (#179): the primitive in downloaders/common.py, the GSV downloader saving
and FLAGGING a stitch with a deep edge band (never refusing it: #213 review S1), the `edge-band` run condition
that reports the count, and scan_black_bands.py, the offline sweep of the existing store, with its exact and
near-black measurements.

The fixtures under tests/fixtures/black_bands/ are real-shaped JPEGs (textured imagery, Pillow's default
quality, bands written as exact RGB 0 before encoding); make_fixtures.py there regenerates them and its
docstring lists what each one is. The threshold-edge tests use in-memory frames instead, because only there
can a band depth be set to the exact line.
"""

import csv
import hashlib
import logging
import os
import shutil

import pytest
from PIL import Image

import DownloadRunner
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

    @pytest.mark.parametrize('path', ['memory', 'file'])
    def test_both_paths_judge_the_frame_at_one_eighth(self, monkeypatch, path):
        """M4/M5: the reduce and the draft both hand _edge_bands_of_luma a 1024x512 frame as 128x64. A path
        that skipped its reduction would still be right, at 64x the cost, so pin the size it is judged at."""
        sizes = []
        real = common._edge_bands_of_luma

        def spy(luma, *args, **kwargs):
            sizes.append(luma.size)
            return real(luma, *args, **kwargs)

        monkeypatch.setattr(common, '_edge_bands_of_luma', spy)
        if path == 'memory':
            common.edge_black_bands(framed(1024, 512, bottom=96))
        else:
            common.edge_band_report_from_file(fixture('d4.jpg'))

        assert sizes and set(sizes) == {(128, 64)}


class TestTheNearBlackMeasurement:
    """#213 review S4: exact black is encoder-dependent (RGB 0 decodes to luma 1 at 26 of Pillow's 96 JPEG
    qualities, and with Google's own DC quantiser tiles), so the sweep records a second band at
    luma <= EDGE_BAND_NEAR_BLACK_LUMA beside the exact one, from the same decode. A measurement for the
    calibration, never a verdict: the downloader's own check stays on exact zero."""

    def near_of(self, image):
        return common._edge_bands_of_luma(image.reduce(common.EDGE_BAND_SCALE).convert('L'),
                                          common.EDGE_BAND_NEAR_BLACK_LUMA)

    def test_a_band_of_rgb_one_is_near_black_and_not_exact(self):
        assert common.EDGE_BAND_NEAR_BLACK_LUMA == 2
        image = framed(1024, 512, bottom=96)
        image.paste(Image.new('RGB', (1024, 96), (1, 1, 1)), (0, 512 - 96))

        assert common.edge_black_bands(image) == common.EdgeBands(0.0, 0.0)
        assert self.near_of(image) == common.EdgeBands(96 / 512.0, 0.0)

    def test_a_band_exactly_at_the_ceiling_is_near_black(self):
        """luma 2 IS near black (<=, the ceiling is inclusive): the `[:max_luma]` mutant survives the RGB 1
        and RGB 3 cases either side of it and dies here."""
        image = framed(1024, 512, bottom=96)
        image.paste(Image.new('RGB', (1024, 96), (2, 2, 2)), (0, 512 - 96))

        assert self.near_of(image) == common.EdgeBands(96 / 512.0, 0.0)

    def test_a_band_of_rgb_three_is_neither(self):
        image = framed(1024, 512, bottom=96)
        image.paste(Image.new('RGB', (1024, 96), (3, 3, 3)), (0, 512 - 96))

        assert common.edge_black_bands(image) == common.EdgeBands(0.0, 0.0)
        assert self.near_of(image) == common.EdgeBands(0.0, 0.0)

    def test_the_near_band_is_never_shallower_than_the_exact_one(self):
        for name in STORE_PANOS.values():
            report = common.edge_band_report_from_file(fixture(name))
            assert report.near.bottom >= report.exact.bottom, name
            assert report.near.right >= report.exact.right, name

    def test_an_exact_band_through_a_quality_40_encoder_is_only_visible_as_near_black(self):
        """near_black_band_q40.jpg is thin_bottom_15's exact-0 band saved at quality=40, where the DC quantiser
        moves 0 to 1: the EXACT measurement reads no band at all, the near one reads the band. This is the
        failure the review found in the calibration plan - a sweep that measured exact black over provider
        bytes would conclude "no caps"."""
        report = common.edge_band_report_from_file(fixture('near_black_band_q40.jpg'))

        assert report.exact == common.EdgeBands(0.0, 0.0)
        assert 77 / 512.0 - ONE_LINE_TALL <= report.near.bottom <= 77 / 512.0
        assert report.near.right == 0.0

    def test_both_measurements_come_from_one_decode(self, monkeypatch):
        opened = []
        real_open = Image.open

        def counting_open(*args, **kwargs):
            opened.append(args[0])
            return real_open(*args, **kwargs)

        monkeypatch.setattr(common.Image, 'open', counting_open)
        common.edge_band_report_from_file(fixture('d4.jpg'))

        assert len(opened) == 1

    def test_edge_black_bands_from_file_is_the_exact_half_of_the_report(self):
        assert common.edge_black_bands_from_file(fixture('d4.jpg')) == \
            common.edge_band_report_from_file(fixture('d4.jpg')).exact

    def test_dims_already_read_are_accepted_and_the_header_is_not_reread(self, monkeypatch):
        """N5: the sweep has already read the header for its ledger row."""
        monkeypatch.setattr(common, 'jpeg_dimensions',
                            lambda path: pytest.fail('the header was read again'))

        report = common.edge_band_report_from_file(fixture('d4.jpg'), dims=(1024, 512))

        assert report.exact.bottom == pytest.approx(0.1875, abs=ONE_LINE_TALL)


# --- The downloader saves and flags -------------------------------------------------------------------------

PANO_ID = 'bandPanoAAAAAAAAAAAAAA'
WARNING = 'IMAGEDOWNLOAD: WARNING'


def stub_stitch(monkeypatch, image, upscaled=False):
    monkeypatch.setattr(gsv, 'resolve_frame',
                        lambda info: gsv.ResolvedFrame(image.width, image.height, 5, True, image.size, 'photometa'))
    monkeypatch.setattr(gsv, 'fetch_pano_image', lambda *a: gsv.StitchedPano(image, 0, upscaled))


def download_with_stitch(monkeypatch, tmp_path, image, upscaled=False):
    stub_stitch(monkeypatch, image, upscaled)
    return gsv.download_single_pano(str(tmp_path), {'pano_id': PANO_ID, 'width': image.width,
                                                    'height': image.height})


def pano_file(tmp_path):
    return tmp_path / PANO_ID[:2] / (PANO_ID + '.jpg')


class TestTheDownloaderSavesAndFlagsADeepEdgeBand:
    """#179 item 2 as #213's review resolved it (S1): a stitch with a deep edge band is SAVED, ledgered
    downloaded=1, and said loudly on both channels - never refused. The store is an archive: a refused pano
    that Google later retires is lost for good, the limit is uncalibrated, and since #181/#214 both arms
    refuse a frame larger than served before the fan-out, so what reaches this check is the unmeasured
    class the calibration needs to see. Because the file is the resume marker, a flagged pano is said once,
    ever. It is NOT a frame disagreement (S2): no exception, no FrameDisagreementError subclass, which #207
    counts by isinstance."""

    @pytest.mark.parametrize('bottom, right', [(96, 192), (52, 0), (0, 64)])
    def test_a_deep_band_is_saved_and_flagged(self, monkeypatch, tmp_path, capsys, caplog, bottom, right):
        caplog.set_level(logging.WARNING)

        result = download_with_stitch(monkeypatch, tmp_path, framed(1024, 512, bottom=bottom, right=right))

        assert result == DownloadResult.success
        assert pano_file(tmp_path).is_file()
        assert sorted(os.listdir(str(tmp_path / PANO_ID[:2]))) == [PANO_ID + '.jpg'], 'no .part left behind'
        out = capsys.readouterr().out
        assert WARNING in out and PANO_ID in out
        assert 'black' in out and 'edge band' in out
        assert 'frame disagreement' not in out
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and 'edge band' in r.getMessage()]
        assert len(warnings) == 1 and PANO_ID in warnings[0].getMessage()
        assert 'frame disagreement' not in warnings[0].getMessage()
        assert gsv._edge_band_run.saved == 1
        detail, = gsv._edge_band_run.details    # the short form; depths are measured at 1/8 scale, so not asserted
        assert detail.startswith('pano %s: saved with an exactly-black edge band ' % PANO_ID)
        assert detail.endswith('%% bottom / %.1f%% right (limit 5.0%%)' % (100 * right / 1024,)) and ' bottom / ' in detail

    def test_the_flag_fires_after_the_save_so_a_flagging_crash_cannot_withhold_the_pano(self, monkeypatch,
                                                                                         tmp_path):
        """The archive rule in one assertion: whatever the flag does, the pano is already on disk."""
        def crashing_flag(image, pano_id):
            assert pano_file(tmp_path).is_file(), 'flagged before the save'
            raise RuntimeError('flag crashed')

        monkeypatch.setattr(gsv, '_flag_deep_edge_band', crashing_flag)
        with pytest.raises(RuntimeError, match='flag crashed'):
            download_with_stitch(monkeypatch, tmp_path, framed(1024, 512, bottom=96))

        assert pano_file(tmp_path).is_file()

    def test_a_band_at_the_limit_is_saved_silently(self, monkeypatch, tmp_path, capsys, caplog):
        """512 * 0.05 = 25.6 rows; 24 rows (three whole 8-row lines) is under it and is real imagery to us."""
        caplog.set_level(logging.WARNING)

        assert download_with_stitch(monkeypatch, tmp_path, framed(1024, 512, bottom=24)) == DownloadResult.success

        assert pano_file(tmp_path).is_file()
        assert WARNING not in capsys.readouterr().out
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert gsv._edge_band_run.saved == 0

    def test_the_limit_is_read_at_call_time(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(common, 'EDGE_BAND_MAX_FRACTION', 0.25)

        assert download_with_stitch(monkeypatch, tmp_path, framed(1024, 512, bottom=96)) == DownloadResult.success
        assert WARNING not in capsys.readouterr().out
        assert gsv._edge_band_run.saved == 0

    def test_an_upscaled_stitch_keeps_fallback_success_when_flagged(self, monkeypatch, tmp_path):
        """The flag changes no verdict: log.csv column 8 still says what the stitch was."""
        result = download_with_stitch(monkeypatch, tmp_path, framed(1024, 512, bottom=96), upscaled=True)

        assert result == DownloadResult.fallback_success
        assert gsv._edge_band_run.saved == 1

    def test_no_exception_class_subclasses_frame_disagreement_for_bands(self):
        """#207 counts FrameDisagreementError by isinstance; a band must never be in that count."""
        assert not hasattr(gsv, 'EdgeBandError')
        assert not hasattr(gsv, '_refuse_deep_edge_band')


def gsv_pano(pano_id, image):
    return {'pano_id': pano_id, 'source': 'gsv', 'width': image.width, 'height': image.height}


class TestTheEdgeBandCondition:
    """The real image loop (DownloadRunner.download_panorama_images) over a flagged stitch: ledgered `1`,
    one end-of-phase line on both channels, and the `edge-band` condition in the run summary - the channel
    that fails the night (#161), because production delivers only nonzero exits."""

    def run_phase(self, monkeypatch, tmp_path, image, pano_id=PANO_ID, summary=None):
        tmp_path.mkdir(exist_ok=True)
        stub_stitch(monkeypatch, image)
        summary = {'image_stop': None, 'depth_stop': None} if summary is None else summary
        DownloadRunner.download_panorama_images(str(tmp_path), [gsv_pano(pano_id, image)], stop_reasons=summary)
        return summary

    def test_a_flagged_stitch_is_ledgered_said_once_at_phase_end_and_is_a_condition(self, monkeypatch, tmp_path,
                                                                                    capsys, caplog):
        caplog.set_level(logging.WARNING)

        summary = self.run_phase(monkeypatch, tmp_path, framed(1024, 512, bottom=96))

        with open(str(tmp_path / 'pano_id_log.csv'), newline='') as f:
            rows = [r for r in csv.reader(f) if r and r[0] == PANO_ID]
        assert rows and rows[0][1] == '1', 'ledgered downloaded=1: the file is the resume marker'
        assert [c['code'] for c in summary['conditions']] == [DownloadRunner.CONDITION_EDGE_BAND]
        detail = summary['conditions'][0]['detail']
        assert detail.startswith('1 pano(s) saved') and PANO_ID in detail
        out = capsys.readouterr().out
        assert out.count('edge-band condition') == 1
        phase_lines = [r for r in caplog.records if 'edge-band condition' in r.getMessage()]
        assert len(phase_lines) == 1 and phase_lines[0].levelno == logging.WARNING
        assert not [r for r in caplog.records if 'frame disagreement' in r.getMessage()]
        assert 'frame disagreement' not in out

    def test_two_flagged_stitches_are_one_condition_naming_the_first(self, monkeypatch, tmp_path, capsys):
        """The detail names the FIRST pano (the queue's line names the first occurrence); the count says two."""
        image = framed(1024, 512, bottom=96)
        stub_stitch(monkeypatch, image)
        summary = {'image_stop': None, 'depth_stop': None}
        panos = [gsv_pano('firstPanoAAAAAAAAAAAAA', image), gsv_pano('secondPanoAAAAAAAAAAAA', image)]
        monkeypatch.setattr(DownloadRunner.random, 'shuffle', lambda seq: None)

        DownloadRunner.download_panorama_images(str(tmp_path), panos, stop_reasons=summary)

        assert len(summary['conditions']) == 1
        detail = summary['conditions'][0]['detail']
        assert detail.startswith('2 pano(s) saved') and 'firstPano' in detail and 'secondPano' not in detail
        assert capsys.readouterr().out.count(WARNING) == 3, 'one per pano plus the end-of-phase line'

    def test_a_clean_phase_notes_no_condition_and_says_nothing(self, monkeypatch, tmp_path, capsys):
        summary = self.run_phase(monkeypatch, tmp_path, framed(1024, 512, bottom=24))

        assert summary.get('conditions', []) == []    # the key is _write_run_summary's; the phase adds none
        assert 'edge-band' not in capsys.readouterr().out

    def test_a_second_phase_in_the_same_process_reports_only_its_own(self, monkeypatch, tmp_path, capsys):
        """The count is a before/after difference on the per-process memory, so a phase after a flagged one
        reports zero, not the total."""
        self.run_phase(monkeypatch, tmp_path / 'one', framed(1024, 512, bottom=96))

        second = self.run_phase(monkeypatch, tmp_path / 'two', framed(1024, 512, bottom=24),
                                pano_id='clean' + PANO_ID[5:])

        assert second.get('conditions', []) == []
        assert capsys.readouterr().out.count('edge-band condition') == 1

    def test_a_second_flagged_phase_names_its_own_first_pano(self, monkeypatch, tmp_path):
        """Nit 1 of the #213 fix-round review: the count was a difference but `first` was the process's, so a
        second flagged phase in one process named the first phase's pano."""
        image = framed(1024, 512, bottom=96)
        self.run_phase(monkeypatch, tmp_path / 'one', image, pano_id='firstPanoAAAAAAAAAAAAA')

        second = self.run_phase(monkeypatch, tmp_path / 'two', image, pano_id='secondPanoAAAAAAAAAAAA')

        detail = second['conditions'][0]['detail']
        assert detail.startswith('1 pano(s) saved') and 'secondPano' in detail and 'firstPano' not in detail
        assert gsv._edge_band_run.saved == 2

    def test_the_detail_is_short_and_names_the_pano_both_depths_and_the_limit(self, monkeypatch, tmp_path):
        """Nit 6: the run summary's detail is one SNS line, not the per-pano sentence with its pointers."""
        summary = self.run_phase(monkeypatch, tmp_path, framed(1024, 512, bottom=96, right=128))

        detail = summary['conditions'][0]['detail']
        assert detail == ('1 pano(s) saved with a deep edge band; first: pano %s: saved with an exactly-black '
                          'edge band 18.8%% bottom / 12.5%% right (limit 5.0%%)' % PANO_ID)
        assert 'docs/ops.md' not in detail and 'ledgered' not in detail

    def test_run_resets_the_memory_per_run(self, monkeypatch):
        gsv._edge_band_run.saved = 7
        monkeypatch.setattr(DownloadRunner, '_run_phases', lambda *a, **k: None)

        gsv._edge_band_run.details.append('pano x')
        DownloadRunner.run('sidewalk-test.invalid', '/nowhere', skip_depth=True)

        assert gsv._edge_band_run.saved == 0 and gsv._edge_band_run.details == []

    def test_the_condition_is_in_the_runners_vocabulary(self):
        assert DownloadRunner.CONDITION_EDGE_BAND == 'edge-band'
        assert DownloadRunner.CONDITION_EDGE_BAND in DownloadRunner.RUN_CONDITIONS


# --- The sweep ------------------------------------------------------------------------------------------------

STORE_PANOS = {
    'd4PanoAAAAAAAAAAAAAAAA': 'd4.jpg',
    'thPanoAAAAAAAAAAAAAAAA': 'thin_bottom_10.jpg',
    'tfPanoAAAAAAAAAAAAAAAA': 'thin_bottom_15.jpg',
    'clPanoAAAAAAAAAAAAAAAA': 'clean.jpg',
    'dkPanoAAAAAAAAAAAAAAAA': 'dark_scene.jpg',
    'ncPanoAAAAAAAAAAAAAAAA': 'nadir_cap_3.jpg',
    'nbPanoAAAAAAAAAAAAAAAA': 'near_black_band_q40.jpg',
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
    """Every file under the store root - not just the .jpg files - so a sweep that wrote anything but its own
    two files, or renamed one, shows up (N1)."""
    out = {}
    for dirpath, _dirs, files in os.walk(str(root)):
        for name in files:
            path = os.path.join(dirpath, name)
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

        assert summary == scan_black_bands.Summary(scanned=7, clean=4, banded=3, current=0, failed=0,
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
        own = {str(store / scan_black_bands.LEDGER_FILENAME), str(store / scan_black_bands.WORKLIST_FILENAME)}
        assert set(after) == set(before) | own, 'exactly the ledger and the work-list were added'
        changed = sorted(p for p in before if before[p] != after[p])
        assert not changed, ['%s: %r -> %r' % (p, before.get(p), after.get(p)) for p in changed]

    def test_the_ledger_records_every_pano_so_it_is_the_calibration_data(self, tmp_path):
        store = build_store(tmp_path)

        scan_black_bands.scan_store(str(store))

        rows = read_rows(store / scan_black_bands.LEDGER_FILENAME)
        assert {r['pano_id'] for r in rows} == set(STORE_PANOS)
        cap = next(r for r in rows if r['pano_id'].startswith('nc'))
        assert 0.0 < float(cap['bottom_band']) < common.EDGE_BAND_MAX_FRACTION

    def test_every_ledger_row_carries_the_near_black_columns_and_the_cut_stays_exact(self, tmp_path):
        """S4: the near-black band is recorded for every pano (the calibration can see an encoder-moved
        band), but the work-list is cut on the exact band, so the q40 pano is ledgered deep-near and NOT
        listed."""
        store = build_store(tmp_path)

        scan_black_bands.scan_store(str(store))

        rows = read_rows(store / scan_black_bands.LEDGER_FILENAME)
        assert rows and all(r['near_bottom_band'] and r['near_right_band'] for r in rows)
        assert all(float(r['near_bottom_band']) >= float(r['bottom_band']) for r in rows)
        q40 = next(r for r in rows if r['pano_id'].startswith('nb'))
        assert float(q40['bottom_band']) == 0.0
        assert float(q40['near_bottom_band']) > common.EDGE_BAND_MAX_FRACTION
        worklist = read_rows(store / scan_black_bands.WORKLIST_FILENAME)
        assert {r['pano_id'] for r in worklist} == FLAGGED
        assert all(r['near_bottom_band'] for r in worklist), 'the work-list carries the near columns too'

    def test_a_rerun_decodes_nothing_and_writes_the_same_worklist(self, tmp_path, monkeypatch):
        store = build_store(tmp_path)
        scan_black_bands.scan_store(str(store))
        first = (store / scan_black_bands.WORKLIST_FILENAME).read_bytes()

        def no_decode(path):
            raise AssertionError('a finished store must cost no decode: %s' % path)

        monkeypatch.setattr(common, 'edge_band_report_from_file', no_decode)
        summary = scan_black_bands.scan_store(str(store))

        assert summary.current == 7 and summary.scanned == 7
        assert (store / scan_black_bands.WORKLIST_FILENAME).read_bytes() == first

    def test_a_changed_pano_is_scanned_again(self, tmp_path):
        """Keyed on size and mtime as well as id, so a pano a repair pass swapped is judged afresh."""
        store = build_store(tmp_path)
        scan_black_bands.scan_store(str(store))
        swapped = store / 'd4' / 'd4PanoAAAAAAAAAAAAAAAA.jpg'
        shutil.copy(fixture('clean.jpg'), str(swapped))

        summary = scan_black_bands.scan_store(str(store))

        assert summary.current == 6 and summary.clean == 1
        assert {r['pano_id'] for r in read_rows(store / scan_black_bands.WORKLIST_FILENAME)} \
            == FLAGGED - {'d4PanoAAAAAAAAAAAAAAAA'}
        # The swapped pano now has two sound rows; the LAST wins, so a third sweep decodes nothing. (With the
        # first winning it would be rescanned every run - nit 4 of the #213 fix-round review.)
        third = scan_black_bands.scan_store(str(store))
        assert third.current == 7 and third.clean == 0 and third.banded == 0

    def test_a_pano_rewritten_with_the_same_size_is_scanned_again(self, tmp_path, monkeypatch):
        """N6: resume is keyed on mtime as well as size, so the same bytes with a bumped mtime - a copy that
        reset timestamps, a refetch that landed identical bytes - are judged afresh, once."""
        store = build_store(tmp_path)
        scan_black_bands.scan_store(str(store))
        target = store / 'cl' / 'clPanoAAAAAAAAAAAAAAAA.jpg'
        stat = os.stat(str(target))
        os.utime(str(target), ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        decoded = []
        real = common.edge_band_report_from_file

        def spy(path, **kwargs):
            decoded.append(path)
            return real(path, **kwargs)

        monkeypatch.setattr(common, 'edge_band_report_from_file', spy)
        summary = scan_black_bands.scan_store(str(store))

        assert decoded == [str(target)]
        assert summary.current == 6 and summary.clean == 1

    def test_a_budget_leaves_the_rest_unreached_and_the_next_run_finishes(self, tmp_path, monkeypatch):
        store = build_store(tmp_path)
        clock = iter([0.0] + [0.0] * 2 + [1e9] * 100)
        monkeypatch.setattr(scan_black_bands.time, 'monotonic', lambda: next(clock))

        first = scan_black_bands.scan_store(str(store), max_runtime_minutes=1)

        assert first.scanned == 2 and first.unreached == 5
        monkeypatch.undo()
        second = scan_black_bands.scan_store(str(store))
        assert second.current == 2 and second.scanned == 7
        assert {r['pano_id'] for r in read_rows(store / scan_black_bands.WORKLIST_FILENAME)} == FLAGGED

    def test_progress_survives_a_kill(self, tmp_path, monkeypatch):
        """Each verdict is on disk as it lands, so a run killed half way - SIGKILL included, which runs no
        `finally` and closes no file - keeps what it did. Read from disk WHILE the run is still going."""
        store = build_store(tmp_path)
        real = common.edge_band_report_from_file
        seen = []
        on_disk_at_the_third = []

        def killed_on_the_third(path, **kwargs):
            if len(seen) == 2:
                on_disk_at_the_third.append(len(read_rows(store / scan_black_bands.LEDGER_FILENAME)))
                raise KeyboardInterrupt
            seen.append(path)
            return real(path, **kwargs)

        monkeypatch.setattr(common, 'edge_band_report_from_file', killed_on_the_third)
        with pytest.raises(KeyboardInterrupt):
            scan_black_bands.scan_store(str(store))

        assert on_disk_at_the_third == [2]
        assert len(read_rows(store / scan_black_bands.LEDGER_FILENAME)) == 2

    @pytest.mark.parametrize('elapsed, scanned', [(59.999, 7), (60.0, 0)])
    def test_the_budget_is_spent_at_exactly_its_length(self, tmp_path, monkeypatch, elapsed, scanned):
        store = build_store(tmp_path)
        clock = iter([0.0] + [elapsed] * 100)
        monkeypatch.setattr(scan_black_bands.time, 'monotonic', lambda: next(clock))

        summary = scan_black_bands.scan_store(str(store), max_runtime_minutes=1)

        assert (summary.scanned, summary.unreached) == (scanned, 7 - scanned)

    def test_an_unreadable_pano_is_failed_unledgered_and_retried(self, tmp_path, capsys):
        store = build_store(tmp_path)
        (store / 'zz').mkdir()
        (store / 'zz' / 'zzBroken.jpg').write_bytes(b'\xff\xd8 truncated')

        first = scan_black_bands.scan_store(str(store))
        second = scan_black_bands.scan_store(str(store))

        assert first.failed == 1 and second.failed == 1
        assert 'zzBroken' not in (store / scan_black_bands.LEDGER_FILENAME).read_text()
        assert 'FAILED' in capsys.readouterr().out

    def test_a_pano_that_vanishes_between_the_walk_and_the_stat_is_failed_not_fatal(self, tmp_path, monkeypatch,
                                                                                   capsys):
        """N3: a refetch swap's rename or an operator's rm mid-sweep. One missing file must not end a sweep of
        a multi-terabyte store, and it is in no ledger, so a re-run sees whatever is there then."""
        store = build_store(tmp_path)
        victim = str(store / 'cl' / 'clPanoAAAAAAAAAAAAAAAA.jpg')
        real_stat = os.stat
        removed = []

        def stat_after_removal(path, *args, **kwargs):
            # scan_black_bands.os IS os, so this patches os.stat for everyone: delegate everything but the one
            # victim, and never call anything that stats from in here.
            if path == victim and not removed:
                os.remove(victim)
                removed.append(path)
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(scan_black_bands.os, 'stat', stat_after_removal)
        summary = scan_black_bands.scan_store(str(store))

        assert summary.failed == 1 and summary.scanned == 7
        assert summary.scanned == summary.clean + summary.banded + summary.current + summary.failed
        assert 'clPano' not in (store / scan_black_bands.LEDGER_FILENAME).read_text()
        out = capsys.readouterr().out
        assert 'FAILED' in out and 'vanished' in out

    def test_a_torn_row_and_a_repeated_header_are_skipped_counted_and_rescanned(self, tmp_path, capsys):
        """N2: the shapes a kill mid-write and a concatenation leave. Neither is a verdict."""
        store = build_store(tmp_path)
        scan_black_bands.scan_store(str(store))
        ledger = store / scan_black_bands.LEDGER_FILENAME
        rows = read_rows(ledger)
        clean = next(r for r in rows if r['pano_id'].startswith('cl'))
        with open(str(ledger), 'a', newline='') as f:
            f.write(','.join(scan_black_bands.LEDGER_FIELDS) + '\n')         # a repeated header
            f.write(',1,2,1024,512,0.0,0.0,0.0,0.0\n')                        # a row with no pano id
            f.write('%s,%s,%s,1024,512,0.00' % (clean['pano_id'], clean['size'], clean['mtime_ns']))   # torn,
            # and torn WITHOUT its newline: the shape a kill mid-write leaves

        loaded, skipped = scan_black_bands.load_ledger(str(ledger))
        assert skipped == 3
        assert loaded[clean['pano_id']] == clean, 'the torn row did not displace the sound one'
        (store / 'nw').mkdir()
        shutil.copy(fixture('clean.jpg'), str(store / 'nw' / 'nwPanoAAAAAAAAAAAAAAAA.jpg'))   # a new pano
        summary = scan_black_bands.scan_store(str(store))

        assert summary.current == 7 and summary.clean == 1, 'every sound row still resumes; the new pano is scanned'
        assert '3 unreadable ledger row(s) skipped' in capsys.readouterr().out
        # Nit 2 of the #213 fix-round review: the torn line is closed before the new row is appended, so the
        # new pano's row is sound and the torn one is still exactly one skip - not two rows glued into one.
        loaded, skipped = scan_black_bands.load_ledger(str(ledger))
        assert 'nwPanoAAAAAAAAAAAAAAAA' in loaded and skipped == 3
        assert scan_black_bands.scan_store(str(store)).current == 8

    def test_a_ledger_with_another_versions_columns_is_kept_aside_and_started_afresh(self, tmp_path, capsys):
        """A ledger from before the near columns: appending 9-field rows under its 7-field header would read
        every new row with the wrong names. It is renamed, never deleted (a measurement someone took), the
        operator is told, and every pano is scanned again under the current header."""
        store = build_store(tmp_path)
        ledger = store / scan_black_bands.LEDGER_FILENAME
        target = store / 'cl' / 'clPanoAAAAAAAAAAAAAAAA.jpg'
        stat = os.stat(str(target))
        old = ('pano_id,size,mtime_ns,width,height,bottom_band,right_band\n'
               'clPanoAAAAAAAAAAAAAAAA,%d,%d,1024,512,0.0000,0.0000\n' % (stat.st_size, stat.st_mtime_ns))
        ledger.write_text(old)

        summary = scan_black_bands.scan_store(str(store))

        assert summary.current == 0 and summary.scanned == 7
        assert (store / (scan_black_bands.LEDGER_FILENAME + '.stale-1')).read_text() == old
        assert 'kept aside' in capsys.readouterr().out
        rows = read_rows(ledger)
        assert {r['pano_id'] for r in rows} == set(STORE_PANOS)
        assert all(r['near_bottom_band'] for r in rows)
        # A second stale ledger does not overwrite the first set-aside copy.
        ledger.write_text(old)
        scan_black_bands.scan_store(str(store))
        assert (store / (scan_black_bands.LEDGER_FILENAME + '.stale-2')).is_file()

    def test_an_empty_ledger_file_is_started_in_place(self, tmp_path):
        """Nit 3: a file created and never written (a kill between open and the header) is not another
        version - the header goes in place, and no `.stale-` copy of nothing is made."""
        store = build_store(tmp_path)
        ledger = store / scan_black_bands.LEDGER_FILENAME
        ledger.write_bytes(b'')

        summary = scan_black_bands.scan_store(str(store))

        assert summary.scanned == 7 and summary.current == 0
        lines = ledger.read_text().splitlines()
        assert lines[0] == ','.join(scan_black_bands.LEDGER_FIELDS) and len(lines) == 8
        assert not list(store.glob(scan_black_bands.LEDGER_FILENAME + '.stale-*'))
        assert scan_black_bands.scan_store(str(store)).current == 7

    def test_a_ledger_this_tool_did_not_write_is_refused_untouched(self, tmp_path, capsys):
        """SF1 of the #213 fix-round review: `--ledger <store>/pano_id_log.csv` is a typo away, and that file
        is the image phase's resume ledger. The old rule (header != LEDGER_FIELDS) renamed it aside and wrote
        black-band rows in its place. Now anything whose header does not start `pano_id,size,mtime_ns` is
        refused: exit 3, nothing written, nothing moved, the file byte-for-byte as it was."""
        store = build_store(tmp_path)
        resume_ledger = store / 'pano_id_log.csv'
        before = b'pano_id,downloaded,fetched_at\r\nretiredPanoAAAAAAAAAAA,0,\r\n'
        resume_ledger.write_bytes(before)
        paths_before = sorted(p for p in store.rglob('*'))

        with pytest.raises(scan_black_bands.ForeignLedgerError):
            scan_black_bands.scan_store(str(store), ledger_path=str(resume_ledger))
        code = scan_black_bands.main([str(store), '--ledger', str(resume_ledger)])

        assert code == scan_black_bands.EXIT_REFUSED_LEDGER == 3
        assert resume_ledger.read_bytes() == before
        assert sorted(p for p in store.rglob('*')) == paths_before, 'no .stale-*, no work-list, nothing'
        out = capsys.readouterr().out
        assert 'REFUSED' in out and 'pano_id_log.csv' in out and 'pano_id,downloaded,fetched_at' in out

    def test_three_sweeps_write_one_header(self, tmp_path):
        store = build_store(tmp_path)
        for _ in range(3):
            scan_black_bands.scan_store(str(store))

        lines = (store / scan_black_bands.LEDGER_FILENAME).read_text().splitlines()
        assert lines[0] == ','.join(scan_black_bands.LEDGER_FIELDS)
        assert lines.count(lines[0]) == 1

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
        assert (s.scanned, s.clean, s.banded, s.current, s.failed) == (8, 0, 1, 6, 1)


class TestTheSweepsCommandLine:

    def test_main_prints_one_summary_line_and_exits_zero(self, tmp_path, capsys):
        store = build_store(tmp_path)

        code = scan_black_bands.main([str(store)])

        out = capsys.readouterr().out
        assert code == 0
        assert 'Examined 7 panorama(s): 3 with an edge band over 5.0%' in out
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
        # The three deep ones plus the 3% nadir cap; the q40 pano has NO exact band, so even --min-band 0 does
        # not list it - its band is in the ledger's near columns only.
        assert len(read_rows(store / scan_black_bands.WORKLIST_FILENAME)) == 4

    @pytest.mark.parametrize('value', ['-0.1', '1.5', 'x'])
    def test_min_band_must_be_a_fraction(self, tmp_path, value):
        with pytest.raises(SystemExit):
            scan_black_bands.main([str(tmp_path), '--min-band', value])
