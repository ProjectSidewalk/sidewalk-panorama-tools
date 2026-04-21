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
from requests.packages.urllib3.util.retry import Retry

from .common import DownloadResult

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
        os.makedirs(destination_dir)
        os.chmod(destination_dir, 0o775 | stat.S_ISGID)

    out_image_name = os.path.join(destination_dir, pano_id + ".jpg")
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        # The orchestrator filters Mapillary panos out when the token is unset, so this shouldn't be reached.
        logging.error("Mapillary token not set (%s); cannot download %s", TOKEN_ENV_VAR, pano_id)
        return DownloadResult.failure

    session = _session()
    meta_resp = session.get(
        f'{GRAPH_API_BASE}/{pano_id}',
        params={'fields': 'thumb_original_url', 'access_token': token},
        timeout=30,
    )
    if meta_resp.status_code != 200:
        logging.error("Mapillary metadata request for %s failed: %s %s",
                      pano_id, meta_resp.status_code, meta_resp.text[:200])
        return DownloadResult.failure

    try:
        image_url = meta_resp.json().get('thumb_original_url')
    except ValueError:
        logging.error("Mapillary metadata response for %s was not valid JSON", pano_id)
        return DownloadResult.failure

    if not image_url:
        logging.error("Mapillary metadata for %s missing thumb_original_url", pano_id)
        return DownloadResult.failure

    image_resp = session.get(image_url, stream=True, timeout=120)
    if image_resp.status_code != 200:
        logging.error("Mapillary image download for %s failed: %s",
                      pano_id, image_resp.status_code)
        return DownloadResult.failure

    with open(out_image_name, 'wb') as f:
        for chunk in image_resp.iter_content(chunk_size=1 << 16):
            if chunk:
                f.write(chunk)
    os.chmod(out_image_name, 0o664)
    return DownloadResult.success
