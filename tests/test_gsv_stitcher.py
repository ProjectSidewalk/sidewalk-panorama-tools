"""Tests for the GSV tile stitcher: grid arithmetic (#44), failed-tile handling (#45), stitch geometry,
and the atomic image save. Network-free throughout - tile downloads and the zoom probes are stubbed at the
gsv module boundary."""

import logging
import os
from io import BytesIO
from types import SimpleNamespace

import aiohttp
import pytest
from PIL import Image

from downloaders import gsv
from downloaders.common import DownloadResult

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

    def test_edge_tile_narrower_than_512_is_pasted_unstretched(self):
        """Defensive: if a server variant ever returns true-size edge tiles, resizing them to 512 (as the
        pre-#44 loop did unconditionally) stretches the edge geometry; pasting at the offset and cropping
        keeps it correct for both padded and true-size tiles."""
        edge = Image.new('RGB', (188, 512), YELLOW)
        # Left half blue, right half yellow: stretching to 512 wide would push blue past x=650.
        for px in range(94):
            for py in range(512):
                edge.putpixel((px, py), BLUE)
        buf = BytesIO()
        edge.save(buf, 'jpeg')
        tiles = [(0, 0, jpeg_bytes(RED)), (1, 0, buf.getvalue())]

        image = gsv._stitch_tiles(tiles, (700, 512), (700, 512))

        assert_color(image.getpixel((650, 100)), YELLOW)


class TestSavePanoImage:
    def test_atomic_success(self, tmp_path):
        out = str(tmp_path / 'pano.jpg')

        gsv._save_pano_image(Image.new('RGB', (32, 16), RED), out)

        assert os.path.isfile(out)
        assert not os.path.exists(out + '.part')
        if os.name == 'posix':
            assert os.stat(out).st_mode & 0o777 == 0o664

    def test_crash_leaves_no_final_file_and_no_part(self, tmp_path, monkeypatch):
        """The pre-#44 code saved straight to the final name, and the skip-if-exists check treats any file
        as done - a crash mid-save left a truncated JPEG that was never re-attempted."""
        out = str(tmp_path / 'pano.jpg')

        def boom(src, dst):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(gsv.os, 'replace', boom)

        with pytest.raises(OSError):
            gsv._save_pano_image(Image.new('RGB', (32, 16), RED), out)

        assert not os.path.exists(out)
        assert not os.path.exists(out + '.part')


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
        """#44 end to end: an 8192x4096 pano that only has zoom 3 must request the 8x4 zoom-3 grid (not the
        16x8 full-res one) and save imagery covering the whole reported frame."""
        stub_probe(monkeypatch, pick_zoom=3)
        requested = stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], jpeg_bytes(RED)))

        result = gsv.download_single_pano(str(tmp_path), self.pano_info(width=8192, height=4096))

        assert result == DownloadResult.success
        assert {(x, y) for x, y, _ in requested} == {(x, y) for x in range(8) for y in range(4)}
        assert all('zoom=3' in url for _, _, url in requested)
        with Image.open(tmp_path / 'st' / 'stitchPanoAAAAAAAAAAAA.jpg') as image:
            assert image.size == (8192, 4096)
            for corner in [(10, 10), (8181, 10), (10, 4085), (8181, 4085), (4096, 2048)]:
                assert_color(image.getpixel(corner), RED)

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
