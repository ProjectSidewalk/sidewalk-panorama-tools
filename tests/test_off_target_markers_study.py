"""Tests for reports/scripts/off_target_markers_study.py — the off-target-markers study
(reports/2026-08-10-off-target-markers-validate.md).

Machinery: the classification cascade and the repair solver are pinned on synthetic frames where
the corruption is injected by construction — a stale heading, a stale heading+pitch, doubled
canvas offsets, a desynced zoom, a re-served pano frame — so each class is exercised against a
known right answer, and repair is checked to both reproduce the stored pano_x/pano_y and recover
the injected truth.

Findings (the report's headline numbers, asserted against the committed
reports/data/2026-08-10-off-target-markers-summary.json):
- every city's in-window >= 10 px Validate-error share strictly exceeds its post-fix share, and the
  post-fix share is <= 0.25% everywhere (Teaneck 6.79% -> 0.00%, Chicago 6.51% -> 0.22%);
- the last record miss in Teaneck, Chicago, and Seattle is the SAME day, 2024-09-25 — the 7.20.7
  deploy date;
- repair from pano_x/pano_y succeeds for 100.00% of the 19,472 in-scope rows in every city;
- SidewalkWebpage#4842's two example labels (Teaneck 14955, Chicago 30652) are both 'exact' —
  their records are self-consistent and NOT instances of the staleness bug;
- same-POV batch groups move together: >= 80% of multi-label same-POV miss groups share one dx in
  the three big cities;
- the dpr2 / zoom_desync attribution degeneracy is real: >= 95% of dpr2 rows also replay at
  zoom + 1 wherever the cohort is >= 100 rows.
"""

import glob
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

import era_replay_study as ers  # noqa: E402
import pov_replay  # noqa: E402
import rawlabels as rl  # noqa: E402
import off_target_markers_study as rss  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'fixtures', 'rawlabels_newberg_head.csv')
SUMMARY = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-10-off-target-markers-summary.json')


def _forward_frame(canvas_x, canvas_y, heading, pitch, zoom, camera_heading=100.0,
                   pano_w=16384.0, pano_h=8192.0, canvas_w=720.0, canvas_h=480.0):
    """A frame whose pano_x/pano_y are produced by the forward math from the given click inputs —
    the ground truth a corruption is then injected against.

    The canvas dims go into the projection as well as onto the frame: setting them only on the
    frame afterwards leaves the stored pixel and the replay disagreeing about the viewport, which
    is a broken fixture rather than a corruption under test.
    """
    canvas_x = np.asarray(canvas_x, float)
    pov_h, pov_p = pov_replay.pov_if_centered(canvas_x, canvas_y, heading, pitch, zoom,
                                              canvas_w, canvas_h)
    px, py = pov_replay.pano_xy_from_pov(pov_h, pov_p, np.full(canvas_x.size, camera_heading),
                                         pano_w, pano_h)
    df = pd.DataFrame({'canvas_x': canvas_x, 'canvas_y': np.asarray(canvas_y, float),
                       'heading': np.asarray(heading, float), 'pitch': np.asarray(pitch, float),
                       'zoom': np.asarray(zoom, float)})
    df['canvas_width'], df['canvas_height'] = canvas_w, canvas_h
    df['pano_x'], df['pano_y'] = px, py
    df['pano_width'], df['pano_height'] = pano_w, pano_h
    df['camera_heading'] = camera_heading
    df['label_id'] = np.arange(len(df)) + 1
    df['user_id'] = 'u1'
    df['pano_id'] = 'p1'
    df['time_created'] = pd.Timestamp('2024-01-15', tz='UTC')
    return df


def _classified(df):
    return rss.classify(ers.replay_frame(df))


def _residual_of(g, heading, pitch, zoom, canvas_x, canvas_y):
    """|dx|, |dy| for an explicit record against the frame's stored pano_x/pano_y — an independent
    re-measurement, so a solver that reports its own residual cannot mark its own homework."""
    w = np.asarray(g['pano_width'], float)
    h = np.asarray(g['pano_height'], float)
    pov_h, pov_p = pov_replay.pov_if_centered(canvas_x, canvas_y, heading, pitch, zoom,
                                              g['canvas_width'], g['canvas_height'])
    rx, ry = pov_replay.pano_xy_from_pov(pov_h, pov_p,
                                         np.asarray(g['camera_heading'], float), w, h)
    return (np.abs(ers.wrapped_dx(g['pano_x'], rx, w)),
            np.abs(np.asarray(g['pano_y'], float) - ry))


class TestCascade:

    def test_the_null_case_is_exact(self):
        """A record stored exactly as clicked classifies 'exact' on every row."""
        out = _classified(_forward_frame([100.0, 360, 500, 650], [100.0, 240, 400, 60],
                                         [10.0, 123.4, 250, 340], [-20.0, -10.5, -30, -5],
                                         [1.0, 1, 2, 3]))
        assert (out['klass'] == 'exact').all()

    def test_a_stale_heading_is_x_only(self):
        """pano_x/y computed at heading H, but heading H+17 submitted (the staged-batch bug):
        pano_y still replays, pano_x misses by exactly the shift."""
        df = _forward_frame([200.0, 500], [200.0, 300], [150.0, 150.0], [-15.0, -15.0], [1.0, 1.0])
        df['heading'] += 17.0
        out = _classified(df)
        assert (out['klass'] == 'x_only').all()
        assert out['dx_deg'].to_numpy() == pytest.approx([-17.0, -17.0], abs=0.05)

    def test_a_stale_heading_and_pitch_is_multi_field(self):
        """Both POV fields stale beyond jitter scale: multi_field."""
        df = _forward_frame([200.0], [200.0], [150.0], [-15.0], [1.0])
        df['heading'] += 20.0
        df['pitch'] += 6.0
        out = _classified(df)
        assert out['klass'].iloc[0] == 'multi_field'

    def test_doubled_center_offsets_are_dpr2(self):
        """Canvas offsets recorded doubled about the canvas center (the device-pixel cohort)."""
        df = _forward_frame([420.0, 300.0], [280.0, 190.0], [80.0, 80.0], [-20.0, -20.0],
                            [2.0, 2.0])
        df['canvas_x'] = 360.0 + (df['canvas_x'] - 360.0) * 2.0
        df['canvas_y'] = 240.0 + (df['canvas_y'] - 240.0) * 2.0
        out = _classified(df)
        assert (out['klass'] == 'dpr2').all()

    def test_a_desynced_zoom_is_explained(self):
        """pano_x/y computed at zoom 3, zoom 2 stored. The fov ladder's near-degeneracy with
        canvas-offset halving means the cascade may attribute either way; the claim under test is
        that the row is EXPLAINED (not x_only/multi_field) — and dpr2_zoom_overlap measures the
        ambiguity honestly."""
        df = _forward_frame([500.0, 220.0], [150.0, 330.0], [45.0, 45.0], [-25.0, -25.0],
                            [3.0, 3.0])
        df['zoom'] = 2.0
        out = _classified(df)
        assert out['klass'].isin(['zoom_desync', 'dpr2']).all()

    def test_a_reserved_frame_is_frame_change(self):
        """The coordinate was written against a 13312x6656 serving; the row now serves
        16384x8192. The record is fine — only the pano frame moved."""
        df = _forward_frame([420.0], [300.0], [200.0], [-18.0], [1.0],
                            pano_w=13312.0, pano_h=6656.0)
        df['pano_width'], df['pano_height'] = 16384.0, 8192.0
        out = _classified(df)
        assert out['klass'].iloc[0] == 'frame_change'

    def test_missing_camera_heading_is_unreplayable(self):
        df = _forward_frame([420.0], [300.0], [200.0], [-18.0], [1.0])
        df['camera_heading'] = np.nan
        assert _classified(df)['klass'].iloc[0] == 'unreplayable'

    def test_the_degeneracy_is_measured(self):
        """dpr2 rows also replay at zoom+1 — the overlap stat reports it."""
        df = _forward_frame([420.0, 300.0], [280.0, 190.0], [80.0, 80.0], [-20.0, -20.0],
                            [1.0, 1.0])
        df['canvas_x'] = 360.0 + (df['canvas_x'] - 360.0) * 2.0
        df['canvas_y'] = 240.0 + (df['canvas_y'] - 240.0) * 2.0
        g = _classified(df)
        g = g[g['klass'] == 'dpr2']
        if len(g):
            stat = rss.dpr2_zoom_overlap(g)
            assert stat['n_dpr2'] == len(g)
            assert stat['pct_also_matching_zoom_plus_1'] >= 0.0


class TestTheCascadeIsSymmetricInXAndY:

    def test_a_pitch_only_staleness_is_its_own_class(self):
        """The cascade had x_only but no y_only, so a record whose ONLY stale field is pitch
        replays with dx exactly 0 and landed in xy_small or multi_field — classes the report
        defines as 'both axes off' and attributes to a pan moving both axes at once."""
        df = _forward_frame([360.0], [240.0], [150.0], [-15.0], [1.0])
        df['pitch'] += 6.0
        out = _classified(df)
        assert out['klass'].iloc[0] == 'y_only'

    def test_the_x_only_mirror_still_holds(self):
        """The symmetric case, unchanged."""
        df = _forward_frame([360.0], [240.0], [150.0], [-15.0], [1.0])
        df['heading'] += 6.0
        out = _classified(df)
        assert out['klass'].iloc[0] == 'x_only'

    def test_a_pitch_only_row_is_not_reported_as_both_axes_off(self):
        """The measurement behind the finding: on the cached corpus, 348 of Seattle's 635
        in-window xy_small rows had |dx| <= 1 px. Here, exactly: dy is large and dx is zero,
        so nothing about this row belongs in a 'both axes' bucket."""
        df = _forward_frame([360.0], [240.0], [150.0], [-15.0], [1.0])
        df['pitch'] += 6.0
        out = _classified(df)
        assert abs(out['dx_deg'].iloc[0]) < 1e-6
        assert out['klass'].iloc[0] not in ('xy_small', 'multi_field')

    def test_y_only_rows_are_still_repaired(self):
        """The new class must stay in repair scope — it was being repaired before under a
        different name, and dropping it would silently shrink the deliverable."""
        assert 'y_only' in rss.REPAIR_CLASSES
        df = _forward_frame([300.0], [200.0], [150.0], [-15.0], [1.0])
        df['pitch'] += 6.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        rep, summary = rss.repair_frame(g)
        assert summary['n'] == 1
        assert summary['pct_repaired'] == 100.0


class TestScatterSampleRespectsItsCap:

    def _misses(self, n):
        return pd.DataFrame({'dx_deg': np.linspace(-5, 5, n), 'dy_deg': np.zeros(n),
                             'klass': ['x_only'] * n})

    def test_a_city_just_under_a_multiple_of_the_cap_is_still_capped(self):
        """`len // cap` gave a city with 1,499 misses step 1, so it contributed all 1,499 — twice
        the budget — while 1,500 contributed 750. The pooled scatter therefore over-weighted
        cities sitting just under each multiple of the cap."""
        pts = rss.scatter_sample([self._misses(1499)], per_city_cap=750)
        assert len(pts) <= 750

    def test_the_cap_holds_across_the_boundary(self):
        for n in (749, 750, 751, 1499, 1500, 1501, 3001):
            pts = rss.scatter_sample([self._misses(n)], per_city_cap=750)
            assert len(pts) <= 750, f'{n} misses produced {len(pts)} points'

    def test_a_small_city_is_not_decimated(self):
        pts = rss.scatter_sample([self._misses(120)], per_city_cap=750)
        assert len(pts) == 120


class TestValidatePx:

    def test_one_degree_at_zoom_1(self):
        """1 deg of residual at zoom 1 (fov 89.75) is 720/89.75 = 8.02 Validate px."""
        df = pd.DataFrame({'dx_deg': [1.0], 'dy_deg': [0.0], 'zoom': [1.0]})
        assert rss.validate_px(df)[0] == pytest.approx(8.022, abs=0.01)

    def test_zoom_scales_the_px(self):
        """The same angular miss is bigger on screen at higher zoom (narrower fov)."""
        df = pd.DataFrame({'dx_deg': [1.0, 1.0], 'dy_deg': [0.0, 0.0], 'zoom': [1.0, 3.0]})
        px = rss.validate_px(df)
        assert px[1] > 2.5 * px[0]


class TestRepair:

    def test_repair_recovers_the_click_time_heading(self):
        """Inject +17 deg of heading staleness; the solver must both reproduce the stored
        pano_x/pano_y within a pixel and land the heading back on the click-time value."""
        df = _forward_frame([200.0, 500], [200.0, 300], [150.0, 150.0], [-15.0, -15.0], [1.0, 1.0])
        df['heading'] += 17.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        rep, summary = rss.repair_frame(g)
        assert summary['pct_repaired'] == 100.0
        assert rep['new_heading'].to_numpy() == pytest.approx([150.0, 150.0], abs=0.05)
        assert (rep['new_validate_px'] <= 1.0).all()

    def test_repair_converges_on_two_stale_fields(self):
        """Heading and pitch both stale: the iterative solve still reproduces pano_x/pano_y and
        recovers both click-time values."""
        df = _forward_frame([200.0], [140.0], [150.0], [-15.0], [2.0])
        df['heading'] += 24.0
        df['pitch'] -= 8.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        rep, summary = rss.repair_frame(g)
        assert summary['pct_repaired'] == 100.0
        assert rep['new_heading'].iloc[0] == pytest.approx(150.0, abs=0.05)
        assert rep['new_pitch'].iloc[0] == pytest.approx(-15.0, abs=0.1)

    def test_dpr2_repair_halves_the_offsets(self):
        df = _forward_frame([420.0], [280.0], [80.0], [-20.0], [2.0])
        true_x, true_y = df['canvas_x'].iloc[0], df['canvas_y'].iloc[0]
        df['canvas_x'] = 360.0 + (df['canvas_x'] - 360.0) * 2.0
        df['canvas_y'] = 240.0 + (df['canvas_y'] - 240.0) * 2.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        rep, _ = rss.repair_frame(g)
        assert rep['new_canvas_x'].iloc[0] == pytest.approx(true_x, abs=0.5)
        assert rep['new_canvas_y'].iloc[0] == pytest.approx(true_y, abs=0.5)

    def test_the_returned_residual_measures_the_record_actually_returned(self, monkeypatch):
        """The loop measured at the top and updated at the bottom, so on exhaustion it applied one
        more heading/pitch step than it measured and returned the two together — a record from
        iteration k+1 with the residual from iteration k.

        The budget is patched low to make exhaustion deterministic, but exhaustion is ordinary:
        303 of 600 randomized synthetic frames hit the real 25-iteration cap, where the gap
        between the returned residual and the returned record reached 33 px.
        """
        monkeypatch.setattr(rss, 'SOLVE_ITERS', 2)
        df = _forward_frame([200.0, 500.0], [200.0, 300.0], [150.0, 150.0], [-15.0, -15.0],
                            [1.0, 1.0])
        df['heading'] += 17.0
        df['pitch'] -= 9.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        scope = g[g['klass'].isin(rss.REPAIR_CLASSES)].copy()
        rh, rp, rz, rcx, rcy, adx, ady = rss.solve_record(scope)

        fresh_x, fresh_y = _residual_of(scope, rh, rp, rz, rcx, rcy)
        assert adx == pytest.approx(fresh_x, abs=1e-9)
        assert ady == pytest.approx(fresh_y, abs=1e-9)

    def test_the_budget_is_genuinely_exhausted_in_that_test(self, monkeypatch):
        """Guards the test above: if two iterations happened to converge, it would pass on the
        buggy code too and pin nothing."""
        monkeypatch.setattr(rss, 'SOLVE_ITERS', 2)
        df = _forward_frame([200.0, 500.0], [200.0, 300.0], [150.0, 150.0], [-15.0, -15.0],
                            [1.0, 1.0])
        df['heading'] += 17.0
        df['pitch'] -= 9.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        scope = g[g['klass'].isin(rss.REPAIR_CLASSES)].copy()
        *_, adx, ady = rss.solve_record(scope)
        assert max(adx.max(), ady.max()) > rss.MATCH_TOL_PX

    def test_the_csv_columns_describe_the_csv_record(self):
        """End-to-end version of the contract, across the rounding layer: post_px_x/post_px_y must
        describe new_heading/new_pitch/new_zoom/new_canvas_*, as written.

        solve_record measured full-precision values while repair_frame rounded them into the CSV.
        That looks harmless at 1e-4 deg, but pano_xy_from_pov lands on integer pixels, so it is
        enough to move the replayed pixel by one — 3 columbus-oh rows shipped post_px_x = 0.00 for
        a record that actually replays 1 px off. Run wide enough that some row is rounding-sensitive.
        """
        n = 3000
        rng = np.random.default_rng(20260812)
        df = _forward_frame(rng.uniform(40, 680, n), rng.uniform(40, 440, n),
                            rng.uniform(0, 360, n), rng.uniform(-35, 10, n),
                            rng.choice([1.0, 2.0, 3.0], n))
        df['heading'] = (df['heading'] + rng.uniform(-20, 20, n)) % 360.0
        df['pitch'] = np.clip(df['pitch'] + rng.uniform(-8, 8, n), -90, 90)
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        rep, _ = rss.repair_frame(g)
        assert len(rep) > 10, 'not enough rows in repair scope to be meaningful'

        scope = g[g['klass'].isin(rss.REPAIR_CLASSES)]
        fresh_x, fresh_y = _residual_of(scope,
                                        rep['new_heading'].to_numpy(float),
                                        rep['new_pitch'].to_numpy(float),
                                        rep['new_zoom'].to_numpy(float),
                                        rep['new_canvas_x'].to_numpy(float),
                                        rep['new_canvas_y'].to_numpy(float))
        # post_px is stored to 1 dp, so 0.05 is the representation floor, not slack.
        assert rep['post_px_x'].to_numpy() == pytest.approx(fresh_x, abs=0.05)
        assert rep['post_px_y'].to_numpy() == pytest.approx(fresh_y, abs=0.05)

    def test_solve_record_returns_an_already_rounded_record(self):
        """The structural half of the guarantee above, and the deterministic one: if solve_record
        hands back values already at RECORD_DECIMALS, repair_frame has nothing left to round and
        the measured residual cannot drift from the written record."""
        df = _forward_frame([200.0, 500.0], [200.0, 300.0], [150.0, 150.0], [-15.0, -15.0],
                            [1.0, 1.0])
        df['heading'] += 17.0
        df['pitch'] -= 9.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        scope = g[g['klass'].isin(rss.REPAIR_CLASSES)].copy()
        rh, rp, *_ = rss.solve_record(scope)
        assert (rh == np.round(rh, rss.RECORD_DECIMALS)).all()
        assert (rp == np.round(rp, rss.RECORD_DECIMALS)).all()

    def test_the_repair_emits_storable_canvas_coordinates(self):
        """canvas_x/canvas_y are whole numbers in all 626,219 rows of the eight-city corpus, while
        the dpr2 halving emitted `.5` values. Rounding those at migration time breaks 482 of the
        19,472 shipped repairs, so the solve has to land on the storable grid itself."""
        # A click at a half-pixel doubles to a whole number, which is what production stores —
        # so halving it back is exactly how a .5 gets emitted.
        df = _forward_frame([421.5], [281.5], [80.0], [-20.0], [2.0])
        df['canvas_x'] = 360.0 + (df['canvas_x'] - 360.0) * 2.0
        df['canvas_y'] = 240.0 + (df['canvas_y'] - 240.0) * 2.0
        assert df['canvas_x'].iloc[0] % 1 == 0 and df['canvas_y'].iloc[0] % 1 == 0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        assert g['klass'].iloc[0] == 'dpr2'
        rep, summary = rss.repair_frame(g)
        assert len(rep) == 1
        assert rep['new_canvas_x'].iloc[0] % 1 == 0, rep['new_canvas_x'].iloc[0]
        assert rep['new_canvas_y'].iloc[0] % 1 == 0, rep['new_canvas_y'].iloc[0]

    def test_the_storable_record_still_reproduces_the_stored_pixel(self):
        """Landing on integers is only worth anything if heading/pitch absorb the remainder —
        they are fractional in 93% and 83% of production rows respectively, so they can."""
        df = _forward_frame([421.5], [281.5], [80.0], [-20.0], [2.0])
        df['canvas_x'] = 360.0 + (df['canvas_x'] - 360.0) * 2.0
        df['canvas_y'] = 240.0 + (df['canvas_y'] - 240.0) * 2.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        rep, summary = rss.repair_frame(g)
        assert summary['pct_repaired'] == 100.0
        assert (rep['post_px_x'] <= rss.MATCH_TOL_PX).all()
        assert (rep['post_px_y'] <= rss.MATCH_TOL_PX).all()

    def test_dpr2_halves_about_the_rows_own_canvas_centre(self):
        """The dpr2 hypothesis is device-pixel scaling about the CSS centre. Hardcoding (360, 240)
        halves a 1440x960 row about the wrong point — it either fails the test and falls through to
        x_only/multi_field, or is 'repaired' to a coordinate that never reproduces its pixel."""
        df = _forward_frame([500.0], [300.0], [80.0], [-20.0], [2.0],
                            canvas_w=1440.0, canvas_h=960.0)
        true_x, true_y = df['canvas_x'].iloc[0], df['canvas_y'].iloc[0]
        df['canvas_x'] = 720.0 + (df['canvas_x'] - 720.0) * 2.0
        df['canvas_y'] = 480.0 + (df['canvas_y'] - 480.0) * 2.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        assert g['klass'].iloc[0] == 'dpr2'
        rep, _ = rss.repair_frame(g)
        assert rep['new_canvas_x'].iloc[0] == pytest.approx(true_x, abs=1.0)
        assert rep['new_canvas_y'].iloc[0] == pytest.approx(true_y, abs=1.0)

    def test_frame_change_is_not_repaired(self):
        """frame_change records are already click-consistent; they are excluded from repair."""
        df = _forward_frame([420.0], [300.0], [200.0], [-18.0], [1.0],
                            pano_w=13312.0, pano_h=6656.0)
        df['pano_width'], df['pano_height'] = 16384.0, 8192.0
        g = _classified(df)
        g['validate_px'] = rss.validate_px(g)
        rep, summary = rss.repair_frame(g)
        assert summary == {'n': 0}
        assert len(rep) == 0


class TestOnTheFixture:

    def test_the_pipeline_runs_on_real_rows(self):
        """Five real Newberg rows load, classify, and price without error; every class label is
        from the cascade's vocabulary."""
        g = _classified(rl.load_rawlabels(FIXTURE))
        g['validate_px'] = rss.validate_px(g)
        assert set(g['klass']) <= {'exact', 'dpr2', 'frame_change', 'zoom_desync', 'x_only',
                                   'xy_small', 'multi_field', 'unreplayable'}


@pytest.fixture(scope='module')
def summary():
    with open(SUMMARY) as f:
        return json.load(f)


@pytest.mark.skipif(not os.path.exists(SUMMARY), reason='committed summary not present')
class TestFindings:
    """The report's headline numbers, pinned to the committed 2026-08-10 summary."""

    def test_the_window_boundaries(self, summary):
        assert summary['bug_window'][0].startswith('2023-03-29')
        assert summary['bug_window'][1].startswith('2024-09-26')

    def test_eight_cities(self, summary):
        assert len(summary['cities']) == 8
        assert {'teaneck-nj', 'chicago-il'} <= set(summary['cities'])

    def test_in_window_exceeds_post_fix_everywhere(self, summary):
        """The bug is the window: >= 10 px shares drop to <= 0.25% after 7.20.7 in every city
        that has post-fix rows."""
        for city, d in summary['cities'].items():
            post = d['post_fix']['visibility']
            if post.get('n', 0) < 100:
                continue
            assert post['pct_ge_10px'] <= 0.25, city
            if d['in_window']['visibility'].get('n', 0) >= 500:
                assert d['in_window']['visibility']['pct_ge_10px'] > post['pct_ge_10px'], city

    def test_the_headline_visibility_numbers(self, summary):
        t = summary['cities']['teaneck-nj']['in_window']['visibility']
        assert t['pct_ge_4px'] == pytest.approx(16.98, abs=0.01)
        assert t['pct_ge_10px'] == pytest.approx(6.79, abs=0.01)
        assert t['pct_ge_30px'] == pytest.approx(2.43, abs=0.01)
        c = summary['cities']['chicago-il']['in_window']['visibility']
        assert c['pct_ge_10px'] == pytest.approx(6.51, abs=0.01)

    def test_the_cliff_is_one_day_in_the_big_cities(self, summary):
        for city in ('teaneck-nj', 'chicago-il', 'seattle-wa'):
            assert summary['cities'][city]['in_window']['last_miss'].startswith('2024-09-25'), city

    def test_repair_succeeds_everywhere(self, summary):
        total = 0
        for city, d in summary['cities'].items():
            if d['repair'].get('n', 0):
                assert d['repair']['pct_repaired'] == 100.0, city
                total += d['repair']['n']
        assert total == 19472

    def test_the_4842_examples_are_not_the_bug(self, summary):
        for city, label_id in (('teaneck-nj', 14955), ('chicago-il', 30652)):
            case = summary['case_studies'][city]
            assert case['label_id'] == label_id
            assert case['klass'] == 'exact'
            assert case['validate_px'] == 0.0

    def test_batch_groups_move_together(self, summary):
        for city in ('teaneck-nj', 'chicago-il', 'seattle-wa'):
            g = summary['cities'][city]['in_window']['stale_x']['same_pov_groups']
            assert g['pct_groups_shared_dx'] >= 80.0, city

    def test_the_dpr2_zoom_degeneracy(self, summary):
        for city, d in summary['cities'].items():
            stat = d['in_window']['dpr2_zoom_overlap']
            if stat.get('n_dpr2', 0) >= 100:
                assert stat['pct_also_matching_zoom_plus_1'] >= 95.0, city

    def test_scatter_sample_is_plottable(self, summary):
        pts = summary['scatter_sample']
        assert len(pts) > 1000
        assert all(set(p) == {'dx', 'dy', 'k'} for p in pts[:50])


EXAMPLES = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-10-off-target-markers-examples.json')


@pytest.mark.skipif(not os.path.exists(EXAMPLES), reason='committed examples not present')
class TestImageryExamples:
    """The imagery-example gallery: every committed crop exists, spans the four miss classes, is
    visibly off (>= 15 px), and shows the repair landing on the truth marker."""

    @pytest.fixture(scope='class')
    def examples(self):
        with open(EXAMPLES) as f:
            return json.load(f)['examples']

    def test_the_gallery_is_ample_and_diverse(self, examples):
        assert len(examples) == 20
        by_class = {}
        for e in examples:
            by_class[e['klass']] = by_class.get(e['klass'], 0) + 1
        assert by_class == {'x_only': 5, 'multi_field': 5, 'dpr2': 5, 'zoom_desync': 5}
        assert all(e['old_validate_px'] >= 15 for e in examples)
        assert len({e['city'] for e in examples}) >= 7
        assert len({e['label_type'] for e in examples}) >= 4

    def test_every_crop_is_committed(self, examples):
        for e in examples:
            assert os.path.exists(os.path.join(REPO_ROOT, 'reports', e['figure'])), e['figure']

    def test_the_repair_lands_on_the_truth(self, examples):
        """The yellow square must sit inside the blue circle: repaired render == stored pano_x/y
        to within a stitched pixel."""
        for e in examples:
            dx = abs(e['repaired_xy'][0] - e['truth_xy'][0])
            dy = abs(e['repaired_xy'][1] - e['truth_xy'][1])
            assert dx <= 1.5 and dy <= 1.5, e['label_id']
