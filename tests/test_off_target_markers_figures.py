"""Figure-layer tests for the off-target-markers study.

The figure module had no tests, which is how a city with nothing to repair — 31 of the 54 in the
committed all-cities summary — reached `min()` as a bare None. These drive the two functions whose
inputs are shaped by the *number* of cities and by cities with empty repair scope, since both grow
whenever the study is pointed at a wider corpus than the eight it was written against.
"""

import gzip
import json
import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'reports', 'scripts'))

import off_target_markers_figures as fg  # noqa: E402


def _city(pct_repaired=100.0, n_repair=12, months=3):
    """One city block shaped like summarize()'s output. pct_repaired=None models a city whose
    repair scope was empty, which repair_frame() reports as {'n': 0} with no pct_repaired key."""
    repair = {'n': 0} if pct_repaired is None else {'n': n_repair, 'pct_repaired': pct_repaired}
    return {
        'repair': repair,
        'monthly_ge10px': {f'2023-{m:02d}': {'n': 50, 'pct_ge_10px': 4.0 + m}
                           for m in range(1, months + 1)},
        'scatter': [],
    }


def _summary(cities):
    return {'fetched': '2026-08-10', 'cities': cities}


@pytest.fixture
def repairs_dir(tmp_path, monkeypatch):
    """A one-row repairs CSV in the shape fig_severity concatenates, pointed at by REPAIRS_GLOB."""
    d = tmp_path / 'data'
    d.mkdir()
    rep = pd.DataFrame({'label_id': [1, 2], 'klass': ['x_only', 'x_only'],
                        'old_validate_px': [12.0, 40.0]})
    with gzip.open(d / 'repairs-somewhere.csv.gz', 'wt', newline='') as f:
        rep.to_csv(f, index=False)
    monkeypatch.setattr(fg, 'REPAIRS_GLOB', str(d / 'repairs-*.csv.gz'))
    figdir = tmp_path / 'figures'
    figdir.mkdir()
    monkeypatch.setattr(fg, 'FIGDIR', str(figdir))
    return figdir


class TestSeverityHandlesEmptyRepairScope:

    def test_a_city_with_no_repair_scope_does_not_crash_the_figure(self, repairs_dir):
        """The committed all-cities summary has 31 of 54 cities with an empty repair scope, so
        `pct_repaired` is absent and .get() yields None. min() over that list is a TypeError."""
        summary = _summary({'aa': _city(100.0), 'bb': _city(None), 'cc': _city(97.5)})
        fg.fig_severity(summary)
        assert (repairs_dir / f'{fg.DATE}-off-target-markers-severity.png').exists()

    def test_the_stated_range_covers_only_the_cities_that_had_scope(self, repairs_dir):
        """A city with no rows to repair must not be read as 0% repaired — it is undefined, and
        folding it in as a zero would understate the repair rate the figure annotates."""
        summary = _summary({'aa': _city(100.0), 'bb': _city(None), 'cc': _city(96.0)})
        text = _annotation_text(summary)
        assert '96–100%' in text, text
        assert '0–100%' not in text

    def test_every_city_lacking_scope_is_still_not_a_crash(self, repairs_dir):
        """The degenerate end: nothing anywhere had repair scope. The figure must still render."""
        summary = _summary({'aa': _city(None), 'bb': _city(None)})
        fg.fig_severity(summary)
        assert (repairs_dir / f'{fg.DATE}-off-target-markers-severity.png').exists()


def _annotation_text(summary):
    """Build the figure once and read every annotation off the live axes."""
    import matplotlib.pyplot as plt
    fg.fig_severity(summary)
    fig = plt.gcf()
    return ' '.join(t.get_text() for ax in fig.axes for t in ax.texts)


class TestMonthlyGridSizesToTheCorpus:

    def test_more_than_eight_cities_are_all_plotted(self, repairs_dir):
        """The grid was a hardcoded 2x4 zipped against the city dict, so city 9 onward vanished
        with no warning — and this PR commits a 54-city summary."""
        cities = {f'city-{i:02d}': _city(100.0) for i in range(12)}
        fg.fig_monthly(_summary(cities))
        import matplotlib.pyplot as plt
        fig = plt.gcf()
        titled = [ax.get_title(loc='left') for ax in fig.axes]
        for name in cities:
            assert name in titled, f'{name} was dropped from the figure'

    def test_fewer_cities_leave_no_empty_panel(self, repairs_dir):
        """An empty styled frame reads as 'no misses' rather than 'no city'."""
        cities = {f'city-{i:02d}': _city(100.0) for i in range(3)}
        fg.fig_monthly(_summary(cities))
        import matplotlib.pyplot as plt
        fig = plt.gcf()
        visible = [ax for ax in fig.axes if ax.get_visible()]
        assert len(visible) == 3
