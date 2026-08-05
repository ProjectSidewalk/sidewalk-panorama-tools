# Google Street View panorama downloader.
#
# Stitches 512x512 tiles from Google's undocumented CBK endpoint into a single equirectangular JPEG, and fetches
# depth maps from Google's photometa endpoint via the streetlevel library (see download_depth_maps).

import asyncio
import csv
import logging
import math
import os
import random
import stat
from datetime import datetime
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
    from xml.etree import cElementTree as ET
except ImportError:
    from xml.etree import ElementTree as ET

from .common import DownloadResult

# Normalize proxy config: treat the sentinel placeholders in config.py as unset.
_proxies = dict(proxies)
if _proxies.get('http') == 'http://' or _proxies.get('https') == 'https://':
    _proxies['http'] = None
    _proxies['https'] = None


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
    response = session.get(url, headers=_random_header(), proxies=_proxies, stream=stream)
    if not stream:
        return response
    return response.raw


def download_single_pano(storage_path, pano_info):
    pano_id = pano_info['pano_id']
    pano_dims = (pano_info.get('width'), pano_info.get('height'))

    base_url = 'https://maps.google.com/cbk?output=tile&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4'

    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        os.makedirs(destination_dir)
        os.chmod(destination_dir, 0o775 | stat.S_ISGID)

    filename = pano_id + ".jpg"
    out_image_name = os.path.join(destination_dir, filename)

    # Skip download if image already exists.
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    final_image_width = int(pano_dims[0]) if pano_dims[0] is not None else None
    final_image_height = int(pano_dims[1]) if pano_dims[1] is not None else None
    zoom = None

    session = _request_session()

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

        # In some cases (e.g., old GSV images), we don't have zoom level 5, so Google returns a transparent image. This
        # means we need to set the zoom level to 3. Google also returns a transparent image if there is no imagery.
        # So check at both zoom levels. How to check:
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
        async with session.get(url[1], proxy=_proxies["http"], headers=_random_header()) as response:
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

    Same retry policy as _request_session(), plus a default timeout (streetlevel never sets one). Headers and
    proxies must live on the session because streetlevel doesn't pass per-request options.
    """
    session = requests.Session()
    retry = Retry(total=5, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
    adapter = _TimeoutHTTPAdapter(max_retries=retry, timeout=30)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    # Drop Host (one config.py entry pins it to maps.google.com, but photometa lives on www.google.com) and the
    # HTML-oriented Accept from the borrowed browser headers.
    session.headers.update({k: v for k, v in _random_header().items() if k not in ('Host', 'Accept')})
    session.proxies.update({k: v for k, v in _proxies.items() if v})
    return session


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


def _write_depth_artifact(storage_path, pano_id, pano):
    """Atomically write <pano_id[:2]>/<pano_id>.depth.npz for a streetlevel pano with depth data.

    Contents: 'depth' = float32 (height, width) array of meters with -1 meaning sky/infinitely far, plus
    'heading'/'pitch'/'roll' scalars in radians (NaN if absent) so the artifact is self-contained for
    pixel<->world alignment.
    """
    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        os.makedirs(destination_dir)
        os.chmod(destination_dir, 0o775 | stat.S_ISGID)

    final_path = os.path.join(destination_dir, pano_id + DEPTH_ARTIFACT_SUFFIX)
    tmp_path = final_path + '.part'

    def scalar(value):
        return float(value) if value is not None else float('nan')

    # savez_compressed needs an open file object: given a path without a .npz extension it silently appends one,
    # which would write to the wrong filename.
    with open(tmp_path, 'wb') as f:
        np.savez_compressed(f, depth=pano.depth.data.astype(np.float32), heading=scalar(pano.heading),
                            pitch=scalar(pano.pitch), roll=scalar(pano.roll))
    os.chmod(tmp_path, 0o664)
    # Atomic rename so a crash can never leave a truncated .npz that would be treated as done forever.
    os.replace(tmp_path, final_path)


def download_depth_maps(storage_path, pano_infos, run_start_time=None, max_runtime_minutes=None, max_requests=None):
    """Fetch GSV depth maps via the streetlevel library for every pano in pano_infos.

    Callers pre-filter to source == 'gsv'. Depth rides Google's photometa response, so this costs one metadata
    request per unresolved pano. Outcomes are remembered in <storage_path>/depth_log.csv: 'saved' (artifact
    written) and 'unavailable' (pano gone or no depth payload — permanent for a given pano id, never retried).
    Transient errors are counted as failures but NOT ledgered, so they retry on the next run. The artifact on
    disk is the ground truth; deleting the ledger just makes the next run re-stat artifacts (re-appending
    'saved') and re-request unresolved panos.

    @param storage_path        Root of the pano store (shard dirs + depth_log.csv live here).
    @param pano_infos          Pano dicts (needs 'pano_id'); typically the full /adminapi/panos list, which is
                               what backfills panos downloaded before this feature existed.
    @param run_start_time      Shared run start used for the max_runtime_minutes budget.
    @param max_runtime_minutes Stop starting new requests once this much wall time has elapsed since run start.
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

    depth_log_path = os.path.join(storage_path, DEPTH_LOG_FILENAME)
    resolved_ids = _load_depth_log(depth_log_path)
    log_existed = os.path.isfile(depth_log_path)

    session = _depth_session()
    with open(depth_log_path, 'a', newline='') as depth_log:
        ledger = csv.writer(depth_log)
        if not log_existed:
            ledger.writerow(['pano_id', 'status'])
            os.chmod(depth_log_path, 0o664)

        def record(pano_id, status):
            ledger.writerow([pano_id, status])
            depth_log.flush()
            resolved_ids.add(pano_id)

        for pano_info in pano_infos:
            pano_id = pano_info['pano_id']
            if pano_id in resolved_ids:
                skipped_count += 1
                continue

            artifact_path = os.path.join(storage_path, pano_id[:2], pano_id + DEPTH_ARTIFACT_SUFFIX)
            if os.path.isfile(artifact_path):
                # Artifact exists but the ledger doesn't know it (e.g. the ledger was deleted): self-heal.
                record(pano_id, 'saved')
                skipped_count += 1
                continue

            if max_runtime_minutes is not None and run_start_time is not None:
                elapsed_minutes = (datetime.now() - run_start_time).total_seconds() / 60.0
                if elapsed_minutes >= max_runtime_minutes:
                    print("DEPTHDOWNLOAD: Max runtime of %.1f minutes reached (%.1f elapsed). Stopping."
                          % (max_runtime_minutes, elapsed_minutes))
                    break
            if max_requests is not None and request_count >= max_requests:
                print("DEPTHDOWNLOAD: Max depth requests (%d) reached. Stopping." % (max_requests))
                break

            print("DEPTHDOWNLOAD: Processing pano %s " % (pano_id))
            request_count += 1
            try:
                pano = streetview.find_panorama_by_id(pano_id, download_depth=True, session=session)
            except (requests.RequestException, ValueError) as e:
                # Transient: connection errors/timeouts, retries exhausted, or a non-JSON (error/captcha) page —
                # streetlevel never checks status codes, so non-200 responses surface as JSONDecodeError. Not
                # ledgered, so the pano retries next run.
                fail_count += 1
                logging.error("DEPTHDOWNLOAD: Failed to fetch depth for pano %s due to error %s", pano_id, str(e))
            except Exception as e:
                # Unexpected (e.g. a malformed depth payload crashing streetlevel's parser). Treated as
                # transient; worst case a permanently-bad pano costs one request per run.
                fail_count += 1
                logging.exception("DEPTHDOWNLOAD: Unexpected error fetching depth for pano %s: %s", pano_id, str(e))
            else:
                if pano is None or pano.depth is None or pano.depth.data is None:
                    # Pano deleted/id rotated, or it has no depth payload. Depth availability for a given pano id
                    # is static, so remember the outcome and never re-request.
                    record(pano_id, 'unavailable')
                    unavailable_count += 1
                    fail_count += 1
                else:
                    _write_depth_artifact(storage_path, pano_id, pano)
                    record(pano_id, 'saved')
                    success_count += 1

            total_completed = success_count + fail_count + skipped_count
            print("DEPTHDOWNLOAD: Completed %d of %d (%d success, %d failed [%d unavailable], %d skipped)"
                  % (total_completed, total_panos, success_count, fail_count, unavailable_count, skipped_count))

    total_completed = success_count + fail_count + skipped_count
    logging.debug("DEPTHDOWNLOAD: Final result: Completed %d of %d (%d success, %d failed [%d unavailable], "
                  "%d skipped)", total_completed, total_panos, success_count, fail_count, unavailable_count,
                  skipped_count)
    return success_count, fail_count, skipped_count, total_completed
