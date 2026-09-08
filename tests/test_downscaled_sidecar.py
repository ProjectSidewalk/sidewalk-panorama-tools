"""The display copy of a wide panorama (#115): the `<pano_id>.w<cap>.jpg` sidecar the scraper writes beside
a pano wider than the viewer's cap, and the sweep that backfills it across a store.

The cap is patched down to 1024 throughout, so a 2048-wide fixture is "wide" and no test allocates a
16384 x 8192 raster. Every case that reads pixels back uses a two-tone image - red left, blue right - so a
copy that was resized correctly is distinguishable from one that was cropped, or padded, or mirrored.
"""

import io
import logging
import os
import re

import pytest
from PIL import Image

from downloaders import common, gsv, mapillary
from downloaders.common import DownloadResult
import downscale_panos
import refetch_panos
from test_gsv_stitcher import stub_probe, stub_tiles, jpeg_bytes as tile_bytes, RED, BLUE, assert_color
from test_image_downloaders import FakeResponse, FakeSession, HEALTHY_MAPILLARY_METADATA, MAPILLARY_PANO
# The one list of "the production tree", shared with the no-pandas rule this guard is a sibling of.
from test_csv_intake import PRODUCTION_MODULES

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


class TestTheOneStoreWalker:
    """`common.walk_store_panos` is the single definition of "this file is a panorama".

    It was written twice - `refetch_panos.walk_store` and the backfill's own sweep - and the clause a third
    copy forgets is the sidecar exclusion, whose failure mode is not an error but an invented pano id.
    """

    def cluttered_store(self, tmp_path):
        """Everything a real shard holds beside the panorama, and two things that are not in a shard at all."""
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.jpg'), 16, 8)
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.w8192.jpg'), 16, 8)
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.w1024.jpg'), 16, 8)
        (tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.depth.npz').write_bytes(b'PK')
        (tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.jpg.part').write_bytes(b'\xff\xd8')
        (tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.w8192.jpg.part').write_bytes(b'\xff\xd8')
        (tmp_path / 'aa' / 'zzWrongShardXXXXXXXX.jpg').write_bytes(b'\xff\xd8')
        two_tone_jpeg(str(tmp_path / 'bb' / 'bbSecondPanoAAAAAAAA.jpg'), 16, 8)
        (tmp_path / 'stray.jpg').write_bytes(b'\xff\xd8')
        (tmp_path / 'log.csv').write_text('a,b\n')
        (tmp_path / 'crops').mkdir()
        (tmp_path / 'crops' / '12345.jpg').write_bytes(b'\xff\xd8')
        return str(tmp_path)

    def test_it_yields_the_panoramas_and_nothing_else(self, tmp_path):
        store = self.cluttered_store(tmp_path)

        assert list(common.walk_store_panos(store)) == [
            os.path.join(store, 'aa', 'aaBBccDDeeFFggHHiiJJ.jpg'),
            os.path.join(store, 'bb', 'bbSecondPanoAAAAAAAA.jpg'),
        ]

    def test_a_sidecar_at_any_cap_is_never_a_panorama(self, tmp_path):
        """The clause with no error to announce it: `<id>.w8192.jpg` shares the panorama's first two
        characters and its .jpg suffix, so a walker that drops it hands on the pano id `<real id>.w8192`."""
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaOnlySidecarsAAAAAA.w8192.jpg'), 16, 8)
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaOnlySidecarsAAAAAA.w4096.jpg'), 16, 8)

        assert list(common.walk_store_panos(str(tmp_path))) == []

    def test_the_order_is_stable_across_calls_and_shards(self, tmp_path):
        """Both callers rely on it: the sweep's --max-runtime resumes where the last run stopped, and the
        repair pass shuffles a list it needs to have drawn identically each time."""
        for shard, pano in (('cc', 'ccThirdAAAAAAAAAAAAA'), ('aa', 'aaFirstAAAAAAAAAAAAA'),
                            ('bb', 'bbSecondAAAAAAAAAAAA')):
            two_tone_jpeg(str(tmp_path / shard / (pano + '.jpg')), 16, 8)

        first = list(common.walk_store_panos(str(tmp_path)))

        assert [os.path.basename(p) for p in first] == \
            ['aaFirstAAAAAAAAAAAAA.jpg', 'bbSecondAAAAAAAAAAAA.jpg', 'ccThirdAAAAAAAAAAAAA.jpg']
        assert list(common.walk_store_panos(str(tmp_path))) == first

    def test_refetch_panos_reads_its_ids_through_it(self, tmp_path):
        store = self.cluttered_store(tmp_path)

        assert [r['pano_id'] for r in refetch_panos.walk_store(store)] == \
            ['aaBBccDDeeFFggHHiiJJ', 'bbSecondPanoAAAAAAAA']

    def test_the_sweep_examines_exactly_those_panoramas(self, tmp_path):
        """Through downscale_store rather than a walker of its own: find_panos was the second copy."""
        store = self.cluttered_store(tmp_path)

        summary = downscale_panos.downscale_store(store, dry_run=True, max_width=CAP)

        assert summary.scanned == 2, 'the sweep must examine the two panoramas and neither sidecar'
        assert (summary.narrow, summary.failed) == (2, 0)

    def test_no_production_module_re_derives_the_predicate(self):
        """The guard the two tests above cannot give: they pin the walkers that exist today.

        A hand-rolled `filename.endswith('.jpg')` over a shard listing is the shape that forgets the sidecar
        clause, so exactly one file in the production tree may contain it - the one defining
        walk_store_panos. Same standing rule as the no-pandas assertion in test_csv_intake.py.
        """
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = re.compile(r"""endswith\(\s*['"]\.jpg['"]""")
        offenders = [m for m in PRODUCTION_MODULES
                     if pattern.search(open(os.path.join(repo_root, m), encoding='utf-8').read())]

        assert offenders == ['downloaders/common.py'], (
            'a second store walker: call downloaders.common.walk_store_panos() instead, or it will take a '
            '<id>.w8192.jpg sidecar for a panorama named <id>.w8192 -- silently. Offenders: %s' % offenders)


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
        # The process-level policy every entry point that opens a stored panorama sets; see
        # TestTheDecompressionBombCeiling for why it is a ceiling and not None.
        assert Image.MAX_IMAGE_PIXELS == common.MAX_PANO_PIXELS

    def test_main_exits_zero_on_a_clean_store(self, tmp_path, monkeypatch, capsys):
        two_tone_jpeg(str(tmp_path / 'aa' / 'aaMissingCopyAAAAAAA.jpg'), 2048, 1024)
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 89478485)

        assert downscale_panos.main([str(tmp_path), '--max-width', str(CAP), '--dry-run']) == 0

        assert '1 would be written' in capsys.readouterr().out
        assert os.listdir(tmp_path / 'aa') == ['aaMissingCopyAAAAAAA.jpg']


class TestSidecarIsCurrent:
    """The sweep's whole affordability rests on this answering from two headers. It must therefore be exact
    about what it can and cannot see, because a false 'current' is permanent: the copy is never revisited."""

    def pano(self, tmp_path, width=2048, height=1024):
        return two_tone_jpeg(str(tmp_path / 'aa' / 'aaPanoAAAAAAAAAAAAAA.jpg'), width, height)

    def sidecar(self, pano, width, height, max_width=CAP):
        return two_tone_jpeg(common.downscaled_sidecar_path(pano, max_width), width, height)

    def test_the_copy_the_rule_would_write_is_current(self, tmp_path):
        pano = self.pano(tmp_path)
        common.write_downscaled_sidecar_from_file(pano, max_width=CAP)

        assert downscale_panos.sidecar_is_current(pano, (2048, 1024), CAP) is True

    def test_a_missing_copy_is_not(self, tmp_path):
        pano = self.pano(tmp_path)

        assert downscale_panos.sidecar_is_current(pano, (2048, 1024), CAP) is False

    def test_a_copy_at_the_cap_with_the_WRONG_HEIGHT_is_not_current(self, tmp_path):
        """The case a width-only check called current, for ever.

        The name already promises the width, so checking only the width proves the file is not truncated and
        says nothing about which panorama the copy belongs to. A copy written under an older aspect rule, or
        beside a panorama that has since been re-framed, keeps the cap width and is silently wrong.
        """
        pano = self.pano(tmp_path)
        self.sidecar(pano, CAP, 999)

        assert downscale_panos.sidecar_is_current(pano, (2048, 1024), CAP) is False

    def test_a_copy_at_another_cap_entirely_is_not_current(self, tmp_path):
        pano = self.pano(tmp_path)
        self.sidecar(pano, 512, 256)

        assert downscale_panos.sidecar_is_current(pano, (2048, 1024), CAP) is False

    def test_an_unreadable_copy_is_rewritten_rather_than_trusted(self, tmp_path):
        pano = self.pano(tmp_path)
        with open(common.downscaled_sidecar_path(pano, CAP), 'wb') as f:
            f.write(b'\xff\xd8 truncated before any SOF')

        assert downscale_panos.sidecar_is_current(pano, (2048, 1024), CAP) is False

    def test_it_reads_two_headers_and_decodes_nothing(self, tmp_path, monkeypatch):
        pano = self.pano(tmp_path)
        common.write_downscaled_sidecar_from_file(pano, max_width=CAP)
        monkeypatch.setattr(Image, 'open', lambda *a, **k: pytest.fail('the check must not decode'))

        assert downscale_panos.sidecar_is_current(pano, (2048, 1024), CAP) is True

    def test_it_cannot_see_a_copy_that_is_stale_only_in_CONTENT(self, tmp_path):
        """Named so nobody mistakes this check for the guarantee. The panorama's bytes can be replaced under
        an unchanged frame - refetch_panos does exactly that - and no header can tell. The copy is kept
        honest at the moment of the swap (refetch_panos._refresh_display_copy), not here.
        """
        pano = self.pano(tmp_path)
        common.write_downscaled_sidecar_from_file(pano, max_width=CAP)
        Image.new('RGB', (2048, 1024), BLUE).save(pano, 'jpeg')   # same frame, different imagery

        assert downscale_panos.sidecar_is_current(pano, (2048, 1024), CAP) is True
        with Image.open(common.downscaled_sidecar_path(pano, CAP)) as copy:
            assert_color(copy.getpixel((CAP // 4, 256)), RED)     # still the old imagery


class TestTheDecompressionBombCeiling:
    """One definition of the policy, because the list of entry points that open a 134 MP panorama stopped
    being one script's concern when #115 put an Image.open on the Mapillary download path."""

    def test_it_raises_pillows_default_to_our_largest_panorama(self, monkeypatch):
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 89478485)

        common.raise_decompression_bomb_ceiling()

        assert Image.MAX_IMAGE_PIXELS == common.MAX_PANO_PIXELS == 16384 * 8192

    def test_it_is_a_ceiling_and_not_None(self):
        """None would also swallow a genuinely corrupt header claiming absurd dimensions, and the sweep is
        the one caller that opens whatever the store happens to hold."""
        assert common.MAX_PANO_PIXELS is not None

    def test_it_never_lowers_a_ceiling_somebody_else_raised(self, monkeypatch):
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 10 ** 12)
        common.raise_decompression_bomb_ceiling()
        assert Image.MAX_IMAGE_PIXELS == 10 ** 12

        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', None)
        common.raise_decompression_bomb_ceiling()
        assert Image.MAX_IMAGE_PIXELS is None, 'an explicit "no limit" is a caller decision, not ours'

    def test_croprunner_still_exports_the_name_its_callers_reach_for(self):
        """reports/scripts/annotation_tiles.py and crop_sizing_v2.py call CropRunner.raise_...()."""
        import CropRunner

        assert CropRunner.raise_decompression_bomb_ceiling is common.raise_decompression_bomb_ceiling

    def test_the_mapillary_display_copy_can_open_a_pano_over_pillows_default(self, tmp_path, monkeypatch):
        """The reason DownloadRunner needs the policy at all: this path re-opens the file it just wrote.

        Pillow only hard-fails above 2x its threshold, so this drives the ceiling down far enough that a
        modest test image trips the error rather than allocating a real 134 MP one.
        """
        pano = two_tone_jpeg(str(tmp_path / 'aa' / 'aaPanoAAAAAAAAAAAAAA.jpg'), 2048, 1024)
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 1000)
        with pytest.raises(Image.DecompressionBombError):
            common.write_downscaled_sidecar_from_file(pano, max_width=CAP)

        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 89478485)
        common.raise_decompression_bomb_ceiling()

        assert common.write_downscaled_sidecar_from_file(pano, max_width=CAP) is not None
