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
