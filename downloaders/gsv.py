# Google Street View panorama downloader.
#
# Stitches tiles from Google's undocumented CBK endpoint into a single equirectangular JPEG, and fetches depth
# maps from Google's photometa endpoint via the streetlevel library (see download_depth_maps).
#
# Do not add viewer parameters to the CBK URL without checking what they do to the tile bodies: `fover`, copied
# from the Street View viewer, made CBK serve the polar rows of zoom 5 at half size (#73). See _CBK_BASE_URL.

import asyncio
import base64
import collections
import csv
import logging
import math
import os
import random
import stat
import struct
import time
from io import BytesIO

import aiohttp
import backoff
import numpy as np
import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import headers_list, proxies, thread_count

try:
    from config import depth_min_request_interval
except ImportError:
    # config.py predates the depth phase (the scraper box carries local edits to this file, so a `git pull` can
    # leave it behind). Don't take the whole scraper down over a throttle that defaults to off anyway.
    depth_min_request_interval = 0.0

from .common import DownloadResult, atomic_output_path


def _normalize_proxies(raw):
    """Normalize the proxy dict from config.py, treating placeholder values as unset - per key.

    config.py ships bare-scheme placeholders ('http://' for both keys), and handing one of those to requests
    as a proxy URL breaks every request through it. Normalizing per key means setting a real proxy for one
    scheme doesn't leak the other key's placeholder through (#51).
    """
    return {scheme: (url if url not in (None, '', 'http://', 'https://') else None)
            for scheme, url in raw.items()}


_proxies = _normalize_proxies(proxies)


def _random_header():
    return random.choice(headers_list)


class _TimeoutHTTPAdapter(HTTPAdapter):
    """HTTPAdapter that applies a default timeout to every request.

    `requests` sets no default timeout of its own, so without this one hung connection stalls a nightly cron
    run indefinitely - not for a long time, forever. Both sessions below need it, for the same reason from
    opposite ends: streetlevel's photometa requests carry no timeout and expose no per-request hook to add
    one, and the image path's zoom probes never set one either (#65 item 5).
    """

    def __init__(self, *args, timeout=30, **kwargs):
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        # Session.send always passes timeout as a kwarg (None when the caller set nothing), so a plain
        # setdefault would never fire.
        if kwargs.get('timeout') is None:
            kwargs['timeout'] = self._timeout
        return super().send(request, **kwargs)


def _request_session():
    """The session the zoom probes ride on: retries, plus the default timeout _TimeoutHTTPAdapter adds."""
    session = requests.Session()
    retry = Retry(total=5, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
    adapter = _TimeoutHTTPAdapter(max_retries=retry, timeout=30)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def _get_response(url, session, stream=False):
    # A filtered copy, not the module global (matching _depth_session's semantics): a normalized-away
    # placeholder must reach requests as absent — a None value blocks the env-proxy fallback — and requests
    # setdefaults env proxies into this dict in place, which must not corrupt _proxies for later calls (#51).
    response = session.get(url, headers=_random_header(), proxies={k: v for k, v in _proxies.items() if v},
                           stream=stream)
    if not stream:
        return response
    return response.raw


TILE_SIZE = 512

# NO `fover` - it is a viewer bandwidth optimisation, and sending any of fover=1/2/3 makes CBK serve the polar
# rows of a zoom-5 grid as 256x256 bodies instead of 512x512, costing half the linear resolution over 62.5% of
# the frame (#73). We inherited fover=2 by copying the Street View viewer's URL wholesale. Dropped 2026-08-07,
# after misaugstad isolated the parameter. `onerr=3` was checked at the same time and is innocent.
#
# With fover gone, CBK's tile bodies are byte-identical to streetviewpixels-pa's, so there is nothing to
# recover by switching endpoints (#74 is now a cleanup, not a resolution fix). Anything added here should be
# checked the same way: tests/test_gsv_tile_contract.py pins the parameter and the band it used to produce.
_CBK_BASE_URL = 'https://maps.google.com/cbk?output=tile&cb_client=maps_sv&onerr=3&renderer=spherical&v=4'

# Tile failures worth retrying. aiohttp.web.HTTPServerError used to head this tuple, but it is a SERVER-side
# response class that client code never raises (#52 item 3) - importing aiohttp.web for it was pure cost. The
# rest of the old tuple was redundant: ClientResponseError, ServerConnectionError, ServerDisconnectedError and
# ClientHttpProxyError all derive from ClientError. asyncio.TimeoutError does NOT, though, so a plain request
# timeout used to get zero retries - and now that one failed tile fails the whole pano, that costs a download.
_TILE_RETRY_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)

# A stitched frame with more black than this is not imagery, it is a grid bug. Nothing below the stitch can
# see one: an out-of-range tile is answered 200 OK with a valid ALL-BLACK image/jpeg (pinned on real bytes in
# tests/test_gsv_tile_contract.py), so a wrong grid looks exactly like a successful download tile by tile.
# Calibration: the repo's real 13312x6656 samples/sample_pano.jpg is 0.0% exactly-black, while the failure
# modes are 75% (#44's zoom-3-in-a-full-canvas, and a degraded body pasted without scaling) to 100% (every
# requested tile out of range). Exact zeros only, so genuinely dark night imagery is not caught by this.
STITCH_MAX_BLACK_FRACTION = 0.5


class StitchedPanoMostlyBlackError(Exception):
    """The stitch produced a frame that is mostly black - a tile-grid fault, not imagery."""


def _pano_max_zoom(width):
    """The pano's own maximum zoom level, inferred from its reported full width.

    At zoom z a pano is at most 2**z x 2**(z-1) tiles of 512px, so the max zoom is the smallest z whose tile
    budget covers the reported width: 16384 -> 5, 13312 -> 5, 3328 (an old zoom-3-native pano) -> 3.
    Cross-checked against Google's own per-zoom image_sizes for 13 live panos spanning 2007-2025 in
    tests/test_gsv_stitcher.py's OBSERVED_PHOTOMETA, including the four- and five-level panos that make the
    inference load-bearing.
    """
    # Left to itself this arithmetic fails three different unhelpful ways: math.log2 raises a bare "math
    # domain error" on <= 0, and math.ceil raises "cannot convert float NaN to integer" / OverflowError on a
    # non-finite width - none of which name the pano. isfinite first, because NaN <= 0 is False.
    if width is None or not math.isfinite(width) or width <= 0:
        raise ValueError('pano width must be a positive finite number to infer a zoom level, got %r'
                         % (width,))
    return max(0, int(math.ceil(math.log2(width / float(TILE_SIZE)))))


def _dims_at_zoom(width, height, zoom):
    """Pixel dimensions of the pano at `zoom`, given its reported full (= max-zoom) dimensions.

    Each zoom step halves both axes; at the pano's max zoom (the common case) this is the identity. The #44
    bug was ignoring this: the tile grid was always derived from the FULL dims, so a zoom-3 download of a
    16384x8192 pano requested a 32x16 grid of which 480 tiles were out of range, and the imagery landed in
    1/16 of a black canvas - saved as success.
    """
    scale = 2 ** max(0, _pano_max_zoom(width) - zoom)
    return int(math.ceil(width / float(scale))), int(math.ceil(height / float(scale)))


def _tile_grid(width, height, zoom):
    zoom_width, zoom_height = _dims_at_zoom(width, height, zoom)
    return (int(math.ceil(zoom_width / float(TILE_SIZE))), int(math.ceil(zoom_height / float(TILE_SIZE))))


def _generate_tile_urls(pano_id, width, height, zoom):
    """The tile fan-out for one pano: a list of (x, y, url) covering exactly the grid `zoom` has."""
    tiles_x, tiles_y = _tile_grid(width, height, zoom)
    return [(x, y, f'{_CBK_BASE_URL}&zoom={zoom}&x={x}&y={y}&panoid={pano_id}')
            for y in range(tiles_y) for x in range(tiles_x)]


async def _fetch_tile(session, tile):
    """Fetch one tile; return (x, y, jpeg_bytes).

    Undecorated so tests can drive it without backoff's sleeps; _download_tile below is the retrying variant
    the fan-out uses.
    """
    x, y, url = tile
    async with session.get(url, proxy=_proxies.get("http"), headers=_random_header()) as response:
        # .get(), not [..]: a response with no Content-Type must raise the same retryable error as a wrong
        # one, not a bare KeyError that is in neither backoff tuple (#45).
        content_type = response.headers.get('Content-Type', '')
        if content_type[0:10] != "image/jpeg":
            raise aiohttp.ClientResponseError(
                response.request_info, response.history, status=response.status,
                message="unexpected Content-Type %r for tile (%d, %d)" % (content_type, x, y))
        return x, y, await response.content.read()


_download_tile = backoff.on_exception(backoff.expo, _TILE_RETRY_ERRORS, max_tries=10)(_fetch_tile)


async def _download_tiles(tiles):
    """Fetch every tile concurrently; failures come back as exception OBJECTS in the result list.

    No whole-batch backoff on purpose: each tile already retries up to 10 times in _download_tile, and with
    return_exceptions=True nothing propagates out of the gather anyway - the decorator this replaces could
    never fire for tile errors and only re-ran connector construction, re-downloading every tile (#45).
    """
    conn = aiohttp.TCPConnector(limit=thread_count)
    async with aiohttp.ClientSession(raise_for_status=True, connector=conn) as session:
        tasks = [asyncio.ensure_future(_download_tile(session, tile)) for tile in tiles]
        return await asyncio.gather(*tasks, return_exceptions=True)


def _partition_tile_results(tiles, results):
    """Split a gather's results into (ok [(x, y, bytes)], failed [((x, y), exception)]).

    The pre-#45 stitch loop indexed every result unconditionally, so one failed tile crashed the pano with
    'ClientResponseError object is not subscriptable' and the real cause never reached scrape.log.
    """
    ok, failed = [], []
    for (x, y, _url), result in zip(tiles, results):
        if isinstance(result, Exception):
            failed.append(((x, y), result))
        elif isinstance(result, BaseException):
            # Not Exception: a CancelledError captured here and re-raised from download_single_pano would
            # sail past download_panorama_images' `except Exception` and abort the whole run rather than
            # one pano. Let it be what it is.
            raise result
        else:
            ok.append(result)
    return ok, failed


def _tile_body_size(data):
    """The tile body's pixel dimensions, read from the JPEG header without decoding the image."""
    with Image.open(BytesIO(data)) as tile_image:
        return tile_image.size


def _stitch_cell_size(tile_results):
    """The pixel size each grid cell occupies in the stitch: the largest body this fan-out returned.

    Defence in depth rather than a live requirement, since dropping `fover` from _CBK_BASE_URL removed the
    only known cause of undersized bodies. It stays because the failure it prevents is silent and expensive:
    with fover, CBK returned 256x256 bodies for the polar rows of every zoom-5 grid, and pasting one of those
    at the full grid pitch leaves three quarters of its cell black in a pano that is still saved as success.
    A half-size body is the same grid cell rendered at half scale (proven against the zoom-4 tile covering
    the same region in tests/test_gsv_tile_contract.py), so cells still tile the pano and only need bringing
    to a common scale.

    Taking the largest keeps the best imagery the fan-out actually got. If every body were undersized the
    cell would simply be smaller, so the canvas is a quarter of the size and one final LANCZOS pass does the
    upscaling instead of 512 per-tile ones.
    """
    if not tile_results:
        return (TILE_SIZE, TILE_SIZE)
    sizes = [_tile_body_size(data) for _x, _y, data in tile_results]
    return (max(s[0] for s in sizes), max(s[1] for s in sizes))


def _undersized_tile_count(tile_results):
    """How many bodies came back below the NOMINAL tile size - i.e. how much of this pano is half-resolution.

    Measured against TILE_SIZE rather than against _stitch_cell_size: if every body in a fan-out were
    undersized the cell size would itself be 256, so nothing would look undersized relative to its
    neighbours even though the whole pano arrived at half resolution. Undersized means "smaller than what
    CBK serves", which is a fixed 512.

    Expected to be 0 on every pano now that `fover` is gone. It is kept as the tripwire for that: if this
    ever fires, some request parameter has started costing us resolution again (#73).
    """
    return sum(1 for _x, _y, data in tile_results
               if min(_tile_body_size(data)) < TILE_SIZE)


def _stitch_tiles(tile_results, zoom_dims, final_dims):
    """Paste tiles into a zoom-native canvas, crop to the zoom's true size, and scale to the reported dims.

    Every body is brought to the cell size (see _stitch_cell_size) before pasting. That is what the pre-#44
    `img.resize((512, 512))` was quietly doing: while the URL still carried `fover`, CBK returned half-size
    bodies for the polar rows of zoom 5, and pasting one at the full grid pitch leaves three quarters of its
    cell black - saved as success, exactly the corruption #44 is about. Google never returns a true-size
    short edge body, so an undersized body is never a legitimately narrow edge tile: a real bottom-edge tile
    arrives as a full 512 body black-padded below (tests/fixtures/tiles/z3_edge_bottom.jpg), and the crop
    below is what removes that padding.

    The final resize is what the pre-#44 code's `if zoom == 3` no-op resize was reaching for: downstream
    consumers (label pixel coords, depth-map alignment) assume the JPEG is at the server-reported
    dimensions, so a zoom-3 download is upscaled rather than saved at native size.
    """
    cell_w, cell_h = _stitch_cell_size(tile_results)
    tiles_x = int(math.ceil(zoom_dims[0] / float(TILE_SIZE)))
    tiles_y = int(math.ceil(zoom_dims[1] / float(TILE_SIZE)))
    canvas = Image.new('RGB', (tiles_x * cell_w, tiles_y * cell_h))
    for x, y, data in tile_results:
        with Image.open(BytesIO(data)) as tile_image:
            body = (tile_image if tile_image.size == (cell_w, cell_h)
                    else tile_image.resize((cell_w, cell_h), Image.LANCZOS))
            canvas.paste(body, (cell_w * x, cell_h * y))
    # zoom_dims is in nominal 512-grid pixels; the canvas is in cell pixels, so scale the crop to match.
    crop_w = int(round(zoom_dims[0] * cell_w / float(TILE_SIZE)))
    crop_h = int(round(zoom_dims[1] * cell_h / float(TILE_SIZE)))
    image = canvas.crop((0, 0, crop_w, crop_h))
    if image.size != tuple(final_dims):
        image = image.resize(final_dims, Image.LANCZOS)
    return image


def _black_fraction(image):
    """Exact fraction of black pixels in the frame, via the luma histogram.

    Counted over every pixel rather than a downsampled probe, and by histogram rather than a numpy array so
    it stays a C-level pass with no second copy of a 16384x8192 frame. Both alternatives to an exact count
    are wrong in a way that matters here: an averaging downscale blends a black region into its neighbours
    and reports "slightly dark" for a frame that is three-quarters missing, while a NEAREST probe aliases on
    exactly the sort of regular black/imagery pattern a tiling bug produces.
    """
    luma = image.convert('L')
    return luma.histogram()[0] / float(luma.width * luma.height)


def _reject_mostly_black_stitch(image, pano_id, zoom):
    """Refuse to save a stitch that is mostly black - the one place a tile-grid fault is visible.

    Raised, not returned as failure: like a failed tile, this must not be ledgered downloaded=1 (the skip
    check treats any saved file as done forever), and raising keeps it transient so a fixed grid or a
    recovered endpoint re-attempts the pano instead of blacklisting it.
    """
    black = _black_fraction(image)
    if black > STITCH_MAX_BLACK_FRACTION:
        logging.error("IMAGEDOWNLOAD: pano %s: stitched frame at zoom %s is %.0f%% black (limit %.0f%%); "
                      "refusing to save - the tile grid or the tile responses are wrong, not the imagery",
                      pano_id, zoom, 100 * black, 100 * STITCH_MAX_BLACK_FRACTION)
        raise StitchedPanoMostlyBlackError(
            'pano %s: stitched frame at zoom %s is %.0f%% black' % (pano_id, zoom, 100 * black))


# What fetch_pano_image got, beyond the frame itself. Both extra fields are verdicts about the imagery rather
# than about the request, and both are invisible in the saved JPEG - which is at the reported dims either way -
# so anything that has to judge a download needs them handed back explicitly.
#
#   undersized_tiles - bodies that came back below the nominal 512px tile, i.e. how much of this frame is at
#                      half resolution. Expected to be 0 now that `fover` is gone; it is the tripwire (#73).
#   upscaled         - the grid we could download did not cover the pano's reported frame, so _stitch_tiles
#                      LANCZOS'd it up to reach those dims and the JPEG holds less imagery than it advertises.
StitchedPano = collections.namedtuple('StitchedPano', ['image', 'undersized_tiles', 'upscaled'])


def resolve_zoom_and_dims(pano_info):
    """(width, height, zoom) for `pano_info`, or None when there is nothing to download.

    None is a PERMANENT verdict about the pano, and covers both of its causes: no reported dimensions (there
    is no other source for them, so asking again tomorrow asks the same question), and a black tile at both
    zoom 5 and zoom 3, which is what Google answers for a pano id it no longer serves. Neither is a transient
    condition, so callers ledger it rather than retrying. A network failure here still RAISES, and stays
    transient.

    Costs up to two HTTP requests, and none at all when the dimensions are missing.
    """
    pano_id = pano_info['pano_id']
    pano_dims = (pano_info.get('width'), pano_info.get('height'))
    final_image_width = int(pano_dims[0]) if pano_dims[0] is not None else None
    final_image_height = int(pano_dims[1]) if pano_dims[1] is not None else None

    # There is no legacy-XML path here any more (#52 items 3/4/5). It read a `<pano_id>.xml` for dims and
    # zoom; #39 removed the downloader that wrote those (cbk?output=xml died in 2022), so the files on the
    # store are frozen 2022 metadata. It could only ever run for a pano with an .xml and NO .jpg - the
    # caller's skip check returns first - which is 1 of the 1,025 .xml files sampled across dc, columbus-oh,
    # amsterdam and newberg-or. On that one pano it did harm: a declared num_zoom_levels was trusted over
    # the probe and test-fetched, and a black tile returned DownloadResult.failure, which is PERMANENT
    # under the #41 ledger. So stale 2022 metadata could blacklist a pano Google still serves.

    # Without dims we cannot size the tile grid. Checked before the session is opened, so this case costs
    # nothing.
    if final_image_width is None or final_image_height is None:
        return None

    # Session scoped to the zoom/dimension probes; the tile fan-out uses its own aiohttp session. This runs
    # once per pano, so leaving it unclosed would pile up connection pools until GC (#51).
    with _request_session() as session:
        # The probe is now the only thing that picks a zoom, so it is unconditional - it used to sit behind
        # `if zoom is None:` because the legacy XML could have set one already.
        url_zoom_3 = f'{_CBK_BASE_URL}&zoom=3&x=0&y=0&panoid={pano_id}'
        url_zoom_5 = f'{_CBK_BASE_URL}&zoom=5&x=0&y=0&panoid={pano_id}'

        req_zoom_3 = _get_response(url_zoom_3, session, stream=True)
        im_zoom_3 = Image.open(req_zoom_3)
        req_zoom_5 = _get_response(url_zoom_5, session, stream=True)
        im_zoom_5 = Image.open(req_zoom_5)

        # In some cases (e.g., old GSV images), we don't have zoom level 5, so Google returns a transparent
        # image. This means we need to set the zoom level to 3. Google also returns a transparent image if
        # there is no imagery. So check at both zoom levels. How to check:
        # http://stackoverflow.com/questions/14041562/python-pil-detect-if-an-image-is-completely-black-or-white
        if im_zoom_5.convert("L").getextrema() != (0, 0):
            zoom = 5
        elif im_zoom_3.convert("L").getextrema() != (0, 0):
            zoom = 3
        else:
            # Can't determine zoom.
            return None

    return final_image_width, final_image_height, zoom


def frame_covers_pano(pano_id, width, height, zoom):
    """Does a (width, height) grid at `zoom` cover everything Google serves for this pano?

    Asked by requesting the two tiles just past the assumed grid - one column right, one row down - and
    checking both come back blank. An out-of-range tile is answered 200 OK with an all-black body (pinned
    on real bytes in tests/test_gsv_tile_contract.py), so imagery there means the real pano is larger than
    the frame we were about to fetch.

    The nightly downloader never needs this: it sizes the grid from /adminapi/panos, which is Google's own
    number for a pano Google is currently serving. refetch_panos.py does, because it fetches at the frame
    the STORED file has, which is a scrape-time archive - and Google re-serves panos larger (measured at
    4.6% of a sampled store, reports/2026-08-10-store-coverage.md). Fetching a 26x13 grid for a pano Google
    now holds at 32x16 does not return a smaller version of the pano; it returns the top-left 81% of it, at
    the stored file's exact dimensions, with no undersized tile and no black to give it away. That is a
    silently cropped panorama saved over a correct one, and this is the only cheap thing that catches it.

    Two requests, spent BEFORE the 512-tile fan-out so a bad frame costs two rather than 514.

    The one way this can err is toward ACCEPTANCE: an out-of-range tile is recognised by being exactly black,
    so a real tile past the grid that happened to decode to all-zero luma would read as blank, and the fetch
    would proceed. That is why the x probe is taken on the grid's middle row rather than on row 0. Row 0 is
    the zenith cap - the one strip of a panorama where a uniformly black real tile is plausible - while the
    middle row is horizon-adjacent imagery, which never is. The y probe lands on ground rows for the same
    reason. Both probes must come back blank for the frame to pass.
    """
    tiles_x, tiles_y = _tile_grid(width, height, zoom)
    with _request_session() as session:
        for x, y in ((tiles_x, tiles_y // 2), (0, tiles_y)):
            url = f'{_CBK_BASE_URL}&zoom={zoom}&x={x}&y={y}&panoid={pano_id}'
            probe = Image.open(_get_response(url, session, stream=True))
            if probe.convert('L').getextrema() != (0, 0):
                logging.warning("IMAGEDOWNLOAD: pano %s: tile (%d, %d) past a %dx%d grid at zoom %s has "
                                "imagery; Google serves this pano larger than %dx%d",
                                pano_id, x, y, tiles_x, tiles_y, zoom, width, height)
                return False
    return True


def fetch_pano_image(pano_id, width, height, zoom):
    """Download every tile of `pano_id`'s grid at `zoom` and stitch one frame at (width, height).

    The seam between "get the imagery" and "decide what to do with it" (#52.1's shape, applied one level
    down). download_single_pano composes it with the store's skip check and an atomic save; refetch_panos.py
    composes it with its own acceptance gates, so a repair pass cannot become a second, drifting stitcher.

    Raises rather than returning a verdict for everything transient - a failed tile, a mostly-black stitch -
    because under the #41 ledger semantics both callers must re-attempt those rather than remember them.

    @return StitchedPano(image, undersized_tiles, upscaled).
    """
    final_im_dimension = (width, height)

    tiles = _generate_tile_urls(pano_id, width, height, zoom)
    results = asyncio.run(_download_tiles(tiles))
    ok, failed = _partition_tile_results(tiles, results)
    if failed:
        # Fail the whole pano: a partial stitch would leave silently-black regions that downstream crops
        # can't detect - exactly the corruption #44 is about. Raise (rather than return failure) so the
        # failure is treated as transient: the tile that timed out today usually exists tomorrow, and under
        # #41's ledger semantics a raised pano is re-attempted next run instead of blacklisted.
        (x, y), first_error = failed[0]
        logging.error("IMAGEDOWNLOAD: pano %s: %d/%d tiles failed; first failure: tile (%d, %d): %r",
                      pano_id, len(failed), len(tiles), x, y, first_error)
        raise first_error

    degraded = _undersized_tile_count(ok)
    if degraded:
        # Should never fire now that `fover` is gone (#73). Not a failure if it does: a half-size body is
        # the same cell at half scale, so the stitch is still correct and full-frame, just softer. But it
        # means some request parameter has started costing us resolution again, and that is invisible in the
        # saved JPEG - which is at the reported dims either way.
        logging.warning("IMAGEDOWNLOAD: pano %s: %d/%d tiles came back below the nominal %dpx tile; "
                        "stitching at reduced resolution - check the CBK request parameters (#73)",
                        pano_id, degraded, len(ok), TILE_SIZE)

    zoom_dims = _dims_at_zoom(width, height, zoom)
    image = _stitch_tiles(ok, zoom_dims, final_im_dimension)
    _reject_mostly_black_stitch(image, pano_id, zoom)

    # The upscaled test is whether the grid we could actually download covers the pano's reported frame; if
    # it doesn't, _stitch_tiles LANCZOS-upscaled to reach it. Deliberately NOT `zoom == 3`: an old four-level
    # pano (3328x1664) has max zoom 3, so zoom 3 IS its native resolution and nothing was lost - calling that
    # degraded would put a permanent false positive in front of ops on the oldest imagery in the store.
    upscaled = zoom_dims != final_im_dimension
    if upscaled:
        logging.info("IMAGEDOWNLOAD: pano %s: only zoom %s was available for a %dx%d frame; stitched %dx%d "
                     "and upscaled", pano_id, zoom, width, height, *zoom_dims)
    return StitchedPano(image, degraded, upscaled)


def download_single_pano(storage_path, pano_info):
    pano_id = pano_info['pano_id']

    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        # exist_ok: concurrent city runs (and the depth phase) race on shard dirs.
        os.makedirs(destination_dir, exist_ok=True)
        try:
            os.chmod(destination_dir, 0o775 | stat.S_ISGID)
        except PermissionError:
            pass  # lost the race to another user's process; their dir, their modes — must not fail the pano

    filename = pano_id + ".jpg"
    out_image_name = os.path.join(destination_dir, filename)

    # Skip download if image already exists. Before the probe on purpose: an image on disk is the resume
    # marker, so a pano already downloaded must cost zero requests.
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    resolved = resolve_zoom_and_dims(pano_info)
    if resolved is None:
        # No dims, or no imagery at any zoom - both permanent properties of the pano, so the #41 ledger
        # writes downloaded=0 and never re-attempts it.
        return DownloadResult.failure
    final_image_width, final_image_height, zoom = resolved

    stitched = fetch_pano_image(pano_id, final_image_width, final_image_height, zoom)
    # atomic_output_path, not a direct save: an image on disk IS the resume marker, so a mid-write crash
    # would otherwise leave a truncated .jpg that every later run reports as a completed download.
    with atomic_output_path(out_image_name) as tmp_path:
        stitched.image.save(tmp_path, 'jpeg')

    # log.csv column 8 (#52 item 2): the JPEG on disk holds less imagery than its dimensions advertise. See
    # fetch_pano_image for why the test is not `zoom == 3`.
    if stitched.upscaled:
        return DownloadResult.fallback_success
    return DownloadResult.success


DEPTH_LOG_FILENAME = 'depth_log.csv'
DEPTH_ARTIFACT_SUFFIX = '.depth.npz'

# Stamped into every artifact so consumers can tell formats apart. Artifacts with no format_version field
# predate v2 and store streetlevel's raw column order, which is x-mirrored relative to the pano JPEG (#58).
# v3 adds Google's plane list - per-pixel plane indices plus plane normals and offsets - which the v2 decode
# threw away (#56). A v2 artifact cannot be upgraded offline (the planes were never stored): delete it and its
# depth_log.csv row to trigger a re-fetch.
DEPTH_ARTIFACT_FORMAT_VERSION = 3

# The value a depth pixel carries when Google modelled no plane there (sky, or anything else it skipped) -
# streetlevel's depth.INFINITELY_FAR. Reconstructed depths are |d / (v . n)|, so they are never negative and
# the sentinel is unambiguous.
DEPTH_NO_PLANE = -1.0

# Consecutive transient failures after which the depth phase gives up for this run. Without a breaker, a run that
# hits a wall (rate limit, captcha, DNS outage) spends its whole --max-runtime budget re-hitting it, then does the
# same thing again the next night.
DEPTH_MAX_CONSECUTIVE_FAILURES = 25

# consecutive-failure count -> seconds to sleep before carrying on. Gives a blip a chance to clear before we spend
# the breaker, without pounding while we wait. Storage failures skip the retreat: a full disk cannot clear itself.
DEPTH_RETREAT_SCHEDULE = {5: 30, 10: 120, 15: 300}

# Values for download_depth_maps' stop_reason - constants so the set sites and the end-of-phase compare sites
# can't drift apart via a typo that silently disables a warning arm.
DEPTH_STOP_BLOCKED = 'blocked'
DEPTH_STOP_CONSECUTIVE_FAILURES = 'consecutive-failures'
DEPTH_STOP_MAX_RUNTIME = 'max-runtime'
DEPTH_STOP_MAX_REQUESTS = 'max-requests'

# Substrings that mark Google's "you are a robot" landing pages rather than pano metadata.
_BLOCK_URL_MARKERS = ('/sorry/', 'consent.google.com')


class DepthBlockedError(Exception):
    """Google answered a photometa request with a rate-limit or captcha/consent interstitial instead of data."""


class DepthPayloadError(RuntimeError):
    """Google's depth payload - or the v3 artifact about to be derived from it - is malformed.

    A RuntimeError subclass on purpose, and deliberately NOT a ValueError: download_depth_maps classes
    ValueError as 'network' (streetlevel surfaces a non-JSON response as JSONDecodeError, a ValueError
    subclass), and a payload this scraper decoded and then rejected is a data fault, not a network one.
    RuntimeError lands in the 'unexpected' arm instead - still transient and still unledgered, so the pano
    retries next run, but the end-of-phase breakdown names the right cause.
    """


def _raise_if_blocked(response, *args, **kwargs):
    """requests response hook: spot Google pushing back before the response reaches streetlevel's JSON parser.

    The photometa endpoint doesn't answer scraping pressure with a 429 - it serves, or redirects to, an
    interstitial carrying HTTP 200, which streetlevel then fails to parse. Without this hook that is
    indistinguishable from one pano having a malformed payload, so a blocked run would keep hammering instead of
    standing down. Hooks fire on redirect hops too, hence checking Location as well as the landing URL.
    """
    if response.status_code == 403:
        raise DepthBlockedError("HTTP 403 from %s" % (response.url))
    for url in (response.url or '', response.headers.get('Location', '')):
        for marker in _BLOCK_URL_MARKERS:
            if marker in url:
                raise DepthBlockedError("redirected to %s" % (url))


def _depth_session():
    """Build the requests.Session handed to streetlevel for photometa requests.

    Same retry policy and default timeout as _request_session(), plus backoff jitter and the
    block-detection hook.

    Deliberately does NOT borrow config.headers_list the way the tile downloader does. streetlevel sends its own
    Accept/Host/Referer/User-Agent on every photometa request, and in requests a request-level header beats a
    session-level one, so all of those would be dead on arrival - leaving only the leftovers (Accept-Language,
    Upgrade-Insecure-Requests, DNT) to contradict streetlevel's Firefox User-Agent, which is a more anomalous
    fingerprint than either set alone. Proxies still have to live on the session because streetlevel passes no
    per-request options.
    """
    session = requests.Session()
    # backoff_jitter keeps concurrent city runs from resynchronising onto an identical retry schedule after a
    # shared outage and pounding in lockstep (requires urllib3 >= 2.0).
    retry = Retry(total=5, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1,
                  backoff_jitter=0.5)
    adapter = _TimeoutHTTPAdapter(max_retries=retry, timeout=30)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.proxies.update({k: v for k, v in _proxies.items() if v})
    session.hooks['response'].append(_raise_if_blocked)
    return session


def _pace(last_request_at):
    """Sleep so consecutive depth requests are at least config.depth_min_request_interval apart.

    @param last_request_at time.monotonic() of the previous request, or None if this is the first one.
    """
    if depth_min_request_interval <= 0 or last_request_at is None:
        return
    # Jitter on top of the floor so overlapping city runs don't settle into a shared cadence.
    wait = depth_min_request_interval - (time.monotonic() - last_request_at)
    wait += random.uniform(0, depth_min_request_interval * 0.25)
    if wait > 0:
        time.sleep(wait)


def _load_depth_log(depth_log_path):
    """Read the depth ledger into a set of resolved pano ids.

    Tolerates malformed rows (e.g. a line truncated by a crash mid-append) by skipping them, so a damaged ledger
    degrades to re-checking a few panos rather than crashing the run.

    @return Set of pano ids whose depth outcome is already known ('saved' or 'unavailable').
    """
    resolved = set()
    if not os.path.isfile(depth_log_path):
        return resolved
    with open(depth_log_path, newline='') as f:
        for row in csv.reader(f):
            if len(row) == 2 and row[0] != 'pano_id' and row[1] in ('saved', 'unavailable'):
                resolved.add(row[0])
    return resolved


def count_unresolved_depth(storage_path, pano_infos):
    """Count panos whose depth outcome is not yet recorded in the depth ledger.

    A cheap local read of depth_log.csv (see _load_depth_log) — no network. DownloadRunner uses it to decide
    whether --min-depth-runtime should reserve image time at all: with nothing unresolved the depth phase
    returns in milliseconds, so a reservation would just burn image throughput.

    A pano whose artifact exists but whose ledger row is missing (only possible after manual ledger surgery)
    counts as unresolved even though the phase will self-heal it without a request; that inaccuracy is not
    worth a directory walk here. An unreadable ledger counts as no backlog: download_depth_maps sits the run
    out in that state, so there is nothing to reserve for.

    @param storage_path Root of the pano store (depth_log.csv lives here).
    @param pano_infos   Pano dicts (needs 'pano_id'); callers pre-filter to source == 'gsv'.
    @return             Number of panos with no 'saved'/'unavailable' ledger row.
    """
    try:
        resolved = _load_depth_log(os.path.join(storage_path, DEPTH_LOG_FILENAME))
    except OSError:
        return 0
    return sum(1 for p in pano_infos if p['pano_id'] not in resolved)


# Google's plane data for one pano, as decoded from the raw photometa depth payload (#56): 'indices' is a
# uint8 (h, w) array of per-pixel indices into the plane list (0 = no plane), in PAYLOAD column order - which
# is the pano JPEG's order (see _write_depth_artifact); 'normals' is float32 (P, 3) and 'distances' float32
# (P,), both verbatim wire values. A plane is {p : p . n = d}, so its perpendicular distance from the camera
# is |d| / ||n|| - for the ground plane, that is the camera height.
DepthPlanes = collections.namedtuple('DepthPlanes', ['indices', 'normals', 'distances'])


# The header every depth payload opens with, little-endian and unpadded:
# uint8 header_size | uint16 number_of_planes | uint16 width | uint16 height | uint8 offset.
_DEPTH_HEADER = struct.Struct('<BHHHB')


def _decode_depth_planes(b64_payload):
    """Decode Google's depth payload into the plane data streetlevel's parser computes with and discards.

    streetlevel's compute_depth_map collapses the planes into one scalar per pixel and never exposes them, so
    capturing camera height and ground tilt (#56) means decoding the payload ourselves. Wire layout (see
    _DEPTH_HEADER, and tests/test_streetlevel_api.py for the end-to-end pin): the 8-byte header, then
    width*height uint8 per-pixel plane indices at `offset`, then 4 float32 (nx, ny, nz, d) per plane.

    NB the offset is a true uint8 at byte 7. streetlevel reads it as a uint16 spanning bytes 7-8, and that is
    STILL true in 0.12.11 - the latest release and the floor of our pin - whose depth.py is byte-for-byte
    identical to 0.12.10's, so the pin does not fix it (pinned by test_streetlevel_api's
    test_streetlevel_still_misreads_the_depth_offset, which also says what to simplify when upstream's fix,
    sk-zk/streetlevel#45, ships). The misread parses correctly only when the first index byte is 0 - true on
    most panos, whose zenith is sky, but false wherever Google models the surface overhead. That is why the
    depth path no longer routes through streetlevel's parser at all: see _fetch_pano_with_depth_planes.

    @return DepthPlanes.
    @raise  DepthPayloadError on a truncated or malformed payload, rather than silently returning short
            arrays that would then be stored as a plausible-looking artifact.
    """
    padded = b64_payload + '=' * ((4 - len(b64_payload) % 4) % 4)
    raw = base64.urlsafe_b64decode(padded)
    if len(raw) < _DEPTH_HEADER.size:
        raise DepthPayloadError("depth payload header truncated: %d bytes" % (len(raw),))
    header_size, number_of_planes, width, height, offset = _DEPTH_HEADER.unpack_from(raw, 0)
    # Every field after byte 0 sits at a fixed position, so a payload announcing a different header size is a
    # wire format this decode does not know and would mis-parse silently rather than reject. Likewise an
    # offset pointing back into the header, which would hand back header bytes dressed up as plane indices.
    if header_size != _DEPTH_HEADER.size or offset < _DEPTH_HEADER.size:
        raise DepthPayloadError("unexpected depth payload header: header_size=%d, offset=%d (expected %d and "
                                ">= %d)" % (header_size, offset, _DEPTH_HEADER.size, _DEPTH_HEADER.size))
    if width == 0 or height == 0:
        raise DepthPayloadError("depth payload declares a zero-area raster: %dx%d" % (width, height))
    indices_end = offset + width * height
    planes_end = indices_end + number_of_planes * 16
    if len(raw) < planes_end:
        raise DepthPayloadError("depth payload truncated: %d bytes, need %d" % (len(raw), planes_end))
    indices = np.frombuffer(raw, dtype=np.uint8, count=width * height, offset=offset)
    max_index = int(indices.max())
    if max_index and max_index >= number_of_planes:
        # _compute_depth_raster gathers planes by these indices; an index past the declared list must be
        # refused here, not surface as a bare IndexError mid-computation (which is how streetlevel fails
        # the same payload).
        raise DepthPayloadError("plane index %d out of range: payload declares %d plane(s)"
                                % (max_index, number_of_planes))
    planes = np.frombuffer(raw, dtype='<f4', count=number_of_planes * 4, offset=indices_end)
    planes = planes.reshape(number_of_planes, 4)
    return DepthPlanes(indices.reshape(height, width).copy(), planes[:, :3].astype(np.float32),
                       planes[:, 3].astype(np.float32))


def _compute_depth_raster(planes):
    """The per-pixel distance raster derived from the plane data, in PAYLOAD (= stored JPEG) column order.

    The same geometry as streetlevel's compute_depth_map - t = |d_i / (v(r, c) . n_i)| for the referenced
    plane, DEPTH_NO_PLANE where the index is 0 - vectorized, and WITHOUT the x-mirror streetlevel applies on
    output (the mirror is its output convention, not the wire's). This is the reconstruction identity the
    artifact documents, running forward. Parity with upstream's decode is pinned by
    tests/test_depth_helpers.py's TestComputeDepthRaster, so swapping the raster source changed nothing for
    the panos both decoders handle. Near the horizon the ground plane runs almost parallel to the ray and
    distances legitimately grow huge - exactly as upstream's decode produces.
    """
    indices = planes.indices
    height, width = indices.shape
    theta = (height - np.arange(height) - 0.5) / height * np.pi
    phi = (width - np.arange(width) - 0.5) / width * 2.0 * np.pi + np.pi / 2.0
    if len(planes.normals) == 0:
        return np.full((height, width), DEPTH_NO_PLANE, dtype=np.float32)
    rays = np.empty((height, width, 3))
    rays[..., 0] = np.sin(theta)[:, None] * np.cos(phi)[None, :]
    rays[..., 1] = np.sin(theta)[:, None] * np.sin(phi)[None, :]
    rays[..., 2] = np.broadcast_to(np.cos(theta)[:, None], (height, width))
    index_grid = indices.astype(np.intp)  # bounds-checked in _decode_depth_planes
    normals = np.asarray(planes.normals, dtype=np.float64)[index_grid]
    offsets = np.asarray(planes.distances, dtype=np.float64)[index_grid]
    # A ray exactly perpendicular to its plane's normal is measure-zero in real payloads; inf beats crashing
    # the pano over it (streetlevel would raise ZeroDivisionError there).
    with np.errstate(divide='ignore', invalid='ignore'):
        raster = np.abs(offsets / np.einsum('hwc,hwc->hw', rays, normals))
    return np.where(index_grid == 0, DEPTH_NO_PLANE, raster).astype(np.float32)


def _msg_path(value, *path):
    """Walk one nested-list path of a photometa msg, returning None when any hop is missing.

    The same tolerance rules as streetlevel's try_get (IndexError/KeyError/TypeError -> None), for the
    handful of fields the depth path reads now that it no longer routes through streetlevel's parser.
    """
    for key in path:
        try:
            value = value[key]
        except (IndexError, KeyError, TypeError):
            return None
    return value


# What the depth path needs from a photometa response: the raster (None when the pano carries no depth
# payload) and the three orientation scalars, shaped like the streetlevel object it replaced so
# _write_depth_artifact and the tests' make_pano are indifferent to the source.
_DepthRaster = collections.namedtuple('_DepthRaster', ['data'])
_PanoOrientation = collections.namedtuple('_PanoOrientation', ['depth', 'heading', 'pitch', 'roll'])


def _fetch_pano_with_depth_planes(pano_id, session):
    """One photometa request -> (pano-shaped namespace | None, DepthPlanes | None).

    Only streetlevel's api half (the protobuf-URL builder + fetch) is used; the response is parsed here.
    The session - and with it the timeout adapter, retry policy, and block-detection hook - passes through
    exactly as streetlevel's own find_panorama_by_id would pass it, so the request on the wire is identical
    and the one-request-per-pano budget is unchanged. The msg paths and the api half's signature are pinned
    by tests/test_streetlevel_api.py.

    WHY the parse half is bypassed, and when to revisit: parse_panorama_id_response calls streetlevel's
    depth decoder unguarded, and every release through 0.12.11 misreads the payload's uint8 offset byte as
    a uint16 (test_streetlevel_still_misreads_the_depth_offset). Any pano whose first index byte is nonzero
    - a modelled zenith: tunnels, overpass soffits, parking structures; a bit under 1% of panos in a large
    sample - therefore raises inside streetlevel on every attempt, is classed transient, and re-requests
    forever without ever resolving. The fix is upstream but unmerged (sk-zk/streetlevel#45); once it ships
    in a release and the pin moves past it, this bypass becomes unnecessary rather than wrong - either
    simplify back to parse_panorama_id_response, or keep it as the first step of dropping the dependency
    (see the #56 discussion).
    """
    # Imported lazily: download_depth_maps' availability probe has already run, and tests reach this seam
    # through an adapter, so the real submodule only loads when a real request is about to happen.
    from streetlevel.streetview import api

    response = api.find_panorama_by_id(pano_id, download_depth=True, locale='en', session=session)
    response_code = _msg_path(response, 1, 0, 0, 0)
    if response_code is None:
        # Not the photometa envelope at all - an error JSON, a quota page that happened to parse. Transient:
        # returning (None, None) instead would ledger 'unavailable' and permanently write off a pano Google
        # may still be serving.
        raise DepthPayloadError("unrecognized photometa response for pano %s" % (pano_id,))
    # 1 = OK, 3 = also OK; 2 = not found (streetlevel's reading of the same field).
    if response_code not in (1, 3):
        return None, None
    msg = response[1][0]
    # Orientation scalars, with streetlevel's conversions: degrees -> radians, and pitch stored as 90 - raw.
    heading = _msg_path(msg, 5, 0, 1, 2, 0)
    pitch = _msg_path(msg, 5, 0, 1, 2, 1)
    roll = _msg_path(msg, 5, 0, 1, 2, 2)
    orientation = _PanoOrientation(
        depth=None,
        heading=math.radians(heading) if heading is not None else None,
        pitch=math.radians(90 - pitch) if pitch is not None else None,
        roll=math.radians(roll) if roll is not None else None)
    payload = _msg_path(msg, 5, 0, 5, 1, 2)
    if not payload:
        return orientation, None
    planes = _decode_depth_planes(payload)
    # The raster is handed back in streetlevel's x-mirrored column order deliberately: _write_depth_artifact
    # un-mirrors on write (#58) and every CI pin on the stored orientation is written against that contract,
    # so the seam keeps the shape its predecessor produced and the two flips cancel. Collapse them if the
    # writer contract is ever revisited.
    raster = _compute_depth_raster(planes)[:, ::-1]
    return orientation._replace(depth=_DepthRaster(raster)), planes


def _write_depth_artifact(storage_path, pano_id, pano, planes):
    """Atomically write <pano_id[:2]>/<pano_id>.depth.npz for a streetlevel pano with depth data.

    Contents (format v3, see DEPTH_ARTIFACT_FORMAT_VERSION):
      'depth'          float32 (h, w) meters; -1 = no plane (sky, or anything Google didn't model)
      'plane_indices'  uint8 (h, w) per-pixel index into the plane list; 0 = no plane
      'planes_n'       float32 (P, 3) plane normals, verbatim wire values (#56)
      'planes_d'       float32 (P,) plane offsets, verbatim; a plane is {p : p . n = d}, so its perpendicular
                       distance from the camera is |d| / ||n|| (the ground plane's is the camera height -
                       see ground_plane_from_artifact / camera_height_from_artifact)
      'heading'/'pitch'/'roll'  scalars in radians (NaN if absent)
      'format_version' int

    The stored raster shares the pano JPEG's column order: streetlevel's decoder x-mirrors the payload
    (compute_depth_map writes the value for payload column x to output column w-1-x), so pano.depth.data is
    horizontally flipped relative to the imagery and is flipped back here on write (#58). plane_indices comes
    from the raw payload, whose column order already IS the JPEG's, so it is stored verbatim - and the plane
    normals live in the pano-local frame of the decode's ray formula, untouched by any raster relabeling.
    The operational definition of that frame, tying every stored field together (pinned by
    tests/test_depth_helpers.py):

        depth[r, c] == |planes_d[i] / (v(r, c) . planes_n[i])|   for i = plane_indices[r, c] > 0,
        v(r, c) = unit ray at theta = (h-r-0.5)/h*pi, phi = (w-c-0.5)/w*2pi + pi/2

    tests/test_streetlevel_api.py pins the decode's end-to-end column order - the ray-direction formula and
    the write index jointly, either of which flipping alone would change the orientation - so a streetlevel
    change fails CI rather than silently re-mirroring new artifacts.

    @raise DepthPayloadError if the plane data is missing, the wrong shape, or disagrees with the raster.
    """
    if planes is None:
        raise DepthPayloadError("pano %s has a depth raster but no plane data; refusing to write a malformed "
                                "v3 artifact" % (pano_id,))
    stored_depth = np.asarray(pano.depth.data)[:, ::-1].astype(np.float32)
    if tuple(planes.indices.shape) != stored_depth.shape:
        raise DepthPayloadError("pano %s plane indices shape %r does not match depth shape %r"
                                % (pano_id, tuple(planes.indices.shape), stored_depth.shape))
    # The one invariant docs/depth.md promises consumers - index 0 sits exactly where the raster says -1 -
    # enforced instead of assumed. Both now come from our own decode, so this is no longer a cross-parser
    # check; what it still guards, for one pass over ~130k pixels, is the flip plumbing between the seam and
    # this writer (payload order -> mirrored -> un-mirrored) and any raster/index divergence a future edit
    # introduces. This backfill is one-shot and cannot be redone offline, so a broken invariant must stop
    # the pano, not quietly produce millions of artifacts whose documented reconstruction identity does not
    # hold.
    if not np.array_equal(planes.indices == 0, stored_depth == DEPTH_NO_PLANE):
        raise DepthPayloadError("pano %s plane indices disagree with the depth raster: %d pixel(s) where "
                                "exactly one of (index == 0, depth == %g) holds"
                                % (pano_id, int(np.count_nonzero((planes.indices == 0)
                                                                 != (stored_depth == DEPTH_NO_PLANE))),
                                   DEPTH_NO_PLANE))

    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        # exist_ok: concurrent city runs (and the image phase) race on shard dirs.
        os.makedirs(destination_dir, exist_ok=True)
        try:
            os.chmod(destination_dir, 0o775 | stat.S_ISGID)
        except PermissionError:
            pass  # lost the race to another user's process; their dir, their modes — must not fail the pano

    final_path = os.path.join(destination_dir, pano_id + DEPTH_ARTIFACT_SUFFIX)

    def scalar(value):
        return float(value) if value is not None else float('nan')

    # .part + rename so a crash can never leave a truncated .npz that would be treated as done forever; the
    # image downloaders now share the same helper.
    with atomic_output_path(final_path) as tmp_path:
        # savez_compressed needs an open file object: given a path without a .npz extension it silently appends
        # one, which would write to the wrong filename.
        with open(tmp_path, 'wb') as f:
            np.savez_compressed(f, depth=stored_depth,
                                plane_indices=np.asarray(planes.indices, dtype=np.uint8),
                                planes_n=np.asarray(planes.normals, dtype=np.float32).reshape(-1, 3),
                                planes_d=np.asarray(planes.distances, dtype=np.float32).reshape(-1),
                                heading=scalar(pano.heading), pitch=scalar(pano.pitch),
                                roll=scalar(pano.roll), format_version=DEPTH_ARTIFACT_FORMAT_VERSION)


def ground_plane_from_artifact(artifact, min_vertical=0.7):
    """Pick the ground plane out of a v3 depth artifact: the near-horizontal plane that most of the pano's
    downward-looking pixels actually land on.

    Deliberately a helper rather than a field baked into the artifact: the artifact stores Google's plane
    list verbatim, so this heuristic (which plane is "ground" on a tilted street, a bridge, a plaza?) stays
    fixable in code instead of frozen into millions of .npz files. Sign-insensitive throughout - the up/down
    sign convention of Google's pano-local frame is not relied on.

    Candidates are drawn only from the below-horizon rows of the raster, and are ranked by how many of those
    pixels reference them (ties broken by verticality, then by lowest index for determinism). Both rules
    matter. Ranking on verticality alone lets a handful of pixels of some *overhead* surface - an overpass
    soffit, a tunnel ceiling, an awning, a sign gantry, all of which are more perfectly horizontal than a
    real cambered road - outrank the tens of thousands of pixels of actual road, and
    camera_height_from_artifact then silently returns the height of the ceiling. The split is not a fudge
    factor: rows from (h+1)//2 on are exactly those whose rays satisfy theta < pi/2, i.e. that point
    strictly below the horizon, under the same ray formula the stored frame is defined by (see
    _write_depth_artifact). For even heights - every real raster - that is plain h//2; the +1 matters only
    for odd heights, whose middle row sits exactly ON the horizon and belongs to neither half.

    @param artifact     An open numpy.load(...) NpzFile, or any mapping with 'plane_indices', 'planes_n',
                        'planes_d' (see _write_depth_artifact for the fields).
    @param min_vertical Minimum |n_z| / ||n|| for a plane to count as ground at all.
    @return             (unit_normal float32 (3,), distance_m, plane_index) for the winning plane - the
                        distance is the camera height when the plane really is the ground - or None if no
                        plane below the horizon is vertical enough. No fallback to the top half: a wrong
                        camera height is worse than an absent one, which the caller can default.
    """
    indices = np.asarray(artifact['plane_indices'])
    if indices.ndim != 2:
        raise ValueError("plane_indices must be the (h, w) raster, got shape %r" % (indices.shape,))
    normals = np.asarray(artifact['planes_n'], dtype=np.float64)
    distances = np.asarray(artifact['planes_d'], dtype=np.float64)
    # One pass gives both the referenced set and the pixel counts, and beats np.unique over ~65k values.
    # minlength keeps every plane addressable below even when no pixel references the tail of the list.
    support = np.bincount(indices[(indices.shape[0] + 1) // 2:].ravel(), minlength=len(normals))
    best = None
    # Index 0 is the no-plane sentinel, so the scan starts at 1; stopping at len(normals) drops out-of-range
    # indices, which would mean a malformed artifact. support is at least that long, by minlength above.
    for index in range(1, len(normals)):
        count = int(support[index])
        if count == 0:
            continue
        length = float(np.linalg.norm(normals[index]))
        if length == 0.0:
            continue
        verticality = abs(float(normals[index][2])) / length
        if verticality < min_vertical:
            continue
        if best is None or (count, verticality) > best[0]:
            best = ((count, verticality), int(index), length)
    if best is None:
        return None
    _, index, length = best
    return (normals[index] / length).astype(np.float32), float(abs(distances[index]) / length), index


def camera_height_from_artifact(artifact, default=None):
    """Camera height in meters from a v3 depth artifact: |d| / ||n|| of the ground plane (#56).

    @return The height, or `default` when no plane qualifies as ground (see ground_plane_from_artifact).
    """
    ground = ground_plane_from_artifact(artifact)
    return default if ground is None else ground[1]


def download_depth_maps(storage_path, pano_infos, run_start_monotonic=None, max_runtime_minutes=None,
                        max_requests=None):
    """Fetch GSV depth maps via the streetlevel library for every pano in pano_infos.

    Callers pre-filter to source == 'gsv'. Depth rides Google's photometa response, so this costs one metadata
    request per unresolved pano. Outcomes are remembered in <storage_path>/depth_log.csv: 'saved' (artifact
    written) and 'unavailable' (pano gone or no depth payload — permanent for a given pano id, never retried).
    Transient errors — including storage failures — are counted as failures but NOT ledgered, so they retry on the
    next run. The artifact on disk is the ground truth; deleting the ledger just makes the next run re-stat
    artifacts (re-appending 'saved') and re-request unresolved panos.

    Unresolved panos are shuffled, so a cluster that fails on every run can't permanently starve max_requests.
    The phase stops early if Google starts refusing requests (see DepthBlockedError) or after
    DEPTH_MAX_CONSECUTIVE_FAILURES transient failures in a row, rather than spending the rest of the budget on a
    wall. config.depth_min_request_interval paces requests if set.

    Note that 'unavailable' — a permanent, expected, non-actionable outcome — is counted in fail_count, and so
    lands in log.csv's depth failure column. That column is therefore not usable as an alert signal; the split is
    printed to stdout and scrape.log instead.

    @param storage_path        Root of the pano store (shard dirs + depth_log.csv live here).
    @param pano_infos          Pano dicts (needs 'pano_id'); typically the full /adminapi/panos list, which is
                               what backfills panos downloaded before this feature existed.
    @param run_start_monotonic Shared run start, a time.monotonic() value, used for the max_runtime_minutes
                               budget. Monotonic, not the wall clock: an NTP step or DST transition must not
                               stretch or shrink the budget (#51).
    @param max_runtime_minutes Stop starting new requests once this much time has elapsed since run start.
    @param max_requests        Stop after this many HTTP attempts this run (manual backfill throttle).
    @return                    (success_count, fail_count, skipped_count, total_completed).
    """
    try:
        # Availability probe only - the fetch seam (_fetch_pano_with_depth_planes) imports the submodules it
        # needs lazily, per request.
        from streetlevel import streetview  # noqa: F401
    except ImportError as e:
        logging.error("DEPTHDOWNLOAD: streetlevel is not installed (%s); skipping depth phase", str(e))
        return 0, 0, 0, 0

    total_panos = len(pano_infos)
    success_count, fail_count, skipped_count, unavailable_count = 0, 0, 0, 0
    request_count = 0
    consecutive_failures = 0
    # Why the phase stopped early, if it did - one of the DEPTH_STOP_* constants, or None if the phase worked
    # through its whole list. The breaker is fed by storage failures as well as network ones, so the cause of a
    # trip is whatever streak_classes and last_error say, not necessarily Google.
    stop_reason = None
    last_error = None
    # Per-class counts (storage/network/unexpected) over the CURRENT failure streak, plus the class of the most
    # recent failure. Reset wherever consecutive_failures resets, so a breaker trip can name its dominant cause
    # even when the last error is the minority class.
    streak_classes = collections.Counter()
    failure_class = None

    depth_log_path = os.path.join(storage_path, DEPTH_LOG_FILENAME)
    log_existed = os.path.isfile(depth_log_path)
    try:
        resolved_ids = _load_depth_log(depth_log_path)
    except OSError as e:
        # Deliberately not degrading to "nothing is resolved": that would re-request the entire corpus against an
        # already-sick store. If the ledger can't be read, sit this run out.
        logging.error("DEPTHDOWNLOAD: Cannot read %s (%s); skipping the depth phase", depth_log_path, str(e))
        print("DEPTHDOWNLOAD: Cannot read the depth ledger (%s). Skipping the depth phase." % (e))
        return 0, 0, 0, 0

    # Partition before requesting anything. Counting the ledger skips up front keeps log.csv's 'skipped' column
    # honest even when a budget cuts the run short, and it means the shuffle below only has to touch panos we
    # might actually fetch - after backfill that's a small set, so this stays cheap on a multi-million-pano corpus.
    candidates = [p for p in pano_infos if p['pano_id'] not in resolved_ids]
    skipped_count = total_panos - len(candidates)

    # Shuffle so a cluster of panos that fail every time can't monopolise --max-depth-requests run after run and
    # starve the rest of the backfill: iteration order is otherwise stable, so the same head block would be
    # re-attempted forever and never make progress.
    random.shuffle(candidates)

    try:
        depth_log = open(depth_log_path, 'a', newline='')
        ledger = csv.writer(depth_log)
        if not log_existed:
            ledger.writerow(['pano_id', 'status'])
            depth_log.flush()
            os.chmod(depth_log_path, 0o664)
    except OSError as e:
        # The ledger isn't writable at all (full, read-only, or the sshfs mount dropped). Without it nothing could
        # be remembered, so there's no useful work this run - but don't take the whole run down over it.
        logging.error("DEPTHDOWNLOAD: Cannot write %s (%s); skipping the depth phase", depth_log_path, str(e))
        # WARNING token so an ops grep for storage trouble matches this at-start message the same as the
        # end-of-phase ones - a store unmounted before the run is likelier than one filling during it.
        print("DEPTHDOWNLOAD: WARNING - cannot write the depth ledger (%s). Skipping the depth phase." % (e))
        return 0, 0, skipped_count, skipped_count

    # Created after the ledger so an early return can't leak it; the with closes both (#51).
    session = _depth_session()
    with depth_log, session:

        def record(pano_id, status):
            ledger.writerow([pano_id, status])
            depth_log.flush()
            resolved_ids.add(pano_id)

        last_request_at = None
        for pano_info in candidates:
            pano_id = pano_info['pano_id']

            artifact_path = os.path.join(storage_path, pano_id[:2], pano_id + DEPTH_ARTIFACT_SUFFIX)
            if os.path.isfile(artifact_path):
                # Artifact exists but the ledger doesn't know it (e.g. the ledger was deleted): self-heal.
                try:
                    record(pano_id, 'saved')
                except OSError as e:
                    # Deliberately not a failure (the artifact is safe; next run self-heals again), but it must
                    # feed last_error: a full store failing every self-heal write used to end the phase with
                    # zero stdout, indistinguishable from a healthy fully-backfilled city.
                    last_error = e
                    logging.error("DEPTHDOWNLOAD: Could not ledger existing artifact for pano %s: %s", pano_id,
                                  str(e))
                skipped_count += 1
                continue

            if max_runtime_minutes is not None and run_start_monotonic is not None:
                elapsed_minutes = (time.monotonic() - run_start_monotonic) / 60.0
                if elapsed_minutes >= max_runtime_minutes:
                    stop_reason = DEPTH_STOP_MAX_RUNTIME
                    print("DEPTHDOWNLOAD: Max runtime of %.1f minutes reached (%.1f elapsed). Stopping."
                          % (max_runtime_minutes, elapsed_minutes))
                    break
            if max_requests is not None and request_count >= max_requests:
                stop_reason = DEPTH_STOP_MAX_REQUESTS
                print("DEPTHDOWNLOAD: Max depth requests (%d) reached. Stopping." % (max_requests))
                break

            _pace(last_request_at)
            last_request_at = time.monotonic()

            print("DEPTHDOWNLOAD: Processing pano %s " % (pano_id))
            request_count += 1
            try:
                pano, planes = _fetch_pano_with_depth_planes(pano_id, session)
                if pano is None or pano.depth is None or pano.depth.data is None \
                        or np.ndim(pano.depth.data) != 2:
                    # Pano deleted/id rotated, no depth payload, or a payload that isn't the (h, w) grid
                    # _write_depth_artifact's [:, ::-1] needs - a property of the pano, not of the network, so
                    # it must not fall through to the write and be miscounted as transient. Depth availability
                    # for a given pano id is static, so remember the outcome and never re-request.
                    record(pano_id, 'unavailable')
                    unavailable_count += 1
                    fail_count += 1
                elif planes is None:
                    # A depth raster with no plane data can only mean the payload path or wire format drifted
                    # upstream (the contract tests exist to catch that first). Depth exists, so 'unavailable'
                    # would be a lie. DepthPayloadError is a RuntimeError, so it lands in the 'unexpected'
                    # arm below - transient, not ledgered, retried next run. The remaining malformed-v3
                    # cases (shape mismatch, indices disagreeing with the raster) raise the same type from
                    # _write_depth_artifact, which is where the comparison the checks need is computed.
                    raise DepthPayloadError("depth payload present but no plane data for pano %s" % (pano_id,))
                else:
                    _write_depth_artifact(storage_path, pano_id, pano, planes)
                    record(pano_id, 'saved')
                    success_count += 1
                # Either outcome proves we're still talking to Google, so the breaker resets.
                consecutive_failures = 0
                streak_classes.clear()
            except (DepthBlockedError, requests.exceptions.RetryError) as e:
                # Google is refusing us: an interstitial, or a 429/5xx that survived every retry. That's a verdict
                # on the endpoint, not on this pano, so stop rather than spend the rest of the budget on a wall.
                fail_count += 1
                stop_reason = DEPTH_STOP_BLOCKED
                last_error = e
                logging.error("DEPTHDOWNLOAD: Stopping depth phase, Google is refusing requests (%s)", str(e))
                print("DEPTHDOWNLOAD: Google is refusing requests (%s). Stopping the depth phase." % (e))
                break
            except (requests.RequestException, ValueError) as e:
                # Transient: connection errors/timeouts, or a non-JSON page that isn't a recognised interstitial -
                # streetlevel never checks status codes, so non-200 responses surface as JSONDecodeError. Not
                # ledgered, so the pano retries next run.
                # NB: requests.RequestException subclasses OSError, so it must be caught above the OSError arm.
                fail_count += 1
                consecutive_failures += 1
                failure_class = 'network'
                streak_classes[failure_class] += 1
                last_error = e
                logging.error("DEPTHDOWNLOAD: Failed to fetch depth for pano %s due to error %s", pano_id, str(e))
            except OSError as e:
                # Storage-side failure writing the artifact or the ledger - ENOSPC/EIO on the sshfs mount is the
                # realistic one, given this feature adds terabytes. Also transient and also not ledgered. Caught
                # deliberately: escaping would fail the whole run and forfeit the rest of the phase's budget over
                # one pano's storage hiccup. (log.csv itself is safe either way - DownloadRunner now pads the row
                # to 18 fields in a finally.)
                fail_count += 1
                consecutive_failures += 1
                failure_class = 'storage'
                streak_classes[failure_class] += 1
                last_error = e
                logging.error("DEPTHDOWNLOAD: Could not store depth for pano %s: %s", pano_id, str(e))
            except Exception as e:
                # Unexpected (e.g. a malformed depth payload crashing streetlevel's parser). Treated as
                # transient; worst case a permanently-bad pano costs one request per run.
                fail_count += 1
                consecutive_failures += 1
                failure_class = 'unexpected'
                streak_classes[failure_class] += 1
                last_error = e
                logging.exception("DEPTHDOWNLOAD: Unexpected error fetching depth for pano %s: %s", pano_id, str(e))

            total_completed = success_count + fail_count + skipped_count
            print("DEPTHDOWNLOAD: Completed %d of %d (%d success, %d failed [%d unavailable], %d skipped)"
                  % (total_completed, total_panos, success_count, fail_count, unavailable_count, skipped_count))

            if consecutive_failures >= DEPTH_MAX_CONSECUTIVE_FAILURES:
                stop_reason = DEPTH_STOP_CONSECUTIVE_FAILURES
                logging.error("DEPTHDOWNLOAD: Stopping depth phase after %d consecutive failures",
                              consecutive_failures)
                print("DEPTHDOWNLOAD: %d consecutive failures. Stopping the depth phase."
                      % (consecutive_failures))
                break
            # The retreat exists to give a network blip or rate limit time to clear. A full or unmounted store
            # cannot clear itself, so storage failures skip the wait (they still count toward the breaker, which
            # then trips fast) instead of burning up to 7.5 minutes of a shared --max-runtime window.
            retreat_seconds = None if failure_class == 'storage' else DEPTH_RETREAT_SCHEDULE.get(consecutive_failures)
            if retreat_seconds:
                print("DEPTHDOWNLOAD: %d consecutive failures, backing off for %ds before continuing."
                      % (consecutive_failures, retreat_seconds))
                time.sleep(retreat_seconds)

    total_completed = success_count + fail_count + skipped_count
    # Loud on stdout because cron mails it: a phase that stopped early means nothing is progressing, and the
    # per-pano detail is buried in scrape.log.
    if stop_reason == DEPTH_STOP_BLOCKED:
        # Actionable sentence first - the error detail ends in Google's redirect URL, which can run 600+
        # characters, so it rides at the tail and is truncated.
        print("DEPTHDOWNLOAD: WARNING - the depth phase stopped early because Google stopped answering. No panos "
              "were lost (unresolved panos are retried next run), but check for a rate limit before the next "
              "run. Last error: %s" % (str(last_error)[:200],))
    elif stop_reason == DEPTH_STOP_CONSECUTIVE_FAILURES:
        # The breaker counts storage failures (ENOSPC/EIO on the sshfs mount) as well as network ones, so don't
        # attribute the trip to Google: break the streak down by class so the dominant cause stays visible even
        # when the last error is the minority class.
        breakdown = ', '.join('%d %s' % (count, cls) for cls, count in streak_classes.most_common())
        print("DEPTHDOWNLOAD: WARNING - the depth phase stopped early after %d consecutive failures (%s). Last "
              "error: %s. No panos were lost (unresolved panos are retried next run); check whether the cause "
              "is the store (full/unmounted) or the network before the next run."
              % (consecutive_failures, breakdown, last_error))
    elif last_error is not None:
        # No breaker tripped, but something did fail: a budget may have stopped the run first (a shared
        # --max-runtime window is often minutes, far fewer than the breaker needs to see a streak), or the
        # failures were scattered, or a self-heal ledger write failed - which counts nowhere else. Without this
        # arm a full store can read as a healthy, fully-backfilled city.
        print("DEPTHDOWNLOAD: WARNING - the depth phase hit errors (%d success, %d failed [%d unavailable], "
              "%d skipped of %d). No panos were lost (unresolved panos are retried next run). Last error: %s"
              % (success_count, fail_count, unavailable_count, skipped_count, total_panos,
                 str(last_error)[:200]))
    logging.debug("DEPTHDOWNLOAD: Final result: Completed %d of %d (%d success, %d failed [%d unavailable], "
                  "%d skipped, %d requests, stop_reason=%s)", total_completed, total_panos, success_count,
                  fail_count, unavailable_count, skipped_count, request_count, stop_reason)
    return success_count, fail_count, skipped_count, total_completed
