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

from .common import DownloadResult, atomic_output_path, jpeg_dimensions

GRAPH_API_BASE = 'https://graph.mapillary.com'
TOKEN_ENV_VAR = 'MAPILLARY_ACCESS_TOKEN'


def is_token_set():
    return bool(os.environ.get(TOKEN_ENV_VAR))


class MapillaryErrorResponse(RuntimeError):
    """A response whose body is not what was asked for, at whatever status it arrived (#99).

    Three shapes: a Graph API body that is not the image record - Meta-style APIs can answer 200 with an
    {"error": {...}} envelope, and a proxy can answer 200 with anything at all; a 404 whose envelope says the
    TOKEN is the problem rather than the image; and an image body from the signed URL that is not a JPEG.
    None is a property of the pano, so this is a condition of the RUN: it raises, the image loop counts it
    among tonight's failures and ledgers nothing, and the pano is re-attempted next run (#41). Returning
    DownloadResult.failure instead would write one permanent downloaded=0 row per pano attempted for as
    long as the condition lasted - the 2026-09-01 incident (161 false rows, hand-edited out of
    pano_id_log.csv on the store) by another route.
    """


# The auth signature, as measured: every auth failure on 2026-09-05 carried code 190, at every status it
# arrived on (400, 401 and 500 - OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05 in tests/test_image_downloaders.py),
# and Meta's Graph family files token trouble under type OAuthException. What this is keyed on matters at
# exactly one status, 404: a Meta-style API puts an envelope on EVERY error status, a genuine "does not
# exist" included, so "any envelope" would stop a retired image from ever ledgering, while the auth
# signature on a 404 says the token cannot see the image - which is not a verdict on the image.
AUTH_ERROR_CODE = 190
AUTH_ERROR_TYPE = 'OAuthException'


def _envelope_detail(error):
    """What scrape.log gets for an error envelope: type and code, which a reader would search the API docs
    for, and the message - capped, because DownloadRunner logs str(e) per pano into a 10 MB x 3 rotation,
    and 9,229 panos times an uncapped HTML blob in `message` would rotate the night's own diagnosis away."""
    if isinstance(error, dict):
        return 'type=%s code=%s message=%.200s' % (error.get('type'), error.get('code'), error.get('message'))
    return '%.200r' % (error,)


def is_auth_envelope(payload):
    """True when a JSON body is an error envelope carrying the measured auth signature: code 190, or type
    OAuthException. Anything else - no envelope, an envelope of another kind, not an object - is False."""
    if not isinstance(payload, dict) or not isinstance(payload.get('error'), dict):
        return False
    error = payload['error']
    return str(error.get('code')) == str(AUTH_ERROR_CODE) or error.get('type') == AUTH_ERROR_TYPE


def original_rendition_url(payload, pano_id):
    """Read the original-resolution rendition URL out of a 200 metadata body, or refuse the body.

    Returns the URL, or None when Mapillary AFFIRMS it knows the image - the body names `pano_id` as its
    `id` - and publishes no `thumb_original_url` for it. That affirmed absence is the one body shape that is
    a permanent property of the pano, and the only one download_single_pano turns into
    DownloadResult.failure.

    Raises MapillaryErrorResponse for every other body: an error envelope (every auth failure measured on
    2026-09-05 carried Meta's {"error": {"type": ..., "code": 190, ...}} shape - see
    OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05 in tests/test_image_downloaders.py), a JSON value that is
    not an object, or an object that does not name this image. The metadata request asks for `id`
    precisely so that a permanent verdict rests on positive evidence, never on a field being absent from
    whatever came back (#99). A body that used to be read as "no rendition exists" - a bare {} - now raises.
    """
    if not isinstance(payload, dict):
        raise MapillaryErrorResponse("Mapillary metadata for %s was not a JSON object: %.80r" % (pano_id, payload))
    error = payload.get('error')
    if error is not None:
        # Checked before the URL, not instead of it: a body that carries both is still saying the token is
        # bad, and following its URL would store whatever that URL serves as the pano.
        raise MapillaryErrorResponse("Mapillary answered for %s with an error envelope (%s)"
                                     % (pano_id, _envelope_detail(error)))
    # str() on both sides: the API quotes the id (live check 2026-09-06) and the pano list may not, and a
    # bare != here would raise for every Mapillary pano forever with a message that looks like a match (#46).
    if str(payload.get('id')) != str(pano_id):
        raise MapillaryErrorResponse("Mapillary metadata for %s does not name that image (id=%.80r)"
                                     % (pano_id, payload.get('id')))
    return payload.get('thumb_original_url') or None


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
            # `id` as well as the URL: a "no rendition" verdict is permanent, so it has to rest on the
            # record naming this image, not on the URL being absent from whatever came back (#99).
            params={'fields': 'id,thumb_original_url'},
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
            # The Graph API doesn't know this id: a permanent property of the pano, so it ledgers (#41) -
            # unless the body says the TOKEN is what cannot see it. Read for the auth signature only, not
            # for any envelope: a Meta-style 404 for a retired image carries an envelope too, and refusing
            # those would re-request every retired image nightly forever. Not JSON, no envelope, or an
            # envelope of another kind keeps the verdict; the real 404 body is on the pre-merge check list.
            try:
                payload = meta_resp.json()
            except ValueError:
                payload = None
            if is_auth_envelope(payload):
                raise MapillaryErrorResponse("Mapillary answered 404 for %s with an auth error envelope (%s)"
                                             % (pano_id, _envelope_detail(payload['error'])))
            logging.error("Mapillary has no image %s (404)", pano_id)
            return DownloadResult.failure
        # Anything else non-200 is a condition of the RUN, not of this pano - 401/403 is an expired or
        # revoked token, and 429/5xx have already exhausted the retry adapter above. Raising retries the
        # pano next run; returning failure would ledger it permanently, so one night with a bad token
        # would blacklist every Mapillary pano in the city (#41).
        meta_resp.raise_for_status()

        try:
            payload = meta_resp.json()
        except ValueError:
            # A proxy error page or a body truncated mid-flight - transient, so let it propagate (#41).
            logging.error("Mapillary metadata response for %s was not valid JSON", pano_id)
            raise

        # Raises for an error envelope or any body that does not name this image (#99); None only when
        # Mapillary affirms it knows the image and publishes no original-resolution rendition: permanent.
        image_url = original_rendition_url(payload, pano_id)
        if image_url is None:
            logging.error("Mapillary knows image %s but publishes no original-resolution rendition", pano_id)
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
            # A 200 from the CDN is no more proof the body is the image than a 200 from the Graph API is
            # proof the body is the record: a signed-URL edge can answer 200 with an HTML error page. Saved
            # as .jpg, that page IS the resume marker - permanent with no ledger row to edit, and an error
            # per label in the cropper every night. gsv decodes every tile; this is the same check at the
            # same point, before the rename, so the .part is discarded and the pano retries (#41).
            if jpeg_dimensions(tmp_path) is None:
                raise MapillaryErrorResponse("Mapillary image response for %s was not a JPEG" % pano_id)
    return DownloadResult.success
