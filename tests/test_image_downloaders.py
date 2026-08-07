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

    @posix_only
    def test_the_renamed_file_is_group_writable(self, tmp_path):
        final = str(tmp_path / 'pano.jpg')

        with atomic_output_path(final) as tmp:
            open(tmp, 'wb').write(b'payload')

        assert os.stat(final).st_mode & 0o777 == 0o664


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

    @pytest.mark.parametrize('status', [401, 403, 500])
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
        monkeypatch.setattr(downloaders.gsv.asyncio, 'run',
                            lambda coro: (coro.close(), [['0 0', tile]])[1])

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
