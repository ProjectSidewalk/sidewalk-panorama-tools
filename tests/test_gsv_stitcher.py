"""Tests for the GSV tile stitcher: grid arithmetic (#44), failed-tile handling (#45), stitch geometry,
and the atomic image save. Network-free throughout - tile downloads and the zoom probes are stubbed at the
gsv module boundary."""

import asyncio
import logging
import os
from io import BytesIO
from types import SimpleNamespace

import aiohttp
import numpy as np
import pytest
from PIL import Image

from downloaders import gsv
from downloaders.common import DownloadResult
from test_gsv_tile_contract import MIXED_BLOCK, fixture_bytes, fixture_image

RED = (200, 30, 30)
BLUE = (30, 30, 200)
YELLOW = (220, 220, 30)


def jpeg_bytes(color, size=(512, 512)):
    buf = BytesIO()
    Image.new('RGB', size, color).save(buf, 'jpeg')
    return buf.getvalue()


def assert_color(pixel, expected):
    """JPEG is lossy; solid blocks decode within a few counts of the encoded color."""
    assert all(abs(channel - want) <= 16 for channel, want in zip(pixel, expected)), \
        "pixel %r is not %r" % (pixel, expected)


class TestGridArithmetic:
    """#44: the tile grid must follow the requested zoom, not the full-resolution dimensions."""

    @pytest.mark.parametrize('width,max_zoom', [(16384, 5), (13312, 5), (4096, 3), (3328, 3), (512, 0)])
    def test_max_zoom_inferred_from_reported_width(self, width, max_zoom):
        assert gsv._pano_max_zoom(width) == max_zoom

    @pytest.mark.parametrize('width,height,zoom,expected', [
        (16384, 8192, 5, (16384, 8192)),
        (16384, 8192, 3, (4096, 2048)),   # each zoom step halves both axes
        (13312, 6656, 3, (3328, 1664)),
        (3328, 1664, 3, (3328, 1664)),    # native zoom-3 pano: no scaling
    ])
    def test_dims_at_zoom(self, width, height, zoom, expected):
        assert gsv._dims_at_zoom(width, height, zoom) == expected

    def test_grid_at_zoom3_full_res_pano(self):
        """The #44 discriminator: a 16384x8192 pano at zoom 3 has 8x4 = 32 real tiles. The pre-#44 code
        requested the full-resolution 32x16 = 512 grid, of which 480 are out of range - the imagery then
        filled 1/16 of the canvas and the pano was saved mostly black, as success."""
        tiles = gsv._generate_tile_urls('panoA', 16384, 8192, 3)

        assert len(tiles) == 32
        assert {(x, y) for x, y, _ in tiles} == {(x, y) for x in range(8) for y in range(4)}
        assert all('zoom=3' in url and 'panoid=panoA' in url for _, _, url in tiles)

    def test_grid_at_zoom3_13312_pano(self):
        tiles = gsv._generate_tile_urls('panoA', 13312, 6656, 3)
        assert {(x, y) for x, y, _ in tiles} == {(x, y) for x in range(7) for y in range(4)}

    def test_grid_at_max_zoom_matches_the_old_arithmetic(self):
        """At the pano's own max zoom the grid equals what the pre-#44 code computed - the common (zoom 5)
        path is unchanged by this fix."""
        assert len(gsv._generate_tile_urls('p', 16384, 8192, 5)) == 32 * 16
        assert len(gsv._generate_tile_urls('p', 13312, 6656, 5)) == 26 * 13

    @pytest.mark.parametrize('width', [0, -1, None, float('nan'), float('inf'), float('-inf')])
    def test_pano_max_zoom_rejects_a_nonsense_width_by_name(self, width):
        """Reported dims are validated for None upstream but not for these. Left alone the arithmetic fails
        three different unhelpful ways - 'math domain error' from log2 on <= 0, 'cannot convert float NaN to
        integer' and OverflowError from ceil on non-finite input - and none of them name the pano, which is
        all an operator gets from scrape.log. NaN is the one that needs isfinite: NaN <= 0 is False, so it
        walks straight past a bounds check. (Raised by Copilot on #68.)"""
        with pytest.raises(ValueError, match='width'):
            gsv._pano_max_zoom(width)


# Per-zoom dimensions Google's own photometa reports, captured from 13 live panos spanning 2007-2025 and
# eight cities. This is the ground truth behind _dims_at_zoom: every level is the full width halved once per
# zoom step below the pano's max, including for the non-power-of-two widths (13312, 5376, 3328) that would
# have broken an "image at zoom z is always 512*2**z wide" reading of the API. Two of these panos also stop
# short of zoom 5 - DC-hist has four levels, Paris-hist five - which is what makes _pano_max_zoom's inference
# from the reported width load-bearing rather than decorative.
OBSERVED_PHOTOMETA = [
    ('Seattle 2022-09', [(512, 256), (1024, 512), (2048, 1024), (4096, 2048), (8192, 4096), (16384, 8192)]),
    ('NYC 2024-08', [(512, 256), (1024, 512), (2048, 1024), (4096, 2048), (8192, 4096), (16384, 8192)]),
    ('SF 2025-10', [(512, 256), (1024, 512), (2048, 1024), (4096, 2048), (8192, 4096), (16384, 8192)]),
    ('London 2022-07', [(512, 256), (1024, 512), (2048, 1024), (4096, 2048), (8192, 4096), (16384, 8192)]),
    ('Sydney 2014-11', [(416, 208), (832, 416), (1664, 832), (3328, 1664), (6656, 3328), (13312, 6656)]),
    ('Tokyo 2018-05', [(416, 208), (832, 416), (1664, 832), (3328, 1664), (6656, 3328), (13312, 6656)]),
    ('Paris 2013-06', [(416, 208), (832, 416), (1664, 832), (3328, 1664), (6656, 3328), (13312, 6656)]),
    ('NYC-hist 2011-08', [(416, 208), (832, 416), (1664, 832), (3328, 1664), (6656, 3328), (13312, 6656)]),
    ('DC-hist 2007-11', [(416, 208), (832, 416), (1664, 832), (3328, 1664)]),
    ('Paris-hist 2016-12', [(336, 168), (672, 336), (1344, 672), (2688, 1344), (5376, 2688)]),
]


class TestGridArithmeticAgainstRealPhotometa:
    @pytest.mark.parametrize('name,sizes', OBSERVED_PHOTOMETA)
    def test_max_zoom_matches_the_number_of_levels_google_reports(self, name, sizes):
        full_width = sizes[-1][0]
        assert gsv._pano_max_zoom(full_width) == len(sizes) - 1, name

    @pytest.mark.parametrize('name,sizes', OBSERVED_PHOTOMETA)
    def test_dims_at_every_zoom_match_what_google_reports(self, name, sizes):
        full_width, full_height = sizes[-1]
        for zoom, expected in enumerate(sizes):
            assert gsv._dims_at_zoom(full_width, full_height, zoom) == expected, \
                '%s: zoom %d' % (name, zoom)

    @pytest.mark.parametrize('name,sizes', OBSERVED_PHOTOMETA)
    def test_the_absolute_zoom_reading_is_ruled_out(self, name, sizes):
        """Discrimination for the test above: an implementation that returned 512*2**zoom (the other
        plausible reading of the API, and the one that would make #44's fix wrong on old panos) has to
        disagree with Google somewhere in this table."""
        naive = [(512 * 2 ** z, 256 * 2 ** z) for z in range(len(sizes))]
        if sizes[-1][0] not in (16384,):     # power-of-two panos are where the two readings coincide
            assert naive != [tuple(s) for s in sizes], name

    def test_grid_native_zoom3_pano(self):
        """Old panos whose reported dims already ARE the zoom-3 dims - the one case the old code got right."""
        tiles = gsv._generate_tile_urls('p', 3328, 1664, 3)
        assert {(x, y) for x, y, _ in tiles} == {(x, y) for x in range(7) for y in range(4)}


class _FakeResponse:
    def __init__(self, headers, body=b''):
        self.headers = headers
        self._body = body
        # str(ClientResponseError) reads request_info.real_url, so the fake needs one.
        self.request_info = SimpleNamespace(real_url='https://tile.invalid')
        self.history = ()
        self.status = 200
        self.content = self

    async def read(self):
        return self._body


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def get(self, url, **kwargs):
        response = self._response

        class _Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def fetch_tile(response, tile=(3, 1, 'https://tile.invalid')):
    import asyncio
    return asyncio.run(gsv._fetch_tile(_FakeSession(response), tile))


class TestFetchTile:
    def test_missing_content_type_raises_client_response_error_not_key_error(self):
        """A response with no Content-Type header must raise the same retryable error as a wrong one -
        the pre-#45 code raised a bare KeyError, which is in neither backoff tuple."""
        with pytest.raises(aiohttp.ClientResponseError):
            fetch_tile(_FakeResponse(headers={}))

    def test_non_jpeg_content_type_names_the_tile_and_type(self):
        with pytest.raises(aiohttp.ClientResponseError) as excinfo:
            fetch_tile(_FakeResponse(headers={'Content-Type': 'text/html'}))
        message = str(excinfo.value)
        assert 'text/html' in message
        assert '(3, 1)' in message

    def test_jpeg_content_type_returns_coords_and_bytes(self):
        body = jpeg_bytes(RED, (4, 4))
        assert fetch_tile(_FakeResponse(headers={'Content-Type': 'image/jpeg'}, body=body)) == (3, 1, body)

    def test_jpeg_with_parameter_suffix_is_accepted(self):
        # Parity with the historical `[0:10] != "image/jpeg"` prefix check.
        body = jpeg_bytes(RED, (4, 4))
        response = _FakeResponse(headers={'Content-Type': 'image/jpeg; charset=UTF-8'}, body=body)
        assert fetch_tile(response) == (3, 1, body)

    def test_retrying_variant_wraps_the_bare_fetch(self):
        assert gsv._download_tile is not gsv._fetch_tile
        assert gsv._download_tile.__wrapped__ is gsv._fetch_tile


class TestPartitionTileResults:
    def test_splits_successes_from_exceptions(self):
        """The #45 crash: gather(return_exceptions=True) hands back exception OBJECTS, and the pre-#45
        stitch loop subscripted them - TypeError: 'ClientResponseError' object is not subscriptable."""
        tiles = [(0, 0, 'u0'), (1, 0, 'u1'), (2, 0, 'u2')]
        boom = RuntimeError('tile failed')
        results = [(0, 0, b'aa'), boom, (2, 0, b'cc')]

        ok, failed = gsv._partition_tile_results(tiles, results)

        assert ok == [(0, 0, b'aa'), (2, 0, b'cc')]
        assert failed == [((1, 0), boom)]

    def test_all_good_yields_no_failures(self):
        tiles = [(0, 0, 'u0')]
        ok, failed = gsv._partition_tile_results(tiles, [(0, 0, b'aa')])
        assert (ok, failed) == ([(0, 0, b'aa')], [])

    def test_a_bare_base_exception_is_not_swallowed_into_the_failed_list(self):
        """download_panorama_images catches Exception, not BaseException. Capturing a CancelledError here
        and re-raising it from download_single_pano would sail past that handler and abort the whole run
        instead of failing one pano, so it has to propagate as itself."""
        tiles = [(0, 0, 'u0'), (1, 0, 'u1')]
        results = [(0, 0, b'aa'), asyncio.CancelledError()]

        with pytest.raises(asyncio.CancelledError):
            gsv._partition_tile_results(tiles, results)

    def test_ordinary_exceptions_are_still_captured(self):
        tiles = [(0, 0, 'u0')]
        boom = aiohttp.ClientError('nope')
        ok, failed = gsv._partition_tile_results(tiles, [boom])
        assert (ok, failed) == ([], [((0, 0), boom)])


class TestTileRetryErrors:
    def test_a_plain_request_timeout_is_retryable(self):
        """asyncio.TimeoutError is NOT an aiohttp.ClientError, so before this it got zero retries - and
        since one failed tile now fails the whole pano, an unretried timeout costs a whole download."""
        assert issubclass(asyncio.TimeoutError, gsv._TILE_RETRY_ERRORS)

    def test_the_aiohttp_client_error_family_is_retryable(self):
        for name in ('ClientError', 'ClientResponseError', 'ServerConnectionError',
                     'ServerDisconnectedError', 'ClientHttpProxyError', 'ServerTimeoutError'):
            assert issubclass(getattr(aiohttp, name), gsv._TILE_RETRY_ERRORS), name

    def test_a_programming_error_is_not_retried(self):
        """Backoff on a KeyError or a TypeError would turn a bug into ten slow bugs."""
        for exc in (KeyError, TypeError, ValueError, AttributeError):
            assert not issubclass(exc, gsv._TILE_RETRY_ERRORS), exc


class TestStitchTiles:
    def test_pastes_at_offsets_and_crops_to_zoom_dims(self):
        tiles = [(0, 0, jpeg_bytes(RED)), (1, 0, jpeg_bytes(BLUE))]

        image = gsv._stitch_tiles(tiles, (700, 300), (700, 300))

        assert image.size == (700, 300)
        assert_color(image.getpixel((100, 100)), RED)
        assert_color(image.getpixel((650, 100)), BLUE)

    def test_upscales_to_final_dims_so_imagery_fills_the_frame(self):
        """The #44 symptom in miniature: the stitched zoom-native image must be scaled up to the reported
        dims, not pasted into a corner of a full-size black canvas."""
        tiles = [(0, 0, jpeg_bytes(RED)), (1, 0, jpeg_bytes(BLUE))]

        image = gsv._stitch_tiles(tiles, (1024, 512), (2048, 1024))

        assert image.size == (2048, 1024)
        assert_color(image.getpixel((300, 300)), RED)
        assert_color(image.getpixel((1800, 300)), BLUE)     # the right half is imagery, not black padding
        assert_color(image.getpixel((2047, 1023)), BLUE)    # ...all the way into the corner

    def test_black_padding_on_a_real_edge_tile_is_cropped_away(self):
        """Google pads a short edge tile to a full 512 body with black rather than returning it true-size
        (pinned on real bytes in test_gsv_tile_contract). The crop to zoom_dims is what removes that
        padding, so the padding must never survive into the saved pano."""
        edge = fixture_bytes('z3_edge_bottom.jpg')          # 512 body, ~383 black rows at the bottom
        tiles = [(0, 0, jpeg_bytes(RED)), (0, 1, edge)]

        # The zoom-3 image this tile came from is 1664 tall: 3 full rows + 128 real rows in the last.
        image = gsv._stitch_tiles(tiles, (512, 512 + 128), (512, 512 + 128))

        assert image.size == (512, 640)
        assert image.convert('L').getextrema()[1] > 0
        bottom_rows = np.asarray(image.convert('L'))[512:]
        assert (bottom_rows == 0).mean() < 0.5, \
            'the black padding below the real imagery was not cropped off'


class TestStitchTilesWithMixedBodySizes:
    """The regression this PR review turned up: cbk answers some zoom-5 positions with a 512 body and
    others with a load-shed 256 body, in the SAME fan-out. Pasting bodies at the nominal 512 grid pitch
    without scaling them to the cell size leaves 3/4 of every degraded cell black - and the pano is saved
    as success. The pre-#44 `img.resize((512, 512))` was what absorbed this; see test_gsv_tile_contract
    for the captured evidence and the proof that a degraded body is the same cell at half scale."""

    def test_all_degraded_bodies_still_fill_the_frame(self):
        """The uniform case: every body comes back at half size. The stitch is then simply done at the
        smaller cell size and upscaled once at the end - no black anywhere."""
        tiles = [(0, 0, jpeg_bytes(RED, (256, 256))), (1, 0, jpeg_bytes(BLUE, (256, 256)))]

        image = gsv._stitch_tiles(tiles, (1024, 512), (1024, 512))

        assert image.size == (1024, 512)
        assert (np.asarray(image.convert('L')) == 0).mean() == 0
        assert_color(image.getpixel((100, 100)), RED)
        assert_color(image.getpixel((900, 400)), BLUE)

    def test_mixed_body_sizes_leave_no_black_cells(self):
        """The mixed case, which is what production actually sees. Cell (1, 0) arrives at half size; its
        pixels must be scaled up to fill the whole cell, not left in the cell's top-left quadrant."""
        tiles = [(0, 0, jpeg_bytes(RED, (512, 512))), (1, 0, jpeg_bytes(BLUE, (256, 256))),
                 (0, 1, jpeg_bytes(BLUE, (512, 512))), (1, 1, jpeg_bytes(RED, (256, 256)))]

        image = gsv._stitch_tiles(tiles, (1024, 1024), (1024, 1024))

        assert (np.asarray(image.convert('L')) == 0).mean() == 0, \
            'a degraded cell was left partly black - the #44-class corruption this fix is about'
        # The degraded cells must carry their own colour across their FULL extent, corner included.
        assert_color(image.getpixel((1000, 100)), BLUE)
        assert_color(image.getpixel((600, 20)), BLUE)
        assert_color(image.getpixel((1000, 1000)), RED)

    def test_real_mixed_fanout_reconstructs_the_zoom4_ground_truth(self):
        """End of the chain, on real bytes: stitch the captured 2x2 zoom-5 neighbourhood (two 512 bodies,
        two 256 bodies, one real fan-out) and compare against the real zoom-4 tile covering the same pano
        region. cbk's zoom-4 image is 8192 wide, so the correct stitch downscaled to 512 IS that tile.

        This is the test that fails loudest on the unscaled paste: the degraded half of the block lands in
        quarter-cells and the comparison blows up."""
        tiles = [(x, y, fixture_bytes(name)) for name, x, y, _ in MIXED_BLOCK]
        # Grid coordinates are absolute; rebase the 2x2 block to (0, 0) so it stitches on its own.
        tiles = [(x - 8, y - 10, body) for x, y, body in tiles]

        stitched = gsv._stitch_tiles(tiles, (1024, 1024), (1024, 1024))
        got = np.asarray(stitched.resize((512, 512), Image.LANCZOS), float)
        want = np.asarray(fixture_image('z4_cover_4_5.jpg'), float)

        assert np.abs(got - want).mean() < 6.0, \
            ('the stitched real block does not match the zoom-4 tile covering the same region '
             '(mean|diff|=%.2f)' % np.abs(got - want).mean())
        assert (np.asarray(stitched.convert('L')) == 0).mean() == 0

    def test_a_degraded_cell_is_not_merely_left_black(self):
        """Discrimination: the assertions above would also pass if the stitcher dropped degraded tiles and
        filled their cells with a neighbour. Pin that the degraded cell carries ITS OWN imagery."""
        tiles = [(0, 0, jpeg_bytes(RED, (512, 512))), (1, 0, jpeg_bytes(YELLOW, (256, 256)))]

        image = gsv._stitch_tiles(tiles, (1024, 512), (1024, 512))

        assert_color(image.getpixel((100, 100)), RED)
        for probe in [(520, 10), (768, 256), (1023, 511)]:
            assert_color(image.getpixel(probe), YELLOW)

    def test_cell_size_is_the_largest_body_not_the_first_one(self):
        """Order independence: a fan-out whose first tile happens to be degraded must still stitch at the
        full cell size, or every full-size body would be thrown away."""
        degraded_first = [(0, 0, jpeg_bytes(RED, (256, 256))), (1, 0, jpeg_bytes(BLUE, (512, 512)))]
        full_first = [(0, 0, jpeg_bytes(BLUE, (512, 512))), (1, 0, jpeg_bytes(RED, (256, 256)))]

        assert gsv._stitch_cell_size(degraded_first) == (512, 512)
        assert gsv._stitch_cell_size(full_first) == (512, 512)
        assert gsv._stitch_tiles(degraded_first, (1024, 512), (1024, 512)).size == (1024, 512)

    def test_undersized_tile_count_reports_the_degradation(self):
        tiles = [(0, 0, jpeg_bytes(RED, (512, 512))), (1, 0, jpeg_bytes(BLUE, (256, 256))),
                 (2, 0, jpeg_bytes(BLUE, (256, 256)))]
        assert gsv._undersized_tile_count(tiles) == 2
        assert gsv._undersized_tile_count(tiles[:1]) == 0


class TestRejectMostlyBlackStitch:
    """Neither #44 nor the unscaled-paste regression is visible at the tile layer: out-of-range tiles are
    valid all-black JPEGs answered 200 OK (pinned on real bytes in test_gsv_tile_contract). The only place
    either shows up is the stitched frame, so that is where the guard belongs. Calibration: the repo's real
    13312x6656 sample_pano.jpg is 0.0% exactly-black, while the failure modes are 75-100%."""

    def test_black_fraction_of_a_fully_black_image(self):
        assert gsv._black_fraction(Image.new('RGB', (512, 256), (0, 0, 0))) == 1.0

    def test_black_fraction_of_real_imagery_is_nil(self):
        assert gsv._black_fraction(fixture_image('z4_cover_4_5.jpg')) < 0.01

    def test_black_fraction_is_exact_not_sampled_or_averaged(self):
        """Discrimination for the counting method. Every other row black is exactly 50%, and it is the one
        pattern both shortcuts get badly wrong: a NEAREST probe with an even stride samples only the black
        rows and says 100%, while an averaging downscale blends each pair and says 0%. Regular black/imagery
        banding is precisely what a tiling bug produces, so the count has to be exact."""
        striped = Image.new('RGB', (512, 512), (0, 0, 0))
        for row in range(1, 512, 2):
            striped.paste(Image.new('RGB', (512, 1), RED), (0, row))

        assert gsv._black_fraction(striped) == pytest.approx(0.5, abs=1e-9)

    def test_black_fraction_of_the_unscaled_paste_failure_mode(self):
        """A 512 cell holding a 256 body in its corner is exactly 75% black."""
        canvas = Image.new('RGB', (512, 512), (0, 0, 0))
        canvas.paste(Image.new('RGB', (256, 256), RED), (0, 0))
        assert 0.7 < gsv._black_fraction(canvas) < 0.8

    def test_mostly_black_stitch_is_rejected(self):
        canvas = Image.new('RGB', (512, 512), (0, 0, 0))
        canvas.paste(Image.new('RGB', (128, 128), RED), (0, 0))

        with pytest.raises(gsv.StitchedPanoMostlyBlackError) as excinfo:
            gsv._reject_mostly_black_stitch(canvas, 'panoZ', zoom=3)

        assert 'panoZ' in str(excinfo.value)

    def test_real_imagery_passes_the_guard(self):
        gsv._reject_mostly_black_stitch(fixture_image('z4_cover_4_5.jpg'), 'panoZ', zoom=5)

    def test_a_dark_but_real_frame_is_not_rejected(self):
        """False-positive guard: night imagery is dark, not exactly black. The check counts only exact
        zeros so a legitimately dark pano survives."""
        dark = Image.new('RGB', (512, 256), (3, 3, 4))
        assert gsv._black_fraction(dark) == 0.0
        gsv._reject_mostly_black_stitch(dark, 'panoZ', zoom=5)

    def test_the_threshold_leaves_room_for_a_partly_black_but_real_frame(self):
        """A third of the frame black (a tunnel mouth, a blown-out nadir) must still pass."""
        canvas = Image.new('RGB', (600, 300), (0, 0, 0))
        canvas.paste(Image.new('RGB', (400, 300), RED), (0, 0))
        gsv._reject_mostly_black_stitch(canvas, 'panoZ', zoom=5)


# The stitched JPEG is written through common.atomic_output_path, the same helper the depth artifacts and
# the Mapillary downloader use. Its contract - rename on success, remove the .part on any BaseException
# including SIGTERM's SystemExit, 0o664 on the result - is covered by tests/test_image_downloaders.py's
# TestAtomicOutputPath. What is pinned here instead is the pano-level consequence: a pano that fails to
# stitch leaves neither a .jpg nor a .part behind (see the end-to-end failure tests below).


# --- download_single_pano end to end (probes and tile fan-out stubbed) ---------------------------------------

def stub_probe(monkeypatch, pick_zoom):
    """Make the zoom probe pick `pick_zoom` without a network: probe requests for that zoom return a
    non-blank JPEG, every other zoom a black one (Google's no-imagery answer)."""

    def fake_get_response(url, session, stream=False):
        color = RED if ('zoom=%d&' % pick_zoom) in url else (0, 0, 0)
        return BytesIO(jpeg_bytes(color, (16, 16)))

    monkeypatch.setattr(gsv, '_get_response', fake_get_response)


def stub_tiles(monkeypatch, result_for_tile):
    """Replace the tile fan-out with canned per-tile results; records the tile list it was asked for."""
    requested = []

    async def fake_download_tiles(tiles):
        requested.extend(tiles)
        return [result_for_tile(tile) for tile in tiles]

    monkeypatch.setattr(gsv, '_download_tiles', fake_download_tiles)
    return requested


class TestDownloadSinglePano:
    def pano_info(self, pano_id='stitchPanoAAAAAAAAAAAA', width=1024, height=512):
        return {'pano_id': pano_id, 'width': width, 'height': height}

    def test_success_stitches_and_saves_at_reported_dims(self, tmp_path, monkeypatch):
        stub_probe(monkeypatch, pick_zoom=5)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], jpeg_bytes(RED if tile[0] == 0 else BLUE)))

        result = gsv.download_single_pano(str(tmp_path), self.pano_info())

        assert result == DownloadResult.success
        path = tmp_path / 'st' / 'stitchPanoAAAAAAAAAAAA.jpg'
        assert path.is_file()
        assert not (tmp_path / 'st' / 'stitchPanoAAAAAAAAAAAA.jpg.part').exists()
        with Image.open(path) as image:
            assert image.size == (1024, 512)
            assert_color(image.getpixel((100, 100)), RED)
            assert_color(image.getpixel((900, 100)), BLUE)

    def test_zoom3_pano_requests_the_zoom3_grid_and_fills_the_frame(self, tmp_path, monkeypatch):
        """#44 end to end. 8192x4096 is a size-reduced stand-in so the test does not allocate a 400 MB
        canvas - the shape fidelity of the arithmetic is covered against real photometa in
        TestGridArithmeticAgainstRealPhotometa. What matters here is that the requested grid follows the
        zoom (8x4, not the 16x8 full-res one) and that the imagery covers the whole reported frame."""
        stub_probe(monkeypatch, pick_zoom=3)
        requested = stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], jpeg_bytes(RED)))

        result = gsv.download_single_pano(str(tmp_path), self.pano_info(width=8192, height=4096))

        # fallback_success, not success: this pano's own dims need zoom 4, so the zoom-3 stitch is upscaled
        # to reach them (#52 item 2, log.csv column 8). TestFallbackResolutionIsReported owns that split.
        assert result == DownloadResult.fallback_success
        assert {(x, y) for x, y, _ in requested} == {(x, y) for x in range(8) for y in range(4)}
        assert all('zoom=3' in url for _, _, url in requested)
        with Image.open(tmp_path / 'st' / 'stitchPanoAAAAAAAAAAAA.jpg') as image:
            assert image.size == (8192, 4096)
            for corner in [(10, 10), (8181, 10), (10, 4085), (8181, 4085), (4096, 2048)]:
                assert_color(image.getpixel(corner), RED)

    def test_a_real_four_level_pano_downloads_at_its_native_zoom(self, tmp_path, monkeypatch):
        """A real shape rather than a stand-in: 3328x1664 with only four zoom levels is the DC-2007 pano in
        TestGridArithmeticAgainstRealPhotometa. Its max zoom IS 3, so zoom 3 is native - the one case the
        pre-#44 code got right, and the one that must stay byte-for-byte unchanged. The 7x4 grid also has a
        partial last column (3328 = 6*512 + 256), which the crop has to handle."""
        stub_probe(monkeypatch, pick_zoom=3)
        requested = stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], jpeg_bytes(RED)))

        result = gsv.download_single_pano(str(tmp_path), self.pano_info(width=3328, height=1664))

        assert result == DownloadResult.success
        assert {(x, y) for x, y, _ in requested} == {(x, y) for x in range(7) for y in range(4)}
        with Image.open(tmp_path / 'st' / 'stitchPanoAAAAAAAAAAAA.jpg') as image:
            assert image.size == (3328, 1664)
            assert gsv._black_fraction(image) == 0.0

    def test_a_fallback_and_a_native_zoom3_are_not_the_same_outcome(self, tmp_path, monkeypatch):
        """Guards the pair below from drifting apart: same picked zoom, same stubs, different verdict, and
        the only thing that differs is whether the pano's own dims need a zoom the download could not get."""
        stub_probe(monkeypatch, pick_zoom=3)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], jpeg_bytes(RED)))

        native = gsv.download_single_pano(str(tmp_path), self.pano_info('nativeZoom3AAAAAAAAAAA',
                                                                       width=3328, height=1664))
        upscaled = gsv.download_single_pano(str(tmp_path), self.pano_info('upscaledZoom3AAAAAAAAA',
                                                                         width=8192, height=4096))

        assert native == DownloadResult.success
        assert upscaled == DownloadResult.fallback_success

    def test_a_legacy_xml_beside_the_pano_is_ignored(self, tmp_path, monkeypatch):
        """#52 items 3/4/5. download_single_pano used to read a legacy `<pano_id>.xml` for dims and zoom.
        Its producer was removed in #39 (the cbk?output=xml endpoint died in 2022), but the files are still
        on the store - sampled across dc/columbus-oh/amsterdam/newberg-or, 1,025 of them, of which exactly
        ONE had no .jpg beside it. That one pano is the only case the block could ever run, because the
        "image already exists -> skipped" check returns first.

        The block is gone; dims come from /adminapi/panos and the zoom comes from the probe. This pins that
        a stray .xml cannot change the outcome - including a malformed one, which the deleted code swallowed
        with a bare `except Exception: pass` so it was indistinguishable from no file at all."""
        shard = tmp_path / 'st'
        shard.mkdir()
        (shard / 'stitchPanoAAAAAAAAAAAA.xml').write_text('<not-even-xml')
        stub_probe(monkeypatch, pick_zoom=5)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], jpeg_bytes(RED)))

        result = gsv.download_single_pano(str(tmp_path), self.pano_info())

        assert result == DownloadResult.success
        with Image.open(shard / 'stitchPanoAAAAAAAAAAAA.jpg') as image:
            assert image.size == (1024, 512)
            assert gsv._black_fraction(image) == 0.0

    def test_a_wellformed_legacy_xml_cannot_veto_a_pano_google_still_serves(self, tmp_path, monkeypatch):
        """The discriminating half, and the reason deleting the block is a fix rather than a tidy-up.

        A well-formed legacy XML declaring `num_zoom_levels` made the old code trust that zoom over the
        probe, then test-fetch one tile at it - and return DownloadResult.failure if that tile came back
        black. failure is PERMANENT under the #41 ledger: the pano is written downloaded=0 and never
        re-attempted. So a stale 2022 XML could blacklist a pano that Google serves perfectly well today,
        which is precisely the pano this block only ever runs on (an .xml with no .jpg beside it).

        With the block gone the probe answers, finds zoom 5, and the pano downloads at native resolution."""
        shard = tmp_path / 'st'
        shard.mkdir()
        (shard / 'stitchPanoAAAAAAAAAAAA.xml').write_text(
            '<panorama><data_properties num_zoom_levels="3" width="8192" height="4096"/></panorama>')
        stub_probe(monkeypatch, pick_zoom=5)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], jpeg_bytes(RED)))

        result = gsv.download_single_pano(str(tmp_path), self.pano_info(width=8192, height=4096))

        assert result == DownloadResult.success
        assert (shard / 'stitchPanoAAAAAAAAAAAA.jpg').is_file()

    def test_degraded_tile_bodies_still_fill_the_whole_frame(self, tmp_path, monkeypatch, caplog):
        """The regression this review caught, end to end: every body arrives at half size (cbk's load-shed
        rendering). The saved pano must still be full-frame imagery at the reported dims, and the run must
        SAY the imagery was degraded - it is a real resolution loss, just not a corruption."""
        stub_probe(monkeypatch, pick_zoom=5)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1],
                                              jpeg_bytes(RED if tile[0] == 0 else BLUE, (256, 256))))

        with caplog.at_level(logging.WARNING):
            result = gsv.download_single_pano(str(tmp_path), self.pano_info())

        assert result == DownloadResult.success
        with Image.open(tmp_path / 'st' / 'stitchPanoAAAAAAAAAAAA.jpg') as image:
            assert image.size == (1024, 512)
            assert gsv._black_fraction(image) == 0.0
            assert_color(image.getpixel((100, 100)), RED)
            assert_color(image.getpixel((1000, 500)), BLUE)
        assert 'stitchPanoAAAAAAAAAAAA' in caplog.text
        assert '2/2' in caplog.text

    def test_a_mix_of_full_and_degraded_bodies_is_stitched_and_logged(self, tmp_path, monkeypatch, caplog):
        """What production actually sees - some positions full, some degraded, in one fan-out."""
        stub_probe(monkeypatch, pick_zoom=5)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], jpeg_bytes(RED, (512, 512))
                                              if tile[0] == 0 else jpeg_bytes(BLUE, (256, 256))))

        with caplog.at_level(logging.WARNING):
            assert gsv.download_single_pano(str(tmp_path), self.pano_info()) == DownloadResult.success

        with Image.open(tmp_path / 'st' / 'stitchPanoAAAAAAAAAAAA.jpg') as image:
            assert image.size == (1024, 512)
            assert gsv._black_fraction(image) == 0.0
            assert_color(image.getpixel((1000, 500)), BLUE)
        assert '1/2' in caplog.text

    def test_full_size_bodies_log_nothing(self, tmp_path, monkeypatch, caplog):
        """Discrimination for the two tests above: the warning must not fire on the healthy path, or it is
        noise the operator learns to ignore."""
        stub_probe(monkeypatch, pick_zoom=5)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], jpeg_bytes(RED, (512, 512))))

        with caplog.at_level(logging.WARNING):
            gsv.download_single_pano(str(tmp_path), self.pano_info())

        assert 'degraded' not in caplog.text.lower()

    def test_an_all_blank_grid_fails_instead_of_saving_a_black_pano(self, tmp_path, monkeypatch, caplog):
        """The #44 failure mode itself, driven with REAL out-of-range tile bytes. Google answers an
        out-of-range tile 200 OK with a valid all-black JPEG, so nothing below the stitch can tell that the
        grid was wrong. The stitched frame can, and the pano must fail rather than be ledgered
        downloaded=1 with a black file that is never re-attempted."""
        stub_probe(monkeypatch, pick_zoom=5)
        blank = fixture_bytes('z3_blank_out_of_range.jpg')
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], blank))

        with caplog.at_level(logging.ERROR):
            with pytest.raises(gsv.StitchedPanoMostlyBlackError):
                gsv.download_single_pano(str(tmp_path), self.pano_info())

        shard = tmp_path / 'st'
        assert not (shard / 'stitchPanoAAAAAAAAAAAA.jpg').exists()
        assert list(shard.glob('*.part')) == []
        assert 'stitchPanoAAAAAAAAAAAA' in caplog.text

    def test_failed_tile_fails_the_pano_loudly_and_writes_nothing(self, tmp_path, monkeypatch, caplog):
        """#45: one failed tile must fail the pano with the REAL cause in scrape.log - not a TypeError from
        subscripting the exception object - and must not leave a partial file that the skip-if-exists check
        would treat as done forever. Raising (rather than returning failure) marks the failure transient:
        under #41's retry semantics the pano is re-attempted next run."""
        stub_probe(monkeypatch, pick_zoom=5)
        boom = RuntimeError('tile exploded')
        stub_tiles(monkeypatch,
                   lambda tile: (tile[0], tile[1], jpeg_bytes(RED)) if tile[:2] != (1, 0) else boom)

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError, match='tile exploded'):
                gsv.download_single_pano(str(tmp_path), self.pano_info())

        assert '1/2 tiles failed' in caplog.text
        assert 'tile (1, 0)' in caplog.text
        assert 'tile exploded' in caplog.text
        shard = tmp_path / 'st'
        assert not (shard / 'stitchPanoAAAAAAAAAAAA.jpg').exists()
        assert list(shard.glob('*.part')) == []

    def test_existing_file_short_circuits_before_any_probe(self, tmp_path, monkeypatch):
        shard = tmp_path / 'st'
        shard.mkdir()
        (shard / 'stitchPanoAAAAAAAAAAAA.jpg').write_bytes(b'already here')

        def no_network(*args, **kwargs):
            raise AssertionError('an existing pano must not touch the network')

        monkeypatch.setattr(gsv, '_get_response', no_network)

        assert gsv.download_single_pano(str(tmp_path), self.pano_info()) == DownloadResult.skipped

    def test_unknown_dims_fail_before_any_tile_request(self, tmp_path, monkeypatch):
        stub_probe(monkeypatch, pick_zoom=5)
        pano = {'pano_id': 'stitchPanoAAAAAAAAAAAA', 'width': None, 'height': None}

        assert gsv.download_single_pano(str(tmp_path), pano) == DownloadResult.failure

    def test_blank_probes_at_both_zooms_fail_the_pano(self, tmp_path, monkeypatch):
        stub_probe(monkeypatch, pick_zoom=-1)  # every probe zoom comes back blank

        assert gsv.download_single_pano(str(tmp_path), self.pano_info()) == DownloadResult.failure
