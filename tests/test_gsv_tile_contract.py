"""Pins what Google's CBK tile endpoint actually does, using real tile bytes captured from it.

Everything here runs against committed fixtures in fixtures/tiles/ (74 KB of real CBK responses; see
manifest.json for pano ids, grid positions and capture dates, and fover_band_map.json for the full-grid
sweeps), so the suite stays network-free. Opt-in live re-checks are at the bottom. The point is to hold the
endpoint's real behaviour in the repo, because none of it is documented and we have already argued about it
once:

  1. THE `fover` PARAMETER COSTS HALF THE RESOLUTION. While the CBK URL carried `fover=2` - inherited by
     copying the Street View viewer's URL - CBK returned 256x256 bodies instead of 512x512 for the POLAR
     ROWS of every zoom-5 grid, in a fixed band: full resolution within ~34 degrees of the horizon, half
     resolution above and below. On a 32x16 grid that is rows 0-4 and 11-15, i.e. 320 of 512 tiles.

     It is a deterministic property of the request, not of load, time or position (#73). Any of
     fover=1/2/3 triggers it; fover=0 or omitting it does not; `onerr` is innocent. With `fover` gone, CBK
     returns bodies BYTE-IDENTICAL to streetviewpixels-pa's - which is why #74 is a cleanup rather than a
     resolution fix.

     Credit where due: misaugstad isolated the parameter and the band on #73. The original diagnosis in
     that issue - "per-request load shedding, sticky per position, drifting over hours" - was wrong; every
     observation behind it was the fixed band seen through scattered sampling.

  2. Out-of-range tiles are answered 200 OK with a valid, ALL-BLACK image/jpeg, and short edge tiles are
     padded to a full 512 body rather than returned at their true size. Together those mean a wrong tile
     grid cannot be detected tile by tile - only by looking at the stitched result, which is what
     _reject_mostly_black_stitch is for.

  3. A half-size body is the same grid cell rendered at half scale - proven against the zoom-4 tile covering
     the same region, which matches it to the pixel. That is why bringing every body to the stitch's cell
     size is the correct repair, and why the pre-#44 `img.resize((512, 512))` was load-bearing without
     saying so. downloaders.gsv still does this, as defence in depth: no live cause remains, but the failure
     it prevents is a silently 75%-black pano saved as success.
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


@pytest.fixture(scope='module')
def band_map():
    """Full zoom-5 grid sweeps of four panos, with fover=2 and (for one) without. 1 = every tile in the row
    came back 256x256, 2 = every tile 512x512."""
    with open(os.path.join(FIXTURES, 'fover_band_map.json')) as f:
        return json.load(f)['bands']


class TestFoverIsNotInTheRequest:
    """The regression guard that matters most: `fover` cost us half the resolution of 62.5% of every zoom-5
    pano for as long as it was in the URL, and it looks utterly innocuous sitting among the other viewer
    parameters. Anyone re-adding it - or adding another viewer parameter without checking - trips this."""

    def test_the_base_url_does_not_send_fover(self):
        assert 'fover' not in gsv._CBK_BASE_URL, (
            'fover makes CBK serve the polar rows of zoom 5 at 256x256 instead of 512x512, costing half the '
            'linear resolution over 62.5% of the frame (#73). It is not a harmless viewer parameter.')

    def test_generated_tile_urls_do_not_send_fover(self):
        for _x, _y, url in gsv._generate_tile_urls('panoA', 16384, 8192, 5)[:8]:
            assert 'fover' not in url

    def test_the_probe_urls_do_not_send_fover_either(self):
        """download_single_pano builds its zoom probes off the same constant, so they inherit whatever it
        carries. Pinning the constant covers both, but only while they share it."""
        assert 'fover' not in gsv._CBK_BASE_URL
        assert gsv._CBK_BASE_URL.startswith('https://maps.google.com/cbk?')

    def test_onerr_is_still_sent(self):
        """`onerr=3` was checked at the same time and is innocent - it is what makes an out-of-range tile
        come back as a black JPEG rather than an error, which the zoom probe depends on. Don't drop it while
        cleaning up."""
        assert 'onerr=3' in gsv._CBK_BASE_URL


class TestTheFoverBandOnRealBytes:
    """Same pano, same grid position, one parameter apart - captured in one pass."""

    def test_fover_body_is_half_size_and_the_plain_request_is_not(self):
        assert fixture_image('z5_fover2_4_2.jpg').size == (256, 256)
        assert fixture_image('z5_nofover_4_2.jpg').size == (512, 512)

    def test_both_bodies_are_the_same_imagery(self):
        """So the parameter costs resolution and nothing else - it is not a different crop, a different
        rendering, or a stale cache entry. That is what makes upscaling the correct repair."""
        small = fixture_image('z5_fover2_4_2.jpg').resize((512, 512), Image.LANCZOS)
        full = fixture_image('z5_nofover_4_2.jpg')
        diff = np.abs(np.asarray(small, float) - np.asarray(full, float)).mean()
        assert diff < 3.0, 'the fover body is not the same cell as the plain one (mean|diff|=%.2f)' % diff

    def test_the_fover_body_has_a_quarter_of_the_pixels(self):
        """Discrimination for the test above: "same imagery" must not be read as "interchangeable". The
        parameter really does cost pixels, whatever it costs in perceived detail."""
        small, full = fixture_image('z5_fover2_4_2.jpg'), fixture_image('z5_nofover_4_2.jpg')
        assert small.size[0] * small.size[1] * 4 == full.size[0] * full.size[1]


class TestWhatTheLostResolutionWasActuallyWorth:
    """Measured, because it is the crux of the #73 re-download decision and the intuitive answer is wrong.

    `fover` halved precisely the polar rows, and equirectangular projection oversamples the poles - so those
    rows carry the least real detail per pixel in the whole frame. Halving them destroys far less than
    halving the horizon would. Recorded per row in fover_band_map.json's halving_cost_by_row: the metric is
    how much a full 512 body changes when halved and re-expanded, i.e. the real detail that a half-size body
    at that row would have cost us.

    This is why the recovered resolution is worth having going forward but does not by itself justify
    re-downloading the store - and why a naive sharpness comparison between a fover body and a plain one
    shows almost nothing (LANCZOS ringing on the upscale can even out-score the genuine tile)."""

    @pytest.fixture(scope='class')
    @classmethod
    def costs(cls):
        with open(os.path.join(FIXTURES, 'fover_band_map.json')) as f:
            return json.load(f)['halving_cost_by_row']['panos']

    @pytest.mark.parametrize('label', ['Seattle 2022', 'Sydney 2014'])
    def test_halving_the_horizon_would_cost_much_more_than_halving_the_poles(self, costs, label):
        entry = costs[label]
        assert entry['mean_horizon_cost'] > 2.0 * entry['mean_polar_cost'], (
            '%s: the polar rows are no longer the cheap ones to halve, which would change the #73 '
            'conclusion' % label)

    @pytest.mark.parametrize('label', ['Seattle 2022', 'Sydney 2014'])
    def test_fover_halved_only_rows_in_the_cheap_zone(self, costs, label):
        """The optimisation is well targeted: every row it halved is a polar row, and it left the whole
        horizon band alone. If that were not true the store would be in worse shape than #73 concluded."""
        entry = costs[label]
        halved = [int(y) for y, v in entry['per_row'].items() if v['zone'].startswith('polar')]
        kept = [int(y) for y, v in entry['per_row'].items() if v['zone'].startswith('horizon')]
        assert halved and kept
        assert max(entry['per_row'][str(y)]['cost'] for y in halved) \
            < max(entry['per_row'][str(y)]['cost'] for y in kept), label

    def test_the_zones_agree_with_the_swept_band_map(self, costs, band_map):
        """Ties this measurement to the band sweep, so the two cannot drift apart: the rows labelled polar
        here must be exactly the rows that came back 256 there."""
        for label in ('Sydney 2014',):
            swept = band_map[label]['row_map']
            for y, entry in costs[label]['per_row'].items():
                degraded = swept[int(y)] == '1'
                assert degraded == entry['zone'].startswith('polar'), \
                    '%s row %s: band map and cost table disagree' % (label, y)


class TestTheBandStructure:
    """Pinned from full-grid sweeps, not samples. The band is why the original #73 diagnosis went wrong:
    32 scattered positions hit it without revealing it."""

    ALL = ['Seattle 2022', 'NYC 2024', 'Sydney 2014', 'Tokyo 2018']

    @pytest.mark.parametrize('label,expected_rows,expected_degraded,total', [
        ('Seattle 2022', '1111122222211111', 320, 512),
        ('NYC 2024', '1111122222211111', 320, 512),
        ('Sydney 2014', '1111222221111', 208, 338),
        ('Tokyo 2018', '1111222221111', 208, 338),
    ])
    def test_swept_row_map(self, band_map, label, expected_rows, expected_degraded, total):
        entry = band_map[label]
        assert entry['row_map'] == expected_rows
        assert entry['degraded_tiles'] == expected_degraded
        assert entry['total_tiles'] == total

    @pytest.mark.parametrize('label', ALL)
    def test_no_row_is_mixed(self, band_map, label):
        """Every row is uniformly one size or the other. A '?' means capture.py hit a tile it could not
        fetch after retries, not that the band is fuzzy - re-capture rather than relaxing this."""
        assert '?' not in band_map[label]['row_map'], (
            '%s has a mixed row; re-run tests/fixtures/tiles/capture.py' % label)

    def test_the_degraded_rows_are_the_polar_caps_not_scattered_positions(self, band_map):
        """Every half-res row is at the top or the bottom; the full-res rows are one contiguous band around
        the horizon. A per-position or per-request effect could not produce this."""
        for label in self.ALL:
            row_map = band_map[label]['row_map']
            full = [i for i, c in enumerate(row_map) if c == '2']
            assert full, label
            assert full == list(range(full[0], full[-1] + 1)), '%s: full-res rows are not contiguous' % label
            assert full[0] > 0 and full[-1] < len(row_map) - 1, '%s: band is not bounded by poles' % label

    def test_the_band_is_centred_on_the_horizon(self, band_map):
        """Which is what makes the lost detail cheap: equirectangular already oversamples the poles."""
        for label in self.ALL:
            row_map = band_map[label]['row_map']
            full = [i for i, c in enumerate(row_map) if c == '2']
            centre = (len(row_map) - 1) / 2.0
            band_centre = (full[0] + full[-1]) / 2.0
            assert abs(band_centre - centre) <= 0.5, '%s: band is not centred on the horizon' % label

    def test_the_measured_counts_match_the_row_structure(self, band_map):
        """Ties the two together: the 320/512 and 208/338 figures in #73 are exactly the row bands, which is
        what showed the original 'roughly 60% of positions, randomly' reading was wrong."""
        for label in self.ALL:
            entry = band_map[label]
            cols, _rows = entry['grid']
            half_rows = entry['row_map'].count('1')
            assert entry['degraded_tiles'] == half_rows * cols, label

    def test_without_fover_the_band_is_gone_entirely(self, band_map):
        """The other half of the causal claim, swept over a full 512-tile grid rather than probed."""
        entry = band_map['Seattle 2022 (no fover)']
        assert entry['row_map'] == '2' * 16
        assert entry['degraded_tiles'] == 0
        assert entry['total_tiles'] == 512

    def test_the_two_sweeps_are_the_same_pano(self, band_map):
        """Otherwise the before/after comparison would prove nothing."""
        assert band_map['Seattle 2022 (no fover)']['pano_id'] == band_map['Seattle 2022']['pano_id']


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


def _live_get(session, url, tries=5):
    """Retry until the response decodes. CBK intermittently answers with a non-image body; production rides
    that out with backoff (_TILE_RETRY_ERRORS), so a live check that fails on the first dropped response is
    testing the network, not the endpoint's behaviour."""
    import time

    for attempt in range(tries):
        body = session.get(url, headers=gsv._random_header(), timeout=30).content
        try:
            Image.open(BytesIO(body)).size
            return body
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError('no decodable response after %d tries: %s' % (tries, url))


def _live_tile(session, pano, zoom, x, y, extra=''):
    return _live_get(session, '%s%s&zoom=%d&x=%d&y=%d&panoid=%s'
                     % (gsv._CBK_BASE_URL, extra, zoom, x, y, pano))


@live_only
def test_live_our_url_returns_full_size_bodies_at_the_polar_rows():
    """The headline regression, live: the rows that `fover` used to halve must come back 512. Probes the
    URL production actually sends, so it also fails if a viewer parameter creeps back in."""
    import requests

    pano = LIVE_PANOS[0][1]        # 32x16 grid; rows 0-4 and 11-15 were the half-res band
    session = requests.Session()
    sizes = {y: Image.open(BytesIO(_live_tile(session, pano, 5, 4, y))).size
             for y in (0, 2, 4, 8, 11, 13, 15)}

    assert set(sizes.values()) == {(512, 512)}, 'polar rows are half-size again: %s' % sizes


@live_only
def test_live_fover_is_still_the_cause_and_onerr_is_still_innocent():
    """Pins the causal claim itself rather than just the outcome, so if Google ever changes what `fover`
    does we find out deliberately instead of inferring it from a resolution drop. Row 2 is inside the band,
    row 8 outside it."""
    import requests

    pano = LIVE_PANOS[0][1]
    session = requests.Session()

    def sizes(extra):
        return [Image.open(BytesIO(_live_tile(session, pano, 5, 4, y, extra))).size for y in (2, 8)]

    banded, unbanded = [(256, 256), (512, 512)], [(512, 512), (512, 512)]
    assert sizes('&fover=2') == banded, 'fover=2 no longer halves the polar rows'
    assert sizes('&fover=1') == banded
    assert sizes('&fover=3') == banded
    assert sizes('&fover=0') == unbanded, 'fover=0 should be equivalent to omitting it'
    assert sizes('') == unbanded
    assert sizes('&onerr=1') == unbanded, 'onerr is not the trigger'


@live_only
def test_live_cbk_without_fover_is_byte_identical_to_the_modern_endpoint():
    """Why #74 is a cleanup and not a resolution recovery. If this ever stops holding, the endpoint question
    is live again and #74 should be re-read."""
    import hashlib

    import requests

    pano = LIVE_PANOS[0][1]
    session = requests.Session()
    modern = ('https://streetviewpixels-pa.googleapis.com/v1/tile?cb_client=maps_sv.tactile'
              '&panoid=%s&x=%d&y=%d&zoom=5')
    for y in (0, 2, 11, 15):
        ours = _live_tile(session, pano, 5, 4, y)
        theirs = _live_get(session, modern % (pano, 4, y))
        assert hashlib.md5(ours).hexdigest() == hashlib.md5(theirs).hexdigest(), \
            'row %d: CBK and streetviewpixels-pa have diverged' % y


@live_only
def test_live_dropping_fover_changed_nothing_else():
    """The side-effect check. The zoom probe and _reject_mostly_black_stitch both depend on out-of-range and
    dead-pano requests answering with a black JPEG rather than an error."""
    import requests

    pano = LIVE_PANOS[0][1]
    session = requests.Session()

    def describe(zoom, x, y, target=None):
        body = _live_tile(session, target or pano, zoom, x, y)
        image = Image.open(BytesIO(body))
        return image.size, image.convert('L').getextrema() == (0, 0)

    assert describe(5, 32, 8) == ((512, 512), True), 'out-of-range x is no longer a black 512 tile'
    assert describe(5, 4, 16) == ((512, 512), True), 'out-of-range y is no longer a black 512 tile'
    assert describe(3, 0, 0) == ((512, 512), False), 'the zoom-3 probe tile changed'
    assert describe(5, 0, 0, target='_qVKgG3dGOoClMQI6QgVRg') == ((512, 512), True), \
        'a retired pano no longer answers with a black tile'


@live_only
def test_live_full_download_has_no_undersized_tiles(tmp_path, monkeypatch):
    """End to end, on the production path: a real pano download must now report zero undersized bodies.
    Before the fover fix this was 320 of 512."""
    seen = {}
    real = gsv._undersized_tile_count

    def counting(tile_results):
        seen['undersized'] = real(tile_results)
        seen['total'] = len(tile_results)
        return seen['undersized']

    monkeypatch.setattr(gsv, '_undersized_tile_count', counting)
    label, pano_id, width, height = LIVE_PANOS[0]
    Image.MAX_IMAGE_PIXELS = None

    gsv.download_single_pano(str(tmp_path), {'pano_id': pano_id, 'width': width, 'height': height})

    assert seen['total'] == 512
    assert seen['undersized'] == 0, '%d of %d tiles came back half-size' % (seen['undersized'], seen['total'])


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
