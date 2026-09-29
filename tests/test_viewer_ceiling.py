"""Tests for the viewer-ceiling tripwire (#121).

Pannellum uploads an equirectangular image as two half-width textures, so the widest panorama a device can
render is 2 x MAX_TEXTURE_SIZE. The 8192-class GPUs - most of the non-Apple fleet - therefore top out at
16384, which is exactly GSV's widest frame today. This repo is the only place that sees a source's reported
width at the moment it changes, so each downloader warns, on BOTH channels, when a frame is wider than that.

What these pin:

* the ceiling's value, and that it is not derived from the display-copy cap (a different number that only
  happens to be half of it today);
* the boundary - 16384 itself is the fleet's normal and must stay silent, 16385 is the first width that fires;
* both channels: `logging` (durable, `scrape.log`) AND `print` (stdout, what the alarm wrapper delivers). The
  repo's rule is that a warning that matters goes to both, so each assertion checks `caplog` and `capsys`;
* that the tripwire never refuses or alters a download - it is an observation, not a gate;
* that each of the three downloaders actually calls it, at the point it learns the frame's width.

Network-free: the GSV probe and the Mapillary/Panoramax sessions are stubbed at the module boundary with the
same fakes their own test modules use.
"""

import ast
import builtins
import io
import logging
import os
import pathlib

import pytest
from PIL import Image

import downloaders
from downloaders import common, gsv
from downloaders.common import DownloadResult
from test_gsv_stitcher import stub_probe
from test_image_downloaders import (MAPILLARY_PANO, PANORAMAX_PANO, PANORAMAX_SHARD, PANORAMIC_ITEM,
                                    FakeResponse, FakeSession, panoramax_session)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CEILING = 16384
WIDE = CEILING + 1


def wide_jpeg_bytes(width, height=2):
    """A real JPEG of the given width. Two rows high so a 16385-wide frame costs a few KB, not 400 MB: only
    the SOF header's width is under test, and every reader here takes it from the header."""
    buf = io.BytesIO()
    Image.new('RGB', (width, height), (120, 120, 120)).save(buf, 'jpeg')
    return buf.getvalue()


def tripwire_records(caplog):
    return [r for r in caplog.records if 'viewer ceiling' in r.getMessage()]


def tripwire_lines(out):
    return [line for line in out.splitlines() if 'viewer ceiling' in line]


class TestTheCeiling:
    def test_it_is_2_x_8192(self):
        """Pinned as a number. 16384 = 2 x 8192, the MAX_TEXTURE_SIZE of a Pixel 7 Pro's Mali-G710, measured."""
        assert common.VIEWER_MAX_PANO_WIDTH == CEILING == 2 * 8192

    def test_it_is_not_derived_from_the_display_copy_cap(self):
        """The two are different facts that happen to be in a 2:1 ratio today. The display-copy cap is a
        choice about how wide a copy to write, and can be lowered to save disk; the ceiling is a property of
        the devices, and lowering the cap does not change what a Mali-G710 can texture. Written as
        `2 * DOWNSCALED_MAX_WIDTH`, the tripwire would silently move with the cap - a mutant the value pin
        above cannot see, since the two expressions are equal today."""
        with open(os.path.join(REPO_ROOT, 'downloaders', 'common.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'VIEWER_MAX_PANO_WIDTH' for t in node.targets)]
        assert len(assignments) == 1
        names = {n.id for n in ast.walk(assignments[0].value) if isinstance(n, ast.Name)}
        assert 'DOWNSCALED_MAX_WIDTH' not in names


class TestTheHelper:
    def test_a_frame_at_the_ceiling_is_silent(self, caplog, capsys):
        """16384 is GSV's widest frame today and the store holds hundreds of thousands of them. A warning
        there would fire on every new pano every night and train everyone to ignore it."""
        with caplog.at_level(logging.WARNING):
            assert common.warn_if_wider_than_viewer_ceiling('p', CEILING, 'gsv') is False

        assert tripwire_records(caplog) == []
        assert capsys.readouterr().out == ''

    def test_one_pixel_over_fires_on_both_channels(self, caplog, capsys):
        with caplog.at_level(logging.WARNING):
            assert common.warn_if_wider_than_viewer_ceiling('panoIdX', WIDE, 'gsv') is True

        records = tripwire_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        lines = tripwire_lines(capsys.readouterr().out)
        assert len(lines) == 1

    @pytest.mark.parametrize('channel', ['log', 'stdout'])
    def test_each_channel_names_the_pano_the_width_the_source_and_what_to_do(self, caplog, capsys, channel):
        """Each channel must stand on its own: `scrape.log` is read next week by someone who never saw the
        mail, and the alarm wrapper cuts the middle of a long night's stdout, so a line that only makes sense
        next to another line may arrive alone.

        What to do is "verify, then budget the disk", pointing at the runbook section - NOT "set the switch
        and run downscale_panos.py" (#153 m6). The line is written to be acted on alone, and that remedy is
        a fleet-wide +63% sweep whose disk budget docs/ops.md puts first; a line that skips the budget is an
        instruction to fill the store."""
        with caplog.at_level(logging.WARNING):
            common.warn_if_wider_than_viewer_ceiling('panoIdX', 20480, 'panoramax')

        if channel == 'log':
            (text,) = [r.getMessage() for r in tripwire_records(caplog)]
        else:
            (text,) = tripwire_lines(capsys.readouterr().out)
        for fragment in ('panoIdX', '20480', '16384', 'panoramax', '#121', 'Verify',
                         'budget the disk before any sweep', "docs/ops.md, 'The width tripwire'"):
            assert fragment in text, fragment
        assert 'Set WRITE_DISPLAY_COPIES' not in text
        assert "'Display copies of wide panoramas'" not in text

    def test_it_writes_nothing_when_it_fires(self, tmp_path, monkeypatch, caplog, capsys):
        """An observation, not a writer: no display copy, no file of any kind. The switch-reading rule in
        tests/test_downscaled_sidecar.py::TestTheSwitch is about callers of the sidecar primitives and
        exempts common.py, where this helper lives - so this test is the only guard that it never becomes one.

        The helper takes no path, so "the store is still empty" alone proves nothing (#153 m15: the test this
        replaces asserted exactly that, against a tmp_path nothing had been pointed at). Instead every way a
        write could happen is made to fail the test the moment it is reached - the sidecar primitives, the
        encoder they end in, the atomic rename, and `open` in any writing mode - with the working directory
        moved into tmp_path so a relative write the guards missed would still show up there.

        Kills: the helper calling write_downscaled_sidecar / write_downscaled_sidecar_from_file /
        _write_reduced, saving an Image, opening any file for writing (builtins.open, io.open, os.open with a
        writing flag), or Path.write_text / write_bytes (#153 final F8) - including behind an
        `except BaseException`, which would swallow pytest.fail: every guard also records what reached it,
        and the list is asserted empty after the call.

        Not a complete guard on its own, and not meant to be (#153 final F16): it sees only the path the
        helper takes for this width and source. The lexical test below is its pair - a writer under a
        condition this call does not set up passes here and is caught there, and a writer spelled in a way
        the lexical set does not name (a new API) passes there and is caught here if this call reaches it.
        """
        reached = []

        def forbidden(name):
            def fail(*args, **kwargs):
                reached.append(name)
                pytest.fail('the tripwire reached %s' % name)
            return fail

        for name in ('write_downscaled_sidecar', 'write_downscaled_sidecar_from_file', '_write_reduced',
                     'atomic_output_path'):
            monkeypatch.setattr(common, name, forbidden('common.%s' % name))
        monkeypatch.setattr(Image.Image, 'save', forbidden('PIL.Image.Image.save'))
        monkeypatch.setattr(os, 'replace', forbidden('os.replace'))
        monkeypatch.setattr(pathlib.Path, 'write_text', forbidden('pathlib.Path.write_text'))
        monkeypatch.setattr(pathlib.Path, 'write_bytes', forbidden('pathlib.Path.write_bytes'))
        real_open, real_os_open = builtins.open, os.open

        def read_only_open(file, mode='r', *args, **kwargs):
            if any(flag in mode for flag in 'wax+'):
                reached.append('open(%r, %r)' % (file, mode))
                pytest.fail('the tripwire opened %r with mode %r' % (file, mode))
            return real_open(file, mode, *args, **kwargs)

        writing_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

        def read_only_os_open(path, flags, *args, **kwargs):
            if flags & writing_flags:
                reached.append('os.open(%r, %#x)' % (path, flags))
                pytest.fail('the tripwire os.open()ed %r for writing' % (path,))
            return real_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(builtins, 'open', read_only_open)
        monkeypatch.setattr(io, 'open', read_only_open)
        monkeypatch.setattr(os, 'open', read_only_os_open)
        monkeypatch.chdir(tmp_path)

        with caplog.at_level(logging.WARNING):
            assert common.warn_if_wider_than_viewer_ceiling('panoIdX', WIDE, 'gsv') is True

        assert reached == []
        assert len(tripwire_records(caplog)) == 1       # it really fired, so the guards were really in play
        assert list(tmp_path.iterdir()) == []

    def test_its_body_names_no_sidecar_primitive(self):
        """The lexical half. The runtime guard above sees only the path the helper takes today; a writer
        reached under a condition that test does not set up (a width, a source) would pass it. So the
        function's own source may not reference any of the primitives at all.

        The two kill mutants as a PAIR (#153 final F16), not each alone: this one cannot see a writer it
        has no name for, and the runtime one cannot see a branch its call does not take. `open` covers
        builtins.open, io.open and os.open alike, since an attribute is matched by its name."""
        with open(os.path.join(REPO_ROOT, 'downloaders', 'common.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        (helper,) = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == 'warn_if_wider_than_viewer_ceiling']
        referenced = {n.id for n in ast.walk(helper) if isinstance(n, ast.Name)}
        referenced |= {n.attr for n in ast.walk(helper) if isinstance(n, ast.Attribute)}

        writers = {'write_downscaled_sidecar', 'write_downscaled_sidecar_from_file', '_write_reduced',
                   'atomic_output_path', 'downscaled_sidecar_path', 'save', 'open', 'write_text',
                   'write_bytes'}
        assert referenced & writers == set()


class TestGsvWiring:
    PANO_ID = 'stitchPanoAAAAAAAAAAAA'

    def test_resolve_zoom_and_dims_warns_on_a_wide_reported_frame(self, monkeypatch, caplog, capsys):
        stub_probe(monkeypatch, pick_zoom=5)

        with caplog.at_level(logging.WARNING):
            resolved = gsv.resolve_zoom_and_dims({'pano_id': self.PANO_ID, 'width': WIDE, 'height': 8192})

        # Never refuses: the frame comes back exactly as reported, so the download proceeds unaltered.
        assert resolved == (WIDE, 8192, 5)
        (record,) = tripwire_records(caplog)
        assert self.PANO_ID in record.getMessage() and 'gsv' in record.getMessage()
        assert len(tripwire_lines(capsys.readouterr().out)) == 1

    def test_the_fleets_normal_frame_is_silent(self, monkeypatch, caplog, capsys):
        stub_probe(monkeypatch, pick_zoom=5)

        with caplog.at_level(logging.WARNING):
            gsv.resolve_zoom_and_dims({'pano_id': self.PANO_ID, 'width': CEILING, 'height': 8192})

        assert tripwire_records(caplog) == []
        assert tripwire_lines(capsys.readouterr().out) == []

    def test_it_fires_before_any_request_is_spent(self, monkeypatch, caplog, capsys):
        """The reported width is the observation, and it is in hand before the probe. A probe that then fails
        - a transient, which raises and retries tomorrow - must not swallow tonight's warning."""
        def network_down(*args, **kwargs):
            raise ConnectionError('network down')

        monkeypatch.setattr(gsv, '_get_response', network_down)

        with caplog.at_level(logging.WARNING), pytest.raises(ConnectionError):
            gsv.resolve_zoom_and_dims({'pano_id': self.PANO_ID, 'width': WIDE, 'height': 8192})

        assert len(tripwire_records(caplog)) == 1
        assert len(tripwire_lines(capsys.readouterr().out)) == 1

    @pytest.mark.parametrize('width, fires', [(str(WIDE), 1), ('9000', 0)])
    def test_string_dims_are_compared_as_numbers(self, monkeypatch, caplog, width, fires):
        """/adminapi/panos and a hand-made CSV hand dims over as strings. A string against 16384 is a
        TypeError, and two strings compare backwards: '9000' > '16384'."""
        stub_probe(monkeypatch, pick_zoom=5)

        with caplog.at_level(logging.WARNING):
            gsv.resolve_zoom_and_dims({'pano_id': self.PANO_ID, 'width': width, 'height': '8192'})

        assert len(tripwire_records(caplog)) == fires

    def test_the_nightly_photometa_path_warns_before_its_request(self, monkeypatch, caplog, capsys):
        """The nightly downloader reaches the tripwire through resolve_frame's photometa arm, not the
        resolve_zoom_and_dims wrapper - which since #74 is the probe-only seam refetch composes. Every test
        above goes through the wrapper, so without this one the call could move back into it (where it would
        fire only for refetch) with the suite green. The warning must also land before photometa is asked,
        for the same reason the probe test above gives."""
        sizes = [gsv._dims_at_zoom(WIDE, 8192, k) for k in range(gsv._pano_max_zoom(WIDE) + 1)]
        seen_before_request = []

        def fake_fetch_image_levels(pano_id, session):
            seen_before_request.append(len(tripwire_records(caplog)))
            return gsv.ImageLevels(sizes, (gsv.TILE_SIZE, gsv.TILE_SIZE))

        def no_probe(*args, **kwargs):
            raise AssertionError('photometa answered, so the probe must not run')

        monkeypatch.setattr(gsv, '_fetch_image_levels', fake_fetch_image_levels)
        monkeypatch.setattr(gsv, '_get_response', no_probe)

        with caplog.at_level(logging.WARNING):
            frame = gsv.resolve_frame({'pano_id': self.PANO_ID, 'width': WIDE, 'height': 8192})

        assert frame.evidence == 'photometa' and frame.consistent and (frame.width, frame.height) == (WIDE, 8192)
        assert seen_before_request == [1]
        (record,) = tripwire_records(caplog)
        assert self.PANO_ID in record.getMessage() and 'gsv' in record.getMessage()
        assert len(tripwire_lines(capsys.readouterr().out)) == 1


@pytest.fixture
def mapillary_token(monkeypatch):
    monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, 'test-token')


def mapillary_session(monkeypatch, image_bytes):
    metadata = {'id': MAPILLARY_PANO['pano_id'], 'thumb_original_url': 'https://cdn/x.jpg'}
    monkeypatch.setattr(downloaders.mapillary, '_session',
                        lambda: FakeSession(FakeResponse(payload=metadata), FakeResponse(chunks=[image_bytes])))


class TestMapillaryWiring:
    def stored(self, tmp_path):
        return tmp_path / MAPILLARY_PANO['pano_id'][:2] / ('%s.jpg' % MAPILLARY_PANO['pano_id'])

    def test_a_wide_image_warns_and_is_still_stored_byte_for_byte(self, monkeypatch, tmp_path, mapillary_token,
                                                                 caplog, capsys):
        body = wide_jpeg_bytes(WIDE)
        mapillary_session(monkeypatch, body)

        with caplog.at_level(logging.WARNING):
            result = downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert result == DownloadResult.success
        assert self.stored(tmp_path).read_bytes() == body
        (record,) = tripwire_records(caplog)
        assert MAPILLARY_PANO['pano_id'] in record.getMessage() and 'mapillary' in record.getMessage()
        assert len(tripwire_lines(capsys.readouterr().out)) == 1

    def test_an_image_at_the_ceiling_is_silent(self, monkeypatch, tmp_path, mapillary_token, caplog, capsys):
        mapillary_session(monkeypatch, wide_jpeg_bytes(CEILING))

        with caplog.at_level(logging.WARNING):
            assert downloaders.mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) \
                == DownloadResult.success

        assert tripwire_records(caplog) == []
        assert tripwire_lines(capsys.readouterr().out) == []


class TestPanoramaxWiring:
    def stored(self, tmp_path):
        return tmp_path / PANORAMAX_SHARD / ('%s.jpg' % PANORAMAX_PANO['pano_id'])

    def test_a_wide_image_warns_and_is_still_stored_byte_for_byte(self, monkeypatch, tmp_path, caplog, capsys):
        body = wide_jpeg_bytes(WIDE)
        panoramax_session(monkeypatch, FakeResponse(payload=PANORAMIC_ITEM), FakeResponse(chunks=[body]))

        with caplog.at_level(logging.WARNING):
            result = downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO)

        assert result == DownloadResult.success
        assert self.stored(tmp_path).read_bytes() == body
        (record,) = tripwire_records(caplog)
        assert PANORAMAX_PANO['pano_id'] in record.getMessage() and 'panoramax' in record.getMessage()
        assert len(tripwire_lines(capsys.readouterr().out)) == 1

    def test_an_image_at_the_ceiling_is_silent(self, monkeypatch, tmp_path, caplog, capsys):
        panoramax_session(monkeypatch, FakeResponse(payload=PANORAMIC_ITEM),
                          FakeResponse(chunks=[wide_jpeg_bytes(CEILING)]))

        with caplog.at_level(logging.WARNING):
            assert downloaders.panoramax.download_single_pano(str(tmp_path), PANORAMAX_PANO) \
                == DownloadResult.success

        assert tripwire_records(caplog) == []
        assert tripwire_lines(capsys.readouterr().out) == []


# ---------------------------------------------------------------------------------------------------------
# The alarm fires ONCE (#121, decided on #153 2026-09-26). The tripwire above only warns, and the queue runs
# under `cron_notify --only-on-failure`, so on a night that otherwise exits 0 the warning reaches no one.
# Failing every run that sees a wide frame would reach someone - and then keep the city red every night after,
# since Google does not un-widen, hiding any real failure behind a known one. So the first sighting on a host
# arms a latch on LOCAL disk and fails that run; later runs see the latch and only warn. Deleting the latch
# re-arms it.

from test_download_runner import CSV_HEADER  # noqa: E402

import DownloadRunner  # noqa: E402

# Captured at import, before conftest's autouse fixture points the default at a per-test tmp dir, so the test of
# the deployment default reads the real one.
REAL_DEFAULT_WIDTH_ALARM_LATCH_PATH = common.default_width_alarm_latch_path


def wide_pano_download(width):
    """A download_pano stand-in that does what every real downloader does first: report the frame's width to
    the tripwire. No network and no disk - the alarm is decided in main(), from the tripwire's count."""
    def fake(storage_path, pano_info):
        common.warn_if_wider_than_viewer_ceiling(pano_info['pano_id'], width, pano_info['source'])
        return DownloadResult.success
    return fake


def run_main_over(monkeypatch, tmp_path, width, latch, name='storage'):
    """Drive the whole of DownloadRunner.main() over one GSV pano whose frame is `width` wide."""
    csv_path = tmp_path / (name + '.csv')
    csv_path.write_text(CSV_HEADER + 'wideTestPano%s,%d,%d,47.6,-122.3,180.0,0.0,gsv,True' % (name, width, width // 2)
                        + chr(10))
    monkeypatch.setattr(DownloadRunner, 'download_pano', wide_pano_download(width))
    monkeypatch.chdir(tmp_path)
    return DownloadRunner.main(['sidewalk-test.invalid', str(tmp_path / name), '-c', str(csv_path),
                                '--skip-depth', '--width-alarm-latch', str(latch)])


class TestTheSightingCount:
    def test_a_wide_frame_is_counted(self):
        before = common.ceiling_sightings()
        common.warn_if_wider_than_viewer_ceiling('p', WIDE, 'gsv')
        assert common.ceiling_sightings() == before + 1

    def test_a_frame_at_the_ceiling_is_not(self):
        before = common.ceiling_sightings()
        common.warn_if_wider_than_viewer_ceiling('p', CEILING, 'gsv')
        assert common.ceiling_sightings() == before


class TestTheLatch:
    def test_the_first_arming_reports_itself(self, tmp_path):
        latch = tmp_path / 'latch'
        assert common.arm_width_alarm(str(latch)) is True
        assert latch.exists()

    def test_a_second_arming_does_not(self, tmp_path):
        latch = tmp_path / 'latch'
        common.arm_width_alarm(str(latch))
        assert common.arm_width_alarm(str(latch)) is False

    def test_it_never_overwrites_the_first_sighting(self, tmp_path):
        """The latch's content is when the ceiling was first crossed - the one date an operator needs."""
        latch = tmp_path / 'latch'
        latch.write_text('first sighting')
        common.arm_width_alarm(str(latch))
        assert latch.read_text() == 'first sighting'

    def test_a_latch_that_cannot_be_written_resolves_towards_alarming(self, tmp_path, caplog):
        """Every ambiguity resolves towards telling a human: a latch nobody can write must not swallow the one
        alarm this exists to deliver. The cost is an alarm every night until the path is fixed - noisy, and
        the message names the path."""
        latch = tmp_path / 'no-such-dir' / 'latch'
        assert common.arm_width_alarm(str(latch)) is True
        assert any(str(latch) in r.getMessage() for r in caplog.records)

    def test_the_default_is_local_disk_not_the_store(self):
        """A fact about Google, not about one city: a per-city latch would alarm once per city, 53 times."""
        import tempfile
        assert os.path.dirname(REAL_DEFAULT_WIDTH_ALARM_LATCH_PATH()) == tempfile.gettempdir()


class TestTheAlarmFiresOnce:
    def test_the_first_sighting_fails_the_run(self, monkeypatch, tmp_path, capsys):
        latch = tmp_path / 'latch'
        assert run_main_over(monkeypatch, tmp_path, WIDE, latch) == 1
        assert latch.exists()
        assert 'WIDTH ALARM' in capsys.readouterr().out

    def test_a_later_sighting_only_warns(self, monkeypatch, tmp_path, capsys):
        latch = tmp_path / 'latch'
        run_main_over(monkeypatch, tmp_path, WIDE, latch, name='first')
        capsys.readouterr()
        assert run_main_over(monkeypatch, tmp_path, WIDE, latch, name='second') == 0
        out = capsys.readouterr().out
        # Still said, on both nights - only the exit code changes.
        assert tripwire_lines(out)
        assert str(latch) in out

    def test_deleting_the_latch_re_arms_it(self, monkeypatch, tmp_path):
        latch = tmp_path / 'latch'
        run_main_over(monkeypatch, tmp_path, WIDE, latch, name='first')
        latch.unlink()
        assert run_main_over(monkeypatch, tmp_path, WIDE, latch, name='second') == 1

    def test_an_ordinary_run_neither_fails_nor_arms(self, monkeypatch, tmp_path):
        latch = tmp_path / 'latch'
        assert run_main_over(monkeypatch, tmp_path, CEILING, latch) == 0
        assert not latch.exists()

    def test_only_this_runs_sightings_count(self, monkeypatch, tmp_path):
        """The count is process-wide, so main() must read the DIFFERENCE across its own run. A wide pano seen
        earlier in the process (another test, or refetch_panos sharing an interpreter) is not tonight's."""
        common.warn_if_wider_than_viewer_ceiling('earlier', WIDE, 'gsv')
        latch = tmp_path / 'latch'
        assert run_main_over(monkeypatch, tmp_path, CEILING, latch) == 0
        assert not latch.exists()

    def test_an_unwritable_latch_fails_every_run(self, monkeypatch, tmp_path):
        latch = tmp_path / 'no-such-dir' / 'latch'
        assert run_main_over(monkeypatch, tmp_path, WIDE, latch, name='first') == 1
        assert run_main_over(monkeypatch, tmp_path, WIDE, latch, name='second') == 1
