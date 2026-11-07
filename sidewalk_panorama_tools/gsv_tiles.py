"""The GSV tile fetcher, installable: the cbk tile fan-out, the zoom probe, the frame check, the stitch and
the push-back classification (#222, step 2).

Moved verbatim from this repo's ``downloaders/gsv.py``, which composes these mechanics with the nightly
scrape's policy - the ledgers, the breakers, the block latch, photometa, the display copy, the edge-band flag
and the atomic save - and re-exports every public name here, so there is one implementation for the scraper
and for every consumer that pins this package (sidewalk-validator-ai, sidewalk-ai-api, RampNet).

Usage (docs/geometry.md, "Fetching a GSV panorama")::

    pip install "sidewalk-panorama-tools[gsv] @ git+https://github.com/ProjectSidewalk/sidewalk-panorama-tools@<tag>"

    from sidewalk_panorama_tools import gsv_tiles

    zoom = gsv_tiles.probe_zoom(pano_id)              # 5, 3, or None: no imagery at either (the pano is gone)
    if zoom is not None and gsv_tiles.frame_covers_pano(pano_id, width, height, zoom):
        stitched = gsv_tiles.fetch_pano_image(pano_id, width, height, zoom)
        stitched.image.save(...)                     # a PIL image at exactly (width, height)

``frame_covers_pano`` returns False when Google serves the pano larger than ``(width, height)`` and raises
:class:`FrameDisagreementError` when it serves it smaller; it must only be asked after the probe found imagery.
:class:`TilePushbackError` (and :func:`pushback_reason`, which reads every shape of refusal) means Google is
refusing this host: stop for the night, never retry.

Settings, read at call time: ``HEADERS`` (rotated per request), ``PROXIES`` and ``CONCURRENCY`` (tiles in
flight). Change them with :func:`configure`; a test monkeypatches them on this module, which undoes itself.

Needs the ``[gsv]`` extra (requests, urllib3, aiohttp, backoff); importing this module without it raises an
ImportError that says so. ``sidewalk_panorama_tools`` itself does not import this module.
"""

import asyncio
import collections
import contextvars
import logging
import math
import random
import re
import time
from io import BytesIO

from PIL import Image

try:
    import aiohttp
    import backoff
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as e:
    raise ImportError("sidewalk_panorama_tools.gsv_tiles needs the [gsv] extra: "
                      "pip install 'sidewalk-panorama-tools[gsv]'") from e

from . import store

__all__ = ['CBK_BASE_URL', 'CONCURRENCY', 'DEFAULT_HEADERS', 'FrameDisagreementError', 'HEADERS', 'PROXIES',
           'STITCH_MAX_BLACK_FRACTION', 'StitchedPano', 'StitchedPanoMostlyBlackError', 'TILE_MAX_TRIES',
           'TILE_PUSHBACK_STATUSES', 'TILE_RETRY_MAX_SECONDS', 'TILE_RETRY_MAX_WAIT', 'TILE_SIZE',
           'TilePushbackError', 'choose_zoom', 'configure', 'fetch_pano_image', 'frame_covers_pano',
           'probe_zoom', 'pushback_reason', 'tile_url']


# --- settings: read at call time; change them with configure() ------------------------------------------------

# The request headers, one picked at random per request. Moved here from config.py, whose `headers_list` is
# still the scraper's own setting - the box carries local edits to that file, and the operator's override is
# its point - and which downloaders/gsv.py hands to configure() at import. tests/test_gsv_tiles.py pins the
# two equal, so they cannot drift silently. A tuple, so HEADERS never aliases a list someone can append to.
# -------------------------------
# Windows Headers
# -------------------------------

# Edge
DEFAULT_HEADERS = (
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.182 Safari/537.36 Edg/88.0.705.81',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # Firefox 85 on Windows 10
    {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:85.0) Gecko/20100101 Firefox/85.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Referer': 'http://maps.google.com',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    },

    # Chrome 88 Windows 10
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.190 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # Opera for Windows 10
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36 OPR/74.0.3911.160',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # -------------------------------
    # Mac Headers
    # -------------------------------

    # Edge 88 Mac
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.182 Safari/537.36 Edg/88.0.705.81',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-GB,en;q=0.9,en-US;q=0.8',
        'Referer': 'http://maps.google.com',
    },

    # Opera 74 Mac
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36 OPR/74.0.3911.160',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # Chrome 88 Mac
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.192 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Referer': 'http://maps.google.com',
        'Accept-Language': 'en-GB,en;q=0.9',
    },

    # Firefox 85 Mac
    {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.16; rv:86.0) Gecko/20100101 Firefox/86.0',
        'Accept': 'image/webp,*/*',
        'Accept-Language': 'en-US,en;q=0.5',
        'Referer': 'http://maps.google.com',
        'DNT': '1',
        'Connection': 'keep-alive',
    },

    # Safari 14 Mac
    {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Upgrade-Insecure-Requests': '1',
        'Host': 'maps.google.com',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15',
        'Accept-Language': 'en-ie',
        'Connection': 'keep-alive',
    },

    # -------------------------------
    # Ubuntu Headers
    # -------------------------------

    # Firefox 86 Ubuntu
    {
        'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:86.0) Gecko/20100101 Firefox/86.0',
        'Accept': 'image/webp,*/*',
        'Accept-Language': 'en-GB,en;q=0.5',
        'Referer': 'http://maps.google.com',
        'DNT': '1',
        'Connection': 'keep-alive',
    },

    # Chrome 88 Ubuntu
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.182 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # Opera 74 Ubuntu
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36 OPR/74.0.3911.160',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': 'http://maps.google.com',
    }
)

HEADERS = DEFAULT_HEADERS

# scheme -> proxy URL, or None for "no proxy for this scheme"; normalized by configure() (_normalize_proxies).
PROXIES = {}

# Tiles in flight per pano: the fan-out's concurrency slot and the aiohttp connector's limit.
CONCURRENCY = 8


def _normalize_proxies(raw):
    """Normalize the proxy dict from config.py, treating placeholder values as unset - per key.

    config.py ships bare-scheme placeholders ('http://' for both keys), and handing one of those to requests
    as a proxy URL breaks every request through it. Normalizing per key means setting a real proxy for one
    scheme doesn't leak the other key's placeholder through (#51).
    """
    return {scheme: (url if url not in (None, '', 'http://', 'https://') else None)
            for scheme, url in raw.items()}


def configure(*, headers=None, proxies=None, concurrency=None):
    """Set the module's request settings; an argument left None keeps its current value.

    headers     - an iterable of header dicts, one chosen at random per request (stored as a tuple).
    proxies     - a {'http': url, 'https': url} dict as config.py writes it; placeholder values ('http://',
                  'https://', '', None) mean "no proxy for that scheme" (_normalize_proxies).
    concurrency - tiles in flight per pano, at least 1.

    Settings are module globals read at call time, which is what lets the probe session, the tile fan-out and
    downloaders/gsv.py's photosphere and depth sessions all follow one call. A caller (or a test) must go
    through here, or monkeypatch the attribute on this module; a binding taken elsewhere at import goes stale
    on the next call.
    """
    global HEADERS, PROXIES, CONCURRENCY
    if headers is not None:
        headers = tuple(headers)
        if not headers:
            raise ValueError('configure(headers=...) needs at least one header dict')
        HEADERS = headers
    if proxies is not None:
        PROXIES = _normalize_proxies(proxies)
    if concurrency is not None:
        if isinstance(concurrency, bool) or int(concurrency) != concurrency or concurrency < 1:
            raise ValueError('configure(concurrency=...) must be a whole number of at least 1, not %r'
                             % (concurrency,))
        CONCURRENCY = int(concurrency)


def _random_header():
    return random.choice(HEADERS)


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
    # setdefaults env proxies into this dict in place, which must not corrupt PROXIES for later calls (#51).
    response = session.get(url, headers=_random_header(), proxies={k: v for k, v in PROXIES.items() if v},
                           stream=stream)
    if not stream:
        return response
    # The streamed path is the two probes (_probe_zoom, frame_covers_pano), and both read a black
    # body as a verdict: "retired" (ledgered downloaded=0, never re-asked) and "the frame covers the pano" -
    # or, for frame_covers_pano's in-grid tile (#181), "Google serves it smaller", a transient refusal.
    # Google answers both of those with a black 200, so only a 200 is evidence (#166 option b, the #99 rule).
    # `== 200`, not raise_for_status(): a 206 or a final 3xx is no more evidence than a 403. The raise is
    # transient wherever it lands. 429/5xx should normally not reach here, since the adapter's Retry owns
    # them and an exhausted retry raises; if one does, this raises it like any other non-200.
    if response.status_code != 200:
        response.close()     # close it; nothing will read this body
        raise requests.HTTPError('cbk probe answered %s, not 200: %s' % (response.status_code, url),
                                 response=response)
    # A 200 that landed on Google's interstitial is a captcha page, not a tile: raised with its Response, so
    # pushback_reason reads it as the refusal it is rather than PIL failing on HTML (#162).
    landing_url = _interstitial_url(_requests_response_urls(response))
    if landing_url is not None:
        response.close()
        raise requests.HTTPError('cbk probe landed on %s: %s' % (landing_url, url), response=response)
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
CBK_BASE_URL = 'https://maps.google.com/cbk?output=tile&cb_client=maps_sv&onerr=3&renderer=spherical&v=4'


def tile_url(pano_id, zoom, x, y):
    """The cbk URL of tile (x, y) of `pano_id`'s grid at `zoom`: the one place a tile URL is built.

    The tile fan-out, the zoom probe and the frame check all call it, so a parameter added to CBK_BASE_URL
    reaches every request at once and is checked once (tests/test_gsv_tile_contract.py). The field order is
    part of the contract - tests read a URL back by its &zoom=, &x= and &y= fields - and tests/test_gsv_tiles.py
    pins the string byte for byte.

    >>> tile_url('abc', 5, 1, 2)[len(CBK_BASE_URL):]
    '&zoom=5&x=1&y=2&panoid=abc'
    """
    return f'{CBK_BASE_URL}&zoom={zoom}&x={x}&y={y}&panoid={pano_id}'


# Tile failures worth retrying. aiohttp.web.HTTPServerError used to head this tuple, but it is a SERVER-side
# response class that client code never raises (#52 item 3) - importing aiohttp.web for it was pure cost. The
# rest of the old tuple was redundant: ClientResponseError, ServerConnectionError, ServerDisconnectedError and
# ClientHttpProxyError all derive from ClientError. asyncio.TimeoutError does NOT, though, so a plain request
# timeout used to get zero retries - and now that one failed tile fails the whole pano, that costs a download.
_TILE_RETRY_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)

# How a tile answer is treated (#162). Before this, every ClientError - a 429 and a 403 included - was retried
# 10 times with uncapped exponential backoff: 5,120 requests and ~1 MB of scrape.log per refused 16384-wide
# pano, which is a soft refusal being escalated by the client. Now:
#   * 429/403, or a landing URL carrying one of _BLOCK_URL_MARKERS, is PUSH-BACK: raised as TilePushbackError,
#     which is not in _TILE_RETRY_ERRORS, so backoff never sees it. (The URL markers are the depth phase's
#     _raise_if_blocked rule applied by analogy; no CBK interstitial has been observed.)
#   * any other 4xx except 408 is this pano's problem, not weather: not retried (_tile_error_is_final), and
#     not push-back either. Every retired pano measured answers 200 with an all-black body, never a 4xx.
#   * 5xx, 408, timeouts, connection errors and a wrong Content-Type are retried, TILE_MAX_TRIES times, each
#     wait capped at TILE_RETRY_MAX_WAIT seconds, and no retry once the tile has spent TILE_RETRY_MAX_SECONDS
#     of its own time - since its first attempt, less every wait for a fan-out slot, on retries too (see
#     _tile_retry_budget_spent).
# The two limits are read at call time (see _tile_retry_waits and the decorator), so a test can zero them.
TILE_PUSHBACK_STATUSES = frozenset((403, 429))
TILE_MAX_TRIES = 10
TILE_RETRY_MAX_WAIT = 32
TILE_RETRY_MAX_SECONDS = 120
# Substrings that mark Google's "you are a robot" landing pages rather than pano metadata. Read at call time, by
# _request_tile and _interstitial_url here and by downloaders/gsv.py's depth-phase hook (_raise_if_blocked).
_BLOCK_URL_MARKERS = ('/sorry/', 'consent.google.com')


class TilePushbackError(Exception):
    """Google refused a tile - an HTTP 429 or 403, or a redirect onto its interstitial - rather than failing it.

    Deliberately NOT an aiohttp.ClientError, so the tile's backoff decorator cannot retry it: asking again is
    how a soft refusal is escalated into a ban. `requested`/`total` are filled in by fetch_pano_image once the
    fan-out has been abandoned, so the one scrape.log line the image loop writes says how much was spent.
    """

    def __init__(self, status, x, y, requested=None, total=None, landing_url=None):
        super().__init__(status, x, y)
        self.status = status
        self.x = x
        self.y = y
        self.requested = requested
        self.total = total
        self.landing_url = landing_url

    @property
    def reason(self):
        """The short cause the image loop's log line carries in parentheses: `HTTP 429`, or `interstitial`."""
        return 'interstitial' if self.landing_url else 'HTTP %d' % (self.status,)

    def __str__(self):
        if self.landing_url:
            message = 'tile (%d, %d) landed on %s' % (self.x, self.y, self.landing_url)
        else:
            message = 'tile (%d, %d) answered HTTP %d' % (self.x, self.y, self.status)
        if self.requested is not None and self.total is not None:
            message += '; %d of %d tile requests made, the rest abandoned' % (self.requested, self.total)
        return message


def _tile_error_is_final(e):
    """backoff's giveup predicate: an HTTP 4xx other than 408 will not get better by asking again.

    A Content-Type failure carries the response's own status (200), so it stays retryable - CBK intermittently
    answers with a non-image body (tests/test_gsv_tile_contract.py's _live_get). 429 and 403 never reach here:
    _fetch_tile has already turned them into TilePushbackError, which the decorator does not catch.
    """
    return isinstance(e, aiohttp.ClientResponseError) and 400 <= e.status < 500 and e.status != 408


class _TileClock:
    """One tile's retry-budget clock: when its first attempt began, and how long it has since queued for a slot."""

    __slots__ = ('tile', 'started', 'slot_wait')

    def __init__(self, tile):
        self.tile = tile
        self.started = time.monotonic()
        self.slot_wait = 0.0


# This task's _TileClock. A context variable because every tile is its own task (ensure_future copies the
# context), so each task gets its own value, and backoff's retries run in that same task and see it. The clock
# names its tile, so a task that ever fetched two DIFFERENT tiles in turn starts a fresh clock for the second
# rather than carrying the first one's into its budget.
_TILE_CLOCK = contextvars.ContextVar('_TILE_CLOCK', default=None)


def _tile_clock(tile):
    """This task's clock for `tile`, started now if the task has none for it yet."""
    clock = _TILE_CLOCK.get()
    if clock is None or clock.tile != tile:
        clock = _TileClock(tile)
        _TILE_CLOCK.set(clock)
    return clock


def _tile_retry_budget_spent():
    """True once this tile has spent TILE_RETRY_MAX_SECONDS of its OWN time (#172 review, ops 2; final MINOR 1).

    Its own time is the time since its first attempt began, less every wait for a fan-out slot - the first one
    and every retry's. Not backoff's max_time, whose clock starts when the decorated call does: all 512 tile
    tasks start at t=0 and then queue for a slot, so on a slow fan-out the queue alone spent the late tiles'
    budget. Excluding only the first wait was not enough either: each retry queues again, behind every tile
    not yet started (the Semaphore wakes in order), so an EARLY tile that failed lost its retries instead -
    at 512 tiles, 8 slots and 2 s a tile, one re-queue is ~128 s against a 120 s budget. What is left counts:
    the requests themselves and backoff's waits between them. Checked after each failed attempt, like
    max_time, so the last wait may overshoot by up to one capped wait; a single attempt is bounded
    separately, by aiohttp's own per-request timeout.
    """
    clock = _TILE_CLOCK.get()
    return clock is not None and time.monotonic() - clock.started - clock.slot_wait >= TILE_RETRY_MAX_SECONDS


def _tile_should_give_up(e):
    """backoff's giveup predicate: the error will not get better by asking again, or the tile is out of time."""
    return _tile_error_is_final(e) or _tile_retry_budget_spent()


def _tile_retry_waits():
    """backoff's wait generator: exponential, each wait capped at TILE_RETRY_MAX_WAIT seconds.

    A generator function rather than `backoff.expo` with `max_value=` so the cap is read when a tile starts
    retrying, not when the module was imported - that is what lets a test zero it. `yield from` forwards the
    priming send() backoff 2.x makes (and 1.x's plain next()) straight to expo.
    """
    yield from backoff.expo(max_value=TILE_RETRY_MAX_WAIT)


class _TileAbandonedError(Exception):
    """A tile never requested, because another tile of the same pano had already been refused (#162)."""


class _TileFanOut:
    """One pano's fan-out state, shared by every tile task: the concurrency slot, and what the tiles saw.

    `slot` bounds requests in flight to CONCURRENCY, like the connector, but it is what lets the refusal
    check sit AFTER a task gets its turn - all 512 tasks start at t=0, so a check at task start would see
    nothing refused yet and every one would go on to send. `requests` counts requests actually made,
    `retries` the backoff retries (backoff itself no longer logs), `refused` the first TilePushbackError.
    """

    def __init__(self):
        self.slot = None
        self.requests = 0
        self.retries = 0
        self.refused = None


# Why a context variable rather than a parameter: `_download_tiles(tiles)` and `_download_tile(session, tile)`
# are replaced by stubs of exactly those signatures across the suite (stub_tiles, TestTheTileFanOutContract,
# test_image_downloaders.py), so threading a state object through them would break every one. fetch_pano_image
# sets it around asyncio.run; asyncio.run and ensure_future copy the context, so every tile task sees the same
# _TileFanOut object. When it is unset - _fetch_tile or _download_tile driven directly - behaviour is exactly
# what it was before #162: no slot, no counting, no abandonment.
_TILE_FANOUT = contextvars.ContextVar('_TILE_FANOUT', default=None)


def _count_tile_retry(details):
    """backoff's on_backoff handler: count the retry on this pano's fan-out, in place of backoff's log line."""
    fanout = _TILE_FANOUT.get()
    if fanout is not None:
        fanout.retries += 1

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
    return [(x, y, tile_url(pano_id, zoom, x, y))
            for y in range(tiles_y) for x in range(tiles_x)]


async def _fetch_tile(session, tile):
    """Fetch one tile; return (x, y, jpeg_bytes).

    Undecorated so tests can drive it without backoff's sleeps; _download_tile below is the retrying variant
    the fan-out uses.

    A refusal - HTTP 429/403 (raised by the session's raise_for_status on entering the request), or a landing
    URL on Google's interstitial - becomes TilePushbackError, which backoff does not catch (#162). Inside a
    pano's fan-out (_TILE_FANOUT set) the first refusal also abandons every tile not yet requested.
    """
    fanout = _TILE_FANOUT.get()
    clock = _tile_clock(tile)
    if fanout is None or fanout.slot is None:
        return await _request_tile(session, tile)
    queued_at = time.monotonic()
    async with fanout.slot:
        # Every wait for a slot is the fan-out's time, not this tile's (_tile_retry_budget_spent).
        clock.slot_wait += time.monotonic() - queued_at
        # After taking the slot, never before: see _TileFanOut.
        if fanout.refused is not None:
            raise _TileAbandonedError('tile (%d, %d) not requested: %s' % (tile[0], tile[1], fanout.refused))
        fanout.requests += 1
        try:
            return await _request_tile(session, tile)
        except TilePushbackError as e:
            if fanout.refused is None:
                fanout.refused = e
            raise


async def _request_tile(session, tile):
    """The one HTTP request behind _fetch_tile, and the translation of Google's refusal into push-back."""
    x, y, url = tile
    try:
        async with session.get(url, proxy=PROXIES.get("http"), headers=_random_header()) as response:
            # Before the Content-Type check: an interstitial is also not image/jpeg, and that check raises a
            # RETRYABLE error - the one answer to push-back this module must never give.
            landing_url = str(response.url)
            if any(marker in landing_url for marker in _BLOCK_URL_MARKERS):
                raise TilePushbackError(response.status, x, y, landing_url=landing_url)
            # .get(), not [..]: a response with no Content-Type must raise the same retryable error as a wrong
            # one, not a bare KeyError that is in neither backoff tuple (#45).
            content_type = response.headers.get('Content-Type', '')
            if content_type[0:10] != "image/jpeg":
                raise aiohttp.ClientResponseError(
                    response.request_info, response.history, status=response.status,
                    message="unexpected Content-Type %r for tile (%d, %d)" % (content_type, x, y))
            return x, y, await response.content.read()
    except aiohttp.ClientResponseError as e:
        if e.status in TILE_PUSHBACK_STATUSES:
            raise TilePushbackError(e.status, x, y) from e
        # The session raises for any non-2xx landing while the request is being ENTERED, so the in-context
        # check above only ever sees a 2xx one. An interstitial served as 503 (or anything else) lands here,
        # and without this it would be retried: 10 tries x (redirect + landing) per tile (#172 review).
        landing_url = _interstitial_url(_aiohttp_error_urls(e))
        if landing_url is not None:
            raise TilePushbackError(e.status, x, y, landing_url=landing_url) from e
        raise


# logger=None: backoff's own handlers wrote one INFO line per retry and one ERROR per give-up - 5,120 lines
# for one refused pano. Capping the 'backoff' logger at WARNING would still have left the 512 ERROR lines, so
# the decorator logs nothing and each pano owns its one line (fetch_pano_image, or the image loop for a
# refusal). The time budget is in the giveup predicate, not max_time, so it can leave out every wait for a
# fan-out slot, first request and retries alike (_tile_retry_budget_spent), and is read at call time.
_download_tile = backoff.on_exception(_tile_retry_waits, _TILE_RETRY_ERRORS, max_tries=TILE_MAX_TRIES,
                                      giveup=_tile_should_give_up,
                                      on_backoff=_count_tile_retry, logger=None)(_fetch_tile)


async def _download_tiles(tiles):
    """Fetch every tile concurrently; failures come back as exception OBJECTS in the result list.

    No whole-batch backoff on purpose: each tile already retries up to 10 times in _download_tile, and with
    return_exceptions=True nothing propagates out of the gather anyway - the decorator this replaces could
    never fire for tile errors and only re-ran connector construction, re-downloading every tile (#45).
    """
    fanout = _TILE_FANOUT.get()
    if fanout is not None:
        # Built here, inside the running loop, because a Semaphore belongs to the loop that first waits on it.
        fanout.slot = asyncio.Semaphore(CONCURRENCY)
    conn = aiohttp.TCPConnector(limit=CONCURRENCY)
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

    Defence in depth rather than a live requirement, since dropping `fover` from CBK_BASE_URL removed the
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


# Defined in sidewalk_panorama_tools/store.py (#222 step 2), which needs nothing but the image, so a consumer
# judging a crop with it never needs the [gsv] extra. The alias keeps the stitch guard, refetch_panos and the
# stitcher/tile-contract tests on the name they already use; it is the same function object, not a copy.
_black_fraction = store.black_fraction


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


class FrameDisagreementError(Exception):
    """The pano Google serves is not the frame the caller asked for, so stitching it would save a crop.

    Raised here by frame_covers_pano when Google serves the pano SMALLER than the frame: the grid's own last
    column is black (#181). The message starts with the words "frame disagreement", which is what to grep a
    log for. This repo's nightly downloader (downloaders/gsv.py) raises it for three more cases it decides
    itself: photometa's reported levels do not admit the frame, photometa's tiles are not 512 px, or
    frame_covers_pano returned False (Google serves it larger).

    What to do about one is the caller's policy, and the nightly's is a transient, unledgered failure,
    retried next run (its reasons are beside the re-export in downloaders/gsv.py); refetch_panos returns it
    as its unledgered 'frame_shrank'. Never store a frame refused with it. One class across the repo, because
    DownloadRunner counts refusals by isinstance (#207): an error that is not a frame refusal must not
    subclass it.
    """


def choose_zoom(sizes, width, height):
    """(zoom, consistent) for fetching a (width, height) frame from a pano whose levels are `sizes`.

    The HIGHEST k for which _dims_at_zoom(width, height, k) == sizes[k] - i.e. the grid the stitcher would
    request at k is exactly a level Google serves - with consistent=True. When no k qualifies the caller's frame
    is not in this pano's pyramid at all: (len(sizes) - 1, False).

    Why a consistency check and not "the top level": the stitcher derives its grid from the caller's frame, so
    fetching the top level for a frame smaller than it returns the top-left corner of the pano at the frame's
    exact dimensions - no black, no undersized tile, nothing downstream can see it. The probe this replaces did
    exactly that for an 8192x4096 app frame on a pano Google serves at 16384x8192.

    >>> choose_zoom([(512, 256), (1024, 512), (2048, 1024)], 2048, 1024)
    (2, True)
    >>> choose_zoom([(512, 256), (1024, 512), (2048, 1024)], 1024, 512)     # a lower level IS the frame
    (1, True)
    >>> choose_zoom([(512, 256), (1024, 512)], 2048, 1024)                  # truncated: a 2x upscale
    (1, True)
    >>> choose_zoom([(416, 208), (832, 416)], 1024, 512)                    # another family entirely
    (1, False)
    """
    for zoom in range(len(sizes) - 1, -1, -1):
        if _dims_at_zoom(width, height, zoom) == tuple(sizes[zoom]):
            return zoom, True
    return len(sizes) - 1, False


def _probe_zoom(pano_id):
    """The two blank-tile requests that picked every zoom before #74: 5, 3, or None (no imagery at either).

    Kept verbatim as the fallback - for every photometa failure, for a fresh block latch, and for photometa's
    "not found" - and so still the only evidence a PERMANENT None verdict may rest on (see resolve_frame).
    Only a 200 is that evidence: _get_response raises requests.HTTPError, closing the response first, on any
    other status (#166 option b), so a 403 or 404 with a black body is a transient failure, never None.
    """
    # Session scoped to the zoom probes; the tile fan-out uses its own aiohttp session. This runs once per
    # pano, so leaving it unclosed would pile up connection pools until GC (#51).
    with _request_session() as session:
        url_zoom_3 = tile_url(pano_id, 3, 0, 0)
        url_zoom_5 = tile_url(pano_id, 5, 0, 0)

        req_zoom_3 = _get_response(url_zoom_3, session, stream=True)
        im_zoom_3 = Image.open(req_zoom_3)
        req_zoom_5 = _get_response(url_zoom_5, session, stream=True)
        im_zoom_5 = Image.open(req_zoom_5)

        # In some cases (e.g., old GSV images), we don't have zoom level 5, so Google returns a transparent
        # image. This means we need to set the zoom level to 3. Google also returns a transparent image if
        # there is no imagery. So check at both zoom levels. How to check:
        # http://stackoverflow.com/questions/14041562/python-pil-detect-if-an-image-is-completely-black-or-white
        if im_zoom_5.convert("L").getextrema() != (0, 0):
            return 5
        if im_zoom_3.convert("L").getextrema() != (0, 0):
            return 3
        # Can't determine zoom.
        return None


# The consumer's name for the probe (docs/geometry.md); the same function object.
probe_zoom = _probe_zoom


def _is_exactly_black(image):
    """True when every pixel of `image` decodes to zero luma: CBK's answer for a tile outside the pano.

    Exact zero, not a threshold: an out-of-range tile is answered 200 OK with an all-black JPEG whose luma
    extrema are (0, 0), while a genuine edge tile is black-PADDED but never all black, and dark imagery is
    not zero after JPEG (both pinned on committed real bytes in tests/test_gsv_tile_contract.py). The rule
    _probe_zoom applies inline.
    """
    return image.convert('L').getextrema() == (0, 0)


def frame_covers_pano(pano_id, width, height, zoom):
    """Is a (width, height) grid at `zoom` exactly what Google serves for this pano?

    True when it is. False when Google serves the pano LARGER (a tile past the grid has imagery). Raises
    FrameDisagreementError when Google serves it SMALLER (the grid's own last column is black) - #181 Part 1.

    Asked with three tile requests, spent BEFORE the 512-tile fan-out so a bad frame costs three rather than
    515. An out-of-range tile is answered 200 OK with an all-black body (pinned on real bytes in
    tests/test_gsv_tile_contract.py), so:

    1. One column past the grid, on the middle row: imagery means the pano is wider. False.
    2. One row past the grid, in column 0: imagery means the pano is taller. False.
    3. The grid's own last column, on the same middle row, (tiles_x - 1, tiles_y // 2): BLACK means the pano
       ends before the grid does. Raises.

    False and the raise are kept apart because their callers treat them differently. A stored file smaller
    than the frame Google serves is a fact about the store (a pano's frame does not change, #217), and
    refetch_panos.py ledgers it as 'frame_grew'. A frame larger than Google serves is the mismatch photometa's
    arm refuses with FrameDisagreementError (unledgered, retried next run), and this is the same refusal on the
    probe arm: the app's record is what is wrong, and a gsv_data refresh corrects it. Without check 3 that
    frame passed checks 1 and 2 - nothing past a 32x16 grid has imagery on a 26x13 pano - and stitched ~34%
    black, under STITCH_MAX_BLACK_FRACTION, saved and ledgered downloaded=1; refetch_panos.py spent the
    512-tile fan-out to refuse it as 'too_black' and then ledgered that for good.

    The nightly downloader needs this only when the tile probe chose the zoom (_frame_refusal): when photometa
    answers, its reported levels already say whether the app's frame is one of them. /adminapi/panos is not
    always Google's current number (#74, #121). refetch_panos.py needs it for every pano, because it fetches
    at the frame the STORED file has, which is a scrape-time archive - and part of it was stitched at the
    13312x6656 the app recorded for every pano before SidewalkWebpage evolution 135 (2022-03), so a 16384x8192
    pano can sit on the store as its own top-left 81% (the likely reading of most of the 4.6% store-vs-app
    mismatch measured in reports/2026-08-10-store-coverage.md; Google does not re-serve a pano larger, #217).
    Fetching a 26x13 grid for a pano Google holds at 32x16 does not return a smaller version of the pano; it
    returns the top-left 81% of it, at the stored file's exact dimensions, with no undersized tile and no black
    to give it away. That is a silently cropped panorama saved over a correct one, and this is the only cheap
    thing that catches it.

    **Callers must ask the probe first.** CBK answers a retired pano with a black 200 at every tile, which
    check 3 reads as "smaller". Both callers run this only after the probe found imagery at (0, 0), so a
    retired pano gets the probe's permanent None and never reaches here; asked first, every retired pano
    would become a frame disagreement retried every night.

    Checks 1 and 2 can err only toward ACCEPTANCE: a real tile past the grid that happened to decode to
    all-zero luma would read as blank, and the fetch would proceed. That is why the x probe is taken on the
    grid's middle row rather than on row 0. Row 0 is the zenith cap - the one strip of a panorama where a
    uniformly black real tile is plausible - while the middle row is horizon-adjacent imagery, which never is.
    The y probe lands on ground rows for the same reason. Check 3 sits on that middle row too, so it errs only
    toward REFUSAL - a transient, loud, retried failure - and only for a horizon tile of exact zero luma.
    Every probe is judged by _is_exactly_black. A probe answered with any status but 200 raises
    requests.HTTPError rather than reading as blank (#166 option b): before that, a 403 or 404 with a black
    body was exactly the acceptance case.
    """
    tiles_x, tiles_y = _tile_grid(width, height, zoom)
    with _request_session() as session:
        def tile_is_black(x, y):
            url = tile_url(pano_id, zoom, x, y)
            return _is_exactly_black(Image.open(_get_response(url, session, stream=True)))

        for x, y in ((tiles_x, tiles_y // 2), (0, tiles_y)):
            if not tile_is_black(x, y):
                logging.warning("IMAGEDOWNLOAD: pano %s: tile (%d, %d) past a %dx%d grid at zoom %s has "
                                "imagery; Google serves this pano larger than %dx%d",
                                pano_id, x, y, tiles_x, tiles_y, zoom, width, height)
                return False
        last = (tiles_x - 1, tiles_y // 2)
        if tile_is_black(*last):
            raise FrameDisagreementError(
                "frame disagreement: the app's frame is %dx%d but tile (%d, %d), the last column of its "
                "%dx%d grid at zoom %s, is black, so Google serves this pano smaller"
                % (width, height, last[0], last[1], tiles_x, tiles_y, zoom))
    return True


def fetch_pano_image(pano_id, width, height, zoom):
    """Download every tile of `pano_id`'s grid at `zoom` and stitch one frame at (width, height).

    The seam between "get the imagery" and "decide what to do with it" (#52.1's shape, applied one level
    down). It never writes. In this repo download_single_pano (downloaders/gsv.py) composes it with the
    store's skip check and an atomic save, and refetch_panos.py with its own acceptance gates, so a repair
    pass cannot become a second, drifting stitcher; a consumer composes it the same way.

    Raises rather than returning a verdict for everything transient - a failed tile, a mostly-black stitch,
    a refusal (TilePushbackError, never retried) - so the caller decides what to remember; under the nightly's
    #41 ledger semantics both callers re-attempt those next run rather than remember them.

    @return StitchedPano(image, undersized_tiles, upscaled).
    """
    final_im_dimension = (width, height)

    tiles = _generate_tile_urls(pano_id, width, height, zoom)
    # The fan-out's shared state (#162) - see _TILE_FANOUT for why it rides in a context variable. Reset in a
    # finally, so no later fan-out in this thread can inherit a refusal it did not see.
    fanout = _TileFanOut()
    token = _TILE_FANOUT.set(fanout)
    try:
        results = asyncio.run(_download_tiles(tiles))
    finally:
        _TILE_FANOUT.reset(token)
    ok, failed = _partition_tile_results(tiles, results)
    if failed:
        refusals = [error for _cell, error in failed if isinstance(error, TilePushbackError)]
        if refusals:
            # Google refused us. Raised WITHOUT a log line: the image loop writes the one line a refused
            # pano gets, naming the push-back, and this layer adding its own is how one refusal used to cost
            # 5,120 lines (#162). The counts go on the exception so that one line says what was spent.
            refusal = refusals[0]
            refusal.requested, refusal.total = fanout.requests, len(tiles)
            raise refusal
        # Fail the whole pano: a partial stitch would leave silently-black regions that downstream crops
        # can't detect - exactly the corruption #44 is about. Raise (rather than return failure) so the
        # failure is treated as transient: the tile that timed out today usually exists tomorrow, and under
        # #41's ledger semantics a raised pano is re-attempted next run instead of blacklisted.
        (x, y), first_error = failed[0]
        logging.error("IMAGEDOWNLOAD: pano %s: %d/%d tiles failed after %d tile retries; first failure: "
                      "tile (%d, %d): %r", pano_id, len(failed), len(tiles), fanout.retries, x, y, first_error)
        raise first_error
    if fanout.retries:
        # backoff no longer logs its retries, so this is the one place a pano that needed them says so - the
        # earliest sign of trouble that is not yet a failure.
        logging.info("IMAGEDOWNLOAD: pano %s: stitched after %d tile retries", pano_id, fanout.retries)

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


# urllib3's ResponseError.SPECIFIC_ERROR, 'too many {status_code} error responses', as it reads inside the
# RetryError requests raises when the zoom probe's retry policy is exhausted.
_RETRY_STATUS_RE = re.compile(r'too many (\d{3}) error responses')


def pushback_reason(exc):
    """'HTTP 429' / 'HTTP 403' / 'interstitial' when `exc` is Google refusing this host; None otherwise (#162).

    What the image loop's push-back breaker counts, so the line is drawn at GOOGLE'S OWN refusal and never at
    this box's network: a timeout, a reset, a DNS failure is weather. Three shapes qualify:

    * TilePushbackError - a tile answered 429/403 or landed on an interstitial, whatever status it wore.
    * requests' RetryError from the zoom probe, whose urllib3 policy retries 429 and 5xx and gives up with
      'too many N error responses'. It counts ONLY when N is in TILE_PUSHBACK_STATUSES - in practice 429,
      since 403 is not in the forcelist and so never exhausts it (the 403 half is for the day it is). A 5xx
      storm exhausting the policy is Google being ill, not Google refusing us - and a trip writes the
      fleet-wide block latch, so reading an outage as a refusal would stand every city's depth phase down for
      six hours. The depth loop (#177) and the image phase's photometa arm (#74/#172) both split on this
      one predicate. A RetryError whose status cannot be read is None for the same reason. One that gave up
      ON Google's interstitial (its message names the /sorry/ path or the consent host) is 'interstitial'
      whatever its status: the landing is the refusal, and a 503 captcha page is not an outage.
    * requests' HTTPError from the zoom probe, which since #166 raises for any status but 200 and carries
      the Response. 403 is not in the retry policy's forcelist, so THIS is the shape a probe 403 arrives in:
      it counts when the status is in TILE_PUSHBACK_STATUSES, or when the landing or any redirect hop
      carries an interstitial marker. Any other status (404, 410, a 5xx the adapter handed back) is None.

    Everything else - a tile 404 or 5xx, a mostly-black stitch, an OSError from the store - is None.
    """
    if isinstance(exc, TilePushbackError):
        return exc.reason
    if isinstance(exc, requests.exceptions.RetryError):
        match = _RETRY_STATUS_RE.search(str(exc))
        if match and int(match.group(1)) in TILE_PUSHBACK_STATUSES:
            return 'HTTP %s' % (match.group(1),)
        if _interstitial_url([str(exc)]) is not None:
            return 'interstitial'
        return None
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        if response is None:
            return None
        if response.status_code in TILE_PUSHBACK_STATUSES:
            return 'HTTP %d' % (response.status_code,)
        if _interstitial_url(_requests_response_urls(response)) is not None:
            return 'interstitial'
    return None


def _interstitial_url(urls):
    """The first of `urls` that carries one of Google's interstitial markers, or None. Read at call time."""
    for url in urls:
        if any(marker in url for marker in _BLOCK_URL_MARKERS):
            return url
    return None


def _aiohttp_error_urls(e):
    """Every URL a tile's ClientResponseError can name: the landing, each redirect hop, and each hop's
    Location. Defensive about shape, because a test double (or a Content-Type failure) may carry less."""
    urls = [str(getattr(getattr(e, 'request_info', None), 'real_url', '') or '')]
    for hop in getattr(e, 'history', None) or ():
        urls.append(str(getattr(hop, 'url', '') or ''))
        urls.append(str((getattr(hop, 'headers', None) or {}).get('Location', '') or ''))
    return urls


def _requests_response_urls(response):
    """The same for a requests Response: the landing, each redirect hop, and each hop's Location."""
    urls = [str(response.url or '')]
    for hop in response.history or ():
        urls.append(str(hop.url or ''))
        urls.append(str(hop.headers.get('Location', '') or ''))
    return urls
