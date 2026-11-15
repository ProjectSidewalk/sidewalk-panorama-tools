"""#185 Part 2: a GSV pano Google serves LARGER than the app's frame is kept at Google's frame, as
`<pano_id>.native`, beside an app-frame `<pano_id>.jpg` resampled from it - behind
gsv.KEEP_SERVED_LARGER_ORIGINALS, which ships OFF.

What these tests hold:

* **The switch is off, and off is master.** The pano is refused exactly as before, no original on disk is ever
  looked at, and no request, stat or tripwire is added (TestTheSwitchIsOff, and the request rows in
  tests/test_gsv_photometa_zoom.py::TestRequestsPerPano).
* **The decision is pure and narrow** (TestTheServedLargerPlan): only photometa's own `frame` refusal, Google's
  top level strictly larger on both axes at the same aspect, within MAX_PANO_PIXELS, and fetchable natively.
* **Neither write ever replaces a file** (atomic_output_path(..., overwrite=False)), the original is written
  first and read back before the copy is made, and the copy is ONE generation from the stored original - the
  same bytes whether made the night of the download or rebuilt later from the original on disk.
* **A rebuilt copy is a skip** (zero requests), not a success.

Network-free: photometa is stubbed at gsv._fetch_image_levels, the probe at gsv_tiles._get_response, the tile
fan-out at gsv_tiles._download_tiles, and fetch_pano_image (a GSV composed seam) on gsv.

The fixture is a small analogue of the 13312-wide app record on a pano Google serves at 16384 (ratio 0.8125):
Google's levels SERVED, the app's frame APP, an 4x2 tile grid at zoom 2 instead of 32x16 at zoom 5.
"""

import builtins
import logging
import os
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from downloaders import common, gsv
from downloaders.common import DownloadResult, jpeg_dimensions
from sidewalk_panorama_tools import gsv_tiles
from test_gsv_photometa_zoom import SERIES_13312, SERIES_16384, dimless, pano_info
from test_gsv_stitcher import RED, assert_color, deny_probe, jpeg_bytes, stub_photometa, stub_tiles

PANO = 'servedLargerPanoAAAAAA'
SERVED = [(512, 256), (1024, 512), (2048, 1024)]
APP = (1664, 832)
NATIVE = SERVED[-1]
NATIVE_ZOOM = 2

# One flat colour per tile of the 4x2 zoom-2 grid, keyed on (x, y), so a pixel says which tile it came from.
COLOURS = {(x, y): (30 + 55 * x, 40 + 140 * y, 220 - 50 * x) for x in range(4) for y in range(2)}


# --- helpers -------------------------------------------------------------------------------------------------

def on(monkeypatch):
    monkeypatch.setattr(gsv, 'KEEP_SERVED_LARGER_ORIGINALS', True)


def shard(tmp_path):
    return tmp_path / PANO[:2]


def jpg(tmp_path):
    return shard(tmp_path) / (PANO + '.jpg')


def native(tmp_path):
    return shard(tmp_path) / (PANO + '.native')


def files_in_shard(tmp_path):
    return sorted(os.listdir(str(shard(tmp_path)))) if shard(tmp_path).is_dir() else []


def colour_tiles(monkeypatch, body=None):
    """The fan-out answers every tile with its own colour (or `body(x, y)`); returns the tiles asked for."""
    body = body or (lambda x, y: jpeg_bytes(COLOURS[(x, y)]))
    return stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], body(tile[0], tile[1])))


def no_tiles(monkeypatch):
    async def refuse(tiles):
        pytest.fail('no tile may be requested here')

    monkeypatch.setattr(gsv_tiles, '_download_tiles', refuse)


def no_photometa(monkeypatch):
    def refuse(pano_id, session):
        pytest.fail('photometa must not be asked here')

    monkeypatch.setattr(gsv, '_fetch_image_levels', refuse)


def served_larger(monkeypatch):
    """Photometa reports SERVED, the probe must not be sent, and the tiles carry their colours."""
    asked = stub_photometa(monkeypatch, SERVED)
    deny_probe(monkeypatch)
    tiles = colour_tiles(monkeypatch)
    return asked, tiles


def download(tmp_path, record=None):
    return gsv.download_single_pano(str(tmp_path), record if record is not None else pano_info(*APP, pano_id=PANO))


def stitched_native():
    """The image the fan-out produces at Google's frame: the eight tile colours, at 2048x1024."""
    image = Image.new('RGB', NATIVE)
    for (x, y), colour in COLOURS.items():
        image.paste(Image.new('RGB', (512, 512), colour), (512 * x, 512 * y))
    return image


def place_native(tmp_path, image=None, raw=None):
    """A `<pano_id>.native` already on disk (and no `.jpg`); returns its bytes."""
    shard(tmp_path).mkdir(parents=True, exist_ok=True)
    if raw is not None:
        native(tmp_path).write_bytes(raw)
    else:
        (image or stitched_native()).save(str(native(tmp_path)), 'jpeg')
    return native(tmp_path).read_bytes()


def decoded(source):
    """The RGB pixels of a JPEG at a path or in a buffer."""
    with Image.open(source if isinstance(source, BytesIO) else str(source)) as image:
        return np.asarray(image.convert('RGB'))


# --- step 1: the name ----------------------------------------------------------------------------------------

def test_the_native_original_sits_beside_the_pano_and_is_not_a_jpg():
    path = common.native_original_path(os.path.join('s', 'ab', 'abc.jpg'))

    assert path == os.path.join('s', 'ab', 'abc.native')
    assert not path.endswith('.jpg') and not path.endswith('.jpeg')
    # A CAoS id is base64url with `.` for `=` padding: its trailing dot is part of the id and must survive.
    assert common.native_original_path(os.path.join('s', 'CA', 'CAoSxyz..jpg')) == \
        os.path.join('s', 'CA', 'CAoSxyz..native')


# --- step 2: the decision (pure) -----------------------------------------------------------------------------

def frame(app, sizes, evidence='photometa', refusal='frame', consistent=False, with_sizes=True):
    return gsv.ResolvedFrame(app[0], app[1], len(sizes) - 1, consistent, tuple(sizes[-1]), evidence, refusal,
                             tuple(tuple(s) for s in sizes) if with_sizes else None)


class TestTheServedLargerPlan:
    def test_the_13312_on_16384_case_is_planned_at_the_top_level(self):
        assert gsv._served_larger_plan(frame((13312, 6656), SERIES_16384)) == (16384, 8192, 5)

    def test_the_fixture_analogue_is_planned_at_its_top_level(self):
        assert gsv._served_larger_plan(frame(APP, SERVED)) == (2048, 1024, 2)

    @pytest.mark.parametrize('kwargs', [
        dict(refusal='tile_size'),
        dict(evidence='photometa-dims'),
        dict(evidence='probe', refusal=None, consistent=True),
        dict(refusal=None, consistent=True),
        dict(with_sizes=False),
    ], ids=['tile-size', 'photometa-dims', 'probe', 'consistent', 'no-sizes'])
    def test_only_a_photometa_frame_refusal_is_planned(self, kwargs):
        assert gsv._served_larger_plan(frame((13312, 6656), SERIES_16384, **kwargs)) is None

    @pytest.mark.parametrize('app,sizes', [
        ((16384, 8192), SERIES_16384),      # equal
        ((16384, 8192), SERIES_13312),      # Google serves it SMALLER (#181's shape)
        ((13312, 8192), SERIES_16384),      # wider only
        ((16384, 6656), SERIES_16384),      # taller only
    ], ids=['equal', 'smaller', 'wider-only', 'taller-only'])
    def test_a_frame_not_larger_on_both_axes_is_not_planned(self, app, sizes):
        assert gsv._served_larger_plan(frame(app, sizes)) is None

    def test_a_different_aspect_ratio_is_not_planned(self):
        sizes = SERIES_16384[:-1] + [(16384, 8000)]
        assert gsv.choose_zoom(sizes, 16384, 8000) == (5, True), 'only the aspect rule may refuse this one'
        assert gsv._served_larger_plan(frame((13312, 6656), sizes)) is None

    def test_a_frame_over_the_pixel_ceiling_is_not_planned(self):
        seven_levels = SERIES_16384 + [(32768, 16384)]
        assert gsv._served_larger_plan(frame((26624, 13312), seven_levels)) is None
        # Exactly MAX_PANO_PIXELS is the frame every modern GSV pano is stored at.
        assert 16384 * 8192 == common.MAX_PANO_PIXELS
        assert gsv._served_larger_plan(frame((13312, 6656), SERIES_16384)) is not None

    @pytest.mark.parametrize('sizes', [
        SERIES_16384[2:],                   # cut short at the bottom: the stitcher would fetch zoom 5's grid at 3
        [(512, 256), (16384, 8192)],        # level 0 matches, the top is not at the stitcher's zoom index
    ], ids=['cut-short', 'gap'])
    def test_a_pyramid_whose_top_is_not_fetchable_natively_is_not_planned(self, sizes):
        assert gsv._served_larger_plan(frame((13312, 6656), sizes)) is None

    def test_resolve_frame_hands_the_levels_back_only_on_a_frame_refusal(self, monkeypatch):
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        assert gsv.resolve_frame(pano_info(*APP, pano_id=PANO)).sizes == tuple(SERVED)

        stub_photometa(monkeypatch, SERVED)
        assert gsv.resolve_frame(pano_info(*NATIVE, pano_id=PANO)).sizes is None

        stub_photometa(monkeypatch, SERVED, tile_size=(256, 256))
        refused = gsv.resolve_frame(pano_info(*APP, pano_id=PANO))
        assert refused.refusal == 'tile_size' and refused.sizes is None


# --- step 3: the switch is off ------------------------------------------------------------------------------

class TestTheSwitchIsOff:
    def test_the_shipped_default_is_off(self):
        """A new file kind on the archive is the owner's to turn on, by a reviewed one-line diff."""
        assert gsv.KEEP_SERVED_LARGER_ORIGINALS is False

    def test_off_the_pano_is_refused_exactly_as_before(self, tmp_path, monkeypatch, capsys):
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        no_tiles(monkeypatch)

        with pytest.raises(gsv.FrameDisagreementError) as refused:
            download(tmp_path)

        assert str(refused.value) == ("pano %s: frame disagreement: the app's frame is 1664x832 but Google serves "
                                      "this pano at 2048x1024, and no reported level is that frame's tile grid"
                                      % PANO)
        assert files_in_shard(tmp_path) == []
        assert gsv._served_larger_run.kept == 0
        assert 'frame disagreement' in capsys.readouterr().out

    def test_off_an_original_on_disk_is_never_looked_at(self, tmp_path, monkeypatch):
        original = place_native(tmp_path)
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        no_tiles(monkeypatch)
        touched = []
        with pytest.MonkeyPatch.context() as recorder:
            for module, name in ((os.path, 'isfile'), (os.path, 'exists'), (os.path, 'lexists'), (os, 'stat'),
                                 (os, 'lstat'), (builtins, 'open')):
                real = getattr(module, name)

                def recording(path, *args, _real=real, **kwargs):
                    touched.append(str(path))
                    return _real(path, *args, **kwargs)

                recorder.setattr(module, name, recording)

            with pytest.raises(gsv.FrameDisagreementError):
                download(tmp_path)

        assert str(native(tmp_path)) not in touched
        assert not any(p.endswith('.native') for p in touched)
        assert native(tmp_path).read_bytes() == original
        assert files_in_shard(tmp_path) == [PANO + '.native']

    def test_off_no_tripwire_fires_on_googles_width(self, tmp_path, monkeypatch):
        """The #121 tripwire reads the app's width in resolve_frame, as on master; Google's larger width is
        read only on the keep path, which is off."""
        monkeypatch.setattr(common, 'VIEWER_MAX_PANO_WIDTH', 1700)
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        no_tiles(monkeypatch)
        before = common.ceiling_sightings()

        with pytest.raises(gsv.FrameDisagreementError):
            download(tmp_path)

        assert common.ceiling_sightings() == before


# --- step 4: the keep path ----------------------------------------------------------------------------------

class TestAPanoServedLargerIsKept:
    @pytest.fixture(autouse=True)
    def switch_on(self, monkeypatch):
        on(monkeypatch)

    def test_one_fan_out_at_googles_frame_makes_an_original_and_an_app_frame_copy(self, tmp_path, monkeypatch):
        asked, tiles = served_larger(monkeypatch)

        assert download(tmp_path) == DownloadResult.success

        assert jpeg_dimensions(str(native(tmp_path))) == NATIVE
        assert jpeg_dimensions(str(jpg(tmp_path))) == APP
        assert files_in_shard(tmp_path) == [PANO + '.jpg', PANO + '.native']
        assert len(asked) == 1
        assert len(tiles) == 8 and all('&zoom=%d&' % NATIVE_ZOOM in url for _x, _y, url in tiles)

    def test_the_copy_is_the_whole_pano_resampled_not_its_corner(self, tmp_path, monkeypatch):
        """(1450, 300) of the copy is x = 1784 of Google's frame, tile (3, 0); a corner crop of the original at
        the app's frame would show tile (2, 0) there instead."""
        served_larger(monkeypatch)

        download(tmp_path)

        with Image.open(str(jpg(tmp_path))) as copy:
            assert_color(copy.convert('RGB').getpixel((1450, 300)), COLOURS[(3, 0)])
            assert_color(copy.convert('RGB').getpixel((1450, 700)), COLOURS[(3, 1)])

    def test_the_copy_is_exactly_one_generation_from_the_stored_original(self, tmp_path, monkeypatch):
        served_larger(monkeypatch)

        download(tmp_path)

        with Image.open(str(native(tmp_path))) as stored:
            expected = stored.convert('RGB').resize(APP, Image.LANCZOS)
        buffer = BytesIO()
        expected.save(buffer, 'jpeg')
        buffer.seek(0)
        assert np.array_equal(decoded(jpg(tmp_path)), decoded(buffer))

    def test_the_original_is_written_first(self, tmp_path, monkeypatch):
        served_larger(monkeypatch)

        def fail(*args, **kwargs):
            raise RuntimeError('the copy failed')

        monkeypatch.setattr(gsv, '_write_app_frame_copy', fail)

        with pytest.raises(RuntimeError, match='the copy failed'):
            download(tmp_path)

        assert files_in_shard(tmp_path) == [PANO + '.native']
        assert gsv._served_larger_run.kept == 0

    @pytest.mark.parametrize('failure', ['tile', 'pushback', 'black'])
    def test_a_failed_fan_out_writes_nothing(self, tmp_path, monkeypatch, failure):
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        black = jpeg_bytes((0, 0, 0))
        expected = {'tile': RuntimeError, 'pushback': gsv.TilePushbackError,
                    'black': gsv.StitchedPanoMostlyBlackError}[failure]

        def result(tile):
            x, y = tile[0], tile[1]
            if failure == 'black':
                return x, y, black
            if (x, y) == (1, 1):
                return RuntimeError('tile timed out') if failure == 'tile' else gsv.TilePushbackError(429, x, y)
            return x, y, jpeg_bytes(COLOURS[(x, y)])

        stub_tiles(monkeypatch, result)

        with pytest.raises(expected):
            download(tmp_path)

        assert files_in_shard(tmp_path) == []
        assert gsv._served_larger_run.kept == 0

    def test_an_original_that_reads_back_at_another_size_is_not_ledgered(self, tmp_path, monkeypatch):
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        monkeypatch.setattr(gsv, 'fetch_pano_image',
                            lambda pano_id, w, h, zoom: gsv.StitchedPano(Image.new('RGB', (1024, 512), RED), 0,
                                                                         False))

        with pytest.raises(gsv.KeptOriginalError) as error:
            download(tmp_path)

        assert not isinstance(error.value, gsv.FrameDisagreementError), \
            '#207 counts FrameDisagreementError by isinstance; this is not a frame disagreement'
        assert '1024x512' in str(error.value) and '2048x1024' in str(error.value)
        assert not jpg(tmp_path).exists()
        assert gsv._served_larger_run.kept == 0

    def test_an_original_that_appears_during_the_fan_out_is_not_replaced(self, tmp_path, monkeypatch):
        """The check before the fan-out is minutes old by the rename; the rename itself must refuse."""
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)

        def fan_out_beside_another_writer(pano_id, w, h, zoom):
            native(tmp_path).write_bytes(b'another writer')
            return gsv.StitchedPano(stitched_native(), 0, False)

        monkeypatch.setattr(gsv, 'fetch_pano_image', fan_out_beside_another_writer)

        with pytest.raises(FileExistsError):
            download(tmp_path)

        assert native(tmp_path).read_bytes() == b'another writer'
        assert files_in_shard(tmp_path) == [PANO + '.native']

    def test_an_unusable_original_already_on_disk_is_never_overwritten(self, tmp_path, monkeypatch, capsys):
        junk = place_native(tmp_path, raw=b'not a jpeg at all')
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        no_tiles(monkeypatch)

        with pytest.raises(gsv.FrameDisagreementError) as refused:
            download(tmp_path)

        assert str(native(tmp_path)) in str(refused.value) and 'left untouched' in str(refused.value)
        assert native(tmp_path).read_bytes() == junk
        assert files_in_shard(tmp_path) == [PANO + '.native']
        assert 'frame disagreement' in capsys.readouterr().out

    def test_the_edge_band_flag_runs_once_on_the_copy(self, tmp_path, monkeypatch):
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)

        def banded(x, y):
            tile = Image.new('RGB', (512, 512), COLOURS[(x, y)])
            if y == 1:
                tile.paste(Image.new('RGB', (512, 256)), (0, 256))     # the bottom quarter of the frame black
            buffer = BytesIO()
            tile.save(buffer, 'jpeg')
            return buffer.getvalue()

        colour_tiles(monkeypatch, banded)
        flagged = []
        real_flag = gsv._flag_deep_edge_band
        monkeypatch.setattr(gsv, '_flag_deep_edge_band',
                            lambda image, pano_id: (flagged.append(image.size), real_flag(image, pano_id)))

        download(tmp_path)

        assert flagged == [APP]
        assert gsv._edge_band_run.saved == 1

    def test_a_clean_kept_pano_flags_no_edge_band(self, tmp_path, monkeypatch):
        served_larger(monkeypatch)

        download(tmp_path)

        assert gsv._edge_band_run.saved == 0

    def test_the_display_copy_derives_from_the_copy(self, tmp_path, monkeypatch):
        served_larger(monkeypatch)
        handed = []
        real_write = gsv.write_downscaled_sidecar
        monkeypatch.setattr(gsv, 'write_downscaled_sidecar',
                            lambda image, path, *a, **k: (handed.append((image.size, path)),
                                                          real_write(image, path, *a, **k))[1])

        download(tmp_path)
        assert handed == [] and not [f for f in files_in_shard(tmp_path) if '.w' in f]

        monkeypatch.setattr(common, 'WRITE_DISPLAY_COPIES', True)
        monkeypatch.setattr(common, 'DOWNSCALED_MAX_WIDTH', 1024)
        os.remove(str(jpg(tmp_path)))
        os.remove(str(native(tmp_path)))
        download(tmp_path)

        assert handed == [(APP, str(jpg(tmp_path)))]
        assert jpeg_dimensions(str(shard(tmp_path) / (PANO + '.w1024.jpg'))) == (1024, 512)

    def test_it_says_so_on_both_channels_without_the_refusal_phrase(self, tmp_path, monkeypatch, capsys, caplog):
        served_larger(monkeypatch)

        with caplog.at_level(logging.INFO):
            download(tmp_path)

        out = [line for line in capsys.readouterr().out.splitlines() if PANO + '.native' in line]
        assert len(out) == 1 and '#185' in out[0] and 'WARNING' in out[0]
        assert '1664x832' in out[0] and '2048x1024' in out[0]
        logged = [r.getMessage() for r in caplog.records if PANO + '.native' in r.getMessage()]
        assert len(logged) == 2
        assert out[0] in logged
        step_f = [m for m in logged if m != out[0]][0]
        assert 'kept the 2048x1024 original' in step_f and '#185' not in step_f
        assert not [r for r in caplog.records if 'frame disagreement' in r.getMessage()]
        assert gsv._served_larger_run.kept == 1
        assert PANO in gsv._served_larger_run.details[0]

    def test_the_tripwire_reads_the_width_about_to_be_stored(self, tmp_path, monkeypatch, capsys):
        """#121's meaning is the width that lands on the store: here Google's, 2048 - not the app's 1664."""
        monkeypatch.setattr(common, 'VIEWER_MAX_PANO_WIDTH', 1700)
        served_larger(monkeypatch)
        before = common.ceiling_sightings()

        download(tmp_path)

        assert common.ceiling_sightings() == before + 1
        wide = [line for line in capsys.readouterr().out.splitlines() if 'viewer ceiling' in line]
        assert len(wide) == 1 and '2048 px wide' in wide[0]


# --- step 5: a copy rebuilt from an original already on disk --------------------------------------------------

class TestTheCopyIsRebuiltFromAKeptOriginal:
    @pytest.fixture(autouse=True)
    def switch_on(self, monkeypatch):
        on(monkeypatch)

    def test_an_original_without_a_copy_costs_zero_requests_and_is_a_skip(self, tmp_path, monkeypatch, capsys):
        original = place_native(tmp_path)
        no_photometa(monkeypatch)
        deny_probe(monkeypatch)
        no_tiles(monkeypatch)

        assert download(tmp_path) == DownloadResult.skipped

        assert jpeg_dimensions(str(jpg(tmp_path))) == APP
        assert native(tmp_path).read_bytes() == original
        assert gsv._served_larger_run.kept == 1
        lines = [line for line in capsys.readouterr().out.splitlines() if PANO + '.native' in line]
        assert len(lines) == 1 and 'rebuilt' in lines[0]

    def test_night_and_recovery_make_the_same_copy(self, tmp_path, monkeypatch):
        served_larger(monkeypatch)
        download(tmp_path)
        night = jpg(tmp_path).read_bytes()
        os.remove(str(jpg(tmp_path)))
        no_photometa(monkeypatch)
        no_tiles(monkeypatch)

        assert download(tmp_path) == DownloadResult.skipped

        assert jpg(tmp_path).read_bytes() == night

    def test_an_original_that_is_not_whole_is_left_alone(self, tmp_path, monkeypatch, capsys, caplog):
        buffer = BytesIO()
        stitched_native().save(buffer, 'jpeg')
        whole = buffer.getvalue()
        cut = place_native(tmp_path, raw=whole[:len(whole) * 2 // 3])
        assert jpeg_dimensions(str(native(tmp_path))) == NATIVE, 'the header survives the cut'
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        no_tiles(monkeypatch)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(gsv.FrameDisagreementError) as refused:
                download(tmp_path)

        assert 'left untouched' in str(refused.value)
        assert native(tmp_path).read_bytes() == cut
        assert not jpg(tmp_path).exists()
        out = [line for line in capsys.readouterr().out.splitlines()
               if str(native(tmp_path)) in line and 'is not usable' in line]
        logged = [r.getMessage() for r in caplog.records
                  if str(native(tmp_path)) in r.getMessage() and 'is not usable' in r.getMessage()]
        assert len(out) == 1 and len(logged) == 1
        assert gsv._served_larger_run.kept == 0

    @pytest.mark.parametrize('size', [APP, (1024, 512), (2048, 1000)], ids=['equal', 'smaller', 'other-aspect'])
    def test_an_original_that_is_not_larger_at_the_same_aspect_is_left_alone(self, tmp_path, monkeypatch, capsys,
                                                                             size):
        original = place_native(tmp_path, Image.new('RGB', size, RED))
        stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        no_tiles(monkeypatch)

        with pytest.raises(gsv.FrameDisagreementError):
            download(tmp_path)

        assert native(tmp_path).read_bytes() == original
        assert not jpg(tmp_path).exists()
        out = capsys.readouterr().out
        assert '%dx%d' % size in out and 'not usable' in out
        assert gsv._served_larger_run.kept == 0

    def test_a_record_with_no_dims_never_looks_for_an_original(self, tmp_path, monkeypatch):
        """#184's path is unchanged: there is no app frame to rebuild a copy at."""
        record = dimless(pano_id=PANO)
        place_native(tmp_path)
        asked = stub_photometa(monkeypatch, SERVED)
        deny_probe(monkeypatch)
        colour_tiles(monkeypatch)

        assert download(tmp_path, record) == DownloadResult.success

        assert asked == [PANO]
        assert jpeg_dimensions(str(jpg(tmp_path))) == NATIVE, "Google's top level is the frame (#184)"
        assert gsv._served_larger_run.kept == 0
