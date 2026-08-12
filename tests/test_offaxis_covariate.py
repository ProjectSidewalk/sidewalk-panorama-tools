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

A third group of tests came out of the 2026-08-11 pre-merge review (`TestDegenerateInput` onward).
Those defects share one shape worth naming, because it is not the shape a study's own tests naturally
cover: every statistic here is legitimately *undefined* for a thin group — one eligible row, or a
subgroup where the covariate or depression has no variance — and `main()` both prints those values
with a format spec and writes them with `allow_nan=False`. So the degenerate path was where the run
died, not where it returned something wrong, and it died at the very end after every city had been
computed. The tests below exercise the thin-group path deliberately rather than only the corpus.
"""

import json
import math
import os
import re
import sys
import warnings

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import era_replay_study  # noqa: E402
import offaxis_covariate as oc  # noqa: E402
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402

COMMITTED = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-11-offaxis-covariate.json')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-08-11-offaxis-covariate.md')


def _strict_load(text):
    """Parse as RFC-8259 JSON: reject the NaN/Infinity tokens Python accepts silently.

    The same check tests/test_committed_data_files.py runs over reports/data/, applied here to a
    freshly written artifact so a NaN that would ship is caught at the point it is produced.
    """
    def reject(token):
        raise ValueError(f'non-standard JSON token {token!r}')
    return json.loads(text, parse_constant=reject)


@pytest.fixture(scope='module')
def doc():
    if not os.path.exists(COMMITTED):
        pytest.skip('committed covariate JSON not present')
    with open(COMMITTED, encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def pooled(doc):
    return doc['pooled']


@pytest.fixture(scope='module')
def text():
    """The report's own markdown — compared against the artifact by TestReportMatchesTheArtifact."""
    with open(REPORT, encoding='utf-8') as f:
        return f.read()


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

    def test_five_canvas_px_at_zoom_one_exceeds_the_consumer_threshold(self):
        """The size argument the amendment makes: canvas-frame errors of a few px are not small in
        the units every consumer threshold is stated in (0.5 deg, consumer-requirements survey)."""
        assert oc.deg_for_canvas_offset(5.0, 1.0) == pytest.approx(0.7923, abs=1e-4)
        assert oc.deg_for_canvas_offset(5.0, 1.0) > 0.5

    def test_the_conversion_shrinks_with_zoom(self):
        """A fixed canvas-px error is supra-threshold at zoom 1 and sub-threshold at zoom 3 — itself
        a discriminating signature, so the monotonicity has to hold."""
        assert (oc.deg_for_canvas_offset(5.0, 1.0) > oc.deg_for_canvas_offset(5.0, 2.0)
                > oc.deg_for_canvas_offset(5.0, 3.0))
        assert oc.deg_for_canvas_offset(5.0, 3.0) < 0.5


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
        # The gnomonic angle, not the linear fov/width average this pinned before review — see
        # TestCanvasOffsetsUseTheGnomonicAngle for why, and note the old value was conservative.
        assert pooled['zoom']['zoom1']['deg_at_5px'] == pytest.approx(0.7923, abs=1e-4)

    def test_the_committed_artifact_is_strict_portable_json(self):
        """reports/data/ is checked wholesale by tests/test_committed_data_files.py; asserted here
        too because a null this study emits is the difference between a portable artifact and one
        only Python can read, and this is the file whose author would notice."""
        with open(COMMITTED, encoding='utf-8') as f:
            _strict_load(f.read())


class TestDegenerateInput:
    """Every statistic here is undefined for a thin group, and `main()` formats and serializes them
    all. Null must survive both.
    """

    @staticmethod
    def _thin(n_eligible):
        """A prepared frame with `n_eligible` identical eligible rows — zero variance on every axis,
        which is the case that makes Pearson's r undefined."""
        return oc.prepare(_frame(canvas_y=np.full(n_eligible, 142.0)))

    def test_one_row_returns_nulls_rather_than_raising(self):
        ident = oc.identification(self._thin(1))
        assert ident['n'] == 1
        assert ident['sd_overall_deg'] is None
        assert ident['pct_surviving_band_fe'] is None
        assert ident['corr_with_depression'] is None

    def test_a_zero_variance_subgroup_reports_a_null_correlation_not_a_nan(self):
        """The defect: Pearson's r is undefined when either series is constant, and pandas answers
        NaN rather than raising. `pct_surviving_band_fe` was guarded for exactly this; corr was not,
        so a handful of duplicate labels on one pano put a bare NaN in the dict.
        """
        ident = oc.identification(self._thin(2))
        assert ident['n'] == 2
        assert ident['corr_with_depression'] is None, 'undefined must be null, never NaN'
        assert ident['pct_surviving_band_fe'] is None

    def test_a_constant_covariate_against_a_varying_depression_is_also_null(self):
        """The other half of the same undefined case: only the covariate is constant."""
        df = pd.DataFrame({'eligible': True, 'band': ['<5', '>30'], 'offaxis_v': [4.0, 4.0],
                           'depression': [2.0, 40.0]})
        assert oc.identification(df)['corr_with_depression'] is None

    def test_a_varying_pair_still_reports_a_real_correlation(self):
        """Discrimination: the guard must not null out correlations that are perfectly well defined."""
        dep = np.array([2.0, 3.0, 40.0, 41.0])
        df = pd.DataFrame({'eligible': True, 'band': ['<5', '<5', '>30', '>30'],
                           'offaxis_v': 0.5 * dep - 12.0, 'depression': dep})
        assert oc.identification(df)['corr_with_depression'] == pytest.approx(1.0)

    def test_no_divide_warning_is_emitted_on_the_undefined_case(self):
        """The NaN was reachable from the existing suite and left a RuntimeWarning behind it that
        nobody read. Guarding by variance rather than by cleaning up after the fact means the warning
        is gone too — and its absence is the cheap signal that the guard runs *before* the division.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            oc.identification(self._thin(2))
        offenders = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
        assert not offenders, offenders

    def test_the_whole_analysis_dict_is_strict_json_for_a_thin_frame(self):
        """The blast radius, asserted at the level it actually mattered: `main()` ends in
        json.dump(..., allow_nan=False), so ONE undefined statistic anywhere in this dict aborts a
        multi-city run at its last line. Covers every block at once rather than key by key.
        """
        for n in (1, 2):
            out = oc.analyze(self._thin(n))
            _strict_load(json.dumps(out, allow_nan=False))

    def test_an_empty_frame_analyzes_to_nulls(self):
        empty = self._thin(1).iloc[:0]
        out = oc.analyze(empty)
        assert out['identification']['n'] == 0
        assert out['floor']['min_pitch_deg'] is None
        assert out['zoom']['n_eligible'] == 0
        assert out['zoom']['other']['min_zoom'] is None
        _strict_load(json.dumps(out, allow_nan=False))


class TestSpread:
    """`_spread` feeds the report's per-band table, so an undefined sd must not print as a number."""

    def test_a_single_row_group_reports_no_sd_rather_than_a_zero(self):
        assert oc._spread([5.0]) == {'n': 1, 'p5': 5.0, 'p50': 5.0, 'p95': 5.0, 'sd': None}

    def test_a_genuinely_flat_group_does_report_zero(self):
        """Discrimination, and the reason 0.0 was the wrong answer above: null has to mean
        "undefined", not "happens to have no spread" — otherwise the two are indistinguishable in the
        report's sd column."""
        assert oc._spread([5.0, 5.0])['sd'] == 0.0

    def test_an_empty_group_is_none(self):
        assert oc._spread([]) is None

    def test_a_one_label_band_publishes_no_sd_through_by_band(self):
        """Through the caller the report's table actually reads. A per-city run on a small deployment
        can reach n=1 in a band (oradell-nj's >30 band is already down to 306 rows)."""
        df = oc.prepare(_frame(canvas_y=np.array([142.0])))
        band = df['band'].iloc[0]
        stats = oc.by_band(df)[band]
        assert stats['n'] == 1
        assert stats['offaxis_v_deg']['sd'] is None


class TestFloorCensusByLabelType:

    def test_floor_exposure_per_type_carries_its_n(self, pooled):
        """The rate alone is not evidence: this function also runs per city, where oradell-nj 'Other'
        is 0.0% from 12 labels, printed beside a 12.07% drawn from 148,796."""
        types = pooled['floor']['by_label_type']
        assert types, 'no label types in the committed floor census'
        for name, stats in types.items():
            assert set(stats) == {'n', 'at_floor_pct'}, name
            assert stats['n'] > 0, name

    def test_the_per_type_counts_partition_the_eligible_corpus(self, pooled):
        """A partition, not a sample — so a type silently dropped by the groupby (a NaN label_type,
        an unobserved category) fails here instead of quietly shrinking the denominator."""
        types = pooled['floor']['by_label_type']
        assert sum(s['n'] for s in types.values()) == pooled['eligibility']['eligible']

    def test_the_thinnest_pooled_type_is_still_thousands_of_labels(self, pooled):
        """The review read 'Signal': 2.5316455696…% as 2 labels of 79. It is 86 of 3,397 — the same
        fraction reduced. Both numbers are now in the artifact, so the reading cannot recur."""
        signal = pooled['floor']['by_label_type']['Signal']
        assert signal['n'] == 3397
        assert signal['at_floor_pct'] == pytest.approx(100.0 * 86 / 3397)
        assert min(s['n'] for s in pooled['floor']['by_label_type'].values()) > 1000

    def test_the_old_rate_only_key_is_gone(self, pooled):
        assert 'by_label_type_pct' not in pooled['floor']

    def test_a_synthetic_frame_reports_n_beside_every_rate(self):
        """Code-level discrimination: the committed-artifact tests above pin the artifact, so they
        would still pass against a reverted `floor_census`. This one would not."""
        df = oc.prepare(_frame(canvas_y=np.full(3, 142.0),
                               label_type=['CurbRamp', 'CurbRamp', 'Signal'],
                               pitch=np.array([-35.0, -20.0, -20.0])))
        types = oc.floor_census(df)['by_label_type']
        assert types['CurbRamp'] == {'n': 2, 'at_floor_pct': 50.0}
        assert types['Signal'] == {'n': 1, 'at_floor_pct': 0.0}


class TestZoomCensus:
    """§3's table reads as a census of the corpus. It has to be one."""

    def test_the_ladder_and_the_tail_account_for_every_eligible_row(self, pooled):
        """The defect: three hardcoded rungs and no residual key, so 280 eligible rows at fractional
        zoom fell out of a table whose three shares summed to 99.94% and displayed as 100.0."""
        z = pooled['zoom']
        counted = z['zoom1']['n'] + z['zoom2']['n'] + z['zoom3']['n'] + z['other']['n']
        assert counted == z['n_eligible']
        assert z['n_eligible'] == pooled['eligibility']['eligible']

    def test_the_shares_sum_to_one_hundred_percent(self, pooled):
        z = pooled['zoom']
        total = sum(z[k]['corpus_share_pct'] for k in ('zoom1', 'zoom2', 'zoom3', 'other'))
        assert total == pytest.approx(100.0, abs=1e-9)

    def test_the_off_ladder_tail_is_fractional_zoom_between_the_stops(self, pooled):
        """What the residual turned out to be — not a fourth stop, but continuously interpolated zoom
        from some clients. Worth pinning: if it were ever outside (1, 3) the fov-scaling argument in
        §3 would need restating, since get_3d_fov changes branch at zoom 2."""
        o = pooled['zoom']['other']
        assert o['n'] == 280
        assert o['n_distinct_zooms'] == 49
        assert 1.0 < o['min_zoom'] <= o['max_zoom'] < 3.0
        assert o['fov_deg_range'] == [pytest.approx(float(pov_replay.get_3d_fov(o['max_zoom']))),
                                      pytest.approx(float(pov_replay.get_3d_fov(o['min_zoom'])))]
        assert o['fov_deg_range'][0] < o['fov_deg_range'][1], 'fov falls as zoom rises'

    def test_an_off_ladder_row_is_reported_rather_than_dropped(self):
        """Discrimination on a synthetic frame: one row per rung plus one between them."""
        df = oc.prepare(_frame(canvas_y=np.full(4, 142.0),
                               zoom=np.array([1.0, 2.0, 3.0, 1.75])))
        z = oc.zoom_conversions(df)
        assert z['n_eligible'] == 4
        assert [z[f'zoom{k}']['n'] for k in (1, 2, 3)] == [1, 1, 1]
        assert z['other']['n'] == 1
        assert z['other']['min_zoom'] == z['other']['max_zoom'] == 1.75
        assert z['other']['corpus_share_pct'] == pytest.approx(25.0)

    def test_an_all_ladder_frame_reports_an_empty_tail(self):
        """The tail must be absent-as-zero rather than absent-as-missing, or a consumer cannot tell
        "no off-ladder rows" from "this artifact predates the check"."""
        df = oc.prepare(_frame(canvas_y=np.full(2, 142.0), zoom=np.array([1.0, 3.0])))
        o = oc.zoom_conversions(df)['other']
        assert o == {'n': 0, 'corpus_share_pct': 0.0, 'n_distinct_zooms': 0,
                     'min_zoom': None, 'max_zoom': None, 'fov_deg_range': None}


class TestSpecimens:
    """§4's two labels are not in the six-city corpus, so nothing joined their values. Without these
    the report's own bar — "reproduce offline from committed bytes" — did not hold for that table."""

    def test_the_4842_records_reproduce_the_reports_offsets(self):
        s = oc.specimen_census({b: {'offaxis_v_deg': None} for b in oc.BAND_LABELS})
        assert s['teaneck-nj 14955']['offaxis_v_deg'] == pytest.approx(-15.7479, abs=1e-4)
        assert s['chicago-il 30652']['offaxis_v_deg'] == pytest.approx(-23.4711, abs=1e-4)
        assert all(v['at_pitch_floor'] for v in s.values()), 'both were clicked at the -35 floor'

    def test_tail_membership_is_computed_from_the_percentiles_it_is_given(self):
        """Code-level discrimination for the correction: the committed-artifact test below reads the
        artifact, so it would still pass if `beyond_p5_bands` were reverted to a prose-style blanket
        claim. Fed the real per-band p5 values, exactly one specimen clears all four."""
        p5 = {'<5': -23.2505, '5-15': -21.3206, '15-30': -15.9994, '>30': -3.8947}
        s = oc.specimen_census({b: {'offaxis_v_deg': {'p5': v}} for b, v in p5.items()})
        assert s['teaneck-nj 14955']['beyond_p5_bands'] == ['>30']
        assert s['chicago-il 30652']['beyond_p5_bands'] == oc.BAND_LABELS

    def test_with_no_band_percentiles_it_claims_no_tail_membership(self):
        """Guards the guard: a band table with null percentiles must yield an empty claim rather than
        a comparison against None."""
        s = oc.specimen_census({b: {'offaxis_v_deg': None} for b in oc.BAND_LABELS})
        assert all(v['beyond_p5_bands'] == [] for v in s.values())

    def test_the_committed_specimens_match_the_reports_table(self, doc):
        s = doc['specimens']
        assert s['teaneck-nj 14955']['offaxis_v_deg'] == pytest.approx(-15.75, abs=0.005)
        assert s['teaneck-nj 14955']['offaxis_r_deg'] == pytest.approx(20.30, abs=0.005)
        assert s['chicago-il 30652']['offaxis_v_deg'] == pytest.approx(-23.47, abs=0.005)
        assert s['chicago-il 30652']['offaxis_r_deg'] == pytest.approx(23.47, abs=0.005)
        assert s['teaneck-nj 14955']['record'] == {'heading': 298.25, 'pitch': -35.0, 'zoom': 1.0,
                                                   'canvas_x': 451.0, 'canvas_y': 142.0,
                                                   'canvas_width': 720.0, 'canvas_height': 480.0}

    def test_only_the_chicago_specimen_is_beyond_the_p5_of_every_band(self, doc, pooled):
        """The report claimed both were, for one revision, while its own §4 prose had it right. The
        membership is now computed from the committed band percentiles instead of asserted in prose.
        """
        s = doc['specimens']
        assert s['chicago-il 30652']['beyond_p5_bands'] == oc.BAND_LABELS
        assert s['teaneck-nj 14955']['beyond_p5_bands'] == ['>30']

        p5 = {b: pooled['by_band'][b]['offaxis_v_deg']['p5'] for b in oc.BAND_LABELS}
        v = s['teaneck-nj 14955']['offaxis_v_deg']
        assert v > p5['15-30'] and v > p5['5-15'] and v > p5['<5'], \
            'teaneck sits INSIDE p5 for the three shallower bands — that is the whole correction'
        assert v < p5['>30']


class TestOneProjectionOneCanvas:
    """The covariate and the eligibility flag must come from the same projection call, and the canvas
    frame must have one definition. Both were true only by hand-synchronisation."""

    @staticmethod
    def _count_projections(monkeypatch):
        """Install a counting wrapper around the projection. Build fixtures BEFORE calling this:
        `_frame` forward-projects to make its rows replay exactly, so it projects once itself."""
        calls = []
        real = pov_replay.pov_if_centered
        monkeypatch.setattr(pov_replay, 'pov_if_centered',
                            lambda *a, **k: (calls.append(1), real(*a, **k))[1])
        return calls

    def test_prepare_projects_each_row_exactly_once(self, monkeypatch):
        """`offaxis_offsets` re-ran the full gnomonic projection over the same rows that
        `replay_frame` had just projected — 438,410 rows twice, with eligibility read off one call and
        the covariate off the other. It now reads replay_frame's pov columns."""
        df = _frame(canvas_y=np.array([142.0, 200.0]))
        calls = self._count_projections(monkeypatch)
        oc.prepare(df)
        assert len(calls) == 1, f'prepare projected {len(calls)} times'

    def test_a_bare_frame_still_projects_once(self, monkeypatch):
        """Discrimination for the test above: the saving is the column reuse, not a lost call. A frame
        with no pov columns must still get a projection, or the fallback is broken."""
        df = _frame(canvas_y=np.array([142.0]))
        calls = self._count_projections(monkeypatch)
        oc.offaxis_offsets(df)
        assert len(calls) == 1

    def test_the_column_path_and_the_bare_frame_path_agree_exactly(self):
        """Both branches must be the same projection, to the bit — otherwise reintroducing the
        duplicate has simply moved where the two implementations can drift."""
        bare = _frame(canvas_x=np.array([451.0, 600.0, 360.0]),
                      canvas_y=np.array([142.0, 400.0, 80.0]),
                      pitch=np.array([-35.0, -10.0, 0.0]),
                      zoom=np.array([1.0, 2.0, 3.0]))
        replayed = era_replay_study.replay_frame(bare)
        v_bare, r_bare = oc.offaxis_offsets(bare)
        v_col, r_col = oc.offaxis_offsets(replayed)
        assert np.array_equal(v_bare, v_col)
        assert np.array_equal(r_bare, r_col)

    def test_replay_frame_publishes_the_pov_it_used(self):
        out = era_replay_study.replay_frame(_frame(canvas_y=np.array([142.0])))
        assert {'pov_heading', 'pov_pitch'} <= set(out.columns)
        h, p = era_replay_study.frame_pov(_frame(canvas_y=np.array([142.0])))
        assert out['pov_pitch'].iloc[0] == p[0] and out['pov_heading'].iloc[0] == h[0]

    def test_this_module_declares_no_second_canvas_constant(self):
        """The local `CANVAS_W, CANVAS_H = 720.0, 480.0` copy meant eligibility (via replay_frame,
        which used pov_replay's) and the covariate (via this module's) could be computed against two
        different canvases, with no test able to see it: every fixture supplies canvas dims
        explicitly, so neither fallback was ever exercised."""
        assert not hasattr(oc, 'CANVAS_W')
        assert not hasattr(oc, 'CANVAS_H')

    def test_the_conversion_default_follows_pov_replays_canvas(self, monkeypatch):
        monkeypatch.setattr(pov_replay, 'CANVAS_W', 1440.0)
        assert oc.deg_for_canvas_offset(5.0, 1.0) == pytest.approx(
            oc.deg_for_canvas_offset(5.0, 1.0, canvas_width=1440.0))

    def test_the_bare_frame_fallback_follows_pov_replays_canvas(self, monkeypatch):
        bare = _frame(canvas_y=np.array([142.0])).drop(columns=['canvas_width', 'canvas_height'])
        before, _ = oc.offaxis_offsets(bare)
        monkeypatch.setattr(pov_replay, 'CANVAS_W', 1440.0)
        monkeypatch.setattr(pov_replay, 'CANVAS_H', 960.0)
        after, _ = oc.offaxis_offsets(bare)
        assert not np.isclose(before[0], after[0]), \
            'a local copy of the canvas constant would not follow pov_replay'


class TestNoDuplicateEraKey:

    def test_the_analysis_does_not_ship_the_post179_result_twice(self):
        out = oc.analyze(oc.prepare(_frame(canvas_y=np.full(2, 142.0))))
        assert 'identification_post179' not in out
        assert out['by_era_identification']['post179']['n'] == 2

    def test_the_committed_artifact_carries_one_post179_row(self, doc, pooled):
        """It was byte-identical to by_era_identification['post179'] — same rows, same predicate —
        and read as a distinct quantity in the report's §1 table."""
        assert 'identification_post179' not in pooled
        assert pooled['by_era_identification']['post179']['n'] == 89837
        for city in doc['cities'].values():
            assert 'identification_post179' not in city


class TestUnreachableGuardsAreGoneButTheirEffectIsPinned:
    """`eligible` carried two `isfinite` terms that `exact_y` already implied. Removing a term with no
    test is how the next reader re-adds it; what is pinned instead is the observable consequence."""

    def test_a_nan_pitch_row_is_ineligible(self):
        df = _frame(canvas_y=np.array([142.0]))
        df.loc[0, 'pitch'] = np.nan
        assert not bool(oc.prepare(df)['eligible'].iloc[0])

    def test_a_nan_pitch_cannot_reach_a_finite_pov_which_is_why_exact_y_suffices(self):
        """The argument itself, measured: a non-finite pitch propagates through cos(p0) into all of
        x/y/z, so pov_pitch is non-finite and exact_y is already false. That is why an
        isfinite(offaxis_v) term could never change the mask — the same reasoning the study used to
        delete the isfinite(depression) term beside it."""
        df = _frame(canvas_y=np.array([142.0]))
        df.loc[0, 'pitch'] = np.nan
        out = oc.prepare(df)
        assert not np.isfinite(out['pov_pitch'].iloc[0])
        assert not bool(out['replayable_y'].iloc[0])
        assert not bool(out['exact_y'].iloc[0])
        assert not np.isfinite(oc.offaxis_offsets(out)[0][0])

    def test_a_nan_pano_height_row_is_ineligible(self):
        """The other deleted term's consequence: no finite depression, hence no band."""
        df = _frame(canvas_y=np.array([142.0]))
        df.loc[0, 'pano_height'] = np.nan
        out = oc.prepare(df)
        assert pd.isna(out['depression'].iloc[0])
        assert not bool(out['eligible'].iloc[0])

    def test_the_band_guard_is_load_bearing_and_stays(self):
        """Discrimination: the band term is NOT redundant, unlike the two isfinite terms removed
        beside it — a row can replay exactly and still have no band, and only this guard drops it.

        The reachable boundary: a click at the pano's top row gives `pano_y == 0`, hence a depression
        of exactly −90°, and BAND_EDGES' leftmost edge is open (pd.cut is right-closed), so the band
        is NaN. The record still replays exactly, because the frame is forward-projected.
        """
        out = oc.prepare(_frame(canvas_x=np.array([360.0]), canvas_y=np.array([240.0]),
                                pitch=np.array([90.0])))
        assert out['pano_y'].iloc[0] == 0
        assert out['depression'].iloc[0] == -90.0
        assert pd.isna(out['band'].iloc[0])
        assert bool(out['exact_y'].iloc[0]), 'exact_y holds — so exact_y alone would let this in'
        assert not bool(out['eligible'].iloc[0])


class TestDocstringFigures:
    """This module's docstrings state the corpus figures a maintainer decides from — which of two
    restrictions to use, whether a canvas-pixel error matters. Prose goes stale silently, so the
    numbers in it are checked against the committed artifact like any other claim."""

    def test_the_rejected_restrictions_cost_is_a_share_of_eligible_rows(self, pooled):
        m = re.search(r'discard ([\d.]+)% of the eligible rows', oc.__doc__)
        assert m, 'the module docstring must state what requiring both axes would cost'
        e = pooled['eligibility']
        assert float(m.group(1)) == pytest.approx(
            100.0 * e['kept_by_using_exact_y_only'] / e['eligible'], abs=0.05)

    def test_that_cost_is_not_the_record_miss_share_restated(self, pooled):
        """The defect: the docstring said 58%, which is the x_only share of record *misses* — correct
        two lines above, and 19x the actual cost. It made the study's central methodological choice
        look far more expensive to reverse than it is, and would have misdirected the
        post-migration sensitivity re-run."""
        cost = float(re.search(r'discard ([\d.]+)% of the eligible rows', oc.__doc__).group(1))
        assert cost < 10.0, 'a share of eligible rows, not a share of the misses'

    def test_the_conversion_docstring_cites_the_committed_zoom1_share(self, pooled):
        """It cited "70% of post-fix labels" — the population the study's own Wrong Turns section
        records abandoning, with a number no committed artifact can verify."""
        m = re.search(r'At zoom 1 -- ([\d.]+)% of (\w+) labels', oc.deg_for_canvas_offset.__doc__)
        assert m, 'the conversion docstring must say what share of the corpus sits at zoom 1'
        assert m.group(2) == 'eligible', 'the study population, not the abandoned post-fix probe'
        assert float(m.group(1)) == pytest.approx(
            pooled['zoom']['zoom1']['corpus_share_pct'], abs=0.05)


class TestReportMatchesTheArtifact:
    """The report's tables, checked cell by cell against the committed artifact.

    This is the class the study most needed and did not have. Every number in the write-up was typed
    from a run's stdout, and two of them drifted: §3's zoom table displayed three shares that summed
    to 100.0 while the corpus had a fourth group, and the Summary generalised a tail-membership claim
    that §4's own prose stated correctly for one specimen only. Both are the same failure — prose and
    artifact are maintained separately and nothing compared them — and the crop-priors pre-merge
    review found an instance of it in this report family a day earlier.
    """

    @staticmethod
    def _cell(s):
        """A markdown table cell as a float. The report uses U+2212 MINUS SIGN, not hyphen-minus."""
        return float(s.replace('−', '-').replace(',', '').replace('°', '').strip())

    def test_the_band_table_matches_the_artifact(self, text, pooled):
        """§2 is where `_spread`'s sd column surfaces, so this is also what would catch a one-row band
        publishing a 0.0 as if it were measured."""
        rows = re.findall(
            r'^\| (<5|5–15|15–30|>30)° \| ([\d,]+) \| (\S+) / (\S+) / (\S+) \| ([\d.]+) '
            r'\| ([\d.]+)° \| \*{0,2}([\d.]+)%\*{0,2} \|$', text, re.M)
        assert len(rows) == len(oc.BAND_LABELS), f'§2 must have one row per band; got {rows}'
        # The report writes the band names with an en dash; BAND_LABELS uses a hyphen.
        for label, n, p5, p50, p95, sd, r95, floor in rows:
            band = pooled['by_band'][label.replace('–', '-')]
            spread = band['offaxis_v_deg']
            assert self._cell(n) == band['n'], label
            assert self._cell(p5) == pytest.approx(spread['p5'], abs=0.05), label
            assert self._cell(p50) == pytest.approx(spread['p50'], abs=0.05), label
            assert self._cell(p95) == pytest.approx(spread['p95'], abs=0.05), label
            assert self._cell(sd) == pytest.approx(spread['sd'], abs=0.005), label
            assert self._cell(r95) == pytest.approx(band['offaxis_r_p95_deg'], abs=0.05), label
            assert self._cell(floor) == pytest.approx(band['at_floor_pct'], abs=0.05), label

    def test_the_zoom_table_accounts_for_every_eligible_row(self, text, pooled):
        rows = re.findall(r'^\| (1|2|3|\*off-ladder\*) \| [^|]+ \| ([\d,]+) \| ([\d.]+)% \|',
                          text, re.M)
        assert len(rows) == 4, f'§3 should have three rungs plus the off-ladder row, found {rows}'
        key = {'1': 'zoom1', '2': 'zoom2', '3': 'zoom3', '*off-ladder*': 'other'}
        total = 0
        for label, n, share in rows:
            z = pooled['zoom'][key[label]]
            assert self._cell(n) == z['n'], label
            assert self._cell(share) == pytest.approx(z['corpus_share_pct'], abs=0.001), label
            total += int(self._cell(n))
        assert total == pooled['eligibility']['eligible'], \
            'the table reads as a census, so its counts must be one'

    def test_the_specimen_table_matches_the_artifact(self, text, doc):
        v = re.search(r'^\| vertical off-axis \| \*\*(\S+?)\*\* \(above centre\) '
                      r'\| \*\*(\S+?)\*\* \(above centre\) \|$', text, re.M)
        r = re.search(r'^\| radial off-axis \| (\S+?) \| (\S+?) \|$', text, re.M)
        assert v and r, '§4 must carry the vertical and radial rows'
        for i, name in enumerate(['teaneck-nj 14955', 'chicago-il 30652']):
            s = doc['specimens'][name]
            assert self._cell(v.group(i + 1)) == pytest.approx(s['offaxis_v_deg'], abs=0.005), name
            assert self._cell(r.group(i + 1)) == pytest.approx(s['offaxis_r_deg'], abs=0.005), name

    def test_the_specimen_p5_row_names_the_one_specimen_that_clears_every_band(self, text, doc):
        m = re.search(r'^\| beyond the p5 of band \| (.+?) \| (.+?) \|$', text, re.M)
        assert m, '§4 must state each specimen\'s tail membership, not generalise it in prose'
        clears_all = [n for n, s in doc['specimens'].items()
                      if s['beyond_p5_bands'] == oc.BAND_LABELS]
        assert clears_all == ['chicago-il 30652'], clears_all
        assert '>30' in m.group(1) and 'only' in m.group(1), 'teaneck is beyond p5 in >30 alone'
        assert 'all four' in m.group(2)

    def test_the_summary_quotes_the_specimen_offsets_it_has_data_for(self, text, doc):
        for s in doc['specimens'].values():
            assert f"{abs(s['offaxis_v_deg']):.2f}°" in text

    def test_the_identification_table_carries_each_era_once(self, text, pooled):
        rows = re.findall(r'^\| (pooled|legacy|mid|post-179) \(n = ([\d,]+)\) \|', text, re.M)
        labels = [r[0] for r in rows]
        assert labels == ['pooled', 'legacy', 'mid', 'post-179'], \
            f'§1 must list pooled then the three eras in order, once each; got {labels}'
        counts = dict(rows)
        assert self._cell(counts['pooled']) == pooled['identification']['n']
        for era, label in (('legacy', 'legacy'), ('mid', 'mid'), ('post179', 'post-179')):
            assert self._cell(counts[label]) == pooled['by_era_identification'][era]['n'], era

    def test_the_headline_identification_figures_match(self, text, pooled):
        ident = pooled['identification']
        assert f"{ident['pct_surviving_band_fe']:.2f}%" in text
        assert f"{ident['sd_overall_deg']:.2f}°".replace('-', '−') in text
        assert f"{ident['corr_with_depression']:.3f}" in text

    def test_the_floor_figures_match(self, text, pooled):
        floor = pooled['floor']
        assert f"{floor['at_floor_pct']:.2f}%" in text
        assert f"{floor['by_band_pct']['>30']:.1f}%" in text


class TestMain:
    """The CLI end to end. Everything above tests a function; these are the two ways the run itself
    died — a thin city, and an input directory that matched nothing."""

    @staticmethod
    def _city_csv(tmp_path, name, df, when='2025-01-01'):
        """Write a prepared-shaped frame back out as a rawLabels CSV the real loader will read."""
        out = df.copy()
        out['time_created'] = int(pd.Timestamp(when, tz='UTC').value // 10 ** 6)
        out['user_id'] = [f'u{i}' for i in range(len(out))]
        for col in rawlabels.STUDY_COLUMNS:
            if col not in out:
                out[col] = np.nan
        path = tmp_path / f'{name}.csv'
        out[rawlabels.STUDY_COLUMNS].to_csv(path, index=False)
        return path

    def test_a_city_with_one_eligible_row_completes_and_reports_nulls(self, tmp_path, capsys):
        """The crash: `identification()` returns None for fewer than two eligible rows — its own
        contract — and main() format-specced those Nones. One thin city aborted the run with a
        TypeError after every other city had been computed and before --write was reached.
        """
        self._city_csv(tmp_path, 'thincity', _frame(canvas_y=np.array([142.0])))
        artifact = tmp_path / 'out.json'
        assert oc.main([str(tmp_path), '--fetched', '2026-08-11', '--write', str(artifact)]) == 0

        printed = capsys.readouterr().out
        assert 'n/a' in printed, 'an undefined statistic must print as n/a, not crash the run'
        doc = _strict_load(artifact.read_text(encoding='utf-8'))
        assert doc['cities']['thincity']['identification']['sd_overall_deg'] is None
        assert doc['pooled']['identification']['n'] == 1

    def test_a_zero_variance_city_still_writes_a_strict_artifact(self, tmp_path):
        """The other abort, at the opposite end of the run: two identical rows make Pearson's r
        undefined, and a bare NaN fails json.dump(allow_nan=False) on main()'s last line."""
        self._city_csv(tmp_path, 'flatcity', _frame(canvas_y=np.full(2, 142.0)))
        artifact = tmp_path / 'out.json'
        assert oc.main([str(tmp_path), '--fetched', '2026-08-11', '--write', str(artifact)]) == 0
        doc = _strict_load(artifact.read_text(encoding='utf-8'))
        assert doc['pooled']['identification']['corr_with_depression'] is None

    def test_a_healthy_city_still_reports_real_numbers(self, tmp_path, capsys):
        """Discrimination for both tests above: n/a must be reserved for the undefined case."""
        rng = np.random.default_rng(20260811)
        n = 60
        self._city_csv(tmp_path, 'realcity',
                       _frame(canvas_x=rng.uniform(50, 670, n), canvas_y=rng.uniform(40, 440, n),
                              pitch=rng.uniform(-35, 0, n), zoom=rng.choice([1.0, 2.0, 3.0], n)))
        artifact = tmp_path / 'out.json'
        assert oc.main([str(tmp_path), '--fetched', '2026-08-11', '--write', str(artifact)]) == 0
        assert 'n/a' not in capsys.readouterr().out
        doc = _strict_load(artifact.read_text(encoding='utf-8'))
        ident = doc['pooled']['identification']
        assert ident['n'] == n
        assert ident['sd_overall_deg'] > 0
        assert ident['corr_with_depression'] is not None
        assert doc['specimens']['chicago-il 30652']['beyond_p5_bands']

    def test_an_empty_csv_dir_names_the_directory(self, tmp_path, capsys):
        """It reached `pd.concat([])` and died as "No objects to concatenate", which names neither the
        directory nor the fact that the input was empty. Easy to hit: the report's reproduce block
        abbreviates the cache path in a comment and spells it in full in the command below it."""
        with pytest.raises(SystemExit) as exc:
            oc.main([str(tmp_path), '--fetched', '2026-08-11'])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert str(tmp_path) in err
        assert 'No objects to concatenate' not in err

    def test_a_dir_of_non_csv_files_is_treated_as_empty(self, tmp_path):
        (tmp_path / 'seattle-wa.csv.gz').write_bytes(b'not a csv')
        (tmp_path / 'notes.txt').write_text('hi', encoding='utf-8')
        with pytest.raises(SystemExit):
            oc.main([str(tmp_path), '--fetched', '2026-08-11'])


class TestCanvasOffsetsUseTheGnomonicAngle:
    """The viewer is a gnomonic (rectilinear) projection, so a canvas offset does NOT subtend
    fov/width degrees per pixel — that linear average understates the true angle at the canvas
    centre by 27% at zoom 1, which is 64.4% of the eligible corpus.

    Direction matters for how this reads: the linear figure was *conservative*. Every consumer
    argument in the report is 'this error already exceeds the threshold', so a larger true angle
    strengthens it. The reason to fix it anyway is that the rest of this module computes angles
    exactly via pov_if_centered, and a study is not entitled to two different projections.
    """

    def _exact(self, px, zoom, canvas_width=720.0):
        fov = float(pov_replay.get_3d_fov(zoom))
        f = (canvas_width / 2.0) / math.tan(math.radians(fov / 2.0))
        return math.degrees(math.atan(px / f))

    def test_five_pixels_at_zoom_one(self):
        assert oc.deg_for_canvas_offset(5.0, 1.0) == pytest.approx(0.7923, abs=1e-4)

    def test_twenty_pixels_at_zoom_one(self):
        assert oc.deg_for_canvas_offset(20.0, 1.0) == pytest.approx(3.1660, abs=1e-4)

    def test_it_matches_an_independent_gnomonic_computation_on_every_rung(self):
        for zoom in (1.0, 2.0, 3.0):
            for px in (1.0, 5.0, 20.0, 100.0):
                assert oc.deg_for_canvas_offset(px, zoom) == pytest.approx(
                    self._exact(px, zoom), abs=1e-9), (zoom, px)

    def test_it_is_strictly_larger_than_the_linear_average(self):
        """The defect, stated as a property: the linear rate is a chord, the truth is an arc."""
        for zoom in (1.0, 2.0, 3.0):
            linear = 5.0 * float(pov_replay.get_3d_fov(zoom)) / 720.0
            assert oc.deg_for_canvas_offset(5.0, zoom) > linear, zoom

    def test_it_is_not_linear_in_the_offset(self):
        """20 px subtends strictly less than 4x what 5 px does — the whole reason a single
        'degrees per pixel' rate cannot be right."""
        assert oc.deg_for_canvas_offset(20.0, 1.0) < 4.0 * oc.deg_for_canvas_offset(5.0, 1.0)

    def test_the_consumer_threshold_argument_still_holds(self):
        """Zoom 1 clears the 0.5 deg placement threshold at 5 px, zoom 3 does not — the report's
        actual claim, now on the exact angle."""
        assert oc.deg_for_canvas_offset(5.0, 1.0) > 0.5
        assert oc.deg_for_canvas_offset(5.0, 3.0) < 0.5


class TestTheZoomTableMatchesTheReport:
    """The published conversion table, cell by cell against the committed artifact — the class of
    check that caught two hand-typed counts in the Mapillary census."""

    @pytest.fixture(scope='class')
    def report_text(self):
        path = os.path.join(REPO_ROOT, 'reports', '2026-08-11-offaxis-covariate.md')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_every_rung_row_matches(self, pooled, report_text):
        for zoom in (1, 2, 3):
            z = pooled['zoom'][f'zoom{zoom}']
            assert f"{z['n']:,}" in report_text, zoom
            assert f"{z['corpus_share_pct']:.3f}%" in report_text, zoom
            assert f"{z['deg_at_5px']:.3f}°" in report_text, (zoom, z['deg_at_5px'])
            assert f"{z['deg_at_20px']:.3f}°" in report_text, (zoom, z['deg_at_20px'])

    def test_the_linear_figures_are_gone_from_the_prose_table(self, report_text):
        """0.623 and 2.493 were the linear-average values; they may only appear in the paragraph
        that explains the correction, never as a table cell."""
        assert '| **0.623°** |' not in report_text
        assert '| 2.493° |' not in report_text


class TestSpecimenCanvasIsRecordedNotAssumed:
    """The specimens are hand-transcribed because neither label is in the six-city corpus. Their
    canvas dims are part of that record: the covariate scales with the frame, so taking the
    720x480 fallback made an unverified default load-bearing for a published correction."""

    def test_every_specimen_states_its_canvas(self):
        for name, rec in oc.SPECIMENS.items():
            assert 'canvas_width' in rec and 'canvas_height' in rec, name
            assert rec['canvas_width'] > 0 and rec['canvas_height'] > 0, name

    def test_the_covariate_moves_with_the_canvas(self):
        """Why it has to be recorded: same click, different frame, materially different answer.
        At DPR-2 teaneck's -15.75 deg becomes -25.58 deg, past the <5 band's p5 of -23.25."""
        rec = dict(oc.SPECIMENS['teaneck-nj 14955'])
        base = oc.specimen_offaxis(rec)
        dpr2 = oc.specimen_offaxis({**rec, 'canvas_width': 1440.0, 'canvas_height': 960.0})
        assert base == pytest.approx(-15.75, abs=0.02)
        assert dpr2 == pytest.approx(-25.58, abs=0.02)

    def test_the_recorded_canvas_is_what_the_report_publishes(self, pooled):
        """Guards against 'fixed' by changing the number instead of the record."""
        assert oc.specimen_offaxis(oc.SPECIMENS['teaneck-nj 14955']) == pytest.approx(
            -15.75, abs=0.02)
        assert oc.specimen_offaxis(oc.SPECIMENS['chicago-il 30652']) == pytest.approx(
            -23.47, abs=0.02)
