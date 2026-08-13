"""Tests for the two rawlabels-loader gaps that only a Mapillary city exposes, plus the
referent-quality exclusion.

Both were found by pointing the existing desk-study machinery at Richmond, the first launched
Mapillary deployment (2026-08-11). Neither is visible on the six GSV cities:

1. **`pano_id` was not dtype-pinned.** Mapillary image ids are all-numeric, so pandas infers int64 for
   any Mapillary-sourced city — while the six GSV cities carry 22-char alphanumeric ids that always
   infer as object. This is the #46 bug class, which `DownloadRunner` and `CropRunner` both pin
   against with a comment naming Mapillary; the study loader did not. It is not cosmetic: every
   `pano_id[:2]` store path, and every merge between a str-keyed and an int-keyed frame, changes
   meaning silently rather than failing.
2. **`tags` was not loaded**, and it is the only field that distinguishes a label whose stored point
   identifies a located thing from one that could have been placed anywhere on a qualifying region.

The exclusion those two enable is a different principle from every other exclusion in the study spec.
The rest exclude on *record* quality — does the stored record replay, do the dims agree. This one
excludes on *referent* quality: a SurfaceProblem tagged brick/cobblestone is a valid label about a
whole brick sidewalk, so there is no particular spot it was aiming at and a stored-vs-gold
displacement has nothing to be a displacement from. Keeping such labels puts an arbitrary, unbounded
offset into the placement noise floor.

Real committed bytes, not synthesised rows: `fixtures/rawlabels_richmond_head.csv` is ten rows of the
live Richmond export, chosen to carry every case the rule decides — including the two it deliberately
does NOT exclude.
"""

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
import rawlabels as rl  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
MAPILLARY = os.path.join(FIXTURES, 'rawlabels_richmond_head.csv')
GSV = os.path.join(FIXTURES, 'rawlabels_newberg_head.csv')


@pytest.fixture(scope='module')
def mly():
    return rl.load_rawlabels(MAPILLARY)


@pytest.fixture(scope='module')
def gsv():
    return rl.load_rawlabels(GSV)


class TestNumericPanoIds:
    """The #46 bug class, recurring in the study loader."""

    def test_mapillary_pano_ids_stay_strings(self, mly):
        """Asserts the contract (every value is a `str`), not the dtype label: `dtype={'pano_id': str}`
        resolves to pandas' StringDtype on current versions and to plain object on older ones, and
        both satisfy every caller. What must never happen is an integer."""
        assert all(isinstance(v, str) for v in mly['pano_id'])
        assert pd.api.types.is_string_dtype(mly['pano_id'])

    def test_the_fixture_would_actually_infer_int64_unpinned(self):
        """Guards the guard. If the fixture's ids were not all-numeric the test above would pass on a
        loader with no pin at all, and pin nothing."""
        raw = pd.read_csv(MAPILLARY, usecols=['pano_id'])
        assert raw['pano_id'].dtype == np.int64, 'fixture must reproduce the unpinned inference'

    def test_gsv_pano_ids_are_unaffected(self, gsv):
        """The pin must not change a single GSV value — the six cities' committed artifacts must not
        move. (Verified end to end as well: re-running the off-axis and click-noise studies over the
        GSV cache after this change reproduces both committed artifacts byte for byte.)"""
        assert all(isinstance(v, str) for v in gsv['pano_id'])
        raw = pd.read_csv(GSV, usecols=['pano_id'], dtype=str)
        assert list(gsv['pano_id']) == list(raw['pano_id'])

    def test_the_two_char_shard_prefix_works(self, mly):
        """What int64 ids break in practice: the store layout is <city>/<pano_id[:2]>/<pano_id>.jpg,
        and slicing an int raises rather than mis-keying."""
        assert all(len(pid[:2]) == 2 for pid in mly['pano_id'])

    def test_a_join_against_string_keys_still_matches(self, mly):
        """The silent failure mode, which is worse than the crash: an int64 key column merged against
        a str key column matches nothing and reports zero coverage rather than an error."""
        keys = pd.DataFrame({'pano_id': mly['pano_id'].unique(), 'flag': True})
        merged = mly.merge(keys, on='pano_id', how='left')
        assert merged['flag'].notna().all()


class TestTagParsing:

    def test_tags_are_loaded(self, mly):
        assert 'tags' in mly.columns

    def test_the_heavy_text_columns_are_still_dropped(self, mly):
        """`tags` is short; `validations` is the bulk of the bytes and no desk study reads it."""
        for col in ('validations', 'description', 'pano_url', 'region_name'):
            assert col not in mly.columns, col

    def test_it_splits_the_bracketed_list(self):
        got = rl.parse_tags(['[points into traffic]', '[height difference,uneven/slanted]', '[]'])
        assert list(got) == [frozenset({'points into traffic'}),
                             frozenset({'height difference', 'uneven/slanted'}),
                             frozenset()]

    def test_json_would_not_have_worked(self):
        """Why this is a split-and-strip rather than json.loads: the brackets look like a JSON array
        but the tag text is unquoted, so every non-empty value fails to parse."""
        import json
        with pytest.raises(json.JSONDecodeError):
            json.loads('[points into traffic]')

    @pytest.mark.parametrize('value', [None, np.nan, '', '[]', float('nan')])
    def test_missing_and_empty_become_an_empty_set(self, value):
        assert rl.parse_tags([value]).iloc[0] == frozenset()

    def test_a_tag_containing_a_slash_survives(self):
        """'debris / pooled water' and 'uneven/slanted' both appear in the live vocabulary, so the
        parser must not split on anything but commas."""
        assert rl.parse_tags(['[debris / pooled water]']).iloc[0] == frozenset({'debris / pooled water'})
        assert rl.parse_tags(['[uneven/slanted]']).iloc[0] == frozenset({'uneven/slanted'})

    def test_real_fixture_tags_parse(self, mly):
        parsed = rl.parse_tags(mly['tags'])
        assert parsed.map(len).sum() > 0
        assert all(isinstance(s, frozenset) for s in parsed)


class TestLocatedReferent:
    """The exclusion itself, on the ten real rows chosen to carry every case it decides."""

    def test_occlusion_is_excluded(self, mly):
        keep = rl.has_located_referent(mly)
        assert not keep[mly['label_type'] == 'Occlusion'].any()

    def test_surface_problem_tagged_brick_cobblestone_is_excluded(self, mly):
        keep = rl.has_located_referent(mly)
        target = (mly['label_type'] == 'SurfaceProblem') & \
            rl.parse_tags(mly['tags']).map(lambda s: 'brick/cobblestone' in s)
        assert target.sum() >= 2, 'fixture must carry the excluded case'
        assert not keep[target].any()

    def test_it_fires_on_a_multi_tag_row(self, mly):
        """The rule is membership, not equality: one row carries [uneven/slanted,brick/cobblestone]
        and must still be excluded. A `tags == '[brick/cobblestone]'` implementation would keep it."""
        parsed = rl.parse_tags(mly['tags'])
        multi = (mly['label_type'] == 'SurfaceProblem') & (parsed.map(len) > 1) & \
            parsed.map(lambda s: 'brick/cobblestone' in s)
        assert multi.sum() >= 1, 'fixture must carry a multi-tag excluded row'
        assert not rl.has_located_referent(mly)[multi].any()

    def test_crosswalks_are_excluded_by_type_whatever_their_tags(self, mly):
        """A crosswalk label is correctly placed *anywhere along* the crosswalk, so two annotators who
        both place it correctly can be metres apart along its length. Extended-feature, like a region
        tag, but inherent to the type — so it is excluded regardless of tag, and the fixture's
        brick/cobblestone crosswalks go with it."""
        keep = rl.has_located_referent(mly)
        crosswalks = mly['label_type'] == 'Crosswalk'
        assert crosswalks.sum() >= 2, 'fixture must carry crosswalks'
        assert not keep[crosswalks].any()

    def test_no_sidewalk_is_excluded_by_type(self):
        """Synthetic because Richmond has no NoSidewalk labels at all — the type carries 82,769 labels
        in the six GSV cities and zero here, so the corpus this rule was written against cannot test it.

        Same argument as Crosswalk with a worse constant: a crosswalk's extent is bounded by the width
        of the roadway, a stretch of missing sidewalk by nothing in particular.
        """
        df = pd.DataFrame({'label_type': ['NoSidewalk', 'NoSidewalk'], 'tags': ['[]', '[street has a]']})
        assert not rl.has_located_referent(df).any()

    def test_no_curb_ramp_is_kept(self):
        """Discrimination against the near-name: NoCurbRamp is a *point* — a specific corner where a
        ramp should be and isn't — so a prefix or substring rule on 'No' would wrongly take it. Richmond
        has 6 of these and they are comparable subjects."""
        df = pd.DataFrame({'label_type': ['NoCurbRamp'] * 2, 'tags': ['[]', '[missing tactile warning]']})
        assert rl.has_located_referent(df).all()

    def test_the_exclusion_is_about_placement_not_crop_corpus_membership(self):
        """Guards a misreading with real consequences: Crosswalk (type 9) and NoSidewalk (type 7) have
        crop consumers and stay in the crop corpus. What they cannot be is the subject of a
        stored-vs-gold displacement. The set is named for referents, and this pins the distinction so
        nobody wires it into crop selection."""
        assert {'Crosswalk', 'NoSidewalk'} <= rl.NO_REFERENT_TYPES
        assert 'has_located_referent' in dir(rl)
        assert 'crop' not in rl.has_located_referent.__doc__.lower()

    def test_the_rule_is_keyed_on_the_type_tag_pair_not_the_tag_alone(self):
        """Synthetic, because no production row distinguishes the two yet: in the live Richmond
        vocabulary `brick/cobblestone` appears only on SurfaceProblem (excluded by tag) and Crosswalk
        (excluded by type), so a tag blacklist would behave identically on real data.

        The distinction is real semantics, though. "brick/cobblestone means unmeasurable" is false — a
        brick-surfaced *obstacle* is still a point you can be right or wrong about. What is unmeasurable
        is a SurfaceProblem whose only distinguishing feature is that the sidewalk is brick.
        """
        df = pd.DataFrame({
            'label_type': ['Obstacle', 'SurfaceProblem'],
            'tags': ['[brick/cobblestone]', '[brick/cobblestone]'],
        })
        keep = rl.has_located_referent(df)
        assert bool(keep.iloc[0]), 'a brick-surfaced obstacle is still a located point'
        assert not bool(keep.iloc[1])

    def test_surface_problems_with_point_tags_are_kept(self, mly):
        """Discrimination in the other direction: the rule must not exclude the type wholesale."""
        keep = rl.has_located_referent(mly)
        target = (mly['label_type'] == 'SurfaceProblem') & \
            rl.parse_tags(mly['tags']).map(lambda s: 'height difference' in s)
        assert target.sum() >= 1
        assert keep[target].all()

    def test_ordinary_and_untagged_labels_are_kept(self, mly):
        keep = rl.has_located_referent(mly)
        curb = mly['label_type'] == 'CurbRamp'
        assert curb.sum() >= 4
        assert keep[curb].all()

    def test_the_counts_reconcile_on_the_fixture(self, mly):
        keep = rl.has_located_referent(mly)
        assert len(mly) == 10
        assert int((~keep).sum()) == 5, \
            'one Occlusion, two Crosswalks, two SurfaceProblem/brick rows'
        assert int(keep.sum()) == 5

    def test_it_returns_an_aligned_boolean_series(self, mly):
        keep = rl.has_located_referent(mly)
        assert keep.dtype == bool
        assert list(keep.index) == list(mly.index)

    def test_it_works_on_a_frame_with_no_tags_column(self, mly):
        """Callers that built a frame before `tags` was loaded must not crash — they simply lose the
        tag half of the rule, keeping the type half."""
        no_tags = mly.drop(columns=['tags'])
        keep = rl.has_located_referent(no_tags)
        assert not keep[no_tags['label_type'] == 'Occlusion'].any()
        assert int(keep.sum()) == 7, 'the type arm still fires: one Occlusion and two Crosswalks'

    def test_it_survives_a_non_default_index(self, mly):
        """Studies filter before excluding, so the frame arriving here rarely has a 0..n-1 index."""
        shuffled = mly.iloc[[9, 0, 3, 6]]
        keep = rl.has_located_referent(shuffled)
        assert list(keep.index) == [9, 0, 3, 6]
        assert not keep.loc[9]          # the Occlusion row
        assert not keep.loc[3]          # a SurfaceProblem/brick row

    def test_the_filter_is_defined_in_terms_of_the_exposed_tag_arm(self, mly, monkeypatch):
        """`region_tag_mask` is not a convenience wrapper — it is the definition, and this is what
        says so. Replace it and the corpus filter has to move with it; a `has_located_referent`
        holding its own copy of the comprehension would sail through.
        """
        monkeypatch.setattr(rl, 'region_tag_mask', lambda df: pd.Series(True, index=df.index))
        assert not rl.has_located_referent(mly).any()
        monkeypatch.setattr(rl, 'region_tag_mask', lambda df: pd.Series(False, index=df.index))
        keep = rl.has_located_referent(mly)
        assert int(keep.sum()) == 7, 'only the type arm left: one Occlusion and two Crosswalks'

    def test_the_rule_is_narrow_by_design(self):
        """Pins the scope so widening it is a visible decision rather than a drift. The adjacent
        candidates in the live Richmond vocabulary are deliberately absent — the two SurfaceProblem tags
        below name a defect you can point at, not a property of a whole stretch."""
        assert rl.NO_REFERENT_TYPES == frozenset({'Occlusion', 'Crosswalk', 'NoSidewalk'})
        assert rl.REGION_TAGS == frozenset({('SurfaceProblem', 'brick/cobblestone')})
        for candidate in (('SurfaceProblem', 'bumpy'), ('SurfaceProblem', 'uneven/slanted')):
            assert candidate not in rl.REGION_TAGS, candidate


class TestRegionTagMask:
    """The tag arm on its own. It exists because two callers needed it and each had a transcription:
    `has_located_referent` filters the corpus on it, and `mapillary_census.referent_exclusion`
    publishes how many labels it removes. Two copies of one comprehension is a rule that can be
    changed in one place and reported from the other, and the only assertion over those published
    counts is that the arms sum to the total — which two different rules would still satisfy.
    """

    def test_it_is_exactly_the_tag_arm(self, mly):
        mask = rl.region_tag_mask(mly)
        assert int(mask.sum()) == 2, 'the two SurfaceProblem/brick rows'
        assert set(mly.loc[mask, 'label_type']) == {'SurfaceProblem'}

    def test_the_type_arm_is_not_in_it(self, mly):
        """Occlusion and Crosswalk are excluded by type, not by tag, and must not be double-counted:
        the census reports the two arms as disjoint and asserts they sum."""
        mask = rl.region_tag_mask(mly)
        assert not mask[mly['label_type'].isin({'Occlusion', 'Crosswalk', 'NoSidewalk'})].any()

    def test_the_two_arms_partition_the_exclusion(self, mly):
        """The invariant `pool_referent_exclusion` asserts, checked here against the definitions
        rather than against numbers a previous run produced."""
        excluded = ~rl.has_located_referent(mly)
        by_type = mly['label_type'].isin(rl.NO_REFERENT_TYPES)
        by_tag = rl.region_tag_mask(mly)
        assert not (by_type & by_tag).any()
        assert list(excluded) == list(by_type | by_tag)

    def test_it_keys_on_the_pair_not_the_tag(self):
        """A CurbRamp on a brick sidewalk is still a point you can stand at."""
        df = pd.DataFrame({'label_type': ['CurbRamp', 'SurfaceProblem'],
                           'tags': ['[brick/cobblestone]', '[brick/cobblestone]']})
        assert list(rl.region_tag_mask(df)) == [False, True]

    def test_a_missing_tags_column_is_no_tags_rather_than_a_crash(self, mly):
        """The drift the two copies had already: the filtering caller tolerated the column's absence
        and the reporting caller read `df['tags']` straight, so a frame built before `tags` was
        loaded crashed one and not the other."""
        assert not rl.region_tag_mask(mly.drop(columns=['tags'])).any()

    def test_it_returns_an_aligned_boolean_series(self, mly):
        mask = rl.region_tag_mask(mly.iloc[[9, 0, 3, 6]])
        assert mask.dtype == bool
        assert list(mask.index) == [9, 0, 3, 6]
        assert bool(mask.loc[3]), 'a SurfaceProblem/brick row'


class TestGsvCorpusUnaffected:
    """The loader change must not move a single committed number, so the GSV path is pinned too."""

    def test_the_gsv_fixture_still_loads_identically(self, gsv):
        assert len(gsv) == 5
        assert gsv['heading'].dtype == np.float64
        assert str(gsv['time_created'].dt.tz) == 'UTC'
        assert gsv['time_created'].iloc[0].strftime('%Y-%m-%d') == '2019-01-30'

    def test_the_exclusion_is_opt_in_not_applied_by_the_loader(self, mly):
        """`load_rawlabels` must return every row. The committed studies do not apply this rule, and a
        loader that filtered silently would change every artifact in reports/data/."""
        assert len(rl.load_rawlabels(MAPILLARY)) == 10


class TestReplayOnMapillary:
    """The finding that made the Mapillary question tractable, pinned against real bytes: the front
    end runs the SAME canvas->pano projection for Mapillary as for GSV, so `exact_y` is a meaningful
    eligibility rule there and the GSV fov ladder applies. If it did not, the replay could not be
    exact — fov sets the focal length."""

    def test_every_fixture_row_replays_exactly_on_both_axes(self, mly):
        out = ers.replay_frame(mly)
        assert int(out['replayable_x'].sum()) == len(mly)
        assert int(out['replayable_y'].sum()) == len(mly)
        assert int(out['exact_x'].sum()) == len(mly), 'Mapillary pano_x must replay at 0 px'
        assert int(out['exact_y'].sum()) == len(mly), 'Mapillary pano_y must replay at 0 px'
        assert float(np.abs(out['dx']).max()) == 0.0
        assert float(np.abs(out['dy']).max()) == 0.0

    def test_the_canvas_is_the_same_720x480_frame(self, mly):
        assert set(mly['canvas_width']) == {720.0}
        assert set(mly['canvas_height']) == {480.0}

    def test_zoom_sits_on_the_gsv_ladder_stops(self, mly):
        """Integer stops, which is why get_3d_fov's ladder transfers unchanged."""
        assert set(mly['zoom']) <= {1.0, 2.0, 3.0}

    def test_camera_roll_is_present_which_it_never_is_on_gsv(self, mly, gsv):
        """The asymmetry that makes Mapillary worth a stratum at all: the pre-registration prices
        endpoint 2 at n ~= 310 because camera_roll is empty in 100% of GSV rawLabels rows and has to
        come from photometa, which only answers for panos still alive at Google. Mapillary serves it
        inline, for every row, with no survival selection."""
        assert mly['camera_roll'].notna().all()
        assert gsv['camera_roll'].isna().all()

    def test_pano_dimensions_vary_per_pano(self, mly):
        """Unlike GSV's small set of frames, Mapillary panos arrive at assorted sizes — which is why
        the dims preflight matters more here."""
        assert mly.groupby(['pano_width', 'pano_height']).ngroups >= 2
