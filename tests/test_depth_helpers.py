"""Tests for the depth-phase helpers: ledger reader, artifact writer, and HTTP session hardening."""

import os

import numpy as np
import pytest
import requests
from requests.adapters import HTTPAdapter

from conftest import make_pano, posix_only
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

    @posix_only
    def test_shard_dir_created_with_group_perms(self, tmp_path):
        storage = str(tmp_path)
        gsv._write_depth_artifact(storage, 'abcdef', make_pano(np.zeros((1, 1))))
        mode = os.stat(os.path.join(storage, 'ab')).st_mode
        assert mode & 0o2777 == 0o2775

    def test_losing_the_shard_dir_race_is_not_an_error(self, tmp_path, monkeypatch):
        """Two processes (or the image and depth phases) can create the same shard dir concurrently; losing
        the isdir/makedirs race must not fail the pano (#51)."""
        storage = str(tmp_path)
        os.makedirs(os.path.join(storage, 'ab'))
        # Simulate the race: only the guard's isdir call - the first one - sees the dir as missing. Later
        # calls (including makedirs' own exist_ok check) see reality.
        real_isdir, guard_called = os.path.isdir, []

        def racy_isdir(path):
            if not guard_called:
                guard_called.append(path)
                return False
            return real_isdir(path)

        monkeypatch.setattr(gsv.os.path, 'isdir', racy_isdir)

        gsv._write_depth_artifact(storage, 'abcdef', make_pano(np.zeros((1, 1))))

        assert os.path.isfile(os.path.join(storage, 'ab', 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX))

    def test_part_file_is_cleaned_up_when_the_write_fails(self, tmp_path, monkeypatch):
        """Nothing else ever sweeps .part files, so a failed write must not leave one on the store."""
        storage = str(tmp_path)

        def boom(*args, **kwargs):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(gsv.np, 'savez_compressed', boom)

        with pytest.raises(OSError):
            gsv._write_depth_artifact(storage, 'abcdef', make_pano(np.zeros((2, 2))))
        assert os.listdir(os.path.join(storage, 'ab')) == []


class TestNormalizeProxies:
    """config.py ships placeholder proxy values; anything that isn't a real proxy URL must reach requests as
    unset, per key (#51). The old all-or-nothing check blanked both entries only when the http key held its
    placeholder, so one real proxy could leak the other key's placeholder to requests as a URL."""

    def test_shipped_placeholders_are_unset(self):
        # Exactly what config.py ships: note the https key's placeholder is 'http://', not 'https://'.
        assert gsv._normalize_proxies({'http': 'http://', 'https': 'http://'}) == {'http': None, 'https': None}

    def test_https_style_placeholder_is_unset_too(self):
        assert gsv._normalize_proxies({'http': 'http://', 'https': 'https://'}) == {'http': None, 'https': None}

    def test_real_proxy_survives_a_placeholder_on_the_other_key(self):
        assert gsv._normalize_proxies({'http': 'http://proxy.example:8080', 'https': 'http://'}) == \
               {'http': 'http://proxy.example:8080', 'https': None}

    def test_empty_and_missing_values_are_unset(self):
        assert gsv._normalize_proxies({'http': '', 'https': None}) == {'http': None, 'https': None}

    def test_trimmed_dict_is_tolerated(self):
        assert gsv._normalize_proxies({}) == {}


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
    def test_does_not_borrow_config_browser_headers(self, monkeypatch):
        """streetlevel sets its own headers per request and request-level wins, so borrowing here is dead weight.

        Worse than dead: only the leftovers config.py sets and streetlevel doesn't (Accept-Language,
        Upgrade-Insecure-Requests, DNT) would survive, contradicting streetlevel's Firefox User-Agent.
        """
        monkeypatch.setattr(gsv, '_random_header', lambda: {
            'User-Agent': 'TestAgent/1.0',
            'Host': 'maps.google.com',
            'Accept-Language': 'en-GB',
            'Upgrade-Insecure-Requests': '1',
        })
        session = gsv._depth_session()
        for header in ('Host', 'Accept-Language', 'Upgrade-Insecure-Requests'):
            assert header not in session.headers
        assert session.headers['User-Agent'] != 'TestAgent/1.0'

    def test_mounts_timeout_adapter_with_retries_and_jitter(self):
        session = gsv._depth_session()
        for prefix in ('http://', 'https://'):
            adapter = session.get_adapter(prefix + 'example.com')
            assert isinstance(adapter, gsv._TimeoutHTTPAdapter)
            assert adapter.max_retries.total == 5
            assert 429 in adapter.max_retries.status_forcelist
            # Without jitter, concurrent city runs resynchronise onto one retry schedule after a shared outage.
            assert adapter.max_retries.backoff_jitter > 0

    def test_session_type_accepted_by_requests(self):
        assert isinstance(gsv._depth_session(), requests.Session)

    def test_block_detection_hook_is_installed(self):
        assert gsv._raise_if_blocked in gsv._depth_session().hooks['response']


def _response(status=200, url='https://www.google.com/maps/photometa/v1', location=None):
    response = requests.Response()
    response.status_code = status
    response.url = url
    if location is not None:
        response.headers['Location'] = location
    return response


class TestRaiseIfBlocked:
    def test_normal_response_passes_through(self):
        gsv._raise_if_blocked(_response())

    @pytest.mark.parametrize('url', [
        'https://www.google.com/sorry/index?continue=https://www.google.com/maps',
        'https://consent.google.com/m?continue=https://www.google.com/maps',
    ])
    def test_interstitial_landing_url_is_a_block(self, url):
        # Google serves these with HTTP 200, so only the URL gives it away.
        with pytest.raises(gsv.DepthBlockedError):
            gsv._raise_if_blocked(_response(url=url))

    def test_redirect_location_is_a_block(self):
        # Hooks fire on the redirect hop too, before the interstitial itself is fetched.
        with pytest.raises(gsv.DepthBlockedError):
            gsv._raise_if_blocked(_response(status=302, location='https://www.google.com/sorry/index'))

    def test_403_is_a_block(self):
        with pytest.raises(gsv.DepthBlockedError):
            gsv._raise_if_blocked(_response(status=403))

    def test_block_propagates_out_of_session_get(self):
        """The hook is only useful if requests lets the exception escape rather than swallowing it."""
        session = gsv._depth_session()

        class _InterstitialAdapter(HTTPAdapter):
            def send(self, request, **kwargs):
                return _response(url='https://www.google.com/sorry/index')

        session.mount('https://', _InterstitialAdapter())
        with pytest.raises(gsv.DepthBlockedError):
            session.get('https://www.google.com/maps/photometa/v1')


class TestPace:
    @pytest.fixture
    def slept(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gsv.time, 'sleep', lambda seconds: calls.append(seconds))
        return calls

    def test_no_sleep_when_throttle_disabled(self, slept, monkeypatch):
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.0)
        gsv._pace(gsv.time.monotonic())
        assert slept == []

    def test_no_sleep_on_the_first_request(self, slept, monkeypatch):
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 5.0)
        gsv._pace(None)
        assert slept == []

    def test_sleeps_the_remainder_of_the_interval(self, slept, monkeypatch):
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 5.0)
        gsv._pace(gsv.time.monotonic() - 1.0)
        assert len(slept) == 1
        # ~4s left of the 5s floor, plus up to 25% jitter.
        assert 3.9 <= slept[0] <= 5.3

    def test_no_sleep_when_the_interval_already_elapsed(self, slept, monkeypatch):
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 1.0)
        gsv._pace(gsv.time.monotonic() - 60.0)
        assert slept == []
