"""Tests for the depth-phase helpers: ledger reader, payload decode, artifact writer, ground-plane
derivation, and HTTP session hardening."""

import os
from types import SimpleNamespace

import numpy as np
import pytest
import requests
from requests.adapters import HTTPAdapter

from conftest import encode_depth_payload, make_pano, posix_only
from downloaders import gsv


def write_artifact(storage, pano_id, pano):
    """Call the writer the way the phase does: with the planes bundle that arrived alongside the pano."""
    gsv._write_depth_artifact(storage, pano_id, pano, pano.planes)


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


class TestDecodeDepthPlanes:
    """gsv._decode_depth_planes: the in-repo decode of Google's depth payload into the plane data
    streetlevel's parser computes with and then discards (#56). Pure bytes-in/arrays-out - no streetlevel."""

    PLANES = [{'n': [0.0, 0.0, 0.0], 'd': 0.0},           # index 0 = "no plane", never dereferenced
              {'n': [0.5, 0.25, -1.5], 'd': 2.75},
              {'n': [1.0, -2.0, 0.5], 'd': -4.5}]

    def test_decodes_header_indices_and_planes(self):
        payload = encode_depth_payload(self.PLANES, [0, 1, 2, 2, 1, 0], width=3, height=2)

        planes = gsv._decode_depth_planes(payload)

        assert planes.indices.dtype == np.uint8
        np.testing.assert_array_equal(planes.indices, [[0, 1, 2], [2, 1, 0]])
        assert planes.normals.dtype == np.float32
        np.testing.assert_allclose(planes.normals, [p['n'] for p in self.PLANES], rtol=1e-6)
        assert planes.distances.dtype == np.float32
        np.testing.assert_allclose(planes.distances, [p['d'] for p in self.PLANES], rtol=1e-6)

    def test_offset_is_read_as_uint8_even_when_the_first_index_is_nonzero(self):
        """The wire format's offset is a uint8 at byte 7. streetlevel 0.12.10 misreads it as a uint16
        spanning bytes 7-8, which only parses correctly when the first index byte happens to be 0 - real
        payloads don't guarantee that, so our decode must not inherit the misreading."""
        payload = encode_depth_payload(self.PLANES[:2], [1, 0, 1, 0], width=2, height=2)

        planes = gsv._decode_depth_planes(payload)

        np.testing.assert_array_equal(planes.indices, [[1, 0], [1, 0]])
        np.testing.assert_allclose(planes.distances, [0.0, 2.75], rtol=1e-6)

    def test_unpadded_base64_is_tolerated(self):
        """Google serves the payload without base64 padding; streetlevel re-pads before decoding, and so
        must we."""
        payload = encode_depth_payload(self.PLANES, [0, 1, 2, 2, 1, 0], width=3, height=2)
        stripped = payload.rstrip('=')

        np.testing.assert_array_equal(gsv._decode_depth_planes(stripped).indices, [[0, 1, 2], [2, 1, 0]])

    def test_truncated_payload_raises(self):
        """A payload cut mid-plane-list must raise, not silently return short arrays that would then be
        stored as a plausible-looking artifact."""
        import base64
        payload = encode_depth_payload(self.PLANES, [0, 1, 2, 2, 1, 0], width=3, height=2)
        raw = base64.urlsafe_b64decode(payload)

        truncated = base64.urlsafe_b64encode(raw[:-4]).decode()

        with pytest.raises(ValueError):
            gsv._decode_depth_planes(truncated)

    def test_header_shorter_than_eight_bytes_raises(self):
        import base64
        with pytest.raises(ValueError):
            gsv._decode_depth_planes(base64.urlsafe_b64encode(b'\x08\x00\x00').decode())

    def test_empty_plane_list_yields_empty_arrays(self):
        payload = encode_depth_payload([], [0, 0], width=2, height=1)

        planes = gsv._decode_depth_planes(payload)

        np.testing.assert_array_equal(planes.indices, [[0, 0]])
        assert planes.normals.shape == (0, 3)
        assert planes.distances.shape == (0,)


class TestWriteDepthArtifact:
    def test_roundtrip_and_atomicity(self, tmp_path):
        storage = str(tmp_path)
        depth = np.array([[1.0, -1.0], [2.5, 3.5]], dtype=np.float64)

        write_artifact(storage, 'abcdef', make_pano(depth, heading=0.5, pitch=None, roll=1.5))

        path = os.path.join(storage, 'ab', 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX)
        assert os.path.isfile(path)
        assert not os.path.exists(path + '.part')
        # No stray file from savez_compressed appending .npz to the temp name.
        assert sorted(os.listdir(os.path.join(storage, 'ab'))) == ['abcdef.depth.npz']
        with np.load(path) as d:
            assert d['depth'].dtype == np.float32
            # Stored in the JPEG's column order, i.e. streetlevel's array flipped in x (see #58).
            np.testing.assert_allclose(d['depth'], [[-1.0, 1.0], [3.5, 2.5]])
            assert float(d['heading']) == pytest.approx(0.5)
            assert np.isnan(float(d['pitch']))
            assert float(d['roll']) == pytest.approx(1.5)

    @posix_only
    def test_shard_dir_created_with_group_perms(self, tmp_path):
        storage = str(tmp_path)
        write_artifact(storage, 'abcdef', make_pano(np.zeros((1, 1))))
        mode = os.stat(os.path.join(storage, 'ab')).st_mode
        assert mode & 0o2777 == 0o2775

    def test_unmirrors_streetlevels_x_order(self, tmp_path):
        """streetlevel's decoder x-mirrors the payload (compute_depth_map writes column x to w-1-x), so the
        array it hands us is horizontally flipped relative to the pano JPEG. The artifact must store the
        image's column order, so a consumer indexing it with a pano_x needs no mirror correction. See #58.
        """
        storage = str(tmp_path)
        # Asymmetric in x so a mirror can't be missed: streetlevel's column order is [near, far].
        streetlevel_data = np.array([[2.0, 100.0], [3.0, 200.0]], dtype=np.float64)

        write_artifact(storage, 'abcdef', make_pano(streetlevel_data))

        path = os.path.join(storage, 'ab', 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX)
        with np.load(path) as d:
            # Image column order: [far, near] - the flip of what streetlevel delivered.
            np.testing.assert_allclose(d['depth'], [[100.0, 2.0], [200.0, 3.0]])

    def test_stamps_format_version(self, tmp_path):
        """Artifacts written before the #58 un-mirroring carry no version field; consumers use its presence to
        tell a corrected artifact from a mirrored one, and the value to tell plane-carrying v3 artifacts (#56)
        from v2 ones. Pinned as a literal: asserting against the constant would pass trivially after any bump."""
        storage = str(tmp_path)
        write_artifact(storage, 'abcdef', make_pano(np.zeros((1, 2))))
        path = os.path.join(storage, 'ab', 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX)
        with np.load(path) as d:
            assert int(d['format_version']) == 3 == gsv.DEPTH_ARTIFACT_FORMAT_VERSION

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

        write_artifact(storage, 'abcdef', make_pano(np.zeros((1, 1))))

        # The one-shot False must have been consumed by the guard at the shard dir — if any isdir call ever
        # interposes, this test would otherwise degrade into a passing no-op.
        assert guard_called == [os.path.join(storage, 'ab')]
        assert os.path.isfile(os.path.join(storage, 'ab', 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX))

    def test_losing_the_race_to_another_users_dir_is_not_an_error(self, tmp_path, monkeypatch):
        """The cross-user variant of the shard-dir race: the winner owns the dir, so the loser's chmod raises
        PermissionError. exist_ok already absorbed the makedirs collision; the chmod must not turn the same
        race into a failed pano (#51 review)."""
        storage = str(tmp_path)
        shard_dir = os.path.join(storage, 'ab')
        os.makedirs(shard_dir)
        real_isdir, guard_called = os.path.isdir, []

        def racy_isdir(path):
            if not guard_called:
                guard_called.append(path)
                return False
            return real_isdir(path)

        real_chmod = os.chmod

        def other_users_dir_chmod(path, mode, **kwargs):
            # Only the shard dir belongs to the other user; the .part file chmod must keep working.
            if os.path.normpath(path) == os.path.normpath(shard_dir):
                raise PermissionError(1, 'Operation not permitted', path)
            return real_chmod(path, mode, **kwargs)

        monkeypatch.setattr(gsv.os.path, 'isdir', racy_isdir)
        monkeypatch.setattr(gsv.os, 'chmod', other_users_dir_chmod)

        write_artifact(storage, 'abcdef', make_pano(np.zeros((1, 1))))

        assert guard_called == [shard_dir]
        assert os.path.isfile(os.path.join(shard_dir, 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX))

    def test_part_file_is_cleaned_up_when_the_write_fails(self, tmp_path, monkeypatch):
        """Nothing else ever sweeps .part files, so a failed write must not leave one on the store."""
        storage = str(tmp_path)

        def boom(*args, **kwargs):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(gsv.np, 'savez_compressed', boom)

        with pytest.raises(OSError):
            write_artifact(storage, 'abcdef', make_pano(np.zeros((2, 2))))
        assert os.listdir(os.path.join(storage, 'ab')) == []

    def test_v3_stores_planes(self, tmp_path):
        """v3 artifacts carry Google's plane list verbatim - per-pixel indices, normals, and offsets - which
        v2 discarded (#56). A consumer can reconstruct camera height and ground tilt from these."""
        storage = str(tmp_path)
        depth = np.array([[-1.0, 4.5], [3.25, 10.0]])  # streetlevel (x-mirrored) order
        planes = SimpleNamespace(indices=np.array([[1, 0], [2, 1]], dtype=np.uint8),  # payload order
                                 normals=np.array([[0.0, 0.0, 0.0], [0.1, -0.2, -0.97], [0.7, 0.7, 0.0]]),
                                 distances=np.array([0.0, 2.6, -8.0]))

        gsv._write_depth_artifact(storage, 'abcdef', make_pano(depth, planes=planes), planes)

        with np.load(os.path.join(storage, 'ab', 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX)) as d:
            assert d['plane_indices'].dtype == np.uint8
            np.testing.assert_array_equal(d['plane_indices'], [[1, 0], [2, 1]])
            assert d['planes_n'].dtype == np.float32
            np.testing.assert_allclose(d['planes_n'], planes.normals, rtol=1e-6)
            assert d['planes_d'].dtype == np.float32
            np.testing.assert_allclose(d['planes_d'], planes.distances, rtol=1e-6)

    def test_plane_indices_align_with_the_unmirrored_depth(self, tmp_path):
        """The depth raster is stored x-flipped (streetlevel order -> JPEG order, #58), but plane indices
        come from the raw payload, whose column order already IS the JPEG order - so they are stored
        verbatim, no flip. Asymmetric fixture: flipping the indices too (the tempting 'consistency' bug)
        breaks the invariant that index 0 sits exactly where the stored depth is -1."""
        storage = str(tmp_path)
        depth = np.array([[-1.0, 6.0, 7.0]])              # streetlevel order: no-plane pixel on the LEFT
        indices = np.array([[1, 1, 0]], dtype=np.uint8)   # payload order: no-plane pixel on the RIGHT
        planes = SimpleNamespace(indices=indices, normals=np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]),
                                 distances=np.array([0.0, 2.5]))

        gsv._write_depth_artifact(storage, 'abcdef', make_pano(depth, planes=planes), planes)

        with np.load(os.path.join(storage, 'ab', 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX)) as d:
            np.testing.assert_array_equal(d['plane_indices'], [[1, 1, 0]])
            np.testing.assert_array_equal(d['plane_indices'] == 0, d['depth'] == -1)

    def test_planes_reconstruct_the_stored_depth(self, tmp_path):
        """The operational definition of the stored frame: depth[r, c] == |d_i / (v(r, c) . n_i)| for
        i = plane_indices[r, c] > 0, with v(r, c) the unit ray for theta = (h-r-0.5)/h*pi and
        phi = (w-c-0.5)/w*2pi + pi/2. Same mirror fixture as tests/test_streetlevel_api.py: the magnitudes
        encode the ray azimuth, so this fails if any stored field is flipped or the formula drifts."""
        storage = str(tmp_path)
        # streetlevel hands the decode back x-mirrored; payload/JPEG order is its flip (#58).
        streetlevel_depth = np.array([[-1.0, 1.0, 3.0, -1.0]])
        planes = SimpleNamespace(indices=np.array([[0, 1, 1, 0]], dtype=np.uint8),
                                 normals=np.array([[0.0, 0.0, 0.0], [2.0, 1.0, 0.0]]),
                                 distances=np.array([0.0, 1.5 * np.sqrt(2)]))

        gsv._write_depth_artifact(storage, 'abcdef', make_pano(streetlevel_depth, planes=planes), planes)

        with np.load(os.path.join(storage, 'ab', 'abcdef' + gsv.DEPTH_ARTIFACT_SUFFIX)) as d:
            depth, indices = d['depth'], d['plane_indices']
            normals, distances = d['planes_n'], d['planes_d']
        h, w = depth.shape
        reconstructed = np.full((h, w), -1.0)
        for r in range(h):
            theta = (h - r - 0.5) / h * np.pi
            for c in range(w):
                if indices[r, c] == 0:
                    continue
                phi = (w - c - 0.5) / w * 2 * np.pi + np.pi / 2
                ray = (np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta))
                reconstructed[r, c] = abs(distances[indices[r, c]] / np.dot(ray, normals[indices[r, c]]))
        np.testing.assert_allclose(reconstructed, depth, rtol=1e-5)

    @pytest.mark.parametrize('bad_planes', [
        None,
        SimpleNamespace(indices=np.zeros((5, 5), dtype=np.uint8),
                        normals=np.zeros((1, 3), dtype=np.float32), distances=np.zeros(1, dtype=np.float32)),
    ], ids=['missing', 'shape-mismatch'])
    def test_missing_or_mismatched_planes_refuse_to_write(self, tmp_path, bad_planes):
        """An artifact without matching plane fields would be a malformed v3: refuse before the .part file
        is even opened, so nothing lands on the store."""
        storage = str(tmp_path)

        with pytest.raises(ValueError):
            gsv._write_depth_artifact(storage, 'abcdef', make_pano(np.zeros((2, 2)), planes=bad_planes),
                                      bad_planes)

        assert not os.path.exists(os.path.join(storage, 'ab'))


class TestGroundPlane:
    """ground_plane_from_artifact / camera_height_from_artifact: the derivation the artifact deliberately
    does NOT bake in - the plane list is stored verbatim so the ground heuristic stays fixable in code,
    unlike a scalar frozen into millions of .npz files."""

    def artifact(self, indices, normals, distances):
        return {'plane_indices': np.asarray(indices, dtype=np.uint8),
                'planes_n': np.asarray(normals, dtype=np.float32),
                'planes_d': np.asarray(distances, dtype=np.float32)}

    def test_most_vertical_referenced_plane_wins(self):
        artifact = self.artifact([[1, 2]],
                                 [[0.0, 0.0, 0.0], [0.1, 0.0, -0.99], [1.0, 0.0, -1.0]],
                                 [0.0, 2.5, 4.0])

        normal, distance, index = gsv.ground_plane_from_artifact(artifact)

        assert index == 1
        length = np.linalg.norm([0.1, 0.0, -0.99])
        assert distance == pytest.approx(2.5 / length)
        np.testing.assert_allclose(normal, np.array([0.1, 0.0, -0.99]) / length, rtol=1e-6)

    def test_sentinel_horizontal_and_unreferenced_planes_are_never_chosen(self):
        # Plane 0 is the no-plane sentinel (vertical normal, but excluded); plane 1 is referenced but
        # horizontal (a facade); plane 2 is perfectly vertical but no pixel references it.
        artifact = self.artifact([[0, 1]],
                                 [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
                                 [9.0, 3.0, 2.5])

        assert gsv.ground_plane_from_artifact(artifact) is None

    def test_below_threshold_returns_none_and_threshold_is_tunable(self):
        # Normal (2, 0, 1): verticality 1/sqrt(5) ~ 0.45, under the default 0.7 floor.
        artifact = self.artifact([[1]], [[0.0, 0.0, 0.0], [2.0, 0.0, 1.0]], [0.0, 3.0])

        assert gsv.ground_plane_from_artifact(artifact) is None
        assert gsv.ground_plane_from_artifact(artifact, min_vertical=0.4)[2] == 1

    def test_out_of_range_index_is_ignored(self):
        artifact = self.artifact([[3, 1]], [[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 2.5])

        assert gsv.ground_plane_from_artifact(artifact)[2] == 1

    def test_camera_height_is_offset_over_norm(self):
        # Non-unit normal and negative d: height is |d| / ||n||. Sign-insensitive on purpose - the up/down
        # sign convention of Google's frame is not something these helpers should have an opinion about.
        artifact = self.artifact([[1]], [[0.0, 0.0, 0.0], [0.0, 0.0, 2.0]], [0.0, -5.0])

        assert gsv.camera_height_from_artifact(artifact) == pytest.approx(2.5)

    def test_camera_height_default_when_no_ground(self):
        artifact = self.artifact([[1]], [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], [0.0, 3.0])

        assert gsv.camera_height_from_artifact(artifact) is None
        assert gsv.camera_height_from_artifact(artifact, default=2.4) == 2.4


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


class TestGetResponseProxyContract:
    """What actually reaches requests' proxy selection on the _get_response path (#51 review).

    TestNormalizeProxies pins the helper; these pin the contract. A capturing session records the proxies
    kwarg _get_response passes, and that value is then run through the same machinery Session.request applies
    to it — merge_environment_settings (env fallback + merge_setting against the session defaults), then the
    adapter's select_proxy — so the assertions hold exactly where requests decides proxy vs direct.
    """

    GSV_HTTPS_URL = 'https://maps.google.com/cbk?output=tile'

    def _capture(self, monkeypatch, config):
        monkeypatch.setattr(gsv, '_proxies', gsv._normalize_proxies(config))
        captured = {}

        class CapturingSession:
            def get(self, url, **kwargs):
                captured.update(kwargs)
                return requests.Response()

        return captured, CapturingSession()

    def _selected_proxy(self, monkeypatch, config, url=GSV_HTTPS_URL, env_proxies=None):
        # Environment reads go through get_environ_proxies; pinning it keeps the box's real env (and, on
        # Windows, registry) proxy settings out of the test while every merge step stays requests' own code.
        monkeypatch.setattr(requests.sessions, 'get_environ_proxies',
                            lambda url, no_proxy=None: dict(env_proxies or {}))
        captured, session = self._capture(monkeypatch, config)
        gsv._get_response(url, session)
        with requests.Session() as merging_session:
            merged = merging_session.merge_environment_settings(url, captured['proxies'],
                                                                None, None, None)['proxies']
        return requests.utils.select_proxy(url, merged)

    def test_placeholder_config_reaches_selection_as_no_proxy(self, monkeypatch):
        assert self._selected_proxy(monkeypatch, {'http': 'http://', 'https': 'http://'}) is None

    def test_configured_proxy_is_selected_for_the_gsv_url(self, monkeypatch):
        proxy = 'http://proxy.example:8080'
        assert self._selected_proxy(monkeypatch, {'http': proxy, 'https': proxy}) == proxy

    def test_mixed_config_goes_direct_for_https_but_proxied_for_http(self, monkeypatch):
        # The config test_real_proxy_survives_a_placeholder_on_the_other_key blesses: the https placeholder
        # means unset, so an https request goes direct (matching the _depth_session path's semantics)...
        config = {'http': 'http://proxy.example:8080', 'https': 'http://'}
        assert self._selected_proxy(monkeypatch, config) is None
        # ...while a plain-http request uses the configured proxy.
        assert self._selected_proxy(monkeypatch, config,
                                    url='http://maps.google.com/cbk') == 'http://proxy.example:8080'

    def test_placeholders_do_not_smother_environment_proxies(self, monkeypatch):
        """Placeholder == unset must hold all the way down: with only placeholders configured, an env proxy
        applies, exactly as on the _depth_session path. Passing the raw Nones through blocked
        merge_environment_settings' setdefault, silently disabling env proxies (#51 review)."""
        assert self._selected_proxy(monkeypatch, {'http': 'http://', 'https': 'http://'},
                                    env_proxies={'https': 'http://envproxy.example:3128'}) \
               == 'http://envproxy.example:3128'

    def test_passes_a_filtered_copy_not_the_module_global(self, monkeypatch):
        captured, session = self._capture(monkeypatch, {'http': 'http://proxy.example:8080', 'https': None})
        gsv._get_response(self.GSV_HTTPS_URL, session)
        # Placeholder keys are absent, not None: merge_setting would drop the Nones anyway, but a None value
        # blocks the env fallback and is one requests version away from meaning something else.
        assert captured['proxies'] == {'http': 'http://proxy.example:8080'}
        # requests setdefaults env proxies into this dict in place; the module global must be immune.
        captured['proxies']['no'] = 'example.org'
        assert gsv._proxies == {'http': 'http://proxy.example:8080', 'https': None}


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
