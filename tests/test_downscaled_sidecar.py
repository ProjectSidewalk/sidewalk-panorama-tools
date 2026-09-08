"""The display copy of a wide panorama (#115): the `<pano_id>.w<cap>.jpg` sidecar the scraper writes beside
a pano wider than the viewer's cap, and the sweep that backfills it across a store.

The cap is patched down to 1024 throughout, so a 2048-wide fixture is "wide" and no test allocates a
16384 x 8192 raster. Every case that reads pixels back uses a two-tone image - red left, blue right - so a
copy that was resized correctly is distinguishable from one that was cropped, or padded, or mirrored.
"""

import io
import logging
import os

import pytest
from PIL import Image

from downloaders import common, gsv, mapillary
from downloaders.common import DownloadResult
import downscale_panos
import refetch_panos
from test_gsv_stitcher import stub_probe, stub_tiles, jpeg_bytes as tile_bytes, RED, BLUE, assert_color
from test_image_downloaders import FakeResponse, FakeSession, HEALTHY_MAPILLARY_METADATA, MAPILLARY_PANO

CAP = 1024


def two_tone(width, height):
    image = Image.new('RGB', (width, height), RED)
    image.paste(BLUE, (width // 2, 0, width, height))
    return image


def two_tone_jpeg(path, width, height):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    two_tone(width, height).save(path, 'jpeg', quality=95)
    return path


def assert_two_tone(path, size):
    with Image.open(path) as image:
        assert image.size == size
        w, h = size
        assert_color(image.getpixel((w // 4, h // 2)), RED)
        assert_color(image.getpixel((3 * w // 4, h // 2)), BLUE)


@pytest.fixture
def small_cap(monkeypatch):
    """The production cap read at call time, so the downloaders see it without threading a parameter."""
    monkeypatch.setattr(common, 'DOWNSCALED_MAX_WIDTH', CAP)


class TestNaming:
    def test_the_sidecar_sits_beside_the_pano_and_carries_the_cap(self):
        assert common.downscaled_sidecar_path('/store/ab/abcdef.jpg') == '/store/ab/abcdef.w8192.jpg'
        assert common.downscaled_sidecar_path('/store/ab/abcdef.jpg', 1024) == '/store/ab/abcdef.w1024.jpg'

    def test_the_default_is_the_production_cap(self):
        assert common.DOWNSCALED_MAX_WIDTH == 8192, 'the web app looks for .w8192.jpg; change both or neither'

    @pytest.mark.parametrize('name, expected', [
        ('abcdef.w8192.jpg', True),
        ('/store/ab/abcdef.w1024.jpg', True),
        ('abcdef.jpg', False),
        ('abcdef.depth.npz', False),
        ('abcdefw8192.jpg', False),   # no dot: some pano id that happens to end in w8192
        ('abcdef.w8192.jpg.part', False),
    ])
    def test_is_downscaled_sidecar_recognises_exactly_the_suffix(self, name, expected):
        assert common.is_downscaled_sidecar(name) is expected

    @pytest.mark.parametrize('dims, expected', [
        ((16384, 8192), (8192, 4096)),   # newer GSV: an exact 2:1
        ((11000, 5500), (8192, 4096)),   # Richmond's Mapillary imagery: not a power of two
        ((11000, 5501), (8192, 4097)),   # rounds, never truncates
        ((8192, 4096), (8192, 4096)),    # at the cap: untouched
        ((3328, 1664), (3328, 1664)),    # under it: never upscaled
    ])
    def test_downscaled_size_keeps_the_aspect_and_never_upscales(self, dims, expected):
        assert common.downscaled_size(dims[0], dims[1], 8192) == expected


class TestWriteFromARaster:
    def test_a_wide_image_gets_an_area_averaged_copy_at_the_cap(self, tmp_path):
        pano = str(tmp_path / 'ab' / 'abcdef.jpg')
        os.makedirs(os.path.dirname(pano))

        written = common.write_downscaled_sidecar(two_tone(2048, 1024), pano, max_width=CAP)

        assert written == str(tmp_path / 'ab' / 'abcdef.w1024.jpg')
        assert_two_tone(written, (CAP, 512))
        assert sorted(os.listdir(tmp_path / 'ab')) == ['abcdef.w1024.jpg'], 'no .part left behind'

    def test_a_narrow_image_gets_nothing(self, tmp_path):
        pano = str(tmp_path / 'ab' / 'abcdef.jpg')
        os.makedirs(os.path.dirname(pano))

        assert common.write_downscaled_sidecar(two_tone(CAP, 512), pano, max_width=CAP) is None
        assert os.listdir(tmp_path / 'ab') == []

    def test_the_copy_is_written_atomically(self, tmp_path, monkeypatch):
        """A crashed write must not leave a file the web app would serve as a texture."""
        pano = str(tmp_path / 'ab' / 'abcdef.jpg')
        os.makedirs(os.path.dirname(pano))

        def full_disk(self, fp, *args, **kwargs):
            with open(fp, 'wb') as f:
                f.write(b'\xff\xd8 truncated')
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(Image.Image, 'save', full_disk)
        with pytest.raises(OSError):
            common.write_downscaled_sidecar(two_tone(2048, 1024), pano, max_width=CAP)
        assert os.listdir(tmp_path / 'ab') == []

    def test_quality_is_the_display_setting_by_default(self, tmp_path):
        """A display copy at 85, not Pillow's 75: it is looked at full-screen, never cut from."""
        pano = str(tmp_path / 'ab' / 'abcdef.jpg')
        os.makedirs(os.path.dirname(pano))
        noise = Image.effect_noise((2048, 1024), 64).convert('RGB')

        default = os.path.getsize(common.write_downscaled_sidecar(noise, pano, max_width=CAP))
        low = os.path.getsize(common.write_downscaled_sidecar(noise, pano, max_width=CAP, quality=50))

        assert common.DOWNSCALED_JPEG_QUALITY == 85
        assert default > low


class TestWriteFromAFile:
    def test_a_wide_pano_on_disk_gets_its_copy(self, tmp_path):
        pano = two_tone_jpeg(str(tmp_path / 'ab' / 'abcdef.jpg'), 2048, 1024)

        written = common.write_downscaled_sidecar_from_file(pano, max_width=CAP)

        assert written == str(tmp_path / 'ab' / 'abcdef.w1024.jpg')
        assert_two_tone(written, (CAP, 512))

    def test_a_ratio_draft_cannot_reach_is_finished_by_resize(self, tmp_path):
        """draft() only decodes at 1/2, 1/4, 1/8; 2048 -> 768 is none of those, so BOX covers the rest."""
        pano = two_tone_jpeg(str(tmp_path / 'ab' / 'abcdef.jpg'), 2048, 1024)

        written = common.write_downscaled_sidecar_from_file(pano, max_width=768)

        assert written.endswith('.w768.jpg')
        assert_two_tone(written, (768, 384))

    def test_a_narrow_pano_gets_nothing(self, tmp_path):
        pano = two_tone_jpeg(str(tmp_path / 'ab' / 'abcdef.jpg'), CAP, 512)
        assert common.write_downscaled_sidecar_from_file(pano, max_width=CAP) is None
        assert os.listdir(tmp_path / 'ab') == ['abcdef.jpg']

    def test_a_file_that_is_not_a_jpeg_raises(self, tmp_path):
        pano = tmp_path / 'ab' / 'abcdef.jpg'
        pano.parent.mkdir()
        pano.write_bytes(b'<html>not an image</html>')
        with pytest.raises(ValueError):
            common.write_downscaled_sidecar_from_file(str(pano), max_width=CAP)


class TestTheGsvDownloaderWritesTheCopy:
    def pano_info(self, width, height, pano_id='wideStitchAAAAAAAAAAAA'):
        return {'pano_id': pano_id, 'width': width, 'height': height}

    def test_a_wide_stitch_lands_with_its_copy(self, tmp_path, monkeypatch, small_cap):
        stub_probe(monkeypatch, pick_zoom=5)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], tile_bytes(RED if tile[0] < 2 else BLUE)))

        assert gsv.download_single_pano(str(tmp_path), self.pano_info(2048, 1024)) == DownloadResult.success

        shard = tmp_path / 'wi'
        assert sorted(os.listdir(shard)) == ['wideStitchAAAAAAAAAAAA.jpg', 'wideStitchAAAAAAAAAAAA.w1024.jpg']
        assert_two_tone(str(shard / 'wideStitchAAAAAAAAAAAA.w1024.jpg'), (CAP, 512))

    def test_a_pano_under_the_cap_lands_alone(self, tmp_path, monkeypatch, small_cap):
        stub_probe(monkeypatch, pick_zoom=5)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], tile_bytes(RED)))

        assert gsv.download_single_pano(str(tmp_path), self.pano_info(CAP, 512)) == DownloadResult.success

        assert os.listdir(tmp_path / 'wi') == ['wideStitchAAAAAAAAAAAA.jpg']

    def test_a_failed_copy_does_not_fail_the_pano(self, tmp_path, monkeypatch, small_cap, caplog):
        """The native file is already the resume marker. Raising here would re-attempt the pano every night,
        skip it at the exists() check, and never write the copy; the sweep is what heals it."""
        stub_probe(monkeypatch, pick_zoom=5)
        stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], tile_bytes(RED)))

        def refuse(image, pano_path, max_width=None, quality=None):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(gsv, 'write_downscaled_sidecar', refuse)
        with caplog.at_level(logging.ERROR):
            result = gsv.download_single_pano(str(tmp_path), self.pano_info(2048, 1024))

        assert result == DownloadResult.success
        assert os.listdir(tmp_path / 'wi') == ['wideStitchAAAAAAAAAAAA.jpg']
        assert 'display copy not written' in caplog.text
        assert 'wideStitchAAAAAAAAAAAA' in caplog.text


class TestTheMapillaryDownloaderWritesTheCopy:
    @pytest.fixture
    def token(self, monkeypatch):
        monkeypatch.setenv(mapillary.TOKEN_ENV_VAR, 'test-token')

    def wide_body(self):
        buf = io.BytesIO()
        two_tone(2048, 1024).save(buf, 'jpeg', quality=95)
        return buf.getvalue()

    def test_a_wide_download_lands_with_its_copy(self, tmp_path, monkeypatch, small_cap, token):
        session = FakeSession(FakeResponse(payload=HEALTHY_MAPILLARY_METADATA),
                              FakeResponse(chunks=[self.wide_body()]))
        monkeypatch.setattr(mapillary, '_session', lambda: session)

        assert mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO) == DownloadResult.success

        pano_id = MAPILLARY_PANO['pano_id']
        shard = tmp_path / pano_id[:2]
        assert sorted(os.listdir(shard)) == ['%s.jpg' % pano_id, '%s.w1024.jpg' % pano_id]
        assert_two_tone(str(shard / ('%s.w1024.jpg' % pano_id)), (CAP, 512))

    def test_a_failed_copy_does_not_fail_the_pano(self, tmp_path, monkeypatch, small_cap, token, caplog):
        session = FakeSession(FakeResponse(payload=HEALTHY_MAPILLARY_METADATA),
                              FakeResponse(chunks=[self.wide_body()]))
        monkeypatch.setattr(mapillary, '_session', lambda: session)

        def refuse(pano_path, max_width=None, quality=None):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(mapillary, 'write_downscaled_sidecar_from_file', refuse)
        with caplog.at_level(logging.ERROR):
            result = mapillary.download_single_pano(str(tmp_path), MAPILLARY_PANO)

        assert result == DownloadResult.success
        assert os.listdir(tmp_path / MAPILLARY_PANO['pano_id'][:2]) == ['%s.jpg' % MAPILLARY_PANO['pano_id']]
        assert 'display copy not written' in caplog.text


class TestTheStoreWalkersIgnoreTheCopy:
    def test_refetch_panos_does_not_take_a_sidecar_for_a_panorama(self, tmp_path):
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.jpg'), 16, 8)
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.w8192.jpg'), 16, 8)

        assert [r['pano_id'] for r in refetch_panos.walk_store(str(tmp_path))] == ['aaBBccDDeeFFggHHiiJJ']

    def test_the_sweep_does_not_either(self, tmp_path):
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.jpg'), 16, 8)
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.w8192.jpg'), 16, 8)
        (tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.jpg.part').write_bytes(b'\xff\xd8')
        (tmp_path / 'aa' / 'zzWrongShardXXXXXXXX.jpg').write_bytes(b'\xff\xd8')
        (tmp_path / 'stray.jpg').write_bytes(b'\xff\xd8')
        (tmp_path / 'log.csv').write_text('a,b\n')

        assert list(downscale_panos.find_panos(str(tmp_path))) == \
            [str(tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.jpg')]


class TestTheSweep:
    """One store that reaches every branch: a wide pano with no copy, one whose copy is already at the cap,
    one whose copy is at another width, a narrow pano, and a file that is not a JPEG."""

    def seed(self, tmp_path):
        store = str(tmp_path)
        two_tone_jpeg(os.path.join(store, 'aa', 'aaMissingCopyAAAAAAA.jpg'), 2048, 1024)
        two_tone_jpeg(os.path.join(store, 'bb', 'bbCurrentCopyAAAAAAA.jpg'), 2048, 1024)
        two_tone_jpeg(os.path.join(store, 'bb', 'bbCurrentCopyAAAAAAA.w1024.jpg'), CAP, 512)
        two_tone_jpeg(os.path.join(store, 'cc', 'ccStaleCopyAAAAAAAAA.jpg'), 2048, 1024)
        two_tone_jpeg(os.path.join(store, 'cc', 'ccStaleCopyAAAAAAAAA.w1024.jpg'), 512, 256)
        two_tone_jpeg(os.path.join(store, 'dd', 'ddNarrowAAAAAAAAAAAA.jpg'), CAP, 512)
        os.makedirs(os.path.join(store, 'ee'))
        with open(os.path.join(store, 'ee', 'eeNotAJpegAAAAAAAAAA.jpg'), 'wb') as f:
            f.write(b'<html>not an image</html>')
        return store

    def test_it_writes_exactly_the_missing_and_stale_copies(self, tmp_path, capsys):
        store = self.seed(tmp_path)

        summary = downscale_panos.downscale_store(store, max_width=CAP)

        assert summary == downscale_panos.Summary(scanned=5, written=2, narrow=1, current=1, failed=1,
                                                  unreached=0)
        assert_two_tone(os.path.join(store, 'aa', 'aaMissingCopyAAAAAAA.w1024.jpg'), (CAP, 512))
        assert_two_tone(os.path.join(store, 'cc', 'ccStaleCopyAAAAAAAAA.w1024.jpg'), (CAP, 512))
        # The current copy is left byte-for-byte alone: nothing decoded it.
        with Image.open(os.path.join(store, 'bb', 'bbCurrentCopyAAAAAAA.w1024.jpg')) as image:
            assert image.size == (CAP, 512)
        assert not os.path.exists(os.path.join(store, 'dd', 'ddNarrowAAAAAAAAAAAA.w1024.jpg'))
        out = capsys.readouterr().out
        assert 'Wrote %s' % os.path.join(store, 'aa', 'aaMissingCopyAAAAAAA.w1024.jpg') in out
        assert 'FAILED %s' % os.path.join(store, 'ee', 'eeNotAJpegAAAAAAAAAA.jpg') in out

    def test_a_second_run_changes_nothing(self, tmp_path):
        store = self.seed(tmp_path)
        downscale_panos.downscale_store(store, max_width=CAP)
        before = {p: os.path.getmtime(os.path.join(store, p)) for p in
                  ('aa/aaMissingCopyAAAAAAA.w1024.jpg', 'cc/ccStaleCopyAAAAAAAAA.w1024.jpg')}

        summary = downscale_panos.downscale_store(store, max_width=CAP)

        assert (summary.written, summary.current) == (0, 3)
        assert {p: os.path.getmtime(os.path.join(store, p)) for p in before} == before

    def test_a_dry_run_counts_without_decoding_or_writing(self, tmp_path, monkeypatch, capsys):
        store = self.seed(tmp_path)
        monkeypatch.setattr(Image, 'open', lambda *a, **k: pytest.fail('a dry run must not decode'))

        summary = downscale_panos.downscale_store(store, dry_run=True, max_width=CAP)

        assert (summary.written, summary.failed) == (2, 1)
        assert not os.path.exists(os.path.join(store, 'aa', 'aaMissingCopyAAAAAAA.w1024.jpg'))
        assert 'Would write' in capsys.readouterr().out

    def test_a_write_that_fails_is_counted_and_the_sweep_goes_on(self, tmp_path, monkeypatch, capsys):
        store = self.seed(tmp_path)

        def refuse(pano_path, max_width=None, quality=None):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(downscale_panos, 'write_downscaled_sidecar_from_file', refuse)
        summary = downscale_panos.downscale_store(store, max_width=CAP)

        assert (summary.written, summary.failed) == (0, 3)
        assert 'No space left on device' in capsys.readouterr().out

    def test_the_runtime_budget_leaves_the_rest_unreached(self, tmp_path, monkeypatch):
        store = self.seed(tmp_path)
        # Start, then one reading per pano: two panos inside the minute, everything after it past.
        readings = [0.0, 0.0, 0.0]
        monkeypatch.setattr(downscale_panos.time, 'monotonic', lambda: readings.pop(0) if readings else 61.0)

        summary = downscale_panos.downscale_store(store, max_width=CAP, max_runtime_minutes=1)

        # Two panos were examined before the clock passed the minute; the other three wait for the next run.
        assert summary.scanned + summary.unreached == 5
        assert summary.unreached == 3

    def test_main_reports_the_summary_and_exits_nonzero_on_a_failure(self, tmp_path, monkeypatch, capsys):
        store = self.seed(tmp_path)
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 89478485)

        assert downscale_panos.main([store, '--max-width', str(CAP)]) == 1

        out = capsys.readouterr().out
        assert 'Examined 5 panorama(s): 2 written, 1 under the cap, 1 already had a copy, 1 failed, 0 unreached.' in out
        # The process-level policy CropRunner.main sets too: this script opens 134 MP files on purpose.
        assert Image.MAX_IMAGE_PIXELS is None

    def test_main_exits_zero_on_a_clean_store(self, tmp_path, monkeypatch, capsys):
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaMissingCopyAAAAAAA.jpg'), 2048, 1024)
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 89478485)

        assert downscale_panos.main([str(tmp_path), '--max-width', str(CAP), '--dry-run']) == 0

        assert '1 would be written' in capsys.readouterr().out
        assert os.listdir(tmp_path / 'aa') == ['aaMissingCopyAAAAAAA.jpg']
