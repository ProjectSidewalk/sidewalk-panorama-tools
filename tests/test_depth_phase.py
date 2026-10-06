"""Tests for gsv.download_depth_maps: ledger semantics, error taxonomy, budgets, and artifact output."""

import csv
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
import requests

from conftest import default_depth_array, make_pano
from test_gsv_stitcher import probe_retry_error, probe_retry_error_at
from downloaders import gsv


def pano_infos(*pano_ids):
    return [{'pano_id': p, 'source': 'gsv'} for p in pano_ids]


def many_pano_infos(count):
    return pano_infos(*['pano%03d' % i for i in range(count)])


# The production retreat schedule, captured before no_retreat_sleeps empties it: the outage floor (#177 review)
# is defined as its first step, and the tests that pin the floor must read the real one.
REAL_RETREAT_SCHEDULE = dict(gsv.DEPTH_RETREAT_SCHEDULE)
OUTAGE_FLOOR = min(REAL_RETREAT_SCHEDULE)


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
        # v3: the plane list rides along (#56) - indices in payload order (index 0 exactly where the
        # stored depth is -1), normals and offsets verbatim. Version pinned as a literal.
        np.testing.assert_array_equal(d['plane_indices'], [[1, 0], [1, 1]])
        assert d['planes_n'].shape == (2, 3)
        np.testing.assert_allclose(d['planes_d'], [0.0, 2.5])
        assert int(d['format_version']) == 3
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


@pytest.mark.parametrize('planes', [
    None,
    SimpleNamespace(indices=np.zeros((9, 9), dtype=np.uint8),
                    normals=np.zeros((1, 3), dtype=np.float32), distances=np.zeros(1, dtype=np.float32)),
], ids=['missing', 'shape-mismatch'])
def test_depth_present_but_planes_missing_is_transient(tmp_path, fake_streetview, planes):
    """A depth raster with no matching plane data can only mean the payload path or wire format drifted
    upstream. Depth exists, so 'unavailable' would be a lie - the pano must count as a transient failure,
    stay unledgered, and retry next run (#56)."""
    storage = str(tmp_path)
    calls = []

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        return make_pano(default_depth_array(), planes=planes)

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    assert read_ledger(storage) == [['pano_id', 'status']]
    assert not os.path.isfile(artifact_path(storage, 'abcdef'))

    # Not ledgered, so a later run tries again.
    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    assert calls == ['abcdef', 'abcdef']


def test_malformed_payload_is_classed_unexpected_not_network(tmp_path, fake_streetview, monkeypatch, capsys):
    """A payload this scraper decoded and then rejected is a data fault, not a network one. The distinction is
    load-bearing: the 'network' class also drives the escalating retreat sleeps, so misfiling these would burn
    up to 7.5 minutes of a shared --max-runtime window per streak waiting for a "blip" that is really a wire
    format change. DepthPayloadError is a RuntimeError precisely so it lands in 'unexpected' (#56 review)."""
    storage = str(tmp_path)
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 2)

    def find(pano_id, **kwargs):
        raise gsv.DepthPayloadError('depth payload truncated: 40 bytes, need 88')

    fake_streetview.find_panorama_by_id = find

    gsv.download_depth_maps(storage, many_pano_infos(50))

    out = capsys.readouterr().out
    assert '2 consecutive failures (2 unexpected)' in out
    # Scoped to the per-class breakdown: the WARNING's closing advice mentions the network either way.
    assert '(2 network)' not in out
    # Still transient: nothing ledgered, so the panos retry next run.
    assert read_ledger(storage) == [['pano_id', 'status']]


def test_indices_disagreeing_with_the_raster_is_transient(tmp_path, fake_streetview):
    """The writer's cross-check between streetlevel's raster and our plane indices (#56 review) must behave
    like the other malformed-v3 cases end to end: no artifact, no ledger row, retried next run - never
    'unavailable', which would permanently write off a pano whose depth Google is still serving."""
    storage = str(tmp_path)
    calls = []
    # Sentinel on the LEFT in stored (JPEG) order; indices claim it is on the right.
    depth = np.array([[-1.0, 5.0]])          # streetlevel order -> stored [5.0, -1.0]
    planes = SimpleNamespace(indices=np.array([[0, 1]], dtype=np.uint8),
                             normals=np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32),
                             distances=np.array([0.0, 2.5], dtype=np.float32))

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        return make_pano(depth, planes=planes)

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    assert read_ledger(storage) == [['pano_id', 'status']]
    assert not os.path.isfile(artifact_path(storage, 'abcdef'))
    # Nothing half-written left behind either.
    assert not os.path.exists(os.path.join(storage, 'ab'))

    assert gsv.download_depth_maps(storage, pano_infos('abcdef')) == (0, 1, 0, 1)
    assert calls == ['abcdef', 'abcdef']


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
    storage hiccup. (log.csv itself is safe either way - DownloadRunner pads the row to its full width in a finally.)
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
    # urllib3's own wording, not a hand-written 'too many 429s': since #177 the arm reads the status out of the
    # message (pushback_reason), so only the realistic shape exercises it.
    probe_retry_error(429),
    # An interstitial at a 5xx status: the landing is the refusal, whatever status it wore.
    probe_retry_error_at('https://www.google.com/sorry/index', 503),
], ids=['blocked-error', 'retry-429', 'retry-at-interstitial-503'])
def test_google_refusing_requests_stops_the_phase_immediately(tmp_path, fake_streetview, error):
    """A block is a verdict on the endpoint, not on one pano, so it shouldn't cost 25 more requests to notice."""
    storage = str(tmp_path)
    latch = str(tmp_path / 'latch')
    calls = []
    sr = {}

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        raise error

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, many_pano_infos(50), block_latch_path=latch,
                                   stop_reasons=sr) == (0, 1, 0, 1)
    assert len(calls) == 1
    # Nothing permanent is concluded from a block; every pano retries next run.
    assert read_ledger(storage) == [['pano_id', 'status']]
    assert os.path.isfile(latch)
    assert sr['depth_stop'] == gsv.DEPTH_STOP_BLOCKED
    assert condition_codes(sr) == [gsv.DEPTH_CONDITION_REFUSED]


# --- A 5xx storm is weather, not a refusal (#177) -------------------------------------------------------------
#
# The photometa session retries [429, 500, 502, 503, 504], so a 5xx storm that outlasts the policy arrives as a
# RetryError just as a 429 does. Before #177 the depth loop read every RetryError as Google refusing this host:
# one request, DEPTH_STOP_BLOCKED, the fleet-wide latch for 6 h and the earned pace forfeited - over an outage.
# The image phase's photometa arm (#172) already draws the line with pushback_reason; the depth loop now does.

FIVE_XX = [500, 502, 503, 504]


@pytest.mark.parametrize('status', FIVE_XX)
def test_a_5xx_storm_is_weather_not_a_refusal(tmp_path, fake_streetview, capsys, status):
    storage = str(tmp_path)
    latch = str(tmp_path / 'latch')
    calls = []
    sr = {}

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        raise probe_retry_error(status)

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, many_pano_infos(5), block_latch_path=latch,
                                   stop_reasons=sr) == (0, 5, 0, 5)
    assert len(calls) == 5, 'every pano is attempted: an outage does not stop the phase at the first request'
    assert sr['depth_stop'] is None
    assert not os.path.exists(latch)
    assert condition_codes(sr) == []
    assert read_ledger(storage) == [['pano_id', 'status']], 'transient: nothing ledgered, all retry next run'
    out = capsys.readouterr().out
    assert 'Google stopped answering' not in out
    assert 'Google is refusing requests' not in out


def test_a_5xx_storm_trips_the_breaker_not_the_latch(tmp_path, fake_streetview, capsys):
    """The storm is bounded the way any network streak is: the retreat schedule, then the breaker at 25."""
    storage = str(tmp_path)
    latch = str(tmp_path / 'latch')
    calls = []
    sr = {}

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        raise probe_retry_error(503)

    fake_streetview.find_panorama_by_id = find

    gsv.download_depth_maps(storage, many_pano_infos(40), block_latch_path=latch, stop_reasons=sr)

    assert len(calls) == gsv.DEPTH_MAX_CONSECUTIVE_FAILURES
    assert sr['depth_stop'] == gsv.DEPTH_STOP_CONSECUTIVE_FAILURES
    assert condition_codes(sr) == [gsv.DEPTH_CONDITION_BREAKER]
    assert '%d network' % gsv.DEPTH_MAX_CONSECUTIVE_FAILURES in sr['conditions'][0]['detail']
    assert not os.path.exists(latch)
    out = capsys.readouterr().out
    assert 'WARNING' in out and 'consecutive failures' in out


def test_a_retry_error_whose_status_cannot_be_read_is_weather_too(tmp_path, fake_streetview):
    """pushback_reason returns None when it cannot read a status out of the RetryError, and the arm must follow
    it: an unreadable give-up is not evidence of a refusal."""
    storage = str(tmp_path)
    latch = str(tmp_path / 'latch')
    sr = {}

    def find(pano_id, **kwargs):
        raise requests.exceptions.RetryError('gave up')

    fake_streetview.find_panorama_by_id = find

    assert gsv.download_depth_maps(storage, many_pano_infos(3), block_latch_path=latch,
                                   stop_reasons=sr) == (0, 3, 0, 3)
    assert sr['depth_stop'] is None
    assert not os.path.exists(latch)


def test_a_5xx_storm_backs_this_run_off_locally(tmp_path, fake_streetview, monkeypatch):
    """The network arm's reflex: slow THIS run down, never as Google's own evidence (which would forfeit)."""
    calls = []
    original = gsv.DepthPacer.on_pushback

    def spy(self, why, from_google=False):
        calls.append((why, from_google))
        return original(self, why, from_google=from_google)

    monkeypatch.setattr(gsv.DepthPacer, 'on_pushback', spy)
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: (_ for _ in ()).throw(probe_retry_error(503))

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(4), block_latch_path=str(tmp_path / 'latch'))

    assert calls == [('network failure', False)] * 4


def test_a_non_json_body_is_classed_network_not_unexpected(tmp_path, fake_streetview, monkeypatch):
    """streetlevel never checks status codes, so a non-200 body surfaces as a ValueError (JSONDecodeError) - the
    network arm's, which drives the retreat sleeps and now the outage floor. The class was pinned by counts
    only, so dropping ValueError from the arm's tuple survived every test (#177 review, finding 5)."""
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 3)
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: (_ for _ in ()).throw(ValueError('not json'))
    sr = {}

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(10), stop_reasons=sr)

    assert '(3 network)' in sr['conditions'][0]['detail']


# --- An outage that runs out the budget before the breaker (#177 review, finding 1) ----------------------------
#
# Each exhausted photometa request is 6 HTTP requests and ~30 s inside urllib3, so in production's 12-minute
# slot a 5xx storm reaches the budget long before 25 failures. Ended on max-runtime it was the scattered-errors
# arm - no condition, so no alarm under --only-on-failure, and re-run by the queue's extra passes into the same
# outage. A network streak of at least the retreat schedule's first step that is still running when a budget
# stops the phase is booked as the breaker it would have become, with the fixed token 'ended_on_budget'.

def storm_on_a_clock(monkeypatch, fake_streetview, raise_error, seconds_per_request=60.0):
    """Drive every request through raise_error() on a fake monotonic clock that each request advances by
    seconds_per_request, and each retreat sleep by what it slept. Returns (calls, sleeps)."""
    now = [0.0]
    calls, sleeps = [], []
    monkeypatch.setattr(gsv.time, 'monotonic', lambda: now[0])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(gsv.time, 'sleep', fake_sleep)

    def find(pano_id, **kwargs):
        calls.append(pano_id)
        now[0] += seconds_per_request
        raise_error()

    fake_streetview.find_panorama_by_id = find
    return calls, sleeps


def raise_503():
    raise probe_retry_error(503)


def test_the_outage_floor_is_the_retreat_schedules_first_step():
    assert gsv.DEPTH_OUTAGE_MIN_STREAK == OUTAGE_FLOOR


def test_a_storm_that_runs_out_the_runtime_budget_is_booked_as_the_breaker(tmp_path, fake_streetview, monkeypatch,
                                                                           capsys):
    calls, _ = storm_on_a_clock(monkeypatch, fake_streetview, raise_503)
    latch = str(tmp_path / 'latch')
    sr = {}

    # One request a minute against a budget of floor + 2 minutes: floor + 2 failures, then the budget stops it.
    gsv.download_depth_maps(str(tmp_path), many_pano_infos(50), run_start_monotonic=0.0,
                            max_runtime_minutes=OUTAGE_FLOOR + 2, block_latch_path=latch, stop_reasons=sr)

    assert len(calls) == OUTAGE_FLOOR + 2 < gsv.DEPTH_MAX_CONSECUTIVE_FAILURES
    assert sr['depth_stop'] == gsv.DEPTH_STOP_CONSECUTIVE_FAILURES, 'the queue must not re-run it into the outage'
    assert condition_codes(sr) == [gsv.DEPTH_CONDITION_BREAKER]
    detail = sr['conditions'][0]['detail']
    assert '(%d network; ended_on_budget)' % (OUTAGE_FLOOR + 2) in detail
    assert 'too many 503 error responses' in detail
    assert not os.path.exists(latch), 'still weather: no latch'
    out = capsys.readouterr().out
    assert 'WARNING' in out and 'ended_on_budget' in out
    assert 'Google stopped answering' not in out


def test_a_storm_that_runs_out_the_request_budget_is_booked_the_same_way(tmp_path, fake_streetview):
    def find(pano_id, **kwargs):
        raise requests.ConnectionError('network down')

    fake_streetview.find_panorama_by_id = find
    sr = {}

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(50), max_requests=OUTAGE_FLOOR, stop_reasons=sr)

    assert sr['depth_stop'] == gsv.DEPTH_STOP_CONSECUTIVE_FAILURES
    assert '(%d network; ended_on_budget)' % OUTAGE_FLOOR in sr['conditions'][0]['detail']


def test_a_streak_below_the_floor_at_the_budget_stays_an_ordinary_budget_stop(tmp_path, fake_streetview,
                                                                               monkeypatch):
    """Without the floor, a city with one unresolved pano and one timeout would fail the night."""
    calls, _ = storm_on_a_clock(monkeypatch, fake_streetview, raise_503)
    sr = {}

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(50), run_start_monotonic=0.0,
                            max_runtime_minutes=OUTAGE_FLOOR - 1, stop_reasons=sr)

    assert len(calls) == OUTAGE_FLOOR - 1
    assert sr['depth_stop'] == gsv.DEPTH_STOP_MAX_RUNTIME
    assert condition_codes(sr) == []


def test_a_storage_streak_at_the_budget_is_not_an_outage(tmp_path, fake_streetview, monkeypatch):
    """The floor counts NETWORK failures: a full store tripping the budget is the scattered-errors arm's, and its
    own breaker is the store's alarm."""
    now = [0.0]
    monkeypatch.setattr(gsv.time, 'monotonic', lambda: now[0])

    def find(pano_id, **kwargs):
        now[0] += 60.0
        return make_pano(default_depth_array())

    fake_streetview.find_panorama_by_id = find
    monkeypatch.setattr(gsv, '_write_depth_artifact', full_disk)
    sr = {}

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(50), run_start_monotonic=0.0,
                            max_runtime_minutes=OUTAGE_FLOOR + 2, stop_reasons=sr)

    assert sr['depth_stop'] == gsv.DEPTH_STOP_MAX_RUNTIME
    assert condition_codes(sr) == []


def test_a_success_breaks_the_streak_the_floor_counts(tmp_path, fake_streetview, monkeypatch):
    """The floor reads the streak still running when the budget stops the phase, not the night's total."""
    now = [0.0]
    seen = []
    monkeypatch.setattr(gsv.time, 'monotonic', lambda: now[0])

    def find(pano_id, **kwargs):
        seen.append(pano_id)
        now[0] += 60.0
        if len(seen) == OUTAGE_FLOOR:   # floor - 1 failures, one save, then floor - 1 more failures
            return make_pano(default_depth_array())
        raise probe_retry_error(503)

    fake_streetview.find_panorama_by_id = find
    sr = {}

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(50), run_start_monotonic=0.0,
                            max_runtime_minutes=2 * OUTAGE_FLOOR - 1, stop_reasons=sr)

    assert len(seen) == 2 * OUTAGE_FLOOR - 1
    assert sr['depth_stop'] == gsv.DEPTH_STOP_MAX_RUNTIME
    assert condition_codes(sr) == []


def test_a_storm_that_runs_out_of_panos_is_not_booked(tmp_path, fake_streetview):
    """Scoped to a BUDGET stop, as decided: a short list that ends inside a streak keeps today's behaviour (the
    scattered-errors WARNING, no condition). A decision, not an accident - see the PR's Decisions for Jon."""
    def find(pano_id, **kwargs):
        raise probe_retry_error(503)

    fake_streetview.find_panorama_by_id = find
    sr = {}

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(OUTAGE_FLOOR + 1), stop_reasons=sr)

    assert sr['depth_stop'] is None
    assert condition_codes(sr) == []


@pytest.mark.parametrize('budget_minutes, expected_sleeps, expected_calls, expected_stop', [
    # no budget: the schedule as written, then the breaker
    (None, [300], 3, gsv.DEPTH_STOP_CONSECUTIVE_FAILURES),
    # two one-minute failures leave 480 s of a 10-minute budget: the 300 s retreat fits, so it runs in full
    (10, [300], 3, gsv.DEPTH_STOP_CONSECUTIVE_FAILURES),
    # 60 s left of a 3-minute budget: the retreat would outlast it, so stop now rather than sleep out the slot
    # (#177 second review, nit 5 - a capped sleep then stopped anyway, without another request)
    (3, [], 2, gsv.DEPTH_STOP_MAX_RUNTIME),
    (2, [], 2, gsv.DEPTH_STOP_MAX_RUNTIME),     # nothing left
    (1.5, [], 2, gsv.DEPTH_STOP_MAX_RUNTIME),   # already 30 s over (a request outlasted the budget)
], ids=['no-budget', 'room-to-spare', 'would-outlast', 'nothing-left', 'overrun'])
def test_a_retreat_never_sleeps_past_the_budget(tmp_path, fake_streetview, monkeypatch, budget_minutes,
                                                expected_sleeps, expected_calls, expected_stop):
    """The 300 s step at failure 15 ignored the budget and overran the queue's 5-minute kill grace in 65% of the
    reviewer's simulated seeds, booking the city timed_out for a reason naming neither Google nor depth. A
    retreat that would outlast the budget left is not shortened: the phase stops instead, handing the rest of
    the slot back to the queue's window, since a sleep to the deadline is followed by no request anyway."""
    monkeypatch.setattr(gsv, 'DEPTH_RETREAT_SCHEDULE', {2: 300})
    monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 3)
    calls, sleeps = storm_on_a_clock(monkeypatch, fake_streetview, raise_503)
    sr = {}

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(10),
                            run_start_monotonic=None if budget_minutes is None else 0.0,
                            max_runtime_minutes=budget_minutes, stop_reasons=sr)

    assert sleeps == expected_sleeps
    assert len(calls) == expected_calls
    assert sr['depth_stop'] == expected_stop


def test_a_retreat_that_would_outlast_the_budget_still_books_an_outage(tmp_path, fake_streetview, monkeypatch):
    """Stopping at the retreat instead of the loop's budget check is still a budget stop, so a streak at the
    floor is booked as the breaker exactly as it would have been one sleep later."""
    monkeypatch.setattr(gsv, 'DEPTH_RETREAT_SCHEDULE', {OUTAGE_FLOOR: 300})
    calls, sleeps = storm_on_a_clock(monkeypatch, fake_streetview, raise_503)
    sr = {}

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(50), run_start_monotonic=0.0,
                            max_runtime_minutes=OUTAGE_FLOOR + 2, stop_reasons=sr)

    assert sleeps == []
    assert len(calls) == OUTAGE_FLOOR
    assert sr['depth_stop'] == gsv.DEPTH_STOP_CONSECUTIVE_FAILURES
    assert '(%d network; ended_on_budget)' % OUTAGE_FLOOR in sr['conditions'][0]['detail']


def test_an_unexpected_streak_at_the_budget_is_not_an_outage(tmp_path, fake_streetview, monkeypatch):
    """The floor counts NETWORK failures only. The 'unexpected' arm is a property of the payload (a
    DepthPayloadError, a parser crash), not of reaching photometa - and it has the 25-breaker given time. Pinned
    because counting it survived every test (#177 second review, nit 3)."""
    def raise_unexpected():
        raise gsv.DepthPayloadError('depth payload present but no plane data')

    calls, _ = storm_on_a_clock(monkeypatch, fake_streetview, raise_unexpected)
    sr = {}

    gsv.download_depth_maps(str(tmp_path), many_pano_infos(50), run_start_monotonic=0.0,
                            max_runtime_minutes=OUTAGE_FLOOR + 2, stop_reasons=sr)

    assert len(calls) == OUTAGE_FLOOR + 2
    assert sr['depth_stop'] == gsv.DEPTH_STOP_MAX_RUNTIME
    assert condition_codes(sr) == []


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


class TestCountUnresolvedDepth:
    """count_unresolved_depth backs DownloadRunner's decision to reserve image time for depth at all: a
    reservation with nothing unresolved would burn image throughput for a phase that returns in milliseconds."""

    def test_counts_everything_with_no_ledger(self, tmp_path):
        assert gsv.count_unresolved_depth(str(tmp_path), pano_infos('aaaaaa', 'bbbbbb')) == 2

    def test_resolved_panos_do_not_count(self, tmp_path):
        with open(os.path.join(str(tmp_path), gsv.DEPTH_LOG_FILENAME), 'w', newline='') as f:
            f.write('pano_id,status\naaaaaa,saved\nbbbbbb,unavailable\n')
        assert gsv.count_unresolved_depth(str(tmp_path), pano_infos('aaaaaa', 'bbbbbb', 'cccccc')) == 1

    def test_malformed_ledger_rows_count_as_unresolved(self, tmp_path):
        # Same tolerance as _load_depth_log: a crash-truncated row means the pano will be re-requested, so it
        # is real depth work and must count toward the backlog.
        with open(os.path.join(str(tmp_path), gsv.DEPTH_LOG_FILENAME), 'w', newline='') as f:
            f.write('pano_id,status\nbbbbbb,saved\naaaaaa\n')
        assert gsv.count_unresolved_depth(str(tmp_path), pano_infos('aaaaaa', 'bbbbbb')) == 1

    def test_unreadable_ledger_counts_as_no_backlog(self, tmp_path, monkeypatch):
        """When the ledger can't be read, download_depth_maps sits the run out — nothing to reserve for."""

        def boom(path):
            raise OSError(5, 'Input/output error')

        monkeypatch.setattr(gsv, '_load_depth_log', boom)
        assert gsv.count_unresolved_depth(str(tmp_path), pano_infos('aaaaaa')) == 0


def test_missing_streetlevel_returns_zeros(tmp_path, monkeypatch):
    storage = str(tmp_path)
    # None in sys.modules makes `from streetlevel import streetview` raise ImportError.
    monkeypatch.setitem(sys.modules, 'streetlevel', None)
    monkeypatch.delitem(sys.modules, 'streetlevel.streetview', raising=False)

    assert gsv.download_depth_maps(storage, pano_infos('aaaaaa')) == (0, 0, 0, 0)
    assert read_ledger(storage) is None


# --- Conditions the depth phase notes for the queue (#161) ---------------------------------------------------
#
# Each of these used to end the city's run in an ordinary exit 0, so the night's only alarm never fired. They
# ride the run summary (stop_reasons['conditions']); a condition does not change what stopped the phase.

def condition_codes(stop_reasons):
    return [c['code'] for c in stop_reasons.get('conditions', [])]


class TestTheDepthPhaseNotesItsConditions:

    def test_a_tripped_breaker_is_noted_with_its_breakdown(self, tmp_path, fake_streetview, monkeypatch):
        monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 3)
        fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())
        monkeypatch.setattr(gsv, '_write_depth_artifact', full_disk)
        stop_reasons = {}

        gsv.download_depth_maps(str(tmp_path), many_pano_infos(50), stop_reasons=stop_reasons)

        assert condition_codes(stop_reasons) == [gsv.DEPTH_CONDITION_BREAKER]
        assert '3 storage' in stop_reasons['conditions'][0]['detail']

    def test_an_unreadable_ledger_is_noted(self, tmp_path, fake_streetview, monkeypatch):
        def boom(path):
            raise OSError(5, 'Input/output error')

        monkeypatch.setattr(gsv, '_load_depth_log', boom)
        stop_reasons = {}

        gsv.download_depth_maps(str(tmp_path), pano_infos('aaaaaa'), stop_reasons=stop_reasons)

        assert condition_codes(stop_reasons) == [gsv.DEPTH_CONDITION_LEDGER]

    def test_an_unreadable_ledger_prints_a_warning(self, tmp_path, fake_streetview, monkeypatch, capsys):
        """The WARNING token an ops grep for storage trouble keys on, like the unwritable arm's."""
        def boom(path):
            raise OSError(5, 'Input/output error')

        monkeypatch.setattr(gsv, '_load_depth_log', boom)

        gsv.download_depth_maps(str(tmp_path), pano_infos('aaaaaa'))

        assert 'WARNING' in capsys.readouterr().out

    def test_an_unwritable_ledger_is_noted(self, tmp_path, fake_streetview, monkeypatch):
        monkeypatch.setattr(gsv.csv, 'writer', lambda *args, **kwargs: _FullDiskWriter())
        stop_reasons = {}

        gsv.download_depth_maps(str(tmp_path), pano_infos('aaaaaa'), stop_reasons=stop_reasons)

        assert condition_codes(stop_reasons) == [gsv.DEPTH_CONDITION_LEDGER]

    def test_a_missing_streetlevel_is_noted_and_said_on_both_channels(self, tmp_path, monkeypatch, capsys,
                                                                       caplog):
        """It was logging-only: no stdout line at all, so the one channel cron delivers carried nothing, and a
        half-written pip install mid-deploy (docs/ops.md warns of it) would silently stop every depth phase."""
        monkeypatch.setitem(sys.modules, 'streetlevel', None)
        monkeypatch.delitem(sys.modules, 'streetlevel.streetview', raising=False)
        stop_reasons = {}

        with caplog.at_level(logging.ERROR):
            gsv.download_depth_maps(str(tmp_path), pano_infos('aaaaaa'), stop_reasons=stop_reasons)

        assert condition_codes(stop_reasons) == [gsv.DEPTH_CONDITION_UNAVAILABLE]
        out = capsys.readouterr().out
        assert 'WARNING' in out and 'streetlevel' in out
        assert any(r.levelno == logging.ERROR and 'streetlevel' in r.getMessage() for r in caplog.records)

    def test_scattered_errors_before_a_budget_stop_are_not_a_condition(self, tmp_path, fake_streetview,
                                                                       monkeypatch):
        """The scattered-errors arm warns on stdout but is not an alarm: a few transient failures inside a
        budget stop are an ordinary night, and alarming on them would make the alarm noise."""
        fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: make_pano(default_depth_array())
        monkeypatch.setattr(gsv, '_write_depth_artifact', full_disk)
        ticks = iter(range(0, 60000, 120))
        monkeypatch.setattr(gsv.time, 'monotonic', lambda: float(next(ticks)))
        stop_reasons = {}

        gsv.download_depth_maps(str(tmp_path), many_pano_infos(50), run_start_monotonic=0.0,
                                max_runtime_minutes=5, stop_reasons=stop_reasons)

        assert stop_reasons['depth_stop'] == gsv.DEPTH_STOP_MAX_RUNTIME
        assert condition_codes(stop_reasons) == []


OPS_MD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs', 'ops.md')


@pytest.mark.skipif(shutil.which('awk') is None, reason='the recipe is an awk one-liner; CI has awk')
@pytest.mark.parametrize('line_ending', ['as written', 'LF'])
def test_the_ledger_scrub_recipe_finds_the_last_save_in_the_ledger_this_writer_writes(
        tmp_path, fake_streetview, line_ending):
    """docs/ops.md's scrub recipe (rule 10's CRITICAL, #163) must find the last `saved` row in the file this
    module actually writes. It said `grep -n ',saved$'`, but csv.writer's default lineterminator is CRLF and
    the depth ledger does not override it (the image ledger does), so on the Linux box `$` never matched
    before the CR and step 3 printed nothing - on the one night the recipe is needed. Git Bash's grep
    strips the CR, so a dry run on a Windows desktop passed. The ledger's line endings are NOT to be changed:
    every production depth_log.csv is already CRLF. So the recipe is run here, verbatim from the page, against
    a ledger written by download_depth_maps itself - and against an LF copy, in case one is ever normalised."""
    with open(OPS_MD, encoding='utf-8') as f:
        commands = re.findall(r"`(awk -F, [^`]*depth_log\.csv)`", f.read())
    assert len(commands) == 1, commands

    storage = str(tmp_path)
    saved_ids = {'aa0001', 'aa0002', 'aa0003'}
    fake_streetview.find_panorama_by_id = (
        lambda pano_id, **kwargs: make_pano(default_depth_array()) if pano_id in saved_ids else None)
    gsv.download_depth_maps(storage, pano_infos(*sorted(saved_ids)))                  # the healthy nights
    gsv.download_depth_maps(storage, pano_infos(*['bb%04d' % i for i in range(5)]))  # the barren span
    ledger = os.path.join(storage, gsv.DEPTH_LOG_FILENAME)
    with open(ledger, 'rb') as f:
        raw = f.read()
    assert raw.count(b'\r\n') == 9, raw  # the premise: header + 3 saved + 5 unavailable, all CRLF
    if line_ending == 'LF':
        with open(ledger, 'wb') as f:
            f.write(raw.replace(b'\r\n', b'\n'))

    out = subprocess.run(shlex.split(commands[0]), cwd=storage, capture_output=True, text=True, check=True)

    assert out.stdout.strip() == '4', out  # line 1 is the header, lines 2-4 the saves


@pytest.mark.skipif(shutil.which('awk') is None, reason='the recipe is an awk one-liner; CI has awk')
def test_the_ledger_scrub_recipe_keeps_the_header_when_nothing_was_ever_saved(tmp_path, fake_streetview):
    """A small city can be written off entirely. The recipe must then name line 1 (keep the header), not print
    nothing - `head -n` with no number is an error, and one with 0 would delete the header."""
    with open(OPS_MD, encoding='utf-8') as f:
        command = re.findall(r"`(awk -F, [^`]*depth_log\.csv)`", f.read())[0]
    fake_streetview.find_panorama_by_id = lambda pano_id, **kwargs: None
    gsv.download_depth_maps(str(tmp_path), pano_infos('bb0001', 'bb0002'))

    out = subprocess.run(shlex.split(command), cwd=str(tmp_path), capture_output=True, text=True, check=True)

    assert out.stdout.strip() == '1', out
