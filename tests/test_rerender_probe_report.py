"""Every number in reports/2026-09-06-rerender-probe.md, re-derived from the committed artifacts.

The repo's convention: a report table is the one place a plausible number has no compiler and no test, and
hand-typed counts in an earlier report were wrong by 2x and 6x with nothing about the sentences looking
different for it. This one was drafted from a table pasted into a GitHub comment, where a per-window median
had been transcribed as 12.5 against the 12.1 the reduction actually produced - so the failure mode is not
hypothetical here, it already happened once on this very finding. And its first draft gated the table's
displacement columns on the classifier, which printed 0 on two panoramas the same fit put a 1.2 px tilt on;
the review that found it is the reason every count in the prose is now pinned here, not only the table.

The table is not spot-checked: every cell is regenerated through `rerender_reduce.format_row`, which is the
same function the report was generated with, so the report and the artifact cannot drift apart without this
failing.
"""

import json
import os
import re
import statistics
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
DATA = os.path.join(REPO_ROOT, 'reports', 'data')
DOCS = os.path.join(REPO_ROOT, 'docs')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-09-06-rerender-probe.md')
for path in (REPO_ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import rerender_probe  # noqa: E402
import rerender_reduce  # noqa: E402


@pytest.fixture(scope='module')
def report():
    with open(REPORT, encoding='utf8') as f:
        return f.read()


@pytest.fixture(scope='module')
def prose(report):
    """The report with every run of whitespace collapsed, so a sentence pin does not depend on where the
    markdown happens to hard-wrap. Table rows are still matched against `report`, exactly."""
    return re.sub(r'\s+', ' ', report)


@pytest.fixture(scope='module')
def probe():
    with open(os.path.join(DATA, '2026-09-06-rerender-probe.json'), encoding='utf8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def meta():
    with open(os.path.join(DATA, '2026-09-06-rerender-photometa.json'), encoding='utf8') as f:
        return {r['pano_id']: r for r in json.load(f)}


@pytest.fixture(scope='module')
def rerendered(probe):
    return rerender_reduce.rerendered_records(probe)


@pytest.fixture(scope='module')
def same(probe):
    return [r for r in probe['records'] if not r['rerendered'] and 'error' not in r]


def classified(records):
    """The panoramas the pre-run classifier flagged as displaced."""
    return [r for r in records if r['label'] in ('shifted', 'warped')]


def by_movement(records, verdict):
    return [r for r in records if rerender_reduce.movement(r) == verdict]


def fit(record):
    return rerender_reduce.fit_pose(record)


def deg_v(record, px):
    return px * rerender_reduce.elevation_deg_per_px(record['height'])


def deg_h(record, px):
    return px * rerender_reduce.azimuth_deg_per_px(record['width'])


class TestTheTableIsTheArtifact:

    def test_every_cell_of_every_row_appears_in_the_markdown(self, report, probe, meta):
        """Regenerated, not spot-checked: `rerender_reduce` produced the table in the report, so this
        fails if either the artifact or the formatting moves without the markdown moving with it."""
        rows = rerender_reduce.rerendered_rows(probe, meta)
        assert len(rows) == 19
        for row in rows:
            cells = rerender_reduce.format_row(row)
            line = '| ' + ' | '.join(cells) + ' |'
            assert line in report, 'row for %s is not in the report as:\n%s' % (row['pano_id'], line)

    def test_the_header_is_the_one_the_reducer_prints(self, report):
        assert '| ' + ' | '.join(rerender_reduce.HEADER) + ' |' in report

    def test_the_table_is_ordered_by_the_column_it_shows(self, probe, meta):
        maes = [row['horizon_mae'] for row in rerender_reduce.rerendered_rows(probe, meta)]
        assert maes == sorted(maes, reverse=True)

    def test_the_artifacts_thresholds_are_the_definitions(self, probe):
        """The reducer reads windows locked under the probe's threshold and the pairs were split under the
        pilot's; if either definition moved after the run, the artifact would say so here."""
        assert probe['summary']['thresholds']['lock_peak'] == rerender_probe.LOCK_PEAK
        assert probe['summary']['thresholds']['rerendered_horizon_mae'] == rerender_probe.RERENDERED_HORIZON_MAE


class TestTheHeadlineSeparation:

    def test_the_group_sizes_are_the_artifacts(self, report, probe, rerendered, same):
        assert probe['summary']['measured'] == 78 and str(78) in report
        assert len(rerendered) == 19 and str(19) in report
        assert len(same) == 59 and str(59) in report

    def test_the_rerendered_share_is_the_artifacts(self, report, rerendered, same):
        share = 100.0 * len(rerendered) / (len(rerendered) + len(same))
        assert '%.1f%%' % share in report

    def test_the_null_ceiling_and_the_signal_floor_are_the_artifacts(self, report, rerendered, same):
        """The 45.6x gap is the whole argument that the instrument is not inventing the finding, and it
        is also the number the proposed fifth gate's threshold would sit inside - which is why docs/ops.md
        and CLAUDE.md quote it too, and are held to the artifact here as well."""
        ceiling = max(r['horizon']['mae'] for r in same)
        floor = min(r['horizon']['mae'] for r in rerendered)
        with open(os.path.join(DOCS, 'ops.md'), encoding='utf8') as f:
            ops = f.read()
        with open(os.path.join(REPO_ROOT, 'CLAUDE.md'), encoding='utf8') as f:
            claude_md = f.read()
        for text in (report, ops):
            assert '%.4f' % ceiling in text
            assert '%.4f' % floor in text
        for text in (report, ops, claude_md):
            assert '%.1f×' % (floor / ceiling) in text

    def test_the_null_really_is_flat(self, report, same):
        assert all(r['horizon']['shift']['max_abs_dy'] == 0 for r in same)
        assert all(r['horizon']['shift']['max_abs_dx'] == 0 for r in same)
        assert all(r['horizon']['shift']['n_locked'] == r['horizon']['shift']['n_windows'] for r in same)
        assert all(rerender_reduce.movement(r) == 'still' for r in same)

    def test_every_panorama_is_still_served_at_its_stored_frame(self, prose, probe, meta):
        """"Every one of the 78 is still served at its stored frame size" - the photometa artifact
        against the probe's, which read the dimensions off the files."""
        for r in probe['records']:
            assert (meta[r['pano_id']]['served_width'], meta[r['pano_id']]['served_height']) == \
                (r['width'], r['height']), r['pano_id']
        assert 'Every one of the 78 is still served at its stored frame size' in prose


class TestTheMovementFinding:
    """Item 1 of "Reading it": who moved, by the probe's own gate, and what the fit says it looked like."""

    def test_the_movement_split_is_the_artifacts(self, report, rerendered):
        still = by_movement(rerendered, 'still')
        modelled = by_movement(rerendered, 'modelled')
        unmodelled = by_movement(rerendered, 'unmodelled')
        assert (len(still), len(modelled), len(unmodelled)) == (4, 14, 1)
        assert '**15 of 19 moved; 14 of them with a camera pose the fit can name.**' in report
        assert 'Four show no displacement at all' in report
        assert '15 of these 19 moved' in report and 'It catches at most 15 of 19' in report

    def test_the_still_panoramas_really_did_not_move(self, report, rerendered):
        """Both halves of the claim: no locked window shifted by a pixel, and the fit finds nothing."""
        still = by_movement(rerendered, 'still')
        largest_window = max(r['horizon']['shift']['max_abs_sub'] for r in still)
        largest_component = max(rerender_reduce.displacement_px(fit(r)) for r in still)
        assert largest_window < 1.0 and '%.1f px' % largest_window in report
        assert largest_component < 0.03 and 'under **0.03 px**' in report

    def test_the_moved_panoramas_all_have_a_window_that_moved_by_a_pixel(self, report, rerendered):
        moved = by_movement(rerendered, 'modelled') + by_movement(rerendered, 'unmodelled')
        smallest = min(r['horizon']['shift']['max_abs_sub'] for r in moved)
        assert smallest >= 1.0 and 'at least one window shifted by **%.1f px** or more' % smallest in report

    def test_the_per_component_counts_and_maxima_are_the_artifacts(self, prose, rerendered):
        moved = by_movement(rerendered, 'modelled') + by_movement(rerendered, 'unmodelled')
        heading = [(r, abs(fit(r)['heading_px'])) for r in moved if abs(fit(r)['heading_px']) >= 1]
        vertical = [(r, abs(fit(r)['vertical_px'])) for r in moved if abs(fit(r)['vertical_px']) >= 1]
        tilt = [(r, fit(r)['tilt_px']) for r in moved if fit(r)['tilt_px'] >= 1]
        assert (len(heading), len(vertical), len(tilt)) == (11, 2, 6)
        assert 'at least 1 px on **11** of them, up to **%.1f px (%.3f°)**' % (
            max(px for _, px in heading), max(deg_h(r, px) for r, px in heading)) in prose
        assert 'at least 1 px on **2**, up to **%.1f px (%.3f°)**' % (
            max(px for _, px in vertical), max(deg_v(r, px) for r, px in vertical)) in prose
        assert 'at least 1 px on **six**, **%.1f to %.1f px (%.3f° to %.3f°)**' % (
            min(px for _, px in tilt), max(px for _, px in tilt),
            min(deg_v(r, px) for r, px in tilt), max(deg_v(r, px) for r, px in tilt)) in prose

    def test_the_unmodelled_panorama_is_described_from_the_artifact(self, report, rerendered):
        """The one row whose fit explains nothing: the residual really is larger than every component."""
        [record] = by_movement(rerendered, 'unmodelled')
        f = fit(record)
        shift = record['horizon']['shift']
        assert f['residual_px'] > rerender_reduce.displacement_px(f)
        assert record['pano_id'] == 'WfLuTvukUoZGf-ldOjry-Q' and record['pano_id'] in report
        assert 'moved on **%d of %d** locked windows by up to **%.1f px**' % (
            shift['n_locked_moved'], shift['n_locked'], shift['max_abs_sub']) in report
        assert 'residual **%.2f px**' % f['residual_px'] in report

    def test_the_fit_residual_range_on_the_modelled_rows_is_the_artifacts(self, report, rerendered):
        residuals = [fit(r)['residual_px'] for r in by_movement(rerendered, 'modelled')]
        assert '**%.2f to %.2f px** on the 14' % (min(residuals), max(residuals)) in report

    def test_removing_the_shift_halves_the_per_window_error(self, report, rerendered):
        """The evidence that the displacement is real rather than a correlation artefact, over the 15
        that moved (the first draft computed it over the classifier's 13, as 12.1 -> 6.7)."""
        moved = by_movement(rerendered, 'modelled') + by_movement(rerendered, 'unmodelled')
        before = statistics.median([w['mae_unshifted'] for r in moved
                                    for w in rerender_reduce.locked_windows(r)])
        after = statistics.median([w['mae_shifted'] for r in moved
                                   for w in rerender_reduce.locked_windows(r)])
        assert after < before / 1.5, 'the report claims the shift roughly halves the error'
        assert 'median **%.1f → %.1f** luma' % (before, after) in report


class TestTheClassifierAgainstTheFit:
    """The count the pre-run classifier gives, and exactly where it and the fit disagree."""

    def test_the_classifier_count_is_the_artifacts(self, report, rerendered):
        assert len(classified(rerendered)) == 13
        assert 'The classifier fixed before the run flags **13**' in report

    def test_the_disagreement_is_the_two_pure_tilts_and_the_unmodelled_row(self, report, rerendered):
        flagged = {r['pano_id'] for r in classified(rerendered)}
        modelled = {r['pano_id'] for r in by_movement(rerendered, 'modelled')}
        assert len(flagged & modelled) == 12 and 'agrees with the fit on **12** of the 14' in report
        assert modelled - flagged == {'TQEXMTPPTyPaAQkWyRm1-w', 'Qd6QYq567lGBJmPHsi5yVA'}
        assert flagged - modelled == {'WfLuTvukUoZGf-ldOjry-Q'}

    def test_the_two_missed_panoramas_are_pure_tilts_the_classifier_cannot_see(self, report, rerendered):
        """Each moved on at least half its locked windows (the gate passes), the fit puts about 1.2 px of
        tilt on it, and its sub-pixel median is under the 1 px the `shifted` test needs."""
        for pano_id, moved_text in (('TQEXMTPPTyPaAQkWyRm1-w', '**8 of 15**'),
                                    ('Qd6QYq567lGBJmPHsi5yVA', '**10 of 14**')):
            [r] = [r for r in rerendered if r['pano_id'] == pano_id]
            shift = r['horizon']['shift']
            assert rerender_probe.moved(shift)
            assert max(abs(shift['median_dy_sub']), abs(shift['median_dx_sub'])) < 1.0
            assert '%.1f' % fit(r)['tilt_px'] == '1.2' and 'pure tilts of **1.2 px**' in report
            assert '**%d of %d**' % (shift['n_locked_moved'], shift['n_locked']) == moved_text
            assert moved_text in report

    def test_the_vertical_offset_the_first_draft_dropped_is_in_the_report(self, report, rerendered):
        [r] = [r for r in rerendered if r['pano_id'] == '1hmsxmHRB-ieRY2WNN1n1g']
        f = fit(r)
        assert abs(f['vertical_px']) == rerender_reduce.displacement_px(f)
        assert 'a **%+.1f px** vertical offset' % f['vertical_px'] in report


class TestTheSharpenAndRegradeFinding:

    def test_the_sharpened_count_and_range_are_the_artifacts(self, report, rerendered):
        sharp = [r['horizon']['lap_ratio_gain_corrected'] for r in rerendered
                 if r['horizon']['lap_ratio_gain_corrected'] >= 1.2]
        assert len(sharp) == 13 and '**13 were sharpened and re-graded**' in report
        assert '%.1f' % min(sharp) in report and '%.1f' % max(sharp) in report

    def test_the_tone_range_is_computed_over_the_thirteen_it_is_attributed_to(self, report, rerendered):
        """The prose attributes the gain and offset range to the sharpened 13, so it is computed on that
        frame - not on all 19, which happens to give the same four numbers today and would not have to."""
        sharpened = [r for r in rerendered if r['horizon']['lap_ratio_gain_corrected'] >= 1.2]
        gains = [r['horizon']['affine']['gain'] for r in sharpened]
        offsets = [r['horizon']['affine']['offset'] for r in sharpened]
        assert 'contrast gain of **%.2f to %.2f**' % (min(gains), max(gains)) in report
        assert 'offset of **%+d to %+d** luma' % (round(min(offsets)), round(max(offsets))) in report

    def test_the_overlap_with_the_moved_panoramas_is_the_artifacts(self, report, rerendered):
        sharpened = {r['pano_id'] for r in rerendered if r['horizon']['lap_ratio_gain_corrected'] >= 1.2}
        moved = {r['pano_id'] for r in rerendered if rerender_reduce.movement(r) != 'still'}
        assert len(sharpened & moved) == 12 and '**12** of them are among the 15 that moved' in report
        assert sharpened - moved == {'HWvXpp4MJSQsOKUpEUhhFA'}
        assert moved - sharpened == {'1hmsxmHRB-ieRY2WNN1n1g', 'TQEXMTPPTyPaAQkWyRm1-w',
                                     'Qd6QYq567lGBJmPHsi5yVA'}


class TestTheNadirFinding:

    def test_the_2025_nadir_panoramas_are_the_artifacts(self, report, rerendered, meta):
        recent = [r for r in rerendered
                  if (meta[r['pano_id']].get('capture_date') or '').startswith('2025-09')]
        assert len(recent) == 3
        assert all(rerender_reduce.movement(r) == 'still' for r in recent)
        horizon = [r['horizon']['mae'] for r in recent]
        bottom = [r['bottom']['mae'] for r in recent]
        for value in (min(horizon), max(horizon), min(bottom), max(bottom)):
            assert '%.1f' % value in report
        signed = sorted(r['bottom']['mean_signed_diff'] for r in recent)
        assert '%.1f' % signed[0] in report, 'the negative nadir swing'
        assert '+%.1f' % signed[-1] in report, 'the positive nadir swing'

    def test_the_fourth_still_panorama_is_the_one_sharpened_without_moving(self, rerendered, meta):
        still = by_movement(rerendered, 'still')
        [other] = [r for r in still if not (meta[r['pano_id']].get('capture_date') or '').startswith('2025-09')]
        assert other['pano_id'] == 'HWvXpp4MJSQsOKUpEUhhFA'
        assert other['horizon']['lap_ratio_gain_corrected'] >= 1.2

    def test_the_bottom_band_lock_range_is_the_artifacts(self, report, rerendered):
        """The report's stated limit: the nadir finding rests on MAE, not on displacement, because the
        polar band is too smooth for phase correlation to lock. The denominator is not one number - a
        13312x6656 frame fits one row of eight windows there, a 16384x8192 frame two rows of eight."""
        locked = [len(rerender_reduce.locked_windows(r, 'bottom')) for r in rerendered]
        windows = sorted({r['bottom']['shift']['n_windows'] for r in rerendered})
        assert windows == [8, 16]
        assert '**%d to %d** of a band\'s **%d or %d** windows' % (min(locked), max(locked), *windows) in report


class TestTheGeometryClaim:

    def test_the_largest_displacements_are_the_artifacts(self, prose, rerendered):
        fits = [(r, fit(r)) for r in rerendered]
        tilt = max(deg_v(r, f['tilt_px']) for r, f in fits)
        heading = max(deg_h(r, abs(f['heading_px'])) for r, f in fits)
        vertical = max(deg_v(r, abs(f['vertical_px'])) for r, f in fits)
        assert ('The largest displacement here is **%.3f°** of tilt, **%.3f°** of heading and '
                '**%.3f°** of vertical offset.' % (tilt, heading, vertical)) in prose

    def test_the_click_noise_sigma_is_quoted_from_its_artifact(self, prose, rerendered):
        """The "third of one sigma" conclusion divides this report's maxima by another report's sigma;
        both halves are held to their artifacts, and the ratio to the fraction the prose claims."""
        with open(os.path.join(DATA, '2026-08-09-click-noise-summary.json'), encoding='utf8') as f:
            overall = json.load(f)['overall']
        sigma_az, sigma_el = overall['sigma_az_deg'], overall['sigma_el_deg']
        assert 'sigma at **%.2f°** azimuth and **%.2f°** elevation' % (sigma_az, sigma_el) in prose
        fits = [(r, fit(r)) for r in rerendered]
        worst = max(max(deg_h(r, abs(f['heading_px'])) / sigma_az,
                        deg_v(r, abs(f['vertical_px'])) / sigma_el,
                        deg_v(r, f['tilt_px']) / sigma_el) for r, f in fits)
        assert worst <= 1 / 3 and 'a third of one sigma' in prose


@pytest.fixture(scope='module')
def census():
    """The committed re-fetch census, whose `decay.pose_drift` block is the report's source."""
    with open(os.path.join(DATA, '2026-09-06-photometa-census.json'), encoding='utf8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def drift(census):
    return census['decay']['pose_drift']


class TestThePoseDriftSection:
    """The report's "how we would ever notice this again" numbers, against the committed block - and the
    committed block against a recomputation, so it cannot go stale if either census moves."""

    def test_the_committed_block_is_what_the_code_computes_today(self, census, drift):
        import pandas as pd

        import photometa_census

        frames = []
        for name in ('2026-08-09-photometa-census.json', '2026-09-06-photometa-census.json'):
            with open(os.path.join(DATA, name), encoding='utf8') as f:
                frames.append(pd.DataFrame(json.load(f)['records']))
        assert census['refetch_of'] == '2026-08-09-photometa-census.json'
        assert photometa_census.decay(*frames)['pose_drift'] == drift

    def test_the_population_and_the_drift_rate_are_the_artifacts(self, report, drift):
        assert str(drift['n']) in report
        assert str(drift['n_drifted_any_axis']) in report
        assert '%.2f%%' % drift['drifted_pct'] in report

    def test_the_per_axis_maxima_are_the_artifacts(self, report, drift):
        for axis in ('pitch_deg', 'roll_deg'):
            assert '%.4f' % drift['axes'][axis]['max_abs_deg'] in report

    def test_the_two_axes_moved_on_the_same_panoramas(self, report, drift):
        """The report leans on this: independent per-axis noise would not move exactly the same set, so
        the agreement is what makes it a re-stitch signal. Equal per-axis counts alone would pass two
        disjoint sets; equal counts that also equal the union across axes cannot, since
        |A| = |B| = |A u B| forces A = B."""
        changed = {axis: drift['axes'][axis]['n_changed'] for axis in ('pitch_deg', 'roll_deg')}
        assert changed['pitch_deg'] == changed['roll_deg'] == drift['n_changed_any_axis']
        assert 'the 95 that moved on roll are the same 95' in report
        assert str(changed['pitch_deg']) == '95'

    def test_the_heading_axis_is_absent_from_the_existing_baseline(self, drift):
        """The report says the two existing censuses can only be compared on pitch and roll. If a future
        pair does carry heading this fails, and that sentence needs rewriting rather than quietly aging."""
        assert drift['axes']['heading_deg'] is None

    def test_the_threshold_quoted_is_the_one_in_the_code(self, report, drift):
        import photometa_census

        assert drift['threshold_deg'] == photometa_census.POSE_DRIFT_DEG
        assert str(photometa_census.POSE_DRIFT_DEG) in report


class TestTheAgeClaim:

    def test_both_groups_span_the_same_years(self, report, rerendered, same, meta):
        def years(records):
            found = {(meta[r['pano_id']].get('capture_date') or '')[:4] for r in records}
            return min(found - {''}), max(found - {''})

        assert years(rerendered) == years(same), 'the report claims this is not an age effect'
        first, last = years(rerendered)
        assert '%s to %s' % (first, last) in report
