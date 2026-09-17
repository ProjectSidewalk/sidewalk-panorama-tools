"""Tests for the depth phase's adaptive request pacing and its cross-run block latch (#43).

Both exist for one reason: the depth backfill is 1.4 M requests to one Google endpoint from one production
IP, and a block earned there does not only stop depth — tile fetches for the image phase leave the same
address. So the phase has to slow down on the first sign of trouble rather than only at the point of
refusal, and a refusal has to be remembered past the end of the process that saw it.

The pacer is bounded exploration, not a rate finder: it never draws a gap shorter than
config.depth_min_request_interval, which is therefore the one knob that decides how aggressive the fleet
can ever get.
"""

import errno
import json
import os
import time
from types import SimpleNamespace

import pytest
import requests
import urllib3
from urllib3.exceptions import NewConnectionError, ProtocolError, ReadTimeoutError
from urllib3.util.retry import RequestHistory, Retry

from conftest import default_depth_array, make_pano
from downloaders import common, gsv


def pano_infos(*pano_ids):
    return [{'pano_id': p, 'source': 'gsv'} for p in pano_ids]


# Captured at import, before conftest's _isolate_depth_host_state redirects the defaults away from the real
# temp directory. TestTheLatchPathIsAHostFact and TestTheStatePathIsAHostFact are the places that have to see
# the deployed behaviour.
_REAL_DEFAULT_LATCH_PATH = gsv.default_block_latch_path
_REAL_DEFAULT_PACE_PATH = gsv.default_pace_state_path


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock that only moves when a test moves it, and a sleep that records instead of sleeping."""
    now = [1000.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(gsv.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(gsv.time, 'sleep', sleep)
    return type('Clock', (), {'now': now, 'slept': slept, 'advance': lambda self, s: now.__setitem__(0, now[0] + s)})()


@pytest.fixture
def no_jitter(monkeypatch):
    """Draw the bottom of the jitter range, so a test can assert an exact gap."""
    monkeypatch.setattr(gsv.random, 'uniform', lambda a, b: a)


@pytest.fixture
def recorder(fake_streetview):
    """A healthy photometa stub that records which panos were actually asked for.

    Almost every latch test asserts on that list rather than on a return value: the point of a latch is
    that no REQUEST is made, and a phase that returned zeros after spending the requests anyway would look
    identical from the counts alone.
    """
    asked = []

    def find(pano_id, download_depth=True, session=None):
        asked.append(pano_id)
        return make_pano(default_depth_array())

    fake_streetview.find_panorama_by_id = find
    fake_streetview.requested = asked
    return fake_streetview


class TestThePacerOpensCarefulAndEarnsSpeed:

    def test_it_opens_at_the_start_interval_not_at_the_floor(self):
        """A run that opened at the floor would spend its first requests at the most aggressive rate the
        configuration allows, against an endpoint whose current mood it has not sampled yet."""
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0)

        assert pacer.interval == 1.0

    def test_a_clean_streak_shorter_than_the_threshold_changes_nothing(self):
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, recover_after=3)

        pacer.on_clean()
        pacer.on_clean()

        assert pacer.interval == 1.0

    def test_the_threshold_earns_one_step_of_speed(self):
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, recover_after=3)

        for _ in range(3):
            pacer.on_clean()

        assert pacer.interval == pytest.approx(1.0 * gsv.DEPTH_PACE_RECOVER_FACTOR)

    def test_speed_is_earned_again_only_by_another_full_streak(self):
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, recover_after=3)

        for _ in range(5):
            pacer.on_clean()

        assert pacer.interval == pytest.approx(0.8), 'one step, not two'

    def test_it_never_speeds_up_past_the_floor(self):
        """The floor is the whole safety argument: 0.25 s is the pace the 2026-08-09 census ran 1,360
        requests at without pushback, and nothing here may go faster than a rate we have evidence for."""
        pacer = gsv.DepthPacer(floor=0.5, start=1.0, ceiling=30.0, recover_after=1)

        for _ in range(200):
            pacer.on_clean()

        assert pacer.interval == 0.5


class TestThePacerBacksOffHard:

    def test_pushback_doubles_the_interval(self):
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0)

        pacer.on_pushback('HTTP 429')

        assert pacer.interval == pytest.approx(1.0 * gsv.DEPTH_PACE_BACKOFF_FACTOR)

    def test_backoff_stops_at_the_ceiling(self):
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=4.0)

        for _ in range(10):
            pacer.on_pushback('HTTP 429')

        assert pacer.interval == 4.0

    def test_pushback_engages_even_when_pacing_is_switched_off(self):
        """floor == 0 means "do not throttle a healthy run", never "do not react to trouble". Multiplying
        0 by 2 stays 0, so the backoff has to start from a real number or the one operator setting most
        likely to be in place on day one silently disables the protection this exists for."""
        pacer = gsv.DepthPacer(floor=0.0, start=0.0, ceiling=30.0, min_backoff=1.0)

        pacer.on_pushback('HTTP 503')

        assert pacer.interval == pytest.approx(1.0)

    def test_pushback_resets_the_clean_streak(self):
        """Otherwise a run alternating trouble and success would keep collecting credit toward speeding up
        while it was still being pushed back."""
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, recover_after=3)

        pacer.on_clean()
        pacer.on_clean()
        pacer.on_pushback('HTTP 429')
        pacer.on_clean()

        assert pacer.interval == pytest.approx(2.0), 'the backed-off value, with no recovery step yet'


class TestTheGapIsJitteredAcrossItsWholeWidth:

    def test_the_gap_is_drawn_between_the_interval_and_twice_it(self, clock, monkeypatch):
        drawn = []
        monkeypatch.setattr(gsv.random, 'uniform', lambda a, b: drawn.append((a, b)) or a)
        pacer = gsv.DepthPacer(floor=1.0, start=1.0, ceiling=30.0)

        pacer.wait()
        pacer.wait()

        assert drawn[-1] == (1.0, 2.0)

    def test_no_draw_is_ever_shorter_than_the_interval(self, clock, monkeypatch):
        """The old jitter was interval + uniform(0, 0.25*interval): a near-deterministic cadence a
        rate-limiter can fingerprint. Widening it must not widen it downwards."""
        monkeypatch.setattr(gsv.random, 'uniform', lambda a, b: a)
        pacer = gsv.DepthPacer(floor=2.0, start=2.0, ceiling=30.0)

        pacer.wait()
        pacer.wait()

        assert clock.slept == [pytest.approx(2.0)]

    def test_the_first_request_does_not_wait(self, clock, no_jitter):
        pacer = gsv.DepthPacer(floor=5.0, start=5.0, ceiling=30.0)

        pacer.wait()

        assert clock.slept == []

    def test_time_already_spent_counts_against_the_gap(self, clock, no_jitter):
        """The gap is between requests, not on top of them: a 30 s photometa timeout has already paid it."""
        pacer = gsv.DepthPacer(floor=10.0, start=10.0, ceiling=30.0)
        pacer.wait()
        clock.advance(4.0)

        pacer.wait()

        assert clock.slept == [pytest.approx(6.0)]

    def test_a_slow_request_means_no_wait_at_all(self, clock, no_jitter):
        pacer = gsv.DepthPacer(floor=1.0, start=1.0, ceiling=30.0)
        pacer.wait()
        clock.advance(60.0)

        pacer.wait()

        assert clock.slept == []

    def test_a_zero_floor_run_never_sleeps(self, clock, no_jitter):
        pacer = gsv.DepthPacer(floor=0.0, start=0.0, ceiling=30.0)

        pacer.wait()
        pacer.wait()

        assert clock.slept == []


class TestTheSessionReportsPushbackToThePacer:
    """urllib3 absorbs 429/5xx inside the adapter, so a retried status never reaches a response hook and a
    hook that only reads status_code would see almost nothing. The signal that survives is the retry
    HISTORY: Google made us try again, even though we eventually got an answer."""

    class _Retries:
        def __init__(self, history):
            self.history = history

    def _response(self, status=200, history=()):
        response = requests.Response()
        response.status_code = status
        response.url = 'https://www.google.com/maps/photometa/v1'
        response.raw = type('Raw', (), {'retries': self._Retries(history)})()
        return response

    def test_a_status_that_reaches_the_hook_is_pushback(self):
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0)

        gsv._pushback_hook(pacer)(self._response(status=429))

        assert pacer.interval > 1.0

    def test_a_clean_200_is_not_pushback(self):
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0)

        gsv._pushback_hook(pacer)(self._response(status=200))

        assert pacer.interval == 1.0

    def test_a_200_that_needed_retries_is_still_pushback(self):
        """The case a status check cannot see, and the earliest warning available."""
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0)

        gsv._pushback_hook(pacer)(self._response(status=200, history=('one retry',)))

        assert pacer.interval > 1.0

    def test_a_response_with_no_urllib3_internals_is_not_pushback(self):
        """`raw.retries` is urllib3's, not part of the requests API this repo pins, so reading it must never
        be able to turn a healthy run into a permanently backed-off one."""
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0)
        response = requests.Response()
        response.status_code = 200
        response.url = 'https://www.google.com/maps/photometa/v1'

        gsv._pushback_hook(pacer)(response)

        assert pacer.interval == 1.0


class TestTheBlockLatchIsCrossRun:
    """download_depth_maps already stands down for the RUN it was refused in. The latch is what carries that
    to the next city: serialised by scrape_queue, 52 cities a night would otherwise each re-discover a live
    block with fresh requests at the endpoint that just refused us."""

    def test_a_fresh_latch_skips_the_phase_without_a_single_request(self, tmp_path, recorder):
        latch = str(tmp_path / 'latch')
        gsv._write_block_latch(latch)

        result = gsv.download_depth_maps(str(tmp_path), pano_infos('pano1', 'pano2'), block_latch_path=latch)

        assert result == (0, 0, 0, 0)
        assert recorder.requested == []

    def test_a_fresh_latch_says_so_on_stdout_because_cron_mails_it(self, tmp_path, recorder, capsys):
        latch = str(tmp_path / 'latch')
        gsv._write_block_latch(latch)

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'), block_latch_path=latch)

        assert 'WARNING' in capsys.readouterr().out

    def test_a_stale_latch_does_not_stop_the_phase(self, tmp_path, recorder):
        latch = str(tmp_path / 'latch')
        with open(latch, 'w') as f:
            f.write(str(time.time() - (gsv.DEPTH_BLOCK_LATCH_HOURS * 3600) - 60))

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'), block_latch_path=latch)

        assert recorder.requested == ['pano1']

    def test_no_latch_means_no_block(self, tmp_path, recorder):
        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'),
                                block_latch_path=str(tmp_path / 'never-written'))

        assert recorder.requested == ['pano1']

    @pytest.mark.parametrize('contents', ['', 'not-a-number', '\x00\x01'],
                             ids=['empty', 'garbage', 'binary'])
    def test_an_unreadable_latch_scrapes_rather_than_wedging_the_fleet(self, tmp_path, recorder,
                                                                      contents):
        """The failure direction matters: a latch nobody can parse must not silently stop depth on every
        city forever. Same reasoning as scrape_queue's advisory lock."""
        latch = str(tmp_path / 'latch')
        with open(latch, 'w') as f:
            f.write(contents)

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'), block_latch_path=latch)

        assert recorder.requested == ['pano1']

    def test_a_latch_read_immediately_after_it_is_written_still_latches(self, tmp_path, recorder,
                                                                        monkeypatch):
        """The regression this suite found by flaking, pinned deterministically.

        The writer used '%f', which rounds to six places and so rounds UP about half the time. That dates the
        latch microseconds in the FUTURE, and a strict `age >= 0` guard then discarded it in exactly the case
        it exists for: the next city in the queue, seconds later. The clock is frozen here because the real
        one advances during the write and hides the race - which is why this only ever failed intermittently.
        """
        latch = str(tmp_path / 'latch')
        frozen = 1_757_000_000.0
        monkeypatch.setattr(gsv.time, 'time', lambda: frozen)
        with open(latch, 'w') as f:
            f.write(repr(frozen + 1e-6))

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'), block_latch_path=latch)

        assert recorder.requested == []

    def test_a_latch_dated_in_the_future_is_not_believed(self, tmp_path, recorder):
        """Wall clock, because it has to outlive the process — so an NTP step backwards can leave one. A
        future timestamp is nonsense, and nonsense resolves towards scraping."""
        latch = str(tmp_path / 'latch')
        with open(latch, 'w') as f:
            f.write(str(time.time() + 86400))

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'), block_latch_path=latch)

        assert recorder.requested == ['pano1']


class TestWhatSetsTheLatch:

    def test_a_blocked_stop_sets_it(self, tmp_path, fake_streetview, monkeypatch):
        latch = str(tmp_path / 'latch')
        monkeypatch.setattr(gsv, '_fetch_pano_with_depth_planes',
                            lambda pano_id, session: (_ for _ in ()).throw(gsv.DepthBlockedError('/sorry/')))

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'), block_latch_path=latch)

        assert os.path.isfile(latch)

    def test_a_breaker_trip_does_not_set_it(self, tmp_path, recorder, monkeypatch):
        """The breaker counts storage failures too. A full disk is not Google refusing us, and latching on
        it would stand the whole fleet's depth phase down for a reason that has nothing to do with Google."""
        latch = str(tmp_path / 'latch')
        monkeypatch.setattr(gsv, 'DEPTH_MAX_CONSECUTIVE_FAILURES', 2)
        monkeypatch.setattr(gsv, 'DEPTH_RETREAT_SCHEDULE', {})
        monkeypatch.setattr(gsv, '_write_depth_artifact',
                            lambda *a, **k: (_ for _ in ()).throw(OSError(28, 'No space left on device')))

        gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2', 'p3'), block_latch_path=latch)

        assert not os.path.exists(latch)

    def test_a_healthy_run_does_not_set_it(self, tmp_path, recorder):
        latch = str(tmp_path / 'latch')

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'), block_latch_path=latch)

        assert not os.path.exists(latch)

    def test_an_unwritable_latch_does_not_take_the_run_down(self, tmp_path, fake_streetview, monkeypatch):
        """Losing the latch costs the next city a handful of wasted requests. Raising here would cost the
        log.csv evidence row for a run that has already been refused."""
        monkeypatch.setattr(gsv, '_fetch_pano_with_depth_planes',
                            lambda pano_id, session: (_ for _ in ()).throw(gsv.DepthBlockedError('/sorry/')))

        result = gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'),
                                         block_latch_path=str(tmp_path / 'no-such-dir' / 'latch'))

        assert result[1] == 1, 'the blocked pano is still counted as a failure'


class TestTheLatchPathIsAHostFact:

    def test_the_default_is_local_disk_not_the_store(self):
        """The store root passed to this phase is ONE CITY's directory, so a latch there could not be
        cross-city even in principle; and the store is a network mount shared with other lab users, while
        what is being remembered is this host's standing with Google."""
        import tempfile

        assert _REAL_DEFAULT_LATCH_PATH() == os.path.join(tempfile.gettempdir(),
                                                          gsv.DEPTH_BLOCK_LATCH_FILENAME)

    def test_download_depth_maps_defaults_to_it(self, tmp_path, recorder, monkeypatch):
        asked = []
        monkeypatch.setattr(gsv, 'default_block_latch_path', lambda: asked.append(1) or str(tmp_path / 'd'))

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'))

        assert asked, 'no explicit path means the host default, not "no latch"'


class TestTheFlagReachesThePhase:
    """The latch is only useful if an operator can point it somewhere; and the default has to survive the
    whole argparse -> run -> run_scraper_and_log_results -> download_depth_maps chain, which is four hops."""

    def test_the_flag_is_passed_through_to_download_depth_maps(self, tmp_path, monkeypatch):
        import DownloadRunner

        seen = {}

        def spy(storage_path, pano_infos, **kwargs):
            seen.update(kwargs)
            return 0, 0, 0, 0

        monkeypatch.setattr(DownloadRunner.gsv, 'download_depth_maps', spy)
        monkeypatch.setattr(DownloadRunner, 'download_panorama_images', lambda *a, **k: (0, 0, 0, 0, 0))

        DownloadRunner.run_scraper_and_log_results(str(tmp_path), [], [{'pano_id': 'p', 'source': 'gsv'}],
                                                   False, depth_block_latch='/some/where/latch')

        assert seen['block_latch_path'] == '/some/where/latch'

    def test_not_passing_it_leaves_the_phase_on_its_own_default(self, tmp_path, monkeypatch):
        """None must mean "use the host default", never "no latch at all" - the difference between the fleet
        standing down together and 52 cities each walking into the same wall."""
        import DownloadRunner

        seen = {}

        def spy(storage_path, pano_infos, **kwargs):
            seen.update(kwargs)
            return 0, 0, 0, 0

        monkeypatch.setattr(DownloadRunner.gsv, 'download_depth_maps', spy)
        monkeypatch.setattr(DownloadRunner, 'download_panorama_images', lambda *a, **k: (0, 0, 0, 0, 0))

        DownloadRunner.run_scraper_and_log_results(str(tmp_path), [], [{'pano_id': 'p', 'source': 'gsv'}],
                                                   False)

        assert seen['block_latch_path'] is None

    def test_the_parser_accepts_it(self):
        import DownloadRunner

        args = DownloadRunner.build_parser().parse_args(['host', 'dir', '--depth-block-latch', '/x/y'])

        assert args.depth_block_latch == '/x/y'


class TestTheDepthPhaseReportsWhyItStopped:
    """stop_reasons is how scrape_queue tells "give this city more window" from "do NOT re-run it" (#43).

    The queue used to infer that from how long it watched the subprocess for, which cannot distinguish a
    budget stop from a stood-down phase, a tripped breaker, or a city that simply took a while to start.
    Only DEPTH_STOP_MAX_RUNTIME means more time would have helped, so each of the others has to arrive as
    itself rather than collapsed into "stopped early".
    """

    def test_a_budget_stop_is_reported_as_max_runtime(self, tmp_path, recorder):
        stop_reasons = {}

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1', 'pano2'),
                                run_start_monotonic=time.monotonic() - 600, max_runtime_minutes=5.0,
                                block_latch_path=str(tmp_path / 'no-latch'), stop_reasons=stop_reasons)

        assert recorder.requested == [], 'the budget must have stopped it for this to be the case under test'
        assert stop_reasons['depth_stop'] == gsv.DEPTH_STOP_MAX_RUNTIME

    def test_a_phase_that_resolved_its_whole_list_reports_no_stop(self, tmp_path, recorder):
        """The false-positive half: nothing stopped this phase, so it must say nothing stopped it."""
        stop_reasons = {}

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'),
                                block_latch_path=str(tmp_path / 'no-latch'), stop_reasons=stop_reasons)

        assert recorder.requested == ['pano1']
        assert stop_reasons['depth_stop'] is None

    def test_a_request_cap_is_reported_as_itself_not_as_a_budget(self, tmp_path, recorder):
        """--max-depth-requests is a per-PROCESS cap the operator asked for, so a re-run would silently
        multiply it. It must not arrive at the queue looking like a budget stop."""
        stop_reasons = {}

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1', 'pano2', 'pano3'), max_requests=1,
                                block_latch_path=str(tmp_path / 'no-latch'), stop_reasons=stop_reasons)

        assert stop_reasons['depth_stop'] == gsv.DEPTH_STOP_MAX_REQUESTS

    def test_a_live_latch_is_reported_as_blocked(self, tmp_path, recorder):
        latch = str(tmp_path / 'latch')
        gsv._write_block_latch(latch)
        stop_reasons = {}

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'), block_latch_path=latch,
                                stop_reasons=stop_reasons)

        assert stop_reasons['depth_stop'] == gsv.DEPTH_STOP_BLOCKED

    def test_the_phase_runs_perfectly_well_without_being_asked(self, tmp_path, recorder):
        """stop_reasons is optional: the by-hand cron line passes no --run-summary-file at all."""
        result = gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'),
                                         block_latch_path=str(tmp_path / 'no-latch'))

        assert result == (1, 0, 0, 1)


# --- The pacer's standing outlives the process (#43) --------------------------------------------------------
#
# Measured on the production store after three nights of the backfill: a city with a backlog gets through a
# median of 585.5 requests in its 12-minute slot - chicago-il's own last run was 582 - because every city is
# a fresh process that opens at 1.0 s and needs 1,400 clean requests to reach the 0.25 s floor. So no slot
# ever gets there, and the fleet's effective rate is the opening interval, not the floor the census earned.
# At that rate the five biggest cities need 240-463 nights. (The figures are the ones the backfill report's
# committed data carries; an earlier draft of this comment said 590 requests at 1.22 s, and neither number
# was in any artifact.) So the speed a run EARNS is written to local disk and the next run opens
# there. Only earned speed: on_pushback is fed by every network failure too, and a persisted back-off would
# let one DNS blip on the box hand the next 51 cities a 30 s gap.

def write_state(path, interval, clean_streak, written_at=None):
    with open(path, 'w') as f:
        json.dump({'interval': interval, 'clean_streak': clean_streak,
                   'written_at': time.time() if written_at is None else written_at}, f)


class TestThePacerRemembersEarnedSpeed:

    def pacer(self, path):
        return gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, recover_after=2, min_backoff=1.0,
                              state_path=str(path))

    def test_with_nothing_saved_it_opens_at_the_start_interval(self, tmp_path):
        assert self.pacer(tmp_path / 'pace').interval == 1.0

    def test_earned_steps_are_handed_to_the_next_pacer(self, tmp_path):
        first = self.pacer(tmp_path / 'pace')
        for _ in range(4):
            first.on_clean()  # two full streaks: 1.0 -> 0.8 -> 0.64
        assert first.interval == pytest.approx(0.64)

        assert self.pacer(tmp_path / 'pace').interval == pytest.approx(0.64)

    def test_the_clean_streak_carries_too(self, tmp_path):
        """~586 requests a slot is two decay steps and ~186 towards the third. Forgetting the 186 would cost
        every city a third of its nightly progress towards the floor."""
        first = self.pacer(tmp_path / 'pace')
        first.on_clean()  # one short of a step
        first.save()

        second = self.pacer(tmp_path / 'pace')
        second.on_clean()  # completes the streak the first one started

        assert second.interval == pytest.approx(0.8)

    def test_a_step_is_saved_when_it_is_earned_not_only_at_the_end(self, tmp_path):
        """A run killed by the queue's hard timeout never reaches save(); what it earned must not die with it."""
        first = self.pacer(tmp_path / 'pace')
        for _ in range(2):
            first.on_clean()

        assert self.pacer(tmp_path / 'pace').interval == pytest.approx(0.8)

    def test_a_pushback_from_google_forfeits_the_credit_at_once(self, tmp_path):
        first = self.pacer(tmp_path / 'pace')
        for _ in range(4):
            first.on_clean()
        # No save() - the forfeit must not wait for the end of the phase. from_google because only Google's
        # own evidence may forfeit the host's standing; TestOnlyGoogleForfeitsTheStanding covers the rest.
        first.on_pushback('HTTP 429', from_google=True)

        second = self.pacer(tmp_path / 'pace')
        assert second.interval == 1.0
        assert second._clean_streak == 0

    def test_a_saved_slowdown_is_never_inherited(self, tmp_path):
        """on_pushback is fed by every network failure and unexpected exception, not only by Google. Were the
        widened interval persisted, one DNS blip on the box would hand the next 51 cities a 30 s gap - at
        which a 12-minute slot makes 16 requests and the first decay step is a fortnight of slots away."""
        first = self.pacer(tmp_path / 'pace')
        for _ in range(5):
            first.on_pushback('network failure')  # 30 s, the ceiling
        first.save()

        assert self.pacer(tmp_path / 'pace').interval == 1.0

    def test_the_file_never_holds_a_value_above_the_start(self, tmp_path):
        first = self.pacer(tmp_path / 'pace')
        first.on_pushback('HTTP 503')
        first.save()

        with open(tmp_path / 'pace') as f:
            assert json.load(f)['interval'] == 1.0

    def test_a_hand_written_slow_value_is_clamped_to_the_start(self, tmp_path):
        write_state(tmp_path / 'pace', 30.0, 0)

        assert self.pacer(tmp_path / 'pace').interval == 1.0

    def test_a_value_below_the_current_floor_is_clamped_up(self, tmp_path):
        """A floor raised in config.py wins over evidence earned under the old one."""
        write_state(tmp_path / 'pace', 0.05, 0)

        assert self.pacer(tmp_path / 'pace').interval == 0.25

    def test_a_streak_cannot_exceed_a_full_one(self, tmp_path):
        write_state(tmp_path / 'pace', 0.8, 10 ** 6)

        assert self.pacer(tmp_path / 'pace')._clean_streak == 1  # recover_after - 1

    @pytest.mark.parametrize('contents', [
        '', 'not json', '[]', '"fast"', '{"interval": 0.25}',
        '{"interval": "0.25", "clean_streak": 0, "written_at": %r}' % time.time(),
        '{"interval": NaN, "clean_streak": 0, "written_at": %r}' % time.time(),
        '{"interval": false, "clean_streak": 0, "written_at": %r}' % time.time(),
        '{"interval": 0.25, "clean_streak": 0, "written_at": "yesterday"}',
    ], ids=['empty', 'garbage', 'list', 'string', 'missing-keys', 'string-number', 'nan', 'bool', 'bad-stamp'])
    def test_anything_unreadable_opens_careful(self, tmp_path, contents):
        """The opposite direction from the latch, for the same reason: a latch nobody can read must not stand
        the fleet down forever, and a pace file nobody can read must not hand it the floor. NaN is the one
        json accepts that would then pass every clamp - min(max(nan, floor), start) is nan, and wait() reads
        a nan interval as "no gap at all". The bool case is `false`, not `true`: a JSON true is 1 to Python,
        which is the opening interval and would pass this test on a loader that took it; false is 0, which a
        loader that took it would clamp UP to the floor - a speed-up out of a file that says nothing."""
        (tmp_path / 'pace').write_text(contents)

        pacer = self.pacer(tmp_path / 'pace')

        assert pacer.interval == 1.0
        assert pacer._clean_streak == 0

    def test_a_day_old_standing_is_not_believed(self, tmp_path):
        write_state(tmp_path / 'pace', 0.25, 0,
                    written_at=time.time() - (gsv.DEPTH_PACE_STATE_HOURS + 1) * 3600)

        assert self.pacer(tmp_path / 'pace').interval == 1.0

    def test_a_standing_dated_far_in_the_future_is_not_believed(self, tmp_path):
        write_state(tmp_path / 'pace', 0.25, 0,
                    written_at=time.time() + (gsv.DEPTH_PACE_STATE_HOURS + 1) * 3600)

        assert self.pacer(tmp_path / 'pace').interval == 1.0

    def test_a_standing_dated_moments_in_the_future_still_loads(self, tmp_path):
        """The latch's lesson: a writer that rounds up dates the file microseconds ahead of a reader that
        opens it at once. A small future offset is just now."""
        write_state(tmp_path / 'pace', 0.25, 0, written_at=time.time() + 0.01)

        assert self.pacer(tmp_path / 'pace').interval == 0.25

    def test_no_state_path_means_no_file_is_read_or_written(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, recover_after=1)

        pacer.on_clean()
        pacer.on_pushback('HTTP 429')
        pacer.save()
        pacer.forfeit()

        assert list(tmp_path.iterdir()) == []


class TestWhatThePhaseDoesWithIt:

    @staticmethod
    def real_pacing(monkeypatch):
        """conftest zeroes the intervals so the suite does not sleep; these tests need the real shape and use
        the clock fixture so they still do not."""
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        monkeypatch.setattr(gsv, 'DEPTH_PACE_RECOVER_AFTER', 2)

    def test_a_healthy_phase_saves_its_standing_at_the_end(self, tmp_path, recorder, clock, monkeypatch):
        self.real_pacing(monkeypatch)
        state = tmp_path / 'pace'

        gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2', 'p3', 'p4', 'p5'),
                                pace_state_path=str(state))

        with open(state) as f:
            saved = json.load(f)
        assert saved['interval'] == pytest.approx(0.64), 'five clean requests: steps after the 2nd and 4th'
        assert saved['clean_streak'] == 1, 'and one towards the third'

    def test_the_next_phase_opens_where_the_last_one_ended(self, tmp_path, recorder, clock, no_jitter,
                                                            monkeypatch):
        """The whole point, end to end: the second process's first gap is the first process's last interval,
        not the opening one."""
        self.real_pacing(monkeypatch)
        state = tmp_path / 'pace'
        gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2', 'p3', 'p4'), pace_state_path=str(state))
        clock.slept.clear()

        gsv.download_depth_maps(str(tmp_path), pano_infos('q1', 'q2'), pace_state_path=str(state))

        assert clock.slept[0] == pytest.approx(0.64), 'the gap before q2 is drawn at the inherited interval'

    def test_a_blocked_stop_forfeits_the_standing(self, tmp_path, fake_streetview, clock, monkeypatch):
        self.real_pacing(monkeypatch)
        state = tmp_path / 'pace'
        write_state(state, 0.25, 1)
        monkeypatch.setattr(gsv, '_fetch_pano_with_depth_planes',
                            lambda pano_id, session: (_ for _ in ()).throw(gsv.DepthBlockedError('/sorry/')))

        gsv.download_depth_maps(str(tmp_path), pano_infos('p1'), pace_state_path=str(state),
                                block_latch_path=str(tmp_path / 'latch'))

        with open(state) as f:
            saved = json.load(f)
        assert saved == {**saved, 'interval': 1.0, 'clean_streak': 0}

    def test_a_stood_down_phase_leaves_the_standing_alone(self, tmp_path, recorder):
        """Zero requests is zero evidence, either way."""
        state, latch = tmp_path / 'pace', tmp_path / 'latch'
        write_state(state, 0.25, 1)
        gsv._write_block_latch(str(latch))

        gsv.download_depth_maps(str(tmp_path), pano_infos('p1'), pace_state_path=str(state),
                                block_latch_path=str(latch))

        with open(state) as f:
            assert json.load(f)['interval'] == 0.25

    def test_an_unwritable_state_path_costs_nothing(self, tmp_path, recorder):
        result = gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2', 'p3'),
                                         pace_state_path=str(tmp_path / 'no-such-dir' / 'pace'))

        assert result[0] == 3
        assert sorted(recorder.requested) == ['p1', 'p2', 'p3']

    def test_an_unwritable_state_path_cannot_feed_the_breaker(self, tmp_path):
        """The forfeit is written from inside a requests response hook. An exception there is the REQUEST's
        exception: it lands in the unexpected arm, counts towards the breaker, and one unwritable temp
        directory would then trip it after 25 panos - on every city, every night."""
        pacer = gsv.DepthPacer(floor=0.0, start=0.0, state_path=str(tmp_path / 'no-such-dir' / 'pace'))
        response = requests.Response()
        response.status_code = 429

        gsv._pushback_hook(pacer)(response)  # must not raise


class TestTheStatePathIsAHostFact:

    def test_the_default_is_local_disk_not_the_store(self):
        """Beside the block latch, for the latch's reasons: the store root passed to this phase is one city's
        directory, and what is remembered is this host's standing with Google."""
        import tempfile

        assert _REAL_DEFAULT_PACE_PATH() == os.path.join(tempfile.gettempdir(), gsv.DEPTH_PACE_STATE_FILENAME)

    def test_download_depth_maps_defaults_to_it(self, tmp_path, recorder, monkeypatch):
        asked = []
        monkeypatch.setattr(gsv, 'default_pace_state_path',
                            lambda: asked.append(1) or str(tmp_path / 'pace'))

        gsv.download_depth_maps(str(tmp_path), pano_infos('pano1'))

        assert asked, 'no explicit path means the host default, not "remember nothing"'
        assert os.path.isfile(tmp_path / 'pace')


class TestTheStateFlagReachesThePhase:

    def test_the_flag_is_passed_through_to_download_depth_maps(self, tmp_path, monkeypatch):
        import DownloadRunner

        seen = {}

        def spy(storage_path, pano_infos, **kwargs):
            seen.update(kwargs)
            return 0, 0, 0, 0

        monkeypatch.setattr(DownloadRunner.gsv, 'download_depth_maps', spy)
        monkeypatch.setattr(DownloadRunner, 'download_panorama_images', lambda *a, **k: (0, 0, 0, 0, 0))

        DownloadRunner.run_scraper_and_log_results(str(tmp_path), [], [{'pano_id': 'p', 'source': 'gsv'}],
                                                   False, depth_pace_state='/some/where/pace')

        assert seen['pace_state_path'] == '/some/where/pace'

    def test_not_passing_it_leaves_the_phase_on_its_own_default(self, tmp_path, monkeypatch):
        import DownloadRunner

        seen = {}

        def spy(storage_path, pano_infos, **kwargs):
            seen.update(kwargs)
            return 0, 0, 0, 0

        monkeypatch.setattr(DownloadRunner.gsv, 'download_depth_maps', spy)
        monkeypatch.setattr(DownloadRunner, 'download_panorama_images', lambda *a, **k: (0, 0, 0, 0, 0))

        DownloadRunner.run_scraper_and_log_results(str(tmp_path), [], [{'pano_id': 'p', 'source': 'gsv'}],
                                                   False)

        assert seen['pace_state_path'] is None

    def test_the_flag_reaches_the_phase_from_argv(self, tmp_path, monkeypatch):
        """The whole four-hop chain, argparse -> main -> run -> run_scraper_and_log_results, in one go."""
        import DownloadRunner

        seen = {}

        def spy(storage_path, pano_infos, **kwargs):
            seen.update(kwargs)
            return 0, 0, 0, 0

        csv_path = tmp_path / 'panos.csv'
        csv_path.write_text('pano_id,width,height,lat,lng,camera_heading,camera_pitch,source,has_labels\n'
                            'gsvPanoIdAAAAAAAAAAAAA,16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n')
        monkeypatch.setattr(DownloadRunner.gsv, 'download_depth_maps', spy)
        monkeypatch.setattr(DownloadRunner, 'download_panorama_images', lambda *a, **k: (0, 0, 0, 0, 0))
        monkeypatch.chdir(tmp_path)

        DownloadRunner.main(['sidewalk-test.invalid', str(tmp_path / 'storage'), '-c', str(csv_path),
                             '--depth-pace-state', '/x/pace'])

        assert seen['pace_state_path'] == '/x/pace'

    def test_the_parser_accepts_it(self):
        import DownloadRunner

        args = DownloadRunner.build_parser().parse_args(['host', 'dir', '--depth-pace-state', '/x/y'])

        assert args.depth_pace_state == '/x/y'


class TestOnlyGoogleForfeitsTheStanding:
    """A back-off is a reflex; forfeiting the HOST's earned standing is a verdict about Google (#125.1).

    The governing precedent is the block latch's, and this is deliberately the same principle rather than a
    second convention: "Only a blocked stop latches - the breaker counts storage failures, and a full disk is
    not Google." on_pushback is fed by the phase's `except (requests.RequestException, ValueError)` arm (a DNS
    blip, one connection reset, a JSONDecodeError from any non-200 body) and by its `except Exception` arm,
    which catches the DepthPayloadError raised for a pano that has a depth raster but no plane data - a
    property of one pano's upstream payload. None of those is Google saying "slow down", and each of them
    would otherwise hand the next 51 cities of the night a 1.0 s opening interval.
    """

    def pacer(self, path):
        return gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, recover_after=2, min_backoff=1.0,
                              state_path=str(path))

    def test_a_local_failure_does_not_forfeit_what_the_host_earned(self, tmp_path):
        write_state(tmp_path / 'pace', 0.25, 1)

        self.pacer(tmp_path / 'pace').on_pushback('network failure')

        assert self.pacer(tmp_path / 'pace').interval == 0.25

    def test_a_local_failure_still_slows_THIS_run_down(self, tmp_path):
        """The reflex is right and stays; only its reach beyond the process was wrong. A test that only
        asserted the file would pass against a pacer that had stopped backing off at all."""
        write_state(tmp_path / 'pace', 0.25, 1)
        first = self.pacer(tmp_path / 'pace')

        first.on_pushback('network failure')

        assert first.interval == 1.0, 'max(0.25 * 2, min_backoff)'

    def test_the_end_of_phase_save_does_not_launder_a_local_backoff(self, tmp_path):
        """Not persisting from on_pushback is not enough on its own: save() runs at the end of every phase,
        and a widened interval clamped back down to the opening one IS the forfeit, one seam later."""
        write_state(tmp_path / 'pace', 0.25, 1)
        first = self.pacer(tmp_path / 'pace')
        first.on_pushback('network failure')

        first.save()

        assert self.pacer(tmp_path / 'pace').interval == 0.25

    def test_google_pushing_back_does_forfeit_it(self, tmp_path):
        write_state(tmp_path / 'pace', 0.25, 1)

        self.pacer(tmp_path / 'pace').on_pushback('HTTP 429', from_google=True)

        assert self.pacer(tmp_path / 'pace').interval == 1.0

    def test_a_pushback_status_reaches_the_pacer_as_googles(self, tmp_path):
        """The response hook is the one caller that can attribute a push-back to Google, so it is the one
        caller that may forfeit. Wiring it up as a local failure would pass every test above."""
        write_state(tmp_path / 'pace', 0.25, 1)
        response = requests.Response()
        response.status_code = 503

        gsv._pushback_hook(self.pacer(tmp_path / 'pace'))(response)

        assert self.pacer(tmp_path / 'pace').interval == 1.0

    @staticmethod
    def _retried(*history):
        """A 200 whose urllib3 retry history is `history` - real RequestHistory entries, because the shape of
        an entry is what the hook has to read."""
        response = requests.Response()
        response.status_code = 200
        response.raw = SimpleNamespace(retries=SimpleNamespace(history=history))
        return response

    def test_a_retry_history_holding_a_pushback_status_reaches_the_pacer_as_googles_too(self, tmp_path):
        """The other half of the hook: a 200 that needed retries because Google answered 429/5xx first.
        urllib3 retries inside the adapter, so this is the earliest observable sign of push-back and it must
        forfeit like an outright 429."""
        write_state(tmp_path / 'pace', 0.25, 1)
        status_retry = RequestHistory('GET', '/maps/photometa/v1', None, 503, None)

        gsv._pushback_hook(self.pacer(tmp_path / 'pace'))(self._retried(status_retry))

        assert self.pacer(tmp_path / 'pace').interval == 1.0

    @pytest.mark.parametrize('error', [
        NewConnectionError(None, 'Name or service not known'),
        ReadTimeoutError(None, '/maps/photometa/v1', 'Read timed out'),
        ProtocolError('Connection aborted'),
    ], ids=['dns', 'read-timeout', 'connection-reset'])
    def test_a_retry_this_box_needed_does_not_forfeit_the_standing(self, tmp_path, error):
        """`Retry.history` records every retry cause. `Retry(connect=5, ...)` appends an entry with `status`
        None and the error for a DNS lookup that failed and then succeeded - the "one DNS blip on the box"
        this whole class exists to keep out of the host's file - and the same for a read timeout or a reset.
        Before this test the hook called every non-empty history Google's, so the blip that CLEARED after
        one retry forfeited the standing while the same blip exhausting all five retries (the phase's
        network arm, from_google=False) kept it."""
        write_state(tmp_path / 'pace', 0.25, 1)
        local_retry = RequestHistory('GET', '/maps/photometa/v1', error, None, None)

        gsv._pushback_hook(self.pacer(tmp_path / 'pace'))(self._retried(local_retry))

        assert self.pacer(tmp_path / 'pace').interval == 0.25

    def test_but_it_still_backs_this_run_off(self, tmp_path):
        """Narrowing what forfeits must not narrow what backs off: a retry is still a retry for this run."""
        write_state(tmp_path / 'pace', 0.25, 1)
        pacer = self.pacer(tmp_path / 'pace')
        local_retry = RequestHistory('GET', '/maps/photometa/v1', ProtocolError('Connection aborted'), None, None)

        gsv._pushback_hook(pacer)(self._retried(local_retry))

        assert pacer.interval == 1.0, 'max(0.25 * 2, min_backoff)'

    def test_one_pushback_status_anywhere_in_a_mixed_history_is_googles(self, tmp_path):
        """A history is the whole chain of retries one request needed; a status entry AHEAD of a connect
        entry is still Google's verdict, so the hook has to read every entry, not the last one."""
        write_state(tmp_path / 'pace', 0.25, 1)
        history = (RequestHistory('GET', '/x', None, 429, None),
                   RequestHistory('GET', '/x', NewConnectionError(None, 'blip'), None, None))

        gsv._pushback_hook(self.pacer(tmp_path / 'pace'))(self._retried(*history))

        assert self.pacer(tmp_path / 'pace').interval == 1.0

    def test_a_status_outside_the_pushback_set_is_not_googles_either(self, tmp_path):
        """What makes an entry Google's is its STATUS being a push-back one, not merely the absence of an
        error: a redirect entry carries a 3xx and no error, and is not Google telling us to slow down."""
        write_state(tmp_path / 'pace', 0.25, 1)
        redirect = RequestHistory('GET', '/x', None, 302, 'https://www.google.com/maps/photometa/v1')

        gsv._pushback_hook(self.pacer(tmp_path / 'pace'))(self._retried(redirect))

        assert self.pacer(tmp_path / 'pace').interval == 0.25

    def test_the_history_shape_is_urllib3s_own(self, tmp_path):
        """Pinned against the real Retry object rather than a hand-built entry, so a urllib3 change to what
        `increment` records fails here rather than silently reclassifying every retry."""
        write_state(tmp_path / 'pace', 0.25, 1)
        retry = Retry(total=5, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
        after_a_blip = retry.increment(method='GET', url='/maps/photometa/v1',
                                       error=NewConnectionError(None, 'Name or service not known'))
        gsv._pushback_hook(self.pacer(tmp_path / 'pace'))(self._retried(*after_a_blip.history))
        assert self.pacer(tmp_path / 'pace').interval == 0.25, 'a connect retry is not Google'

        refused = urllib3.response.HTTPResponse(body=b'', status=429, headers={}, preload_content=False)
        after_a_429 = retry.increment(method='GET', url='/maps/photometa/v1', response=refused)
        gsv._pushback_hook(self.pacer(tmp_path / 'pace'))(self._retried(*after_a_429.history))
        assert self.pacer(tmp_path / 'pace').interval == 1.0, 'a status retry is'

    def test_one_malformed_pano_does_not_slow_the_next_city(self, tmp_path, fake_streetview, clock,
                                                            monkeypatch):
        """End to end, on the concrete case: a pano whose payload carries depth but no planes raises
        DepthPayloadError into the unexpected arm. Candidates are shuffled, so such a pano is met at random
        rather than being stuck at the head of one city's list."""
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        state = tmp_path / 'pace'
        write_state(state, 0.25, 0)
        fake_streetview.find_panorama_by_id = (
            lambda pano_id, **kw: make_pano(default_depth_array(), planes=None))

        gsv.download_depth_maps(str(tmp_path), pano_infos('p1'), pace_state_path=str(state))

        with open(state) as f:
            assert json.load(f)['interval'] == 0.25

    def test_a_refusal_from_google_still_forfeits_through_the_phase(self, tmp_path, fake_streetview, clock,
                                                                    monkeypatch):
        """The counterpart: narrowing the forfeit must not narrow it to nothing."""
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        state = tmp_path / 'pace'
        write_state(state, 0.25, 0)
        monkeypatch.setattr(gsv, '_fetch_pano_with_depth_planes',
                            lambda pano_id, session: (_ for _ in ()).throw(gsv.DepthBlockedError('/sorry/')))

        gsv.download_depth_maps(str(tmp_path), pano_infos('p1'), pace_state_path=str(state),
                                block_latch_path=str(tmp_path / 'latch'))

        with open(state) as f:
            assert json.load(f)['interval'] == 1.0


class TestTheStreakIsPersistedWithTheIntervalItWasEarnedAt:
    """The decay rule is "200 consecutive clean requests AT the current interval" (#125.2).

    _persist writes the earned interval, which is capped at the opening one. When that cap bites, the streak
    standing beside it was gathered at a DIFFERENT, slower interval, and carrying it over spends evidence
    Google gave at one rate to justify a speed-up at another.
    """

    def pacer(self, path):
        return gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, recover_after=200, min_backoff=1.0,
                              state_path=str(path))

    def test_a_streak_earned_at_a_slower_interval_is_not_carried_over(self, tmp_path):
        first = self.pacer(tmp_path / 'pace')
        first.on_pushback('HTTP 429', from_google=True)  # live interval 2.0 s, credit forfeited
        for _ in range(190):
            first.on_clean()                             # 190 clean requests, all of them AT 2.0 s

        first.save()

        assert self.pacer(tmp_path / 'pace')._clean_streak == 0

    def test_and_so_the_next_run_cannot_decay_ten_requests_in(self, tmp_path):
        """The consequence the pairing exists to prevent, asserted on the interval rather than the streak."""
        first = self.pacer(tmp_path / 'pace')
        first.on_pushback('HTTP 429', from_google=True)
        for _ in range(190):
            first.on_clean()
        first.save()

        second = self.pacer(tmp_path / 'pace')
        for _ in range(10):
            second.on_clean()

        assert second.interval == 1.0, 'ten clean requests is not a decay step'

    def test_a_streak_earned_at_the_persisted_interval_still_carries(self, tmp_path):
        """The pairing narrows what is carried; it must not stop the feature working. ~586 requests a slot is
        two steps and ~186 towards the third, and forgetting the 186 costs a third of a city's nightly gain."""
        first = self.pacer(tmp_path / 'pace')
        for _ in range(190):
            first.on_clean()

        first.save()

        assert self.pacer(tmp_path / 'pace')._clean_streak == 190


class TestZeroRequestsIsZeroEvidence:
    """A phase that asked Google nothing must not restamp the standing's freshness (#125.3).

    DEPTH_PACE_STATE_HOURS exists so evidence about this host's recent behaviour towards one endpoint expires.
    run_scraper_and_log_results calls download_depth_maps unconditionally, so a Mapillary-only city, a
    fully-backfilled one, and a run whose image phase already spent --max-runtime all reach the end of the
    phase having made no requests. Restamping there means the window never expires while the queue runs.
    """

    def test_a_phase_with_no_candidates_does_not_restamp_the_standing(self, tmp_path, recorder):
        state = tmp_path / 'pace'
        write_state(state, 0.25, 7, written_at=time.time() - 23.5 * 3600)

        gsv.download_depth_maps(str(tmp_path), [], pace_state_path=str(state))

        with open(state) as f:
            age_hours = (time.time() - json.load(f)['written_at']) / 3600.0
        assert age_hours == pytest.approx(23.5, abs=0.1)

    def test_a_fully_backfilled_city_does_not_restamp_it_either(self, tmp_path, recorder):
        """The steady state after the backfill: every pano is already in the ledger, so the phase walks the
        whole corpus, spends nothing, and would otherwise refresh the file 52 times a night."""
        state = tmp_path / 'pace'
        write_state(state, 0.25, 7, written_at=time.time() - 23.5 * 3600)
        with open(tmp_path / 'depth_log.csv', 'w') as f:
            f.write('pano_id,status\np1,saved\n')

        gsv.download_depth_maps(str(tmp_path), pano_infos('p1'), pace_state_path=str(state))

        with open(state) as f:
            age_hours = (time.time() - json.load(f)['written_at']) / 3600.0
        assert age_hours == pytest.approx(23.5, abs=0.1)

    def test_a_phase_that_did_spend_requests_still_saves(self, tmp_path, recorder, clock, monkeypatch):
        """The guard narrows what is written; it must not stop the standing being written at all."""
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        monkeypatch.setattr(gsv, 'DEPTH_PACE_RECOVER_AFTER', 2)
        state = tmp_path / 'pace'

        gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2'), pace_state_path=str(state))

        with open(state) as f:
            assert json.load(f)['interval'] == pytest.approx(0.8)


class TestTheStandingSurvivesAStopThePhaseDidNotChoose:
    """The decay steps persist themselves as they are earned, but the streak in progress is written only at
    the end of the phase - and DownloadRunner translates SIGTERM into SystemExit(143) precisely so that
    end-of-phase work runs under the queue's --kill-grace backstop, a systemctl stop, or an operator's kill
    (#125 review, finding 2). A save placed after the loop was skipped by every one of those, and save()'s
    own docstring is the cost: the ~186 requests towards the next step, a third of a slot's progress.
    """

    @staticmethod
    def stopped_on_the_third_request(fake_streetview, exc):
        calls = []

        def find(pano_id, download_depth=True, session=None):
            calls.append(pano_id)
            if len(calls) == 3:
                raise exc
            return make_pano(default_depth_array())

        fake_streetview.find_panorama_by_id = find

    def test_a_sigterm_translated_stop_still_writes_the_streak(self, tmp_path, fake_streetview, monkeypatch):
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        state = tmp_path / 'pace'
        self.stopped_on_the_third_request(fake_streetview, SystemExit(143))

        with pytest.raises(SystemExit):
            gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2', 'p3', 'p4'), pace_state_path=str(state))

        with open(state) as f:
            assert json.load(f)['clean_streak'] == 2, 'two clean requests were made before the stop'

    def test_and_so_does_a_keyboard_interrupt(self, tmp_path, fake_streetview, monkeypatch):
        """An operator's Ctrl-C on a manual backfill is the other BaseException the loop does not catch."""
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        state = tmp_path / 'pace'
        self.stopped_on_the_third_request(fake_streetview, KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2', 'p3', 'p4'), pace_state_path=str(state))

        with open(state) as f:
            assert json.load(f)['clean_streak'] == 2

    def test_the_stop_is_still_the_stop(self, tmp_path, fake_streetview, monkeypatch):
        """Remembering the standing on the way out must not swallow the exit: the runner's exit code 143 is
        what tells scrape_queue the city was stopped rather than finished."""
        state = tmp_path / 'pace'
        self.stopped_on_the_third_request(fake_streetview, SystemExit(143))

        with pytest.raises(SystemExit) as stop:
            gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2', 'p3'), pace_state_path=str(state))

        assert stop.value.code == 143

    def test_a_stop_before_any_request_still_writes_nothing(self, tmp_path, recorder, monkeypatch):
        """Zero requests is zero evidence on this path too (#125.3): a SIGTERM that lands during the phase's
        setup - here, in the candidate shuffle, the last thing before the loop - must not restamp."""
        state = tmp_path / 'pace'
        write_state(state, 0.25, 7, written_at=time.time() - 23.5 * 3600)
        monkeypatch.setattr(gsv.random, 'shuffle', lambda seq: (_ for _ in ()).throw(SystemExit(143)))

        with pytest.raises(SystemExit):
            gsv.download_depth_maps(str(tmp_path), pano_infos('p1'), pace_state_path=str(state))

        with open(state) as f:
            age_hours = (time.time() - json.load(f)['written_at']) / 3600.0
        assert age_hours == pytest.approx(23.5, abs=0.1)


class TestNothingUnreadableCanEndThePhase:
    """"Everything _load_pace_state cannot believe resolves to open careful" has to hold for the whole class
    of unreadable files, not for an enumerated part of it (#125.4).

    The path is a FIXED name in the machine's temp directory, so on a shared box anything can create it first.
    A RecursionError or MemoryError out of json.load escapes DepthPacer.__init__, which download_depth_maps
    calls outside its own try - so one such file ends the depth phase for every city, every night.
    """

    def test_a_deeply_nested_file_opens_careful(self, tmp_path):
        state = tmp_path / 'pace'
        state.write_text('[' * 200_000 + ']' * 200_000)

        assert gsv._load_pace_state(str(state)) is None

    def test_and_the_pacer_built_on_it_still_starts(self, tmp_path):
        """Asserted through the constructor, because that is where the exception would actually escape."""
        state = tmp_path / 'pace'
        state.write_text('[' * 200_000 + ']' * 200_000)

        pacer = gsv.DepthPacer(floor=0.25, start=1.0, state_path=str(state))

        assert pacer.interval == 1.0

    def test_a_phase_reading_one_still_runs(self, tmp_path, recorder):
        state = tmp_path / 'pace'
        state.write_text('[' * 200_000 + ']' * 200_000)

        result = gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2'), pace_state_path=str(state))

        assert result[0] == 2
        assert sorted(recorder.requested) == ['p1', 'p2']


class TestOneProcessAtATimeSpendsTheStanding:
    """The persisted standing is a fact about the host, so exactly one live process may spend it (#125.5).

    scrape_queue serialises the fleet, but its lock guards the queue, not this host's depth rate: a manual
    backfill alongside the nightly window is a documented workflow. Two phases inheriting the same floor would
    double the rate Google sees at precisely the moment that assumption breaks.
    """

    def held(self, state):
        return common.exclusive_host_lock(str(state) + gsv.PACE_LOCK_SUFFIX)

    def test_a_second_phase_does_not_inherit_the_standing(self, tmp_path, recorder, clock, no_jitter,
                                                          monkeypatch):
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        state = tmp_path / 'pace'
        write_state(state, 0.25, 0)

        with self.held(state):
            gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2'), pace_state_path=str(state))

        assert clock.slept[0] == pytest.approx(1.0), 'the opening interval, not the inherited 0.25'

    def test_a_second_phase_does_not_overwrite_the_standing(self, tmp_path, recorder, clock, monkeypatch):
        """The other half: a locked-out run must not clobber the holder's read-modify-write either."""
        monkeypatch.setattr(gsv, 'DEPTH_PACE_RECOVER_AFTER', 2)
        state = tmp_path / 'pace'
        write_state(state, 0.25, 0)

        with self.held(state):
            gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2', 'p3'), pace_state_path=str(state))

        with open(state) as f:
            assert json.load(f)['interval'] == 0.25

    def test_a_locked_out_phase_still_does_its_work(self, tmp_path, recorder):
        """It is a pacing decision, never a reason to skip the corpus."""
        state = tmp_path / 'pace'

        with self.held(state):
            result = gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2'), pace_state_path=str(state))

        assert result[0] == 2
        assert sorted(recorder.requested) == ['p1', 'p2']

    def test_a_locked_out_phase_says_so_where_cron_will_see_it(self, tmp_path, recorder, capsys):
        state = tmp_path / 'pace'

        with self.held(state):
            gsv.download_depth_maps(str(tmp_path), pano_infos('p1'), pace_state_path=str(state))

        assert 'WARNING' in capsys.readouterr().out

    def test_the_lock_is_released_when_the_phase_ends(self, tmp_path, recorder, clock, no_jitter, monkeypatch):
        """A lock held past the end of a phase would stand the whole fleet down after the first city - the
        failure mode CLAUDE.md rejects O_EXCL lock files for."""
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        monkeypatch.setattr(gsv, 'DEPTH_PACE_RECOVER_AFTER', 2)
        state = tmp_path / 'pace'
        gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2'), pace_state_path=str(state))
        clock.slept.clear()

        gsv.download_depth_maps(str(tmp_path), pano_infos('q1', 'q2'), pace_state_path=str(state))

        assert clock.slept[0] == pytest.approx(0.8), 'the second phase inherited, so the lock was free'

    def test_a_lock_that_cannot_be_created_costs_only_the_memory(self, tmp_path, recorder):
        """An unwritable temp directory is not a reason to skip depth - it is a reason to forget."""
        state = tmp_path / 'no-such-dir' / 'pace'

        result = gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2'), pace_state_path=str(state))

        assert result[0] == 2


class TestTheStandingIsWrittenOnlyWhenItChanges:
    """A write that carries no new evidence is noise (#125.6).

    A sustained outage drives up to DEPTH_MAX_CONSECUTIVE_FAILURES push-backs. After the first the interval is
    at the ceiling and the streak is 0, so every later write is identical but for its timestamp - and on an
    unwritable path each one logs an error, 25 per city per night, into a scrape.log on the shared store where
    an ops grep cannot tell them from real storage trouble.
    """

    def test_a_run_of_identical_forfeits_is_written_once(self, tmp_path, monkeypatch):
        writes = []
        monkeypatch.setattr(gsv, '_write_pace_state', lambda *a: writes.append(a))
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, min_backoff=1.0,
                               state_path=str(tmp_path / 'pace'))

        for _ in range(25):
            pacer.on_pushback('HTTP 503', from_google=True)

        assert len(writes) == 1

    def test_every_step_that_does_change_it_is_still_written(self, tmp_path, monkeypatch):
        """The dedup compares the value; it must not merely suppress every write after the first. Each decay
        step is earned evidence that has to survive the queue's hard kill, so none of them may be swallowed."""
        writes = []
        monkeypatch.setattr(gsv, '_write_pace_state', lambda *a: writes.append(a))
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, recover_after=2, min_backoff=1.0,
                               state_path=str(tmp_path / 'pace'))

        for _ in range(4):
            pacer.on_clean()

        assert [w[1] for w in writes] == [pytest.approx(0.8), pytest.approx(0.64)]


class TestTheTemporaryFileCannotAccumulate:
    """The write is a temp file plus a rename, and the queue SIGKILLs cities (#125.7).

    A kill between the two leaves the temp file behind, and that window now opens on every decay step and
    every forfeit rather than once per run. A per-pid name made every such orphan permanent and unswept; a
    fixed one is reclaimed by the next write, which the pacing lock makes safe.
    """

    def test_an_orphan_from_a_killed_run_is_reclaimed(self, tmp_path):
        state = tmp_path / 'pace'
        orphan = tmp_path / ('pace' + '.tmp')
        orphan.write_text('{"interval": 0.9, half-written')

        gsv._write_pace_state(str(state), 0.25, 3)

        assert not orphan.exists()
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith('.tmp')] == []

    def test_repeated_writes_leave_exactly_one_file_behind(self, tmp_path):
        """Five writes, one file: the temp name is fixed, so nothing about the writing process - not its pid,
        which an earlier design put in the name - can make a second one."""
        state = tmp_path / 'pace'

        for streak in range(5):
            gsv._write_pace_state(str(state), 0.25, streak)

        assert sorted(p.name for p in tmp_path.iterdir()) == ['pace']

    def test_the_temp_name_does_not_depend_on_the_process(self, tmp_path, monkeypatch):
        """The property the reclaim above rests on, asserted directly: a write from a different pid targets
        the same temp name, so an orphan from a killed run is what the next run's write replaces."""
        state = tmp_path / 'pace'
        orphan = tmp_path / 'pace.tmp'
        orphan.write_text('half-written by pid 1000')
        monkeypatch.setattr(gsv.os, 'getpid', lambda: 2000)

        gsv._write_pace_state(str(state), 0.25, 0)

        assert not orphan.exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ['pace']


class TestForfeitGivesUpTheStandingNotTheLiveGap:
    """forfeit() runs after the request loop has stopped, so moving the live interval there changes nothing
    and reads as throttling a run that has already ended (#125.8)."""

    def test_it_writes_the_opening_interval(self, tmp_path):
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, recover_after=2, state_path=str(tmp_path / 'pace'))
        for _ in range(2):
            pacer.on_clean()

        pacer.forfeit()

        with open(tmp_path / 'pace') as f:
            assert json.load(f)['interval'] == 1.0

    def test_it_leaves_the_live_interval_where_the_backoff_put_it(self, tmp_path):
        pacer = gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, min_backoff=1.0,
                               state_path=str(tmp_path / 'pace'))
        pacer.on_pushback('HTTP 429', from_google=True)

        pacer.forfeit()

        assert pacer.interval == 2.0


class TestEarnedSpeedIsRememberedNotRecomputed:
    """The standing written is what the host EARNED, which is not the live gap and not the opening one."""

    def pacer(self, path):
        return gsv.DepthPacer(floor=0.25, start=1.0, ceiling=30.0, recover_after=2, min_backoff=1.0,
                              state_path=str(path))

    def test_an_inherited_standing_survives_a_run_that_earned_nothing_new(self, tmp_path):
        """A 12-minute slot that makes a handful of requests must hand the floor on, not give it back."""
        write_state(tmp_path / 'pace', 0.25, 0)
        first = self.pacer(tmp_path / 'pace')
        first.on_clean()  # one short of a step, so nothing new is earned

        first.save()

        with open(tmp_path / 'pace') as f:
            assert json.load(f)['interval'] == 0.25

    def test_climbing_back_after_a_local_blip_does_not_count_as_earning(self, tmp_path):
        """A local back-off widens the live gap, and the decay steps that walk it back are re-earning ground
        the host already held. Recording them as newly earned would let one DNS blip ratchet the standing
        upwards a step at a time - the same forfeit as #125.1, paid in instalments."""
        write_state(tmp_path / 'pace', 0.25, 0)
        first = self.pacer(tmp_path / 'pace')
        first.on_pushback('network failure')  # live gap 1.0 s; the standing is untouched
        for _ in range(2):
            first.on_clean()                  # one step back down, to 0.8 - still slower than 0.25

        first.save()

        with open(tmp_path / 'pace') as f:
            assert json.load(f)['interval'] == 0.25

    def test_a_network_failure_in_the_phase_does_not_forfeit_it_either(self, tmp_path, fake_streetview,
                                                                       clock, monkeypatch):
        """End to end through the phase's `except (requests.RequestException, ValueError)` arm, so a call site
        that told the pacer this was Google's doing is caught here rather than only in the unit tests."""
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        state = tmp_path / 'pace'
        write_state(state, 0.25, 0)
        monkeypatch.setattr(gsv, '_fetch_pano_with_depth_planes',
                            lambda pano_id, session: (_ for _ in ()).throw(
                                requests.exceptions.ConnectionError('name resolution failed')))

        gsv.download_depth_maps(str(tmp_path), pano_infos('p1'), pace_state_path=str(state))

        with open(state) as f:
            assert json.load(f)['interval'] == 0.25


class TestTheHostLockDrivesBothPlatformApis:
    """`common.exclusive_host_lock` is the pacing lock's mechanism, and one of its two arms is dead on
    whichever OS is running. CI is Ubuntu and this desk is Windows, so without substituting a module-shaped
    stand-in each arm is only ever checked by half the places this repo runs.
    """

    def test_the_posix_lock_api_is_driven_correctly(self, monkeypatch, tmp_path):
        calls = []

        class FakeFcntl:
            LOCK_EX = 2
            LOCK_NB = 4

            def flock(self, fd, op):
                calls.append(op)

        monkeypatch.setattr(common, '_lock_module', lambda: FakeFcntl())

        with common.exclusive_host_lock(str(tmp_path / 'pace.lock')):
            pass

        assert calls == [FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB], 'exclusive AND non-blocking'

    def test_the_windows_lock_api_is_driven_correctly(self, monkeypatch, tmp_path):
        calls = []

        class FakeMsvcrt:
            LK_NBLCK = 3

            def locking(self, fd, mode, nbytes):
                calls.append((mode, nbytes))

        monkeypatch.setattr(common, '_lock_module', lambda: FakeMsvcrt())

        with common.exclusive_host_lock(str(tmp_path / 'pace.lock')):
            pass

        assert calls == [(FakeMsvcrt.LK_NBLCK, 1)], 'must be a NON-blocking single-byte lock'

    def test_a_refusal_from_the_platform_becomes_hoststatelocked(self, monkeypatch, tmp_path):
        """download_depth_maps catches exactly this, so a platform OSError arriving unwrapped would take the
        depth phase down instead of costing it the standing."""
        class Refusing:
            LOCK_EX = 2
            LOCK_NB = 4

            def flock(self, fd, op):
                raise OSError(11, 'Resource temporarily unavailable')

        monkeypatch.setattr(common, '_lock_module', lambda: Refusing())

        with pytest.raises(common.HostStateLocked):
            with common.exclusive_host_lock(str(tmp_path / 'pace.lock')):
                pass

    @pytest.mark.parametrize('code', ['EAGAIN', 'EWOULDBLOCK', 'EACCES'])
    def test_every_contended_lock_errno_is_hoststatelocked(self, monkeypatch, tmp_path, code):
        """flock says EWOULDBLOCK (EAGAIN on Linux); msvcrt.locking says EACCES (measured on Windows)."""
        class Refusing:
            LOCK_EX = 2
            LOCK_NB = 4

            def flock(self, fd, op):
                raise OSError(getattr(errno, code), 'held')

        monkeypatch.setattr(common, '_lock_module', lambda: Refusing())

        with pytest.raises(common.HostStateLocked):
            with common.exclusive_host_lock(str(tmp_path / 'pace.lock')):
                pass

    @pytest.mark.parametrize('code', ['ENOLCK', 'EBADF'])
    def test_a_lock_that_cannot_be_taken_for_another_reason_is_not_a_second_process(self, monkeypatch,
                                                                                    tmp_path, code):
        """"Another depth phase holds the pacing lock" is the one diagnosis the caller prints to stdout, where
        cron mails it. A filesystem that cannot lock at all (ENOLCK, EOPNOTSUPP) or a bad descriptor is not
        that, and wrapping it as HostStateLocked sent the operator looking for a process that did not exist -
        for every city, every night (#125 review, finding 4)."""
        class Broken:
            LOCK_EX = 2
            LOCK_NB = 4

            def flock(self, fd, op):
                raise OSError(getattr(errno, code), 'cannot lock here')

        monkeypatch.setattr(common, '_lock_module', lambda: Broken())

        with pytest.raises(OSError) as raised:
            with common.exclusive_host_lock(str(tmp_path / 'pace.lock')):
                pass

        assert not isinstance(raised.value, common.HostStateLocked)

    def test_and_the_phase_then_remembers_nothing_without_claiming_a_second_phase(self, monkeypatch, tmp_path,
                                                                                  recorder, capsys, caplog):
        """The arm download_depth_maps already had for a lock file it cannot open: a log line, no stdout."""
        class Broken:
            LOCK_EX = 2
            LOCK_NB = 4

            def flock(self, fd, op):
                raise OSError(errno.ENOLCK, 'No locks available')

        monkeypatch.setattr(common, '_lock_module', lambda: Broken())
        state = tmp_path / 'pace'
        write_state(state, 0.25, 0)

        result = gsv.download_depth_maps(str(tmp_path), pano_infos('p1', 'p2'), pace_state_path=str(state))

        assert result[0] == 2, 'the corpus is still worked'
        assert 'another depth phase' not in capsys.readouterr().out
        assert 'cannot open the pacing lock' in caplog.text
        with open(state) as f:
            assert json.load(f)['interval'] == 0.25, 'and nothing was written'

    def test_the_holder_leaves_its_pid_behind(self, tmp_path):
        """The lock file is never unlinked - unlinking races - so it is left holding the pid of whoever to
        go looking for."""
        lock = tmp_path / 'pace.lock'

        with common.exclusive_host_lock(str(lock)):
            pass

        assert lock.read_text().strip() == str(os.getpid())
