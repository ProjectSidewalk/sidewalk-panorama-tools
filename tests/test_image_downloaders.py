"""Tests for the image downloaders' half of the #41 ledger contract.

`downloaders.download_pano` promises the image loop two things: `DownloadResult.failure` is a PERMANENT
property of the pano (it is ledgered `downloaded=0` and never re-attempted), and every transient condition
RAISES instead (no row, retried next run). These tests pin both halves at the source, plus the atomic-write
guarantee the contract depends on — because an existing `.jpg` is itself the resume marker, a download that
dies mid-write must not leave a truncated file for the next run to report as a completed success.

Network-free: the Mapillary tests install a fake session, and the GSV tests stub the tile fetches.
"""

import io
import logging
import os
import sys

import pytest
import requests
from PIL import Image

import downloaders
from downloaders.common import DownloadResult, atomic_output_path

from conftest import posix_only

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

MAPILLARY_PANO = {'pano_id': '123456789012345', 'source': 'mapillary'}
HEALTHY_MAPILLARY_METADATA = {'id': MAPILLARY_PANO['pano_id'], 'thumb_original_url': 'https://cdn/x.jpg'}

# Every response graph.mapillary.com gave `GET /<image id>?fields=thumb_original_url` under bad auth, measured
# 2026-09-05 for #99, as (HTTP status, JSON body). None is a 200, so raise_for_status() already refuses all
# of them; what is worth pinning is the ENVELOPE. It is Meta's Graph-style {"error": {...}}: its `type`
# varies (MLYApiException without a header, OAuthException with a bad one) while its `code` is 190, Meta's
# invalid-token code, throughout. A real expiry answered 400 with the same envelope (#98, 2026-09-01). The one
# auth failure not measurable without a live token - a token lacking the needed scope - is the case the
# envelope check exists for.
OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05 = {
    'no Authorization header': (500, {
        'error': {'message': 'Invalid OAuth 2.0 Access Token', 'type': 'MLYApiException', 'code': 190,
                  'error_data': {}, 'fbtrace_id': 'AVClUCSPotDOO7MhX8oji1w'}}),
    'malformed token (MLY|bogus)': (401, {
        'error': {'message': 'Failed to decode', 'type': 'OAuthException', 'code': 190,
                  'fbtrace_id': 'A8OIotiwZ1Hh3WMPzG6jHGd'}}),
    'well-formed token that was never issued': (401, {
        'error': {'message': 'Error validating application', 'type': 'OAuthException', 'code': 190,
                  'fbtrace_id': 'AAf3CgtCoTPgu45pnemNL6U'}}),
    'that token as an access_token query param instead': (400, {
        'error': {'message': 'Error validating application', 'type': 'OAuthException', 'code': 190,
                  'fbtrace_id': 'A-54WSA6X3o82B7YOvzkWIz'}}),
}
# Measured 2026-09-06 with the production token, against two ids Mapillary does not have. NOT a 404: the
# "does not exist" answer is a 400, and its message conflates three conditions by Meta's design.
OBSERVED_MAPILLARY_NOT_FOUND_2026_09_06 = {
    'id 1': (400, {
        'error': {'message': "Unsupported get request. Object with ID '1' does not exist, cannot be loaded due "
                             "to missing permissions, or does not support this operation",
                  'type': 'MLYApiException', 'code': 100, 'error_subcode': 33,
                  'fbtrace_id': 'AE2tEGzm0HlOCoTp0HVzLMY'}}),
    'id 999999999999999': (400, {
        'error': {'message': "Unsupported get request. Object with ID '999999999999999' does not exist, cannot "
                             "be loaded due to missing permissions, or does not support this operation",
                  'type': 'MLYApiException', 'code': 100, 'error_subcode': 33,
                  'fbtrace_id': 'Acdn89a_GGBfAGUP5ew-hl8'}}),
}
GSV_PANO = {'pano_id': 'gsvPanoIdAAAAAAAAAAAAA', 'source': 'gsv', 'width': 512, 'height': 512}


def jpeg_bytes(shade):
    buf = io.BytesIO()
    Image.new('RGB', (512, 512), (shade, shade, shade)).save(buf, 'jpeg')
    return buf.getvalue()


class TestAtomicOutputPath:
    def test_success_renames_into_place(self, tmp_path):
        final = str(tmp_path / 'pano.jpg')

        with atomic_output_path(final) as tmp:
            assert tmp == final + '.part'
            with open(tmp, 'wb') as f:
                f.write(b'payload')

        assert open(final, 'rb').read() == b'payload'
        assert not os.path.exists(final + '.part')

    def test_failure_leaves_neither_the_final_file_nor_the_part(self, tmp_path):
        final = str(tmp_path / 'pano.jpg')

        with pytest.raises(OSError):
            with atomic_output_path(final) as tmp:
                with open(tmp, 'wb') as f:
                    f.write(b'half a jpeg')
                raise OSError(28, 'No space left on device')

        assert not os.path.exists(final), "a truncated file here would be read as a completed download"
        assert not os.path.exists(final + '.part'), "debris would otherwise accumulate forever"

    def test_a_killed_run_cleans_up_too(self, tmp_path):
        """SIGTERM is translated to SystemExit, which is a BaseException - the cleanup must still fire."""
        final = str(tmp_path / 'pano.jpg')

        with pytest.raises(SystemExit):
            with atomic_output_path(final) as tmp:
                open(tmp, 'wb').write(b'partial')
                raise SystemExit(143)

        assert not os.path.exists(final)
        assert not os.path.exists(final + '.part')

    def test_a_part_file_that_was_never_created_does_not_mask_the_real_error(self, tmp_path):
        """Failing before the first byte is written is the common case, not an exotic one: a 404, an expired
        signed URL, or a full store all abort before the open. The cleanup's own os.remove then fails, and
        if that FileNotFoundError escaped it would replace the real cause in scrape.log with a message about
        a temp file - sending whoever reads it to look in entirely the wrong place.
        """
        final = str(tmp_path / 'pano.jpg')

        with pytest.raises(RuntimeError, match='the actual cause'):
            with atomic_output_path(final):
                raise RuntimeError('the actual cause')

        assert not os.path.exists(final)
        assert not os.path.exists(final + '.part')

    @posix_only
    def test_the_renamed_file_is_group_writable(self, tmp_path):
        final = str(tmp_path / 'pano.jpg')

        with atomic_output_path(final) as tmp:
            open(tmp, 'wb').write(b'payload')

        assert os.stat(final).st_mode & 0o777 == 0o664


class TestDownloadResultIsARealEnum:
    """#52 item 2. `DownloadResult` was a hand-rolled class whose members were tuple indices, which cost
    three things the stdlib gives away: `skipped` was 0 and therefore FALSY (so `if result:` anywhere
    misclassifies it), a typo'd member raised `ValueError` from inside `__getattr__` rather than
    `AttributeError` (so `hasattr` RAISED instead of returning False, and the message never named the
    attribute), and members printed into logs as bare ints."""

    def test_no_member_is_falsy(self):
        """The one that could have silently misclassified a pano: 'skipped' was index 0."""
        for member in DownloadResult:
            assert member, f"{member!r} is falsy; `if result:` would misread it"

    def test_a_typo_raises_attribute_error_naming_the_attribute(self):
        with pytest.raises(AttributeError, match='sucess'):
            DownloadResult.sucess

    def test_hasattr_answers_instead_of_raising(self):
        assert hasattr(DownloadResult, 'success')
        assert not hasattr(DownloadResult, 'sucess')

    def test_members_identify_themselves_in_logs(self):
        """A log line carrying a bare 2 says nothing; the point of the enum is that the name travels."""
        assert 'fallback_success' in repr(DownloadResult.fallback_success)

    def test_the_four_outcomes_are_distinct(self):
        members = [DownloadResult.skipped, DownloadResult.success,
                   DownloadResult.fallback_success, DownloadResult.failure]
        assert len(set(members)) == 4


class FakeResponse:
    # requests exposes these two on every response and panoramax reads them, because it fetches the hd href
    # with allow_redirects=False: a redirect off a URL the catalog published is refused rather than followed.
    REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

    def __init__(self, status_code=200, payload=None, body=None, chunks=None):
        self.status_code = status_code
        self._payload = payload
        self.text = body or ''
        self._chunks = chunks or []

    @property
    def is_redirect(self):
        return self.status_code in self.REDIRECT_STATUSES

    @property
    def is_permanent_redirect(self):
        return self.status_code in (301, 308)

    def json(self):
        if self._payload is None:
            raise ValueError('not JSON')
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('%s' % self.status_code, response=self)

    def iter_content(self, chunk_size=None):
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class FakeSession:
    """Returns queued responses in order; records the whole call, headers merged the way a real
    requests.Session merges them.

    `calls` carries the kwargs, because WHERE a secret rides - params or headers - is the thing
    TestTheTokenStaysOutOfURLs has to see, and the url argument alone cannot show it.

    `headers` mirrors requests.Session.headers: a real session merges its own session-level defaults with
    whatever headers a call passes, request headers winning on conflict. Without this, the natural DRY-up of
    download_single_pano's two identical-shaped `session.get(...)` calls - `_session()` setting
    `session.headers['Authorization']` once instead of passing `headers=` per call - would send the token to
    the CDN on every image request while TestTheTokenStaysOutOfURLs kept passing, because nothing here would
    ever see a session-level default; only its sibling's `kwargs['headers']` assertion happened to catch it,
    incidentally (2026-09 PR #100 review, finding 6).
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        merged_headers = {**self.headers, **(kwargs.get('headers') or {})}
        if merged_headers:
            kwargs = {**kwargs, 'headers': merged_headers}
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.fixture
def mapillary_token(monkeypatch):
    monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, 'test-token')


class TestMapillaryPermanentVerdicts:
    """These are properties of the PANO, so they ledger downloaded=0 and are never re-attempted."""

    def test_unknown_image_id_is_permanent(self, monkeypatch, tmp_path, mapillary_token):
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=404)))

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.failure

    @pytest.mark.parametrize('response', [
        FakeResponse(status_code=404, payload=None, body='<html>404</html>'),
        FakeResponse(status_code=404, payload=OBSERVED_MAPILLARY_NOT_FOUND_2026_09_06['id 1'][1]),
        FakeResponse(status_code=404, payload={}),
    ], ids=['not JSON', 'a does-not-exist envelope', 'empty object'])
    def test_a_404_stays_permanent_unless_its_body_carries_the_auth_signature(self, monkeypatch, tmp_path,
                                                                             mapillary_token, response):
        """A Meta-style API puts an envelope on every error status, a genuine "does not exist" included, so an
        envelope on a 404 is not a counter-signal by itself: refusing any envelope here would stop a retired
        image from ever ledgering and re-request it nightly forever. Only the measured auth signature
        overrides the status - see TestMapillaryTransientConditions."""
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: FakeSession(response))

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.failure

    @pytest.mark.parametrize('body', [{'id': MAPILLARY_PANO['pano_id']},
                                      {'id': MAPILLARY_PANO['pano_id'], 'thumb_original_url': ''}],
                             ids=['field absent', 'field empty'])
    def test_missing_original_rendition_is_permanent(self, monkeypatch, tmp_path, mapillary_token, body):
        """Mapillary names the image we asked about and publishes no original-resolution rendition for it.
        The body used to be a bare {} here; since #99 the verdict needs the record to name the image, and a
        {} raises instead - see TestMapillaryErrorEnvelopes."""
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(payload=body)))

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.failure


class TestMapillaryTransientConditions:
    """These are properties of the RUN. Returning failure would ledger them permanently - an expired token
    for one night would blacklist every Mapillary pano in the city - so they must raise instead (#41)."""

    # Only 404 is permanent; every other non-200 must raise. Parametrised over a SPREAD rather than the
    # statuses we happen to have seen, because the last gap here was exactly that kind of omission. The list
    # was [401, 403, 500] on the reasoning that those are what a bad token produces; when richmond-va's token
    # stopped working, graph.mapillary.com answered 400 - the one status not covered. The code deployed then
    # predated this contract and ledgered any non-200 as permanent, so it wrote off 162 panos as "Mapillary
    # has no image" (161 of them wrongly - the 162nd was an id that had since left the corpus). The city sat
    # at 21 of 182 until pano_id_log.csv was hand-edited on the store (2026-09-01), because replacing the
    # token recovered nothing on its own. Pinning the rule rather than the observed instances is what stops
    # the next unanticipated status - 402, 410, 451 - from being another gap.
    #
    # 500 is a contract case, not a wire case: _session()'s status_forcelist retries 429/500/502/503/504, so
    # in production those surface as RetryError rather than HTTPError. FakeSession bypasses the adapter.
    # 400/401/403 are the ones that reach raise_for_status() for real.
    @pytest.mark.parametrize('status', [400, 401, 402, 403, 410, 422, 451, 500])
    def test_metadata_http_errors_raise(self, monkeypatch, tmp_path, mapillary_token, status):
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=status)))

        with pytest.raises(requests.HTTPError):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

    @pytest.mark.parametrize('condition', sorted(OBSERVED_MAPILLARY_NOT_FOUND_2026_09_06))
    def test_mapillarys_measured_does_not_exist_answer_raises_and_is_not_ledgered(
            self, monkeypatch, tmp_path, mapillary_token, condition):
        """Measured 2026-09-06: an id Mapillary does not have gets a 400, not a 404, with code 100 / subcode 33
        and a message that itself conflates "does not exist" with "missing permissions". A token lacking
        scope would produce that body for every pano - the 2026-09-01 incident by another route - so it is a
        condition of the run until a run-level breaker bounds the damage of treating it as a verdict. The
        cost of this decision is one request per retired image per night; the alternative was 9,229 false
        rows. Pinned so that changing it is a decision, not a drift."""
        status, envelope = OBSERVED_MAPILLARY_NOT_FOUND_2026_09_06[condition]
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=status, payload=envelope)))

        with pytest.raises(requests.HTTPError):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert os.listdir(tmp_path / MAPILLARY_PANO['pano_id'][:2]) == []

    @pytest.mark.parametrize('condition', sorted(OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05))
    def test_a_404_carrying_the_auth_signature_raises_instead_of_ledgering(self, monkeypatch, tmp_path,
                                                                          mapillary_token, condition):
        """404 was the one status left where an envelope produced a permanent row: refused at the status
        everywhere else, refused at the body on a 200, trusted unread here. A token that cannot see the image
        is a condition of the run at any status. Unmeasured - the real 404 body is on the pre-merge check
        list - so keyed on the signature every measured auth failure carried, not on any envelope."""
        _, envelope = OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05[condition]
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=404, payload=envelope)))

        with pytest.raises(downloaders.mapillary.MapillaryErrorResponse) as excinfo:
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert '404' in str(excinfo.value) and '190' in str(excinfo.value)
        assert os.listdir(tmp_path / MAPILLARY_PANO['pano_id'][:2]) == []

    def test_the_auth_signature_is_either_measured_half_and_nothing_wider(self):
        """Both halves are measured: code 190 arrived under two different types, and OAuthException is where
        Meta files every token problem. A does-not-exist envelope, a non-object error, or no envelope at all
        must stay on the permanent side, or a retired image is re-requested nightly forever."""
        is_auth = downloaders.mapillary.is_auth_envelope

        assert is_auth({'error': {'type': 'OAuthException', 'code': 102, 'message': 'Session expired'}})
        assert is_auth({'error': {'type': 'MLYApiException', 'code': '190'}})
        for condition, (_, envelope) in OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05.items():
            assert is_auth(envelope), condition

        for condition, (_, envelope) in OBSERVED_MAPILLARY_NOT_FOUND_2026_09_06.items():
            assert not is_auth(envelope), condition
        assert not is_auth({'error': {'type': 'GraphMethodException', 'code': 100,
                                      'message': "Object with ID '1' does not exist"}})
        assert not is_auth({'error': 'Invalid token'})
        assert not is_auth({'id': '123456789012345'})
        assert not is_auth({})
        assert not is_auth([])
        assert not is_auth(None)

    def test_a_non_json_metadata_body_raises(self, monkeypatch, tmp_path, mapillary_token):
        """A proxy error page or a body truncated in flight - not a verdict on the pano."""
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(payload=None, body='<html>502</html>')))

        with pytest.raises(ValueError):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

    def test_an_expired_signed_image_url_raises(self, monkeypatch, tmp_path, mapillary_token):
        session = FakeSession(FakeResponse(payload=HEALTHY_MAPILLARY_METADATA),
                              FakeResponse(status_code=403))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        with pytest.raises(requests.HTTPError):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

    def test_a_missing_token_raises_rather_than_blacklisting_the_corpus(self, monkeypatch, tmp_path):
        monkeypatch.delenv(downloaders.mapillary.TOKEN_ENV_VAR, raising=False)

        with pytest.raises(RuntimeError, match=downloaders.mapillary.TOKEN_ENV_VAR):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

    def test_a_dying_stream_leaves_no_file_to_mistake_for_success(self, monkeypatch, tmp_path,
                                                                  mapillary_token):
        """The regression this guards: with no ledger row written (#41), the NEXT run reaches the
        os.path.isfile() check - so a truncated .jpg left here would be reported as a completed download."""
        session = FakeSession(
            FakeResponse(payload=HEALTHY_MAPILLARY_METADATA),
            FakeResponse(chunks=[b'\xff\xd8\xff\xe0 partial',
                                 requests.ConnectionError('connection reset mid-stream')]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        with pytest.raises(requests.ConnectionError):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert os.listdir(tmp_path / '12') == []

    def test_a_healthy_download_still_lands(self, monkeypatch, tmp_path, mapillary_token):
        session = FakeSession(FakeResponse(payload=HEALTHY_MAPILLARY_METADATA),
                              FakeResponse(chunks=[jpeg_bytes(120)]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.success
        assert os.listdir(tmp_path / '12') == ['%s.jpg' % MAPILLARY_PANO['pano_id']]

    def test_an_integer_id_in_the_record_still_names_the_image(self, monkeypatch, tmp_path, mapillary_token):
        """The comparison is str() on both sides. The API quotes the id (live check 2026-09-06) and every other
        body in this file does too, so a bare != passed the whole battery - and would raise for every
        Mapillary pano forever the day the quotes go, with a scrape.log line that looks like a match (#46)."""
        body = {'id': int(MAPILLARY_PANO['pano_id']),
                'thumb_original_url': HEALTHY_MAPILLARY_METADATA['thumb_original_url']}
        session = FakeSession(FakeResponse(payload=body), FakeResponse(chunks=[jpeg_bytes(120)]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.success

    def test_a_200_image_body_that_is_not_a_jpeg_is_refused_before_it_can_become_the_resume_marker(
            self, monkeypatch, tmp_path, mapillary_token):
        """The mirror of the metadata check, on the success side, where it matters more: a saved .jpg IS the
        resume marker, so an HTML error page from the signed-URL edge would be permanent with no ledger row
        to edit, and an error per label in the cropper every night."""
        session = FakeSession(FakeResponse(payload=HEALTHY_MAPILLARY_METADATA),
                              FakeResponse(chunks=[b'<!DOCTYPE html><html><body>403 Forbidden</body></html>']))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        with pytest.raises(downloaders.mapillary.MapillaryErrorResponse):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert os.listdir(tmp_path / '12') == []


class TestTheTokenIsNormalisedOnRead:
    """`_token()` strips the raw environment value once, so is_token_set() and the header build can never
    disagree about what counts as "set" (2026-09 PR #100 review, findings 1 and 12).
    """

    def test_a_trailing_newline_is_stripped_before_it_reaches_the_header(self, monkeypatch, tmp_path):
        """Left unstripped, this exact value makes requests.Session.get() raise InvalidHeader, whose
        message embeds the header value VERBATIM - the malformed-token leak finding 1 exists to close."""
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, 'test-token\n')
        # The shared healthy body, not a bare {'thumb_original_url': ...}: since #99 a metadata body has to
        # name the image it describes, so a hand-rolled one without `id` is refused before the header this
        # test is about can be asserted on.
        session = FakeSession(FakeResponse(payload=HEALTHY_MAPILLARY_METADATA),
                              FakeResponse(chunks=[jpeg_bytes(120)]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert session.calls[0][1]['headers'] == {'Authorization': 'OAuth test-token'}

    def test_a_whitespace_only_token_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, '   ')

        assert downloaders.mapillary.is_token_set() is False

    def test_a_whitespace_only_token_raises_rather_than_401ing_every_pano_all_night(self, monkeypatch,
                                                                                    tmp_path):
        """Finding 12: bool(' ') is True, so without the strip filter_supported_sources would keep the
        city's Mapillary panos in the run and every one would 401 nightly forever with nothing permanent
        ever ledgered."""
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, '   ')

        with pytest.raises(RuntimeError, match=downloaders.mapillary.TOKEN_ENV_VAR):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)


class TestMapillaryErrorEnvelopes:
    """#99. A permanent verdict has to rest on Mapillary AFFIRMING it knows the image, never on a field being
    absent from whatever came back.

    The 2026-09-01 incident wrote 161 false downloaded=0 rows through a 400, and every auth failure measured
    since (OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05) is likewise a non-200 that already raises. What was
    still open was the other branch: a 200 whose body lacked thumb_original_url was read as "no rendition
    exists" and ledgered. Any condition that ever answers 200 with an error envelope, or with a body that is
    not the record asked for, would therefore write off every pano attempted that night - and richmond-va is
    9,229 panos on one token. None of these bodies is a property of the pano, so all of them raise (#41).
    """

    @pytest.mark.parametrize('condition', sorted(OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05))
    def test_an_error_envelope_raises_even_on_a_200(self, monkeypatch, tmp_path, mapillary_token, condition):
        _, envelope = OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05[condition]
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=200, payload=envelope)))

        with pytest.raises(downloaders.mapillary.MapillaryErrorResponse) as excinfo:
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        # scrape.log gets str(e), so the line has to say what Mapillary said: type and code are what a
        # reader would search the API docs for.
        assert envelope['error']['type'] in str(excinfo.value)
        assert '190' in str(excinfo.value)

    @pytest.mark.parametrize('condition', sorted(OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05))
    def test_every_measured_auth_failure_is_refused_as_measured(self, monkeypatch, tmp_path, mapillary_token,
                                                                condition):
        """As measured, each one is a non-200, refused at the status before the body is read. The test above
        is what covers the shape nobody has measured."""
        status, envelope = OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05[condition]
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=status, payload=envelope)))

        with pytest.raises(requests.HTTPError):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

    @pytest.mark.parametrize('body', [{}, {'thumb_original_url': None}, {'id': '999999999999999'},
                                      {'data': []}], ids=['empty', 'null url, no id', 'another image', 'a list'])
    def test_a_body_that_does_not_name_this_image_cannot_ledger_it(self, monkeypatch, tmp_path,
                                                                   mapillary_token, body):
        """An empty {} used to be THE permanent case. "No rendition" now needs the record to name the image
        it describes; a body that names none, or a different one, is not evidence about this pano."""
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=200, payload=body)))

        with pytest.raises(downloaders.mapillary.MapillaryErrorResponse) as excinfo:
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert MAPILLARY_PANO['pano_id'] in str(excinfo.value)

    def test_an_envelope_whose_error_is_not_an_object_still_raises_and_still_says_why(
            self, monkeypatch, tmp_path, mapillary_token):
        """The envelope's shape is Mapillary's to change. `error` being present is the signal; what is inside
        it only decides what scrape.log says."""
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=200, payload={'error': 'Invalid token'})))

        with pytest.raises(downloaders.mapillary.MapillaryErrorResponse) as excinfo:
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert 'Invalid token' in str(excinfo.value)

    def test_an_envelope_wins_over_a_url_in_the_same_body(self, monkeypatch, tmp_path, mapillary_token):
        """`error` is checked first, not only when the URL is absent. Every measured envelope lacks the URL, so
        an implementation that follows any URL it finds passed the whole battery - and would store whatever
        that URL serves as the pano, out of a body that is saying the token is bad."""
        _, envelope = OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05['malformed token (MLY|bogus)']
        body = dict(envelope, id=MAPILLARY_PANO['pano_id'], thumb_original_url='https://cdn.example/x.jpg')
        session = FakeSession(FakeResponse(status_code=200, payload=body),
                              FakeResponse(chunks=[jpeg_bytes(120)]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        with pytest.raises(downloaders.mapillary.MapillaryErrorResponse):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert [url for url, _ in session.calls] == ['%s/%s' % (downloaders.mapillary.GRAPH_API_BASE, MAPILLARY_PANO['pano_id'])], \
            'the URL in an error body must not be fetched'

    @pytest.mark.parametrize('payload', [
        {'error': {'type': 'OAuthException', 'code': 190, 'message': 'M' * 100_000}},
        {'error': {'type': 'T' * 100_000, 'code': 190, 'message': 'Invalid OAuth 2.0 Access Token'}},
        {'error': {'type': 'OAuthException', 'code': 'C' * 100_000, 'message': 'Invalid OAuth 2.0 Access Token'}},
        {'id': '9' * 100_000},
    ], ids=['message', 'type', 'code', 'id'])
    def test_what_reaches_scrape_log_is_bounded(self, payload):
        """DownloadRunner logs str(e) per raised pano into a 10 MB x 3 rotation. 9,229 panos times an uncapped
        HTML blob is ~900 MB into a 40 MB window: the night's own diagnosis rotates away.

        EVERY field of the envelope, not just `message`. The first cap covered `message` and `id` and left
        `type` and `code` formatted with a bare %s - measured at 100,080 characters for one pano - even
        though all four arrive by the same route, an untrusted JSON body. A `type` carrying a class name or
        an HTML fragment from a proxy is if anything likelier than a 100 KB `message` from Mapillary itself.
        """
        with pytest.raises(downloaders.mapillary.MapillaryErrorResponse) as excinfo:
            downloaders.mapillary.original_rendition_url(payload, MAPILLARY_PANO['pano_id'])

        assert len(str(excinfo.value)) < 400

    @pytest.mark.parametrize('body', [[], 'a string', 42, True], ids=['list', 'string', 'number', 'bool'])
    def test_a_json_body_that_is_not_an_object_raises(self, monkeypatch, tmp_path, mapillary_token, body):
        """Valid JSON is not the same as the record asked for: a proxy or a CDN error page can be either."""
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=200, payload=body)))

        with pytest.raises(downloaders.mapillary.MapillaryErrorResponse):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

    def test_a_refused_body_leaves_nothing_on_disk(self, monkeypatch, tmp_path, mapillary_token):
        """No image was fetched, so there is nothing for the next run to mistake for a completed download."""
        _, envelope = OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05['malformed token (MLY|bogus)']
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=200, payload=envelope)))

        with pytest.raises(downloaders.mapillary.MapillaryErrorResponse):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert os.listdir(tmp_path / MAPILLARY_PANO['pano_id'][:2]) == []

    def test_the_error_is_transient_under_the_dispatcher_contract(self):
        """download_panorama_images treats any Exception as transient; a BaseException would escape that
        contract, and an HTTPError would read in scrape.log as a status failure it is not."""
        assert issubclass(downloaders.mapillary.MapillaryErrorResponse, Exception)
        assert not issubclass(downloaders.mapillary.MapillaryErrorResponse, requests.HTTPError)


class TestTheTokenStaysOutOfURLs:
    """A token in the query string is a token in the logs, and these logs are not private.

    `scrape.log` is written to the SHARED pano store, and DownloadRunner logs `str(e)` for every failed
    pano. requests puts the full URL into an HTTPError's message, so `params={'access_token': ...}` writes
    the live secret into that file in cleartext. Not hypothetical: a production city's `scrape.log`
    carried a live token after the 2026-09-01 run whose own token had expired. Header auth is the form Graph API
    v4 documents, and was confirmed against the live API (HTTP 200, thumb_original_url returned) before
    this change landed.
    """

    @staticmethod
    def _url_on_the_wire(call):
        """Build the URL through requests' OWN machinery, so this test cannot pass by construction.

        Asserting on the bare `url` argument would be vacuous - it never holds the token either way,
        since params are merged in at prepare() time. Preparing the request is precisely what makes
        putting access_token back into params fail here.
        """
        url, kwargs = call
        return requests.Request('GET', url, params=kwargs.get('params')).prepare().url

    def _healthy_session(self, monkeypatch):
        session = FakeSession(FakeResponse(payload=HEALTHY_MAPILLARY_METADATA),
                              FakeResponse(chunks=[jpeg_bytes(120)]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)
        return session

    def test_the_metadata_url_never_carries_the_token(self, monkeypatch, tmp_path, mapillary_token):
        session = self._healthy_session(monkeypatch)

        downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        wire_url = self._url_on_the_wire(session.calls[0])
        assert 'test-token' not in wire_url
        # The request still has to ask for the fields it needs, or "no token in the URL" is trivially
        # satisfiable by sending no params at all. `id` is asked for so that a "no rendition" verdict can
        # require the record to name the image it describes (#99); requests percent-encodes the comma.
        assert 'fields=id%2Cthumb_original_url' in wire_url

    def test_the_token_is_still_sent_as_an_oauth_header(self, monkeypatch, tmp_path, mapillary_token):
        """The other half: dropping the token entirely would also pass the test above, and would take
        every Mapillary pano in the city down with a 401 the next night."""
        session = self._healthy_session(monkeypatch)

        downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert session.calls[0][1]['headers'] == {'Authorization': 'OAuth test-token'}

    def test_the_signed_image_url_is_fetched_with_no_credentials_of_ours(self, monkeypatch, tmp_path,
                                                                        mapillary_token):
        """The CDN URL is already signed and short-lived; sending our token to it would widen the blast
        radius of a leak from one metadata URL to every image request."""
        session = self._healthy_session(monkeypatch)

        downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert 'test-token' not in str(session.calls[1])


class TestFakeSessionModelsSessionLevelHeaderDefaults:
    """FakeSession.headers itself (2026-09 PR #100 review, finding 6) - not download_single_pano, which
    never sets it today. This pins the fake's own merge behaviour so a later change to FakeSession.get()
    can't silently drop it and defang TestTheTokenStaysOutOfURLs against the DRY-up finding 6 describes.
    """

    def test_a_session_level_default_is_merged_into_the_call(self):
        session = FakeSession(FakeResponse(status_code=200))
        session.headers['Authorization'] = 'OAuth session-level-token'

        session.get('https://example.invalid')

        assert session.calls[0][1]['headers'] == {'Authorization': 'OAuth session-level-token'}

    def test_a_per_call_header_overrides_the_session_default(self):
        session = FakeSession(FakeResponse(status_code=200))
        session.headers['Authorization'] = 'OAuth session-level-token'

        session.get('https://example.invalid', headers={'Authorization': 'OAuth per-call-token'})

        assert session.calls[0][1]['headers'] == {'Authorization': 'OAuth per-call-token'}


class TestTokenRedactionFilter:
    """`downloaders.mapillary.TokenRedactionFilter`, wired onto DownloadRunner's one log handler in
    configure_logging (2026-09 PR #100 review, finding 2). Exercised here directly against LogRecord, not
    through caplog: caplog captures records via its OWN handler, so a filter added only to the production
    RotatingFileHandler would never touch what caplog sees, and testing through caplog would silently prove
    nothing about this filter. tests/test_download_runner.py's
    test_configure_logging_redacts_the_token_from_scrape_log covers the wiring, by reading scrape.log itself
    back after a real configure_logging() call - this class covers the filter's own behaviour.
    """

    @staticmethod
    def _record(msg, args=()):
        return logging.LogRecord('test', logging.ERROR, __file__, 1, msg, args, None)

    def test_the_token_is_redacted_out_of_a_bare_message(self, monkeypatch):
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, 'secret123')
        record = self._record('token leaked: secret123')

        assert downloaders.mapillary.TokenRedactionFilter().filter(record) is True
        assert record.getMessage() == 'token leaked: <redacted>'

    def test_the_token_is_redacted_out_of_a_percent_style_argument(self, monkeypatch):
        """The DownloadRunner.py:376 shape this exists for: logging.error('... %s', pano_id, str(e))."""
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, 'secret123')
        record = self._record('Failed to download pano %s due to error %s',
                              args=('123456789012345', "InvalidHeader: ...'OAuth secret123\\n'"))

        assert downloaders.mapillary.TokenRedactionFilter().filter(record) is True
        assert 'secret123' not in record.getMessage()
        assert record.args[0] == '123456789012345'  # the non-secret argument is untouched

    def test_a_dict_style_argument_is_also_redacted(self, monkeypatch):
        """logging accepts a single Mapping argument for %(name)s-style formatting; LogRecord unwraps a
        one-element args tuple holding a Mapping into that Mapping directly, so record.args isn't a tuple
        at all in this case - the filter has to handle both shapes."""
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, 'secret123')
        record = self._record('token: %(token)s', args=({'token': 'secret123'},))

        assert downloaders.mapillary.TokenRedactionFilter().filter(record) is True
        assert record.getMessage() == 'token: <redacted>'

    def test_no_token_set_is_a_no_op(self, monkeypatch):
        monkeypatch.delenv(downloaders.mapillary.TOKEN_ENV_VAR, raising=False)
        record = self._record('nothing to redact here')

        assert downloaders.mapillary.TokenRedactionFilter().filter(record) is True
        assert record.getMessage() == 'nothing to redact here'

    def test_a_whitespace_only_token_does_not_corrupt_every_message(self, monkeypatch):
        """The other half of the .strip() fix (finding 12): '' must never reach str.replace, or it would
        insert '<redacted>' between every character of every log line in the process."""
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, '   ')
        record = self._record('an ordinary message')

        assert downloaders.mapillary.TokenRedactionFilter().filter(record) is True
        assert record.getMessage() == 'an ordinary message'


class TestGsvAtomicSave:
    """The GSV path stitches tiles in memory and writes one JPEG at the end; that write is where a full store
    or a killed container leaves a stub behind."""

    @pytest.fixture
    def stitchable(self, monkeypatch):
        """Stub the two zoom probes and the tile fan-out so the stitch reaches its save with no network.

        The tile is encoded once, up front: a test that patches Image.Image.save to fail would otherwise
        break this helper too.
        """
        tile = jpeg_bytes(120)
        monkeypatch.setattr(downloaders.gsv, '_get_response',
                            lambda url, session, stream=False: io.BytesIO(tile))

        # The fan-out hands back (x, y, jpeg_bytes) per tile, one entry per requested grid position
        # (#44/#45 replaced the old ['<x> <y>', bytes] pairs). Stubbing _download_tiles rather than
        # asyncio.run keeps this at the module's own seam.
        async def fake_download_tiles(tiles):
            return [(x, y, tile) for x, y, _url in tiles]

        monkeypatch.setattr(downloaders.gsv, '_download_tiles', fake_download_tiles)

    def test_a_healthy_pano_is_written(self, tmp_path, stitchable):
        assert downloaders.gsv.download_single_pano(str(tmp_path), GSV_PANO) == DownloadResult.success
        assert os.listdir(tmp_path / 'gs') == ['%s.jpg' % GSV_PANO['pano_id']]

    def test_a_failed_save_leaves_no_file_to_mistake_for_success(self, monkeypatch, tmp_path, stitchable):
        def full_disk(self, fp, *args, **kwargs):
            with open(fp, 'wb') as f:
                f.write(b'\xff\xd8\xff\xe0 truncated')
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(Image.Image, 'save', full_disk)

        with pytest.raises(OSError):
            downloaders.gsv.download_single_pano(str(tmp_path), GSV_PANO)

        assert os.listdir(tmp_path / 'gs') == [], "a stub .jpg would be read as done by every later run"


class TestDownloadPanoRoutesBySource:
    """`downloaders.download_pano` itself — the dispatcher whose docstring states the #41 contract the two
    modules above are held to, and which nothing called with a real source string until #57.

    Every test in this file goes through one downloader directly; the image loop goes through here.
    """

    @pytest.fixture
    def recorded(self, monkeypatch):
        """Replace both downloaders with recorders, so nothing here can reach a network or a disk."""
        calls = []

        def recorder(name, verdict):
            def fake(storage_path, pano_info):
                calls.append((name, storage_path, pano_info))
                return verdict
            return fake

        monkeypatch.setattr(downloaders.gsv, 'download_single_pano',
                            recorder('gsv', DownloadResult.success))
        monkeypatch.setattr(downloaders.mapillary, 'download_single_pano',
                            recorder('mapillary', DownloadResult.skipped))
        return calls

    def test_a_gsv_pano_reaches_gsv_with_its_arguments_intact(self, recorded):
        result = downloaders.download_pano('/store', GSV_PANO)

        assert recorded == [('gsv', '/store', GSV_PANO)]
        assert result == DownloadResult.success

    def test_a_mapillary_pano_reaches_mapillary(self, recorded):
        result = downloaders.download_pano('/store', MAPILLARY_PANO)

        assert recorded == [('mapillary', '/store', MAPILLARY_PANO)]
        # The verdict is passed through untouched rather than re-derived - the ledger's whole contract is
        # that the downloader decides permanence, not the dispatcher.
        assert result == DownloadResult.skipped

    def test_a_pano_with_no_source_field_defaults_to_gsv(self, recorded):
        """The -c CSV intake is hand-made by operators and carries whatever columns they wrote; a pano with
        no 'source' at all is the ordinary case there, not a malformed one."""
        downloaders.download_pano('/store', {'pano_id': 'abcdefghijklmnopqrstuv'})

        assert [name for name, _, _ in recorded] == ['gsv']

    def test_an_unrecognised_source_raises_rather_than_ledgering_the_pano(self, recorded):
        """A source we don't recognise is a property of OUR code - a new imagery type shipped by the server
        before this repo learned about it - not a permanent property of the pano. Returning
        DownloadResult.failure would ledger every such pano downloaded=0 and never look at them again, so a
        few hours of deploy skew would permanently blacklist a whole city's corpus (#41).
        """
        with pytest.raises(ValueError) as excinfo:
            downloaders.download_pano('/store', {'pano_id': 'x', 'source': 'bing'})

        assert 'bing' in str(excinfo.value)
        assert recorded == [], 'neither downloader should have been consulted'


class TestMapillarySessionRetryPolicy:
    """`_session()`'s adapter configuration, which is what stands between the fleet and Mapillary's 429s."""

    def test_both_schemes_carry_the_retry_policy(self):
        session = downloaders.mapillary._session()

        for url in ('https://graph.mapillary.com/1', 'http://cdn.example/x.jpg'):
            retries = session.get_adapter(url).max_retries
            assert retries.total == 5
            assert retries.connect == 5
            assert set(retries.status_forcelist) >= {429, 500, 502, 503, 504}

    def test_the_plain_http_scheme_is_mounted_too(self):
        """Not redundant with the above: thumb_original_url is a short-lived signed CDN URL that this code
        never inspects, so an http:// rendition (or a redirect through one) must keep the policy rather
        than falling back to requests' default of no retries at all."""
        session = downloaders.mapillary._session()

        assert session.get_adapter('http://cdn.example/x.jpg') is \
            session.get_adapter('https://cdn.example/x.jpg')


class TestAnImageAlreadyOnDiskIsItsOwnResumeMarker:

    def test_an_existing_jpg_is_skipped_before_the_token_or_the_network(self, monkeypatch, tmp_path):
        """The skip has to come first. A resumed run over a fully-downloaded city must not need the token
        set, and must not open a session per pano to discover it has nothing to do.
        """
        monkeypatch.delenv(downloaders.mapillary.TOKEN_ENV_VAR, raising=False)

        def explode():
            raise AssertionError('a pano already on disk must not build a session')

        monkeypatch.setattr(downloaders.mapillary, '_session', explode)
        shard = tmp_path / MAPILLARY_PANO['pano_id'][:2]
        shard.mkdir()
        (shard / (MAPILLARY_PANO['pano_id'] + '.jpg')).write_bytes(jpeg_bytes(80))

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.skipped


class TestALostShardDirRaceDoesNotFailThePano:
    """Both downloaders chmod the shard directory they just created, and swallow PermissionError.

    The pano store is shared: another user's scraper run can create the same shard a microsecond earlier,
    and then the chmod is against their directory and fails. Letting that propagate would turn a harmless
    race into a failed pano - and, on the GSV side, into a transient error retried every night forever.
    Stubbing chmod rather than manipulating real modes keeps this meaningful on Windows too.
    """

    @staticmethod
    def deny_chmod_on_directories(monkeypatch):
        """Refuse chmod on directories only.

        `module.os` is the one shared os module, so a blanket stub would also hit atomic_output_path's chmod
        of the .part file and prove nothing about the shard-dir race. Directories are exactly what the race
        is about, and it keeps the atomic write's own failure modes visible.
        """
        real_chmod = os.chmod

        def refuse_directories(path, mode, *args, **kwargs):
            if os.path.isdir(path):
                raise PermissionError(1, 'Operation not permitted')
            return real_chmod(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, 'chmod', refuse_directories)

    def test_mapillary_still_downloads(self, monkeypatch, tmp_path, mapillary_token):
        self.deny_chmod_on_directories(monkeypatch)
        session = FakeSession(FakeResponse(payload=HEALTHY_MAPILLARY_METADATA),
                              FakeResponse(chunks=[jpeg_bytes(120)]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.success

    def test_panoramax_still_downloads(self, monkeypatch, tmp_path):
        self.deny_chmod_on_directories(monkeypatch)
        panoramax_session(monkeypatch, FakeResponse(payload=PANORAMIC_ITEM),
                          FakeResponse(chunks=[jpeg_bytes(120)]))

        assert (downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
                == DownloadResult.success)

    def test_gsv_still_downloads(self, monkeypatch, tmp_path):
        tile = jpeg_bytes(120)
        monkeypatch.setattr(downloaders.gsv, '_get_response',
                            lambda url, session, stream=False: io.BytesIO(tile))

        async def fake_download_tiles(tiles):
            return [(x, y, tile) for x, y, _url in tiles]

        monkeypatch.setattr(downloaders.gsv, '_download_tiles', fake_download_tiles)
        self.deny_chmod_on_directories(monkeypatch)

        assert downloaders.gsv.download_single_pano(str(tmp_path), GSV_PANO) == DownloadResult.success


# --- Panoramax (#110) -----------------------------------------------------------------------------------
#
# Two REAL STAC items, fetched from the live keyless catalog on 2026-09-08 and trimmed to the fields this
# module reads (exif, geometry, links and the tile matrix dropped; nothing else altered). They are pinned as
# data for the reason OBSERVED_MAPILLARY_AUTH_FAILURES_2026_09_05 is: the shape a permanent verdict rests on
# has to be the shape the API actually serves, not the shape we imagined while writing the parser.
#
# Both were returned by ONE bbox search over Bayonne, which is the point of the pair: the catalog is a
# federation and the flat picture is not an error, an edge case, or someone else's city - it is a
# contributor's GoPro HERO8 photograph sitting in the same bbox as the 360 survey Project Sidewalk is here
# for, on a different host and under a different licence. 323 of 1,000 pictures in that bbox are flat.
OBSERVED_PANORAMAX_ITEMS_2026_09_08 = {
    "a 360 panorama from Bayonne's own survey": {
        'id': '4ebd63bc-9b6f-4ea8-94d4-6eec2b6ce6a6',
        'type': 'Feature',
        'collection': 'ad10778e-56f7-4fca-88d5-368445cffd2c',
        'assets': {
            'hd': {'type': 'image/jpeg', 'roles': ['data'],
                   'href': 'https://panoramax.ign.fr/api/pictures/'
                           '4ebd63bc-9b6f-4ea8-94d4-6eec2b6ce6a6/hd.jpg'},
            'sd': {'type': 'image/jpeg', 'roles': ['visual'],
                   'href': 'https://panoramax.ign.fr/api/pictures/'
                           '4ebd63bc-9b6f-4ea8-94d4-6eec2b6ce6a6/sd.jpg'},
            'thumb': {'type': 'image/jpeg', 'roles': ['thumbnail'],
                      'href': 'https://panoramax.ign.fr/api/pictures/'
                              '4ebd63bc-9b6f-4ea8-94d4-6eec2b6ce6a6/thumb.jpg'},
        },
        'properties': {
            'datetime': '2024-08-08T11:57:28+00:00',
            'license': 'etalab-2.0',
            'geovisio:producer': 'sig_bayonne',
            'view:azimuth': 31,
            'panoramax:horizontal_pixel_density': 16,
            'pers:interior_orientation': {'camera_manufacturer': 'GoPro', 'camera_model': 'Max',
                                          'field_of_view': 360, 'focal_length': 3.0,
                                          'sensor_array_dimensions': [5760, 2880]},
        },
    },
    'a flat 92-degree photograph in the same bbox': {
        'id': 'de0d8e2f-6063-4811-8bfd-c39af3256aa3',
        'type': 'Feature',
        'collection': '9a13941b-e541-4cd4-8332-1370f903ab67',
        'assets': {
            'hd': {'type': 'image/jpeg', 'roles': ['data'],
                   'href': 'https://panoramax.openstreetmap.fr/images/de/0d/8e/2f/'
                           '6063-4811-8bfd-c39af3256aa3.jpg'},
            'sd': {'type': 'image/jpeg', 'roles': ['visual'],
                   'href': 'https://panoramax.openstreetmap.fr/derivates/de/0d/8e/2f/'
                           '6063-4811-8bfd-c39af3256aa3/sd.jpg'},
            'thumb': {'type': 'image/jpeg', 'roles': ['thumbnail'],
                      'href': 'https://panoramax.openstreetmap.fr/derivates/de/0d/8e/2f/'
                              '6063-4811-8bfd-c39af3256aa3/thumb.jpg'},
        },
        'properties': {
            'datetime': '2022-07-09T16:43:37.009600+00:00',
            'license': 'CC-BY-SA-4.0',
            'geovisio:producer': 'Patchanka',
            'view:azimuth': 56,
            'panoramax:horizontal_pixel_density': 43,
            'pers:interior_orientation': {'camera_manufacturer': 'GoPro', 'camera_model': 'HERO8 Black',
                                          'field_of_view': 92, 'focal_length': 3.0,
                                          'sensor_array_dimensions': [4000, 2020]},
        },
    },
}
PANORAMIC_ITEM = OBSERVED_PANORAMAX_ITEMS_2026_09_08["a 360 panorama from Bayonne's own survey"]
FLAT_ITEM = OBSERVED_PANORAMAX_ITEMS_2026_09_08['a flat 92-degree photograph in the same bbox']

# Measured 2026-09-08 against GET /api/pictures/<a well-formed uuid the catalog does not have>. The status is
# a real 404, unlike Mapillary's does-not-exist (a 400), which is why this source can ledger on the status
# alone. The body is pinned because the SEARCH endpoint answers the same question with 200 + {"features":[]},
# and choosing between those two endpoints is what decides whether absence can ever be a verdict here (#99).
OBSERVED_PANORAMAX_NOT_FOUND_2026_09_08 = (404, {'status': 404, 'message': 'Feature not found'})
OBSERVED_PANORAMAX_SEARCH_MISS_2026_09_08 = (200, {'features': [], 'links': []})

PANORAMAX_PANO = {'pano_id': PANORAMIC_ITEM['id'], 'source': 'panoramax'}
PANORAMAX_SHARD = PANORAMAX_PANO['pano_id'][:2]


def panoramax_session(monkeypatch, *responses):
    session = FakeSession(*responses)
    monkeypatch.setattr(downloaders.panoramax, 'retrying_session', lambda: session)
    return session


class TestPanoramaxPermanentVerdicts:
    """Properties of the PICTURE: ledgered downloaded=0 and never re-attempted (#41)."""

    def test_an_unknown_picture_id_is_permanent(self, monkeypatch, tmp_path):
        status, body = OBSERVED_PANORAMAX_NOT_FOUND_2026_09_08
        panoramax_session(monkeypatch, FakeResponse(status_code=status, payload=body))

        assert downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO) \
            == DownloadResult.failure

    def test_a_404_carrying_the_catalogs_own_body_is_the_verdict(self, monkeypatch, tmp_path):
        """The measured body is what makes a 404 permanent, not the status."""
        status, body = OBSERVED_PANORAMAX_NOT_FOUND_2026_09_08
        panoramax_session(monkeypatch, FakeResponse(status_code=status, payload=body))

        assert downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO) \
            == DownloadResult.failure

    @pytest.mark.parametrize('response', [
        FakeResponse(status_code=404, payload=None, body='<html>404 Not Found</html>'),
        FakeResponse(status_code=404, payload=None, body=''),
        FakeResponse(status_code=404, payload={'error': 'gateway'}),
        FakeResponse(status_code=404, payload=['not', 'an', 'object']),
        FakeResponse(status_code=404, payload={'message': None}),
        FakeResponse(status_code=404, payload={'message': 'Not Found'}),
        FakeResponse(status_code=404, payload={'status': 500, 'message': 'Feature not found'}),
    ], ids=['an HTML error page', 'an empty body', 'JSON that does not say it', 'a non-object body',
            'a null message', 'a bare message with no status', 'a status field that is not 404'])
    def test_a_404_WITHOUT_the_catalogs_body_raises_instead_of_ledgering(self, monkeypatch, tmp_path,
                                                                        response):
        """The status alone does not say the CATALOG answered, and this used to read no further than it.

        The old justification - keyless, so no "we are not allowed to see it" state to confuse with "it is
        not there" - is true and answers the wrong question. api.panoramax.xyz is a keyless community API
        and this is one path segment of it: a renamed endpoint, a CDN or WAF answering 404 for an
        unrecognised UA, or DNS landing on a parked host all 404 for every pano in the city, and every one
        of them ledgered. That is the 2026-09-01 Mapillary incident at whole-city scale, and this source has
        no #113 breaker entry to bound it. The affirmation is free in the same body (2026-09-09 review).
        """
        panoramax_session(monkeypatch, response)

        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
        assert os.listdir(tmp_path / PANORAMAX_PANO['pano_id'][:2]) == []

    def test_a_flat_photograph_is_refused_and_never_stored(self, monkeypatch, tmp_path):
        """The real measured flat item, which HAS a perfectly good hd asset - so this can only be the field
        of view talking, not a missing-asset verdict wearing its clothes.

        What it prevents: a 4000x2020 rectilinear photograph saved as <pano_id>.jpg. Nothing downstream
        inspects projection. CropRunner would cut from it with the equirectangular seam modulo, wrap pano_x
        at a seam that does not exist, and emit a crop that looks entirely plausible."""
        pano = {'pano_id': FLAT_ITEM['id'], 'source': 'panoramax'}
        panoramax_session(monkeypatch, FakeResponse(payload=FLAT_ITEM))

        assert downloaders.panoramax.download_single_pano(str(tmp_path), pano) == DownloadResult.failure
        assert os.listdir(tmp_path / FLAT_ITEM['id'][:2]) == []

    @pytest.mark.parametrize('interior', [
        {'field_of_view': None},
        {'focal_length': 3.0},
    ], ids=['field_of_view present but null', 'an interior orientation with no field_of_view'])
    def test_an_unstated_field_of_view_reads_as_none_and_is_NOT_rescued_to_360(self, interior):
        """The anti-rescue assertion, in the module that owns the guard.

        Both existing `is None` cases exit at the `properties` guard without ever reaching the
        `interior.get('field_of_view')` line, so `.get('field_of_view', PANORAMIC_FIELD_OF_VIEW_DEG)` -
        a default that silently turns "the catalog said nothing" into "the catalog affirmed 360" - passed
        the entire suite. These two reach that line. Behaviourally neutral today, since both spellings
        download, which is exactly why it survived (2026-09-09 review); it stops being neutral the moment
        anything downstream distinguishes "affirmed 360" from "said nothing".
        """
        item = {'id': 'x', 'properties': {'pers:interior_orientation': interior}}

        assert downloaders.panoramax.declared_field_of_view(item) is None

    @pytest.mark.parametrize('fov, panoramic', [
        (360, True), (360.0, True), ('360', True), ('360.0', True), (359.9997, True),
        (92, False), (180, False), (200, False), ('panoramic', False), (None, False),
        ({'value': 360}, False), ([360], False),
    ])
    def test_a_panoramic_field_of_view_survives_a_change_of_type(self, fov, panoramic):
        """It was an exact `!= 360` against the int until the 2026-09-09 review - safe for 360.0 and nothing
        else. A catalog that started serving `"360"`, a Decimal, or a stitched 359.9997 would have routed
        every genuine panorama in the city to a permanent downloaded=0 row. The module already applies this
        repo's #46 doctrine to the id comparison in require_picture_record, where the consequence is milder
        because it raises; this is the comparison where being wrong is unrecoverable."""
        assert downloaders.panoramax.is_panoramic_field_of_view(fov) is panoramic

    @pytest.mark.parametrize('fov', [360, 360.0, '360', 359.9997],
                             ids=['int', 'float', 'the string 360', 'a stitched 359.9997'])
    def test_a_panoramic_picture_downloads_whatever_type_its_fov_arrives_as(self, monkeypatch, tmp_path,
                                                                           fov):
        """The call site, not just the helper: an exact `!= 360` passes every assertion on
        is_panoramic_field_of_view and still writes off the whole city, because the guard is where the
        comparison is used."""
        item = dict(PANORAMIC_ITEM)
        item['properties'] = dict(item['properties'],
                                  **{'pers:interior_orientation': {'field_of_view': fov}})
        panoramax_session(monkeypatch, FakeResponse(payload=item), FakeResponse(chunks=[jpeg_bytes(120)]))

        assert (downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
                == DownloadResult.success)

    @pytest.mark.parametrize('fov', [92, 180, 200, 'panoramic'], ids=['92', '180', '200', 'a word'])
    def test_any_affirmed_non_360_field_of_view_is_refused(self, monkeypatch, tmp_path, fov):
        """A value the catalog affirmed that is not 360, including one that is not a number at all.

        NOT including the string `'360'`, which this docstring claimed until 2026-09-10 and which
        test_a_panoramic_picture_downloads_whatever_type_its_fov_arrives_as covers from the other side: the
        comparison became tolerant in the same PR, so `'360'` is a panorama and is downloaded. The
        catalog serves the value as a number today; the day that changes, the tolerant compare is what
        stops the entire city from being refused loudly and permanently."""
        interior = dict(PANORAMIC_ITEM['properties']['pers:interior_orientation'], field_of_view=fov)
        properties = dict(PANORAMIC_ITEM['properties'])
        properties['pers:interior_orientation'] = interior
        panoramax_session(monkeypatch, FakeResponse(payload=dict(PANORAMIC_ITEM, properties=properties)))

        assert downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO) \
            == DownloadResult.failure

    @pytest.mark.parametrize('assets', [{}, {'sd': {'href': 'https://x/sd.jpg'}}],
                             ids=['no hd', 'sd only'])
    def test_a_picture_with_no_hd_asset_is_permanent(self, monkeypatch, tmp_path, assets):
        """The catalog affirmed the picture and publishes no full-resolution pixels for it - the same
        verdict shape as Mapillary's "knows the image, no original-resolution rendition".

        `{'hd': {}}` was a third case here until 2026-09-10 and is now on the raising side, beside
        `{'hd': {'href': None}}`: an hd entry that exists and carries no usable href is the catalog
        changing shape, not a statement about this picture's pixels."""
        panoramax_session(monkeypatch, FakeResponse(payload=dict(PANORAMIC_ITEM, assets=assets)))

        assert downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO) \
            == DownloadResult.failure


class TestPanoramaxTransientConditions:
    """Properties of the RUN. Returning failure would ledger them permanently, so one bad night would
    blacklist every Panoramax pano in the city (#41)."""

    # The same spread the Mapillary suite uses, and for the same reason: the rule is "only 404 is
    # permanent", so pinning the rule rather than the statuses we happened to observe is what stops the next
    # unanticipated one - 402, 410, 451 - from being a gap. 429/5xx are contract cases here, not wire cases:
    # retrying_session's status_forcelist retries them inside the adapter, so in production they surface as
    # RetryError. FakeSession bypasses the adapter.
    @pytest.mark.parametrize('status', [400, 401, 402, 403, 410, 422, 429, 451, 500, 503])
    def test_every_non_404_error_status_raises(self, monkeypatch, tmp_path, status):
        panoramax_session(monkeypatch, FakeResponse(status_code=status))

        with pytest.raises(requests.HTTPError):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
        assert os.listdir(tmp_path / PANORAMAX_SHARD) == []

    def test_a_non_json_metadata_body_raises(self, monkeypatch, tmp_path):
        panoramax_session(monkeypatch, FakeResponse(payload=None, body='<html>502</html>'))

        with pytest.raises(ValueError):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)

    def test_the_error_envelope_on_a_200_raises_rather_than_ledgering(self, monkeypatch, tmp_path):
        """The measured 404 body, arriving on a 200. Something between us and the catalog rewrote the
        status, which is a condition of the run - and if this were read as "no picture" the whole city would
        ledger downloaded=0 in one night."""
        _, envelope = OBSERVED_PANORAMAX_NOT_FOUND_2026_09_08
        panoramax_session(monkeypatch, FakeResponse(payload=envelope))

        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse, match='error envelope'):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
        assert os.listdir(tmp_path / PANORAMAX_SHARD) == []

    def test_the_search_endpoints_empty_answer_could_never_have_been_a_verdict(self, monkeypatch, tmp_path):
        """/api/search?ids=<unknown> answers 200 with {"features":[]} - absence dressed as success, the
        exact body #99 says must not found a permanent verdict. This module resolves by /api/pictures/<id>
        precisely so that absence arrives as a 404 instead; the test exists so that a future "just use
        search, it is one request either way" change fails here rather than in the ledger."""
        _, body = OBSERVED_PANORAMAX_SEARCH_MISS_2026_09_08
        panoramax_session(monkeypatch, FakeResponse(payload=body))

        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)

    @pytest.mark.parametrize('payload', [[], 'not an object', 42],
                             ids=['empty list', 'a string', 'a number'])
    def test_a_body_that_is_not_a_json_object_raises(self, monkeypatch, tmp_path, payload):
        panoramax_session(monkeypatch, FakeResponse(payload=payload))

        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse, match='not a JSON object'):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)

    def test_the_item_wrapped_in_a_list_raises(self, monkeypatch, tmp_path):
        """The natural mistake if anyone ever swaps the endpoint for search: right item, wrong envelope."""
        panoramax_session(monkeypatch, FakeResponse(payload=[PANORAMIC_ITEM]))

        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse, match='not a JSON object'):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)

    def test_a_record_naming_another_picture_raises(self, monkeypatch, tmp_path):
        """A cache or a redirect serving someone else's item. Following its hd href would store another
        picture's pixels under this pano's id - permanent, and invisible to everything downstream."""
        panoramax_session(monkeypatch, FakeResponse(payload=dict(PANORAMIC_ITEM, id=FLAT_ITEM['id'])))

        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse, match='does not name'):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)

    @pytest.mark.parametrize('href', ['/api/pictures/x/hd.jpg', 'http://panoramax.ign.fr/x/hd.jpg',
                                      'file:///etc/passwd', '//panoramax.ign.fr/x/hd.jpg'],
                             ids=['relative', 'plain http', 'file', 'protocol-relative'])
    def test_an_hd_href_that_is_not_absolute_https_raises(self, monkeypatch, tmp_path, href):
        """Relative hrefs do occur in the same document - the `rel: related` links are `/api/...` paths - so
        this is not a hypothetical shape, just one that has never appeared under `assets.hd`. Whatever it
        means, it is the catalog changing shape rather than a verdict on the picture."""
        assets = dict(PANORAMIC_ITEM['assets'])
        assets['hd'] = {'type': 'image/jpeg', 'href': href}
        panoramax_session(monkeypatch, FakeResponse(payload=dict(PANORAMIC_ITEM, assets=assets)))

        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse, match='absolute https'):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)

    @pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
    def test_a_redirect_off_the_published_href_is_refused_rather_than_followed(self, monkeypatch, tmp_path,
                                                                              status):
        """The hd href is the one URL in this repo taken from a remote response rather than built, on a
        public commons anyone can contribute to - and requests follows up to 30 redirects to ANY scheme and
        host by default, so the https check covers the first hop only. A 302 to http://169.254.169.254/ or
        to a private address would be followed and its body streamed into the store; if it happened to start
        with a JPEG SOF it would be renamed in and ledgered downloaded=1 (2026-09-09 review).

        Raises rather than ledgering: a federated instance serving its own pictures has no reason to
        redirect, so one that starts is the catalog changing shape, and the pano retries untouched."""
        panoramax_session(monkeypatch,
                          FakeResponse(payload=PANORAMIC_ITEM),
                          FakeResponse(status_code=status, body='moved'))

        # Matched on the reason: this double returns the queued response rather than following anything, so
        # without the match= a mutant that drops the check still "passes" - the redirect body is simply not
        # a JPEG and raises one line later, for a completely different reason.
        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse, match='redirect'):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
        assert os.listdir(tmp_path / PANORAMAX_SHARD) == []

    @pytest.mark.parametrize('assets, reason', [
        (None, 'no assets block at all'),
        ('not an object', 'a non-object assets block'),
        ({'hd': 'not an object'}, 'a non-object hd asset'),
        ({'hd': {'href': ''}}, 'an hd asset with an empty href'),
        ({'hd': {'href': None}}, 'an hd asset with a null href'),
        ({'hd': {}}, 'an hd asset with no href at all'),
    ])
    def test_a_malformed_assets_block_raises_rather_than_writing_the_picture_off(self, monkeypatch,
                                                                                tmp_path, assets, reason):
        """require_picture_record establishes only that the body is a dict whose id matches - which
        `{"id": "<uuid>"}` alone satisfies - so none of these is the catalog saying anything about this
        picture's pixels. They are the catalog changing shape, and ledgering them meant one `?fields=`
        slimming, or assets moving under properties the way pers:interior_orientation already sits there,
        would write off a whole city in one night with nothing loud about it (2026-09-09 review).

        Contrast test_a_picture_with_no_hd_asset_is_permanent: a well-formed assets block that simply does
        not offer `hd` IS the verdict."""
        item = dict(PANORAMIC_ITEM)
        if assets is None:
            item.pop('assets', None)
        else:
            item['assets'] = assets
        panoramax_session(monkeypatch, FakeResponse(payload=item))

        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
        assert os.listdir(tmp_path / PANORAMAX_SHARD) == []

    def test_an_unreachable_instance_raises(self, monkeypatch, tmp_path):
        """The catalog already proved the picture exists; the instance holding its pixels being down is
        never a verdict on the picture."""
        panoramax_session(monkeypatch, FakeResponse(payload=PANORAMIC_ITEM), FakeResponse(status_code=503))

        with pytest.raises(requests.HTTPError):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)

    def test_a_dying_stream_leaves_no_file_to_mistake_for_success(self, monkeypatch, tmp_path):
        """With no ledger row written (#41), the NEXT run reaches the os.path.isfile() check - so a
        truncated .jpg left here would be reported as a completed download forever."""
        panoramax_session(monkeypatch, FakeResponse(payload=PANORAMIC_ITEM),
                          FakeResponse(chunks=[b'\xff\xd8\xff\xe0 partial',
                                               requests.ConnectionError('connection reset mid-stream')]))

        with pytest.raises(requests.ConnectionError):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
        assert os.listdir(tmp_path / PANORAMAX_SHARD) == []

    def test_a_200_image_body_that_is_not_a_jpeg_is_refused_before_it_becomes_the_resume_marker(
            self, monkeypatch, tmp_path):
        """An instance mid-deploy answering 200 with a holding page. Saved as .jpg that page IS the resume
        marker: permanent, with no ledger row to edit."""
        panoramax_session(monkeypatch, FakeResponse(payload=PANORAMIC_ITEM),
                          FakeResponse(chunks=[b'<!DOCTYPE html><html><body>502</body></html>']))

        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse, match='not a JPEG'):
            downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
        assert os.listdir(tmp_path / PANORAMAX_SHARD) == []


class TestPanoramaxHealthyPath:
    def test_a_real_bayonne_item_downloads_from_the_instance_named_in_its_href(self, monkeypatch, tmp_path):
        session = panoramax_session(monkeypatch, FakeResponse(payload=PANORAMIC_ITEM),
                                    FakeResponse(chunks=[jpeg_bytes(120)]))

        assert downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO) \
            == DownloadResult.success
        assert os.listdir(tmp_path / PANORAMAX_SHARD) == ['%s.jpg' % PANORAMAX_PANO['pano_id']]

        metadata_url, image_url = session.calls[0][0], session.calls[1][0]
        assert metadata_url == '%s/pictures/%s' % (downloaders.panoramax.CATALOG_API_BASE,
                                                   PANORAMAX_PANO['pano_id'])
        # Followed from the item, not built from the catalog base: the pixels live on a different host than
        # the record, and only the record knows which.
        assert image_url == PANORAMIC_ITEM['assets']['hd']['href']
        assert image_url.startswith('https://panoramax.ign.fr/')

    def test_no_credential_is_sent_anywhere(self, monkeypatch, tmp_path):
        """Panoramax is keyless. The mirror of TestTheTokenStaysOutOfURLs: there is nothing to leak here,
        and this is what fails if someone copies mapillary.py's header or params in wholesale."""
        session = panoramax_session(monkeypatch, FakeResponse(payload=PANORAMIC_ITEM),
                                    FakeResponse(chunks=[jpeg_bytes(120)]))

        downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)

        for url, kwargs in session.calls:
            assert 'token' not in url.lower()
            assert not kwargs.get('params')
            assert 'Authorization' not in (kwargs.get('headers') or {})

    @pytest.mark.parametrize('properties', [
        {},
        {'pers:interior_orientation': {}},
        {'pers:interior_orientation': {'camera_model': 'Max'}},
        {'pers:interior_orientation': 'not an object'},
    ], ids=['no interior orientation', 'empty', 'no field_of_view key', 'not an object'])
    def test_a_picture_that_states_no_field_of_view_is_still_downloaded(self, monkeypatch, tmp_path,
                                                                       properties):
        """The asymmetry that makes the guard safe: it refuses only what the item AFFIRMS is not 360.
        `pers:interior_orientation` is a STAC extension the catalog is under no obligation to keep serving,
        and an absent field is exactly the absence-of-evidence a verdict must not rest on (#99). Getting
        this backwards would ledger a whole city the day the field moved."""
        panoramax_session(monkeypatch, FakeResponse(payload=dict(PANORAMIC_ITEM, properties=properties)),
                          FakeResponse(chunks=[jpeg_bytes(120)]))

        assert downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO) \
            == DownloadResult.success

    def test_an_existing_file_is_the_resume_marker_and_costs_no_request(self, monkeypatch, tmp_path):
        shard = tmp_path / PANORAMAX_SHARD
        shard.mkdir()
        (shard / ('%s.jpg' % PANORAMAX_PANO['pano_id'])).write_bytes(jpeg_bytes(9))
        session = panoramax_session(monkeypatch)

        assert downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO) \
            == DownloadResult.skipped
        assert session.calls == []


class TestPanoramaxChunking:
    def test_an_empty_chunk_is_skipped_rather_than_written(self, monkeypatch, tmp_path):
        """requests yields b'' for a keep-alive chunk. Writing it is harmless; the `if chunk:` guard exists
        so the two downloaders behave identically, and this closes the branch rather than leaving it in
        .coveragerc's enumerated remainder the way the Mapillary copy is."""
        body = jpeg_bytes(120)
        panoramax_session(monkeypatch, FakeResponse(payload=PANORAMIC_ITEM),
                          FakeResponse(chunks=[body[:64], b'', body[64:]]))

        assert (downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)
                == DownloadResult.success)
        assert (tmp_path / PANORAMAX_SHARD / ('%s.jpg' % PANORAMAX_PANO['pano_id'])).read_bytes() == body


class TestPanoramaxSeams:
    """The three body readers, driven directly. download_single_pano composes them; these say what each one
    decides on its own, which is where the positive-evidence rule (#99) actually lives."""

    def test_the_field_of_view_reader_agrees_with_both_measured_items(self):
        assert downloaders.panoramax.declared_field_of_view(PANORAMIC_ITEM) == 360
        assert downloaders.panoramax.declared_field_of_view(FLAT_ITEM) == 92
        assert downloaders.panoramax.declared_field_of_view({}) is None
        assert downloaders.panoramax.declared_field_of_view({'pers:interior_orientation': None}) is None

    def test_the_hd_reader_returns_the_measured_href_and_never_sd(self):
        assert downloaders.panoramax.hd_asset_url(PANORAMIC_ITEM) \
            == PANORAMIC_ITEM['assets']['hd']['href']
        # sd is a different frame; storing it under dimensions describing hd is how every label in the city
        # ends up skipped by the cropper's dims_mismatch preflight.
        assert downloaders.panoramax.hd_asset_url(PANORAMIC_ITEM) \
            != PANORAMIC_ITEM['assets']['sd']['href']
        assert downloaders.panoramax.hd_asset_url({'assets': {'sd': {'href': 'https://x/sd.jpg'}}}) is None

    def test_the_not_found_reader_needs_the_catalogs_WHOLE_fingerprint(self):
        """Both measured fields, not just the one that reads like prose.

        `{"message": "Not Found"}` is the DEFAULT 404 body of AWS API Gateway and of Express - the two
        things likeliest to sit in front of a community API without anyone here knowing - so a match on the
        message alone accepts a stranger's reply as the catalog's verdict and ledgers a whole city off it.
        That is the same failure the 2026-09-09 review closed one level up by reading the body at all, one
        field short of closed: reading a body and then matching a substring every proxy on the internet
        also emits is not positive evidence (#99).

        `status` is compared numerically for the #46 reason is_panoramic_field_of_view gives - the catalog
        is free to serve it as a string - and the cost of getting the tolerance wrong is asymmetric here:
        too strict means an absent picture is re-requested nightly (noisy, self-announcing, free to fix),
        too loose means a permanent downloaded=0 row that nothing downstream will ever revisit.
        """
        not_found = downloaders.panoramax.is_catalog_not_found
        status, body = OBSERVED_PANORAMAX_NOT_FOUND_2026_09_08
        assert not_found(FakeResponse(status_code=status, payload=body))
        assert not_found(FakeResponse(status_code=404,
                                      payload={'status': '404', 'message': 'Feature not found'}))
        # The catalog is free to ADD fields; the verdict must survive that or every absent picture becomes
        # a nightly retry forever.
        assert not_found(FakeResponse(status_code=404,
                                      payload=dict(body, detail='https://api.panoramax.xyz/errors/404')))

        for payload in ({'message': 'Not Found'},
                        {'message': 'Feature not found'},
                        {'status': 500, 'message': 'Feature not found'},
                        {'status': None, 'message': 'Feature not found'},
                        {'status': 404},
                        {'status': 404, 'message': None}):
            assert not not_found(FakeResponse(status_code=404, payload=payload)), payload

    def test_the_record_gate_accepts_only_the_picture_that_was_asked_for(self):
        require = downloaders.panoramax.require_picture_record
        assert require(PANORAMIC_ITEM, PANORAMIC_ITEM['id']) is None

        for payload in ([], None, 'x', {}, {'id': None},
                        {'status': 404, 'message': 'Feature not found'},
                        dict(PANORAMIC_ITEM, id=FLAT_ITEM['id'])):
            with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse):
                require(payload, PANORAMIC_ITEM['id'])

    def test_an_envelope_message_cannot_flood_the_log(self):
        """DownloadRunner logs str(e) per failed pano into a 10 MB x 3 rotation, so an uncapped body times a
        city's corpus rotates away the night's own diagnosis. Every field of the envelope comes from the
        same untrusted place, so both are capped, not just the one that looks like prose."""
        message = 'x' * 100_000
        with pytest.raises(downloaders.panoramax.PanoramaxErrorResponse) as excinfo:
            downloaders.panoramax.require_picture_record({'status': message, 'message': message}, 'some-id')

        assert len(str(excinfo.value)) < 1000
