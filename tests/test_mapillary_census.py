"""Tests for reports/scripts/mapillary_census.py — does the GSV-derived study machinery transfer?

Two layers, as elsewhere in reports/: the machinery on synthetic frames where the answer is known by
construction, and the committed corpus findings pinned so a re-fetch that moves them fails CI rather
than silently restating the report.

The finding the whole census turns on is `replay`: stored pano_x/pano_y reproduce **exactly** from the
stored canvas/POV record on Mapillary imagery. That single measurement settles two of the three
concerns that made Mapillary look out of reach, because an exact replay is only possible if the front
end ran this same projection with this same fov ladder — fov sets the focal length, so a different fov
model could not land on the same integer pixel. So `exact_y` is a meaningful eligibility rule on
Mapillary, and `get_3d_fov`'s zoom ladder applies there.

What does NOT transfer is left explicit: no depth (photometa is GSV-only), and the gravity-alignment
assumption behind `depression_from_pano_y` is weaker on a crowd-sourced rig — RampNet measures
flat-ground geometry against metric depth at Spearman 0.95 on GSV vs 0.81 on this same Richmond
imagery. Neither is testable here; both are stated in the report.
"""

import itertools
import json
import re
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

import mapillary_census as mc  # noqa: E402
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402

COMMITTED = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-11-mapillary-census.json')
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'fixtures', 'rawlabels_richmond_head.csv')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-08-11-mapillary-census.md')


@pytest.fixture(scope='module')
def doc():
    if not os.path.exists(COMMITTED):
        pytest.skip('committed Mapillary census not present')
    with open(COMMITTED, encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def pooled(doc):
    return doc['pooled']


@pytest.fixture(scope='module')
def fixture_frame():
    return rawlabels.load_rawlabels(FIXTURE)


@pytest.fixture(scope='module')
def text():
    """The report's markdown, compared against the committed artifact by the last class."""
    if not os.path.exists(REPORT):
        pytest.skip('report not present')
    with open(REPORT, encoding='utf-8') as f:
        return f.read()


def _frame(**cols):
    """A rawlabels-shaped frame with defaults for everything the census touches, forward-projected so
    it replays exactly by construction."""
    n = len(next(iter(cols.values())))
    base = {
        'label_id': np.arange(n), 'user_id': ['u1'] * n,
        'pano_id': [f'{100000000000000 + i}' for i in range(n)],
        'label_type': ['CurbRamp'] * n, 'tags': ['[]'] * n,
        'time_created': pd.date_range('2026-08-11', periods=n, freq='min', tz='UTC'),
        'canvas_x': np.full(n, 360.0), 'canvas_y': np.full(n, 240.0),
        'canvas_width': np.full(n, 720.0), 'canvas_height': np.full(n, 480.0),
        'heading': np.full(n, 0.0), 'pitch': np.full(n, -20.0), 'zoom': np.full(n, 1.0),
        'camera_heading': np.full(n, 0.0), 'camera_pitch': np.full(n, 1.0),
        'camera_roll': np.full(n, 0.5),
        'pano_width': np.full(n, 11000.0), 'pano_height': np.full(n, 5500.0),
        'latitude': np.full(n, 37.54), 'longitude': np.full(n, -77.44),
        'agree_count': np.ones(n), 'disagree_count': np.zeros(n),
    }
    base.update(cols)
    df = pd.DataFrame(base)
    if 'pano_x' not in df:
        pov_h, pov_p = pov_replay.pov_if_centered(
            df['canvas_x'], df['canvas_y'], df['heading'], df['pitch'], df['zoom'],
            df['canvas_width'], df['canvas_height'])
        px, py = pov_replay.pano_xy_from_pov(pov_h, pov_p, df['camera_heading'],
                                            df['pano_width'], df['pano_height'])
        df['pano_x'] = px.astype(float)
        df['pano_y'] = py.astype(float)
    return df


class TestImagerySource:
    """No `source` column exists on rawLabels, so id shape is the only discriminator a desk study has."""

    def test_it_separates_the_three_id_shapes(self):
        df = pd.DataFrame({'pano_id': [
            '511129198087695',                      # Mapillary: all-numeric
            'hXlPoi3-dwfgmXBWL-yJlw',               # GSV: 22-char base64
            'CAoSLEFGMVFpcE1UVjdTcVhqeEI3VnV4ZFFxcHQwLTdPa3llLW9CT01uV0NVX0lJ',  # photosphere
        ]})
        assert mc.imagery_source(df)['by_source'] == {
            'mapillary': 1, 'gsv': 1, 'gsv_photosphere': 1}

    def test_the_fixture_is_entirely_mapillary(self, fixture_frame):
        got = mc.imagery_source(fixture_frame)
        assert got['by_source'] == {'mapillary': len(fixture_frame)}

    def test_the_committed_corpus_is_entirely_mapillary(self, pooled):
        """The premise of the whole census. If a GSV pano appeared here the tilt contrast would be
        mixing rigs."""
        assert pooled['imagery_source']['by_source'] == {'mapillary': 267}


class TestReplay:
    """The central measurement."""

    def test_the_fixture_replays_exactly_on_both_axes(self, fixture_frame):
        got = mc.replay(fixture_frame)
        n = len(fixture_frame)
        assert got['exact_x'] == n and got['exact_y'] == n
        assert got['max_abs_dx_px'] == 0.0 and got['max_abs_dy_px'] == 0.0

    def test_the_committed_corpus_replays_exactly(self, pooled):
        r = pooled['replay']
        assert r['n_labels'] == 267
        assert r['exact_x'] == r['exact_y'] == 267
        assert r['exact_x_pct'] == 100.0 and r['exact_y_pct'] == 100.0
        assert r['max_abs_dy_px'] == 0.0

    def test_a_perturbed_record_stops_replaying(self, fixture_frame):
        """Discrimination: 100% exact must be a property of the data, not of a replay that always
        agrees. Move one stored pixel and the rate has to drop."""
        broken = fixture_frame.copy()
        broken.loc[broken.index[0], 'pano_y'] = broken['pano_y'].iloc[0] + 25
        got = mc.replay(broken)
        assert got['exact_y'] == len(broken) - 1
        assert got['max_abs_dy_px'] == 25.0

    def test_the_canvas_is_the_gsv_viewer_s_and_zoom_sits_on_its_ladder(self, pooled):
        """Why the fov ladder transfers: the same 720x480 canvas, and zoom concentrated on the same
        three stops. An exact replay could not happen under a different fov model.

        Three of 267 labels carry a *fractional* zoom strictly between the stops — the same off-ladder
        tail the off-axis study found in the GSV corpus (280 of 433,866), from clients that
        interpolated zoom continuously. `get_3d_fov` is continuous, so those rows still have a
        well-defined fov; asserting `<= {1,2,3}` would be wrong on both corpora.
        """
        z = pooled['replay']['zoom_values']
        assert list(pooled['replay']['canvas_frames']) == ['720x480']
        assert {'1', '2', '3'} <= set(z)
        assert sum(z[k] for k in ('1', '2', '3')) / sum(z.values()) > 0.98
        assert all(1.0 <= float(k) <= 3.0 for k in z), 'nothing outside the ladder\'s span'

    @pytest.mark.parametrize('key', ['canvas_frames', 'zoom_values', 'pano_frames'])
    def test_every_histogram_accounts_for_every_label(self, pooled, key):
        """The reconciliation that caught a real defect in this script: formatting float keys into a
        dict comprehension silently dropped 3 of 267 labels, because `2.999999999999998` and `3.0`
        format to the same string and the later entry replaced the earlier."""
        assert sum(pooled['replay'][key].values()) == pooled['replay']['n_labels']

    def test_the_histogram_helper_sums_colliding_keys(self):
        """Discrimination at the source, on the exact values that collided."""
        assert mc._histogram(f'{z:g}' for z in
                             pd.Series([3.0, 2.999999999999998, 1.0]).round(4)) == {'1': 1, '3': 2}

    def test_float_noise_in_zoom_is_bucketed_onto_the_ladder(self):
        """Code-level, because the committed-artifact reconciliation above was generated *by* the fixed
        code and would stay green against a revert. Richmond really carries `2.999999999999998`, which
        must land on rung 3 and must not vanish."""
        df = _frame(zoom=np.array([3.0, 2.999999999999998, 1.9924999999999995, 1.992499999999999]))
        got = mc.replay(df)['zoom_values']
        assert got == {'1.9925': 2, '3': 2}
        assert sum(got.values()) == 4

    def test_zoom_keys_use_the_off_axis_study_s_4dp_convention(self):
        """`%g` alone would already collapse the float noise above (6 significant digits), so the
        `.round(4)` is not what makes that test pass — it is what makes these keys comparable with
        `offaxis_covariate.zoom_conversions`, which also rounds to 4 dp. Pinned on a value where the two
        formattings disagree: 1.23456789 is '1.2346' rounded and '1.23457' raw.
        """
        got = mc.replay(_frame(zoom=np.array([1.23456789])))['zoom_values']
        assert list(got) == ['1.2346'], 'must match the off-axis study, not raw %g'

    def test_pano_frames_vary_which_they_do_not_on_gsv(self, pooled):
        """Mapillary panos arrive at assorted sizes, so #77's dims preflight matters more here."""
        assert len(pooled['replay']['pano_frames']) >= 3


class TestTiltDesign:

    def test_se_scales_as_sigma_over_sd_root_n(self):
        """The formula §5 uses, pinned rather than trusted: SE = sigma_resid / (sd * sqrt(n))."""
        df = _frame(camera_pitch=np.array([2.0, -2.0, 3.0, -3.0]),
                    camera_roll=np.array([1.0, -1.0, 2.0, -2.0]),
                    canvas_x=np.array([100.0, 300.0, 500.0, 700.0]))
        got = mc.tilt(df, sigma_resid_deg=0.59)
        expected = 0.59 / (got['sd_pitch_term_deg'] * np.sqrt(4))
        assert got['se_beta_pitch'] == pytest.approx(expected)

    def test_more_regressor_spread_tightens_the_se(self):
        """Discrimination, and the mechanism the report leans on: Mapillary wins on SE because its
        tilt varies more, not because it has more labels."""
        narrow = _frame(camera_roll=np.array([0.1, -0.1, 0.1, -0.1]),
                        canvas_x=np.array([100.0, 300.0, 500.0, 700.0]))
        wide = _frame(camera_roll=np.array([6.0, -6.0, 6.0, -6.0]),
                      canvas_x=np.array([100.0, 300.0, 500.0, 700.0]))
        assert mc.tilt(wide)['se_beta_roll'] < mc.tilt(narrow)['se_beta_roll']

    def test_delta_bearing_uses_stored_pixels_only(self):
        """§1's standing constraint: no camera_heading term. Δb must be unchanged by it."""
        df = _frame(canvas_x=np.array([100.0, 600.0]))
        moved = df.copy()
        moved['camera_heading'] = moved['camera_heading'] + 90.0
        assert list(mc.delta_bearing(df)) == list(mc.delta_bearing(moved))

    def test_delta_bearing_spans_the_full_circle(self):
        df = _frame(pano_x=np.array([0.0, 5500.0, 11000.0]), pano_y=np.full(3, 2750.0))
        assert list(np.round(mc.delta_bearing(df), 6)) == [-180.0, 0.0, 180.0]

    def test_a_single_row_reports_nulls_rather_than_raising(self):
        got = mc.tilt(_frame(camera_pitch=np.array([1.0])))
        assert got['n_labels'] == 1
        assert got['sd_pitch_term_deg'] is None
        assert got['se_beta_pitch'] is None
        assert got['decision_rule_reachable'] is False
        json.dumps(got, allow_nan=False)

    def test_the_reachability_verdict_follows_the_se(self):
        """Code-level, both directions. The committed artifact says True, so a test reading only the
        artifact would stay green if the comparison were inverted — and the n=1 null case short-circuits
        before the comparison, so it cannot see the inversion either."""
        wide = _frame(camera_pitch=np.array([6.0, -6.0, 5.0, -5.0]),
                      camera_roll=np.array([6.0, -6.0, 5.0, -5.0]),
                      canvas_x=np.array([100.0, 300.0, 500.0, 700.0]))
        got = mc.tilt(wide)
        assert max(got['se_beta_pitch'], got['se_beta_roll']) < got['se_required']
        assert got['decision_rule_reachable'] is True

        narrow = _frame(camera_pitch=np.array([0.02, -0.02, 0.01, -0.01]),
                        camera_roll=np.array([0.02, -0.02, 0.01, -0.01]),
                        canvas_x=np.array([100.0, 300.0, 500.0, 700.0]))
        got = mc.tilt(narrow)
        assert max(got['se_beta_pitch'], got['se_beta_roll']) > got['se_required']
        assert got['decision_rule_reachable'] is False

    def test_committed_camera_roll_is_fully_available(self, pooled, doc):
        """The asymmetry that makes a Mapillary stratum worth having, both halves measured."""
        assert pooled['tilt']['camera_roll_available_pct'] == 100.0
        assert pooled['tilt']['camera_pitch_available_pct'] == 100.0
        assert doc['gsv_contrast'], 'the GSV contrast must be recorded, not quoted'
        for city, stats in doc['gsv_contrast'].items():
            assert stats['camera_roll_available_pct'] == 0.0, city

    def test_committed_tilt_spread_exceeds_the_gsv_prior(self, pooled):
        """The photometa census's GSV prior is |pitch| p90 2.6 deg, |roll| p90 2.2 deg. Richmond's rig
        is materially more tilted, which is what tightens SE."""
        assert pooled['tilt']['camera_pitch_deg']['abs_p90'] > 2.6 * 2
        assert pooled['tilt']['camera_roll_deg']['abs_p90'] > 2.2 * 2
        assert pooled['tilt']['sd_pitch_term_deg'] > 1.20, 'GSV photometa census value'
        assert pooled['tilt']['sd_roll_term_deg'] > 1.00

    def test_the_decision_rule_is_already_reachable(self, pooled):
        """The counter-intuitive result: endpoint 2 does not need more Richmond labels. It clears the
        §2.2 band at the achieved n with margin, because the regressors vary so much."""
        t = pooled['tilt']
        assert t['decision_rule_reachable'] is True
        assert max(t['se_beta_pitch'], t['se_beta_roll']) < t['se_required'] / 5


class TestWithinPanoStratum:

    def test_it_counts_panos_not_labels(self):
        """§2.3's gate is a pano count. Three separated labels on one pano is one pano."""
        df = _frame(pano_x=np.array([0.0, 3700.0, 7400.0]), pano_y=np.full(3, 2750.0))
        df['pano_id'] = 'shared'
        got = mc.within_pano_stratum(df)
        assert got['n_panos'] == 1 and got['n_panos_multi_label'] == 1
        assert got['n_panos_separated'] == 1

    def test_a_pano_whose_labels_share_a_bearing_does_not_count(self):
        """Discrimination: co-located labels give the pano fixed effect nothing to work with."""
        df = _frame(pano_x=np.array([5500.0, 5510.0]), pano_y=np.full(2, 2750.0))
        df['pano_id'] = 'shared'
        assert mc.within_pano_stratum(df)['n_panos_separated'] == 0

    def test_the_separation_gate_is_the_documented_60_deg(self):
        assert mc.BEARING_SEPARATION_DEG == 60.0
        assert mc.WITHIN_PANO_PANOS_REQUIRED == 60

    def test_single_label_panos_are_excluded(self):
        df = _frame(canvas_x=np.array([100.0, 600.0]))    # distinct pano ids by default
        got = mc.within_pano_stratum(df)
        assert got['n_panos'] == 2 and got['n_panos_multi_label'] == 0

    def test_committed_progress_and_shortfall(self, pooled):
        w = pooled['within_pano_stratum']
        assert w['n_panos_separated'] == 43
        assert w['required'] == 60
        assert w['estimable'] is False
        assert w['shortfall_panos'] == 17
        assert w['n_panos_separated'] + w['shortfall_panos'] == w['required']


class TestMultiPerspective:

    def test_labels_on_one_spot_collapse_to_one_object(self):
        df = _frame(latitude=np.array([37.5400, 37.54001, 37.54002]),
                    longitude=np.full(3, -77.44))
        got = mc.multi_perspective(df)
        assert got['n_labels'] == 3 and got['n_objects'] == 1
        assert got['n_objects_multi_pano'] == 1

    def test_labels_far_apart_stay_separate_objects(self):
        """Discrimination: 8 m must not swallow the street. 0.0005 deg of latitude is ~55 m — chosen so
        that inflating the radius to anything street-scale merges them and fails here."""
        df = _frame(latitude=np.array([37.5400, 37.5405]), longitude=np.full(2, -77.44))
        assert mc.multi_perspective(df)['n_objects'] == 2

    def test_the_radius_is_object_scale_not_street_scale(self):
        """Pins the constant itself: one curb ramp is metres across, and a radius that reached tens of
        metres would report a corner's four ramps as one object and halve the effective n it warns
        about."""
        assert 2.0 <= mc.OBJECT_RADIUS_M <= 15.0

    def test_it_reports_which_objects_both_users_labelled(self):
        df = _frame(latitude=np.full(2, 37.54), longitude=np.full(2, -77.44),
                    user_id=['u1', 'u2'])
        got = mc.multi_perspective(df)
        assert got['n_objects'] == 1 and got['n_objects_both_users'] == 1

    def test_an_absent_label_type_is_reported_as_empty(self):
        assert mc.multi_perspective(_frame(canvas_x=np.array([1.0])),
                                    label_type='Nonexistent')['n_objects'] == 0

    def test_committed_object_structure(self, pooled):
        """The reason effective n for endpoint 1 is nearer the object count than the label count."""
        m = pooled['multi_perspective']
        assert m['n_labels'] == 101 and m['n_objects'] == 39
        assert m['n_objects_multi_pano'] == 25
        assert sum(int(k) * v for k, v in m['panos_per_object'].items()) >= m['n_objects']


class TestCrossedBlock:

    def test_matched_recovers_more_pairs_than_clustered(self, pooled):
        """The tooling finding: clustering needs both clicks inside a radius, matching only needs them
        on the same pano and type. 2 pairs versus 7 on the same data."""
        c = pooled['crossed_block']
        assert c['clustered']['n_pairs'] == 2
        assert c['matched']['n_pairs'] == 6
        assert c['matched']['n_pairs'] > c['clustered']['n_pairs']

    def test_the_block_is_far_short_of_a_usable_sigma(self, pooled):
        """Sharing a route is not sharing panos: two labellers with ~40 comparable panos each overlapped
        on 7 once Crosswalk and the region tags came out. A sigma needs ~150 pairs."""
        c = pooled['crossed_block']
        assert c['n_users'] == 2
        assert c['n_panos_shared_by_two_users'] == 7
        assert min(c['panos_per_user'].values()) >= 35, 'each labelled plenty — they just diverged'
        assert c['matched']['n_pairs'] < 20

    def test_the_referent_rule_is_applied_before_pairing(self, pooled):
        c = pooled['crossed_block']
        assert c['n_dropped_unlocated_referent'] == 86


class TestReferentExclusion:

    @staticmethod
    def _mixed():
        """Two region-tagged SurfaceProblems, one Occlusion, one ordinary CurbRamp."""
        df = _frame(label_type=['SurfaceProblem', 'SurfaceProblem', 'Occlusion', 'CurbRamp'],
                    tags=['[brick/cobblestone]', '[bumpy,brick/cobblestone]', '[]', '[steep]'])
        return df

    def test_the_arms_are_counted_separately_on_a_synthetic_frame(self):
        """Code-level: the committed-artifact test below reads counts the fixed code produced, so it
        would stay green if one arm were reported as the total."""
        got = mc.referent_exclusion(self._mixed())
        assert got['n_labels'] == 4
        assert got['n_comparable'] == 1
        assert got['n_excluded'] == 3
        assert got['excluded_no_referent_type'] == 1
        assert got['excluded_region_tag'] == 2

    def test_the_crossed_block_applies_the_rule_before_pairing(self):
        """Code-level for the same reason. Two labellers on one region-tagged SurfaceProblem must yield
        no pair: a brick sidewalk has no particular spot, so their separation is not placement noise."""
        df = _frame(label_type=['SurfaceProblem'] * 2 + ['CurbRamp'] * 2,
                    tags=['[brick/cobblestone]'] * 2 + ['[]'] * 2,
                    user_id=['u1', 'u2', 'u1', 'u2'],
                    canvas_x=np.array([360.0, 362.0, 360.0, 362.0]))
        df['pano_id'] = 'shared'
        got = mc.crossed_block(df)
        assert got['n_dropped_unlocated_referent'] == 2
        assert got['matched']['n_pairs'] == 1, 'only the CurbRamp pair is comparable'

    def test_the_two_arms_are_counted_separately(self, pooled):
        r = pooled['referent_exclusion']
        assert r['n_labels'] == 267
        assert r['n_comparable'] == 181
        assert r['n_excluded'] == 86
        assert r['excluded_no_referent_type'] == 65, '62 Crosswalk + 3 Occlusion'
        assert r['excluded_region_tag'] == 21
        assert r['excluded_no_referent_type'] + r['excluded_region_tag'] == r['n_excluded']
        assert r['n_comparable'] + r['n_excluded'] == r['n_labels']

    def test_the_rule_is_recorded_in_the_artifact(self, pooled):
        """So a consumer reading the JSON alone knows which rule produced these counts."""
        assert pooled['referent_exclusion']['rule'] == {
            'no_referent_types': ['Crosswalk', 'NoSidewalk', 'Occlusion'],
            'region_tags': ['SurfaceProblem+brick/cobblestone']}

    def test_adding_no_sidewalk_moved_no_richmond_number(self, pooled):
        """Why the counts above are unchanged by a rule that grew an arm: Richmond has no NoSidewalk
        labels. The arm is live and consequential — 82,769 labels in the six GSV cities — but it cannot
        be exercised by this corpus, so its behaviour is pinned synthetically in
        tests/test_rawlabels_mapillary.py instead."""
        assert 'NoSidewalk' not in pooled['labels_by_type']
        assert pooled['referent_exclusion']['excluded_no_referent_type'] == 65, '62 Crosswalk + 3 Occlusion'


class TestGsvReferentExclusion:
    """The rule is derived on 267 Richmond labels and applied to 438,410 GSV ones, and its arms are
    sized completely differently in the two: NoSidewalk dominates the GSV corpus and does not occur in
    Richmond at all. That asymmetry is why the GSV column is computed and committed rather than
    reasoned about — the first draft of the report's table carried two hand-typed arm counts that were
    wrong by 2x and 6x, and no surrounding prose looked any different for it."""

    @staticmethod
    def _per_city():
        """Two synthetic cities whose arms are disjoint by construction, shaped like `gsv_contrast`.

        City A carries the Richmond-shaped arms, city B the GSV-shaped one, so pooling has to take a
        union over types rather than assuming both cities carry the same keys.
        """
        a = _frame(label_type=['Crosswalk', 'Occlusion', 'SurfaceProblem', 'CurbRamp'],
                   tags=['[]', '[]', '[brick/cobblestone]', '[]'])
        b = _frame(label_type=['NoSidewalk', 'NoSidewalk', 'Crosswalk', 'NoCurbRamp'],
                   tags=['[]'] * 4)
        return {city: {'referent_exclusion': mc.referent_exclusion(df)}
                for city, df in (('a', a), ('b', b))}

    def test_excluded_by_type_splits_the_arm(self):
        got = mc.referent_exclusion(_frame(
            label_type=['Crosswalk', 'Crosswalk', 'NoSidewalk', 'Occlusion', 'CurbRamp'],
            tags=['[]'] * 5))
        assert got['excluded_by_type'] == {'Crosswalk': 2, 'NoSidewalk': 1, 'Occlusion': 1}
        assert sum(got['excluded_by_type'].values()) == got['excluded_no_referent_type'] == 4

    def test_absent_types_are_omitted_not_reported_as_zero(self):
        """A zero would read as "measured none here"; omission reads as "this corpus has none". The
        distinction matters because Richmond genuinely has no NoSidewalk labels."""
        got = mc.referent_exclusion(_frame(label_type=['Crosswalk', 'CurbRamp'], tags=['[]'] * 2))
        assert got['excluded_by_type'] == {'Crosswalk': 1}

    def test_pooling_sums_the_arms_across_cities(self):
        got = mc.pool_referent_exclusion(self._per_city())
        assert got['n_labels'] == 8
        assert got['excluded_by_type'] == {'Crosswalk': 2, 'NoSidewalk': 2, 'Occlusion': 1}
        assert got['excluded_region_tag'] == 1
        assert got['n_excluded'] == 6
        assert got['n_comparable'] == 2, 'the CurbRamp and the NoCurbRamp'

    def test_pooling_takes_a_union_over_types(self):
        """City A has no NoSidewalk and city B no Occlusion. A dict comprehension over one city's keys
        would silently drop the other's arm — which is exactly the shape of the corpus this runs on."""
        per_city = self._per_city()
        assert 'NoSidewalk' not in per_city['a']['referent_exclusion']['excluded_by_type']
        assert 'Occlusion' not in per_city['b']['referent_exclusion']['excluded_by_type']
        assert set(mc.pool_referent_exclusion(per_city)['excluded_by_type']) == \
            {'Crosswalk', 'NoSidewalk', 'Occlusion'}

    def test_the_reconciliation_assertion_actually_fires(self):
        """Guard-the-guard: the three asserts in `pool_referent_exclusion` are the only thing standing
        between a mis-summed column and a published table, so prove one of them can fail."""
        tampered = self._per_city()
        tampered['a']['referent_exclusion']['n_comparable'] += 1
        with pytest.raises(AssertionError):
            mc.pool_referent_exclusion(tampered)

    def test_the_arm_reconciliation_actually_fires(self):
        """The third assert, guarded separately: type-arm plus tag-arm must equal the total. Without
        its own tampered case the synthetic corpus satisfies it either way, so dropping the assert
        survived a first mutation battery."""
        tampered = self._per_city()
        tampered['a']['referent_exclusion']['excluded_region_tag'] += 1
        with pytest.raises(AssertionError):
            mc.pool_referent_exclusion(tampered)

    def test_a_type_arm_that_stops_matching_is_caught(self):
        """The by-type sum is checked against the independently-computed `isin` count, so an
        excluded_by_type that drifted out of step with NO_REFERENT_TYPES cannot pass."""
        tampered = self._per_city()
        tampered['b']['referent_exclusion']['excluded_by_type']['NoSidewalk'] = 1
        with pytest.raises(AssertionError):
            mc.pool_referent_exclusion(tampered)

    def test_gsv_contrast_measures_the_whole_city(self, tmp_path):
        """Code-level, and the reason it exists: `test_committed_gsv_column` below reads numbers the
        current code wrote, so a `gsv_contrast` that quietly measured a subset of each city would keep
        it green until the artifact was regenerated. Run the function and check its counts against a
        frame of known composition instead."""
        import shutil
        shutil.copy(FIXTURE, tmp_path / 'somecity.csv')
        got = mc.gsv_contrast(str(tmp_path))['somecity']
        assert got['n_labels'] == 10
        assert got['referent_exclusion']['n_labels'] == 10, 'measured over the whole city, not a head'
        assert got['referent_exclusion']['n_excluded'] == 5
        assert got['referent_exclusion']['excluded_by_type'] == {'Crosswalk': 2, 'Occlusion': 1}

    def test_every_city_referent_count_covers_its_whole_label_count(self, doc):
        """The same check applied to the committed artifact: a per-city referent block that counted
        fewer labels than the city has is the signature of a subset bug that regenerated cleanly."""
        for city, row in doc['gsv_contrast'].items():
            assert row['referent_exclusion']['n_labels'] == row['n_labels'], city
            assert sum(row['labels_by_type'].values()) == row['n_labels'], city

    def test_committed_gsv_column(self, doc):
        g = doc['gsv_referent_exclusion']
        assert g['n_labels'] == 438410
        assert g['n_comparable'] == 336995
        assert g['n_excluded'] == 101415
        assert g['excluded_by_type'] == {'Crosswalk': 14697, 'NoSidewalk': 82769, 'Occlusion': 2656}
        assert g['excluded_region_tag'] == 1293

    def test_no_sidewalk_dominates_the_corpus_the_rule_will_be_applied_to(self, doc, pooled):
        """The finding that made this worth computing: the arm Richmond cannot see is the largest one,
        at 5.6x the Crosswalk arm it was reasoned from."""
        g = doc['gsv_referent_exclusion']['excluded_by_type']
        assert g['NoSidewalk'] > g['Crosswalk'] * 5
        assert g['NoSidewalk'] > max(pooled['labels_by_type'].values()) * 100
        assert 'NoSidewalk' not in pooled['referent_exclusion']['excluded_by_type']

    def test_every_gsv_city_carries_the_pitch_roll_asymmetry(self, doc):
        """Unchanged by this addition, and re-pinned because gsv_contrast grew two keys."""
        for city, row in doc['gsv_contrast'].items():
            assert row['camera_roll_available_pct'] == 0.0, city
            assert set(row) == {'n_labels', 'camera_pitch_available_pct', 'camera_roll_available_pct',
                                'referent_exclusion', 'labels_by_type'}, city


class TestGeometry:

    def test_it_counts_only_eligible_rows(self):
        """Code-level, and the committed corpus cannot test this at all: every Richmond label replays
        exactly, so filtering to `eligible` is a no-op there and a revert would stay green. Break one
        stored pixel and the ineligible row must drop out."""
        df = _frame(canvas_y=np.array([200.0, 200.0, 200.0]))
        assert mc.geometry(df)['n_eligible'] == 3
        broken = df.copy()
        broken.loc[broken.index[0], 'pano_y'] = broken['pano_y'].iloc[0] + 30
        assert mc.geometry(broken)['n_eligible'] == 2

    def test_every_committed_label_is_eligible(self, pooled):
        """A consequence of the 100% exact replay: exact_y excludes nothing here."""
        g = pooled['geometry']
        assert g['n_eligible'] == 267
        assert sum(g['by_band'].values()) == 267

    def test_no_label_sits_above_the_horizon(self, pooled):
        """RampNet finds 5 above-horizon GT ramps per 1,066 Mapillary ramps (0.5%) and 0 per 994 GSV.
        267 labels is too few to see a 0.5% rate, so 0 here is consistent with that, not evidence
        against it — pinned so the report cannot overclaim."""
        assert pooled['geometry']['n_above_horizon'] == 0

    def test_the_tail_bands_are_the_thin_ones(self, pooled):
        bands = pooled['geometry']['by_band']
        assert bands['<5'] == 8 and bands['>30'] == 8
        assert bands['5-15'] > 100 and bands['15-30'] > 100


class TestMain:

    def test_an_empty_dir_names_the_directory(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            mc.main([str(tmp_path), '--fetched', '2026-08-11'])
        assert exc.value.code == 2
        assert str(tmp_path) in capsys.readouterr().err

    def test_it_runs_end_to_end_on_the_fixture(self, tmp_path):
        import shutil
        shutil.copy(FIXTURE, tmp_path / 'richmond.csv')
        artifact = tmp_path / 'out.json'
        assert mc.main([str(tmp_path), '--fetched', '2026-08-11', '--write', str(artifact)]) == 0
        doc = json.loads(artifact.read_text(encoding='utf-8'))
        assert doc['pooled']['replay']['exact_y'] == 10
        assert 'gsv_contrast' not in doc, '--gsv-dir was not passed'

    def test_the_artifact_is_strict_json_on_a_one_label_city(self, tmp_path):
        """Every statistic is null or degenerate at n=1, and the write uses allow_nan=False."""
        import shutil
        shutil.copy(FIXTURE, tmp_path / 'richmond.csv')
        one = pd.read_csv(tmp_path / 'richmond.csv', dtype=str).head(1)
        one.to_csv(tmp_path / 'richmond.csv', index=False)
        artifact = tmp_path / 'out.json'
        assert mc.main([str(tmp_path), '--fetched', '2026-08-11', '--write', str(artifact)]) == 0
        text = artifact.read_text(encoding='utf-8')
        assert 'NaN' not in text
        json.loads(text)


class TestReportMatchesTheArtifact:
    """The report/data mismatch class this repo has now hit twice. Every headline number the write-up
    quotes is checked against the committed JSON."""

    def test_the_headline_counts_appear(self, text, pooled):
        for value in (pooled['replay']['n_labels'],
                      pooled['tilt']['n_panos'],
                      pooled['within_pano_stratum']['n_panos_separated'],
                      pooled['multi_perspective']['n_objects'],
                      pooled['referent_exclusion']['n_excluded']):
            assert str(value) in text, value

    def test_the_se_figures_match(self, text, pooled):
        t = pooled['tilt']
        assert f"{t['se_beta_pitch']:.3f}" in text
        assert f"{t['se_beta_roll']:.3f}" in text

    def test_the_pair_shortfall_is_stated(self, text, pooled):
        c = pooled['crossed_block']
        assert str(c['matched']['n_pairs']) in text
        assert str(c['n_panos_shared_by_two_users']) in text

    def test_the_gsv_referent_column_is_transcribed_not_invented(self, text, doc):
        """The one class of error this file exists to catch, applied to the table that already had it:
        every GSV arm count in §6 must appear in the prose exactly as the artifact computed it. The
        report writes thousands separators, so compare in that form."""
        g = doc['gsv_referent_exclusion']
        for value in (*g['excluded_by_type'].values(), g['excluded_region_tag'],
                      g['n_excluded'], g['n_comparable'], g['n_labels']):
            assert f'{value:,}' in text, value


class TestBlankPanoGeometryDoesNotKillTheCensus:
    """Rows whose pano metadata never resolved carry NaN geometry — rawlabels preserves that
    deliberately, because a crashed lookup must not read as pixel 0. The six GSV cities carry 84
    (cdmx), 106 (newberg), 109 (columbus) and 1,761 (seattle) such rows today, so any Mapillary
    deployment that ever serves one reaches these paths. Each defect below fires after the whole
    census has been computed and before anything is written.
    """

    def test_replay_counts_a_row_with_blank_pano_dims_instead_of_raising(self, fixture_frame):
        """`int(w)` over the raw column raises on NaN — the exact rows replay_frame's
        replayable_x/replayable_y masks exist to tolerate."""
        df = fixture_frame.copy()
        df.loc[df.index[0], 'pano_width'] = np.nan
        out = mc.replay(df)
        assert out['n_labels'] == len(df)

    def test_the_frame_histogram_names_the_unresolved_rows(self, fixture_frame):
        df = fixture_frame.copy()
        df.loc[df.index[0], 'pano_width'] = np.nan
        out = mc.replay(df)
        assert sum(out['pano_frames'].values()) == len(df), \
            'every row must be accounted for in the frame histogram, resolved or not'
        assert out['pano_frames'].get('unresolved') == 1, \
            'rows with no dimensions need their own bucket'
        # And that bucket must not be dimension-shaped: '0x0' would read as a real degenerate
        # frame, silently merging "we never resolved this" with "the pano is 0 by 0".
        dimension_shaped = re.compile(r'\d+x\d+')
        for key in out['pano_frames']:
            if key != 'unresolved':
                assert dimension_shaped.fullmatch(key), key
        assert not dimension_shaped.fullmatch('unresolved')

    def test_crossed_block_does_not_emit_nan_into_the_artifact(self):
        """NaN costs are never rejected by max_sep_deg because NaN > 10.0 is False, so a NaN
        sigma reaches json.dump(allow_nan=False) and aborts the run on its last line.

        The unresolved row has to sit INSIDE a shared pano to reach the estimator — nulling an
        arbitrary row proves nothing, which is how the first version of this test passed against
        the unfixed code.
        """
        n = 4
        df = _frame(
            pano_id=['p1'] * n,
            user_id=['u1', 'u2', 'u1', 'u2'],
            label_type=['CurbRamp'] * n,
            pano_x=np.array([1000.0, 1002.0, 4000.0, 4003.0]),
            pano_y=np.array([500.0, 502.0, 700.0, 701.0]),
        )
        df.loc[df.index[0], 'pano_x'] = np.nan       # inside the shared pano
        out = mc.crossed_block(df)
        blob = json.dumps(out, allow_nan=False)      # must not raise
        assert 'NaN' not in blob

    def test_the_unresolved_row_is_excluded_rather_than_silently_costed(self):
        n = 4
        df = _frame(
            pano_id=['p1'] * n,
            user_id=['u1', 'u2', 'u1', 'u2'],
            label_type=['CurbRamp'] * n,
            pano_x=np.array([1000.0, 1002.0, 4000.0, 4003.0]),
            pano_y=np.array([500.0, 502.0, 700.0, 701.0]),
        )
        full = mc.crossed_block(df)
        df.loc[df.index[0], 'pano_x'] = np.nan
        holed = mc.crossed_block(df)
        assert holed['matched']['n_labels_considered'] < full['matched']['n_labels_considered']


class TestAnEmptyCorpusIsReportedNotCrashed:

    def test_multi_perspective_always_publishes_its_full_key_set(self, fixture_frame):
        """It returned a short three-key dict when no label of the requested type exists, so
        main() raised KeyError after the full census — and a JSON consumer reading the key
        KeyErrors too."""
        empty = fixture_frame[fixture_frame['label_type'] == '__nothing__']
        out = mc.multi_perspective(empty, label_type='CurbRamp')
        assert out['n_labels'] == 0
        assert out['n_objects'] == 0
        assert out['n_objects_multi_pano'] == 0

    def test_the_key_set_matches_the_populated_case(self, fixture_frame):
        """The real guarantee: a consumer sees the same shape either way."""
        empty = fixture_frame[fixture_frame['label_type'] == '__nothing__']
        assert set(mc.multi_perspective(empty, label_type='CurbRamp')) == \
            set(mc.multi_perspective(fixture_frame, label_type='CurbRamp'))
