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
import io
import logging
import os

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
        next to another line may arrive alone."""
        with caplog.at_level(logging.WARNING):
            common.warn_if_wider_than_viewer_ceiling('panoIdX', 20480, 'panoramax')

        if channel == 'log':
            (text,) = [r.getMessage() for r in tripwire_records(caplog)]
        else:
            (text,) = tripwire_lines(capsys.readouterr().out)
        for fragment in ('panoIdX', '20480', '16384', 'panoramax', 'WRITE_DISPLAY_COPIES', 'downscale_panos.py',
                         'docs/ops.md', '#121'):
            assert fragment in text, fragment

    def test_it_writes_nothing_to_the_store(self, tmp_path, caplog, capsys):
        """An observation, not a writer: no display copy, no file of any kind. The switch-reading rule in
        tests/test_downscaled_sidecar.py::TestTheSwitch is about callers of the sidecar primitives, and this
        helper must never become one."""
        common.warn_if_wider_than_viewer_ceiling('panoIdX', WIDE, 'gsv')

        assert list(tmp_path.iterdir()) == []


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
