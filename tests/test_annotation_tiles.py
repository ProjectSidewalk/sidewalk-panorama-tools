"""Tests for reports/scripts/annotation_tiles.py — the gold-standard annotation tile renderer.

Two properties carry the whole gold standard, and both are structural rather than statistical:

**The transform must be exact and its own.** Amendment 1(e) forbids porting the webpage's render path,
because Study 1 measures stored `pano_x`/`pano_y` against gold annotations *in pano coordinates* — so if
the tile→pano mapping carried the same error as the projection under test, the study would measure zero
by construction. The mapping is therefore verified by round-trip against directly-indexed pixels rather
than against any other implementation, seam-crossing windows included.

**The annotator must not be able to see the answer.** §4 requires that the stored point, any crop box,
and any prior annotation are never rendered, and that the viewport is jittered. The jitter is not
decoration: it is what stops an annotator from converging on the centre of the tile. So the tests below
check that the annotator-facing package contains no stored coordinate and no jitter — the point being
that blindness is enforced by what the file *lacks*, not by the UI choosing not to draw something.
"""

import json
import os
import sys

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import annotation_tiles as at  # noqa: E402
import pandas as pd  # noqa: E402
import rawlabels  # noqa: E402

PIL = pytest.importorskip('PIL', reason='Pillow is needed to cut tiles')
from PIL import Image  # noqa: E402


def corpus_row(label_uid='seattle-wa:1', pano_id='pano1', label_type='CurbRamp',
               pano_x=8000.0, pano_y=5000.0, pano_width=16384.0, pano_height=8192.0,
               city='seattle-wa', **extra):
    row = {'label_uid': label_uid, 'label_id': int(label_uid.split(':')[1]), 'city': city,
           'pano_id': pano_id, 'label_type': label_type, 'pano_x': pano_x, 'pano_y': pano_y,
           'pano_width': pano_width, 'pano_height': pano_height, 'band': '5-15',
           'quality': 'post_fix', 'split': 'tune', 'measurable': True, 'roles': 'cell'}
    row.update(extra)
    return row


def corpus(rows):
    return pd.DataFrame(rows)


def gradient_pano(width=512, height=256):
    """A pano whose every pixel encodes its own (x, y), so a cut tile can be checked pixel-by-pixel
    against direct indexing. Red carries x, green carries y, blue marks the seam column."""
    xs = np.arange(width, dtype=np.uint16)
    ys = np.arange(height, dtype=np.uint16)
    r = np.tile((xs % 256).astype(np.uint8), (height, 1))
    g = np.tile((ys % 256).astype(np.uint8)[:, None], (1, width))
    b = np.zeros((height, width), dtype=np.uint8)
    b[:, 0] = 255
    return Image.fromarray(np.dstack([r, g, b]), mode='RGB')


class TestTileExtent:
    """The tile is a fixed ANGULAR window, so an annotator sees the same amount of world on a
    13312x6656 pano as on a 16384x8192 one. A fixed pixel window would show half as much world on the
    low-resolution panos — and resolution-dependence is the very thing #32 is trying to remove."""

    def test_the_extent_is_angular_not_pixel(self):
        """Asserted in DEGREES, within a pixel's worth. Comparing the pixel fractions directly fails on
        integer rounding (910/16384 vs 740/13312 differ in the fourth decimal), and loosening that
        comparison until it passes would stop testing the property — which is that an annotator sees
        the same angular extent of world at either resolution."""
        big = at.tile_extent_px(16384.0, 8192.0)
        small = at.tile_extent_px(13312.0, 6656.0)
        assert big != small, 'different rasters must give different pixel extents'
        assert big[0] / 16384.0 * 360.0 == pytest.approx(at.TILE_FOV_DEG, abs=0.02)
        assert small[0] / 13312.0 * 360.0 == pytest.approx(at.TILE_FOV_DEG, abs=0.02)
        assert big[1] / 8192.0 * 180.0 == pytest.approx(at.TILE_FOV_DEG, abs=0.02)
        assert small[1] / 6656.0 * 180.0 == pytest.approx(at.TILE_FOV_DEG, abs=0.02)

    def test_the_extent_matches_the_declared_field_of_view(self):
        w, h = at.tile_extent_px(16384.0, 8192.0, fov_deg=20.0)
        assert w == round(20.0 / 360.0 * 16384.0)
        assert h == round(20.0 / 180.0 * 8192.0)

    def test_a_two_to_one_pano_gives_a_square_tile(self):
        """Equirectangular 2:1 panos have equal degrees-per-pixel on both axes, so the angular window
        is square in pixels too. Computed per axis anyway, because that equality is a property of the
        aspect ratio and not something to assume."""
        assert at.tile_extent_px(16384.0, 8192.0) == at.tile_extent_px(16384.0, 8192.0)[::-1]

    def test_the_view_never_exceeds_the_cut(self):
        """The bound the wide cut exists to remove: a tile of angular width F can only measure a
        displacement up to F/2, because past that the object is off the tile and `object-absent` is the
        only response left — so a tight CUT silently deletes the largest errors from the very
        distribution Study 1 estimates.

        The VIEW is a separate question and moved on 2026-08-13 from 20 deg to the full cut, after
        annotating against it: `fitScale` is bound by the short axis, so on a landscape canvas a
        one-third fraction is 20 deg vertically and ~32 deg across — tight enough that finding the object
        meant panning, which the setting existed to avoid. Opening at the full cut is also the safe
        direction for Study 2, since a window equal to the cut implies nothing tighter than the cut.

        What must hold is only the inequality. A view WIDER than the cut would be a framing showing
        pixels that were never rendered.
        """
        assert at.CUT_FOV_DEG == 60.0
        assert at.VIEW_FOV_DEG == 60.0
        assert at.VIEW_FOV_DEG <= at.CUT_FOV_DEG
        assert at.TILE_FOV_DEG == at.CUT_FOV_DEG, 'geometry is denominated in the cut'

    def test_the_measurable_displacement_reaches_the_observed_signal_offsets(self):
        """Sized against a measurement, not a guess: 45 of the drawn corpus's 72 Signal labels sit
        10-42 deg below the horizon, on the pole base rather than the head their rubric names. Half of
        60 deg covers a 30 deg displacement; half of 20 deg would not have covered any of them."""
        assert at.CUT_FOV_DEG / 2 >= 30.0

    def test_the_cut_is_still_resolvable_on_a_low_resolution_pano(self):
        """The other side of the trade: the corpus contains 3328x1664 panos, where the whole cut is only
        ~555 px. Recorded rather than guarded — those labels have coarser gold by construction, and the
        analysis needs to know that rather than the renderer pretending otherwise."""
        w, h = at.tile_extent_px(3328.0, 1664.0)
        assert (w, h) == (554, 554)
        big, _ = at.tile_extent_px(16384.0, 8192.0)
        assert big / w > 4

    def test_every_extent_is_even(self):
        """So the window centre lands on a pixel boundary and the mapping stays exactly invertible."""
        for W, H in ((16384.0, 8192.0), (13312.0, 6656.0), (3328.0, 1664.0), (512.0, 256.0)):
            w, h = at.tile_extent_px(W, H)
            assert w % 2 == 0 and h % 2 == 0, (W, H, w, h)


STD = (16384, 8192)     # the pano height 641 of the 763 drawn labels sit on


class TestJitter:
    """§4: 'a uniform random jitter of ±40–80 px per axis (seeded, logged)'. Read as magnitude uniform
    in the band with a random sign, NOT uniform in [-max, +max] — the latter puts the stored point at
    or near the tile centre for a meaningful share of labels, which is exactly the anchoring the
    jitter exists to prevent. Read as a FRACTION of the tile rather than as pixels, for the reason in
    TestJitterIsAnAngle below."""

    def test_the_magnitude_is_always_in_the_specified_band(self):
        w, h = at.tile_extent_px(*STD)
        for i in range(400):
            jx, jy = at.jitter_for(f'city:{i}', 1, *STD)
            assert at.JITTER_MIN_FRAC <= abs(jx) / w <= at.JITTER_MAX_FRAC, jx
            assert at.JITTER_MIN_FRAC <= abs(jy) / h <= at.JITTER_MAX_FRAC, jy

    def test_the_stored_point_is_never_at_the_tile_centre(self):
        """The property that matters, stated over the offset rather than over the distribution."""
        for i in range(200):
            jx, jy = at.jitter_for(f'city:{i}', 7, *STD)
            assert jx != 0 and jy != 0

    def test_both_signs_occur(self):
        """Discrimination: a magnitude-only implementation would displace every tile the same way, and
        an annotator could learn the offset."""
        signs = {(np.sign(at.jitter_for(f'city:{i}', 3, *STD)[0]),
                  np.sign(at.jitter_for(f'city:{i}', 3, *STD)[1])) for i in range(200)}
        assert signs == {(-1, -1), (-1, 1), (1, -1), (1, 1)}

    def test_it_is_deterministic_per_label(self):
        assert at.jitter_for('seattle-wa:5', 11, *STD) == at.jitter_for('seattle-wa:5', 11, *STD)

    def test_it_is_seed_sensitive(self):
        different = [at.jitter_for(f'c:{i}', 1, *STD) != at.jitter_for(f'c:{i}', 2, *STD)
                     for i in range(50)]
        assert sum(different) > 40

    def test_one_labels_jitter_does_not_depend_on_any_other(self):
        """Derived from the label's own uid, so re-running after the corpus gains or loses a label
        reproduces every surviving label's tile exactly. A shared sequential RNG would reshuffle
        everything downstream of an insertion — and tiles already annotated would no longer match the
        geometry the annotation was recorded against."""
        alone = at.jitter_for('seattle-wa:99', 5, *STD)
        assert alone == at.jitter_for('seattle-wa:99', 5, *STD)
        assert alone != at.jitter_for('seattle-wa:98', 5, *STD)


class TestJitterIsAnAngle:
    """The jitter has to be resolution-independent for the same reason the tile does.

    The module's whole design argument is that a fixed PIXEL window shows an annotator different
    amounts of world on different panos, so the tile is a fixed angular window. §4 wrote the jitter as
    40–80 px, which left the one quantity whose job is to decentre the stored point varying with
    resolution — and varying the wrong way. Measured on the drawn corpus, 40–80 px was 1.5–2.9% of the
    tile on the 8192-height panos 641 of 763 labels sit on, against 7.2–14.4% on the 1664s: a ~5x
    swing, weakest on 84% of the corpus, where 2% off centre reads as dead centre.
    """

    HEIGHTS = (1664, 2880, 6656, 8192, 16384)

    # The offset is an angle stored as whole pixels, so two panos agree only to their own rounding.
    # One pixel of the coarsest pano in the set is the honest tolerance: tighter than that asserts
    # something the integer return type cannot deliver, looser stops discriminating.
    COARSEST_PIXEL_DEG = 360.0 / (2 * min(HEIGHTS))

    def test_the_same_label_is_displaced_by_the_same_angle_at_every_resolution(self):
        for i in range(40):
            degs = [abs(at.jitter_for(f'city:{i}', 20260812, h * 2, h)[0]) / (h * 2) * 360.0
                    for h in self.HEIGHTS]
            assert max(degs) - min(degs) <= self.COARSEST_PIXEL_DEG, (i, degs)

    def test_a_pixel_jitter_would_not_have_this_property(self):
        """Discrimination, twice over: the assertion above passes trivially for a constant, so pin
        that the pixel offsets it is computed from really do differ across resolutions — and that the
        fixed-pixel reading it replaced would blow the tolerance by two orders of magnitude."""
        pixels = {at.jitter_for('seattle-wa:1', 20260812, h * 2, h)[0] for h in self.HEIGHTS}
        assert len(pixels) == len(self.HEIGHTS)
        fixed = [60.0 / (h * 2) * 360.0 for h in self.HEIGHTS]      # a constant 60 px, as §4 wrote it
        assert max(fixed) - min(fixed) > 20 * self.COARSEST_PIXEL_DEG   # measured: 54x the tolerance

    def test_it_lands_in_the_band_section_4_meant(self):
        """§4's 40–80 px, on the pano the corpus is mostly made of, at the 20 deg tile §4 was written
        for. The cut later tripled to 60 deg and the pixel constant did not move, which is what
        diluted it; restating it as a fraction restores the design point and makes the drift
        impossible."""
        assert (at.JITTER_MIN_FRAC, at.JITTER_MAX_FRAC) == (40.0 / 910.0, 80.0 / 910.0)
        assert at.tile_extent_px(16384, 8192, fov_deg=20.0)[1] == 910
        assert 2.6 < at.JITTER_MIN_FRAC * at.CUT_FOV_DEG < 2.7
        assert 5.2 < at.JITTER_MAX_FRAC * at.CUT_FOV_DEG < 5.3


class TestTileWindow:

    def test_the_window_is_centred_on_the_jittered_point(self):
        w = at.tile_window(pano_x=8000.0, pano_y=4000.0, pano_width=16384.0, pano_height=8192.0,
                           jx=50, jy=-60)
        assert w.left + w.width / 2 == pytest.approx(8000.0 + 50)
        assert w.top + w.height / 2 == pytest.approx(4000.0 - 60)
        assert w.shifted is False

    def test_a_window_crossing_the_seam_keeps_its_full_width(self):
        """Column 0 and column pano_width are the same place in the world, so a window straddling them
        is ordinary — it must wrap, not clip. Clipping would silently narrow the tile and move the
        mapping origin."""
        w = at.tile_window(10.0, 4000.0, 16384.0, 8192.0, jx=-40, jy=40)
        assert w.width == at.tile_extent_px(16384.0, 8192.0)[0]
        assert w.wraps is True

    def test_a_window_at_the_pole_shifts_instead_of_overflowing(self):
        """Cannot happen for real labels — corpus depression p99 is 43.5 deg against a 20 deg tile —
        but a clip or an out-of-range read is a crash or a black band, and the shift keeps the tile
        full-size with an exact mapping."""
        w = at.tile_window(8000.0, 5.0, 16384.0, 8192.0, jx=40, jy=-40)
        assert w.top == 0
        assert w.height == at.tile_extent_px(16384.0, 8192.0)[1]
        assert w.shifted is True

        w2 = at.tile_window(8000.0, 8190.0, 16384.0, 8192.0, jx=40, jy=40)
        assert w2.top + w2.height == 8192
        assert w2.shifted is True

    def test_the_window_never_leaves_the_raster_vertically(self):
        for y in (0.0, 1.0, 100.0, 4096.0, 8100.0, 8192.0):
            w = at.tile_window(8000.0, y, 16384.0, 8192.0, jx=40, jy=40)
            assert w.top >= 0 and w.top + w.height <= 8192, y


class TestCoordinateRoundTrip:
    """The mapping is what Study 1's estimate is denominated in: an annotation is submitted in tile
    pixels and becomes a pano coordinate. An off-by-one here is a systematic placement bias
    indistinguishable from the real thing."""

    @pytest.mark.parametrize('pano_x', [0.0, 1.0, 10.0, 8000.0, 16380.0, 16383.0])
    @pytest.mark.parametrize('pano_y', [200.0, 4000.0, 7000.0])
    def test_pano_to_tile_inverts_tile_to_pano(self, pano_x, pano_y):
        W, H = 16384.0, 8192.0
        win = at.tile_window(pano_x, pano_y, W, H, jx=-55, jy=65)
        for tx in (0, 1, win.width // 2, win.width - 1):
            for ty in (0, 1, win.height // 2, win.height - 1):
                px, py = at.tile_to_pano(win, tx, ty, W)
                back = at.pano_to_tile(win, px, py, W)
                assert back == (tx, ty), (pano_x, pano_y, tx, ty, px, py, back)

    def test_the_stored_point_maps_to_where_the_jitter_puts_it(self):
        """The jitter offset is exactly recoverable from the geometry — which is why the geometry is
        withheld from the annotator (see TestBlindness) rather than merely unrendered."""
        W, H = 16384.0, 8192.0
        win = at.tile_window(8000.0, 4000.0, W, H, jx=50, jy=-60)
        tx, ty = at.pano_to_tile(win, 8000.0, 4000.0, W)
        assert (tx, ty) == (win.width // 2 - 50, win.height // 2 + 60)

    def test_a_seam_crossing_window_maps_continuously(self):
        """Across the seam the pano x jumps from pano_width-1 to 0, and the tile must not."""
        W, H = 16384.0, 8192.0
        win = at.tile_window(5.0, 4000.0, W, H, jx=-40, jy=40)
        xs = [at.tile_to_pano(win, tx, 10, W)[0] for tx in range(win.width)]
        assert all(0 <= x < W for x in xs)
        assert len(set(xs)) == win.width, 'no pano column may be visited twice'
        # Exactly one step in the sequence is the wrap itself.
        steps = [b - a for a, b in zip(xs, xs[1:])]
        assert steps.count(1) == len(steps) - 1
        assert sorted(steps)[0] == -(W - 1)


class TestCutTile:

    def test_the_tile_pixels_are_the_pano_pixels(self):
        """Checked against direct indexing of a coordinate-encoding pano, not against another
        implementation of the same idea."""
        pano = gradient_pano(512, 256)
        win = at.tile_window(200.0, 128.0, 512.0, 256.0, jx=40, jy=-40, fov_deg=20.0)
        tile = at.cut_tile(pano, win, 512.0)
        assert tile.size == (win.width, win.height)
        src, out = np.asarray(pano), np.asarray(tile)
        for tx in (0, win.width // 3, win.width - 1):
            for ty in (0, win.height // 2, win.height - 1):
                px, py = at.tile_to_pano(win, tx, ty, 512.0)
                assert tuple(out[ty, tx]) == tuple(src[int(py), int(px)]), (tx, ty)

    def test_a_seam_crossing_tile_carries_no_synthetic_black(self):
        """The failure this guards is a tile with a black band where the raster ran out — an annotator
        would read it as the edge of the world and place differently.

        The jitter here is deliberately smaller than §4's band: on this 512-wide test raster a 20 deg
        tile is 28 px, so any jitter of 40+ px carries the window clear of the seam and the case under
        test would not arise. `tile_window` takes the offset as an argument precisely so geometry can be
        exercised independently of the draw that generates it; the ±40–80 band is `jitter_for`'s
        property and is tested there.
        """
        pano = gradient_pano(512, 256)
        win = at.tile_window(3.0, 128.0, 512.0, 256.0, jx=-5, jy=5, fov_deg=20.0)
        assert win.wraps
        tile = np.asarray(at.cut_tile(pano, win, 512.0))
        assert tile.shape[:2] == (win.height, win.width)
        assert (tile[:, :, 2] == 255).sum() == win.height, 'the seam column appears exactly once'
        for tx in range(win.width):
            px, py = at.tile_to_pano(win, tx, 5, 512.0)
            assert tuple(tile[5, tx]) == tuple(np.asarray(pano)[int(py), int(px)]), tx

    def test_the_tile_is_cut_once_per_pano_regardless_of_label_count(self):
        """Two labels on one pano must not decode a 250 MB raster twice — the same reason CropRunner
        groups by pano."""
        pano = gradient_pano(512, 256)
        wins = [at.tile_window(x, 128.0, 512.0, 256.0, jx=40, jy=40, fov_deg=20.0)
                for x in (100.0, 300.0)]
        tiles = [at.cut_tile(pano, w, 512.0) for w in wins]
        assert len({t.size for t in tiles}) == 1
        assert not np.array_equal(np.asarray(tiles[0]), np.asarray(tiles[1]))


class TestRubric:
    """§4's rubric is binding, and it is data here rather than prose in a prompt so that the text the
    annotator sees is the text under version control."""

    def test_every_corpus_label_type_has_a_canonical_point_rule(self):
        for label_type in at.CORPUS_LABEL_TYPES:
            assert label_type in at.RUBRIC, label_type
            assert len(at.RUBRIC[label_type]) > 20, label_type

    def test_it_covers_the_eight_types_the_corpus_carries(self):
        assert at.CORPUS_LABEL_TYPES == frozenset({
            'CurbRamp', 'NoCurbRamp', 'Obstacle', 'SurfaceProblem', 'Crosswalk', 'Signal',
            'NoSidewalk', 'Other'})

    def test_the_flags_are_the_four_the_protocol_names(self):
        """Order matters as much as membership: the page binds these to keys 1..N by index, so
        reordering them silently rebinds every annotator's muscle memory mid-study, and a flag recorded
        against the wrong key is indistinguishable from a real judgement in the output."""
        assert at.FLAGS == ('object-absent', 'ambiguous', 'occluded', 'no-extent')

    def test_no_extent_is_distinct_from_ambiguous(self):
        """Amendment 3 could have overloaded `ambiguous` and saved a key. It must not: they fail
        differently and the analysis has to tell them apart. `ambiguous` means "I cannot tell WHICH
        thing this label is about"; `no-extent` means "I know exactly what it is about and it has no
        particular centre or edge". Only the second is evidence about the referent rule's coverage —
        it is the leak-catcher for the labels the pre-draw tag rule cannot see, because tags are
        optional and 14% of Obstacle labels carry none.
        """
        assert 'no-extent' in at.FLAGS and 'ambiguous' in at.FLAGS
        assert len(set(at.FLAGS)) == len(at.FLAGS)


class TestBlindness:
    """The blindness audit, enforced structurally. §4 says the stored point is never rendered; this goes
    further and never *ships* it, because a UI that merely declines to draw a value it was handed is one
    edit away from anchoring every annotation, and the leak would be invisible in the output."""

    @pytest.fixture
    def built(self):
        rows = [corpus_row(f'seattle-wa:{i}', pano_id=f'p{i}', pano_x=8000.0 + i, pano_y=5000.0 - i)
                for i in range(5)]
        return at.build_tasks(corpus(rows), seed=20260812)

    def test_the_annotator_package_has_no_stored_coordinate(self, built):
        tasks, _ = built
        blob = json.dumps(tasks)
        for row in tasks['tasks']:
            assert 'pano_x' not in row and 'pano_y' not in row
            assert 'jitter_x' not in row and 'jitter_y' not in row
            assert 'left' not in row and 'top' not in row
        assert '8000' not in blob and '5000' not in blob

    def test_the_annotator_package_carries_what_the_task_needs_and_no_more(self, built):
        """A strict allowlist, not a denylist, so every field added to a task record is a decision
        someone had to come here and make. `tags` was added on 2026-08-13 and this is the test that
        stopped it going in unexamined: tags name WHAT a label is about — `pole`, `stairs` — and never
        where, so no function takes them to a stored coordinate. They earn their place by telling the
        annotator which of several candidate objects on a busy tile the rubric is talking about, which
        the rubric alone cannot do.
        """
        tasks, _ = built
        row = tasks['tasks'][0]
        assert set(row) == {'label_uid', 'tile', 'tile_width', 'tile_height', 'label_type', 'tags',
                            'rubric'}

    def test_tags_are_the_only_metadata_that_got_in(self, built):
        """The corpus row carries plenty that would be convenient and is not blindness-safe — `city`
        and `pano_id` identify the imagery, `band` and `depression` are the covariate under study, and
        `severity`/`agree_count` are the labeller's own confidence. None of them belong in front of an
        annotator."""
        row = built[0]['tasks'][0]
        for leaked in ('city', 'pano_id', 'band', 'depression', 'severity', 'agree_count',
                       'pano_width', 'pano_height', 'user_id'):
            assert leaked not in row, leaked

    def test_the_geometry_is_kept_separately_for_the_analysis(self, built):
        tasks, geometry = built
        assert set(geometry['geometry']) == {t['label_uid'] for t in tasks['tasks']}
        g = geometry['geometry']['seattle-wa:0']
        for key in ('left', 'top', 'width', 'height', 'jitter_x', 'jitter_y',
                    'pano_width', 'pano_height', 'pano_x', 'pano_y'):
            assert key in g, key

    def test_the_two_files_are_enough_to_recover_a_pano_coordinate(self, built):
        """The round trip the analysis performs: a tile-space annotation plus the private geometry
        gives a pano coordinate, with nothing the annotator held contributing to it."""
        tasks, geometry = built
        g = geometry['geometry']['seattle-wa:0']
        win = at.window_from_geometry(g)
        px, py = at.tile_to_pano(win, g['width'] // 2, g['height'] // 2, g['pano_width'])
        assert px == pytest.approx(8000.0 + g['jitter_x'], abs=1.0)
        assert py == pytest.approx(5000.0 + g['jitter_y'], abs=1.0)

    def test_the_rubric_shipped_is_the_type_specific_one(self, built):
        tasks, _ = built
        for row in tasks['tasks']:
            assert row['rubric'] == at.RUBRIC[row['label_type']]

    def test_no_task_leaks_the_stored_point_through_the_tile_name(self, built):
        """A filename like `p3_8000_5000.jpg` would defeat every check above."""
        tasks, _ = built
        for row in tasks['tasks']:
            assert row['tile'] == f"{row['label_uid'].replace(':', '_')}.jpg"


class TestBuildTasks:

    def test_it_covers_every_corpus_row(self):
        rows = [corpus_row(f'c:{i}', pano_id=f'p{i}') for i in range(12)]
        tasks, geometry = at.build_tasks(corpus(rows), seed=1)
        assert len(tasks['tasks']) == 12
        assert len(geometry['geometry']) == 12

    def test_it_records_the_seed_so_the_tiles_can_be_regenerated(self):
        tasks, geometry = at.build_tasks(corpus([corpus_row()]), seed=4242)
        assert geometry['seed'] == 4242
        assert geometry['cut_fov_deg'] == at.CUT_FOV_DEG
        assert geometry['view_fov_deg'] == at.VIEW_FOV_DEG

    def test_the_view_fraction_ships_but_the_geometry_does_not(self):
        """The starting zoom is a constant of the protocol — identical for every label — so it tells an
        annotator nothing about where any stored point is, unlike the tile origin."""
        tasks, _ = at.build_tasks(corpus([corpus_row()]), seed=1)
        assert tasks['initial_view_fraction'] == pytest.approx(at.VIEW_FOV_DEG / at.CUT_FOV_DEG)

    def test_it_is_deterministic(self):
        rows = [corpus_row(f'c:{i}', pano_id=f'p{i}') for i in range(6)]
        a, _ = at.build_tasks(corpus(rows), seed=9)
        b, _ = at.build_tasks(corpus(rows), seed=9)
        assert a == b

    def test_it_rejects_a_label_type_with_no_rubric(self):
        """An unknown type must not reach an annotator with an empty instruction — the rubric is
        binding and 'use your judgement' is what it exists to prevent."""
        with pytest.raises(ValueError, match='rubric'):
            at.build_tasks(corpus([corpus_row(label_type='Bollard')]), seed=1)

    def test_the_task_order_does_not_group_by_stratum(self):
        """Interleaved deliberately: §4 has Jon's 50 running through the same tooling, and a run
        ordered by band or type lets an annotator calibrate on a block of similar labels and drift
        between blocks."""
        rows = ([corpus_row(f'c:{i}', pano_id=f'p{i}', label_type='CurbRamp') for i in range(10)]
                + [corpus_row(f'c:{100 + i}', pano_id=f'q{i}', label_type='Obstacle')
                   for i in range(10)])
        tasks, _ = at.build_tasks(corpus(rows), seed=3)
        types = [t['label_type'] for t in tasks['tasks']]
        runs = sum(1 for a, b in zip(types, types[1:]) if a != b)
        assert runs >= 5, f'order looks blocked by type: {types}'



def _capture_rendered_uids(monkeypatch):
    """Replace `render` with a recorder, so main()'s filtering can be checked without any pixels.

    Returns a list that fills with the uids main() actually handed to the renderer. Every outcome is
    `unreachable`, which is the real code path for a pano the local cache does not hold, so main()
    finishes normally and writes its task file.
    """
    seen = []

    def fake_render(tasks, geometry, pano_root, out_dir):
        seen.extend(t['label_uid'] for t in tasks['tasks'])
        return {t['label_uid']: 'unreachable' for t in tasks['tasks']}

    monkeypatch.setattr(at, 'render', fake_render)
    return seen


class TestMeasurableOnlyUsesTheLiveRule:
    """`--measurable-only` recomputes the referent rule and the study frame; it must never read the
    corpus CSV's `measurable` column.

    That column is a snapshot of the rule as it stood when the corpus was drawn, and the rule has moved
    twice since — on the committed GSV corpus it says 584 where the live rule says 368. This script had
    the stale reading, which is the failure `annotation_subset.py` was written to prevent, one script
    upstream of it: a tile set rendered from the wrong population looks perfectly well-formed, and 216
    labels of annotation would be spent on labels with no point to be displaced from.
    """

    def _corpus_csv(self, tmp_path, rows):
        path = tmp_path / 'corpus.csv'
        corpus(rows).to_csv(path, index=False)
        return str(path)

    def test_a_stale_column_does_not_decide_the_queue(self, tmp_path, monkeypatch):
        rows = [
            # The column says measurable, the live rule says no (Signal has no located referent).
            corpus_row('c:1', pano_id='p1', label_type='Signal', tags='[]', measurable=True),
            # The column says not measurable, the live rule says yes.
            corpus_row('c:2', pano_id='p2', label_type='CurbRamp', tags='[]', measurable=False),
            # Excluded by the frame arm rather than the referent arm.
            corpus_row('c:3', pano_id='p3', label_type='CurbRamp', tags='[]', measurable=True,
                       city='taipei'),
        ]
        seen = _capture_rendered_uids(monkeypatch)
        at.main([self._corpus_csv(tmp_path, rows), '--pano-root', str(tmp_path),
                 '--out-dir', str(tmp_path / 'out'), '--seed', '1', '--measurable-only'])
        assert seen == ['c:2']

    def test_without_the_flag_the_whole_corpus_is_rendered(self, tmp_path, monkeypatch):
        """Discrimination: the filter must be the flag's doing, not something applied unconditionally.
        The corpus arm and the measurable arm are deliberately different populations — Study 2 sizes
        crops for types Study 1 cannot measure."""
        rows = [corpus_row('c:1', pano_id='p1', label_type='Signal', tags='[]'),
                corpus_row('c:2', pano_id='p2', label_type='CurbRamp', tags='[]')]
        seen = _capture_rendered_uids(monkeypatch)
        at.main([self._corpus_csv(tmp_path, rows), '--pano-root', str(tmp_path),
                 '--out-dir', str(tmp_path / 'out'), '--seed', '1'])
        assert sorted(seen) == ['c:1', 'c:2']

    def test_it_is_the_shared_rule_and_not_a_transcription(self, tmp_path, monkeypatch):
        """Patch the rule and the filter must move with it — otherwise this grew its own copy of the
        type list and could drift from `annotation_subset`'s answer."""
        rows = [corpus_row('c:1', pano_id='p1', label_type='CurbRamp', tags='[]'),
                corpus_row('c:2', pano_id='p2', label_type='Obstacle', tags='[]')]
        monkeypatch.setattr(rawlabels, 'NO_REFERENT_TYPES', frozenset({'CurbRamp'}))
        seen = _capture_rendered_uids(monkeypatch)
        at.main([self._corpus_csv(tmp_path, rows), '--pano-root', str(tmp_path),
                 '--out-dir', str(tmp_path / 'out'), '--seed', '1', '--measurable-only'])
        assert seen == ['c:2']
