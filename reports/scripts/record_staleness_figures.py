"""Figures for the record-staleness study. Everything reads committed bytes (the summary JSON and
the per-city repair CSVs), so the figures regenerate offline:

    python reports/scripts/record_staleness_figures.py
        -> reports/figures/2026-08-10-record-staleness-monthly.png      (fig A)
        -> reports/figures/2026-08-10-record-staleness-classes.png     (fig B)
        -> reports/figures/2026-08-10-record-staleness-scatter.png     (fig C)
        -> reports/figures/2026-08-10-record-staleness-severity.png    (fig D)

Months with fewer than MIN_N labels are dropped from fig A: a 3-label month is sampling noise,
not a client-behaviour signal.
"""

import glob
import gzip
import json
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.dirname(HERE)
DATE = '2026-08-10'
SUMMARY = os.path.join(REPORTS, 'data', f'{DATE}-record-staleness-summary.json')
REPAIRS_GLOB = os.path.join(REPORTS, 'data', f'{DATE}-repairs-*.csv.gz')
FIGDIR = os.path.join(REPORTS, 'figures')

MIN_N = 30
INK, MUTED, GRID, SPINE = '#0b0b0b', '#52514e', '#eceae6', '#d8d6d1'
LINE = '#2a78d6'
WINDOW = ('2023-03-29', '2024-09-26')  # evolution 179 deploy -> 7.20.7 deploy
CLASS_COLORS = {  # miss classes only; 'exact' never plotted
    'x_only': '#2a78d6', 'multi_field': '#d1495b', 'xy_small': '#8d99ae',
    'zoom_desync': '#e28f41', 'dpr2': '#7b5ea7', 'frame_change': '#3f9b7c',
}
CLASS_ORDER = ['x_only', 'multi_field', 'xy_small', 'zoom_desync', 'dpr2', 'frame_change']


def _style(ax):
    ax.grid(True, axis='y', color=GRID, linewidth=0.8, zorder=0)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        ax.spines[side].set_color(SPINE)
    ax.tick_params(colors=MUTED, labelsize=8)


def _save(fig, name):
    out = os.path.join(FIGDIR, name)
    fig.savefig(out, dpi=160)
    print(f'wrote {out}')


def _title(fig, title, subtitle_lines, plot_top):
    """Left-aligned bold title with a gray subtitle under it (pre-wrapped lines), and the axes
    laid out below both. tight_layout has already been applied by the caller via rect."""
    fig.suptitle(title, fontsize=12, color=INK, x=0.02, y=0.985, ha='left')
    fig.text(0.02, plot_top, '\n'.join(subtitle_lines), fontsize=8.5, color=MUTED, va='top')


def fig_monthly(summary):
    """Fig A: monthly share of labels >= 10 Validate px off, per city, bug window shaded."""
    cities = summary['cities']
    fig, axes = plt.subplots(2, 4, figsize=(13, 5.2), sharex=True, sharey=True)
    fig.patch.set_facecolor('white')
    for ax, (city, d) in zip(axes.ravel(), sorted(cities.items())):
        rows = [(pd.Timestamp(m + '-15'), v['pct_ge_10px'])
                for m, v in sorted(d['monthly_ge10px'].items()) if v['n'] >= MIN_N]
        if rows:
            t, y = zip(*rows)
            ax.plot(t, y, color=LINE, linewidth=2, zorder=3)
        ax.axvspan(pd.Timestamp(WINDOW[0]), pd.Timestamp(WINDOW[1]), color='#e8e6e1', zorder=1)
        ax.set_title(city, fontsize=10, color=INK, loc='left')
        _style(ax)
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    axes[0, 0].annotate('bug window\n(evo 179 → 7.20.7)', xy=(0.05, 0.72),
                        xycoords='axes fraction', fontsize=8, color=MUTED, ha='left')
    fig.supylabel('% of labels ≥ 10 px off', fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0.01, 0, 1, 0.88))
    _title(fig, 'Mis-rendered labels are the bug window: monthly share ≥ 10 px off in Validate',
           ['Per month: % of post-179 labels whose stored viewport record misses the stored '
            'pano_x/pano_y by ≥ 10 Validate-canvas px',
            f"(the record is what Validate renders). Months with <{MIN_N} labels dropped. "
            f"rawLabels fetched {summary['fetched']}."], 0.945)
    _save(fig, f'{DATE}-record-staleness-monthly.png')


def fig_classes(summary):
    """Fig B: what the in-window misses are, per city (share of in-window labels by class)."""
    cities = sorted(summary['cities'].items())
    fig, ax = plt.subplots(figsize=(9.5, 4.6))
    fig.patch.set_facecolor('white')
    xs = np.arange(len(cities))
    bottom = np.zeros(len(cities))
    for klass in CLASS_ORDER:
        vals = []
        for _, d in cities:
            counts = d['in_window']['class_counts']
            total = sum(counts.values())
            vals.append(100.0 * counts.get(klass, 0) / total if total else 0.0)
        vals = np.asarray(vals)
        ax.bar(xs, vals, bottom=bottom, color=CLASS_COLORS[klass], label=klass, zorder=3,
               width=0.62)
        bottom += vals
    ax.set_xticks(xs, [c for c, _ in cities], fontsize=9)
    _style(ax)
    ax.legend(frameon=False, fontsize=8.5, ncols=3, loc='upper left')
    fig.supylabel('% of in-window labels', fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0.01, 0, 1, 0.86))
    _title(fig, 'What the in-window record misses are, per city',
           ['% of in-window (2023-03-29 → 2024-09-25) labels whose stored record does not '
            'reproduce pano_x/pano_y,',
            f"by first matching explanation. rawLabels fetched {summary['fetched']}."], 0.935)
    _save(fig, f'{DATE}-record-staleness-classes.png')


def fig_scatter(summary):
    """Fig C: the miss residuals themselves — heading staleness is a horizontal phenomenon."""
    pts = summary.get('scatter_sample', [])
    fig, ax = plt.subplots(figsize=(7.2, 5.4))
    fig.patch.set_facecolor('white')
    for klass in CLASS_ORDER:
        sub = [(p['dx'], p['dy']) for p in pts if p['k'] == klass]
        if not sub:
            continue
        x, y = zip(*sub)
        ax.scatter(x, y, s=6, alpha=0.45, color=CLASS_COLORS[klass], label=klass, zorder=3,
                   linewidths=0)
    lim = 12
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.axhline(0, color=SPINE, linewidth=0.8, zorder=1)
    ax.axvline(0, color=SPINE, linewidth=0.8, zorder=1)
    ax.set_xlabel('heading residual dx (°)', fontsize=9, color=MUTED)
    ax.set_ylabel('elevation residual dy (°)', fontsize=9, color=MUTED)
    _style(ax)
    ax.grid(True, axis='x', color=GRID, linewidth=0.8, zorder=0)
    ax.legend(frameon=False, fontsize=8.5, loc='upper left')
    fig.tight_layout(rect=(0.01, 0, 1, 0.84))
    _title(fig, 'In-window record misses live on the heading axis',
           ['Stored-minus-replay residual per label (deterministic 1-in-k sample of in-window '
            'misses, all cities pooled;',
            'axes clipped to ±12°). x_only rows sit on dy = 0 by construction — the point is how '
            'much of everything',
            f"else does too. rawLabels fetched {summary['fetched']}."], 0.93)
    _save(fig, f'{DATE}-record-staleness-scatter.png')


def fig_severity(summary):
    """Fig D: how visible the misses are in Validate (CDF of Validate-px error, by class), with
    the repair outcome stated on the figure."""
    frames = [pd.read_csv(gzip.open(p, 'rt')) for p in sorted(glob.glob(REPAIRS_GLOB))]
    rep = pd.concat(frames, ignore_index=True)
    fig, ax = plt.subplots(figsize=(8.2, 4.8))
    fig.patch.set_facecolor('white')
    for klass in CLASS_ORDER:
        px = np.sort(rep.loc[rep['klass'] == klass, 'old_validate_px'].to_numpy(float))
        px = px[np.isfinite(px)]
        if len(px) < 20:
            continue
        ax.plot(px, 100.0 * np.arange(1, len(px) + 1) / len(px), color=CLASS_COLORS[klass],
                linewidth=2, label=f'{klass} (n={len(px):,})', zorder=3)
    for tier, name in ((4, 'perceptible'), (10, 'clearly off'), (30, 'far off')):
        ax.axvline(tier, color=SPINE, linewidth=0.8, zorder=1)
        ax.annotate(f'{tier}px\n{name}', xy=(tier, 3), fontsize=7.5, color=MUTED, ha='left')
    ax.set_xscale('log')
    ax.set_xlim(1, 2000)
    ax.set_ylim(0, 100)
    ax.set_xlabel('Validate-canvas error of the stored record (px, log scale)', fontsize=9,
                  color=MUTED)
    pct = [summary['cities'][c]['repair'].get('pct_repaired') for c in summary['cities']]
    n_rep = sum(summary['cities'][c]['repair'].get('n', 0) for c in summary['cities'])
    rng = f'{min(pct):g}%' if min(pct) == max(pct) else f'{min(pct):g}–{max(pct):g}%'
    ax.annotate(f'after repair from pano_x/y:\n{rng} of {n_rep:,} rows reproduce\n'
                'stored pano_x/y within 1 px', xy=(0.985, 0.06),
                xycoords='axes fraction', fontsize=8.5, color=INK, ha='right')
    _style(ax)
    ax.legend(frameon=False, fontsize=8.5, loc='upper left')
    fig.supylabel('cumulative % of misses', fontsize=9, color=MUTED)
    fig.tight_layout(rect=(0.01, 0, 1, 0.85))
    _title(fig, 'How far off the stored record renders, and what repair does',
           ['CDF of per-label Validate-canvas error over in-window record misses, all cities '
            'pooled, by class (small-angle',
            'center-of-canvas conversion at the stored zoom — floor estimates). '
            f"rawLabels fetched {summary['fetched']}."], 0.94)
    _save(fig, f'{DATE}-record-staleness-severity.png')


def main():
    with open(SUMMARY) as f:
        summary = json.load(f)
    fig_monthly(summary)
    fig_classes(summary)
    fig_scatter(summary)
    fig_severity(summary)


if __name__ == '__main__':
    main()
