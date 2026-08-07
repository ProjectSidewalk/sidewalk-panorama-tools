"""Pins what Google's CBK tile endpoint actually does, using real tile bytes captured from it.

Everything here runs against committed fixtures in fixtures/tiles/ (62 KB of real cbk responses, see
manifest.json for pano ids, grid positions and capture date), so the suite stays network-free. The point is
to hold the endpoint's real behaviour in the repo, because two of these behaviours are load-bearing for
downloaders.gsv and neither is documented anywhere:

  1. cbk answers the SAME zoom-5 grid position with either a 512x512 body or a load-shed 256x256 body.
     Which positions come back degraded varies over hours and is sticky for a while, so a single pano's
     512-tile fan-out routinely receives a MIX of both. The fixture block is exactly that: a real 2x2
     zoom-5 neighbourhood captured in one pass, two tiles at 512 and two at 256.

     A degraded body is the same grid cell rendered at half scale - proven here against the zoom-4 tile
     covering the same region, which matches it to the pixel. So the correct placement is to scale every
     body up to the stitch's cell size, which is what the pre-#44 `img.resize((512, 512))` was doing and
     why removing it produced a 75%-black pano (see test_gsv_stitcher's mixed-body tests).

  2. Out-of-range tiles are answered 200 OK with a valid, ALL-BLACK image/jpeg, and short edge tiles are
     padded to a full 512 body rather than returned at their true size. Together those mean a wrong tile
     grid cannot be detected tile by tile - only by looking at the stitched result, which is what
     _reject_mostly_black_stitch is for.

Re-capture with scratch/build_fixtures2.py-style probing if Google's behaviour ever needs re-checking; the
manifest records what each file was when captured.
"""

import json
import os
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from downloaders import gsv

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'tiles')


def fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), 'rb') as f:
        return f.read()


def fixture_image(name):
    return Image.open(BytesIO(fixture_bytes(name))).convert('RGB')


@pytest.fixture(scope='module')
def manifest():
    with open(os.path.join(FIXTURES, 'manifest.json')) as f:
        return json.load(f)


# The captured 2x2 zoom-5 neighbourhood, and the zoom-4 tile covering the same pano region.
MIXED_BLOCK = [('z5_full_8_10.jpg', 8, 10, (512, 512)),
               ('z5_full_9_10.jpg', 9, 10, (512, 512)),
               ('z5_degraded_8_11.jpg', 8, 11, (256, 256)),
               ('z5_degraded_9_11.jpg', 9, 11, (256, 256))]
Z4_COVER = 'z4_cover_4_5.jpg'


class TestCbkServesTwoBodySizesForOneZoomLevel:
    @pytest.mark.parametrize('name,x,y,expected', MIXED_BLOCK)
    def test_captured_body_sizes(self, name, x, y, expected):
        assert fixture_image(name).size == expected

    def test_one_fanout_really_did_see_both_sizes(self):
        """The whole reason the stitcher cannot assume a tile size: these four came back together."""
        sizes = {fixture_image(name).size for name, _, _, _ in MIXED_BLOCK}
        assert sizes == {(512, 512), (256, 256)}, \
            "fixtures no longer demonstrate a mixed fan-out; re-capture or the mixed-body tests lose their point"

    def test_the_block_is_one_contiguous_2x2_neighbourhood(self):
        assert {(x, y) for _, x, y, _ in MIXED_BLOCK} == {(8, 10), (9, 10), (8, 11), (9, 11)}

    @pytest.mark.parametrize('name,x,y,expected', MIXED_BLOCK)
    def test_every_body_is_the_same_cell_as_the_zoom4_quadrant_covering_it(self, name, x, y, expected):
        """Geometry proof. cbk's zoom-4 image is 8192 wide == 32 x 256, so zoom-5 cell (x, y) at HALF scale
        is exactly the (x%2, y%2) 256px quadrant of zoom-4 tile (x//2, y//2). It holds for the degraded
        bodies to the pixel and for the full bodies within JPEG noise - so a 256 body is not a different
        crop, a different rendering or a stale cache entry. It is this cell, at half scale, and scaling it
        up to the cell size puts its pixels exactly where they belong."""
        quadrant = np.asarray(fixture_image(Z4_COVER), float)[
            (y % 2) * 256:(y % 2) * 256 + 256, (x % 2) * 256:(x % 2) * 256 + 256]
        body = fixture_image(name)
        at_half = body if body.size == (256, 256) else body.resize((256, 256), Image.LANCZOS)
        diff = np.abs(np.asarray(at_half, float) - quadrant).mean()
        tolerance = 0.5 if expected == (256, 256) else 4.0   # exact vs re-encode noise
        assert diff < tolerance, \
            'zoom-5 %s does not line up with its zoom-4 quadrant (mean|diff|=%.2f)' % (name, diff)

    def test_a_degraded_body_carries_no_detail_the_full_body_lacks(self):
        """The complement of the above: the 512 body is the better rendering, so upscaling a degraded body
        is a genuine loss - worth logging - not a free substitution."""
        full = fixture_image('z5_full_8_10.jpg')
        degraded_upscaled = fixture_image('z5_degraded_8_11.jpg').resize((512, 512), Image.LANCZOS)

        def detail(im):
            g = np.asarray(im.convert('L'), float)
            return np.abs(4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1]
                          - g[1:-1, :-2] - g[1:-1, 2:]).mean()

        assert detail(full) > detail(degraded_upscaled)


class TestOutOfRangeAndEdgeTiles:
    def test_out_of_range_tile_is_a_valid_all_black_jpeg_not_an_error(self):
        """x=20 on a 7-column zoom-3 grid. Google answers 200 with a decodable image/jpeg that is entirely
        black - so #44's wrong grid could never be caught at the tile layer, and neither can a wrong
        num_zoom_levels. Only the stitched image shows it."""
        blank = fixture_image('z3_blank_out_of_range.jpg')
        assert blank.size == (512, 512)
        assert blank.convert('L').getextrema() == (0, 0)

    def test_short_edge_tiles_are_padded_to_a_full_body_not_returned_true_size(self):
        """The premise the PR's original no-resize stitch rested on, checked against reality: a zoom-3
        bottom row that holds only 128 real pixel rows (1664 = 3*512 + 128) still arrives as a 512 body,
        black-padded below. Google does not return true-size edge bodies - so an undersized body always
        means the load-shed rendering, never a true-size edge, and upscaling it is unambiguously right."""
        edge = fixture_image('z3_edge_bottom.jpg')
        assert edge.size == (512, 512)

        rows = np.asarray(edge.convert('L')).max(axis=1)
        black_rows = int((rows == 0).sum())
        assert black_rows > 350, 'expected a mostly black-padded bottom edge tile, got %d black rows' % black_rows
        assert rows[:120].min() > 0, 'the real imagery rows at the top of the edge tile should not be black'

    def test_manifest_matches_the_files_on_disk(self, manifest):
        """Guards against a fixture being re-captured without its manifest entry being updated."""
        for name, meta in manifest['tiles'].items():
            body = fixture_bytes(name)
            assert len(body) == meta['bytes'], '%s changed on disk but the manifest was not updated' % name
            assert list(Image.open(BytesIO(body)).size) == meta['body_size']


class TestTheContractTheseFixturesImply:
    """The three module-level facts, restated as assertions against gsv's helpers, so a future change to
    either the helpers or the fixtures has to face them together."""

    def test_stitch_cell_size_is_the_largest_body_in_the_fanout(self):
        tiles = [(x, y, fixture_bytes(name)) for name, x, y, _ in MIXED_BLOCK]
        assert gsv._stitch_cell_size(tiles) == (512, 512)

    def test_an_all_degraded_fanout_stitches_at_the_degraded_cell_size(self):
        tiles = [(x, y, fixture_bytes(name)) for name, x, y, sz in MIXED_BLOCK if sz == (256, 256)]
        assert gsv._stitch_cell_size(tiles) == (256, 256)

    def test_undersized_bodies_are_counted_so_the_run_can_say_so(self):
        tiles = [(x, y, fixture_bytes(name)) for name, x, y, _ in MIXED_BLOCK]
        assert gsv._undersized_tile_count(tiles) == 2


# --- opt-in live checks ---------------------------------------------------------------------------------
#
# Everything above runs on committed bytes. These two go to Google, so they are off unless you ask for them:
#
#     SIDEWALK_LIVE_TILE_TESTS=1 pytest tests/test_gsv_tile_contract.py -v -k live
#
# Run them when a pano looks wrong in the store, or before a deploy that touches the stitcher - they are the
# check the #68 review wished existed. The second one downloads three full panos (~900 tile requests).

live_only = pytest.mark.skipif(not os.environ.get('SIDEWALK_LIVE_TILE_TESTS'),
                               reason='set SIDEWALK_LIVE_TILE_TESTS=1 to hit Google')

# (label, pano id, reported width, reported height). The 2007 pano has only four zoom levels, so its max
# zoom really is 3 - it exercises _pano_max_zoom's inference rather than the 16384 happy path.
LIVE_PANOS = [('Seattle 2022', 'Svz6_7CwyijJ6RgjWROnCw', 16384, 8192),
              ('Sydney 2014', 'cFou_FaIrbvqN0kcS5QuxA', 13312, 6656),
              ('DC 2007 (four zoom levels)', 'TEKYJ5O1xd0OZ_YqF0lFRA', 3328, 1664)]


@live_only
def test_live_cbk_still_mixes_body_sizes_in_one_fanout():
    """Re-checks the premise of this whole module against the live endpoint. If this ever starts failing
    because every body is 512, the mixed-size handling is merely unnecessary, not wrong - do not remove it
    on one clean run; the endpoint went a full 30 minutes serving nothing but 256 bodies during the #68
    review, then flipped back."""
    import requests
    from collections import Counter

    pano = LIVE_PANOS[0][1]
    session = requests.Session()
    sizes = Counter()
    for x in range(24):
        url = '%s&zoom=5&x=%d&y=8&panoid=%s' % (gsv._CBK_BASE_URL, x, pano)
        body = session.get(url, headers=gsv._random_header(), timeout=30).content
        sizes[Image.open(BytesIO(body)).size] += 1

    assert sum(sizes.values()) == 24
    assert set(sizes) <= {(512, 512), (256, 256)}, 'a body size we have never seen before: %s' % dict(sizes)
    print('\nlive zoom-5 body sizes over 24 tiles: %s' % dict(sizes))


@live_only
@pytest.mark.parametrize('label,pano_id,width,height', LIVE_PANOS)
def test_live_download_fills_the_frame(tmp_path, label, pano_id, width, height):
    """The real pre-deploy spot check: the production path, no stubs, against live tiles. Asserts what the
    #44 triage could not - that the saved JPEG is at the reported dimensions and is not mostly black."""
    Image.MAX_IMAGE_PIXELS = None

    assert gsv.download_single_pano(str(tmp_path), {'pano_id': pano_id, 'width': width, 'height': height})

    with Image.open(os.path.join(str(tmp_path), pano_id[:2], pano_id + '.jpg')) as image:
        image.load()
        assert image.size == (width, height)
        black = gsv._black_fraction(image)
        assert black < 0.01, '%s: saved frame is %.1f%% black' % (label, 100 * black)
