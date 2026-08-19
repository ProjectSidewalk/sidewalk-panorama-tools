"""Tests for reports/scripts/crop_geometry_census.py — the #47 crop-geometry census.

The census replicates CropRunner's geometry vectorized — it has to run over 438k rows, so it needs
the array form. Both replicas are pinned against the REAL functions here, so if the deployed
geometry ever changes the census fails in CI rather than silently measuring a stale formula.

The replicas are pinned against frozen copies of sizing rule v1 rather than against the deployed
functions, which now implement rule v2 — see the note by _v1_predict_crop_size. CropRunner is still
imported (its import is side-effect-free since #52.1, pinned by test_crop_runner.py) for the
geometry properties that v2 did not change.

The committed-findings class pins the conclusions the PR #77 review and the follow-up fixes rest
on, offline, from reports/data/2026-08-10-crop-geometry-census.json.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import CropRunner  # noqa: E402
import crop_geometry_census as cgc  # noqa: E402
import crop_rule_v1  # noqa: E402

# Rule v1, frozen, from the one copy in reports/scripts/crop_rule_v1.py.
#
# This census was run under rule v1 - raw pixels into constants fit on 6656-px panos, square window.
# CropRunner has since shipped rule v2 (resolution-normalised, x2.5, 3:2, angular clamps; see
# reports/2026-08-19-crop-sizing-v2.md), so pinning against the deployed function would now compare
# this report's numbers to a formula that did not produce them.
#
# The pin against CropRunner did its job: it failed the moment the rule changed, which is how this
# note came to be written instead of the census quietly describing geometry nobody cuts any more.
# What replaces it is the same guard against a different target - the vectorized replica in
# crop_geometry_census must still faithfully reproduce the rule the corpus was measured under, which
# is now a historical constant. Imported rather than transcribed, for the reason in crop_rule_v1's
# docstring: three files needed a frozen v1 and three hand copies agree until one is edited.
#
# NOTE FOR ANY RE-USE: findings that depend on the window's SIZE are v1 findings and do not carry
# over. The seam-crossing rate in particular is a function of window width, and v2's windows are
# ~2.5x wider - re-run the census before citing that number against current crops.

_v1_predict_crop_size = crop_rule_v1.predict_crop_size
_v1_compute_crop_box = crop_rule_v1.compute_crop_box

CENSUS_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-10-crop-geometry-census.json')


class TestReplicaFidelity:
    """The census is only as good as its replicas matching the deployed code."""

    def test_predict_crop_size_replica_matches(self):
        real = _v1_predict_crop_size
        for h in (1664.0, 3328.0, 6656.0, 8192.0):
            ys = np.linspace(-800, h + 200, 97)
            mine = cgc.predict_crop_size(ys, np.full_like(ys, h))
            theirs = np.array([real(y, h) for y in ys])
            assert np.allclose(mine, theirs), h

    def test_compute_crop_box_replica_matches(self):
        """Including the banker's rounding: np.round and Python's round are both half-to-even, and
        the census's seam/shift rates would drift by a pixel at odd crop sizes if they weren't."""
        real = _v1_compute_crop_box
        rng = np.random.default_rng(0)
        for w, h in ((16384, 8192), (13312, 6656), (3328, 1664), (512, 256), (200, 600)):
            xs = np.concatenate([rng.uniform(-50, w + 50, 200), [0, w, w - 1, w / 2]])
            ys = np.concatenate([rng.uniform(-800, h + 800, 200), [0, h, h - 1, h / 2]])
            sizes = np.concatenate([rng.uniform(1, 1600, 200), [50, 503, 1500, h + 10]])
            mine = cgc.compute_crop_box(xs, ys, sizes, w, h)
            for i, (x, y, sz) in enumerate(zip(xs, ys, sizes)):
                mine_i = tuple(int(v[i]) for v in mine)
                assert mine_i == real(x, y, sz, w, h), (w, h, x, y, sz)


class TestGeometryFlags:
    """The census machinery, on synthetic frames whose answers are known by construction."""

    def _frame(self, rows):
        df = pd.DataFrame(rows)
        df['time_created'] = pd.Timestamp('2024-01-01', tz='UTC')
        return cgc.add_geometry_columns(df)

    def test_seam_flag_fires_only_near_the_edges(self):
        g = self._frame([
            {'label_id': 1, 'pano_id': 'a', 'pano_x': 8192, 'pano_y': 4800,   # dead centre
             'pano_width': 16384, 'pano_height': 8192},
            {'label_id': 2, 'pano_id': 'a', 'pano_x': 3, 'pano_y': 4800,      # left seam
             'pano_width': 16384, 'pano_height': 8192},
            {'label_id': 3, 'pano_id': 'a', 'pano_x': 16380, 'pano_y': 4800,  # right seam
             'pano_width': 16384, 'pano_height': 8192},
        ])
        assert g['wraps'].tolist() == [False, True, True]

    def test_shift_flag_fires_at_the_poles(self):
        g = self._frame([
            {'label_id': 1, 'pano_id': 'a', 'pano_x': 100, 'pano_y': 4800,
             'pano_width': 16384, 'pano_height': 8192},
            {'label_id': 2, 'pano_id': 'a', 'pano_x': 100, 'pano_y': 8100,   # near the bottom
             'pano_width': 16384, 'pano_height': 8192},
        ])
        assert g['shifts'].tolist() == [False, True]

    def test_x_outside_the_frame_is_separated_from_y(self):
        """The two cases have opposite consequences and must never be pooled: x == width is the
        same world column as x == 0 and crops correctly, y < 0 cannot be recovered."""
        g = self._frame([
            {'label_id': 1, 'pano_id': 'a', 'pano_x': 16384, 'pano_y': 5010,
             'pano_width': 16384, 'pano_height': 8192},
            {'label_id': 2, 'pano_id': 'a', 'pano_x': 845, 'pano_y': -720,
             'pano_width': 13312, 'pano_height': 6656},
        ])
        assert g['x_outside_frame'].tolist() == [True, False]
        assert g['y_outside_frame'].tolist() == [False, True]

    def test_x_at_the_seam_boundary_still_centres_the_label(self):
        """Why x is exempt from the bounds check, asserted rather than argued."""
        width = CropRunner.crop_window_width(5010, 8192)
        at_width = CropRunner.compute_crop_box(16384, 5010, width, 16384, 8192)
        at_zero = CropRunner.compute_crop_box(0, 5010, width, 16384, 8192)
        assert at_width == at_zero
        assert abs((16384 - at_width.left) % 16384 - at_width.width / 2) <= 1

    def test_dims_are_per_pano_detects_a_planted_split(self):
        """Discrimination: the corpus answer is 0, so the check must be able to return non-zero."""
        df = pd.DataFrame([
            {'label_id': 1, 'pano_id': 'a', 'pano_width': 16384.0, 'pano_height': 8192.0},
            {'label_id': 2, 'pano_id': 'a', 'pano_width': 13312.0, 'pano_height': 6656.0},
            {'label_id': 3, 'pano_id': 'b', 'pano_width': 16384.0, 'pano_height': 8192.0},
        ])
        df['time_created'] = pd.to_datetime(
            ['2019-01-01', '2025-01-01', '2020-01-01'], utc=True)
        out = cgc.dims_are_per_pano(df)
        assert out['panos'] == 2
        assert out['panos_with_multiple_dims'] == 1
        assert out['panos_labelled_over_4y_apart'] == 1
        assert out['of_those_with_multiple_dims'] == 1


@pytest.mark.skipif(not os.path.exists(CENSUS_JSON), reason='census JSON not present')
class TestCommittedFindings:
    """The census conclusions the review and the fixes rest on, pinned offline."""

    @staticmethod
    @pytest.fixture(scope='class')
    def census():
        with open(CENSUS_JSON) as f:
            return json.load(f)

    def test_dims_are_a_per_pano_join_not_a_click_time_snapshot(self, census):
        """Finding 1. No pano in 172,790 carries two frames — including the 196 whose labels are
        more than four years apart, which is where a re-serve would have to show up. So the dims
        preflight cannot see a row whose pano_x/pano_y are stale in a refreshed frame."""
        d = census['dims_are_per_pano']
        assert d['panos'] > 150000
        assert d['panos_with_multiple_dims'] == 0
        assert d['panos_labelled_over_4y_apart'] >= 100
        assert d['of_those_with_multiple_dims'] == 0

    def test_the_seam_fix_reaches_about_one_and_a_half_percent_of_labels(self, census):
        """Finding 3's scale: this many crops in a pre-#77 store carry black padding."""
        seam = census['geometry']['seam_crossing']
        assert seam['n'] > 6000
        assert 1.0 < seam['pct'] < 2.5

    def test_the_vertical_shift_fires_only_on_out_of_frame_rows(self, census):
        """Finding 2. Every genuine label's window already fits: the shift count equals the count
        of rows whose pano_y is outside its own frame, so the clamp is a corrupt-data path."""
        geo = census['geometry']
        assert geo['vertical_shift']['n'] == geo['outside_frame']['y_unrecoverable']
        assert geo['vertical_shift']['n'] < 10
        shifted = [r for r in census['outside_frame_rows'] if r['shifts']]
        assert shifted and all(r['axis'] == 'y' for r in shifted)

    def test_real_labels_sit_far_from_both_poles(self, census):
        """Why the shift never fires legitimately: labels live around the horizon."""
        frac = census['geometry']['pano_y_over_height']
        assert frac['0'] > 0.2 and frac['100'] < 0.95

    def test_x_outside_the_frame_is_recoverable_and_must_not_be_rejected(self, census):
        """The wrong turn this census caught: an x-bounds check would discard these."""
        x_rows = [r for r in census['outside_frame_rows'] if r['axis'] == 'x']
        assert x_rows
        assert all(r['recoverable'] and not r['shifts'] for r in x_rows)
