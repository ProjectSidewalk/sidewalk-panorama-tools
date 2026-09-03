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
    return DownloadResult.success
