"""Figures for reports/2026-09-26-tilt-error-study.md (called from tilt_error_study.py analyze).

Two plots from committed data (the facade frame, the lean profiles) and two image figures that need
the gitignored pano / sheet caches (the rig-frame horizon, example adjudication sheets); the image
figures are skipped when their cache is absent, so `analyze` still runs from a fresh clone.
Palette: the dataviz skill's validated reference slots (blue #2a78d6, orange #eb6834) on its light
surface; one series per panel, so no legend boxes.
"""

import json
import os

import numpy as np
from PIL import Image, ImageDraw

import tilt_geometry as tg

BLUE, ORANGE = '#2a78d6', '#eb6834'
SURFACE, INK, INK2, GRID = '#fcfcfb', '#0b0b0b', '#52514e', '#e4e3df'
PREFIX = '2026-09-26-tilt-'


def _style(ax):
    ax.set_facecolor(SURFACE)
    for s in ('top', 'right'):
        ax.spines[s].set_visible(False)
    for s in ('left', 'bottom'):
        ax.spines[s].set_color(INK2)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def facade_figure(fac_table, fit, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    x = (fac_table['pitch_deg'] * np.cos(np.radians(fac_table['bearing_deg']))
         + fac_table['roll_deg'] * np.sin(np.radians(fac_table['bearing_deg']))).to_numpy()
    y = fac_table['elevation_deg'].to_numpy()
    fig, ax = plt.subplots(figsize=(5.2, 4.4), facecolor=SURFACE)
    _style(ax)
    ax.scatter(x, y, s=3, color=BLUE, alpha=0.25, linewidths=0)
    lim = np.nanpercentile(np.abs(np.r_[x, y]), 99.5)
    ax.plot([-lim, lim], [-lim, lim], color=INK2, linewidth=1, linestyle='--')
    ax.plot([-lim, lim], [0, 0], color=ORANGE, linewidth=2)
    ax.text(lim * 0.95, lim * 0.8, 'rig frame: slope 1', ha='right', color=INK2, fontsize=8)
    ax.text(lim * 0.95, lim * 0.08, 'gravity frame: slope 0', ha='right', color=INK2, fontsize=8)
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_xlabel('T(b) = pitch cos b + roll sin b at the facade bearing (deg)', color=INK, fontsize=9)
    ax.set_ylabel('facade-normal elevation (deg)', color=INK, fontsize=9)
    ax.set_title('F1: Seattle depth-artifact facades, n = {:,} on {:,} panos\nslopes {:.4f} / {:.4f}'.format(
        fit['n'], fit['n_clusters'], fit['beta_p'], fit['beta_r']), color=INK, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def lean_figure(panels, path):
    """panels: [(title, predicted, measured)] with pano-demeaned values."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, len(panels), figsize=(3.2 * len(panels), 3.4), facecolor=SURFACE, sharey=True)
    edges = np.linspace(-8, 8, 17)
    for ax, (title, pred, meas) in zip(np.atleast_1d(axes), panels):
        _style(ax)
        idx = np.digitize(pred, edges)
        cx, my = [], []
        for k in range(1, len(edges)):
            sel = idx == k
            if sel.sum() >= 20:
                cx.append(0.5 * (edges[k - 1] + edges[k]))
                my.append(float(np.mean(meas[sel])))
        ax.plot([-8, 8], [-8, 8], color=INK2, linewidth=1, linestyle='--')
        ax.plot([-8, 8], [0, 0], color=ORANGE, linewidth=2)
        ax.plot(cx, my, color=BLUE, linewidth=2, marker='o', markersize=4)
        ax.set_xlim(-8, 8)
        ax.set_ylim(-8, 8)
        ax.set_title(title, color=INK, fontsize=9)
        ax.set_xlabel('rig-aligned prediction (deg)', color=INK, fontsize=8)
    np.atleast_1d(axes)[0].set_ylabel('measured lean, binned mean (deg)', color=INK, fontsize=8)
    fig.suptitle('F2: vertical-edge lean vs the rig-aligned prediction (dashed = slope 1, orange = levelled)',
                 color=INK, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def horizon_figure(examples, path, width=2048):
    """examples: [(jpg path, pitch, roll, caption)]. Draws the straight rig horizon and the curve the
    gravity horizon follows in a rig-frame raster (el_rig = +T(b), F1's measured sign)."""
    tiles = []
    for p, pitch, roll, caption in examples:
        with Image.open(p) as im:
            im.draft('RGB', (width, width // 2))
            im = im.convert('RGB').resize((width, width // 2))
        d = ImageDraw.Draw(im)
        h = width // 2
        d.line([(0, h / 2), (width, h / 2)], fill=(255, 255, 255), width=2)
        b = np.linspace(-180, 180, 721)
        T = tg.tilt_term_deg(b, pitch, roll)
        xs, ys = tg.pixel_from_bearing_elevation(b, T, width, h)
        order = np.argsort(xs)
        d.line(list(zip(xs[order], ys[order])), fill=(235, 104, 52), width=4)
        d.rectangle([0, 0, width, 30], fill=(24, 24, 24))
        d.text((8, 8), caption, fill=(255, 255, 255))
        tiles.append(im)
    out = Image.new('RGB', (width, sum(t.size[1] for t in tiles)))
    y = 0
    for t in tiles:
        out.paste(t, (0, y))
        y += t.size[1]
    out.save(path, quality=85)


def sheet_figure(sheet_paths, captions, path):
    ims = [Image.open(p).convert('RGB') for p in sheet_paths]
    w = max(i.size[0] for i in ims)
    out = Image.new('RGB', (w, sum(i.size[1] + 24 for i in ims)), (24, 24, 24))
    d = ImageDraw.Draw(out)
    y = 0
    for im, cap in zip(ims, captions):
        d.text((12, y + 6), cap, fill=(255, 230, 0))
        out.paste(im, (0, y + 24))
        y += im.size[1] + 24
    out.thumbnail((1500, 10000))
    out.save(path, quality=80)


def make_all(summary, fac_table, lean_panels, figure_dir, horizon_examples=None, sheets=None):
    facade_figure(fac_table, summary['f1_depth_frame']['seattle']['fit'], os.path.join(figure_dir, PREFIX + 'facade-frame.png'))
    lean_figure(lean_panels, os.path.join(figure_dir, PREFIX + 'lean-profiles.png'))
    if horizon_examples:
        horizon_figure(horizon_examples, os.path.join(figure_dir, PREFIX + 'horizon-examples.jpg'))
    if sheets:
        sheet_figure(sheets[0], sheets[1], os.path.join(figure_dir, PREFIX + 'adjudication-sheet.jpg'))
