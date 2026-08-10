"""Figure for the era-replay study: post-179 monthly pano_y replay agreement, one panel per city,
with the placement-record bug window shaded. Reads the committed summary JSON, so the figure
regenerates offline from committed bytes:

    python era_replay_figure.py            # -> reports/figures/2026-08-09-era-replay-monthly.png

Months with fewer than MIN_N post-179 labels are dropped: a 3-label month at 67% is sampling
noise, not a client behaviour signal.
"""

import json
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.dirname(HERE)
SUMMARY = os.path.join(REPORTS, 'data', '2026-08-09-era-replay-summary.json')
OUT = os.path.join(REPORTS, 'figures', '2026-08-09-era-replay-monthly.png')

MIN_N = 30
LINE = '#2a78d6'
WINDOW = ('2023-03-29', '2024-09-26')  # evolution 179 deploy -> 7.20.7 deploy
INK, MUTED = '#0b0b0b', '#52514e'


def main():
    with open(SUMMARY) as f:
        summary = json.load(f)
    cities = summary['cities']

    series = {}
    for city, d in cities.items():
        series[city] = [(pd.Timestamp(m + '-15'), v['exact_y_pct'])
                        for m, v in sorted(d['post179_monthly'].items())
                        if v['n'] >= MIN_N and v['exact_y_pct'] is not None]
    # The axis floor comes from the data: clipping a dip mid-air would misstate its depth.
    y_min = min((y for rows in series.values() for _, y in rows), default=80)
    y_floor = 5 * ((y_min - 3) // 5)

    fig, axes = plt.subplots(2, 3, figsize=(10.5, 5.2), sharex=True, sharey=True)
    fig.patch.set_facecolor('white')
    for ax, (city, rows) in zip(axes.ravel(), sorted(series.items())):
        if rows:
            t, y = zip(*rows)
            ax.plot(t, y, color=LINE, linewidth=2, zorder=3)
        ax.axvspan(pd.Timestamp(WINDOW[0]), pd.Timestamp(WINDOW[1]),
                   color='#e8e6e1', zorder=1)
        ax.set_title(city, fontsize=10, color=INK, loc='left')
        ax.set_ylim(y_floor, 101.5)
        ax.grid(True, axis='y', color='#eceae6', linewidth=0.8, zorder=0)
        for side in ('top', 'right'):
            ax.spines[side].set_visible(False)
        for side in ('left', 'bottom'):
            ax.spines[side].set_color('#d8d6d1')
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))

    axes[0, 0].annotate('bug window\n(evo 179 → 7.20.7)', xy=(0.18, 0.12),
                        xycoords='axes fraction', fontsize=8, color=MUTED, ha='left')
    fig.suptitle('pano_y replays exactly — except while the 2023-04→2024-09 client was live',
                 fontsize=12, color=INK, x=0.02, ha='left')
    fig.text(0.02, 0.93, 'Monthly % of post-179 labels whose stored pano_y is reproduced '
             f'bit-for-bit by the replay math; months with <{MIN_N} labels dropped. '
             f"rawLabels fetched {summary['fetched']}.",
             fontsize=8.5, color=MUTED)
    fig.supylabel('exact pano_y replay (%)', fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0.01, 0, 1, 0.9))
    fig.savefig(OUT, dpi=160)
    print(f'wrote {OUT}')


if __name__ == '__main__':
    main()
