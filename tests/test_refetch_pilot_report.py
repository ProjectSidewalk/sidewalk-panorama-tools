"""Every number in reports/2026-09-05-fover-refetch-pilot.md, transcribed from a committed artifact.

Three artifacts feed the prose: the pilot reduction (outcomes and the MAE metric), the sharpness reduction
(Laplacian ratios), and the committed tile pair under tests/fixtures/tiles/, which section 5 measures
directly. The run's own stdout is committed too, because the zero-request outcomes are deliberately not
ledgered and the 1 / 3 split in section 1 exists nowhere else.

The convention this follows is the repo's: a report table is the one place a plausible number has no
compiler, so the test computes each figure from the artifact and asserts the formatted string is in the
markdown. The fixture-derived figures are recomputed from the bytes rather than read from a JSON, which
makes this file the artifact for section 5.
"""

import json
import os
import re
import statistics
import sys

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
DATA = os.path.join(REPO_ROOT, 'reports', 'data')
FIXTURES = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'tiles')
for _p in (REPO_ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import refetch_pilot as rf  # noqa: E402
import refetch_pilot_sharpness as sh  # noqa: E402

REPORT = os.path.join(REPO_ROOT, 'reports', '2026-09-05-fover-refetch-pilot.md')


@pytest.fixture(scope='module')
def report():
    with open(REPORT, encoding='utf8') as f:
        return f.read()


@pytest.fixture(scope='module')
def pilot():
    with open(os.path.join(DATA, '2026-09-05-fover-refetch-pilot.json')) as f:
        return json.load(f)


@pytest.fixture(scope='module')
def sharpness():
    with open(os.path.join(DATA, '2026-09-05-fover-refetch-pilot-sharpness.json')) as f:
        return json.load(f)


@pytest.fixture(scope='module')
def run_stdout():
    with open(os.path.join(DATA, '2026-09-05-fover-refetch-pilot-run.txt'), encoding='utf8') as f:
        return f.read()


def has(report, value, spec='.3f'):
    return format(value, spec) in report


class TestTheArtifactsAreWhatTheReportSaysTheyAre:
    def test_the_pilot_artifact_is_the_reducers_own_output(self, pilot):
        """The summary block must be recomputable from the records and outcome counts it sits beside."""
        counts = __import__('collections').Counter(pilot['summary']['outcomes'])
        assert rf.summarise(counts, pilot['records']) == pilot['summary']
        assert pilot['probed'] == '2026-09-05'

    def test_the_sharpness_artifact_is_the_scripts_own_output(self, sharpness):
        assert sh.summarise(sharpness['records']) == sharpness['summary']

    def test_both_artifacts_cover_the_same_78_panoramas(self, pilot, sharpness):
        assert {r['pano_id'] for r in pilot['records']} == {r['pano_id'] for r in sharpness['records']}
        assert len(pilot['records']) == 78

    def test_the_ledger_and_the_run_summary_agree_with_the_artifact(self, pilot, run_stdout):
        import csv
        import gzip
        with gzip.open(os.path.join(DATA, '2026-09-05-fover-refetch-pilot-ledger.csv.gz'), 'rt', newline='') as f:
            rows = [r for r in csv.reader(f) if len(r) == 2 and r[0] != 'pano_id']
        counted = __import__('collections').Counter(r[1] for r in rows)
        assert dict(counted) == pilot['summary']['outcomes']
        assert re.search(r'absent\s+1\b', run_stdout) and re.search(r'dims_changed\s+3\b', run_stdout)
        assert re.search(r'gone\s+118\b', run_stdout) and re.search(r'replaced\s+78\b', run_stdout)


class TestSection1And2:
    def test_the_draw_and_the_outcome_table(self, pilot, report, run_stdout):
        s = pilot['summary']
        assert s['panoramas_considered'] == 196 and '196' in report
        assert s['outcomes'] == {'gone': 118, 'replaced': 78}
        for n in (118, 78, 200, 188, 12):
            assert str(n) in report
        assert '20260905' in report
        # The zero-request split comes from the run's own summary, the only place it is recorded.
        assert re.search(r'absent\s+1\b', run_stdout) and '**1** `absent`' in report
        assert re.search(r'dims_changed\s+3\b', run_stdout) and '**3** `dims_changed`' in report

    def test_survival_and_cleanliness(self, pilot, report):
        s = pilot['summary']
        assert has(report, s['still_served_pct'], '.1f')           # 39.8
        assert s['clean_refetch_pct'] == 100.0 and s['undersized_pct'] == 0.0 and s['frame_grew'] == 0
        assert '47.9%' in report                                    # the photometa census figure it is set against

    def test_the_request_count_is_the_outcome_counts_at_the_documented_costs(self, pilot, report):
        """docs/ops.md's cost table: gone <= 2, replaced = 2 zoom probes + 2 frame probes + 512 tiles."""
        o = pilot['summary']['outcomes']
        requests = o['gone'] * 2 + o['replaced'] * 516
        assert '{:,}'.format(requests) in report                    # 40,484

    def test_the_rerendered_quarter(self, pilot, report):
        r = pilot['summary']['rerendered']
        assert r == {'threshold_horizon_mae': 3.0, 'n': 19, 'pct_of_measured': pytest.approx(24.36, abs=0.01)}
        assert '19 of 78' in report and has(report, r['pct_of_measured'], '.1f') and '**3.0**' in report
        # The gap the threshold sits in, from the records: nothing between the two populations.
        horizon = sorted(x['horizon']['mae_old_vs_new'] for x in pilot['records'])
        below = [v for v in horizon if v <= 3.0]
        above = [v for v in horizon if v > 3.0]
        assert len(below) == 59 and len(above) == 19
        assert has(report, max(below), '.1f') and has(report, min(above), '.1f') and has(report, max(above), '.0f')
        assert has(report, min(below), '.1f')                       # 0.5


class TestSection3TheMaeMetric:
    def test_the_headline_table(self, pilot, report):
        s = pilot['summary']
        assert has(report, s['bottom_band_mae_median']) and has(report, s['horizon_band_mae_median'])
        assert has(report, s['recovered_above_noise']['median'])
        assert s['recovered_above_noise']['n_positive'] == 25 and '25 of 78' in report
        assert has(report, s['bottom_band_halving_cost_median']) and has(report, s['horizon_band_halving_cost_median'])

    def test_the_same_rendering_core_and_the_per_frame_split(self, pilot, report):
        c = pilot['summary']['same_rendering']
        assert c['n'] == 59 and '**59**' in report
        assert has(report, c['bottom_band_mae_median']) and has(report, c['horizon_band_mae_median'])
        assert has(report, c['recovered_above_noise_median']) and c['n_positive'] == 18 and '18** of 59' in report
        f = pilot['summary']['by_frame']
        assert f['16384x8192']['n'] == 71 and has(report, f['16384x8192']['median'])
        assert f['13312x6656']['n'] == 7 and has(report, f['13312x6656']['median']) and f['13312x6656']['n_positive'] == 4

    def test_the_six_times_texture_claim_is_the_two_halving_costs(self, pilot, report):
        s = pilot['summary']
        assert 5.5 < s['horizon_band_halving_cost_median'] / s['bottom_band_halving_cost_median'] < 6.5
        assert 'six' in report


class TestSection4Sharpness:
    def test_the_ratio_table(self, sharpness, report):
        s = sharpness['summary']
        for band in ('bottom', 'horizon'):
            for key in ('ratio_p10', 'ratio_median', 'ratio_p90'):
                assert has(report, s[band][key]), (band, key)
        assert s['bottom']['n_sharper'] == 0 and '0 of 78' in report
        assert s['horizon']['n_sharper'] == 44 and '44 of 78' in report
        assert s['n_bottom_sharper_than_horizon'] == 0

    def test_the_rerendered_split_and_the_absolute_medians(self, sharpness, pilot, report):
        mae = {r['pano_id']: r['horizon']['mae_old_vs_new'] for r in pilot['records']}
        recs = sharpness['records']
        same = [r for r in recs if mae[r['pano_id']] <= rf.RERENDERED_HORIZON_MAE]
        rerendered = [r for r in recs if mae[r['pano_id']] > rf.RERENDERED_HORIZON_MAE]
        assert len(same) == 59 and len(rerendered) == 19
        assert has(report, statistics.median(r['horizon']['ratio_new_over_old'] for r in rerendered))   # 1.633
        assert has(report, statistics.median(r['bottom']['ratio_new_over_old'] for r in rerendered))    # 0.583
        assert has(report, statistics.median(r['bottom']['ratio_new_over_old'] for r in same))          # 0.595
        for band, key in (('bottom', 'lap_var_old'), ('bottom', 'lap_var_new'),
                          ('horizon', 'lap_var_old'), ('horizon', 'lap_var_new')):
            assert has(report, statistics.median(r[band][key] for r in recs), '.1f'), (band, key)
        by_frame = {}
        for r in recs:
            by_frame.setdefault('%dx%d' % (r['width'], r['height']), []).append(r['bottom']['ratio_new_over_old'])
        assert len(by_frame['16384x8192']) == 71 and has(report, statistics.median(by_frame['16384x8192']))
        assert len(by_frame['13312x6656']) == 7 and has(report, statistics.median(by_frame['13312x6656']))

    def test_the_figure_pano_is_the_median_same_rendering_one_and_the_figure_exists(self, sharpness, pilot, report):
        mae = {r['pano_id']: r['horizon']['mae_old_vs_new'] for r in pilot['records']}
        same = sorted((r for r in sharpness['records'] if mae[r['pano_id']] <= 3.0 and r['width'] == 16384),
                      key=lambda r: r['bottom']['ratio_new_over_old'])
        pick = same[len(same) // 2]
        assert pick['pano_id'] == 'CkUrdiulTbw482CMAkrKyg'
        assert has(report, pick['bottom']['ratio_new_over_old'])
        figure = os.path.join(REPO_ROOT, 'reports', 'figures', '2026-09-05-fover-refetch-pilot-CkUrdiulTbw482CMAkrKyg.png')
        assert os.path.isfile(figure) and 'figures/2026-09-05-fover-refetch-pilot-CkUrdiulTbw482CMAkrKyg.png' in report


class TestSection5TheTilePair:
    """The one section whose artifact is the committed bytes themselves; every figure is recomputed here."""

    @pytest.fixture(scope='class')
    def tiles(self):
        A = lambda im: np.asarray(im, dtype=np.float32)  # noqa: E731
        f256 = Image.open(os.path.join(FIXTURES, 'z5_fover2_4_2.jpg')).convert('L')
        g512 = A(Image.open(os.path.join(FIXTURES, 'z5_nofover_4_2.jpg')).convert('L'))
        h512 = A(Image.open(os.path.join(FIXTURES, 'z5_full_8_10.jpg')).convert('L'))
        assert f256.size == (256, 256) and g512.shape == (512, 512)
        ups = {name: A(f256.resize((512, 512), method)) for name, method in
               (('lanczos', Image.LANCZOS), ('bicubic', Image.BICUBIC), ('bilinear', Image.BILINEAR))}
        return f256, g512, h512, ups

    @staticmethod
    def halve_and_restore(a):
        im = Image.fromarray(a.astype('uint8'))
        return np.asarray(im.resize((a.shape[1] // 2, a.shape[0] // 2), Image.LANCZOS)
                          .resize((a.shape[1], a.shape[0]), Image.LANCZOS), dtype=np.float32)

    def test_the_laplacian_table(self, tiles, report):
        _f256, g512, h512, ups = tiles
        served, lanczos, bicubic, bilinear, horizon = (sh.laplacian_variance(g512), sh.laplacian_variance(ups['lanczos']),
                                                       sh.laplacian_variance(ups['bicubic']),
                                                       sh.laplacian_variance(ups['bilinear']), sh.laplacian_variance(h512))
        for v in (served, lanczos, bicubic, bilinear, horizon):
            assert has(report, v, '.1f'), v
        # The relationships the section rests on, not just the strings.
        assert bilinear < served < lanczos
        assert horizon > 10 * served

    def test_the_served_body_is_within_a_quarter_luma_of_a_bilinear_upscale(self, tiles, report):
        _f256, g512, _h512, ups = tiles
        to_bilinear = float(abs(g512 - ups['bilinear']).mean())
        to_lanczos = float(abs(g512 - ups['lanczos']).mean())
        assert has(report, to_bilinear) and has(report, to_lanczos)
        assert to_bilinear < 0.3 and to_bilinear < to_lanczos

    def test_halving_the_served_body_costs_almost_nothing(self, tiles, report):
        _f256, g512, h512, _ups = tiles
        served_cost = float(abs(g512 - self.halve_and_restore(g512)).mean())
        horizon_cost = float(abs(h512 - self.halve_and_restore(h512)).mean())
        assert has(report, served_cost) and has(report, horizon_cost)
        assert served_cost < 0.05 and horizon_cost > 10 * served_cost

    def test_downsampled_the_served_body_is_smoother_than_the_raw_256(self, tiles, report):
        f256, g512, _h512, _ups = tiles
        down = np.asarray(Image.fromarray(g512.astype('uint8')).resize((256, 256), Image.LANCZOS), dtype=np.float32)
        served_down, raw = sh.laplacian_variance(down), sh.laplacian_variance(np.asarray(f256, dtype=np.float32))
        assert has(report, served_down, '.1f') and has(report, raw, '.1f')
        assert served_down < raw


class TestTheReportsPlaceInTheRepo:
    def test_it_is_indexed_and_its_date_matches(self, report):
        with open(os.path.join(REPO_ROOT, 'reports', 'README.md'), encoding='utf8') as f:
            index = f.read()
        assert '2026-09-05-fover-refetch-pilot.md' in index
        assert report.startswith('# ') and '**2026-09-05**' in report

    def test_the_decision_is_stated_and_points_at_the_tool_docs(self, report):
        assert 'Do not run the full pass' in report
        assert '`refetch_panos.py` stays' in report
