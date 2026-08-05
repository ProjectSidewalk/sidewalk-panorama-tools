"""Tests for the depth-phase helpers: ledger reader, artifact writer, and HTTP session hardening."""

import os

import numpy as np
import pytest
import requests
from requests.adapters import HTTPAdapter

from conftest import make_pano
from downloaders import gsv


class TestLoadDepthLog:
    def test_missing_file_is_empty(self, tmp_path):
        assert gsv._load_depth_log(str(tmp_path / 'depth_log.csv')) == set()

    def test_reads_both_statuses_and_ignores_header(self, tmp_path):
        path = tmp_path / 'depth_log.csv'
        path.write_text('pano_id,status\naaaaaa,saved\nbbbbbb,unavailable\n')
        assert gsv._load_depth_log(str(path)) == {'aaaaaa', 'bbbbbb'}

    def test_skips_malformed_and_unknown_rows(self, tmp_path):
        path = tmp_path / 'depth_log.csv'
        path.write_text('pano_id,status\naaaaaa,saved\ntruncated\ncccccc,bogus-status\n\ndddddd,saved,extra\n')
        assert gsv._load_depth_log(str(path)) == {'aaaaaa'}


class TestWriteDepthArtifact:
    def test_roundtrip_and_atomicity(self, tmp_path):
        storage = str(tmp_path)
        depth = np.array([[1.0, -1.0], [2.5, 3.5]], dtype=np.float64)

        gsv._write_depth_artifact(storage, 'abcdef', make_pano(depth, heading=0.5, pitch=None, roll=1.5))

        path = os.path.join(storage, 'ab', 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX)
        assert os.path.isfile(path)
        assert not os.path.exists(path + '.part')
        # No stray file from savez_compressed appending .npz to the temp name.
        assert sorted(os.listdir(os.path.join(storage, 'ab'))) == ['abcdef.depth.npz']
        with np.load(path) as d:
            assert d['depth'].dtype == np.float32
            np.testing.assert_allclose(d['depth'], depth.astype(np.float32))
            assert float(d['heading']) == pytest.approx(0.5)
            assert np.isnan(float(d['pitch']))
            assert float(d['roll']) == pytest.approx(1.5)

    def test_shard_dir_created_with_group_perms(self, tmp_path):
        storage = str(tmp_path)
        gsv._write_depth_artifact(storage, 'abcdef', make_pano(np.zeros((1, 1))))
        mode = os.stat(os.path.join(storage, 'ab')).st_mode
        assert mode & 0o2777 == 0o2775


class TestTimeoutHTTPAdapter:
    @pytest.fixture
    def captured_send(self, monkeypatch):
        captured = {}

        def fake_send(self, request, **kwargs):
            captured.update(kwargs)
            return 'response'

        monkeypatch.setattr(HTTPAdapter, 'send', fake_send)
        return captured

    def test_injects_default_timeout(self, captured_send):
        adapter = gsv._TimeoutHTTPAdapter(timeout=30)
        # Session.send always passes timeout explicitly, as None when the caller set nothing.
        adapter.send('request', timeout=None)
        assert captured_send['timeout'] == 30

    def test_preserves_caller_timeout(self, captured_send):
        adapter = gsv._TimeoutHTTPAdapter(timeout=30)
        adapter.send('request', timeout=5)
        assert captured_send['timeout'] == 5


class TestDepthSession:
    def test_borrowed_headers_drop_host_and_accept(self, monkeypatch):
        monkeypatch.setattr(gsv, '_random_header', lambda: {
            'User-Agent': 'TestAgent/1.0',
            'Host': 'maps.google.com',
            'Accept': 'text/html',
            'Accept-Language': 'en-US',
        })
        session = gsv._depth_session()
        assert session.headers['User-Agent'] == 'TestAgent/1.0'
        assert session.headers['Accept-Language'] == 'en-US'
        assert 'Host' not in session.headers
        # The requests default Accept survives; the browser HTML Accept must not override it.
        assert session.headers['Accept'] == '*/*'

    def test_mounts_timeout_adapter_with_retries(self):
        session = gsv._depth_session()
        for prefix in ('http://', 'https://'):
            adapter = session.get_adapter(prefix + 'example.com')
            assert isinstance(adapter, gsv._TimeoutHTTPAdapter)
            assert adapter.max_retries.total == 5
            assert 429 in adapter.max_retries.status_forcelist

    def test_session_type_accepted_by_requests(self):
        assert isinstance(gsv._depth_session(), requests.Session)
