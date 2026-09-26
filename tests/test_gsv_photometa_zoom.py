"""The GSV zoom decision from photometa's own image_sizes (#74).

Until #74 the image phase picked a zoom by requesting two tiles (zoom 5 and zoom 3 at x=0, y=0) and asking
which came back non-black. That answers "is there imagery at this zoom?", never "does the frame the app
reported agree with what Google serves?" - so an app frame smaller than Google's top level was fetched at the
top zoom with the app's grid, which is the top-left crop of the pano, saved at the app's exact dimensions with
no black and no undersized tile to give it away.

What replaced it, and what these tests hold:

* **The rule is a consistency check** (choose_zoom): the highest level k whose grid - _dims_at_zoom of the
  app's frame at k - IS a level Google reports. For a native frame it is the probe's answer for every six-level
  shape in OBSERVED_PHOTOMETA, and the native top level for the four- and five-level ones (the probe could only
  say 5 or 3, so a five-level 5376 pano moves from its zoom 3 to zoom 4).
* **A frame no level admits is refused loudly and transiently** by download_single_pano, never cropped - but
  NOT by resolve_frame, which only reports it. resolve_zoom_and_dims, which refetch_panos.py composes with its
  own frame gate, is the probe alone: it never asks photometa and never reads or writes the block latch.
* **On the probe arm, download_single_pano checks the frame too** (frame_covers_pano) - which guards a frame
  SMALLER than Google serves, not a larger one.
* **The probe survives verbatim as the fallback** for every photometa failure and for "not found", so a
  permanent downloaded=0 still rests on the same two black tiles it always did.
* **The image phase shares the depth phase's block latch file, not its pacer.**

Network-free: photometa is stubbed at gsv._fetch_image_levels, the probe at gsv._get_response, tiles at
gsv._download_tiles.
"""

import io
import logging
import os
import time

import pytest
import requests

from PIL import Image

from downloaders import gsv
from downloaders.common import DownloadResult, jpeg_dimensions
from test_gsv_stitcher import (OBSERVED_PHOTOMETA, RED, deny_probe, jpeg_bytes, stub_photometa, stub_probe,
                               stub_tiles)

PANO = 'photometaPanoAAAAAAAAA'

SERIES_16384 = [(512, 256), (1024, 512), (2048, 1024), (4096, 2048), (8192, 4096), (16384, 8192)]
SERIES_13312 = [(416, 208), (832, 416), (1664, 832), (3328, 1664), (6656, 3328), (13312, 6656)]
SERIES_3328 = [(416, 208), (832, 416), (1664, 832), (3328, 1664)]            # DC-hist 2007, four levels
SERIES_5376 = [(336, 168), (672, 336), (1344, 672), (2688, 1344), (5376, 2688)]  # Paris-hist, five levels
TWO_LEVELS = [(512, 256), (1024, 512)]

# One encoded tile shared by every stitch, so a 512-tile grid decodes the same small body 512 times.
TILE = jpeg_bytes(RED)


def pano_info(width, height, pano_id=PANO):
    return {'pano_id': pano_id, 'width': width, 'height': height}


def red_tiles(monkeypatch):
    return stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], TILE))


def count_probes(monkeypatch, pick_zoom):
    """stub_probe, keeping the photometa stub the test already installed."""
    fetch = gsv._fetch_image_levels
    requested = stub_probe(monkeypatch, pick_zoom)
    monkeypatch.setattr(gsv, '_fetch_image_levels', fetch)
    return requested


def fresh_latch():
    gsv._write_block_latch(gsv.default_block_latch_path())


# --- the pure rule -----------------------------------------------------------------------------------------

class TestChooseZoomFromReportedLevels:
    @pytest.mark.parametrize('name,sizes', OBSERVED_PHOTOMETA)
    def test_the_native_frame_picks_the_top_level_for_every_observed_pano(self, name, sizes):
        """Reproduces the probe's answer on every real shape - and is not "zoom 5 always": DC-hist has four
        levels and Paris-hist five."""
        assert gsv.choose_zoom(sizes, *sizes[-1]) == (len(sizes) - 1, True), name

    def test_an_app_frame_equal_to_a_lower_level_picks_that_level_not_the_top(self):
        """The max-level shortcut would fetch this 16x8 grid at zoom 5: the top-left quarter of the pano."""
        assert gsv.choose_zoom(SERIES_16384, 8192, 4096) == (4, True)

    def test_a_frame_google_now_serves_larger_is_inconsistent(self):
        """13312 against the 16384 series: no level is the grid the app's frame implies at that level."""
        assert gsv.choose_zoom(SERIES_16384, 13312, 6656) == (5, False)

    def test_a_truncated_series_that_still_agrees_is_a_consistent_fallback(self):
        assert gsv.choose_zoom(SERIES_16384[:4], 16384, 8192) == (3, True)
        assert gsv.choose_zoom(SERIES_16384[:5], 16384, 8192) == (4, True)

    def test_a_height_only_disagreement_is_inconsistent(self):
        """Every other fixture is 2:1 on both sides, so a width-only comparison passed them all - and would
        fetch a frame whose width matches a level with the app's y-grid, silently cutting rows (review 5a)."""
        assert gsv.choose_zoom(SERIES_16384, 16384, 6656) == (5, False)
        assert gsv.choose_zoom(SERIES_16384, 16384, 8192) == (5, True)

    def test_a_frame_from_another_family_is_inconsistent_even_when_smaller_levels_exist(self):
        assert gsv.choose_zoom(SERIES_3328, 16384, 8192) == (3, False)

    def test_it_scans_from_the_top_down(self):
        """Contrived: levels 2 and 4 are both the frame's grid. The higher one is the better image."""
        sizes = [(1, 1), (2, 2), (1024, 512), (3, 3), (4096, 2048)]
        assert gsv.choose_zoom(sizes, 4096, 2048) == (4, True)

    def test_the_series_the_app_frame_belongs_to_is_consistent_at_every_prefix(self):
        """Every level of a real series is itself a consistent fallback for the full frame - the property
        the truncated-series cases above depend on, checked across the whole observed table."""
        for name, sizes in OBSERVED_PHOTOMETA:
            for top in range(len(sizes)):
                assert gsv.choose_zoom(sizes[:top + 1], *sizes[-1]) == (top, True), (name, top)


class TestProbeZoomIsTodaysProbe:
    @pytest.mark.parametrize('pick,expected', [(5, 5), (3, 3), (-1, None)])
    def test_it_answers_what_the_two_tiles_say(self, monkeypatch, pick, expected):
        requested = stub_probe(monkeypatch, pick_zoom=pick)

        assert gsv._probe_zoom(PANO) == expected
        assert requested == ['%s&zoom=3&x=0&y=0&panoid=%s' % (gsv._CBK_BASE_URL, PANO),
                             '%s&zoom=5&x=0&y=0&panoid=%s' % (gsv._CBK_BASE_URL, PANO)]


# --- resolve_frame -----------------------------------------------------------------------------------------

class TestResolveFrameUsesPhotometaFirst:
    def test_reported_levels_pick_the_zoom_and_no_probe_is_sent(self, monkeypatch):
        asked = stub_photometa(monkeypatch, SERIES_16384)
        deny_probe(monkeypatch)

        frame = gsv.resolve_frame(pano_info(16384, 8192))

        assert frame == gsv.ResolvedFrame(16384, 8192, 5, True, (16384, 8192), 'photometa')
        assert asked == [PANO]

    def test_the_photometa_request_asks_for_no_depth(self, monkeypatch):
        """Through the real _fetch_image_levels, with the api half stubbed: the request must be the 16 KB one,
        and it must ride the photometa session - the one carrying the block-detection hook."""
        captured = {}

        def fake_find(panoid, download_depth=False, locale='en', session=None):
            captured.update(panoid=panoid, download_depth=download_depth, session=session)
            return [None, [[[1], [None, panoid], [None, None, None, [[[[256, 512]], [[512, 1024]]], [512, 512]]]]]]

        api = pytest.importorskip('streetlevel.streetview.api')
        monkeypatch.setattr(api, 'find_panorama_by_id', fake_find)
        deny_probe(monkeypatch)

        frame = gsv.resolve_frame(pano_info(1024, 512))

        assert frame == gsv.ResolvedFrame(1024, 512, 1, True, (1024, 512), 'photometa')
        assert captured['download_depth'] is False
        assert isinstance(captured['session'], requests.Session)
        assert gsv._raise_if_blocked in captured['session'].hooks['response']

    @pytest.mark.parametrize('pick,expected', [(5, gsv.ResolvedFrame(1024, 512, 5, True, None, 'probe')),
                                               (-1, None)])
    def test_not_found_falls_through_to_the_probe_before_a_permanent_verdict(self, monkeypatch, pick, expected):
        """D5: photometa's code 2 is not allowed to found a permanent downloaded=0 on its own. The verdict
        rests on the two black tiles, as it always has - GSV has no permanent-verdict breaker, so a photometa
        fault answering "not found" for live panos would otherwise write off a city's new panos in a night."""
        stub_photometa(monkeypatch, gone=True)
        requested = count_probes(monkeypatch, pick)

        assert gsv.resolve_frame(pano_info(1024, 512)) == expected
        assert len(requested) == 2

    @pytest.mark.parametrize('error', [gsv.DepthPayloadError('unrecognized photometa response'),
                                       requests.ConnectionError('connection reset'),
                                       ValueError('Expecting value: line 1 column 1 (char 0)'),
                                       ImportError('No module named streetlevel'),
                                       KeyError('anything else at all')])
    def test_a_photometa_failure_falls_back_to_the_probe_and_logs_it(self, monkeypatch, caplog, error):
        stub_photometa(monkeypatch, error=error)
        count_probes(monkeypatch, 5)

        with caplog.at_level(logging.WARNING):
            frame = gsv.resolve_frame(pano_info(1024, 512))

        assert frame == gsv.ResolvedFrame(1024, 512, 5, True, None, 'probe')
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any('photometa' in r.getMessage() and PANO in r.getMessage() for r in warnings)
        assert gsv._block_latch_age_hours(gsv.default_block_latch_path()) is None, \
            "only a refusal from Google latches; an ordinary failure says nothing about this host's standing"

    def test_missing_dims_still_cost_nothing(self, monkeypatch):
        asked = stub_photometa(monkeypatch, TWO_LEVELS)
        deny_probe(monkeypatch)

        assert gsv.resolve_frame({'pano_id': PANO, 'width': None, 'height': 512}) is None
        assert gsv.resolve_frame({'pano_id': PANO, 'width': 1024}) is None
        assert asked == []

    def test_a_non_512_tile_size_is_inconsistent(self, monkeypatch):
        stub_photometa(monkeypatch, SERIES_16384, tile_size=(256, 256))
        deny_probe(monkeypatch)

        frame = gsv.resolve_frame(pano_info(16384, 8192))

        assert frame.consistent is False
        assert frame.evidence == 'photometa'
        assert frame.refusal == 'tile_size'
        assert frame.zoom == 5, 'the top level, as reported - not a zoom the stitcher would ever request'

    def test_an_inconsistent_frame_is_reported_not_raised(self, monkeypatch):
        stub_photometa(monkeypatch, SERIES_16384)
        deny_probe(monkeypatch)

        assert gsv.resolve_frame(pano_info(13312, 6656)) == \
            gsv.ResolvedFrame(13312, 6656, 5, False, (16384, 8192), 'photometa', 'frame')


class TestTheImagePhaseSharesTheBlockLatch:
    @pytest.mark.parametrize('error', [gsv.DepthBlockedError('redirected to https://www.google.com/sorry/'),
                                       requests.exceptions.RetryError('too many 429 error responses')])
    def test_a_refusal_writes_the_latch_and_warns_on_both_channels(self, monkeypatch, capsys, caplog, error):
        stub_photometa(monkeypatch, error=error)
        count_probes(monkeypatch, 5)

        with caplog.at_level(logging.ERROR):
            frame = gsv.resolve_frame(pano_info(1024, 512))

        assert frame == gsv.ResolvedFrame(1024, 512, 5, True, None, 'probe')
        age = gsv._block_latch_age_hours(gsv.default_block_latch_path())
        assert age is not None and age < gsv.DEPTH_BLOCK_LATCH_HOURS
        out = capsys.readouterr().out
        assert 'IMAGEDOWNLOAD: WARNING' in out and 'latch' in out
        assert any(r.levelno == logging.ERROR and 'photometa' in r.getMessage() for r in caplog.records)

    def test_a_fresh_latch_means_zero_photometa_requests(self, monkeypatch):
        """Counted, not raised: a stub raising AssertionError is caught by _photometa_levels' own
        `except Exception` and read as a photometa failure, so it could never fail this test (review 5d)."""
        fresh_latch()
        asked = stub_photometa(monkeypatch, TWO_LEVELS)
        count_probes(monkeypatch, 5)

        assert gsv.resolve_frame(pano_info(1024, 512)) == gsv.ResolvedFrame(1024, 512, 5, True, None, 'probe')
        assert asked == []

    def test_an_expired_latch_is_ignored(self, monkeypatch):
        with open(gsv.default_block_latch_path(), 'w') as f:
            f.write(repr(time.time() - (gsv.DEPTH_BLOCK_LATCH_HOURS + 1) * 3600))
        asked = stub_photometa(monkeypatch, [(512, 256), (1024, 512)])
        deny_probe(monkeypatch)

        assert gsv.resolve_frame(pano_info(1024, 512)).evidence == 'photometa'
        assert asked == [PANO]

    def test_an_explicit_latch_path_is_honoured(self, monkeypatch, tmp_path):
        latch = str(tmp_path / 'elsewhere')
        gsv._write_block_latch(latch)
        asked = stub_photometa(monkeypatch, TWO_LEVELS)
        count_probes(monkeypatch, 5)

        assert gsv.resolve_frame(pano_info(1024, 512), block_latch_path=latch).evidence == 'probe'
        assert asked == []

    def test_the_warning_is_printed_once_per_run_not_once_per_pano(self, tmp_path, monkeypatch, capsys):
        """No module state: the first refusal writes the latch, and the second pano reads it and never asks."""
        asked = stub_photometa(monkeypatch, error=gsv.DepthBlockedError('HTTP 403'))
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        for pano_id in ('refusedPanoOneAAAAAAAA', 'refusedPanoTwoAAAAAAAA'):
            gsv.download_single_pano(str(tmp_path), pano_info(1024, 512, pano_id))

        assert capsys.readouterr().out.count('WARNING - Google refused') == 1
        assert asked == ['refusedPanoOneAAAAAAAA']

    def test_the_depth_phase_then_stands_down(self, tmp_path, monkeypatch, capsys):
        """The point of sharing the FILE: a refusal met in the image phase keeps the depth phase, later in the
        same run, from walking back into the wall."""
        stub_photometa(monkeypatch, error=gsv.DepthBlockedError('HTTP 403'))
        count_probes(monkeypatch, 5)
        pytest.importorskip('streetlevel.streetview')

        gsv.resolve_frame(pano_info(1024, 512))
        stop_reasons = {}
        result = gsv._run_depth_phase(str(tmp_path), [{'pano_id': PANO}], stop_reasons=stop_reasons)

        assert result == (0, 0, 0, 0)
        assert stop_reasons['depth_stop'] == gsv.DEPTH_STOP_BLOCKED


class TestResolveZoomAndDimsIsTheProbe:
    """refetch_panos.py's seam keeps its pre-#74 answers (review item 2): the probe alone, no photometa, no
    latch. tests/test_refetch_panos.py::TestRefetchKeepsItsPre74Decisions pins the outcomes through it."""

    def test_it_returns_the_three_tuple_of_a_probe_only_resolve_frame(self, monkeypatch):
        seen = []
        monkeypatch.setattr(gsv, 'resolve_frame', lambda info, **kw: seen.append(kw) or
                            gsv.ResolvedFrame(1, 2, 3, True, None, 'probe'))
        assert gsv.resolve_zoom_and_dims(pano_info(1, 2)) == (1, 2, 3)
        assert seen == [{'photometa': False}]

        monkeypatch.setattr(gsv, 'resolve_frame', lambda info, **kw: None)
        assert gsv.resolve_zoom_and_dims(pano_info(1, 2)) is None

    def test_it_never_asks_photometa(self, monkeypatch):
        """Counted, not raised: _photometa_levels swallows any Exception from the fetch into the fallback,
        so a stub that raised would pass whether or not photometa was asked."""
        asked = stub_photometa(monkeypatch, SERIES_16384)
        count_probes(monkeypatch, 5)

        assert gsv.resolve_zoom_and_dims(pano_info(8192, 4096)) == (8192, 4096, 5)
        assert asked == []

    def test_a_fresh_latch_changes_nothing_here(self, monkeypatch):
        fresh_latch()
        count_probes(monkeypatch, 3)

        assert gsv.resolve_zoom_and_dims(pano_info(5376, 2688)) == (5376, 2688, 3)


# --- the nightly composition -------------------------------------------------------------------------------

# A 16384x8192 frame is past PIL's decompression-bomb pixel count; the stitch is ours, not an upload.
@pytest.mark.filterwarnings('ignore::PIL.Image.DecompressionBombWarning')
class TestDownloadSinglePanoWithReportedLevels:
    def test_a_served_larger_frame_is_refused_not_cropped(self, tmp_path, monkeypatch, capsys, caplog):
        """On master the probe finds zoom 5 and a 26x13 grid is stitched from a 32x16 pano: the top-left 81%,
        saved at 13312x6656 as success. Now it is refused, loudly, and retried next run."""
        stub_photometa(monkeypatch, SERIES_16384)
        deny_probe(monkeypatch)
        stub_tiles(monkeypatch, lambda tile: pytest.fail('a refused frame must not fan out'))

        with caplog.at_level(logging.ERROR):
            with pytest.raises(gsv.FrameDisagreementError) as refused:
                gsv.download_single_pano(str(tmp_path), pano_info(13312, 6656))

        shard = tmp_path / PANO[:2]
        assert list(shard.iterdir()) == []
        out = capsys.readouterr().out
        # scrape.log's line is DownloadRunner's ERROR carrying the exception text (see
        # TestOneErrorLinePerRefusal), so the exception is what must name the pano and both frames.
        for text in (out, str(refused.value)):
            assert PANO in text and '13312x6656' in text and '16384x8192' in text
            assert 'frame disagreement' in text
        assert 'IMAGEDOWNLOAD: WARNING' in out
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    def test_a_lower_matching_level_downloads_the_whole_pano_natively(self, tmp_path, monkeypatch):
        """On master the probe says zoom 5 and this 16x8 grid is requested AT ZOOM 5 - a quarter of the pano."""
        stub_photometa(monkeypatch, SERIES_16384)
        deny_probe(monkeypatch)
        requested = red_tiles(monkeypatch)

        assert gsv.download_single_pano(str(tmp_path), pano_info(8192, 4096)) == DownloadResult.success
        assert all('&zoom=4&' in url for _x, _y, url in requested)
        assert {(x, y) for x, y, _ in requested} == {(x, y) for x in range(16) for y in range(8)}

    # (app frame, reported levels, zoom every tile URL must carry, tile grid, outcome) - plan §3.6's
    # consistent rows. The saved frame is always the APP's, never Google's.
    CONSISTENT_ROWS = [
        ((16384, 8192), SERIES_16384, 5, (32, 16), DownloadResult.success),
        ((3328, 1664), SERIES_3328, 3, (7, 4), DownloadResult.success),
        ((5376, 2688), SERIES_5376, 4, (11, 6), DownloadResult.success),
        ((16384, 8192), SERIES_16384[:4], 3, (8, 4), DownloadResult.fallback_success),
        ((16384, 8192), SERIES_16384[:5], 4, (16, 8), DownloadResult.fallback_success),
        ((8192, 4096), SERIES_16384, 4, (16, 8), DownloadResult.success),
    ]

    @pytest.mark.parametrize('frame,sizes,zoom,grid,outcome', CONSISTENT_ROWS)
    def test_every_consistent_case_fetches_that_level_and_saves_the_apps_frame(
            self, tmp_path, monkeypatch, frame, sizes, zoom, grid, outcome):
        stub_photometa(monkeypatch, sizes)
        deny_probe(monkeypatch)
        requested = red_tiles(monkeypatch)

        assert gsv.download_single_pano(str(tmp_path), pano_info(*frame)) == outcome
        assert all('&zoom=%d&' % zoom in url for _x, _y, url in requested)
        assert {(x, y) for x, y, _ in requested} == {(x, y) for x in range(grid[0]) for y in range(grid[1])}
        # Read from the header: a 16384x8192 decode trips PIL's decompression-bomb warning for nothing.
        assert jpeg_dimensions(str(tmp_path / PANO[:2] / (PANO + '.jpg'))) == frame

    def test_fallback_success_is_still_the_upscale_not_the_zoom_number(self, tmp_path, monkeypatch):
        """Zoom 3 is native for a four-level pano and a 4x upscale for a six-level one cut short; zoom 4 is an
        upscale that no "zoom < 5" or "zoom == 3" rule can see. Only the grid-vs-frame test gets all three.

        The stitch itself is stubbed to a thumbnail (the verdict is fetch_pano_image's grid-vs-frame compare,
        which still runs for real); the full-size stitches of these rows are the parametrized test above."""
        red_tiles(monkeypatch)
        monkeypatch.setattr(gsv, '_stitch_tiles', lambda ok, zoom_dims, final_dims: Image.new('RGB', (8, 4), RED))
        deny_probe(monkeypatch)
        outcomes = {}
        for pano_id, frame, sizes in (('nativeZoom3AAAAAAAAAAA', (3328, 1664), SERIES_3328),
                                      ('cutAtFourAAAAAAAAAAAAA', (16384, 8192), SERIES_16384[:4]),
                                      ('cutAtFiveAAAAAAAAAAAAA', (16384, 8192), SERIES_16384[:5])):
            stub_photometa(monkeypatch, sizes)
            outcomes[pano_id] = gsv.download_single_pano(str(tmp_path), pano_info(*frame, pano_id=pano_id))

        assert outcomes == {'nativeZoom3AAAAAAAAAAA': DownloadResult.success,
                            'cutAtFourAAAAAAAAAAAAA': DownloadResult.fallback_success,
                            'cutAtFiveAAAAAAAAAAAAA': DownloadResult.fallback_success}

    def test_a_gone_pano_is_still_the_permanent_failure_verdict(self, tmp_path, monkeypatch):
        stub_photometa(monkeypatch, gone=True)
        count_probes(monkeypatch, -1)
        requested = red_tiles(monkeypatch)

        assert gsv.download_single_pano(str(tmp_path), pano_info(1024, 512)) == DownloadResult.failure
        assert requested == []

    def test_a_disagreement_is_transient_not_a_verdict(self, tmp_path, monkeypatch):
        """Raised, so DownloadRunner counts it in tonight's failures and does NOT ledger it - the app's frame
        may catch up, and permanence with no GSV breaker is the wrong default for a new evidence source."""
        stub_photometa(monkeypatch, SERIES_3328)
        deny_probe(monkeypatch)

        with pytest.raises(gsv.FrameDisagreementError):
            gsv.download_single_pano(str(tmp_path), pano_info(16384, 8192))
        assert not issubclass(gsv.FrameDisagreementError, (OSError, ValueError))


class TestRequestsPerPano:
    """Plan §3.5, asserted: (photometa, probe, tile) requests per pano."""

    def run(self, tmp_path, monkeypatch, *, sizes=None, gone=False, pick=5, frame=(1024, 512)):
        asked = stub_photometa(monkeypatch, sizes, gone=gone)
        probes = count_probes(monkeypatch, pick)
        tiles = red_tiles(monkeypatch)
        gsv.download_single_pano(str(tmp_path), pano_info(*frame))
        return len(asked), len(probes), len(tiles)

    def test_a_new_live_pano_costs_one_photometa_and_no_probe(self, tmp_path, monkeypatch):
        assert self.run(tmp_path, monkeypatch, sizes=[(512, 256), (1024, 512)]) == (1, 0, 2)

    def test_a_retired_pano_costs_one_photometa_and_two_probes(self, tmp_path, monkeypatch):
        assert self.run(tmp_path, monkeypatch, gone=True, pick=-1) == (1, 2, 0)

    def test_a_latched_run_costs_four_probes_and_no_photometa(self, tmp_path, monkeypatch):
        """Two to pick the zoom and two for the probe arm's frame check (frame_covers_pano)."""
        fresh_latch()
        assert self.run(tmp_path, monkeypatch, sizes=[(512, 256), (1024, 512)], pick=5) == (0, 4, 2)

    def test_a_photometa_failure_costs_one_photometa_and_four_probes(self, tmp_path, monkeypatch):
        asked = stub_photometa(monkeypatch, error=gsv.DepthPayloadError('photometa down'))
        probes = count_probes(monkeypatch, 5)
        tiles = red_tiles(monkeypatch)
        gsv.download_single_pano(str(tmp_path), pano_info(1024, 512))
        assert (len(asked), len(probes), len(tiles)) == (1, 4, 2)

    def test_a_pano_already_on_disk_costs_nothing(self, tmp_path, monkeypatch):
        shard = tmp_path / PANO[:2]
        shard.mkdir()
        (shard / (PANO + '.jpg')).write_bytes(b'already here')
        assert self.run(tmp_path, monkeypatch, sizes=[(512, 256), (1024, 512)]) == (0, 0, 0)


# --- the probe arm checks the frame too (review item 1) ------------------------------------------------------

def serve_pyramid(monkeypatch, sizes):
    """A CBK stand-in for a pano Google serves as `sizes`: a tile is imagery iff it lies inside the level at its
    zoom, black otherwise - Google's answer for an out-of-range tile. The probe's tiles and frame_covers_pano's
    both go through _get_response, so this is the whole of CBK the probe arm can see. Returns the URLs asked."""
    requested = []
    black, grey = jpeg_bytes((0, 0, 0), (16, 16)), jpeg_bytes((90, 90, 90), (16, 16))

    def fake_get_response(url, session, stream=False):
        requested.append(url)
        query = dict(part.split('=', 1) for part in url.split('?', 1)[1].split('&'))
        zoom, x, y = int(query['zoom']), int(query['x']), int(query['y'])
        inside = zoom < len(sizes) and x * gsv.TILE_SIZE < sizes[zoom][0] and y * gsv.TILE_SIZE < sizes[zoom][1]
        return io.BytesIO(grey if inside else black)

    monkeypatch.setattr(gsv, '_get_response', fake_get_response)
    return requested


@pytest.mark.filterwarnings('ignore::PIL.Image.DecompressionBombWarning')
class TestTheProbeArmChecksTheFrame:
    """The probe answers "is there imagery at zoom 5 or 3 at (0, 0)?", which a crop passes. Before the review
    fix an 8192x4096 app frame on a pano Google serves at 16384 was stitched as the top-left quarter and
    ledgered downloaded=1 on any night photometa did not answer - so a photometa refusal was only a delay."""

    @pytest.mark.parametrize('frame', [(8192, 4096), (13312, 6656)])
    @pytest.mark.parametrize('why', ['failure', 'latch', 'not_found'])
    def test_a_frame_smaller_than_google_serves_is_refused_and_saves_nothing(
            self, tmp_path, monkeypatch, capsys, frame, why):
        if why == 'latch':
            fresh_latch()
        stub_photometa(monkeypatch, SERIES_16384, gone=(why == 'not_found'),
                       error=gsv.DepthPayloadError('photometa down') if why == 'failure' else None)
        requested = serve_pyramid(monkeypatch, SERIES_16384)
        stub_tiles(monkeypatch, lambda tile: pytest.fail('a refused frame must not fan out'))

        with pytest.raises(gsv.FrameDisagreementError) as refused:
            gsv.download_single_pano(str(tmp_path), pano_info(*frame))

        assert list((tmp_path / PANO[:2]).iterdir()) == []
        assert len(requested) == 3, 'two probe tiles, then the first tile past the grid - never the fan-out'
        assert 'frame disagreement' in str(refused.value) and 'tile probe' in str(refused.value)
        assert 'frame disagreement' in capsys.readouterr().out

    @pytest.mark.parametrize('frame,sizes,outcome', [((16384, 8192), SERIES_16384, DownloadResult.success),
                                                      ((3328, 1664), SERIES_3328, DownloadResult.success)])
    def test_a_frame_that_is_what_google_serves_stitches_as_before(self, tmp_path, monkeypatch, frame, sizes,
                                                                    outcome):
        stub_photometa(monkeypatch, error=gsv.DepthPayloadError('photometa down'))
        requested = serve_pyramid(monkeypatch, sizes)
        tiles = red_tiles(monkeypatch)

        assert gsv.download_single_pano(str(tmp_path), pano_info(*frame)) == outcome
        assert len(requested) == 4
        assert jpeg_dimensions(str(tmp_path / PANO[:2] / (PANO + '.jpg'))) == frame
        assert tiles, 'the fan-out ran'

    def test_the_check_asks_at_the_zoom_the_probe_chose(self, tmp_path, monkeypatch):
        """Round two: a check hard-coded to zoom 5 survived every test above, since each one that reaches it at
        zoom 3 has a frame that passes - and it would switch the check off for every zoom-3 fallback. A
        four-level pano (3328 top) whose app frame is its zoom-2 level, 1664x832: the probe finds zoom 5 black
        and zoom 3 live, so the fan-out would be a 4x2 grid at zoom 3, the top-left quarter of a 7x4 level. At
        zoom 5 the check finds nothing past any grid and passes it."""
        stub_photometa(monkeypatch, error=gsv.DepthPayloadError('photometa down'))
        requested = serve_pyramid(monkeypatch, SERIES_3328)
        stub_tiles(monkeypatch, lambda tile: pytest.fail('a refused frame must not fan out'))

        with pytest.raises(gsv.FrameDisagreementError):
            gsv.download_single_pano(str(tmp_path), pano_info(1664, 832))
        assert requested[2:] and all('zoom=3' in url for url in requested[2:])

    def test_the_check_is_not_spent_when_photometa_answered(self, tmp_path, monkeypatch):
        stub_photometa(monkeypatch, SERIES_16384)
        monkeypatch.setattr(gsv, 'frame_covers_pano', lambda *a: pytest.fail('photometa already decided'))
        deny_probe(monkeypatch)
        red_tiles(monkeypatch)

        assert gsv.download_single_pano(str(tmp_path), pano_info(16384, 8192)) == DownloadResult.success


class TestTheRefusalNamesItsReason:
    def test_a_non_512_tile_refusal_says_tiles_not_levels(self, tmp_path, monkeypatch):
        """The reviewers' repro read "no reported level admits it" over two identical frames (NIT)."""
        stub_photometa(monkeypatch, SERIES_16384, tile_size=(256, 256))
        deny_probe(monkeypatch)
        stub_tiles(monkeypatch, lambda tile: pytest.fail('a refused frame must not fan out'))

        with pytest.raises(gsv.FrameDisagreementError) as refused:
            gsv.download_single_pano(str(tmp_path), pano_info(16384, 8192))

        assert 'frame disagreement' in str(refused.value)
        assert 'tiles that are not 512 px' in str(refused.value)
        assert 'no reported level' not in str(refused.value)


class TestOneErrorLinePerRefusal:
    def test_the_runner_logs_one_error_naming_the_disagreement(self, tmp_path, monkeypatch, caplog):
        """scrape.log carries one ERROR per refused pano - DownloadRunner's, with the exception text - so
        `grep "frame disagreement" scrape.log` counts refusals."""
        import DownloadRunner
        stub_photometa(monkeypatch, SERIES_16384)
        deny_probe(monkeypatch)
        stub_tiles(monkeypatch, lambda tile: pytest.fail('a refused frame must not fan out'))

        with caplog.at_level(logging.WARNING):
            DownloadRunner.download_panorama_images(
                str(tmp_path), [dict(pano_info(13312, 6656), source='gsv')])

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert 'frame disagreement' in errors[0] and PANO in errors[0]


# --- per-run memory, the once-per-run line, and the host state (review items 4, 6, 7, 10) --------------------

def stub_photometa_answers(monkeypatch, answers):
    """_fetch_image_levels answering from a script, one entry per call: an Exception is raised, anything else
    is a sizes list answered as 512 px levels. Returns the pano ids asked, in order."""
    asked = []
    script = list(answers)

    def fake_fetch_image_levels(pano_id, session):
        asked.append(pano_id)
        answer = script.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return gsv.ImageLevels([tuple(size) for size in answer], (512, 512))

    monkeypatch.setattr(gsv, '_fetch_image_levels', fake_fetch_image_levels)
    return asked


def download_all(tmp_path, count, prefix):
    for i in range(count):
        pano_id = ('%s%02d' % (prefix, i)).ljust(22, 'A')
        gsv.download_single_pano(str(tmp_path), pano_info(1024, 512, pano_id))



class TestPhotometaFailuresAreRememberedForTheRun:
    """Review item 4: a photometa fault that is not a refusal used to cost every new pano the whole retry
    policy (~3.5 minutes at the production timeout) with nothing on stdout."""

    def test_five_panos_against_a_failing_photometa_make_three_photometa_calls(self, tmp_path, monkeypatch,
                                                                              capsys, caplog):
        asked = stub_photometa(monkeypatch, error=gsv.DepthPayloadError('photometa down'))
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        with caplog.at_level(logging.WARNING):
            download_all(tmp_path, 5, 'failingPano')

        assert len(asked) == gsv.PHOTOMETA_MAX_CONSECUTIVE_FAILURES == 3
        out = capsys.readouterr().out
        assert out.count('photometa did not answer') == 1, 'named once per run, not once per pano'
        assert sum('photometa did not answer' in r.getMessage() for r in caplog.records) == 1
        assert len(list(tmp_path.rglob('*.jpg'))) == 5, 'every pano still downloads, from the probe'
        assert gsv._block_latch_age_hours(gsv.default_block_latch_path()) is None

    def test_only_consecutive_failures_count(self, tmp_path, monkeypatch):
        down = gsv.DepthPayloadError('photometa down')
        asked = stub_photometa_answers(monkeypatch, [down, down, TWO_LEVELS, down, down, TWO_LEVELS])
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        download_all(tmp_path, 6, 'flakyPano')

        assert len(asked) == 6

    def test_a_not_found_is_an_answer_and_resets_the_count(self, tmp_path, monkeypatch):
        """Round two: "only a levels answer resets the count" survived. Code 2 is photometa answering, so fail,
        fail, not-found, fail, fail leaves the count at 2 and the sixth pano still asks."""
        asked = []
        script = ['fail', 'fail', 'gone', 'fail', 'fail', 'levels']

        def fake_fetch_image_levels(pano_id, session):
            asked.append(pano_id)
            answer = script.pop(0)
            if answer == 'fail':
                raise gsv.DepthPayloadError('photometa down')
            return None if answer == 'gone' else gsv.ImageLevels([tuple(size) for size in TWO_LEVELS], (512, 512))

        monkeypatch.setattr(gsv, '_fetch_image_levels', fake_fetch_image_levels)
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        download_all(tmp_path, 6, 'goneResetsPano')

        assert len(asked) == 6

    def test_a_refusal_after_failures_still_latches_and_warns(self, tmp_path, monkeypatch, capsys):
        down = gsv.DepthPayloadError('photometa down')
        asked = stub_photometa_answers(monkeypatch, [down, down, gsv.DepthBlockedError('HTTP 403')])
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        download_all(tmp_path, 4, 'refusedLatePano')

        assert len(asked) == 3
        age = gsv._block_latch_age_hours(gsv.default_block_latch_path())
        assert age is not None and age < gsv.DEPTH_BLOCK_LATCH_HOURS
        out = capsys.readouterr().out
        assert out.count('WARNING - Google refused') == 1

    def test_a_refusal_with_an_unwritable_latch_is_still_remembered_for_the_run(self, tmp_path, monkeypatch):
        """Round two F1: the latch file is the cross-process record of a refusal, but _write_block_latch never
        raises - a --depth-block-latch in a missing directory, or a full temp dir, leaves no file. The run must
        still stop asking a host that just refused it, or every new pano pays the refusal's retry back-off."""
        monkeypatch.setattr(gsv, 'image_block_latch_path', str(tmp_path / 'no-such-dir' / 'latch'))
        asked = stub_photometa(monkeypatch, error=gsv.DepthBlockedError('HTTP 403'))
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        download_all(tmp_path, 4, 'unlatchablePano')

        assert gsv._block_latch_age_hours(gsv.image_block_latch_path) is None, 'the latch really was unwritable'
        assert len(asked) == 1, 'a refused host was asked %d times in one run' % len(asked)
        assert len(list(tmp_path.rglob('*.jpg'))) == 4, 'every pano still downloads, from the probe'

    def test_both_channels_give_the_latch_the_same_reach(self, tmp_path, monkeypatch, capsys, caplog):
        """Round two: stdout said "next 6 hours, on every city" while scrape.log said "the rest of this run"."""
        stub_photometa(monkeypatch, error=gsv.DepthBlockedError('HTTP 403'))
        count_probes(monkeypatch, 5)

        with caplog.at_level(logging.WARNING):
            gsv.resolve_frame(pano_info(1024, 512))

        logged = [r.getMessage() for r in caplog.records if 'Google refused' in r.getMessage()]
        reach = 'next %g hours, on every city this host runs' % gsv.DEPTH_BLOCK_LATCH_HOURS
        assert len(logged) == 1 and reach in logged[0] and 'rest of this run' not in logged[0]
        assert reach in capsys.readouterr().out

    def test_a_refusal_is_the_runs_only_fallback_line(self, tmp_path, monkeypatch, capsys):
        """The refusal WARNING already says the probe answers from here on; the latched panos after it must
        not add the latch line on top."""
        stub_photometa(monkeypatch, error=gsv.DepthBlockedError('HTTP 403'))
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        download_all(tmp_path, 3, 'refusedFirstPano')

        narrative = [line for line in capsys.readouterr().out.splitlines()
                     if line.startswith('IMAGEDOWNLOAD: ')]
        assert len(narrative) == 1 and 'Google refused' in narrative[0]


class TestAFreshLatchSaysSoOnStdout:
    """Review item 10: under a fresh latch the image phase ran on the probe with nothing on stdout."""

    def test_one_line_per_run_naming_the_latch_and_its_reach(self, tmp_path, monkeypatch, capsys, caplog):
        fresh_latch()
        stub_photometa(monkeypatch, TWO_LEVELS)
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        with caplog.at_level(logging.WARNING):
            download_all(tmp_path, 3, 'latchedPano')

        lines = [line for line in capsys.readouterr().out.splitlines() if 'block latch' in line]
        assert len(lines) == 1
        assert 'hours' in lines[0] and 'every city' in lines[0]
        assert sum('block latch' in r.getMessage() for r in caplog.records) == 1


class TestARefusalForfeitsTheEarnedDepthPace:
    """Review item 7, shipped default D6: an image-phase refusal forfeits the depth pacer's earned standing,
    exactly as a depth-phase refusal does - the latch expires after 6 hours, the earned pace after 24."""

    @pytest.fixture
    def earned(self, monkeypatch):
        monkeypatch.setattr(gsv, 'depth_start_interval', 1.0)
        monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.25)
        gsv._write_pace_state(gsv.default_pace_state_path(), 0.25, 150)
        assert gsv._load_pace_state(gsv.default_pace_state_path()) == (0.25, 150)

    def test_a_refusal_resets_it_to_the_opening_interval(self, monkeypatch, earned):
        stub_photometa(monkeypatch, error=gsv.DepthBlockedError('HTTP 403'))
        count_probes(monkeypatch, 5)

        gsv.resolve_frame(pano_info(1024, 512))

        assert gsv._load_pace_state(gsv.default_pace_state_path()) == (1.0, 0)
        assert gsv.DepthPacer(state_path=gsv.default_pace_state_path()).interval == 1.0

    def test_an_ordinary_failure_leaves_it_alone(self, monkeypatch, earned):
        stub_photometa(monkeypatch, error=gsv.DepthPayloadError('photometa down'))
        count_probes(monkeypatch, 5)

        gsv.resolve_frame(pano_info(1024, 512))

        assert gsv._load_pace_state(gsv.default_pace_state_path()) == (0.25, 150)


class TestTheFlagsMoveTheImagePhasesHostStateToo:
    """Review item 6: with --depth-block-latch set, the image phase used to read and write the DEFAULT latch
    while the depth phase read the flagged one - so an image-phase refusal promised a stand-down on stdout
    and the depth phase then sent its requests anyway."""

    def call_main(self, tmp_path, monkeypatch, *flags):
        import DownloadRunner
        csv_path = tmp_path / 'panos.csv'
        csv_path.write_text('pano_id,width,height,lat,lng,camera_heading,camera_pitch,source,has_labels\n'
                            '%s,1024,512,47.6,-122.3,180.0,0.0,gsv,True\n' % PANO)
        monkeypatch.chdir(tmp_path)
        DownloadRunner.main(['sidewalk-test.invalid', str(tmp_path / 'storage'), '-c', str(csv_path),
                             '--skip-depth', *flags])

    def test_a_refusal_writes_the_flagged_latch_and_pace_state(self, tmp_path, monkeypatch):
        latch, pace = tmp_path / 'my-latch', tmp_path / 'my-pace'
        stub_photometa(monkeypatch, error=gsv.DepthBlockedError('HTTP 403'))
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        self.call_main(tmp_path, monkeypatch, '--depth-block-latch', str(latch), '--depth-pace-state', str(pace))

        assert gsv._block_latch_age_hours(str(latch)) is not None
        assert gsv._block_latch_age_hours(gsv.default_block_latch_path()) is None
        assert pace.exists() and not os.path.exists(gsv.default_pace_state_path())

    def test_a_second_main_in_one_process_asks_photometa_again(self, tmp_path, monkeypatch):
        gsv._photometa_run.given_up = True          # what a previous in-process city left behind
        asked = stub_photometa(monkeypatch, TWO_LEVELS)
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        self.call_main(tmp_path, monkeypatch)

        assert asked == [PANO]

    def test_a_fresh_flagged_latch_is_what_the_image_phase_reads(self, tmp_path, monkeypatch):
        latch = tmp_path / 'my-latch'
        gsv._write_block_latch(str(latch))
        asked = stub_photometa(monkeypatch, TWO_LEVELS)
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        self.call_main(tmp_path, monkeypatch, '--depth-block-latch', str(latch))

        assert asked == []
        assert (tmp_path / 'storage' / PANO[:2] / (PANO + '.jpg')).exists()


class TestRunOwnsTheImagePhasesPerRunState:
    """Round two: the per-run photometa memory and the image phase's host-state paths were set in main() only,
    so run() - the in-process seam tests and any multi-city caller use - inherited the last run's given_up and
    read the default latch while its depth phase read depth_block_latch."""

    @staticmethod
    def call_run(tmp_path, **kwargs):
        import DownloadRunner
        csv_path = tmp_path / 'panos.csv'
        csv_path.write_text('pano_id,width,height,lat,lng,camera_heading,camera_pitch,source,has_labels\n'
                            '%s,1024,512,47.6,-122.3,180.0,0.0,gsv,True\n' % PANO)
        os.makedirs(tmp_path / 'storage', exist_ok=True)
        DownloadRunner.run('sidewalk-test.invalid', str(tmp_path / 'storage'), pano_metadata_csv=str(csv_path),
                           skip_depth=True, **kwargs)

    def test_each_run_starts_with_a_fresh_photometa_memory(self, tmp_path, monkeypatch):
        gsv._photometa_run.given_up = True          # what a previous in-process city left behind
        asked = stub_photometa(monkeypatch, TWO_LEVELS)
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        self.call_run(tmp_path)

        assert asked == [PANO]

    def test_run_hands_its_latch_to_the_image_phase(self, tmp_path, monkeypatch):
        latch = tmp_path / 'my-latch'
        gsv._write_block_latch(str(latch))
        asked = stub_photometa(monkeypatch, TWO_LEVELS)
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        self.call_run(tmp_path, depth_block_latch=str(latch))

        assert asked == []
        assert (tmp_path / 'storage' / PANO[:2] / (PANO + '.jpg')).exists()

    def test_run_hands_its_pace_state_to_the_image_phase(self, tmp_path, monkeypatch):
        pace = tmp_path / 'my-pace'
        stub_photometa(monkeypatch, error=gsv.DepthBlockedError('HTTP 403'))
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        self.call_run(tmp_path, depth_block_latch=str(tmp_path / 'my-latch'), depth_pace_state=str(pace))

        assert pace.exists() and not os.path.exists(gsv.default_pace_state_path())


# --- review item 5: the holes the tests lens found ----------------------------------------------------------

class TestTheFallbackSeamsStillRaiseAndStillFallBack:
    def test_a_probe_network_failure_raises_through_both_seams(self, monkeypatch):
        """refetch_panos books resolve_zoom_and_dims' None as the PERMANENT 'gone', so a wrapper that swallowed
        a probe blip into None would ledger a live pano as retired (review 5b)."""
        stub_photometa(monkeypatch, error=gsv.DepthPayloadError('photometa down'))

        def blip(url, session, stream=False):
            raise requests.ConnectionError('probe blip')

        monkeypatch.setattr(gsv, '_get_response', blip)
        with pytest.raises(requests.ConnectionError):
            gsv.resolve_frame(pano_info(1024, 512))
        with pytest.raises(requests.ConnectionError):
            gsv.resolve_zoom_and_dims(pano_info(1024, 512))

    @pytest.mark.parametrize('msg2', [[None, None, None], [None, None, None, [[], [512, 512]]]],
                             ids=['no-image-sizes', 'empty-image-sizes'])
    def test_an_ok_envelope_without_levels_is_photometa_trouble(self, monkeypatch, msg2):
        """Not "gone" (None) and not an IndexError out of resolve_frame: photometa trouble, so the probe
        answers (review 5c; gsv.py's unreadable-levels lines were uncovered locally and in CI)."""
        api = pytest.importorskip('streetlevel.streetview.api')
        monkeypatch.setattr(api, 'find_panorama_by_id', lambda *a, **k: [None, [[[1], [None, PANO], msg2]]])

        with pytest.raises(gsv.DepthPayloadError):
            gsv._fetch_image_levels(PANO, object())

        count_probes(monkeypatch, 5)
        monkeypatch.setattr(api, 'find_panorama_by_id', lambda *a, **k: [None, [[[1], [None, PANO], msg2]]])
        assert gsv.resolve_frame(pano_info(1024, 512)) == gsv.ResolvedFrame(1024, 512, 5, True, None, 'probe')

    def test_response_code_3_is_read_as_levels(self, monkeypatch):
        """1 and 3 are both OK in the envelope both seams share; nothing fed a code 3 before (review NIT)."""
        api = pytest.importorskip('streetlevel.streetview.api')
        monkeypatch.setattr(api, 'find_panorama_by_id', lambda *a, **k: [
            None, [[[3], [None, PANO], [None, None, None, [[[[256, 512]], [[512, 1024]]], [512, 512]]]]]])

        assert gsv._fetch_image_levels(PANO, object()) == gsv.ImageLevels([(512, 256), (1024, 512)], (512, 512))


class TestNoPerPanoWarningOnAFallbackRun:
    """The once-per-run test counts one exact phrase; any OTHER per-pano WARNING on a fallback path - one
    cron-mail line per new pano for six hours - passed it (review 5e)."""

    def test_a_latched_run_prints_no_warning_at_all(self, tmp_path, monkeypatch, capsys):
        fresh_latch()
        stub_photometa(monkeypatch, TWO_LEVELS)
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        download_all(tmp_path, 3, 'latchedQuietPano')

        assert 'WARNING' not in capsys.readouterr().out

    def test_a_failing_photometa_run_prints_one_warning_however_many_panos(self, tmp_path, monkeypatch, capsys):
        stub_photometa(monkeypatch, error=gsv.DepthPayloadError('photometa down'))
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        download_all(tmp_path, 5, 'failingQuietPano')

        assert capsys.readouterr().out.count('WARNING') == 1

    def test_a_refused_run_prints_one_warning_however_many_panos(self, tmp_path, monkeypatch, capsys):
        stub_photometa(monkeypatch, error=gsv.DepthBlockedError('HTTP 403'))
        count_probes(monkeypatch, 5)
        red_tiles(monkeypatch)

        download_all(tmp_path, 3, 'refusedQuietPano')

        assert capsys.readouterr().out.count('WARNING') == 1
