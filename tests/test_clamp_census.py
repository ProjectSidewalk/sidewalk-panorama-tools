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
    def summary(self):
        import json
        path = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-09-clamp-census.json')
        with open(path) as f:
            return json.load(f)

    def test_a_fifth_of_the_corpus_is_size_clamped(self, summary):
        assert 15.0 <= summary['overall']['clamp_1500_pct'] <= 25.0
        assert summary['overall']['clamp_50_pct'] == pytest.approx(0.0, abs=0.01)

    def test_edge_truncation_is_a_non_issue_in_production(self, summary):
        """The #77 shift machinery is safety, not a hot path: measured exposure ~0."""
        assert summary['overall']['truncated_bottom_pct'] < 0.05
        assert summary['overall']['truncated_top_pct'] < 0.05

    def test_resolution_dependence_hits_ninety_percent_of_labels(self, summary):
        rd = summary['resolution_dependence']
        h8192 = rd['8192']
        assert h8192['share_pct'] >= 80.0
        assert 1.15 <= h8192['crop_ratio_vs_6656_p50'] <= 1.25
        assert rd['6656']['crop_ratio_vs_6656_p50'] == pytest.approx(1.0)


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
