"""Tests for reports/scripts/offaxis_covariate.py — the off-axis click-geometry covariate.

The study makes two structural claims that the pre-registration amendment then leans on, and
neither is self-evident, so both are pinned on synthetic frames where the answer is known by
construction rather than only on the committed corpus numbers:

1. **The covariate is heading-free.** This is what licenses restricting eligible rows on `exact_y`
   instead of on both replay axes. If it were false, the `x_only` staleness class (58% of the
   record misses, stale only in viewport heading) would be contaminating rather than harmless, and
   the eligible population would be wrong by 13,485 rows.
2. **The covariate is identified against the pre-registration's depression-band fixed effects.**
   `identification()` has to return ~0% for a covariate that is a pure function of band and ~100%
   for one that is orthogonal to it; a metric that cannot tell those apart would report a
   comfortable number for a covariate that estimates nothing.

The corpus findings the amendment cites are pinned separately from the committed JSON, so a
re-fetch that moves them fails CI instead of silently restating the amendment's evidence.
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

import offaxis_covariate as oc  # noqa: E402
import pov_replay  # noqa: E402

COMMITTED = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-11-offaxis-covariate.json')


@pytest.fixture(scope='module')
def pooled():
    if not os.path.exists(COMMITTED):
        pytest.skip('committed covariate JSON not present')
    with open(COMMITTED, encoding='utf-8') as f:
        return json.load(f)['pooled']


def _frame(**cols):
    """A rawlabels-shaped frame with sensible defaults for every column `prepare` touches."""
    n = len(next(iter(cols.values())))
    base = {
        'label_id': np.arange(n), 'pano_id': ['p%02d' % i for i in range(n)],
        'label_type': ['CurbRamp'] * n, 'era': ['post179'] * n,
        'canvas_x': np.full(n, 360.0), 'canvas_y': np.full(n, 240.0),
        'canvas_width': np.full(n, 720.0), 'canvas_height': np.full(n, 480.0),
        'heading': np.full(n, 0.0), 'pitch': np.full(n, -20.0), 'zoom': np.full(n, 1.0),
        'camera_heading': np.full(n, 0.0),
        'pano_width': np.full(n, 16384.0), 'pano_height': np.full(n, 8192.0),
    }
    base.update(cols)
    df = pd.DataFrame(base)
    if 'pano_x' not in df or 'pano_y' not in df:
        # Forward-project so the frame replays exactly by construction.
        pov_h, pov_p = pov_replay.pov_if_centered(
            df['canvas_x'], df['canvas_y'], df['heading'], df['pitch'], df['zoom'],
            df['canvas_width'], df['canvas_height'])
        px, py = pov_replay.pano_xy_from_pov(pov_h, pov_p, df['camera_heading'],
                                             df['pano_width'], df['pano_height'])
        df['pano_x'] = px.astype(float)
        df['pano_y'] = py.astype(float)
    return df


class TestHeadingIndependence:
    """Claim 1 — the fact the `exact_y` restriction rests on."""

    def test_vertical_offaxis_is_exactly_invariant_to_viewport_heading(self):
        headings = [0.0, 37.5, 90.0, 187.3, 298.25, 359.99]
        df = _frame(heading=np.array(headings), canvas_x=np.full(6, 451.0),
                    canvas_y=np.full(6, 142.0), pitch=np.full(6, -35.0))
        vertical, _ = oc.offaxis_offsets(df)
        assert np.allclose(vertical, vertical[0], rtol=0, atol=0), \
            'heading must cancel exactly, not approximately — the restriction depends on it'

    def test_vertical_offaxis_does_move_with_the_fields_it_reads(self):
        """Guards the guard: invariance to heading is only meaningful if the metric varies at all."""
        v0, _ = oc.offaxis_offsets(_frame(canvas_y=np.array([142.0])))
        for col, value in (('canvas_y', 300.0), ('canvas_x', 600.0), ('zoom', 3.0)):
            cols = {'canvas_y': np.array([142.0])}
            cols[col] = np.array([value])
            v1, _ = oc.offaxis_offsets(_frame(**cols))
            assert not np.isclose(v0[0], v1[0]), f'{col} must move the covariate'

    def test_on_the_canvas_centerline_it_is_exactly_the_canvas_angle(self):
        """A click on the vertical centerline is a pure rotation about the camera's horizontal
        axis, so its offset from that axis is -atan(dv/f) and the viewport aim divides out
        entirely. This is why the covariate is a canvas-frame quantity rather than a mixture of
        canvas position and where the user happened to be looking — and it is what keeps it close
        to orthogonal to the pitch-floor covariate registered alongside it.
        """
        dv = 240.0 - 142.0
        f = 0.5 * 720.0 / np.tan(np.radians(float(pov_replay.get_3d_fov(1.0))) / 2)
        expected = -np.degrees(np.arctan(dv / f))
        for pitch in (0.0, -10.0, -20.0, -35.0):
            df = _frame(canvas_x=np.array([360.0]), canvas_y=np.array([142.0]),
                        pitch=np.array([pitch]))
            vertical, _ = oc.offaxis_offsets(df)
            assert vertical[0] == pytest.approx(expected, abs=1e-9), pitch

    def test_off_the_centerline_it_couples_to_viewport_pitch(self):
        """The centerline identity is special, not general — off-axis in both axes the sphere
        geometry couples, so a test that only ever probed the centerline would miss a real
        dependence."""
        seen = set()
        for pitch in (0.0, -10.0, -20.0, -35.0):
            df = _frame(canvas_x=np.array([650.0]), canvas_y=np.array([142.0]),
                        pitch=np.array([pitch]))
            vertical, _ = oc.offaxis_offsets(df)
            seen.add(round(float(vertical[0]), 6))
        assert len(seen) == 4, 'vertical offset must vary with pitch away from the centerline'


class TestOffAxisGeometry:

    def test_click_at_canvas_centre_is_on_axis(self):
        df = _frame(canvas_x=np.array([360.0]), canvas_y=np.array([240.0]))
        vertical, radial = oc.offaxis_offsets(df)
        assert vertical[0] == pytest.approx(0.0, abs=1e-9)
        assert radial[0] == pytest.approx(0.0, abs=1e-9)

    def test_sign_convention_positive_is_below_the_viewport_centre(self):
        df = _frame(canvas_y=np.array([400.0, 80.0]))   # below centre, above centre
        vertical, _ = oc.offaxis_offsets(df)
        assert vertical[0] > 0, 'a click below the canvas centre must read positive'
        assert vertical[1] < 0

    def test_radial_separation_bounds_the_vertical_component(self):
        rng = np.random.default_rng(20260811)
        df = _frame(canvas_x=rng.uniform(0, 720, 200), canvas_y=rng.uniform(0, 480, 200),
                    pitch=rng.uniform(-35, 0, 200),
                    zoom=rng.choice([1.0, 2.0, 3.0], 200))
        vertical, radial = oc.offaxis_offsets(df)
        assert np.all(radial >= np.abs(vertical) - 1e-9)

    def test_a_horizontal_only_click_has_radial_but_no_vertical_offset(self):
        """At pitch 0 a click along the canvas midline is a pure azimuth move."""
        df = _frame(canvas_x=np.array([600.0]), canvas_y=np.array([240.0]),
                    pitch=np.array([0.0]))
        vertical, radial = oc.offaxis_offsets(df)
        assert vertical[0] == pytest.approx(0.0, abs=1e-9)
        assert radial[0] > 1.0

    def test_radial_is_never_nan_at_the_degenerate_on_axis_case(self):
        """arccos's domain is the trap here: an exactly-on-axis click makes cos_sep land on 1.0 from
        below or from 1+1e-16 above depending on the rounding of the row, and the unclipped version
        silently yields NaN for the second kind. Sweeping the whole viewer envelope at the canvas
        centre is what actually reaches it; a single hand-picked row does not.
        """
        n = 0
        for pitch in np.linspace(-35.0, 0.0, 36):
            for zoom in (1.0, 2.0, 3.0):
                df = _frame(canvas_x=np.array([360.0]), canvas_y=np.array([240.0]),
                            pitch=np.array([pitch]), zoom=np.array([zoom]))
                _, radial = oc.offaxis_offsets(df)
                assert np.isfinite(radial[0]), f'NaN radial at pitch={pitch} zoom={zoom}'
                n += 1
        assert n == 108


class TestPitchFloor:

    @pytest.mark.parametrize('pitch,expected', [
        (-35.0, True), (-34.995, True), (-35.5, True),
        (-34.0, False), (-20.0, False), (0.0, False), (float('nan'), False),
    ])
    def test_floor_detection(self, pitch, expected):
        assert bool(oc.at_pitch_floor(np.array([pitch]))[0]) is expected

    def test_nan_pitch_is_not_silently_at_the_floor(self):
        """NaN <= x is False in numpy, but relying on that is fragile — pin it."""
        assert not oc.at_pitch_floor(np.array([np.nan])).any()


class TestBands:

    @pytest.mark.parametrize('depression,band', [
        (-3.0, '<5'), (0.0, '<5'), (5.0, '<5'),         # pd.cut is right-closed
        (5.001, '5-15'), (15.0, '5-15'),
        (15.001, '15-30'), (30.0, '15-30'),
        (30.001, '>30'), (89.0, '>30'),
    ])
    def test_band_edges_match_the_prereg(self, depression, band):
        assert oc.depression_band([depression])[0] == band

    def test_out_of_range_depression_is_not_forced_into_a_band(self):
        assert pd.isna(oc.depression_band([np.nan])[0])
        assert pd.isna(oc.depression_band([120.0])[0])


class TestCanvasPixelConversion:

    def test_matches_the_gsv_fov_ladder(self):
        for zoom in (1.0, 2.0, 3.0):
            assert oc.deg_per_canvas_px(zoom) == pytest.approx(
                float(pov_replay.get_3d_fov(zoom)) / 720.0)

    def test_five_canvas_px_at_zoom_one_exceeds_the_consumer_threshold(self):
        """The size argument the amendment makes: canvas-frame errors of a few px are not small in
        the units every consumer threshold is stated in (0.5 deg, consumer-requirements survey)."""
        assert 5 * oc.deg_per_canvas_px(1.0) == pytest.approx(0.6233, abs=1e-4)
        assert 5 * oc.deg_per_canvas_px(1.0) > 0.5

    def test_the_conversion_shrinks_with_zoom(self):
        """A fixed canvas-px error is supra-threshold at zoom 1 and sub-threshold at zoom 3 — itself
        a discriminating signature, so the monotonicity has to hold."""
        assert (oc.deg_per_canvas_px(1.0) > oc.deg_per_canvas_px(2.0)
                > oc.deg_per_canvas_px(3.0))
        assert 5 * oc.deg_per_canvas_px(3.0) < 0.5


class TestIdentification:
    """Claim 2 — the metric must discriminate, or it reports comfort rather than evidence."""

    @staticmethod
    def _prepared(band, offaxis_v):
        return pd.DataFrame({'eligible': True, 'band': band, 'offaxis_v': offaxis_v,
                             'depression': np.where(np.asarray(band) == '<5', 2.0, 40.0)})

    def test_a_covariate_collinear_with_band_survives_nothing(self):
        band = np.array(['<5'] * 50 + ['>30'] * 50)
        offaxis = np.where(band == '<5', -8.0, 4.0)      # constant within band
        assert oc.identification(self._prepared(band, offaxis))['pct_surviving_band_fe'] \
            == pytest.approx(0.0, abs=1e-9)

    def test_a_covariate_orthogonal_to_band_survives_entirely(self):
        band = np.array(['<5'] * 50 + ['>30'] * 50)
        wave = np.tile(np.linspace(-10, 10, 50), 2)      # same spread inside each band
        pct = oc.identification(self._prepared(band, wave))['pct_surviving_band_fe']
        assert pct == pytest.approx(100.0, abs=1e-6)

    def test_a_partly_absorbed_covariate_lands_in_between(self):
        band = np.array(['<5'] * 50 + ['>30'] * 50)
        mixed = np.tile(np.linspace(-10, 10, 50), 2) + np.where(band == '<5', -6.0, 6.0)
        pct = oc.identification(self._prepared(band, mixed))['pct_surviving_band_fe']
        assert 30.0 < pct < 90.0

    def test_it_removes_the_band_mean_and_not_the_band_median(self):
        """A band fixed effect removes the group MEAN — that is what OLS does. Demeaning by the
        median is a different estimator and understates nothing on symmetric data, which is why
        every other test here (all symmetric by construction) cannot see the difference. Off-axis
        offset is visibly skewed in the real corpus, so the distinction is not academic.
        """
        # The two bands must differ in how far their mean sits from their median. Residual sd is
        # shift-invariant, so giving both bands the SAME skew shifts them both by the same constant
        # and the two estimators agree exactly — a fixture that looks discriminating and is not.
        band = np.array(['<5'] * 8 + ['>30'] * 8)
        skewed = np.array([0.0] * 7 + [40.0]                      # skewed: mean 5, median 0
                          + [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0])  # symmetric: both 35
        got = oc.identification(self._prepared(band, skewed))['sd_within_band_deg']

        v = pd.Series(skewed)
        by_mean = float((v - v.groupby(pd.Series(band)).transform('mean')).std(ddof=1))
        by_median = float((v - v.groupby(pd.Series(band)).transform('median')).std(ddof=1))
        assert by_mean != pytest.approx(by_median), 'fixture must discriminate the two estimators'
        assert got == pytest.approx(by_mean)

    def test_ineligible_rows_are_excluded_from_the_estimate(self):
        df = self._prepared(np.array(['<5'] * 50 + ['>30'] * 50),
                            np.tile(np.linspace(-10, 10, 50), 2))
        df.loc[:49, 'eligible'] = False
        assert oc.identification(df)['n'] == 50

    def test_degenerate_input_returns_nulls_rather_than_raising(self):
        empty = self._prepared(np.array(['<5']), np.array([1.0])).iloc[:0]
        assert oc.identification(empty)['sd_overall_deg'] is None


class TestEligibility:
    """The restriction decision itself: exact_y, not exact_x AND exact_y."""

    def test_a_heading_stale_row_stays_eligible(self):
        """The x_only class — pano_y replays, pano_x does not. Its only stale field is the viewport
        heading, which the covariate provably does not read, so dropping it would be pure loss."""
        df = _frame(canvas_y=np.array([142.0]))
        df.loc[0, 'pano_x'] = (df.loc[0, 'pano_x'] + 900) % 16384.0   # break x only
        out = oc.prepare(df)
        assert not bool(out['exact_x'].iloc[0])
        assert bool(out['exact_y'].iloc[0])
        assert bool(out['eligible'].iloc[0])

    def test_a_row_whose_vertical_record_is_stale_is_excluded(self):
        df = _frame(canvas_y=np.array([142.0]))
        df.loc[0, 'pano_y'] = df.loc[0, 'pano_y'] + 25
        out = oc.prepare(df)
        assert not bool(out['exact_y'].iloc[0])
        assert not bool(out['eligible'].iloc[0])

    def test_eligibility_counts_report_what_the_restriction_bought(self):
        df = _frame(canvas_y=np.array([142.0, 142.0, 142.0]))
        df.loc[0, 'pano_x'] = (df.loc[0, 'pano_x'] + 900) % 16384.0   # x_only: kept
        df.loc[1, 'pano_y'] = df.loc[1, 'pano_y'] + 25                # y stale: dropped
        counts = oc.eligibility(oc.prepare(df))
        assert counts['exact_y'] == 2 and counts['exact_x_and_y'] == 1
        assert counts['kept_by_using_exact_y_only'] == 1
        assert counts['eligible'] == 2

    def test_a_row_with_no_pano_dims_is_not_eligible(self):
        df = _frame(canvas_y=np.array([142.0]))
        df.loc[0, 'pano_height'] = np.nan
        assert not bool(oc.prepare(df)['eligible'].iloc[0])


class TestCommittedFindings:
    """The corpus numbers the amendment cites. A re-fetch that moves them must fail here."""

    def test_provenance_is_recorded(self, pooled):
        with open(COMMITTED, encoding='utf-8') as f:
            doc = json.load(f)
        assert doc['fetched'] == '2026-08-09'
        assert doc['restriction'].startswith('exact_y')

    def test_the_restriction_keeps_essentially_the_whole_corpus(self, pooled):
        e = pooled['eligibility']
        assert e['n_labels'] == 438410
        assert e['eligible'] == 433866
        assert 100.0 * e['eligible'] / e['n_labels'] > 98.5

    def test_requiring_both_axes_would_have_cost_13485_rows(self, pooled):
        assert pooled['eligibility']['kept_by_using_exact_y_only'] == 13485

    def test_the_covariate_is_identified_against_the_band_fixed_effects(self, pooled):
        assert pooled['identification']['pct_surviving_band_fe'] == pytest.approx(95.08, abs=0.01)
        assert abs(pooled['identification']['corr_with_depression']) < 0.4

    def test_identification_holds_in_every_era(self, pooled):
        """The study corpus spans all three eras, so a covariate identified only post-179 would
        not serve the strata the pre-registration actually fits."""
        for era, stats in pooled['by_era_identification'].items():
            assert stats['pct_surviving_band_fe'] > 90.0, era

    def test_the_pitch_floor_is_hard_and_populated(self, pooled):
        floor = pooled['floor']
        assert floor['min_pitch_deg'] == -35.0
        assert floor['at_floor_pct'] == pytest.approx(10.18, abs=0.01)

    def test_floor_exposure_concentrates_in_the_deepest_band(self, pooled):
        """Why the floor is worth registering as its own covariate: it is not spread evenly across
        the strata, it is nearly half of the band where crop sizing is already weakest."""
        by_band = pooled['floor']['by_band_pct']
        assert by_band['>30'] == pytest.approx(49.2, abs=0.1)
        assert by_band['<5'] < 1.0
        assert by_band['<5'] < by_band['5-15'] < by_band['15-30'] < by_band['>30']

    def test_the_corpus_sits_mostly_at_the_widest_fov(self, pooled):
        """Zoom 1 is where a canvas-px error converts to the largest angular error, so the corpus
        being concentrated there is what makes the mechanism worth testing at all."""
        assert pooled['zoom']['zoom1']['corpus_share_pct'] > 60.0
        assert pooled['zoom']['zoom1']['deg_at_5px'] == pytest.approx(0.6233, abs=1e-4)
