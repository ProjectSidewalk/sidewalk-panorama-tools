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


def _token():
    """Read and normalise MAPILLARY_ACCESS_TOKEN once, so is_token_set() and the header build can never
    disagree about what counts as "set".

    .strip() closes two holes at once (2026-09 PR #100 review, findings 1 and 12). A trailing newline -
    easy to pick up from a BASH_ENV-sourced `export` line, or a copy-paste - makes requests.Session.get()
    raise InvalidHeader when the raw value is placed in a header, and InvalidHeader's message embeds the
    header value VERBATIM: the exact cleartext leak into scrape.log this whole change exists to close, just
    relocated from a well-formed token onto a malformed one. And bool(' ') is True, so without the strip a
    whitespace-only token passes is_token_set(), filter_supported_sources keeps the city's Mapillary panos
    in the run, and every one of them 401s nightly forever with nothing permanent ledgered - #41's
    ledger-blacklist failure mode, triggered by whitespace instead of an absent variable.
    """
    return (os.environ.get(TOKEN_ENV_VAR) or '').strip()


def is_token_set():
    return bool(_token())


class TokenRedactionFilter(logging.Filter):
    """A logging.Filter that rewrites the live Mapillary token to '<redacted>' wherever it appears in a
    record. DownloadRunner.configure_logging wires this onto the run's one log handler - the chokepoint
    every record passes through regardless of which logger (or third-party library) produced it.

    This is the backstop for the whole CLASS of leak, not just the params-vs-headers call site finding 1
    (2026-09 PR #100 review) fixed: that one call-site discipline was defeated within the same PR by a
    library exception message (InvalidHeader) that embeds a header value verbatim. Every future leak of this
    shape - a message built by code with no idea the string it's formatting is a secret - looks the same
    from here, so a filter at the one place every record funnels through is what closes the class rather
    than the instance.

    Reads the token from the environment on every record rather than caching it at construction: a cached
    value would go stale the moment the token rotates in a long-running process, and the test suite sets and
    clears MAPILLARY_ACCESS_TOKEN with monkeypatch mid-process, which a cached value would silently miss.
    The per-record cost is one dict lookup plus a couple of string operations - negligible next to the
    synchronous write to scrape.log over sshfs this filter sits in front of (configure_logging's docstring).

    A no-op when the token is unset or blank: str.replace('', '<redacted>') would insert '<redacted>'
    between every character of every message, since '' is a substring of everything.
    """

    def filter(self, record):
        token = _token()
        if not token:
            return True
        if isinstance(record.msg, str):
            record.msg = record.msg.replace(token, '<redacted>')
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(a.replace(token, '<redacted>') if isinstance(a, str) else a for a in args)
        elif isinstance(args, dict):
            record.args = {k: (v.replace(token, '<redacted>') if isinstance(v, str) else v)
                            for k, v in args.items()}
        # record.exc_info (and the record.exc_text the Formatter caches from it) are NOT scrubbed here: a
        # traceback is rendered lazily by the Formatter AFTER filters run, so redacting it would mean
        # reformatting the whole exception ourselves rather than mutating a string already on the record.
        # Nothing in this module logs the token via exc_info - str(e) always travels as a %s argument
        # (DownloadRunner.py:376) - so msg/args is where the invariant needs to hold today; a future
        # logging.exception() call carrying the token in its traceback would not be covered by this filter.
        return True


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
    for, and the message. ALL THREE are capped, because DownloadRunner logs str(e) per pano into a 10 MB x 3
    rotation, and 9,229 panos times an uncapped blob would rotate the night's own diagnosis away.

    Only `message` was capped at first, which left the likelier shape open: every field here comes from the
    same untrusted place - any JSON body carrying an `error` object - and a proxy that stuffs a class name or
    an HTML fragment into `type` is more plausible than Mapillary sending a 100 KB `message`. Measured before
    this cap, a `type` alone produced a 100,080-character line for one pano. Cap the channel, not the field
    you happened to think of.
    """
    if isinstance(error, dict):
        return 'type=%.80s code=%.40s message=%.200s' % (error.get('type'), error.get('code'),
                                                         error.get('message'))
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

    token = _token()
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
            # every lab user. Not hypothetical: a production city's scrape.log held a live token after
            # the 2026-09-01 400s. Graph API v4 accepts either form; only this one keeps the secret out of
            # URLs, logs, and any intermediary's access log.
            headers={'Authorization': 'OAuth %s' % token},
            timeout=30,
        )
        if meta_resp.status_code == 404:
            # The Graph API doesn't know this id: a permanent property of the pano, so it ledgers (#41) -
            # unless the body says the TOKEN is what cannot see it. Read for the auth signature only, not
            # for any envelope: a Meta-style "does not exist" carries an envelope too (code 100, subcode 33,
            # measured 2026-09-06 - OBSERVED_MAPILLARY_NOT_FOUND_2026_09_06 in tests/test_image_downloaders.py),
            # and refusing those would re-request every retired image nightly forever. Not JSON, no envelope,
            # or an envelope of another kind keeps the verdict. This is the DOCUMENTED shape, not the observed
            # one: Mapillary answered an id it does not have with a 400, which raise_for_status() below
            # refuses, deliberately unledgered - see the comment there.
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
        #
        # That includes Mapillary's measured answer for an id it does not have (2026-09-06): a 400, code 100,
        # error_subcode 33, whose message itself reads "does not exist, cannot be loaded due to missing
        # permissions, or does not support this operation". A token lacking scope would produce that body
        # for every pano in the city, which is the 2026-09-01 incident by another route, so it stays a
        # condition of the run. The cost is one metadata request per retired image per night; writing
        # 100/33 off as permanent needs a run-level breaker first, the shape refetch_panos.py uses for
        # undersized, and that is a follow-up, not this function.
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
            # THE one permanent verdict this function still reaches in production, and it has no breaker
            # (#113). Every other permanent path is a 404, and the 2026-09-06 live check found that Mapillary
            # answers an id it does not have with a 400: no 404 has ever been observed, so this branch alone
            # decides what gets a downloaded=0 row. That matters because the scope-less token - the one auth
            # condition nobody can measure - need not arrive as an envelope at all: Meta's Graph family
            # commonly answers a permission-denied field by OMITTING it from an otherwise-healthy 200 record,
            # which is exactly this shape. A run-level breaker (N consecutive of these stop ledgering, the
            # shape refetch_panos.py uses for undersized) is the defence that does not depend on knowing
            # which way that goes; the depth phase and refetch_panos both have one and this loop does not.
            logging.error("Mapillary knows image %s but publishes no original-resolution rendition", pano_id)
            return DownloadResult.failure

        image_resp = session.get(image_url, stream=True, timeout=120)
        # The signed URL is short-lived, so a non-200 here is a stale URL or a CDN hiccup, never a property
        # of the pano - the metadata request above already proved the imagery exists (#41).
        #
        # This raise_for_status() DOES put image_url - including its oh=/oe= signature and expiry, bearer
        # credentials for this one CDN object - into the HTTPError's message, and DownloadRunner logs str(e)
        # into scrape.log the same as the account token above (2026-09 PR #100 review, finding 5).
        # Deliberately left unredacted: TokenRedactionFilter only knows MAPILLARY_ACCESS_TOKEN's value, not a
        # URL minted fresh per pano, so it structurally can't cover this one. The trade is accepted rather
        # than engineered around because the blast radius is much narrower than the account token's - this
        # credential authorises one image and expires in minutes, versus every download the account token
        # could ever make until it's rotated.
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
            # per label in the cropper every night. Same point in the flow as gsv's per-tile decode - before
            # the rename, so the .part is discarded and the pano retries (#41) - but a weaker test: this
            # parses the SOF header, where gsv's Image.open decodes. It refuses a body that is not a JPEG at
            # all (an error page, an empty body, a PNG), and NOT a JPEG truncated after its header, which
            # still reports its full dimensions from 5% of its bytes. That gap is covered upstream instead:
            # a short read raises out of iter_content before this line, and the .part never lands.
            if jpeg_dimensions(tmp_path) is None:
                raise MapillaryErrorResponse("Mapillary image response for %s was not a JPEG" % pano_id)
    return DownloadResult.success
