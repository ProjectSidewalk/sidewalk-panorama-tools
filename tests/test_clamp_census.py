"""Tests for reports/scripts/clamp_census.py — the crop-size clamp/truncation census.

The census replicates CropRunner.predict_crop_size (CropRunner has no import guard, so importing
it would run a download). The replica is pinned against the REAL function, ast-extracted from
CropRunner.py source — if the deployed formula ever changes, the census fails here rather than
silently measuring a stale formula.
"""

import ast
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

import clamp_census as cc  # noqa: E402


def _real_predict_crop_size():
    """Extract predict_crop_size from CropRunner.py without importing the module."""
    with open(os.path.join(REPO_ROOT, 'CropRunner.py')) as f:
        tree = ast.parse(f.read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == 'predict_crop_size')
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'CropRunner.py', 'exec'), ns)
    return ns['predict_crop_size']


class TestReplicaFidelity:

    def test_replica_matches_croprunner_everywhere(self):
        """Vectorized replica == the deployed scalar function across the full (pano_y, height)
        grid, clamp regions included."""
        real = _real_predict_crop_size()
        for h in (1664.0, 3328.0, 6656.0, 8192.0, 16384.0):
            ys = np.linspace(0, h, 97)
            mine = cc.predict_crop_size(ys, np.full_like(ys, h))
            theirs = np.array([real(y, h) for y in ys])
            assert np.allclose(mine, theirs), h


class TestCommittedFindings:
    """The census conclusions, pinned against the committed JSON (offline)."""

    @pytest.fixture(scope='class')
    @classmethod
    def summary(cls):
        import json
        path = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-09-clamp-census.json')
        with open(path) as f:
            return json.load(f)

    def test_a_fifth_of_the_corpus_is_size_clamped(self, summary):
        assert 15.0 <= summary['overall']['clamp_1500_pct'] <= 25.0
        assert summary['overall']['clamp_50_pct'] == pytest.approx(0.0, abs=0.01)

    def test_edge_truncation_is_a_non_issue_in_production(self, summary):
        """The #77 shift machinery is safety, not a hot path: measured exposure is exactly zero.

        Asserted as == 0, not < 0.05: the report used to claim "9 labels corpus-wide" while the
        committed JSON said 0.0, and a `< 0.05` pin passed for both, so it could not adjudicate.
        9/436348 would be 0.00206%.
        """
        for scope in ('truncated_bottom_pct', 'truncated_top_pct'):
            assert summary['overall'][scope] == 0.0, scope
        for group in ('by_city', 'by_label_type'):
            for name, s in summary[group].items():
                assert s['truncated_bottom_pct'] == 0.0, (group, name)
                assert s['truncated_top_pct'] == 0.0, (group, name)

    def test_resolution_dependence_hits_ninety_percent_of_labels(self, summary):
        rd = summary['resolution_dependence']
        h8192 = rd['8192']
        assert h8192['share_pct'] >= 80.0
        assert 1.15 <= h8192['crop_ratio_vs_6656_p50'] <= 1.25
        assert rd['6656']['crop_ratio_vs_6656_p50'] == pytest.approx(1.0)


class TestCrossCensusReconciliation:
    """Two committed censuses measure nearly the same thing over nearly the same corpus and report
    different numbers (0 truncations here, 2 vertical shifts in the #77 crop-geometry census). That
    is exactly the kind of pair that gets re-argued months later, so the reconciliation is pinned
    rather than left to a paragraph."""

    @staticmethod
    def _load(name):
        import json
        with open(os.path.join(REPO_ROOT, 'reports', 'data', name)) as f:
            return json.load(f)

    def test_the_two_corpora_differ_by_exactly_the_corrupt_rows(self):
        """This census filters the 2 negative-pano_y rows; the geometry census keeps them."""
        clamp = self._load('2026-08-09-clamp-census.json')
        geo = self._load('2026-08-10-crop-geometry-census.json')
        shift = geo['geometry']['vertical_shift']
        geo_n = round(shift['n'] / (shift['pct'] / 100))
        assert clamp['overall']['n'] == 436348
        assert geo_n == clamp['overall']['n'] + 2

    def test_every_vertical_shift_is_a_corrupt_negative_y_row(self):
        """The whole reconciliation: the geometry census's shift population is precisely the rows
        this census drops, so among sound labels the exposure really is zero."""
        geo = self._load('2026-08-10-crop-geometry-census.json')
        shifting = [r for r in geo['outside_frame_rows'] if r.get('shifts')]
        assert geo['geometry']['vertical_shift']['n'] == len(shifting) == 2
        assert {r['label_id'] for r in shifting} == {231546, 233419}
        for r in shifting:
            assert r['pano_y'] < 0, r
            assert r['axis'] == 'y' and r['recoverable'] is False


class TestClampAndTruncationOnsets:
    """The analytic boundaries the report now leans on instead of a raw count."""

    def test_clamp_onset_matches_a_brute_force_scan(self):
        """Closed form vs sweeping predict_crop_size — a transcription slip in either shows here."""
        for h in (3328.0, 6656.0, 8192.0):
            onset = float(cc.clamp_onset_depression_deg(h))
            deps = np.linspace(0, 90, 900001)
            crop = cc.predict_crop_size(h / 2 + deps / 90 * (h / 2), np.full_like(deps, h))
            first = deps[np.nonzero(crop >= 1500.0)[0][0]]
            assert onset == pytest.approx(first, abs=1e-3), h

    def test_the_onset_scales_inversely_with_height(self):
        """The whole resolution mechanism in one assertion: halve the height, double the onset."""
        assert float(cc.clamp_onset_depression_deg(8192.0)) == pytest.approx(22.24, abs=0.01)
        assert float(cc.clamp_onset_depression_deg(6656.0)) == pytest.approx(27.37, abs=0.01)
        assert (float(cc.clamp_onset_depression_deg(3328.0))
                == pytest.approx(2 * float(cc.clamp_onset_depression_deg(6656.0))))

    def test_the_far_clamp_sits_above_the_horizon_so_it_can_never_fire(self):
        """0.000% far clamp is structural, not rare: it needs a label sighted 81 deg ABOVE the
        horizon, which no click can produce."""
        assert float(cc.clamp_onset_depression_deg(8192.0, 50.0)) == pytest.approx(-81.0, abs=0.1)
        assert float(cc.clamp_onset_depression_deg(6656.0, 50.0)) < 0

    def test_the_modelled_distance_reaches_zero_inside_the_corpus(self):
        """Past this depression the deployed model says the label is at 0 m. On 8192 that is
        28.6 deg, against a corpus p90 of 27.3 deg — a tenth of all labels."""
        assert float(cc.clamp_onset_depression_deg(8192.0, np.inf)) == pytest.approx(28.56, abs=0.01)
        assert float(cc.clamp_onset_depression_deg(6656.0, np.inf)) == pytest.approx(35.15, abs=0.01)

    def test_bottom_truncation_onset_is_far_outside_the_corpus(self):
        """Corpus depression p99 is 43.5 deg; truncation needs 53-74 deg at every height that
        carries labels. That headroom is why the census counts exactly zero."""
        onsets = {h: cc.bottom_truncation_onset_depression_deg(h)
                  for h in (1664.0, 3328.0, 6656.0, 8192.0)}
        assert onsets[8192.0] == pytest.approx(73.52, abs=0.01)
        assert onsets[6656.0] == pytest.approx(69.72, abs=0.01)
        assert min(onsets.values()) > 50.0

    def test_the_truncation_onset_actually_truncates(self):
        """Discrimination: just past the onset the crop overflows, just before it does not."""
        h = 8192.0
        onset = cc.bottom_truncation_onset_depression_deg(h)
        for dep, want in ((onset + 0.05, True), (onset - 0.05, False)):
            y = h / 2 + dep / 90 * (h / 2)
            assert bool(y + cc.predict_crop_size(np.array([y]), np.array([h]))[0] / 2 > h) is want


class TestCensusMechanics:

    def test_saturation_flags(self):
        """distance < ~4.37 m clamps to 1500; the 50 clamp needs distance > ~76 m."""
        h = 6656.0
        steep = h / 2 + 0.30 * h  # deep depression -> tiny distance -> 1500
        assert cc.predict_crop_size(np.array([steep]), np.array([h]))[0] == 1500
        horizon = h / 2  # old_pano_y = 0 -> distance 19.8 m -> mid-range
        v = cc.predict_crop_size(np.array([horizon]), np.array([h]))[0]
        assert 50 < v < 1500

    def test_resolution_dependence_is_real(self):
        """The same physical depression yields different crop sizes at 6656 vs 8192 height —
        the pixel-linear distance formula is resolution-dependent. This is the phenomenon the
        census quantifies (and #32 will fix)."""
        dep = 15.0
        y1, h1 = 6656 / 2 + dep / 90 * 6656 / 2, 6656.0
        y2, h2 = 8192 / 2 + dep / 90 * 8192 / 2, 8192.0
        c1 = cc.predict_crop_size(np.array([y1]), np.array([h1]))[0]
        c2 = cc.predict_crop_size(np.array([y2]), np.array([h2]))[0]
        assert abs(c1 - c2) / c1 > 0.10

    def test_bottom_truncation_flag(self):
        """A steep label on a low-res pano wants a 1500 crop that runs off the bottom edge."""
        h = 3328.0
        y = h / 2 + 0.75 * h / 2  # depression ~67.5 deg: distance ~0.8 m -> 1500 crop
        df = pd.DataFrame({'pano_y': [y], 'pano_x': [100.0], 'pano_height': [h],
                           'pano_width': [2 * h]})
        out = cc.add_census_columns(df)
        assert bool(out['truncated_bottom'].iloc[0])
        assert out['crop_size'].iloc[0] == 1500

    def test_resolution_dependence_aggregates_and_skips_thin_heights(self):
        """The function behind the census's 1.198x headline, which no test previously called.

        600 labels at 8192 and 600 at 6656, same depressions: the 6656 rows are the fitted frame so
        their ratio is exactly 1.0; the 8192 rows inflate; a 100-row height is dropped as too thin.
        """
        dep = np.linspace(5.0, 20.0, 600)
        parts = []
        for h, n in ((8192.0, 600), (6656.0, 600), (3328.0, 100)):
            d = dep[:n] if n <= len(dep) else dep
            parts.append(pd.DataFrame({'pano_y': h / 2 + d / 90 * (h / 2),
                                       'pano_x': 100.0, 'pano_height': h, 'pano_width': 2 * h}))
        out = cc.add_census_columns(pd.concat(parts, ignore_index=True))
        rd = cc.resolution_dependence(out)

        assert set(rd) == {'8192', '6656'}, 'the 100-row height must be skipped'
        assert rd['6656']['crop_ratio_vs_6656_p50'] == pytest.approx(1.0)
        assert rd['8192']['crop_ratio_vs_6656_p50'] > 1.10
        assert rd['8192']['n'] == 600
        assert rd['8192']['share_pct'] == pytest.approx(100 * 600 / 1300, abs=0.01)

    def test_resolution_dependence_is_masked_by_the_clamp(self):
        """Why the committed 1.198x is a floor: at a depression where 8192 clamps and 6656 does
        not, the deployed ratio collapses to ~1 while the formula wanted ~7x."""
        dep = 27.33
        y8, y6 = 8192 / 2 + dep / 90 * 4096, 6656 / 2 + dep / 90 * 3328
        c8 = cc.predict_crop_size(np.array([y8]), np.array([8192.0]))[0]
        c6 = cc.predict_crop_size(np.array([y6]), np.array([6656.0]))[0]
        assert c8 == 1500.0 and c6 < 1500.0
        assert c8 / c6 == pytest.approx(1.006, abs=0.005)

        unclamped = lambda d, h: cc.CROP_SCALE * (
            cc.DIST_INTERCEPT_M + cc.DIST_PER_PIXEL_M * (-d * h / 180)) ** cc.CROP_EXPONENT
        assert unclamped(dep, 8192.0) / unclamped(dep, 6656.0) == pytest.approx(7.10, abs=0.05)

    def test_census_aggregation(self):
        df = pd.DataFrame({
            'pano_y': [3328.0, 3328 + 0.3 * 6656, 100.0],
            'pano_x': [100.0, 200.0, 300.0],
            'pano_height': [6656.0] * 3,
            'pano_width': [13312.0] * 3,
            'label_type': ['CurbRamp'] * 3,
        })
        out = cc.add_census_columns(df)
        s = cc.summarize_census(out)
        assert s['n'] == 3
        assert s['clamp_1500_pct'] == pytest.approx(100 / 3, abs=0.1)
        # the sky-side row (pano_y=100 -> distance ~70 m -> crop ~55) is not clamped at 50
        assert s['clamp_50_pct'] == pytest.approx(0.0)
