"""Tests for the image downloaders' half of the #41 ledger contract.

`downloaders.download_pano` promises the image loop two things: `DownloadResult.failure` is a PERMANENT
property of the pano (it is ledgered `downloaded=0` and never re-attempted), and every transient condition
RAISES instead (no row, retried next run). These tests pin both halves at the source, plus the atomic-write
guarantee the contract depends on — because an existing `.jpg` is itself the resume marker, a download that
dies mid-write must not leave a truncated file for the next run to report as a completed success.

Network-free: the Mapillary tests install a fake session, and the GSV tests stub the tile fetches.
"""

import io
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
    def __init__(self, status_code=200, payload=None, body=None, chunks=None):
        self.status_code = status_code
        self._payload = payload
        self.text = body or ''
        self._chunks = chunks or []

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
    """Returns queued responses in order; records the URLs asked for."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.urls.append(url)
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

    def test_missing_original_rendition_is_permanent(self, monkeypatch, tmp_path, mapillary_token):
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(payload={})))

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.failure


class TestMapillaryTransientConditions:
    """These are properties of the RUN. Returning failure would ledger them permanently - an expired token
    for one night would blacklist every Mapillary pano in the city - so they must raise instead (#41)."""

    # 400 is not hypothetical. richmond-va's token stopped working and graph.mapillary.com answered every
    # request with one; the pre-#41 code ledgered each as permanent, writing off 162 panos as "Mapillary has
    # no image" and never re-attempting them. Recovering them meant hand-editing pano_id_log.csv on the store
    # (2026-09-01). The status the incident actually produced was the one status this list omitted, so it is
    # first here.
    @pytest.mark.parametrize('status', [400, 401, 403, 500])
    def test_metadata_http_errors_raise(self, monkeypatch, tmp_path, mapillary_token, status):
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(status_code=status)))

        with pytest.raises(requests.HTTPError):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

    def test_a_non_json_metadata_body_raises(self, monkeypatch, tmp_path, mapillary_token):
        """A proxy error page or a body truncated in flight - not a verdict on the pano."""
        monkeypatch.setattr(downloaders.mapillary, '_session',
                            lambda: FakeSession(FakeResponse(payload=None, body='<html>502</html>')))

        with pytest.raises(ValueError):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

    def test_an_expired_signed_image_url_raises(self, monkeypatch, tmp_path, mapillary_token):
        session = FakeSession(FakeResponse(payload={'thumb_original_url': 'https://cdn/x.jpg'}),
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
            FakeResponse(payload={'thumb_original_url': 'https://cdn/x.jpg'}),
            FakeResponse(chunks=[b'\xff\xd8\xff\xe0 partial',
                                 requests.ConnectionError('connection reset mid-stream')]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        with pytest.raises(requests.ConnectionError):
            downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert os.listdir(tmp_path / '12') == []

    def test_a_healthy_download_still_lands(self, monkeypatch, tmp_path, mapillary_token):
        session = FakeSession(FakeResponse(payload={'thumb_original_url': 'https://cdn/x.jpg'}),
                              FakeResponse(chunks=[jpeg_bytes(120)]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.success
        assert os.listdir(tmp_path / '12') == ['%s.jpg' % MAPILLARY_PANO['pano_id']]


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
        session = FakeSession(FakeResponse(payload={'thumb_original_url': 'https://cdn/x.jpg'}),
                              FakeResponse(chunks=[jpeg_bytes(120)]))
        monkeypatch.setattr(downloaders.mapillary, '_session', lambda: session)

        assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
            == DownloadResult.success

    def test_gsv_still_downloads(self, monkeypatch, tmp_path):
        tile = jpeg_bytes(120)
        monkeypatch.setattr(downloaders.gsv, '_get_response',
                            lambda url, session, stream=False: io.BytesIO(tile))

        async def fake_download_tiles(tiles):
            return [(x, y, tile) for x, y, _url in tiles]

        monkeypatch.setattr(downloaders.gsv, '_download_tiles', fake_download_tiles)
        self.deny_chmod_on_directories(monkeypatch)

        assert downloaders.gsv.download_single_pano(str(tmp_path), GSV_PANO) == DownloadResult.success
