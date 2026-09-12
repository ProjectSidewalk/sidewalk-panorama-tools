"""rawLabels changed shape between the 2026-08 censuses and the SidewalkWebpage#4842 post-repair sweep:
`time_created` went from epoch milliseconds to ISO-8601, `tags` from a bracketed comma-join to a JSON
array. Both eras are cached and both must load identically, or the after-sweep silently fails to
reproduce the before-table."""

import os
import sys

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'reports', 'scripts'))

import rawlabels  # noqa: E402


def test_time_created_loads_from_epoch_ms_and_from_iso8601():
    legacy = rawlabels.parse_time_created(pd.Series([1676068244439]))
    current = rawlabels.parse_time_created(pd.Series(['2023-02-10T22:30:44.439Z']))
    assert legacy.iloc[0] == current.iloc[0] == pd.Timestamp('2023-02-10T22:30:44.439Z')
    assert str(legacy.dt.tz) == str(current.dt.tz) == 'UTC'


def test_tags_load_from_the_bracket_join_and_from_json():
    legacy, current = rawlabels.parse_tags(['[points into traffic,steep]', '["points into traffic","steep"]'])
    assert legacy == current == frozenset({'points into traffic', 'steep'})


def test_empty_and_missing_tags_are_empty_in_both_forms():
    assert rawlabels.parse_tags(['[]', '', None]).tolist() == [frozenset()] * 3


def test_a_json_tag_containing_a_comma_is_one_tag():
    # The legacy form could not express this; the JSON form can, and must not be re-split on the comma.
    assert rawlabels.parse_tags(['["a, b"]']).iloc[0] == frozenset({'a, b'})
