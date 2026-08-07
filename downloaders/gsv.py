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

# Consecutive transient failures after which the depth phase gives up for this run. Without a breaker, a run that
# hits a wall (rate limit, captcha, DNS outage) spends its whole --max-runtime budget re-hitting it, then does the
# same thing again the next night.
DEPTH_MAX_CONSECUTIVE_FAILURES = 25

# consecutive-failure count -> seconds to sleep before carrying on. Gives a blip a chance to clear before we spend
# the breaker, without pounding while we wait.
DEPTH_RETREAT_SCHEDULE = {5: 30, 10: 120, 15: 300}

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


def _write_depth_artifact(storage_path, pano_id, pano):
    """Atomically write <pano_id[:2]>/<pano_id>.depth.npz for a streetlevel pano with depth data.

    Contents: 'depth' = float32 (height, width) array of meters with -1 meaning sky/infinitely far, plus
    'heading'/'pitch'/'roll' scalars in radians (NaN if absent) so the artifact is self-contained for
    pixel<->world alignment.
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
            np.savez_compressed(f, depth=pano.depth.data.astype(np.float32), heading=scalar(pano.heading),
                                pitch=scalar(pano.pitch), roll=scalar(pano.roll))
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
    blocked = False

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
        print("DEPTHDOWNLOAD: Cannot write the depth ledger (%s). Skipping the depth phase." % (e))
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
                    logging.error("DEPTHDOWNLOAD: Could not ledger existing artifact for pano %s: %s", pano_id,
                                  str(e))
                skipped_count += 1
                continue

            if max_runtime_minutes is not None and run_start_monotonic is not None:
                elapsed_minutes = (time.monotonic() - run_start_monotonic) / 60.0
                if elapsed_minutes >= max_runtime_minutes:
                    print("DEPTHDOWNLOAD: Max runtime of %.1f minutes reached (%.1f elapsed). Stopping."
                          % (max_runtime_minutes, elapsed_minutes))
                    break
            if max_requests is not None and request_count >= max_requests:
                print("DEPTHDOWNLOAD: Max depth requests (%d) reached. Stopping." % (max_requests))
                break

            _pace(last_request_at)
            last_request_at = time.monotonic()

            print("DEPTHDOWNLOAD: Processing pano %s " % (pano_id))
            request_count += 1
            try:
                pano = streetview.find_panorama_by_id(pano_id, download_depth=True, session=session)
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
                # Either outcome proves we're still talking to Google, so the breaker resets.
                consecutive_failures = 0
            except (DepthBlockedError, requests.exceptions.RetryError) as e:
                # Google is refusing us: an interstitial, or a 429/5xx that survived every retry. That's a verdict
                # on the endpoint, not on this pano, so stop rather than spend the rest of the budget on a wall.
                fail_count += 1
                blocked = True
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
                logging.error("DEPTHDOWNLOAD: Failed to fetch depth for pano %s due to error %s", pano_id, str(e))
            except OSError as e:
                # Storage-side failure writing the artifact or the ledger - ENOSPC/EIO on the sshfs mount is the
                # realistic one, given this feature adds terabytes. Also transient and also not ledgered. Caught
                # deliberately: escaping here would abort the run part-way through log.csv and leave a 12-field
                # line where the analyzer expects 18.
                fail_count += 1
                consecutive_failures += 1
                logging.error("DEPTHDOWNLOAD: Could not store depth for pano %s: %s", pano_id, str(e))
            except Exception as e:
                # Unexpected (e.g. a malformed depth payload crashing streetlevel's parser). Treated as
                # transient; worst case a permanently-bad pano costs one request per run.
                fail_count += 1
                consecutive_failures += 1
                logging.exception("DEPTHDOWNLOAD: Unexpected error fetching depth for pano %s: %s", pano_id, str(e))

            total_completed = success_count + fail_count + skipped_count
            print("DEPTHDOWNLOAD: Completed %d of %d (%d success, %d failed [%d unavailable], %d skipped)"
                  % (total_completed, total_panos, success_count, fail_count, unavailable_count, skipped_count))

            if consecutive_failures >= DEPTH_MAX_CONSECUTIVE_FAILURES:
                blocked = True
                logging.error("DEPTHDOWNLOAD: Stopping depth phase after %d consecutive failures",
                              consecutive_failures)
                print("DEPTHDOWNLOAD: %d consecutive failures. Stopping the depth phase."
                      % (consecutive_failures))
                break
            retreat_seconds = DEPTH_RETREAT_SCHEDULE.get(consecutive_failures)
            if retreat_seconds:
                print("DEPTHDOWNLOAD: %d consecutive failures, backing off for %ds before continuing."
                      % (consecutive_failures, retreat_seconds))
                time.sleep(retreat_seconds)

    total_completed = success_count + fail_count + skipped_count
    if blocked:
        # Loud on stdout because cron mails it: a blocked phase means nothing is progressing, and the per-pano
        # detail is buried in scrape.log.
        print("DEPTHDOWNLOAD: WARNING - the depth phase stopped early because Google stopped answering. No panos "
              "were lost (unresolved panos are retried next run), but check for a rate limit before the next run.")
    logging.debug("DEPTHDOWNLOAD: Final result: Completed %d of %d (%d success, %d failed [%d unavailable], "
                  "%d skipped, %d requests, blocked=%s)", total_completed, total_panos, success_count, fail_count,
                  unavailable_count, skipped_count, request_count, blocked)
    return success_count, fail_count, skipped_count, total_completed
