"""Tests for gsv.download_depth_maps: ledger semantics, error taxonomy, budgets, and artifact output."""

import csv
import logging
import os
import sys
import time

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


def full_disk(*args, **kwargs):
    raise OSError(28, 'No space left on device')


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
        # Stored in the JPEG's column order, i.e. streetlevel's array flipped in x (see #58).
        np.testing.assert_allclose(d['depth'], [[4.5, -1.0], [10.0, 3.25]])
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


def test_non_2d_depth_payload_ledgers_unavailable_and_never_retries(tmp_path, fake_streetview):
    """A depth payload that isn't an (h, w) grid is unusable, and that's a property of the pano, not of the
    network. It must take the same path as no-depth - ledgered 'unavailable' - rather than raise inside the
    artifact write's [:, ::-1], where the catch-all would count it transient and re-request it every run
    forever."""
    storage = str(tmp_path)
    calls = []

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        return make_pano(np.array([1.0, 2.0]))  # 1-D: no column axis to unmirror

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    assert read_ledger(storage) == [['pano_id', 'status'], ['abcdef', 'unavailable']]
    assert not os.path.isfile(artifact_path(storage, 'abcdef'))

    # Resolved, so a later run must skip it without a new request.
    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 0, 1, 1)
    assert calls == ['abcdef']


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
    started_long_ago = time.monotonic() - 600.0

    result = gsv.download_depth_maps(storage, pano_infos('aaaaaa'), run_start_monotonic=started_long_ago,
                                     max_runtime_minutes=5)

    assert result == (0, 0, 0, 0)


def test_runtime_budget_uses_the_monotonic_clock(tmp_path, fake_streetview, monkeypatch):
    """An NTP step or DST transition perturbs datetime.now() - backwards extends the run, forwards ends it
    early. The budget must come from time.monotonic (#51), which _pace() a few lines away already uses."""
    storage = str(tmp_path)
    fake_now = [1000.0]
    monkeypatch.setattr(gsv.time, 'monotonic', lambda: fake_now[0])

    def find(pano_id, **kwargs):
        fake_now[0] += 600.0  # each request "takes" 10 minutes of monotonic time
        return make_pano(default_depth_array())

    fake_streetview.find_panorama_by_id = find

    result = gsv.download_depth_maps(storage, pano_infos('aaaaaa', 'bbbbbb'),
                                     run_start_monotonic=fake_now[0], max_runtime_minutes=5)

    # First pano fits the budget; the 10 monotonic minutes it consumed must stop the second.
    assert result == (1, 0, 0, 1)


def test_resolved_panos_are_counted_even_when_the_budget_is_exhausted(tmp_path, fake_streetview):
    """log.csv's skipped column must describe the whole corpus, not just what was scanned before the budget ran out.

    The ledger skips are counted up front for exactly this reason: 'dddddd' is unresolved and sits first, so a
    count that accrued during the fetch loop would stop at 0 and make a fully-backfilled city look untouched.
    """
    storage = str(tmp_path)
    with open(os.path.join(storage, gsv.DEPTH_LOG_FILENAME), 'w', newline='') as f:
        f.write('pano_id,status\naaaaaa,saved\nbbbbbb,saved\ncccccc,unavailable\n')
    started_long_ago = time.monotonic() - 600.0

    result = gsv.download_depth_maps(storage, pano_infos('dddddd', 'aaaaaa', 'bbbbbb', 'cccccc'),
                                     run_start_monotonic=started_long_ago, max_runtime_minutes=5)

    assert result == (0, 0, 3, 3)


def test_storage_failure_is_transient_not_fatal(tmp_path, fake_streetview, monkeypatch):
    """A failed artifact write must not escape.

    An escaping OSError would fail the whole run and forfeit the rest of the phase's budget over one pano's
    storage hiccup. (log.csv itself is safe either way - DownloadRunner pads the row to 18 fields in a finally.)
    """
    storage = str(tmp_path)
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())
    monkeypatch.setattr(gsv, '_write_depth_artifact', full_disk)

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


def test_unwritable_ledger_skips_the_phase_without_crashing(tmp_path, fake_streetview, monkeypatch, capsys):
    """A store that's full or read-only at phase start must not take the run down with it either."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv.csv, 'writer', lambda *args, **kwargs: _FullDiskWriter())

    # Writing the ledger header is the first thing the phase does on a fresh store.
    assert gsv.download_depth_maps(storage, pano_infos('aaaaaa')) == (0, 0, 0, 0)
    # Carries the WARNING token so an ops grep for storage trouble matches this at-start message the same as
    # the mid-run ones - a store unmounted before the run is likelier than one filling during it.
    assert 'WARNING' in capsys.readouterr().out


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
    monkeypatch.setattr(gsv, '_write_depth_artifact', full_disk)

    gsv.download_depth_maps(storage, many_pano_infos(50))

    out = capsys.readouterr().out
    assert 'WARNING' in out
    # The parenthesized per-class breakdown is unique to the end-of-phase WARNING - the bare
    # '3 consecutive failures' substring is already printed in-loop by the breaker trip, so asserting it
    # wouldn't pin the summary at all.
    assert '3 consecutive failures (3 storage)' in out
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
    # Pin the new detail on the WARNING line itself - the in-loop stop print always interpolated the error, so
    # asserting over the whole stdout would pass against pre-#60 code unmodified - and pin that a block is
    # never dressed up as a breaker trip.
    warning = next(line for line in out.splitlines() if 'WARNING' in line)
    assert 'redirected to' in warning
    assert 'consecutive failures' not in out


def test_blocked_warning_leads_with_the_action_and_truncates_the_url(tmp_path, fake_streetview, capsys):
    """Google's interstitial redirects carry 600+ character URLs. The WARNING must put the actionable sentence
    first and cap the error detail, or the one message that used to be readable drowns in urlencoding."""
    storage = str(tmp_path)
    long_url = 'https://www.google.com/sorry/index?continue=' + 'x' * 600

    def find(pano_id, **kwargs):
        raise gsv.DepthBlockedError('redirected to %s' % long_url)

    fake_streetview.find_panorama_by_id = find

    gsv.download_depth_maps(storage, many_pano_infos(5))

    out = capsys.readouterr().out
    warning = next(line for line in out.splitlines() if 'WARNING' in line)
    assert warning.index('check for a rate limit') < warning.index('redirected to')
    assert 'x' * 300 not in warning


def test_breaker_warning_breaks_a_mixed_streak_down_by_class(tmp_path, fake_streetview, monkeypatch, capsys):
    """2x ENOSPC then 1x ConnectionError: the error that trips the breaker is the minority class, so naming
    only the last error would send the reader to the network while the disk is full. The WARNING must carry
    per-class counts over the streak (#60 review, IMPORTANT 1)."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 3)
    calls = []

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        if len(calls) >= 3:
            raise requests.ConnectionError('HTTPSConnectionPool: Max retries exceeded')
        return make_pano(default_depth_array())

    fake_streetview.find_panorama_by_id = find
    monkeypatch.setattr(gsv, '_write_depth_artifact', full_disk)

    gsv.download_depth_maps(storage, many_pano_infos(50))

    out = capsys.readouterr().out
    assert '3 consecutive failures (2 storage, 1 network)' in out
    assert 'Max retries exceeded' in out


def test_failures_then_runtime_expiry_still_warn_on_stdout(tmp_path, fake_streetview, monkeypatch, capsys,
                                                           caplog):
    """Depth runs last and shares --max-runtime, so a typical depth window is minutes: the store fills, a few
    panos fail, and the clock runs out long before the breaker's threshold. That must not read as a clean
    budget stop (#60 review, IMPORTANT 2)."""
    storage = str(tmp_path)
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())
    monkeypatch.setattr(gsv, '_write_depth_artifact', full_disk)

    # The budget clock is monotonic since #51; every read (budget check, pace, request stamp) advances the
    # fake by two minutes, so a couple of failures land before the 5-minute budget trips.
    ticks = iter(range(0, 60000, 120))

    monkeypatch.setattr(gsv.time, 'monotonic', lambda: float(next(ticks)))

    with caplog.at_level(logging.DEBUG):
        gsv.download_depth_maps(storage, many_pano_infos(50), run_start_monotonic=0.0, max_runtime_minutes=5)

    out = capsys.readouterr().out
    assert 'Max runtime' in out
    assert 'WARNING' in out
    assert 'No space left on device' in out
    assert 'stop_reason=max-runtime' in caplog.text


def test_failures_then_request_budget_still_warn_on_stdout(tmp_path, fake_streetview, monkeypatch, capsys,
                                                           caplog):
    """Same shape as the runtime budget: failures followed by a max_requests stop must still warn."""
    storage = str(tmp_path)

    def find(pano_id, **kwargs):
        raise requests.ConnectionError('network down')

    fake_streetview.find_panorama_by_id = find

    with caplog.at_level(logging.DEBUG):
        gsv.download_depth_maps(storage, many_pano_infos(50), max_requests=3)

    out = capsys.readouterr().out
    assert 'Max depth requests' in out
    assert 'WARNING' in out
    assert 'network down' in out
    assert 'stop_reason=max-requests' in caplog.text


def test_self_heal_ledger_failures_are_not_silent(tmp_path, fake_streetview, monkeypatch, capsys):
    """Panos whose artifacts exist but whose ledger write fails used to produce zero stdout at all - a
    completely full store looked like a healthy, fully-backfilled city (#60 review, IMPORTANT 3)."""
    storage = str(tmp_path)
    with open(os.path.join(storage, gsv.DEPTH_LOG_FILENAME), 'w', newline='') as f:
        f.write('pano_id,status\n')
    infos = many_pano_infos(40)
    for info in infos:
        path = artifact_path(storage, info['pano_id'])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'placeholder')
    monkeypatch.setattr(gsv.csv, 'writer', lambda *args, **kwargs: _FullDiskWriter())

    result = gsv.download_depth_maps(storage, infos)

    out = capsys.readouterr().out
    assert result == (0, 0, 40, 40)
    assert 'WARNING' in out
    assert 'No space left on device' in out


def test_storage_failures_skip_the_retreat_sleeps(tmp_path, fake_streetview, monkeypatch):
    """The retreat schedule waits for a network blip or rate limit to clear, but a full disk cannot clear
    itself: a storage streak must march straight to the breaker instead of burning up to 7.5 minutes of a
    shared --max-runtime window (#60 review, finding 7)."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 4)
    monkeypatch.setattr(gsv, 'DEPTH_RETREAT_SCHEDULE', {2: 30})
    sleeps = []
    monkeypatch.setattr(gsv.time, 'sleep', lambda seconds: sleeps.append(seconds))
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())
    monkeypatch.setattr(gsv, '_write_depth_artifact', full_disk)

    assert gsv.download_depth_maps(storage, many_pano_infos(10)) == (0, 4, 0, 4)
    assert sleeps == []


def test_network_failures_keep_the_retreat_sleeps(tmp_path, fake_streetview, monkeypatch):
    """The counterpart guard: a network streak still gets the escalating back-off before the breaker."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 4)
    monkeypatch.setattr(gsv, 'DEPTH_RETREAT_SCHEDULE', {2: 30})
    sleeps = []
    monkeypatch.setattr(gsv.time, 'sleep', lambda seconds: sleeps.append(seconds))

    def find(pano_id, **kwargs):
        raise requests.ConnectionError('network down')

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, many_pano_infos(10)) == (0, 4, 0, 4)
    assert sleeps == [30]


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
