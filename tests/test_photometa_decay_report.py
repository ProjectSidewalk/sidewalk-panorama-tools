"""Every number in reports/2026-09-06-photometa-decay.md, transcribed from the two committed censuses.

The repo's convention: a report table is the one place a plausible number has no compiler and no test, and
hand-typed counts in an earlier report were wrong by 2x and 6x with nothing about the sentences looking
different for it. So each figure is recomputed here from the artifact and the formatted string asserted to
be in the markdown.

This report matters more than most, because it *reverses* the premise the depth rollout was argued on. If
the decay figure drifts from the artifact, the argument for how fast to run the backfill silently drifts
with it.
"""

import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO_ROOT, 'reports', 'data')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-09-06-photometa-decay.md')


@pytest.fixture(scope='module')
def report():
    with open(REPORT, encoding='utf8') as f:
        return f.read()


def quoted(count, report):
    """The prose uses thousands separators where a table does not, so accept either spelling of one count.

    Deliberately not a regex over digits: matching loosely is how a transcription test stops discriminating.
    """
    return str(count) in report or '{:,}'.format(count) in report


@pytest.fixture(scope='module')
def after():
    with open(os.path.join(DATA, '2026-09-06-photometa-census.json')) as f:
        return json.load(f)


@pytest.fixture(scope='module')
def before():
    with open(os.path.join(DATA, '2026-08-09-photometa-census.json')) as f:
        return json.load(f)


class TestTheDecayHeadline:

    def test_the_transition_counts_are_the_artifacts(self, report, after):
        d = after['decay']
        for key in ('still_alive', 'died', 'still_dead', 'resurrected', 'alive_before', 'alive_after',
                    'depth_lost', 'n_matched'):
            assert quoted(d[key], report), '%s = %s is not in the report' % (key, d[key])

    def test_the_death_rate_is_the_artifacts(self, report, after):
        assert '%.2f%%' % after['decay']['died_pct_of_alive_before'] in report

    def test_the_dead_counts_on_both_sides_are_the_artifacts(self, report, after):
        d = after['decay']
        assert quoted(d['n_matched'] - d['alive_before'], report), 'dead in August'
        assert quoted(d['n_matched'] - d['alive_after'], report), 'dead now'

    def test_nothing_went_unfetched_and_the_report_says_so(self, report, after):
        assert after['decay']['n_unfetched'] == 0
        assert '| not re-fetched | 0 |' in report

    def test_the_per_era_deaths_are_the_artifacts(self, report, after):
        by_era = after['decay']['by_era']
        assert '**1** of %d' % by_era['legacy']['alive_before'] in report
        assert '**1** of %d' % by_era['mid']['alive_before'] in report
        assert '**0** of %d' % by_era['post179']['alive_before'] in report


class TestTheAliveRatesQuotedAgainstEachOther:

    def test_both_alive_percentages_are_the_artifacts(self, report, before, after):
        assert '%.1f%%' % before['summary']['alive_pct'] in report
        assert '%.1f%%' % after['summary']['alive_pct'] in report

    def test_the_replicated_coverage_figures_are_the_artifacts(self, report, after):
        assert '%.1f%%' % after['summary']['depth_available_pct_of_alive'] in report
        assert '%.1f%%' % after['summary']['dims_drift_pct_of_alive'] in report

    def test_the_error_count_is_the_artifacts_and_is_the_same_on_both_sides(self, report, before, after):
        assert after['summary']['errors'] == before['summary']['errors'] == 3
        assert quoted(after['summary']['errors'], report)

    def test_the_three_errored_panos_are_literally_the_same_three(self, before, after):
        """The report leans on this: because all three were already dead in August, they contribute no
        ambiguity to the death count. A different three would mean three fresh unknowns."""
        errored = lambda census: {r['pano_id'] for r in census['records'] if r.get('error')}

        assert errored(before) == errored(after)

    def test_none_of_the_errored_panos_was_alive_in_august(self, before, after):
        was_alive = {r['pano_id'] for r in before['records'] if r['found']}
        errored_now = {r['pano_id'] for r in after['records'] if r.get('error')}

        assert not (errored_now & was_alive)


class TestTheTiltReplication:

    @pytest.mark.parametrize('key', ['abs_pitch_p50_deg', 'abs_pitch_p90_deg', 'abs_pitch_p99_deg',
                                     'abs_roll_p50_deg', 'abs_roll_p90_deg', 'abs_roll_p99_deg'])
    def test_both_censuses_tilt_quantiles_are_quoted_as_measured(self, report, before, after, key):
        assert '%.2f' % after['summary']['tilt'][key] in report
        assert '%.2f' % before['summary']['tilt'][key] in report


class TestTheDeathsWereConfirmedNotAssumed:
    """The one methodological claim the headline rests on. An errored request is recorded found=False, so
    without the second probe a transient timeout would be indistinguishable from a retirement - and the
    report's whole point is a very small death count, which one false death would move by 50%."""

    def test_every_death_carries_the_reprobe_confirmation_flag(self, before, after):
        was_alive = {r['pano_id'] for r in before['records'] if r['found']}
        deaths = [r for r in after['records'] if r['pano_id'] in was_alive and not r['found']]

        assert len(deaths) == after['decay']['died']
        assert all(r.get('reprobe_confirmed') for r in deaths), 'a death nobody asked twice'

    def test_no_death_was_recovered_and_the_artifact_records_that(self, after):
        assert after['decay']['reprobe_recovered'] == 0
        assert not any(r.get('reprobe_recovered') for r in after['records'])


class TestTheProvenance:

    def test_the_artifact_names_the_census_it_replays(self, after):
        assert after['refetch_of'] == '2026-08-09-photometa-census.json'
        assert after['since_days'] == 28.0

    def test_it_carries_the_original_draw_forward(self, before, after):
        assert after['seed'] == before['seed']
        assert after['rawlabels_fetched'] == before['rawlabels_fetched']

    def test_it_is_the_same_panos_in_the_same_order(self, before, after):
        """A re-draw would make the comparison sampling noise rather than decay, and would look identical
        from the summary alone."""
        assert [r['pano_id'] for r in after['records']] == [r['pano_id'] for r in before['records']]

    def test_the_artifact_is_strict_json_with_no_nan_tokens(self):
        with open(os.path.join(DATA, '2026-09-06-photometa-census.json'), encoding='utf8') as f:
            text = f.read()

        assert 'NaN' not in text
        json.loads(text)
