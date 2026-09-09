"""Tests for the depth phase's adaptive request pacing and its cross-run block latch (#43).

Both exist for one reason: the depth backfill is 1.4 M requests to one Google endpoint from one production
IP, and a block earned there does not only stop depth — tile fetches for the image phase leave the same
address. So the phase has to slow down on the first sign of trouble rather than only at the point of
refusal, and a refusal has to be remembered past the end of the process that saw it.

The pacer is bounded exploration, not a rate finder: it never draws a gap shorter than
config.depth_min_request_interval, which is therefore the one knob that decides how aggressive the fleet
can ever get.
"""

import json
import os
import time

import pytest
import requests

from conftest import default_depth_array, make_pano
from downloaders import gsv


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


# --- The pacer's standing outlives the process (#43) --------------------------------------------------------
#
# Measured on the production store after three nights of the backfill: every city with a backlog makes ~590
# requests in its 12-minute slot, at 1.22 s each, because every city is a fresh process that opens at 1.0 s
# and needs 1,400 clean requests to reach the 0.25 s floor - so no slot ever gets there, and the fleet's
# effective rate is the opening interval, not the floor the census earned. The five biggest cities need
# 250-460 nights at that rate. So the speed a run EARNS is written to local disk and the next run opens
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
        """590 requests a slot is two decay steps and 190 towards the third. Forgetting the 190 would cost
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

    def test_a_pushback_forfeits_the_credit_at_once(self, tmp_path):
        first = self.pacer(tmp_path / 'pace')
        for _ in range(4):
            first.on_clean()
        first.on_pushback('HTTP 429')  # and no save() - the forfeit must not wait for the end of the phase

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
