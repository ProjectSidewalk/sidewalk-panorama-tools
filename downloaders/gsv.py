# Google Street View panorama downloader.
#
# Stitches tiles from Google's undocumented CBK endpoint into a single equirectangular JPEG, and fetches depth
# maps from Google's photometa endpoint via the streetlevel library (see download_depth_maps).
#
# Do not add viewer parameters to the CBK URL without checking what they do to the tile bodies: `fover`, copied
# from the Street View viewer, made CBK serve the polar rows of zoom 5 at half size (#73). See _CBK_BASE_URL.

import asyncio
import collections
import csv
import logging
import math
import os
import random
import stat
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

try:
    from xml.etree import cElementTree as ET
except ImportError:
    from xml.etree import ElementTree as ET

from .common import DownloadResult


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


def _request_session():
    session = requests.Session()
    retry = Retry(total=5, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retry)
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
    if width is None or width <= 0:
        # math.log2 would raise a bare "math domain error" that says nothing about which pano died.
        raise ValueError('pano width must be positive to infer a zoom level, got %r' % (width,))
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


def _save_pano_image(image, out_image_name):
    """Atomically write the stitched JPEG: .part + os.replace, like _write_depth_artifact.

    The skip check in download_single_pano treats ANY existing file as done, so a direct save that crashed
    mid-write used to leave a truncated JPEG that was never re-attempted.
    """
    tmp_path = out_image_name + '.part'
    try:
        image.save(tmp_path, 'jpeg')
        os.chmod(tmp_path, 0o664)
        os.replace(tmp_path, out_image_name)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def download_single_pano(storage_path, pano_info):
    pano_id = pano_info['pano_id']
    pano_dims = (pano_info.get('width'), pano_info.get('height'))

    base_url = _CBK_BASE_URL

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

    # Skip download if image already exists.
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    final_image_width = int(pano_dims[0]) if pano_dims[0] is not None else None
    final_image_height = int(pano_dims[1]) if pano_dims[1] is not None else None
    zoom = None

    # Session scoped to the zoom/dimension probes below; the tile fan-out uses its own aiohttp session. This
    # runs once per pano, so leaving it unclosed would pile up connection pools until GC (#51).
    with _request_session() as session:
        # Check XML metadata for image width/height max zoom if its downloaded.
        xml_metadata_path = os.path.join(destination_dir, pano_id + ".xml")
        if os.path.isfile(xml_metadata_path):
            print(xml_metadata_path)
            with open(xml_metadata_path, 'rb') as pano_xml:
                try:
                    tree = ET.parse(pano_xml)
                    root = tree.getroot()

                    # Get the number of zoom levels.
                    for child in root:
                        if child.tag == 'data_properties':
                            zoom = int(child.attrib['num_zoom_levels'])
                            if final_image_width is None:
                                final_image_width = int(child.attrib['width'])
                            if final_image_height is None:
                                final_image_height = int(child.attrib['height'])

                    # If there is no zoom in the XML, then we skip this and try some zoom levels below.
                    if zoom is not None:
                        # Check if the image exists (occasionally we will have XML but no JPG).
                        test_url = f'{base_url}&zoom={zoom}&x=0&y=0&panoid={pano_id}'
                        test_request = _get_response(test_url, session, stream=True)
                        test_tile = Image.open(test_request)
                        if test_tile.convert("L").getextrema() == (0, 0):
                            return DownloadResult.failure
                except Exception:
                    pass

        # If we did not find image width/height from API or XML, then set download to failure.
        if final_image_width is None or final_image_height is None:
            return DownloadResult.failure

        # If we did not find a zoom level in the XML above, then try a couple zoom level options here.
        if zoom is None:
            url_zoom_3 = f'{base_url}&zoom=3&x=0&y=0&panoid={pano_id}'
            url_zoom_5 = f'{base_url}&zoom=5&x=0&y=0&panoid={pano_id}'

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
                return DownloadResult.failure

    final_im_dimension = (final_image_width, final_image_height)

    tiles = _generate_tile_urls(pano_id, final_image_width, final_image_height, zoom)
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

    image = _stitch_tiles(ok, _dims_at_zoom(final_image_width, final_image_height, zoom), final_im_dimension)
    _reject_mostly_black_stitch(image, pano_id, zoom)
    _save_pano_image(image, out_image_name)
    return DownloadResult.success


DEPTH_LOG_FILENAME = 'depth_log.csv'
DEPTH_ARTIFACT_SUFFIX = '.depth.npz'

# Stamped into every artifact so consumers can tell formats apart. Artifacts with no format_version field
# predate v2 and store streetlevel's raw column order, which is x-mirrored relative to the pano JPEG (#58).
DEPTH_ARTIFACT_FORMAT_VERSION = 2

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


class _TimeoutHTTPAdapter(HTTPAdapter):
    """HTTPAdapter that applies a default timeout to every request.

    streetlevel's internal requests carry no timeout, so without this a single hung connection would stall a
    nightly cron run indefinitely.
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


def _depth_session():
    """Build the requests.Session handed to streetlevel for photometa requests.

    Same retry policy as _request_session(), plus a default timeout (streetlevel never sets one), backoff jitter,
    and the block-detection hook.

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


def _write_depth_artifact(storage_path, pano_id, pano):
    """Atomically write <pano_id[:2]>/<pano_id>.depth.npz for a streetlevel pano with depth data.

    Contents: 'depth' = float32 (height, width) array of meters with -1 meaning no plane (sky, or anything
    Google didn't model), 'heading'/'pitch'/'roll' scalars in radians (NaN if absent) so the artifact is
    self-contained for pixel<->world alignment, and 'format_version' (see DEPTH_ARTIFACT_FORMAT_VERSION).

    The stored array shares the pano JPEG's column order: streetlevel's decoder x-mirrors the payload
    (compute_depth_map writes the value for payload column x to output column w-1-x), so pano.depth.data is
    horizontally flipped relative to the imagery and is flipped back here on write (#58). A consumer can
    therefore index it with a stored pano_x/pano_y scaled by width/height, no mirror correction needed.
    tests/test_streetlevel_api.py pins the decode's end-to-end column order - the ray-direction formula and
    the write index jointly, either of which flipping alone would change the orientation - so a streetlevel
    change fails CI rather than silently re-mirroring new artifacts.
    """
    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        # exist_ok: concurrent city runs (and the image phase) race on shard dirs.
        os.makedirs(destination_dir, exist_ok=True)
        try:
            os.chmod(destination_dir, 0o775 | stat.S_ISGID)
        except PermissionError:
            pass  # lost the race to another user's process; their dir, their modes — must not fail the pano

    final_path = os.path.join(destination_dir, pano_id + DEPTH_ARTIFACT_SUFFIX)
    tmp_path = final_path + '.part'

    def scalar(value):
        return float(value) if value is not None else float('nan')

    try:
        # savez_compressed needs an open file object: given a path without a .npz extension it silently appends
        # one, which would write to the wrong filename.
        with open(tmp_path, 'wb') as f:
            np.savez_compressed(f, depth=pano.depth.data[:, ::-1].astype(np.float32),
                                heading=scalar(pano.heading), pitch=scalar(pano.pitch),
                                roll=scalar(pano.roll), format_version=DEPTH_ARTIFACT_FORMAT_VERSION)
        os.chmod(tmp_path, 0o664)
        # Atomic rename so a crash can never leave a truncated .npz that would be treated as done forever.
        os.replace(tmp_path, final_path)
    except BaseException:
        # A half-written .part would otherwise accumulate on the store forever - nothing else ever cleans it up,
        # and the retry next run writes to the same name.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


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
        from streetlevel import streetview
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
                pano = streetview.find_panorama_by_id(pano_id, download_depth=True, session=session)
                if pano is None or pano.depth is None or pano.depth.data is None \
                        or np.ndim(pano.depth.data) != 2:
                    # Pano deleted/id rotated, no depth payload, or a payload that isn't the (h, w) grid
                    # _write_depth_artifact's [:, ::-1] needs - a property of the pano, not of the network, so
                    # it must not fall through to the write and be miscounted as transient. Depth availability
                    # for a given pano id is static, so remember the outcome and never re-request.
                    record(pano_id, 'unavailable')
                    unavailable_count += 1
                    fail_count += 1
                else:
                    _write_depth_artifact(storage_path, pano_id, pano)
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
