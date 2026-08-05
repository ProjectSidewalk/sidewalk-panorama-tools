"""Tests for gsv.download_depth_maps: ledger semantics, error taxonomy, budgets, and artifact output."""

import csv
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pytest
import requests

from conftest import default_depth_array, make_pano
from downloaders import gsv


def pano_infos(*pano_ids):
    return [{'pano_id': p, 'source': 'gsv'} for p in pano_ids]


def read_ledger(storage):
    path = os.path.join(storage, gsv.DEPTH_LOG_FILENAME)
    if not os.path.isfile(path):
        return None
    with open(path, newline='') as f:
        return list(csv.reader(f))


def artifact_path(storage, pano_id):
    return os.path.join(storage, pano_id[:2], pano_id + gsv.DEPTH_ARTIFACT_SUFFIX)


def test_success_saves_artifact_and_ledgers(tmp_path, fake_streetview):
    storage = str(tmp_path)
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())

    result = gsv.download_depth_maps(storage, pano_infos('abcdef'))

    assert result == (1, 0, 0, 1)
    path = artifact_path(storage, 'abcdef')
    assert os.path.isfile(path)
    with np.load(path) as d:
        assert d['depth'].dtype == np.float32
        assert d['depth'].shape == (2, 2)
        np.testing.assert_allclose(d['depth'], default_depth_array().astype(np.float32))
        assert float(d['heading']) == pytest.approx(1.25)
    assert read_ledger(storage) == [['pano_id', 'status'], ['abcdef', 'saved']]
    # No leftover temp file from the atomic write.
    assert not os.path.exists(path + '.part')
    assert os.stat(path).st_mode & 0o777 == 0o664


def test_missing_orientation_saved_as_nan(tmp_path, fake_streetview):
    storage = str(tmp_path)
    fake_streetview.find_panorama_by_id = \
        lambda pano_id, **kwargs: make_pano(default_depth_array(), heading=None, pitch=None, roll=None)

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (1, 0, 0, 1)
    with np.load(artifact_path(storage, 'abcdef')) as d:
        assert np.isnan(float(d['heading']))
        assert np.isnan(float(d['pitch']))
        assert np.isnan(float(d['roll']))


def test_pano_gone_ledgers_unavailable_and_never_retries(tmp_path, fake_streetview):
    storage = str(tmp_path)
    calls = []

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        return None

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    assert read_ledger(storage) == [['pano_id', 'status'], ['abcdef', 'unavailable']]
    assert not os.path.isfile(artifact_path(storage, 'abcdef'))

    # A later run must skip it without a new request.
    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 0, 1, 1)
    assert calls == ['abcdef']


def test_no_depth_payload_ledgers_unavailable(tmp_path, fake_streetview):
    storage = str(tmp_path)
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(depth_array=None)

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    assert read_ledger(storage) == [['pano_id', 'status'], ['abcdef', 'unavailable']]


@pytest.mark.parametrize('error', [requests.ConnectionError('boom'), ValueError('not json'), RuntimeError('bug')])
def test_errors_fail_without_ledgering_so_next_run_retries(tmp_path, fake_streetview, error):
    storage = str(tmp_path)
    calls = []

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        raise error

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    assert read_ledger(storage) == [['pano_id', 'status']]

    # Not ledgered, so a later run tries again.
    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    assert calls == ['abcdef', 'abcdef']


def test_existing_artifact_self_heals_missing_ledger(tmp_path, fake_streetview):
    storage = str(tmp_path)
    path = artifact_path(storage, 'abcdef')
    os.makedirs(os.path.dirname(path))
    with open(path, 'wb') as f:
        f.write(b'placeholder')

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 0, 1, 1)
    assert read_ledger(storage) == [['pano_id', 'status'], ['abcdef', 'saved']]


def test_ledgered_panos_skip_without_requests(tmp_path, fake_streetview):
    storage = str(tmp_path)
    with open(os.path.join(storage, gsv.DEPTH_LOG_FILENAME), 'w', newline='') as f:
        f.write('pano_id,status\naaaaaa,saved\nbbbbbb,unavailable\n')

    assert gsv.download_depth_maps(storage, pano_infos('aaaaaa', 'bbbbbb')) == (0, 0, 2, 2)


def test_malformed_ledger_rows_are_retried(tmp_path, fake_streetview):
    storage = str(tmp_path)
    # 'aaaaaa' has a crash-truncated row; 'bbbbbb' is intact.
    with open(os.path.join(storage, gsv.DEPTH_LOG_FILENAME), 'w', newline='') as f:
        f.write('pano_id,status\nbbbbbb,saved\naaaaaa\n')
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())

    assert gsv.download_depth_maps(storage, pano_infos('aaaaaa', 'bbbbbb')) == (1, 0, 1, 2)
    assert ['aaaaaa', 'saved'] in read_ledger(storage)


def test_max_requests_caps_http_attempts(tmp_path, fake_streetview):
    storage = str(tmp_path)
    calls = []

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        return make_pano(default_depth_array())

    fake_streetview.find_panorama_by_id = find

    result = gsv.download_depth_maps(storage, pano_infos('aaaaaa', 'bbbbbb', 'cccccc'), max_requests=1)

    assert calls == ['aaaaaa']
    assert result == (1, 0, 0, 1)


def test_max_runtime_stops_before_any_request(tmp_path, fake_streetview):
    storage = str(tmp_path)
    started_long_ago = datetime.now() - timedelta(minutes=10)

    result = gsv.download_depth_maps(storage, pano_infos('aaaaaa'), run_start_time=started_long_ago,
                                     max_runtime_minutes=5)

    assert result == (0, 0, 0, 0)


def test_max_runtime_does_not_block_skip_scanning(tmp_path, fake_streetview):
    storage = str(tmp_path)
    with open(os.path.join(storage, gsv.DEPTH_LOG_FILENAME), 'w', newline='') as f:
        f.write('pano_id,status\naaaaaa,saved\n')
    started_long_ago = datetime.now() - timedelta(minutes=10)

    # Already-resolved panos still count as skipped even when the runtime budget is exhausted.
    result = gsv.download_depth_maps(storage, pano_infos('aaaaaa', 'bbbbbb'), run_start_time=started_long_ago,
                                     max_runtime_minutes=5)

    assert result == (0, 0, 1, 1)


def test_missing_streetlevel_returns_zeros(tmp_path, monkeypatch):
    storage = str(tmp_path)
    # None in sys.modules makes `from streetlevel import streetview` raise ImportError.
    monkeypatch.setitem(sys.modules, 'streetlevel', None)
    monkeypatch.delitem(sys.modules, 'streetlevel.streetview', raising=False)

    assert gsv.download_depth_maps(storage, pano_infos('aaaaaa')) == (0, 0, 0, 0)
    assert read_ledger(storage) is None
