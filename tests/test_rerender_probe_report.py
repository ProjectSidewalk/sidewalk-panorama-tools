"""Every number in reports/2026-09-06-rerender-probe.md, re-derived from the committed artifacts.

The repo's convention: a report table is the one place a plausible number has no compiler and no test, and
hand-typed counts in an earlier report were wrong by 2x and 6x with nothing about the sentences looking
different for it. This one was drafted from a table pasted into a GitHub comment, where a per-window median
had been transcribed as 12.5 against the 12.1 the reduction actually produces - so the failure mode is not
hypothetical here, it already happened once on this very finding.

The table is not spot-checked: every cell is regenerated through `rerender_reduce.format_row`, which is the
same function the report was generated with, so the report and the artifact cannot drift apart without this
failing.
"""

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
DATA = os.path.join(REPO_ROOT, 'reports', 'data')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-09-06-rerender-probe.md')
for path in (REPO_ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import rerender_reduce  # noqa: E402


@pytest.fixture(scope='module')
def report():
    with open(REPORT, encoding='utf8') as f:
        return f.read()


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
    return [r for r in probe['records'] if r['rerendered'] and 'error' not in r]


@pytest.fixture(scope='module')
def same(probe):
    return [r for r in probe['records'] if not r['rerendered'] and 'error' not in r]


def displaced(records):
    return [r for r in records if r['label'] in ('shifted', 'warped')]


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

    def test_the_table_is_ordered_by_the_column_it_shows(self, probe, meta):
        maes = [row['horizon_mae'] for row in rerender_reduce.rerendered_rows(probe, meta)]
        assert maes == sorted(maes, reverse=True)


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
        is also the number the proposed fifth gate's threshold would sit inside."""
        ceiling = max(r['horizon']['mae'] for r in same)
        floor = min(r['horizon']['mae'] for r in rerendered)
        assert '%.4f' % ceiling in report
        assert '%.4f' % floor in report
        assert '%.1f' % (floor / ceiling) in report

    def test_the_null_really_is_flat(self, report, same):
        assert all(r['horizon']['shift']['max_abs_dy'] == 0 for r in same)
        assert all(r['horizon']['shift']['max_abs_dx'] == 0 for r in same)
        assert all(r['horizon']['shift']['n_locked'] == r['horizon']['shift']['n_windows'] for r in same)


class TestTheReposeFinding:

    def test_the_count_of_re_posed_panoramas_is_the_artifacts(self, report, rerendered):
        assert len(displaced(rerendered)) == 13 and '13' in report

    def test_the_largest_heading_and_tilt_are_the_artifacts(self, report, rerendered):
        rows = displaced(rerendered)
        heading_px = max(abs(rerender_reduce.heading_offset_px(r)) for r in rows)
        heading_deg = max(abs(rerender_reduce.heading_offset_px(r)) *
                          rerender_reduce.degrees_per_pixel(r['width']) for r in rows)
        tilt_px = max(rerender_reduce.tilt_amplitude_px(r) for r in rows)
        tilt_deg = max(rerender_reduce.tilt_amplitude_px(r) *
                       rerender_reduce.degrees_per_pixel(r['width']) for r in rows)
        for value, spec in ((heading_px, '.1f'), (heading_deg, '.3f'), (tilt_px, '.1f'), (tilt_deg, '.3f')):
            assert format(value, spec) in report, '%s is not in the report' % format(value, spec)

    def test_removing_the_shift_halves_the_per_window_error(self, report, rerendered):
        """The 12.1 -> 6.7 pair is the evidence that the displacement is real rather than a correlation
        artefact, and it is the number the GitHub comment got wrong."""
        import statistics
        rows = displaced(rerendered)
        before = statistics.median([w['mae_unshifted'] for r in rows
                                    for w in rerender_reduce.locked_windows(r)])
        after = statistics.median([w['mae_shifted'] for r in rows
                                   for w in rerender_reduce.locked_windows(r)])
        assert after < before / 1.5, 'the report claims the shift roughly halves the error'
        assert '%.1f' % before in report and '%.1f' % after in report


class TestTheSharpenAndRegradeFinding:

    def test_the_sharpened_count_and_range_are_the_artifacts(self, report, rerendered):
        sharp = [r['horizon']['lap_ratio_gain_corrected'] for r in rerendered
                 if r['horizon']['lap_ratio_gain_corrected'] >= 1.2]
        assert len(sharp) == 13 and '13' in report
        assert '%.1f' % min(sharp) in report and '%.1f' % max(sharp) in report

    def test_the_tone_range_is_the_artifacts(self, report, rerendered):
        gains = [r['horizon']['affine']['gain'] for r in rerendered]
        offsets = [r['horizon']['affine']['offset'] for r in rerendered]
        assert '%.2f' % min(gains) in report and '%.2f' % max(gains) in report
        assert '+%d' % round(min(offsets)) in report and '+%d' % round(max(offsets)) in report


class TestTheNadirFinding:

    def test_the_no_displacement_group_is_the_artifacts(self, report, rerendered):
        assert len(rerendered) - len(displaced(rerendered)) == 6 and 'Six' in report

    def test_the_2025_nadir_panoramas_are_the_artifacts(self, report, rerendered, meta):
        recent = [r for r in rerendered
                  if (meta[r['pano_id']].get('capture_date') or '').startswith('2025-09')]
        assert len(recent) == 3
        horizon = [r['horizon']['mae'] for r in recent]
        bottom = [r['bottom']['mae'] for r in recent]
        for value in (min(horizon), max(horizon), min(bottom), max(bottom)):
            assert '%.1f' % value in report
        signed = sorted(r['bottom']['mean_signed_diff'] for r in recent)
        assert '%.1f' % signed[0] in report, 'the negative nadir swing'
        assert '+%.1f' % signed[-1] in report, 'the positive nadir swing'

    def test_the_bottom_band_lock_range_is_the_artifacts(self, report, rerendered):
        """The report's stated limit: the nadir finding rests on MAE, not on displacement, because the
        polar band is too smooth for phase correlation to lock."""
        locked = [len(rerender_reduce.locked_windows(r, 'bottom')) for r in rerendered]
        assert '%d to %d of 16' % (min(locked), max(locked)) in report


class TestTheAgeClaim:

    def test_both_groups_span_the_same_years(self, report, rerendered, same, meta):
        def years(records):
            found = {(meta[r['pano_id']].get('capture_date') or '')[:4] for r in records}
            return min(found - {''}), max(found - {''})

        assert years(rerendered) == years(same), 'the report claims this is not an age effect'
        first, last = years(rerendered)
        assert '%s to %s' % (first, last) in report
