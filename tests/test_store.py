"""sidewalk_panorama_tools.store: the pano store's layout, once (#222 step 5, docs/geometry.md).

The walker, the sidecar rule and the header reader moved here from downloaders/common.py, which re-exports
them; tests/test_downscaled_sidecar.py keeps exercising them through those names, and the identity tests
below are what make both suites test one definition.
"""

import os
import shutil
import warnings

import pytest
from PIL import Image, ImageFile

from downloaders import common
from sidewalk_panorama_tools import store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_PANO = os.path.join(REPO_ROOT, 'samples', 'sample_pano.jpg')

GSV_ID = '-0BIbaGbb6Ao2ctRBhyX9w'
MAPILLARY_ID = '1234567890123456'
PANORAMAX_ID = '0a1b2c3d-0000-4000-8000-00000000abcd'


class TestPaths:

    @pytest.mark.parametrize('pano_id, expected', [(GSV_ID, '-0'), (MAPILLARY_ID, '12'), (PANORAMAX_ID, '0a')],
                             ids=['gsv-base64url', 'mapillary-digits', 'panoramax-uuid'])
    def test_the_shard_is_the_first_two_characters(self, pano_id, expected):
        assert store.shard(pano_id) == expected

    def test_a_numeric_id_is_sharded_as_text(self):
        assert store.shard(1234567890123456) == '12'

    @pytest.mark.parametrize('pano_id', ['', 'x'])
    def test_an_id_too_short_to_shard_is_refused(self, pano_id):
        with pytest.raises(ValueError):
            store.shard(pano_id)

    def test_paths_without_a_city_are_under_the_given_directory(self):
        assert store.pano_path('/s/seattle-wa', GSV_ID) == os.path.join('/s/seattle-wa', '-0', GSV_ID + '.jpg')
        assert store.depth_path('/s/seattle-wa', GSV_ID) == os.path.join('/s/seattle-wa', '-0',
                                                                        GSV_ID + '.depth.npz')

    def test_paths_with_a_city_add_its_directory(self):
        assert store.pano_path('/s', PANORAMAX_ID, city='bayonne-fr') == os.path.join(
            '/s', 'bayonne-fr', '0a', PANORAMAX_ID + '.jpg')
        assert store.depth_path('/s', GSV_ID, city='seattle-wa') == os.path.join(
            '/s', 'seattle-wa', '-0', GSV_ID + '.depth.npz')

    def test_the_sidecar_carries_its_width_and_is_recognised(self):
        pano = store.pano_path('/s', GSV_ID, city='seattle-wa')
        copy = store.sidecar_path(pano, 8192)
        assert copy == os.path.join('/s', 'seattle-wa', '-0', GSV_ID + '.w8192.jpg')
        assert store.is_downscaled_sidecar(copy) and not store.is_downscaled_sidecar(pano)

    def test_commons_sidecar_path_is_the_package_one_at_the_repos_cap(self, monkeypatch):
        assert common.downscaled_sidecar_path('/a/ab/abc.jpg') == store.sidecar_path('/a/ab/abc.jpg', 8192)
        monkeypatch.setattr(common, 'DOWNSCALED_MAX_WIDTH', 1024)
        assert common.downscaled_sidecar_path('/a/ab/abc.jpg') == '/a/ab/abc.w1024.jpg'


class TestOneDefinition:

    @pytest.mark.parametrize('name', ['walk_store_panos', 'is_downscaled_sidecar', 'jpeg_dimensions',
                                      'raise_decompression_bomb_ceiling', 'MAX_PANO_PIXELS'])
    def test_common_re_exports_the_package_object(self, name):
        assert getattr(common, name) is getattr(store, name)


class TestTheWalker:

    def test_a_shard_holding_a_pano_its_copy_its_depth_and_a_stray_yields_one_pano(self, tmp_path):
        shard = tmp_path / 'ab'
        shard.mkdir()
        for name in ('abPano.jpg', 'abPano.w8192.jpg', 'abPano.depth.npz', 'abPano.jpg.part'):
            (shard / name).write_bytes(b'x')
        (tmp_path / 'cd').mkdir()
        (tmp_path / 'cd' / 'abWrongShard.jpg').write_bytes(b'x')
        (tmp_path / 'stray.jpg').write_bytes(b'x')

        assert list(store.walk_store_panos(str(tmp_path))) == [str(shard / 'abPano.jpg')]


class TestJpegDimensions:

    def test_the_sample_pano(self):
        with Image.open(SAMPLE_PANO) as image:
            expected = image.size
        assert store.jpeg_dimensions(SAMPLE_PANO) == expected

    def test_a_file_that_is_not_a_jpeg_is_none(self, tmp_path):
        path = tmp_path / 'x.jpg'
        path.write_bytes(b'not a jpeg')
        assert store.jpeg_dimensions(str(path)) is None
        assert store.jpeg_dimensions(str(tmp_path / 'absent.jpg')) is None


def small_pano(path, size=(64, 32)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', size, (10, 20, 30)).save(path, quality=90)
    return path


class TestReadPano:

    def test_matching_dims_decode(self, tmp_path):
        path = small_pano(str(tmp_path / 'ab' / 'abPano.jpg'))
        image = store.read_pano(path, expected_width=64, expected_height=32)
        try:
            assert image.size == (64, 32) and image.getpixel((0, 0))[2] > 20
        finally:
            image.close()

    def test_no_expectation_decodes_without_a_check(self, tmp_path):
        image = store.read_pano(small_pano(str(tmp_path / 'p.jpg')))
        image.close()

    @pytest.mark.parametrize('width, height', [('64', '32'), ('64.0', '32.0'), (64.0, 32), (' 64 ', '32.0')])
    def test_dims_from_a_csv_read_as_croprunner_reads_them(self, tmp_path, width, height):
        """CropRunner._metadata_dims reads float() then int(), so '64.0' from a pandas-written CSV is 64 (#229
        review: int('64.0') raised a bare ValueError that `except PanoDimsMismatch` missed)."""
        store.read_pano(small_pano(str(tmp_path / 'p.jpg')), expected_width=width, expected_height=height).close()
        with pytest.raises(store.PanoDimsMismatch):
            store.read_pano(str(tmp_path / 'p.jpg'), expected_width='13312.0', expected_height='6656.0')

    @pytest.mark.parametrize('bad', ['x', '', None, 'nan', 0, -64, float('inf')])
    def test_an_expected_dimension_that_is_not_a_frame_is_a_bad_argument(self, tmp_path, bad):
        with pytest.raises(ValueError) as caught:
            store.read_pano(small_pano(str(tmp_path / 'p.jpg')), expected_width=bad, expected_height=32)
        assert not isinstance(caught.value, store.PanoDimsMismatch)

    def test_a_mismatch_is_refused_without_touching_pillow(self, tmp_path, monkeypatch):
        """#229 review: the refusal is jpeg_dimensions, stdlib only - no Image.open, no decode, no warnings
        filter (catch_warnings rewrites process-wide state and is not thread-safe)."""
        path = small_pano(str(tmp_path / 'p.jpg'))

        def no_pillow(*args, **kwargs):
            raise AssertionError('the refusal path must not open the image with Pillow')
        monkeypatch.setattr(Image, 'open', no_pillow)
        filters_before = list(warnings.filters)

        with pytest.raises(store.PanoDimsMismatch) as caught:
            store.read_pano(path, expected_width=13312, expected_height=6656)

        assert caught.value.expected == (13312, 6656) and caught.value.stored == (64, 32)
        assert caught.value.path == path and '13312x6656' in str(caught.value)
        assert isinstance(caught.value, ValueError)
        assert list(warnings.filters) == filters_before
        assert not hasattr(store, 'warnings'), 'store must not use the warnings module at all'

    def test_a_missing_file_and_an_unreadable_header(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            store.read_pano(str(tmp_path / 'absent.jpg'))
        bad = tmp_path / 'bad.jpg'
        bad.write_bytes(b'not a jpeg at all')
        with pytest.raises(OSError):
            store.read_pano(str(bad), 64, 32)

    def test_the_counting_load_does_see_a_real_decode(self, tmp_path, monkeypatch):
        """Guards the guard above: the patched load is the one read_pano's decode goes through."""
        loads = []
        real_load = ImageFile.ImageFile.load

        def counting_load(self):
            loads.append(self)
            return real_load(self)
        monkeypatch.setattr(ImageFile.ImageFile, 'load', counting_load)

        store.read_pano(small_pano(str(tmp_path / 'p.jpg')), 64, 32).close()
        assert loads

    def test_half_an_expectation_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match='both'):
            store.read_pano(small_pano(str(tmp_path / 'p.jpg')), expected_width=64)

    def test_a_read_leaves_the_pixel_ceiling_to_the_process(self, tmp_path, monkeypatch):
        """read_pano never rewrites Image.MAX_IMAGE_PIXELS (process policy, raise_decompression_bomb_ceiling)."""
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 10 ** 9)
        store.read_pano(small_pano(str(tmp_path / 'p.jpg')), 64, 32).close()
        assert Image.MAX_IMAGE_PIXELS == 10 ** 9

    def test_a_header_over_the_ceiling_is_refused_before_decoding(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, 'MAX_PANO_PIXELS', 64 * 32 - 1)
        with pytest.raises(Image.DecompressionBombError):
            store.read_pano(small_pano(str(tmp_path / 'p.jpg')))

    def test_a_truncated_body_raises_and_is_not_returned(self, tmp_path):
        path = small_pano(str(tmp_path / 'p.jpg'), size=(256, 128))
        with open(path, 'rb') as f:
            data = f.read()
        with open(path, 'wb') as f:
            f.write(data[:len(data) // 2])
        with pytest.raises(OSError):
            store.read_pano(path, 256, 128)

    def test_the_sample_pano_reads_at_its_own_dims(self, tmp_path):
        path = str(tmp_path / 'sample.jpg')
        shutil.copyfile(SAMPLE_PANO, path)
        width, height = store.jpeg_dimensions(path)
        image = store.read_pano(path, width, height)
        try:
            assert image.size == (width, height)
        finally:
            image.close()
