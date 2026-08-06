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


def many_pano_infos(count):
    return pano_infos(*['pano%03d' % i for i in range(count)])


@pytest.fixture(autouse=True)
def no_retreat_sleeps(monkeypatch):
    """Keep the escalating-retreat sleeps out of the test suite's wall clock."""
    monkeypatch.setattr(gsv, 'DEPTH_RETREAT_SCHEDULE', {})


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
    if os.name == 'posix':
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

    # Which pano is picked is deliberately not fixed - candidates are shuffled (see the starvation test below).
    assert len(calls) == 1
    assert result == (1, 0, 0, 1)


def test_max_runtime_stops_before_any_request(tmp_path, fake_streetview):
    storage = str(tmp_path)
    started_long_ago = datetime.now() - timedelta(minutes=10)

    result = gsv.download_depth_maps(storage, pano_infos('aaaaaa'), run_start_time=started_long_ago,
                                     max_runtime_minutes=5)

    assert result == (0, 0, 0, 0)


def test_resolved_panos_are_counted_even_when_the_budget_is_exhausted(tmp_path, fake_streetview):
    """log.csv's skipped column must describe the whole corpus, not just what was scanned before the budget ran out.

    The ledger skips are counted up front for exactly this reason: 'dddddd' is unresolved and sits first, so a
    count that accrued during the fetch loop would stop at 0 and make a fully-backfilled city look untouched.
    """
    storage = str(tmp_path)
    with open(os.path.join(storage, gsv.DEPTH_LOG_FILENAME), 'w', newline='') as f:
        f.write('pano_id,status\naaaaaa,saved\nbbbbbb,saved\ncccccc,unavailable\n')
    started_long_ago = datetime.now() - timedelta(minutes=10)

    result = gsv.download_depth_maps(storage, pano_infos('dddddd', 'aaaaaa', 'bbbbbb', 'cccccc'),
                                     run_start_time=started_long_ago, max_runtime_minutes=5)

    assert result == (0, 0, 3, 3)


def test_storage_failure_is_transient_not_fatal(tmp_path, fake_streetview, monkeypatch):
    """A failed artifact write must not escape.

    DownloadRunner writes the depth and total-duration columns after this returns, so an escaping OSError left a
    12-field line in log.csv where scraper-log-analyzer expects 18 - and killed the run.
    """
    storage = str(tmp_path)
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())

    def boom(*args, **kwargs):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(gsv, '_write_depth_artifact', boom)

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    # Not ledgered, so the pano retries once there's space again.
    assert read_ledger(storage) == [['pano_id', 'status']]


class _FullDiskWriter:
    def writerow(self, row):
        raise OSError(28, 'No space left on device')


def test_ledger_append_failure_is_transient_not_fatal(tmp_path, fake_streetview, monkeypatch):
    storage = str(tmp_path)
    with open(os.path.join(storage, gsv.DEPTH_LOG_FILENAME), 'w', newline='') as f:
        f.write('pano_id,status\n')
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())
    monkeypatch.setattr(gsv.csv, 'writer', lambda *args, **kwargs: _FullDiskWriter())

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    # The artifact landed before the ledger append failed; next run's self-heal registers it without re-fetching.
    assert os.path.isfile(artifact_path(storage, 'abcdef'))


def test_unreadable_ledger_skips_the_phase_without_re_requesting_everything(tmp_path, fake_streetview, monkeypatch):
    """Degrading to 'nothing is resolved' would re-request the whole corpus against an already-sick store."""
    storage = str(tmp_path)
    calls = []
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: calls.append(pano_id)

    def boom(path):
        raise OSError(5, 'Input/output error')

    monkeypatch.setattr(gsv, '_load_depth_log', boom)

    assert gsv.download_depth_maps(storage, pano_infos('aaaaaa', 'bbbbbb')) == (0, 0, 0, 0)
    assert calls == []


def test_unwritable_ledger_skips_the_phase_without_crashing(tmp_path, fake_streetview, monkeypatch):
    """A store that's full or read-only at phase start must not take the run down with it either."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv.csv, 'writer', lambda *args, **kwargs: _FullDiskWriter())

    # Writing the ledger header is the first thing the phase does on a fresh store.
    assert gsv.download_depth_maps(storage, pano_infos('aaaaaa')) == (0, 0, 0, 0)


def test_circuit_breaker_stops_the_phase(tmp_path, fake_streetview, monkeypatch):
    """A run that hits a wall must stand down instead of spending its whole budget on it, every night."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 3)
    calls = []

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        raise requests.ConnectionError('network down')

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, many_pano_infos(50)) == (0, 3, 0, 3)
    assert len(calls) == 3


def test_any_resolved_outcome_resets_the_breaker(tmp_path, fake_streetview, monkeypatch):
    """Intermittent failures must not accumulate into a false trip across an otherwise healthy run."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 3)
    seen = []

    def find(pano_id, **kwargs):
        seen.append(pano_id)
        if len(seen) % 3 == 0:  # fail, fail, succeed - never three in a row
            return make_pano(default_depth_array())
        raise requests.ConnectionError('flaky')

    fake_streetview.find_panorama_by_id = find

    success, fail, skipped, total = gsv.download_depth_maps(storage, many_pano_infos(9))
    assert len(seen) == 9, "the breaker tripped despite a success in every window"
    assert (success, fail, skipped, total) == (3, 6, 0, 9)


def test_breaker_trip_on_storage_failures_reports_the_actual_cause(tmp_path, fake_streetview, monkeypatch,
                                                                   capsys):
    """A full or unmounted store is the most likely way this phase fails at scale, and it trips the same
    breaker as network failures. The end-of-run warning must name the real error instead of sending whoever
    reads the cron mail to look for a Google rate limit (#50)."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 3)
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())

    def full_disk(*args, **kwargs):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(gsv, '_write_depth_artifact', full_disk)

    gsv.download_depth_maps(storage, many_pano_infos(50))

    out = capsys.readouterr().out
    assert 'WARNING' in out
    assert '3 consecutive failures' in out
    assert 'No space left on device' in out
    assert 'Google stopped answering' not in out


def test_block_stop_still_blames_google(tmp_path, fake_streetview, capsys):
    """When the phase stops because of an interstitial, the rate-limit warning is the correct one."""
    storage = str(tmp_path)

    def find(pano_id, **kwargs):
        raise gsv.DepthBlockedError('redirected to https://www.google.com/sorry/index')

    fake_streetview.find_panorama_by_id = find

    gsv.download_depth_maps(storage, many_pano_infos(5))

    out = capsys.readouterr().out
    assert 'WARNING' in out
    assert 'Google stopped answering' in out


@pytest.mark.parametrize('error', [
    gsv.DepthBlockedError('redirected to https://www.google.com/sorry/index'),
    requests.exceptions.RetryError('too many 429s'),
])
def test_google_refusing_requests_stops_the_phase_immediately(tmp_path, fake_streetview, error):
    """A block is a verdict on the endpoint, not on one pano, so it shouldn't cost 25 more requests to notice."""
    storage = str(tmp_path)
    calls = []

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        raise error

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, many_pano_infos(50)) == (0, 1, 0, 1)
    assert len(calls) == 1
    # Nothing permanent is concluded from a block; every pano retries next run.
    assert read_ledger(storage) == [['pano_id', 'status']]


def test_persistent_failures_cannot_starve_the_request_budget(tmp_path, fake_streetview, monkeypatch):
    """Iteration order is otherwise stable, so a head block of always-failing panos would monopolise
    --max-depth-requests run after run and the backfill would never reach anything behind it."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 1000)
    attempted = set()

    def find(pano_id, **kwargs):
        attempted.add(pano_id)
        raise requests.ConnectionError('always fails')

    fake_streetview.find_panorama_by_id = find

    panos = many_pano_infos(50)
    for _ in range(10):
        gsv.download_depth_maps(storage, panos, max_requests=5)

    # Unshuffled this would be exactly the same 5 ids on all ten runs.
    assert len(attempted) > 5


def test_missing_streetlevel_returns_zeros(tmp_path, monkeypatch):
    storage = str(tmp_path)
    # None in sys.modules makes `from streetlevel import streetview` raise ImportError.
    monkeypatch.setitem(sys.modules, 'streetlevel', None)
    monkeypatch.delitem(sys.modules, 'streetlevel.streetview', raising=False)

    assert gsv.download_depth_maps(storage, pano_infos('aaaaaa')) == (0, 0, 0, 0)
    assert read_ledger(storage) is None
