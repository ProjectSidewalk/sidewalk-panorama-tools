# Mapillary panorama downloader.
#
# Uses the Graph API v4 to fetch a short-lived signed URL for the original-resolution equirectangular image, then
# downloads it in one request. Requires MAPILLARY_ACCESS_TOKEN to be set in the environment. Client tokens can be
# created at https://www.mapillary.com/dashboard/developers.

import logging
import os
import stat

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import DownloadResult, atomic_output_path

GRAPH_API_BASE = 'https://graph.mapillary.com'
TOKEN_ENV_VAR = 'MAPILLARY_ACCESS_TOKEN'


def is_token_set():
    return bool(os.environ.get(TOKEN_ENV_VAR))


def _session():
    session = requests.Session()
    retry = Retry(total=5, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def download_single_pano(storage_path, pano_info):
    pano_id = pano_info['pano_id']

    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        # exist_ok: concurrent runs race on shard dirs.
        os.makedirs(destination_dir, exist_ok=True)
        try:
            os.chmod(destination_dir, 0o775 | stat.S_ISGID)
        except PermissionError:
            pass  # lost the race to another user's process; their dir, their modes — must not fail the pano

    out_image_name = os.path.join(destination_dir, pano_id + ".jpg")
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        # A property of the RUN, not of this pano, so it raises rather than returning failure (#41):
        # filter_supported_sources drops Mapillary panos when the token is unset so this shouldn't be
        # reached, but if it ever is, ledgering would blacklist the city's whole corpus over a missing
        # environment variable.
        raise RuntimeError("%s is not set; cannot download Mapillary pano %s" % (TOKEN_ENV_VAR, pano_id))

    # Context-managed so the per-pano connection pool is released deterministically, not at GC (#51).
    with _session() as session:
        meta_resp = session.get(
            f'{GRAPH_API_BASE}/{pano_id}',
            params={'fields': 'thumb_original_url'},
            # The token travels in the header, never the query string. requests puts the full URL in an
            # HTTPError's message and DownloadRunner logs str(e) for a failed pano, so a token in params
            # is a token in cleartext in scrape.log - which lives on the SHARED pano store, readable by
            # every lab user. Not hypothetical: richmond-va's scrape.log held a live token after the
            # 2026-09-01 400s. Graph API v4 accepts either form; only this one keeps the secret out of
            # URLs, logs, and any intermediary's access log.
            headers={'Authorization': 'OAuth %s' % token},
            timeout=30,
        )
        if meta_resp.status_code == 404:
            # The Graph API doesn't know this id: a permanent property of the pano, so it ledgers (#41).
            logging.error("Mapillary has no image %s (404)", pano_id)
            return DownloadResult.failure
        # Anything else non-200 is a condition of the RUN, not of this pano - 401/403 is an expired or
        # revoked token, and 429/5xx have already exhausted the retry adapter above. Raising retries the
        # pano next run; returning failure would ledger it permanently, so one night with a bad token
        # would blacklist every Mapillary pano in the city (#41).
        meta_resp.raise_for_status()

        try:
            image_url = meta_resp.json().get('thumb_original_url')
        except ValueError:
            # A proxy error page or a body truncated mid-flight - transient, so let it propagate (#41).
            logging.error("Mapillary metadata response for %s was not valid JSON", pano_id)
            raise

        if not image_url:
            # Mapillary knows the image but publishes no original-resolution rendition: permanent.
            logging.error("Mapillary metadata for %s missing thumb_original_url", pano_id)
            return DownloadResult.failure

        image_resp = session.get(image_url, stream=True, timeout=120)
        # The signed URL is short-lived, so a non-200 here is a stale URL or a CDN hiccup, never a property
        # of the pano - the metadata request above already proved the imagery exists (#41).
        image_resp.raise_for_status()

        # .part + rename: iter_content can die mid-stream (reset connection, full store), and a truncated
        # .jpg left at the final path is reported as a completed download by every later run.
        with atomic_output_path(out_image_name) as tmp_path:
            with open(tmp_path, 'wb') as f:
                for chunk in image_resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
    return DownloadResult.success
