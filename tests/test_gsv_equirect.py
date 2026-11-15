"""sidewalk_panorama_tools.gsv_tiles.fetch_equirect: the one-call GSV fetch for consumers (#236, part of #222).

fetch_equirect composes the pieces the nightly scraper composes with its ledgers - the two-tile probe, the
three-tile frame check and the tile fan-out - into one call that never writes and never retries a refusal.
What is pinned here:

* **The composition**: the request order and count (probe 2, frame check 3, then the grid), a retired pano as
  PanoGoneError rather than a frame disagreement, a frame Google serves larger or smaller refused before the
  fan-out, a refused probe or tile raised unchanged and never retried.
* **An explicit zoom**: always probed; only zoom 5 against a probe that saw imagery at zoom 3 alone is a
  ZoomDisagreementError (the probe cannot name zoom 4, so zoom 4 is fetched on a five-level pano).
* **max_width**: one resample of the zoom's own imagery, the aspect kept, `upscaled` judged against the
  returned width, and the grid identity that path rests on, swept over every width.
* **Refusals at zero requests**: photosphere ids, URL-shaped ids, and bad frames, zooms and widths.
* **The event loop**: the sync name refuses inside a running loop without leaving a coroutine unawaited; the
  async name works there, keeps each fetch's refusal to itself under the real fan-out, resets the fan-out
  state in the awaiting task, and runs the blocking work off the loop thread.
* **One stitch path**: the private async tile fetch returns what fetch_pano_image returns.

Network-free. Every request is stubbed on gsv_tiles, the module that looks the names up: the probe and frame
check at _get_response, the tiles at _download_tiles (or, for the real fan-out, at _request_tile). The stubs
use the module's own grid arithmetic, so these tests pin the composition, not the arithmetic, which
tests/test_gsv_stitcher.py pins against observed photometa.
"""

import ast
import asyncio
import gc
import os
import threading
import warnings
from io import BytesIO

import aiohttp
import numpy
import pytest
import requests
from PIL import Image

from downloaders import gsv
from sidewalk_panorama_tools import gsv_tiles
from test_gsv_photosphere import AF1Q_PANO
from test_gsv_stitcher import (BLACK_BODY, BLUE, RED, assert_color, cbk_query, jpeg_bytes, probe_http_error,
                               probe_retry_error)

PANO = 'equirectPanoAAAAAAAAAA'
REFUSED_PANO = 'refusedPanoAAAAAAAAAAA'
SERVED_PANO = 'servedPanoBBBBBBBBBBBB'

RED_TILE = jpeg_bytes(RED)
BLUE_TILE = jpeg_bytes(BLUE)
RED_SMALL_TILE = jpeg_bytes(RED, (256, 256))
BLUE_SMALL_TILE = jpeg_bytes(BLUE, (256, 256))

NEW_PUBLIC_NAMES = ('PHOTOSPHERE_ID_PREFIX', 'PanoGoneError', 'PhotosphereUnsupportedError',
                    'ZoomDisagreementError', 'fetch_equirect', 'fetch_equirect_async', 'is_photosphere_id')


def triples(urls):
    """(zoom, x, y) of each cbk URL, in order."""
    return [(cbk_query(url, 'zoom'), cbk_query(url, 'x'), cbk_query(url, 'y')) for url in urls]


class FakeCbk:
    """cbk as these tests need it: a pano served at `served` (its full frame) with imagery at `levels`.

    A tile has imagery when its zoom is one of `levels` and (x, y) is inside the served pano's grid at that
    zoom; it is red on the left half of the grid and blue on the right. Anything else is the committed
    out-of-range body, exactly black. A retired pano is levels=(); a five-level pano is levels=(0, 1, 2, 3, 4).

    Patches gsv_tiles._get_response (the probe and the frame check) and, unless stub_tiles=False,
    gsv_tiles._download_tiles (the fan-out). `error_for(url)` may return an exception for the probe to raise;
    `tile_result(tile)` may return what the fan-out hands back for a tile (bytes, a triple, or an exception).
    """

    def __init__(self, monkeypatch, served=(1024, 512), levels=(0, 1, 2, 3, 4, 5), stub_tiles=True,
                 error_for=None, tile_result=None):
        self.served = served
        self.levels = set(levels)
        self.error_for = error_for
        self.tile_result = tile_result
        self.probe_urls = []
        self.probe_threads = []
        self.tiles = []
        self.fanouts = []
        self.download_calls = 0
        monkeypatch.setattr(gsv_tiles, '_get_response', self._get_response)
        if stub_tiles:
            monkeypatch.setattr(gsv_tiles, '_download_tiles', self._download_tiles)

    def has_imagery(self, zoom, x, y):
        if zoom not in self.levels:
            return False
        tiles_x, tiles_y = gsv_tiles._tile_grid(self.served[0], self.served[1], zoom)
        return 0 <= x < tiles_x and 0 <= y < tiles_y

    def body(self, zoom, x, y, small=False):
        if not self.has_imagery(zoom, x, y):
            return BLACK_BODY
        tiles_x, _tiles_y = gsv_tiles._tile_grid(self.served[0], self.served[1], zoom)
        left = x < tiles_x / 2.0
        if small:
            return RED_SMALL_TILE if left else BLUE_SMALL_TILE
        return RED_TILE if left else BLUE_TILE

    def body_for_url(self, url):
        return self.body(cbk_query(url, 'zoom'), cbk_query(url, 'x'), cbk_query(url, 'y'))

    def _get_response(self, url, session, stream=False):
        self.probe_urls.append(url)
        self.probe_threads.append(threading.get_ident())
        if self.error_for is not None:
            error = self.error_for(url)
            if error is not None:
                raise error
        return BytesIO(self.body_for_url(url))

    async def _download_tiles(self, tiles):
        self.download_calls += 1
        self.tiles.extend(tiles)
        self.fanouts.append(gsv_tiles._TILE_FANOUT.get())
        results = []
        for x, y, url in tiles:
            if self.tile_result is not None:
                result = self.tile_result((x, y, url))
                if isinstance(result, bytes):
                    result = (x, y, result)
            else:
                result = (x, y, self.body_for_url(url))
            results.append(result)
        return results


def tile_triples(cbk):
    return triples(url for _x, _y, url in cbk.tiles)


def real_fan_out(monkeypatch, answer):
    """Run the real fan-out (_download_tiles, _download_tile, _fetch_tile) with only the one HTTP request
    behind it stubbed: `answer(tile)` returns the (x, y, bytes) triple or an exception to raise. Records every
    tile that reached the request. Creating the aiohttp session opens no socket."""
    requested = []

    async def fake_request_tile(session, tile):
        requested.append(tile)
        await asyncio.sleep(0.001)
        result = answer(tile)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(gsv_tiles, '_request_tile', fake_request_tile)
    return requested


def refuse_one_pano(tile):
    """`answer` for real_fan_out: REFUSED_PANO's tiles answer 429, everyone else's are red."""
    x, y, url = tile
    if '&panoid=%s' % REFUSED_PANO in url:
        return gsv_tiles.TilePushbackError(429, x, y)
    return x, y, RED_TILE


def record_stitches(monkeypatch):
    """Wrap gsv_tiles._stitch_tiles, calling through: the final_dims each stitch was asked for, and its thread."""
    calls = []
    real = gsv_tiles._stitch_tiles

    def wrapper(tile_results, zoom_dims, final_dims):
        calls.append((tuple(final_dims), threading.get_ident()))
        return real(tile_results, zoom_dims, final_dims)

    monkeypatch.setattr(gsv_tiles, '_stitch_tiles', wrapper)
    return calls


def count_resizes(monkeypatch):
    """Count PIL resamples, calling through."""
    sizes = []
    real = Image.Image.resize

    def wrapper(self, size, *args, **kwargs):
        sizes.append(tuple(size))
        return real(self, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, 'resize', wrapper)
    return sizes


# --- the composition ----------------------------------------------------------------------------------------

class TestTheComposition:

    def test_a_live_pano_costs_two_probes_three_frame_checks_and_its_grid(self, monkeypatch):
        cbk = FakeCbk(monkeypatch)

        stitched = gsv_tiles.fetch_equirect(PANO, 1024, 512)

        assert isinstance(stitched, gsv_tiles.StitchedPano)
        assert isinstance(stitched.image, Image.Image)
        assert stitched.image.size == (1024, 512)
        assert_color(stitched.image.getpixel((100, 256)), RED)
        assert_color(stitched.image.getpixel((900, 256)), BLUE)
        assert stitched.undersized_tiles == 0
        assert stitched.upscaled is False
        assert triples(cbk.probe_urls) == [(3, 0, 0), (5, 0, 0), (5, 2, 0), (5, 0, 1), (5, 1, 0)]
        assert tile_triples(cbk) == [(5, 0, 0), (5, 1, 0)]
        assert cbk.download_calls == 1

    def test_a_retired_pano_is_gone_not_a_frame_disagreement(self, monkeypatch):
        cbk = FakeCbk(monkeypatch, levels=())

        with pytest.raises(gsv_tiles.PanoGoneError) as raised:
            gsv_tiles.fetch_equirect(PANO, 1024, 512)

        assert isinstance(raised.value, ValueError)
        assert not isinstance(raised.value, gsv_tiles.FrameDisagreementError)
        assert raised.value.pano_id == PANO
        assert len(cbk.probe_urls) == 2
        assert cbk.tiles == []

    @pytest.mark.parametrize('served,requests_made', [((2048, 1024), 3), ((1024, 1024), 4)],
                             ids=['wider', 'taller-only'])
    def test_a_pano_served_larger_than_the_frame_is_refused_before_the_fan_out(self, monkeypatch, served,
                                                                                requests_made):
        cbk = FakeCbk(monkeypatch, served=served)

        with pytest.raises(gsv_tiles.FrameDisagreementError) as raised:
            gsv_tiles.fetch_equirect(PANO, 1024, 512)

        assert str(raised.value).startswith('frame disagreement')
        assert 'larger' in str(raised.value)
        assert len(cbk.probe_urls) == requests_made
        assert cbk.tiles == []

    def test_a_pano_served_smaller_than_the_frame_is_refused_before_the_fan_out(self, monkeypatch):
        cbk = FakeCbk(monkeypatch, served=(1024, 512))

        with pytest.raises(gsv_tiles.FrameDisagreementError) as raised:
            gsv_tiles.fetch_equirect(PANO, 2048, 1024)

        assert 'smaller' in str(raised.value)
        assert len(cbk.probe_urls) == 5
        assert cbk.tiles == []

    @pytest.mark.parametrize('error,reason', [(probe_http_error(403), 'HTTP 403'),
                                              (probe_retry_error(429), 'HTTP 429')],
                             ids=['403-HTTPError', '429-RetryError'])
    def test_a_refused_probe_is_raised_unchanged(self, monkeypatch, error, reason):
        cbk = FakeCbk(monkeypatch, error_for=lambda url: error)

        with pytest.raises(Exception) as raised:
            gsv_tiles.fetch_equirect(PANO, 1024, 512)

        assert raised.value is error
        assert gsv_tiles.pushback_reason(raised.value) == reason
        assert not isinstance(raised.value, gsv_tiles.PanoGoneError)
        assert len(cbk.probe_urls) == 1
        assert cbk.tiles == []

    def test_a_refused_tile_is_raised_and_never_retried(self, monkeypatch):
        cbk = FakeCbk(monkeypatch, tile_result=lambda tile: gsv_tiles.TilePushbackError(429, tile[0], tile[1]))

        with pytest.raises(gsv_tiles.TilePushbackError) as raised:
            gsv_tiles.fetch_equirect(PANO, 1024, 512)

        assert cbk.download_calls == 1
        assert raised.value.total == 2

    def test_a_failed_tile_fails_the_pano(self, monkeypatch):
        boom = aiohttp.ClientConnectionError('boom')

        def tile_result(tile):
            return boom if tile[:2] == (1, 0) else RED_TILE

        FakeCbk(monkeypatch, tile_result=tile_result)

        with pytest.raises(aiohttp.ClientConnectionError) as raised:
            gsv_tiles.fetch_equirect(PANO, 1024, 512)
        assert raised.value is boom

    def test_it_writes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        FakeCbk(monkeypatch)

        gsv_tiles.fetch_equirect(PANO, 1024, 512, max_width=256)

        assert os.listdir(tmp_path) == []


# --- an explicit zoom ---------------------------------------------------------------------------------------

class TestAnExplicitZoom:

    def test_an_explicit_zoom_still_probes_so_a_retired_pano_is_gone(self, monkeypatch):
        cbk = FakeCbk(monkeypatch, levels=())

        with pytest.raises(gsv_tiles.PanoGoneError):
            gsv_tiles.fetch_equirect(PANO, 1024, 512, zoom=5)

        assert len(cbk.probe_urls) == 2

    def test_zoom_5_on_a_pano_whose_zoom_5_corner_is_black_is_a_zoom_disagreement(self, monkeypatch):
        cbk = FakeCbk(monkeypatch, levels=(0, 1, 2, 3))

        with pytest.raises(gsv_tiles.ZoomDisagreementError) as raised:
            gsv_tiles.fetch_equirect(PANO, 1024, 512, zoom=5)

        assert isinstance(raised.value, ValueError)
        assert not isinstance(raised.value, gsv_tiles.FrameDisagreementError)
        assert (raised.value.pano_id, raised.value.zoom, raised.value.probed) == (PANO, 5, 3)
        assert 'zoom=None' in str(raised.value) and 'zoom=4' in str(raised.value)
        assert len(cbk.probe_urls) == 2
        assert cbk.tiles == []

    def test_zoom_4_is_fetched_on_a_pano_the_probe_can_only_call_zoom_3(self, monkeypatch):
        """A five-level pano (native zoom 4): the probe sees the zoom-5 corner black and says 3, and an
        explicit zoom 4 is exactly what the caller should be able to ask for."""
        cbk = FakeCbk(monkeypatch, served=(5376, 2688), levels=(0, 1, 2, 3, 4))

        stitched = gsv_tiles.fetch_equirect(PANO, 5376, 2688, zoom=4)

        assert stitched.image.size == (5376, 2688)
        assert stitched.upscaled is False
        assert [z for z, _x, _y in triples(cbk.probe_urls[2:])] == [4, 4, 4]
        assert len(cbk.tiles) == 11 * 6
        assert {z for z, _x, _y in tile_triples(cbk)} == {4}

    def test_a_lower_zoom_than_the_frames_own_is_fetched_and_flagged_upscaled(self, monkeypatch):
        cbk = FakeCbk(monkeypatch, served=(8192, 4096))

        stitched = gsv_tiles.fetch_equirect(PANO, 8192, 4096, zoom=3)

        assert len(cbk.tiles) == 32
        assert {z for z, _x, _y in tile_triples(cbk)} == {3}
        assert stitched.image.size == (8192, 4096)
        assert stitched.upscaled is True

    @pytest.mark.parametrize('zoom', [0, 2, 6, -1, 4.0, 4.5, '5', True, numpy.bool_(True)],
                             ids=lambda z: '%s-%r' % (type(z).__name__, z))
    def test_a_zoom_that_is_not_3_4_or_5_is_refused_before_any_request(self, monkeypatch, zoom):
        cbk = FakeCbk(monkeypatch)

        with pytest.raises(ValueError):
            gsv_tiles.fetch_equirect(PANO, 1024, 512, zoom=zoom)

        assert cbk.probe_urls == [] and cbk.tiles == []

    def test_a_numpy_integer_is_accepted(self, monkeypatch):
        FakeCbk(monkeypatch, served=(4096, 2048))

        stitched = gsv_tiles.fetch_equirect(PANO, numpy.int64(4096), numpy.int64(2048), zoom=numpy.int64(3))

        assert stitched.image.size == (4096, 2048)


# --- max_width ----------------------------------------------------------------------------------------------

class TestMaxWidth:

    def test_it_downsizes_keeping_the_aspect(self, monkeypatch):
        FakeCbk(monkeypatch)

        stitched = gsv_tiles.fetch_equirect(PANO, 1024, 512, max_width=256)

        assert stitched.image.size == (256, 128)
        assert_color(stitched.image.getpixel((25, 64)), RED)
        assert_color(stitched.image.getpixel((230, 64)), BLUE)

    def test_the_height_is_the_nearest_pixel(self, monkeypatch):
        FakeCbk(monkeypatch, served=(1024, 500))

        stitched = gsv_tiles.fetch_equirect(PANO, 1024, 500, max_width=100)

        assert stitched.image.size == (100, 49)          # 500 * 100 / 1024 = 48.8

    @pytest.mark.parametrize('max_width', [1024, 4096])
    def test_a_max_width_at_or_above_the_frame_changes_nothing(self, monkeypatch, max_width):
        FakeCbk(monkeypatch)
        stitches = record_stitches(monkeypatch)

        stitched = gsv_tiles.fetch_equirect(PANO, 1024, 512, max_width=max_width)

        assert stitched.image.size == (1024, 512)
        assert [dims for dims, _thread in stitches] == [(1024, 512)]
        assert stitched.upscaled is False

    @pytest.mark.parametrize('max_width,upscaled', [(None, True), (6000, True), (4097, True), (4096, False),
                                                    (1024, False)])
    def test_upscaled_is_judged_against_the_returned_width(self, monkeypatch, max_width, upscaled):
        """A zoom-3 fetch of an 8192 frame holds 4096 columns of imagery: anything wider is an upscale."""
        FakeCbk(monkeypatch, served=(8192, 4096))

        stitched = gsv_tiles.fetch_equirect(PANO, 8192, 4096, zoom=3, max_width=max_width)

        assert stitched.upscaled is upscaled
        assert stitched.image.size[0] == (8192 if max_width is None else max_width)

    @pytest.mark.parametrize('max_width', [4096, 2048, 6000])
    def test_a_max_width_below_the_frame_stitches_the_zooms_own_frame_once(self, monkeypatch, max_width):
        """The tiles are stitched at the zoom's own frame, never upscaled to the full frame first, so the output
        is at most ONE resample of the imagery fetched (none when the zoom's frame is the output)."""
        FakeCbk(monkeypatch, served=(8192, 4096))
        stitches = record_stitches(monkeypatch)
        resizes = count_resizes(monkeypatch)

        stitched = gsv_tiles.fetch_equirect(PANO, 8192, 4096, zoom=3, max_width=max_width)

        assert [dims for dims, _thread in stitches] == [(4096, 2048)]
        assert stitched.image.size == (max_width, max_width // 2)
        if max_width == 4096:
            assert resizes == []
            plain = gsv_tiles.fetch_pano_image(PANO, 4096, 2048, 3)
            assert stitched.image.tobytes() == plain.image.tobytes()
        else:
            assert resizes == [(max_width, max_width // 2)]

    def test_undersized_tiles_survives_the_resize(self, monkeypatch):
        def small(tile):
            x, _y, _url = tile
            return RED_SMALL_TILE if x == 0 else BLUE_SMALL_TILE

        FakeCbk(monkeypatch, tile_result=small)

        assert gsv_tiles.fetch_equirect(PANO, 1024, 512).undersized_tiles == 2
        assert gsv_tiles.fetch_equirect(PANO, 1024, 512, max_width=256).undersized_tiles == 2

    def test_the_zoom_native_frame_has_the_frames_grid(self):
        """What the max_width path rests on: at every zoom a frame can be fetched at, the zoom's own frame
        (_dims_at_zoom) asks for exactly the frame's grid, and is its own frame at that zoom."""
        pairs = 0
        for width in range(513, 16385):
            height = (width + 1) // 2
            for zoom in range(3, gsv_tiles._pano_max_zoom(width) + 1):
                native = gsv_tiles._dims_at_zoom(width, height, zoom)
                assert gsv_tiles._tile_grid(native[0], native[1], zoom) == gsv_tiles._tile_grid(width, height, zoom), \
                    (width, height, zoom)
                assert gsv_tiles._dims_at_zoom(native[0], native[1], zoom) == native, (width, height, zoom)
                pairs += 1
        assert pairs == 34816

    @pytest.mark.parametrize('max_width', [0, -1, 1.5, True, '8192'], ids=repr)
    def test_a_bad_max_width_is_refused_before_any_request(self, monkeypatch, max_width):
        cbk = FakeCbk(monkeypatch)

        with pytest.raises(ValueError):
            gsv_tiles.fetch_equirect(PANO, 1024, 512, max_width=max_width)

        assert cbk.probe_urls == []


# --- refused before any request -----------------------------------------------------------------------------

class TestRefusedBeforeAnyRequest:

    @pytest.mark.parametrize('pano_id', [AF1Q_PANO, 'CAoSnotAPhotoId', 'CAoS.with.dots'],
                             ids=['AF1Qip-photosphere', 'undecodable', 'not-base64url'])
    def test_a_photosphere_id_is_refused_by_name(self, monkeypatch, pano_id):
        cbk = FakeCbk(monkeypatch)

        with pytest.raises(gsv_tiles.PhotosphereUnsupportedError) as raised:
            gsv_tiles.fetch_equirect(pano_id, 16384, 8192)

        assert isinstance(raised.value, ValueError)
        assert '#223' in str(raised.value) and 'photosphere' in str(raised.value)
        assert 'photo host' not in str(raised.value)
        assert raised.value.pano_id == pano_id
        assert cbk.probe_urls == []

    @pytest.mark.parametrize('pano_id', [None, '', 123, b'abc', 'abc&zoom=3', 'abc#x', 'a/b', 'a b'], ids=repr)
    def test_a_bad_pano_id_is_refused(self, monkeypatch, pano_id):
        cbk = FakeCbk(monkeypatch)

        with pytest.raises(ValueError) as raised:
            gsv_tiles.fetch_equirect(pano_id, 1024, 512)

        assert not isinstance(raised.value, gsv_tiles.PhotosphereUnsupportedError)
        assert cbk.probe_urls == []

    @pytest.mark.parametrize('side', ['width', 'height'])
    @pytest.mark.parametrize('bad', [0, -1, None, 1024.0, float('nan'), True, numpy.bool_(True), '1024'],
                             ids=lambda v: '%s-%r' % (type(v).__name__, v))
    def test_a_bad_frame_is_refused(self, monkeypatch, side, bad):
        cbk = FakeCbk(monkeypatch)
        frame = {'width': 1024, 'height': 512}
        frame[side] = bad

        with pytest.raises(ValueError):
            gsv_tiles.fetch_equirect(PANO, frame['width'], frame['height'])

        assert cbk.probe_urls == []


# --- the event loop -----------------------------------------------------------------------------------------

class TestTheEventLoop:

    def test_the_sync_call_inside_a_running_loop_says_what_to_await(self, monkeypatch):
        cbk = FakeCbk(monkeypatch)

        async def main():
            # Caught here and returned as text: a held exception would keep a coroutine alive past the
            # warnings block below, hiding the never-awaited warning this test exists to see.
            try:
                gsv_tiles.fetch_equirect(PANO, 1024, 512)
            except RuntimeError as e:
                return str(e)
            return None

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            message = asyncio.run(main())
            gc.collect()

        assert message is not None and 'fetch_equirect_async' in message
        assert cbk.probe_urls == []
        assert not [w for w in caught if issubclass(w.category, RuntimeWarning) and 'never awaited' in str(w.message)]

    def test_two_fetches_awaited_together_keep_separate_refusals(self, monkeypatch):
        """Under the real fan-out: a refusal in one fetch abandons that fetch's tiles and nobody else's."""
        FakeCbk(monkeypatch, served=(8192, 4096), stub_tiles=False)
        requested = real_fan_out(monkeypatch, refuse_one_pano)

        async def main():
            return await asyncio.gather(gsv_tiles.fetch_equirect_async(REFUSED_PANO, 8192, 4096, zoom=4),
                                        gsv_tiles.fetch_equirect_async(SERVED_PANO, 8192, 4096, zoom=4),
                                        return_exceptions=True)

        refused, served = asyncio.run(main())

        assert isinstance(refused, gsv_tiles.TilePushbackError), refused
        assert 1 <= refused.requested <= gsv_tiles.CONCURRENCY
        assert refused.total == 128
        assert isinstance(served, gsv_tiles.StitchedPano), served
        assert len([t for t in requested if '&panoid=%s' % SERVED_PANO in t[2]]) == 128

    def test_the_fan_out_state_is_reset_in_the_awaiting_task(self, monkeypatch):
        FakeCbk(monkeypatch, stub_tiles=False)
        real_fan_out(monkeypatch, refuse_one_pano)

        async def main():
            with pytest.raises(gsv_tiles.TilePushbackError):
                await gsv_tiles.fetch_equirect_async(REFUSED_PANO, 1024, 512)
            left_behind = gsv_tiles._TILE_FANOUT.get()
            return left_behind, await gsv_tiles.fetch_equirect_async(SERVED_PANO, 1024, 512)

        left_behind, stitched = asyncio.run(main())

        assert left_behind is None
        assert stitched.image.size == (1024, 512)

    def test_the_async_call_works_inside_a_running_loop(self, monkeypatch):
        cbk = FakeCbk(monkeypatch)

        async def main():
            return await gsv_tiles.fetch_equirect_async(PANO, 1024, 512)

        stitched = asyncio.run(main())

        assert stitched.image.size == (1024, 512) and stitched.upscaled is False
        assert triples(cbk.probe_urls) == [(3, 0, 0), (5, 0, 0), (5, 2, 0), (5, 0, 1), (5, 1, 0)]
        assert tile_triples(cbk) == [(5, 0, 0), (5, 1, 0)]

    def test_the_blocking_work_runs_off_the_loop_thread(self, monkeypatch):
        cbk = FakeCbk(monkeypatch)
        stitches = record_stitches(monkeypatch)

        async def main():
            loop_thread = threading.get_ident()
            await gsv_tiles.fetch_equirect_async(PANO, 1024, 512)
            return loop_thread

        loop_thread = asyncio.run(main())

        assert len(cbk.probe_threads) == 5
        assert loop_thread not in cbk.probe_threads
        assert len(stitches) == 1 and stitches[0][1] != loop_thread

    def test_a_refusal_is_not_chained_to_the_loop_check(self, monkeypatch):
        """The running-loop check must not leave its own RuntimeError ("no running event loop") as the context
        of every exception the fetch raises, or each consumer traceback reads "during handling of the above
        exception" about an error nobody made."""
        FakeCbk(monkeypatch, levels=())

        with pytest.raises(gsv_tiles.PanoGoneError) as raised:
            gsv_tiles.fetch_equirect(PANO, 1024, 512)

        assert raised.value.__context__ is None

    def test_the_sync_call_runs_outside_a_loop_twice(self, monkeypatch):
        FakeCbk(monkeypatch)

        assert gsv_tiles.fetch_equirect(PANO, 1024, 512).image.size == (1024, 512)
        assert gsv_tiles.fetch_equirect(PANO, 1024, 512).image.size == (1024, 512)


# --- the async tile fetch -----------------------------------------------------------------------------------

class TestTheAsyncTileFetch:

    @pytest.mark.parametrize('served,frame,zoom', [((1024, 512), (1024, 512), 5), ((8192, 4096), (8192, 4096), 3)],
                             ids=['native', 'upscaled'])
    def test_the_async_tile_fetch_returns_what_fetch_pano_image_returns(self, monkeypatch, served, frame, zoom):
        FakeCbk(monkeypatch, served=served)

        sync = gsv_tiles.fetch_pano_image(PANO, frame[0], frame[1], zoom)
        async_ = asyncio.run(gsv_tiles._fetch_pano_image_async(PANO, frame[0], frame[1], zoom))

        assert async_.image.tobytes() == sync.image.tobytes()
        assert (async_.undersized_tiles, async_.upscaled) == (sync.undersized_tiles, sync.upscaled)

    def test_a_refusal_carries_its_counts_on_both(self, monkeypatch):
        FakeCbk(monkeypatch, tile_result=lambda tile: gsv_tiles.TilePushbackError(429, tile[0], tile[1]))

        with pytest.raises(gsv_tiles.TilePushbackError) as sync:
            gsv_tiles.fetch_pano_image(PANO, 1024, 512, 5)
        with pytest.raises(gsv_tiles.TilePushbackError) as async_:
            asyncio.run(gsv_tiles._fetch_pano_image_async(PANO, 1024, 512, 5))

        assert sync.value.total == async_.value.total == 2

    def test_there_is_one_stitch_path(self):
        """fetch_pano_image (the nightly's) and the async tile fetch stitch through one function, so the
        partition, the refusal counts, the black guard and the upscaled flag cannot drift between them."""
        with open(gsv_tiles.__file__, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        # Every read of the name, not only calls: the async path hands _stitch_fan_out to asyncio.to_thread.
        callers = {}
        for function in tree.body:
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(function):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                        callers.setdefault(node.id, set()).add(function.name)
        assert callers.get('_stitch_tiles') == {'_stitch_fan_out'}
        assert callers.get('_reject_mostly_black_stitch') == {'_stitch_fan_out'}
        assert {'fetch_pano_image', '_fetch_pano_image_async'} <= callers.get('_stitch_fan_out', set())


# --- the public surface -------------------------------------------------------------------------------------

class TestThePublicSurface:

    @pytest.mark.parametrize('name', NEW_PUBLIC_NAMES)
    def test_the_new_names_are_public(self, name):
        assert name in gsv_tiles.__all__

    def test_the_async_tile_fetch_is_private(self):
        assert '_fetch_pano_image_async' not in gsv_tiles.__all__
        assert 'fetch_pano_image_async' not in gsv_tiles.__all__

    @pytest.mark.parametrize('name', ['PanoGoneError', 'ZoomDisagreementError', 'PhotosphereUnsupportedError'])
    def test_the_new_errors_are_value_errors_and_not_frame_refusals(self, name):
        error = getattr(gsv_tiles, name)
        assert issubclass(error, ValueError)
        assert not issubclass(error, gsv_tiles.FrameDisagreementError)

    def test_gsv_does_not_re_export_the_one_call(self):
        assert 'fetch_equirect' not in gsv.RE_EXPORTED_FROM_GSV_TILES
        assert 'fetch_equirect_async' not in gsv.RE_EXPORTED_FROM_GSV_TILES
