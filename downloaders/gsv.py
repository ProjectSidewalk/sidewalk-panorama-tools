# Google Street View panorama downloader.
#
# Stitches 512x512 tiles from Google's undocumented CBK endpoint into a single equirectangular JPEG, and fetches
# depth maps from Google's photometa endpoint via the streetlevel library (see download_depth_maps).

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
from aiohttp import web  # noqa: F401  (imported for aiohttp.web.HTTPServerError)
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


def download_single_pano(storage_path, pano_info):
    pano_id = pano_info['pano_id']
    pano_dims = (pano_info.get('width'), pano_info.get('height'))

    base_url = 'https://maps.google.com/cbk?output=tile&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4'

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

    def generate_gsv_urls(zoom):
        sites_gsv = []
        for y in range(int(math.ceil(final_image_height / 512.0))):
            for x in range(int(math.ceil(final_image_width / 512.0))):
                url = f'{base_url}&zoom={zoom}&x={str(x)}&y={str(y)}&panoid={pano_id}'
                sites_gsv.append((str(x) + " " + str(y), url))
        return sites_gsv

    @backoff.on_exception(backoff.expo, (aiohttp.web.HTTPServerError, aiohttp.ClientError, aiohttp.ClientResponseError,
                                         aiohttp.ServerConnectionError, aiohttp.ServerDisconnectedError,
                                         aiohttp.ClientHttpProxyError), max_tries=10)
    async def download_single_gsv(session, url):
        async with session.get(url[1], proxy=_proxies.get("http"), headers=_random_header()) as response:
            head_content = response.headers['Content-Type']
            # Ensures content type is an image.
            if head_content[0:10] != "image/jpeg":
                raise aiohttp.ClientResponseError(response.request_info, response.history)
            image = await response.content.read()
            return [url[0], image]

    @backoff.on_exception(backoff.expo,
                          (aiohttp.web.HTTPServerError, aiohttp.ClientError, aiohttp.ClientResponseError, aiohttp.ServerConnectionError,
                           aiohttp.ServerDisconnectedError, aiohttp.ClientHttpProxyError), max_tries=10)
    async def download_all_gsv_images(sites):
        conn = aiohttp.TCPConnector(limit=thread_count)
        async with aiohttp.ClientSession(raise_for_status=True, connector=conn) as session:
            tasks = []
            for url in sites:
                task = asyncio.ensure_future(download_single_gsv(session, url))
                tasks.append(task)
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            return responses

    blank_image = Image.new('RGB', final_im_dimension, (0, 0, 0, 0))
    sites = generate_gsv_urls(zoom)
    all_pano_images = asyncio.run(download_all_gsv_images(sites))

    for cell_image in all_pano_images:
        img = Image.open(BytesIO(cell_image[1]))
        img = img.resize((512, 512))
        x, y = int(str.split(cell_image[0])[0]), int(str.split(cell_image[0])[1])
        blank_image.paste(img, (512 * x, 512 * y))

    if zoom == 3:
        blank_image = blank_image.resize(final_im_dimension, Image.LANCZOS)
    blank_image.save(out_image_name, 'jpeg')
    os.chmod(out_image_name, 0o664)
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
    test_streetlevel_still_misreads_the_depth_offset). The misread parses correctly only when the first index
    byte is 0, which in practice it always is: payload row 0 is the zenith (theta ~ pi in the decode's ray
    formula) and sky carries index 0. Where it is not - a pano under a tunnel, an overpass soffit, or a
    parking structure, where Google models the surface overhead - streetlevel's own parser raises before this
    function is ever reached, so the two decodes cannot silently disagree; that pano simply never resolves.

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
    indices_end = offset + width * height
    planes_end = indices_end + number_of_planes * 16
    if len(raw) < planes_end:
        raise DepthPayloadError("depth payload truncated: %d bytes, need %d" % (len(raw), planes_end))
    indices = np.frombuffer(raw, dtype=np.uint8, count=width * height, offset=offset)
    planes = np.frombuffer(raw, dtype='<f4', count=number_of_planes * 4, offset=indices_end)
    planes = planes.reshape(number_of_planes, 4)
    return DepthPlanes(indices.reshape(height, width).copy(), planes[:, :3].astype(np.float32),
                       planes[:, 3].astype(np.float32))


def _fetch_pano_with_depth_planes(pano_id, session):
    """One photometa request -> (StreetViewPanorama | None, DepthPlanes | None).

    streetlevel's find_panorama_by_id is api.find_panorama_by_id + parse.parse_panorama_id_response, but the
    parse half discards Google's plane list while computing the per-pixel raster (#56). Calling the two
    halves ourselves keeps the one-request-per-pano budget and hands us the raw payload to decode planes
    from. Both entry points and the msg path are pinned by tests/test_streetlevel_api.py; the session (and
    with it the timeout adapter, retry policy, and block-detection hook) is passed through exactly as the
    high-level call would.
    """
    # Imported lazily: download_depth_maps' availability probe has already run, and tests reach this seam
    # through an adapter, so the real submodules only load when a real request is about to happen.
    from streetlevel.streetview import api, parse

    response = api.find_panorama_by_id(pano_id, download_depth=True, locale='en', session=session)
    pano = parse.parse_panorama_id_response(response)
    if pano is None:
        return None, None
    try:
        payload = response[1][0][5][0][5][1][2]
    except (IndexError, KeyError, TypeError):
        payload = None
    return pano, (_decode_depth_planes(payload) if payload else None)


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
    # The one invariant the README promises consumers - index 0 sits exactly where the raster says -1 -
    # enforced instead of assumed. It costs one pass over ~130k pixels and is the only cross-check that the
    # two decodes of the same payload (streetlevel's, for the raster; ours, for the planes) still agree on
    # column order and on which pixels have no plane. This backfill is one-shot and cannot be redone offline,
    # so drift upstream must stop the pano, not quietly produce millions of artifacts whose documented
    # reconstruction identity does not hold.
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
    tmp_path = final_path + '.part'

    def scalar(value):
        return float(value) if value is not None else float('nan')

    try:
        # savez_compressed needs an open file object: given a path without a .npz extension it silently appends
        # one, which would write to the wrong filename.
        with open(tmp_path, 'wb') as f:
            np.savez_compressed(f, depth=stored_depth,
                                plane_indices=np.asarray(planes.indices, dtype=np.uint8),
                                planes_n=np.asarray(planes.normals, dtype=np.float32).reshape(-1, 3),
                                planes_d=np.asarray(planes.distances, dtype=np.float32).reshape(-1),
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


def ground_plane_from_artifact(artifact, min_vertical=0.7):
    """Pick the ground plane out of a v3 depth artifact: the near-horizontal plane that most of the pano's
    downward-looking pixels actually land on.

    Deliberately a helper rather than a field baked into the artifact: the artifact stores Google's plane
    list verbatim, so this heuristic (which plane is "ground" on a tilted street, a bridge, a plaza?) stays
    fixable in code instead of frozen into millions of .npz files. Sign-insensitive throughout - the up/down
    sign convention of Google's pano-local frame is not relied on.

    Candidates are drawn only from the bottom half of the raster, and are ranked by how many of those pixels
    reference them (ties broken by verticality, then by lowest index for determinism). Both rules matter.
    Ranking on verticality alone lets a handful of pixels of some *overhead* surface - an overpass soffit, a
    tunnel ceiling, an awning, a sign gantry, all of which are more perfectly horizontal than a real cambered
    road - outrank the tens of thousands of pixels of actual road, and camera_height_from_artifact then
    silently returns the height of the ceiling. Restricting to the bottom half is not a fudge factor: rows
    from h//2 on are exactly those whose rays satisfy theta < pi/2, i.e. that point below the horizon, under
    the same ray formula the stored frame is defined by (see _write_depth_artifact).

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
    support = np.bincount(indices[indices.shape[0] // 2:].ravel(), minlength=len(normals))
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
