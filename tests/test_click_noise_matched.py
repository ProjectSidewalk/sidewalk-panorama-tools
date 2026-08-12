"""Tests for click_noise_study's matched-pair mode — pairing by design instead of by radius.

Why the mode exists. The clustering estimator infers pair identity from angular proximity, which is
all that is available for *incidental* co-location in production data. It is also why the study reports
a radius sweep with no plateau: at every radius the pair population is a mixture of same-object noise
and genuinely distinct neighbours, so sigma_el runs 0.299 deg (r = 0.75) to 0.599 deg (r = 2) with no
correct answer. When two labellers are *asked* to label the same panos exhaustively, identity is known
by construction, and a one-to-one assignment recovers it with no radius at all.

Two things this file is careful about, because both were live risks:

1. **The assignment must be optimal, not greedy.** Greedy smallest-first is the obvious
   implementation and it is wrong in a way that is easy to miss — take the globally closest pair first
   and the remaining labels can be forced across the frame. `test_greedy_would_get_this_wrong` builds
   the counterexample and asserts the optimum is chosen.
2. **The mode is only valid on a designed block, and it is worse than clustering without one.**
   Measured on the six-city corpus it returns sigma_el 0.967 deg against the clustered 0.507 deg,
   because a corner's four curb ramps get paired across users almost arbitrarily. So `study()` must not
   emit it unless a pano list is supplied — a plausible sigma from force-paired objects landing in a
   committed artifact is exactly the failure the radius sweep already documents.
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

import click_noise_study as cn  # noqa: E402
import rawlabels  # noqa: E402
import studyfmt  # noqa: E402

PANO_H = 4096.0
PANO_W = 8192.0


def _labels(rows):
    """A minimal loaded-frame shape: (user, label_type, azimuth_deg, elevation_deg) per row, converted
    back to the stored pano pixels the estimator actually reads."""
    out = []
    for i, (user, lt, az, el) in enumerate(rows):
        out.append({
            'label_id': i, 'user_id': user, 'pano_id': 'p1', 'label_type': lt,
            'tags': '[]',
            'time_created': pd.Timestamp('2026-08-11', tz='UTC') + pd.Timedelta(minutes=i),
            'pano_x': (az % 360.0) / 360.0 * PANO_W,
            'pano_y': PANO_H / 2 - el * (PANO_H / 2) / 90.0,
            'pano_width': PANO_W, 'pano_height': PANO_H,
            'agree_count': 1, 'disagree_count': 0,
        })
    return pd.DataFrame(out)


class TestAssignment:
    """The combinatorial core, tested independently of any label frame."""

    def test_it_picks_the_minimum_total_cost(self):
        cost = np.array([[1.0, 9.0], [9.0, 1.0]])
        assert sorted(cn._assign(cost)[0]) == [(0, 0), (1, 1)]

    def test_it_is_one_to_one(self):
        cost = np.array([[1.0, 1.1], [1.2, 1.3]])
        pairs, _ = cn._assign(cost)
        assert len({i for i, _ in pairs}) == len(pairs)
        assert len({j for _, j in pairs}) == len(pairs)

    def test_greedy_would_get_this_wrong(self):
        """The counterexample that makes 'optimal' load-bearing rather than decorative.

        Greedy takes (0,0) at 1.0 first, which forces (1,1) at 10.0 for a total of 11.0. The optimum
        is (0,1) + (1,0) = 2.0 + 2.0 = 4.0. A greedy implementation would pass every other test here.
        """
        cost = np.array([[1.0, 2.0], [2.0, 10.0]])
        pairs, exact = cn._assign(cost)
        assert exact
        assert sorted(pairs) == [(0, 1), (1, 0)]
        total = sum(cost[i, j] for i, j in pairs)
        assert total == pytest.approx(4.0)

    def test_the_smaller_side_is_fully_matched_and_the_surplus_is_left_over(self):
        cost = np.array([[1.0, 5.0, 9.0]])
        pairs, _ = cn._assign(cost)
        assert pairs == [(0, 0)]

    def test_it_falls_back_to_greedy_beyond_the_search_cap_and_says_so(self):
        """5 into 9 is 15,120 injective maps, past ASSIGNMENT_MAX_MAPS. The result must still be a
        valid one-to-one matching, and `exact` must be False so the caller can count it."""
        rng = np.random.default_rng(11)
        cost = rng.uniform(0, 10, (5, 9))
        pairs, exact = cn._assign(cost)
        assert not exact
        assert len(pairs) == 5
        assert len({i for i, _ in pairs}) == 5 and len({j for _, j in pairs}) == 5

    def test_the_cap_is_high_enough_for_a_realistic_block(self):
        """Guards the guard: if the cap were low the exact path would rarely run, and
        test_greedy_would_get_this_wrong would be pinning a branch nothing uses."""
        maps_4_into_8 = 8 * 7 * 6 * 5
        assert maps_4_into_8 <= cn.ASSIGNMENT_MAX_MAPS


class TestMatchedPairs:

    def test_it_pairs_the_same_object_across_users(self):
        df = _labels([('u1', 'CurbRamp', 10.0, -5.0), ('u2', 'CurbRamp', 10.4, -5.3)])
        pairs, diag = cn.matched_pairs(df)
        assert diag['n_pairs_matched'] == 1
        assert pairs['d_az'].iloc[0] == pytest.approx(-0.4 * np.cos(np.radians(-5.15)), abs=1e-9)
        assert pairs['d_el'].iloc[0] == pytest.approx(0.3, abs=1e-9)

    def test_it_recovers_pairs_the_radius_estimator_misses(self):
        """The motivation, measured. Two labellers place the same two ramps but disagree by ~3 deg —
        beyond every radius in the sweep, so clustering finds nothing while the assignment finds both.
        """
        df = _labels([('u1', 'CurbRamp', 10.0, -5.0), ('u1', 'CurbRamp', 40.0, -6.0),
                      ('u2', 'CurbRamp', 12.8, -5.4), ('u2', 'CurbRamp', 42.9, -6.2)])
        clustered = cn.cluster_pairs(cn.cluster_labels(df, cn.PRIMARY_RADIUS_DEG))
        assert len(clustered) == 0, 'the radius estimator must miss these by construction'
        pairs, diag = cn.matched_pairs(df)
        assert diag['n_pairs_matched'] == 2
        assert set(np.round(pairs['d_az'].abs(), 1)) == {2.8, 2.9}

    def test_it_does_not_cross_objects_when_matching(self):
        """Nearest-partner-per-label would pair both of u2's clicks with u1's nearer ramp. The
        assignment cannot, because it is one-to-one."""
        df = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u1', 'CurbRamp', 11.0, 0.0),
                      ('u2', 'CurbRamp', 10.2, 0.0), ('u2', 'CurbRamp', 11.2, 0.0)])
        pairs, _ = cn.matched_pairs(df)
        assert len(pairs) == 2
        assert np.allclose(pairs['d_az'].abs(), 0.2, atol=1e-9)

    def test_the_difference_is_signed(self):
        """sigma reads median|d|, so `abs()` anywhere in the pair record would leave every sigma test
        green — but a *bias* estimate reads the signed mean, and the clustered estimator is signed.
        Constructed so both axes come out negative-and-positive rather than both positive.
        """
        df = _labels([('u1', 'CurbRamp', 10.0, -5.0), ('u2', 'CurbRamp', 9.6, -4.7)])
        pairs, _ = cn.matched_pairs(df)
        assert pairs['d_el'].iloc[0] < 0, 'first user clicked lower, so d_el must be negative'
        assert pairs['d_el'].iloc[0] == pytest.approx(-0.3, abs=1e-9)
        assert pairs['d_az'].iloc[0] > 0, 'first user clicked further clockwise'

    def test_the_sign_convention_survives_the_internal_swap(self):
        """`matched_pairs` puts the smaller side on the cost matrix rows, so the two users get swapped
        whenever the second labelled fewer objects. If that swap is not undone, those panos report
        their differences negated — and a signed mean over a corpus with both shapes partially
        cancels, which looks like an absence of bias rather than a bug.
        """
        geometry = [('CurbRamp', 10.0, -5.0), ('CurbRamp', 9.6, -4.7)]
        surplus = ('CurbRamp', 80.0, -4.0)
        # u1 labels one object, u2 labels two -> no swap.
        few_first, _ = cn.matched_pairs(_labels([
            ('u1',) + geometry[0], ('u2',) + geometry[1], ('u2',) + surplus]))
        # u1 labels two, u2 labels one -> swap, which must be undone.
        many_first, _ = cn.matched_pairs(_labels([
            ('u1',) + geometry[0], ('u1',) + surplus, ('u2',) + geometry[1]]))
        assert len(few_first) == len(many_first) == 1
        for col in ('d_az', 'd_el'):
            assert few_first[col].iloc[0] == pytest.approx(many_first[col].iloc[0], abs=1e-12), col
        assert many_first['d_el'].iloc[0] < 0, 'and still u1 minus u2, not the other way round'

    def test_it_never_pairs_a_user_with_themselves(self):
        """A double-submit is not an independent placement. With one user the answer is no pairs, not a
        self-pair at zero separation, which would drag any sigma to 0."""
        df = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u1', 'CurbRamp', 10.1, 0.0)])
        pairs, diag = cn.matched_pairs(df)
        assert diag['n_pairs_matched'] == 0 and len(pairs) == 0

    def test_unequal_counts_leave_the_surplus_unmatched(self):
        df = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u1', 'CurbRamp', 40.0, 0.0),
                      ('u1', 'CurbRamp', 80.0, 0.0), ('u2', 'CurbRamp', 10.3, 0.0)])
        pairs, diag = cn.matched_pairs(df)
        assert diag['n_pairs_matched'] == 1
        assert pairs['d_az'].abs().iloc[0] == pytest.approx(0.3, abs=1e-9)

    def test_a_forced_match_beyond_the_gate_is_rejected_and_counted(self):
        """Unequal counts force a partner for the surplus; without the gate one match across the frame
        moves the median. Rejections are reported, never silently dropped."""
        df = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u2', 'CurbRamp', 200.0, 0.0)])
        pairs, diag = cn.matched_pairs(df, max_sep_deg=10.0)
        assert diag['n_pairs_matched'] == 0
        assert diag['n_rejected_beyond_max_sep'] == 1

    def test_the_gate_can_be_disabled(self):
        df = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u2', 'CurbRamp', 200.0, 0.0)])
        _, diag = cn.matched_pairs(df, max_sep_deg=None)
        assert diag['n_pairs_matched'] == 1 and diag['n_rejected_beyond_max_sep'] == 0

    def test_different_label_types_are_never_paired(self):
        df = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u2', 'Crosswalk', 10.1, 0.0)])
        assert cn.matched_pairs(df)[1]['n_pairs_matched'] == 0

    def test_the_seam_is_wrapped(self):
        """359.8 deg and 0.1 deg are 0.3 deg apart, not 359.7."""
        df = _labels([('u1', 'CurbRamp', 359.8, 0.0), ('u2', 'CurbRamp', 0.1, 0.0)])
        pairs, diag = cn.matched_pairs(df, max_sep_deg=1.0)
        assert diag['n_pairs_matched'] == 1
        assert pairs['d_az'].abs().iloc[0] == pytest.approx(0.3, abs=1e-9)

    def test_the_pano_list_restricts_the_block(self):
        df = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u2', 'CurbRamp', 10.2, 0.0)])
        other = df.copy()
        other['pano_id'] = 'p2'
        both = pd.concat([df, other], ignore_index=True)
        assert cn.matched_pairs(both)[1]['n_pairs_matched'] == 2
        assert cn.matched_pairs(both, panos=['p1'])[1]['n_pairs_matched'] == 1
        assert cn.matched_pairs(both, panos=['nope'])[1]['n_pairs_matched'] == 0

    def test_it_agrees_with_the_clustered_estimator_on_a_pair_both_find(self):
        """The two modes differ in *which* pairs they select, never in the arithmetic of a difference —
        both go through `_pair_record`. If they disagreed here, one of the two sigmas in any comparison
        would be measuring a different quantity."""
        df = _labels([('u1', 'CurbRamp', 10.0, -7.0), ('u2', 'CurbRamp', 10.4, -7.3)])
        matched, _ = cn.matched_pairs(df)
        clustered = cn.cluster_pairs(cn.cluster_labels(df, cn.PRIMARY_RADIUS_DEG))
        assert len(matched) == len(clustered) == 1
        for col in ('d_az', 'd_el', 'd_total', 'el_mean'):
            assert matched[col].iloc[0] == pytest.approx(clustered[col].iloc[0], abs=1e-12), col

    def test_it_recovers_a_known_injected_sigma(self):
        """End-to-end estimator check, constructed exactly as the clustered path's equivalent
        (tests/test_click_noise_study.py::TestSigmaRecovery): **both** labellers are perturbed
        independently around one shared truth, so the pair difference is N(0, 2*sigma^2) and
        `sigma = 1.4826 * median|d| / sqrt(2)` recovers sigma.

        Perturbing only one side instead returns sigma/sqrt(2) — 0.27 for an injected 0.4 — which is a
        plausible-looking wrong answer and was this test's first version.
        """
        rng = np.random.default_rng(20260811)
        n, sigma = 400, 0.4
        rows = []
        for i in range(n):
            az0, el0 = rng.uniform(20, 340), rng.uniform(-40, -5)
            daz = rng.normal(0, sigma, 2) / np.cos(np.radians(el0))
            dele = rng.normal(0, sigma, 2)
            # One pano per object, so each is its own assignment problem — the designed-block shape.
            rows.append(('u1', 'CurbRamp', az0 + daz[0], el0 + dele[0], f'p{i}'))
            rows.append(('u2', 'CurbRamp', az0 + daz[1], el0 + dele[1], f'p{i}'))
        df = _labels([(u, lt, a, e) for u, lt, a, e, _ in rows])
        df['pano_id'] = [p for *_, p in rows]

        pairs, diag = cn.matched_pairs(df)
        assert diag['n_pairs_matched'] == n
        got = cn.sigma_from_pairs(pairs)
        assert got['sigma_el_deg'] == pytest.approx(sigma, rel=0.15)
        assert got['sigma_az_deg'] == pytest.approx(sigma, rel=0.15)

    def test_it_is_deterministic(self):
        rng = np.random.default_rng(3)
        rows = [(f'u{i % 2 + 1}', 'CurbRamp', float(a), float(e))
                for i, (a, e) in enumerate(zip(rng.uniform(0, 360, 12), rng.uniform(-20, 0, 12)))]
        df = _labels(rows)
        first = cn.matched_pairs(df)[0]
        for _ in range(3):
            again = cn.matched_pairs(df.sample(frac=1.0, random_state=7))[0]
            assert sorted(np.round(first['d_total'], 9)) == sorted(np.round(again['d_total'], 9))


class TestSharedPanos:

    def test_it_finds_only_panos_with_two_users(self):
        df = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u2', 'CurbRamp', 10.2, 0.0)])
        solo = df.iloc[:1].copy()
        solo['pano_id'] = 'p2'
        assert cn.shared_panos(pd.concat([df, solo], ignore_index=True)) == ['p1']

    def test_it_documents_the_richmond_failure_shape(self):
        """Two labellers on a shared *route* pick different perspectives of the same ramps, so pano
        overlap is far below intent. This is the diagnostic that showed 15 shared of 55 and 49."""
        a = _labels([('u1', 'CurbRamp', 10.0, 0.0)] * 1)
        a['pano_id'] = 'pA'
        b = _labels([('u2', 'CurbRamp', 10.0, 0.0)] * 1)
        b['pano_id'] = 'pB'
        both = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u2', 'CurbRamp', 10.2, 0.0)])
        assert cn.shared_panos(pd.concat([a, b, both], ignore_index=True)) == ['p1']


class TestReadPanoList:

    def test_it_skips_blanks_and_comments(self, tmp_path):
        p = tmp_path / 'panos.txt'
        p.write_text('# the agreed block\n511129198087695\n\n  712269211672483  # a note\n',
                     encoding='utf-8')
        assert cn.read_pano_list(str(p)) == ['511129198087695', '712269211672483']

    def test_numeric_ids_stay_strings(self, tmp_path):
        """Mapillary ids are all-numeric; an int here would fail every merge against a str pano_id."""
        p = tmp_path / 'panos.txt'
        p.write_text('511129198087695\n', encoding='utf-8')
        assert all(isinstance(v, str) for v in cn.read_pano_list(str(p)))


class TestMatchedStudyIsOptIn:
    """The footgun guard. Matched mode on incidental data reports sigma_el 0.967 deg against the
    clustered 0.507 deg, so it must never appear in an artifact unasked."""

    @staticmethod
    def _city(tmp_path):
        base = {'pano_id': 'p1', 'label_type': 'CurbRamp', 'tags': '[]',
                'pano_width': PANO_W, 'pano_height': PANO_H, 'canvas_width': 720.0,
                'canvas_height': 480.0, 'heading': 0.0, 'pitch': -10.0, 'zoom': 1.0,
                'canvas_x': 360.0, 'canvas_y': 240.0, 'camera_heading': 0.0,
                'time_created': int(pd.Timestamp('2026-08-11', tz='UTC').value // 10 ** 6)}
        rows = [dict(base, label_id=1, user_id='u1', pano_x=2048.0, pano_y=2048.0),
                dict(base, label_id=2, user_id='u2', pano_x=2052.0, pano_y=2050.0)]
        df = pd.DataFrame(rows)
        for col in rawlabels.STUDY_COLUMNS:
            if col not in df:
                df[col] = np.nan
        df[rawlabels.STUDY_COLUMNS].to_csv(tmp_path / 'city.csv', index=False)
        return tmp_path

    def test_without_a_pano_list_the_key_is_absent(self, tmp_path):
        out = cn.study(str(self._city(tmp_path)))
        assert 'matched' not in out, 'a force-paired sigma must not appear by default'
        assert 'overall' in out and 'radius_sweep' in out

    def test_with_a_pano_list_it_runs(self, tmp_path):
        out = cn.study(str(self._city(tmp_path)), pano_list=['p1'])
        assert out['matched']['n_pairs_matched'] == 1
        assert out['matched']['n_panos_in_list'] == 1
        assert out['matched']['n_panos_with_two_users'] == 1

    def test_matched_study_requires_the_pano_list_explicitly(self, tmp_path):
        """It used to default to `shared_panos(df)`, which is the footgun. Callers must state the
        block."""
        with pytest.raises(TypeError):
            cn.matched_study(pd.DataFrame())

    def test_the_cli_reports_when_it_did_not_run(self, tmp_path, capsys):
        cn.main([str(self._city(tmp_path)), '--fetched', '2026-08-11'])
        assert 'matched mode: not run' in capsys.readouterr().out

    def test_the_committed_artifact_carries_no_matched_block(self):
        """It predates the mode and was produced without a pano list, so consumers must treat the key
        as optional — the same convention era_replay_study uses for drift_signature_post_fix."""
        path = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-09-click-noise-summary.json')
        if not os.path.exists(path):
            pytest.skip('committed click-noise summary not present')
        with open(path, encoding='utf-8') as f:
            assert 'matched' not in json.load(f)


class TestReferentExclusionIsApplied:

    @staticmethod
    def _frame():
        df = _labels([('u1', 'SurfaceProblem', 10.0, 0.0), ('u2', 'SurfaceProblem', 10.2, 0.0),
                      ('u1', 'CurbRamp', 40.0, 0.0), ('u2', 'CurbRamp', 40.2, 0.0)])
        df.loc[df['label_type'] == 'SurfaceProblem', 'tags'] = '[brick/cobblestone]'
        return df

    def test_region_tagged_labels_do_not_become_pairs(self):
        """A brick sidewalk has no particular spot, so the two clicks differ by an arbitrary amount
        that is not placement noise."""
        out = cn.matched_study(self._frame(), panos=['p1'])
        assert out['n_pairs_matched'] == 1, 'only the CurbRamp pair is comparable'
        assert out['n_dropped_unlocated_referent'] == 2

    def test_it_can_be_turned_off(self):
        out = cn.matched_study(self._frame(), panos=['p1'], exclude_unlocated=False)
        assert out['n_pairs_matched'] == 2
        assert out['n_dropped_unlocated_referent'] == 0


class TestSameUserDoubleSubmitsAreNotIndependentPlacements:
    """matched_pairs' own comment says "One placement per user per object: a double-submit is not an
    independent placement, and the earliest is the one the clustering estimator keeps too" — but
    sort_values('time_created') enforces nothing on its own. The clustered estimator does the real
    work with drop_duplicates('user_id', keep='first') inside each cluster.
    """

    def _block(self):
        """u1 clicks ramp A twice; u2 clicks ramp A and ramp B. One true pair, one forced."""
        return _labels([('u1', 'CurbRamp', 10.0, 0.0),
                        ('u1', 'CurbRamp', 10.05, 0.0),
                        ('u2', 'CurbRamp', 10.2, 0.0),
                        ('u2', 'CurbRamp', 14.0, 0.0)])

    def test_the_double_submit_does_not_manufacture_a_second_pair(self):
        pairs, diag = cn.matched_pairs(self._block(), ['p1'])
        assert len(pairs) == 1, 'the second u1 click is the same object, not a second placement'

    def test_the_sigma_is_not_inflated_by_the_forced_match(self):
        """The measured consequence: the forced pair sits at d_az -3.95 deg and drags a sigma that
        should read ~0.2 deg up by an order of magnitude."""
        pairs, _ = cn.matched_pairs(self._block(), ['p1'])
        sigma = cn.sigma_from_pairs(pairs)
        assert sigma['sigma_az_deg'] < 0.5, sigma['sigma_az_deg']

    def test_it_agrees_with_the_clustered_estimator_on_this_block(self):
        """The two estimators disagreeing by 10x on a four-label block is the signal that one of
        them is pairing distinct objects."""
        block = self._block()
        matched, _ = cn.matched_pairs(block, ['p1'])
        clustered = cn.cluster_pairs(cn.cluster_labels(block, cn.PRIMARY_RADIUS_DEG))
        assert len(matched) == len(clustered) == 1
        assert cn.sigma_from_pairs(matched)['sigma_az_deg'] == pytest.approx(
            cn.sigma_from_pairs(clustered)['sigma_az_deg'], rel=0.05)

    def test_two_genuinely_distinct_objects_still_pair(self):
        """Guards the guard: the dedupe must not collapse a user's two real labels on one pano."""
        block = _labels([('u1', 'CurbRamp', 10.0, 0.0),
                         ('u1', 'CurbRamp', 40.0, 0.0),
                         ('u2', 'CurbRamp', 10.2, 0.0),
                         ('u2', 'CurbRamp', 40.3, 0.0)])
        pairs, _ = cn.matched_pairs(block, ['p1'])
        assert len(pairs) == 2


class TestTheSummaryPrintSurvivesADisabledGate:
    """matched_pairs explicitly supports max_sep_deg=None and a test exercises it, but main()
    format-specced it with :g — the exact format(None, spec) defect studyfmt was added in this PR
    to eliminate, left in the function the PR just fixed. It fires at the summary print, after the
    whole study and before --write."""

    def test_a_disabled_gate_prints_instead_of_raising(self, tmp_path, capsys):
        df = _labels([('u1', 'CurbRamp', 10.0, 0.0), ('u2', 'CurbRamp', 10.2, 0.0)])
        csv = tmp_path / 'somewhere.csv'
        df.to_csv(csv, index=False)
        _, diag = cn.matched_pairs(df, ['p1'], max_sep_deg=None)
        assert diag['max_sep_deg'] is None
        # the print path, exercised directly on the diagnostics dict main() would hold
        line = (f"matched: {diag['n_pairs_matched']} pairs "
                f"(rejected {diag['n_rejected_beyond_max_sep']} beyond "
                f"{studyfmt.fmt(diag['max_sep_deg'], 'g')} deg)")
        assert 'n/a' in line
