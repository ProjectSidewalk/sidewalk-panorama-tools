"""Record-staleness study: decompose the in-window canvas/POV record misses, price what Validate
shows, and measure how much of the record is repairable from pano_x/pano_y.

Successor to the era replay study (reports/2026-08-09-era-replay-study.md), which established the
bug window (evolution 179 -> SidewalkWebpage 7.20.7, 2023-03-29 -> 2024-09-25), proved stored
pano_x/pano_y is click-time truth, and decomposed the *pano_y* misses. This study answers the three
questions it left open, for SidewalkWebpage#4842 (also #2478, #1529):

1. What are the in-window *pano_x* misses? (The x axis is where most of the staleness lives.)
2. What does the bug look like to a user? Validate, Gallery, and the label-detail views re-derive
   the label's direction from the *record* side (heading/pitch/zoom + canvas_x/y at 720x480 --
   SidewalkWebpage validate/src/label/Label.getOriginalPov -> PanoManager.renderPanoMarker), i.e.
   from exactly the fields that went stale, so every record miss is a mis-rendered label.
3. Can the record be repaired? pano_x/pano_y is click-time truth, so a self-consistent record can
   be re-solved from it per label.

Everything below is a pure function of rawLabels columns; no network. The replay itself is
era_replay_study.replay_frame (the verbatim production projection via pov_replay).

Usage:
    python reports/scripts/record_staleness_study.py reports/scripts/.cache/rawlabels \\
        --fetched <date> --write reports/data/<date>-record-staleness-summary.json \\
        --repairs-dir reports/data
"""

import argparse
import glob
import gzip
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import era_replay_study  # noqa: E402
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402

# Known GSV equirect generations (heights; widths are always 2x). Used only to *recognize* a
# frame-change row -- a record whose pano coordinate lives in a previous serving of the pano.
GENERATION_HEIGHTS = (1664.0, 3328.0, 6656.0, 8192.0)

# Zoom candidates for the desync refit: the three UI levels plus the fractional plateaus GSV's
# animated zoom actually parks at (the #2478 signature -- a 1.999 that truncated to 1 on store).
ZOOM_CANDIDATES = (1.0, 1.999, 2.0, 2.999, 3.0)

# A record reproduces pano_x/pano_y when both axes land within one pixel -- pano_x/y are stored
# rounded, and the round-trip through a repaired record re-rounds.
MATCH_TOL_PX = 1.0

# Validate-canvas visibility tiers (px on the 720x480 frame): barely perceptible / clearly off /
# nowhere near the target.
PX_TIERS = (4.0, 10.0, 30.0)

# The x-only class is heading-shift-shaped by construction; batch staleness and camera_heading
# drift are separated statistically (drift is per-pano-constant; staleness varies per label and
# vanishes post-fix). Same-POV groups sharing one dx to this tolerance are counted as moving
# together -- rounding puts ~0.011 deg of noise on a 16384-px pano, so 0.05 deg is 4-sigma clear.
SHARED_DX_TOL_DEG = 0.05


def _replay_residuals(df, canvas_x, canvas_y, zoom):
    """Replay the frame with substituted canvas/zoom inputs; returns (|dx|, |dy|) in pixels
    against the stored pano_x/pano_y (NaN where not replayable). Used by the classification
    cascade to test 'would this alternative input reproduce the stored coordinate'."""
    pov_h, pov_p = pov_replay.pov_if_centered(
        canvas_x, canvas_y, df['heading'], df['pitch'], zoom,
        df['canvas_width'], df['canvas_height'])
    w = np.asarray(df['pano_width'], float)
    h = np.asarray(df['pano_height'], float)
    cam = np.asarray(df['camera_heading'], float)
    ok = np.isfinite(pov_h) & np.isfinite(pov_p) & np.isfinite(cam) & np.isfinite(w) & (w > 0)
    safe = lambda a, fill: np.where(np.isfinite(a), a, fill)
    rx, ry = pov_replay.pano_xy_from_pov(safe(pov_h, 0.0), safe(pov_p, 0.0),
                                         safe(cam, 0.0), safe(w, 8192.0), safe(h, 4096.0))
    adx = np.abs(era_replay_study.wrapped_dx(df['pano_x'], rx, safe(w, 8192.0)))
    ady = np.abs(np.asarray(df['pano_y'], float) - ry)
    return np.where(ok, adx, np.nan), np.where(ok, ady, np.nan)


def _implied_height(df):
    """The pano height the stored pano_y actually lives in, given the record's own pov_pitch:
    inverting pano_y = h/2 - (h/2) * pov_pitch / 90. A row whose implied height is a *different*
    generation than the served one is a frame-change row (the pano was re-served at a new
    resolution after the click)."""
    pov_h, pov_p = pov_replay.pov_if_centered(
        df['canvas_x'], df['canvas_y'], df['heading'], df['pitch'], df['zoom'],
        df['canvas_width'], df['canvas_height'])
    denom = 1.0 - np.asarray(pov_p, float) / 90.0
    with np.errstate(divide='ignore', invalid='ignore'):
        return 2.0 * np.asarray(df['pano_y'], float) / denom


def classify(df):
    """Per-label classification of replay agreement, as a 'klass' column. Cascade order matters:
    each label takes the FIRST explanation that reproduces its stored pano_x/pano_y.

    exact         both axes replay within tolerance as stored
    dpr2          halving the stored canvas offsets reproduces both axes (device-pixel canvas
                  recorded for a CSS-pixel frame; the era study's per-user s = 0.5 cohort)
    frame_change  the stored coordinate lives in a previous pano generation; the record itself is
                  self-consistent with the click, only the served frame moved (a pano_x/y-consumer
                  concern, NOT a record error -- Validate renders these correctly)
    zoom_desync   replaying with a different zoom level reproduces both axes (the stored zoom is
                  not the zoom the click was projected with; #2478's truncation signature)
    x_only        pano_y exact, pano_x off: heading-shift-shaped (batch staleness or, at drift
                  scale, camera_heading refresh; separated statistically, not per label)
    xy_small      both axes off by <= 10 px: the era study's residual per-label jitter scale
    multi_field   both axes off, beyond jitter scale: several record fields stale at once
    unreplayable  a replay input (camera_heading, dims, canvas/POV) is missing
    """
    out = df.copy()
    klass = np.full(len(df), 'unreplayable', dtype=object)
    both = df['replayable_x'].to_numpy() & df['replayable_y'].to_numpy()
    adx = np.abs(df['dx'].to_numpy(float))
    ady = np.abs(df['dy'].to_numpy(float))
    hit = both & (adx <= MATCH_TOL_PX) & (ady <= MATCH_TOL_PX)
    klass[hit] = 'exact'
    todo = both & ~hit

    # dpr2: the click's offsets from canvas center were recorded doubled (device pixels about the
    # CSS center); halving the offsets recovers the click. Verified center-scaled, not
    # origin-scaled, on the era study's per-user s = 0.5 cohort.
    if todo.any():
        adx2, ady2 = _replay_residuals(df, 360.0 + (df['canvas_x'] - 360.0) * 0.5,
                                       240.0 + (df['canvas_y'] - 240.0) * 0.5, df['zoom'])
        m = todo & (adx2 <= MATCH_TOL_PX) & (ady2 <= MATCH_TOL_PX)
        klass[m] = 'dpr2'
        todo &= ~m

    # frame_change: implied height is a known generation other than the served one, and the full
    # replay in that frame reproduces both axes.
    if todo.any():
        implied = _implied_height(df)
        for gen_h in GENERATION_HEIGHTS:
            cand = todo & (np.abs(implied - gen_h) <= 2.0) & \
                (np.asarray(df['pano_height'], float) != gen_h)
            if not cand.any():
                continue
            sub = df.loc[cand].copy()
            sub['pano_width'] = gen_h * 2.0
            sub['pano_height'] = gen_h
            adx_g, ady_g = _replay_residuals(sub, sub['canvas_x'], sub['canvas_y'], sub['zoom'])
            ok = np.zeros(len(df), bool)
            ok[np.flatnonzero(cand)] = (adx_g <= MATCH_TOL_PX) & (ady_g <= MATCH_TOL_PX)
            klass[ok] = 'frame_change'
            todo &= ~ok

    # zoom_desync: some other zoom level projects the stored click onto the stored coordinate.
    if todo.any():
        fitted = np.full(len(df), np.nan)
        for z in ZOOM_CANDIDATES:
            adx_z, ady_z = _replay_residuals(df, df['canvas_x'], df['canvas_y'], z)
            m = todo & ~np.isclose(df['zoom'].to_numpy(float), z) & \
                (adx_z <= MATCH_TOL_PX) & (ady_z <= MATCH_TOL_PX) & ~np.isfinite(fitted)
            fitted[m] = z
        m = np.isfinite(fitted)
        klass[m] = 'zoom_desync'
        out['fitted_zoom'] = fitted
        todo &= ~m

    y_ok = ady <= MATCH_TOL_PX
    klass[todo & y_ok] = 'x_only'
    klass[todo & ~y_ok & (adx <= 10.0) & (ady <= 10.0)] = 'xy_small'
    klass[todo & ~y_ok & ((adx > 10.0) | (ady > 10.0))] = 'multi_field'
    out['klass'] = klass
    return out


def dpr2_zoom_overlap(g):
    """The dpr2/zoom-desync attribution degeneracy, measured: halving canvas offsets multiplies
    tan(offset angle) by 0.5, and the fov ladder happens to nearly do the same per level
    (2*atan(tan(fov1/2)/2) = 52.9 deg vs fov(2) = 53 deg), so a dpr2 row usually ALSO replays at
    zoom+1. Attribution between the two is therefore per-user statistics, not per-label math; a
    repair is valid under either reading (both reproduce the stored pano_x/pano_y)."""
    d = g[g['klass'] == 'dpr2']
    if len(d) == 0:
        return {'n_dpr2': 0}
    adx_z, ady_z = _replay_residuals(d, d['canvas_x'], d['canvas_y'], d['zoom'] + 1.0)
    also = (adx_z <= MATCH_TOL_PX) & (ady_z <= MATCH_TOL_PX)
    return {'n_dpr2': int(len(d)),
            'pct_also_matching_zoom_plus_1': round(float(100.0 * np.nansum(also) / len(d)), 2)}


def validate_px(df):
    """The record miss expressed as Validate-canvas pixels: the angular residual between the
    stored coordinate and what the record renders, scaled by the record's own zoom
    (px = deg / fov * 720). Small-angle, center-of-canvas conversion -- the gnomonic stretch
    off-center makes the true on-screen miss slightly larger, so these are floor estimates."""
    err_deg = np.hypot(df['dx_deg'].to_numpy(float), df['dy_deg'].to_numpy(float))
    fov = pov_replay.get_3d_fov(df['zoom'])
    return err_deg / np.asarray(fov, float) * 720.0


def visibility_summary(g):
    """Share of rows at or beyond each Validate-px tier, over rows replayable on both axes."""
    both = g['replayable_x'] & g['replayable_y']
    px = g.loc[both, 'validate_px']
    n = int(both.sum())
    if n == 0:
        return {'n': 0}
    out = {'n': n}
    for tier in PX_TIERS:
        out[f'pct_ge_{int(tier)}px'] = round(float(100.0 * (px >= tier).sum() / n), 2)
    out['p99_px'] = round(float(np.percentile(px, 99)), 1)
    out['max_px'] = round(float(px.max()), 1)
    return out


def stale_x_analysis(g):
    """The x_only cohort's statistical fingerprints, distinguishing batch staleness from
    camera_heading drift on in-window rows:

    - per-pano spread: drift is per-pano-constant (within-pano sigma at rounding noise), batch
      staleness varies per label;
    - same-POV batch groups: labels sharing one stored POV tuple but different canvas positions,
      whose dx moves TOGETHER -- the smoking gun that a staged, shared record field drifted after
      the clicks (a live mechanism claim the era study's group-miss-rate test cannot see);
    - the implied heading-shift magnitude distribution.
    """
    x_only = g[g['klass'] == 'x_only']
    out = {'n': int(len(x_only))}
    if len(x_only) == 0:
        return out
    adx = x_only['dx_deg'].abs()
    out['abs_dh_deg'] = {'p50': round(float(adx.median()), 3),
                         'p90': round(float(adx.quantile(0.9)), 3),
                         'max': round(float(adx.max()), 2)}

    # Per-pano spread over panos with >= 2 x_only rows.
    sizes = x_only.groupby('pano_id')['dx_deg'].size()
    multi = sizes[sizes >= 2].index
    if len(multi):
        spread = x_only[x_only['pano_id'].isin(multi)].groupby('pano_id')['dx_deg'] \
            .agg(lambda s: s.max() - s.min())
        out['per_pano'] = {'n_panos': int(len(multi)),
                           'pct_constant_dx': round(float(100.0 * (spread <= SHARED_DX_TOL_DEG).sum()
                                                          / len(spread)), 2)}

    # Same-POV batch groups: >= 2 labels, one stored POV tuple, > 1 distinct canvas position.
    misses = g[g['klass'].isin(['x_only', 'xy_small', 'multi_field'])]
    key = ['pano_id', 'user_id', 'heading', 'pitch', 'zoom']
    stats = misses.groupby(key).agg(
        n=('label_id', 'size'),
        n_canvas=('canvas_x', 'nunique'),
        dx_spread=('dx_deg', lambda s: s.max() - s.min()),
        max_abs_dx=('dx_deg', lambda s: s.abs().max()),
    )
    groups = stats[(stats['n'] >= 2) & (stats['n_canvas'] > 1)]
    if len(groups):
        shared = groups['dx_spread'] <= SHARED_DX_TOL_DEG
        out['same_pov_groups'] = {
            'n_groups': int(len(groups)),
            'n_labels': int(groups['n'].sum()),
            'pct_groups_shared_dx': round(float(100.0 * shared.sum() / len(groups)), 2),
            'max_shared_abs_dx_deg': round(float(groups.loc[shared, 'max_abs_dx'].max()), 2)
            if shared.any() else None,
        }
    return out


def solve_record(g):
    """Re-solve a self-consistent record from the stored pano_x/pano_y, holding canvas and zoom:
    rotate the viewport heading by the wrapped x residual (an exact correction -- azimuths rotate
    rigidly with the camera), then walk pitch down its residual (d(pov_pitch)/d(pitch) ~ 1 near
    axis; a few iterations converge). dpr2 rows instead halve the canvas; zoom_desync rows first
    take their fitted zoom. Returns (heading, pitch, zoom, canvas_x, canvas_y, post_adx, post_ady).

    The x correction routes through the row's *served* camera_heading, so repaired headings inherit
    whatever drift that column has accumulated -- the across-pano sigma the era study measured at
    0.12-0.73 deg, versus the 1-30 deg staleness being repaired.
    """
    heading = g['heading'].to_numpy(float).copy()
    pitch = g['pitch'].to_numpy(float).copy()
    zoom = g['zoom'].to_numpy(float).copy()
    canvas_x = g['canvas_x'].to_numpy(float).copy()
    canvas_y = g['canvas_y'].to_numpy(float).copy()

    is_dpr2 = (g['klass'] == 'dpr2').to_numpy()
    canvas_x[is_dpr2] = 360.0 + (canvas_x[is_dpr2] - 360.0) * 0.5
    canvas_y[is_dpr2] = 240.0 + (canvas_y[is_dpr2] - 240.0) * 0.5
    if 'fitted_zoom' in g:
        fz = g['fitted_zoom'].to_numpy(float)
        zoom = np.where(np.isfinite(fz), fz, zoom)

    sub = g.copy()
    sub['canvas_x'] = canvas_x
    sub['canvas_y'] = canvas_y
    w = np.asarray(g['pano_width'], float)
    h = np.asarray(g['pano_height'], float)
    for _ in range(25):
        pov_h, pov_p = pov_replay.pov_if_centered(canvas_x, canvas_y, heading, pitch, zoom,
                                                  g['canvas_width'], g['canvas_height'])
        rx, ry = pov_replay.pano_xy_from_pov(
            np.where(np.isfinite(pov_h), pov_h, 0.0), np.where(np.isfinite(pov_p), pov_p, 0.0),
            np.where(np.isfinite(g['camera_heading']), g['camera_heading'], 0.0),
            np.where(np.isfinite(w), w, 8192.0), np.where(np.isfinite(h), h, 4096.0))
        dx = era_replay_study.wrapped_dx(g['pano_x'], rx, np.where(np.isfinite(w), w, 8192.0))
        dy = np.asarray(g['pano_y'], float) - ry
        if (np.abs(dx) <= MATCH_TOL_PX).all() and (np.abs(dy) <= MATCH_TOL_PX).all():
            break
        heading = (heading + dx * 360.0 / w) % 360.0
        pitch = np.clip(pitch - dy * 180.0 / h, -90.0, 90.0)

    return heading, pitch, zoom, canvas_x, canvas_y, np.abs(dx), np.abs(dy)


def repair_frame(g):
    """Repair every replayable non-exact in-window record except frame_change (whose record is
    already consistent with the click). Returns (per-row repair frame, summary dict)."""
    scope = g[g['klass'].isin(['dpr2', 'zoom_desync', 'x_only', 'xy_small', 'multi_field'])].copy()
    if len(scope) == 0:
        return scope, {'n': 0}
    rh, rp, rz, rcx, rcy, post_adx, post_ady = solve_record(scope)
    repaired = (post_adx <= MATCH_TOL_PX) & (post_ady <= MATCH_TOL_PX)

    rep = scope[['label_id', 'klass', 'time_created', 'heading', 'pitch', 'zoom',
                 'canvas_x', 'canvas_y', 'validate_px']].copy()
    rep.columns = ['label_id', 'klass', 'time_created', 'old_heading', 'old_pitch', 'old_zoom',
                   'old_canvas_x', 'old_canvas_y', 'old_validate_px']
    rep['old_validate_px'] = np.round(rep['old_validate_px'].to_numpy(float), 1)
    rep['new_heading'] = np.round(rh, 4)
    rep['new_pitch'] = np.round(rp, 4)
    rep['new_zoom'] = rz
    rep['new_canvas_x'] = rcx
    rep['new_canvas_y'] = rcy
    rep['post_px_x'] = np.round(post_adx, 1)
    rep['post_px_y'] = np.round(post_ady, 1)
    # The repaired record's own Validate-px residual, same conversion as validate_px().
    w = np.asarray(scope['pano_width'], float)
    h = np.asarray(scope['pano_height'], float)
    post_deg = np.hypot(post_adx * 360.0 / w, post_ady * 180.0 / h)
    rep['new_validate_px'] = np.round(post_deg / np.asarray(pov_replay.get_3d_fov(rz), float)
                                      * 720.0, 2)
    rep['repaired'] = repaired

    summary = {'n': int(len(scope)),
               'pct_repaired': round(float(100.0 * repaired.sum() / len(scope)), 2),
               'by_class': {k: {'n': int((scope['klass'] == k).sum()),
                                'pct_repaired': round(float(
                                    100.0 * repaired[(scope['klass'] == k).to_numpy()].mean()), 2)}
                            for k in scope['klass'].unique()}}
    return rep, summary


def case_study(df, label_id):
    """One label's full diagnostic row (for #4842's named examples)."""
    row = df[df['label_id'] == label_id]
    if len(row) == 0:
        return None
    r = row.iloc[0]
    return {'label_id': int(label_id), 'time_created': str(r['time_created']),
            'klass': r['klass'],
            'stored': {'heading': float(r['heading']), 'pitch': float(r['pitch']),
                       'zoom': float(r['zoom']), 'canvas_x': float(r['canvas_x']),
                       'canvas_y': float(r['canvas_y']), 'pano_x': float(r['pano_x']),
                       'pano_y': float(r['pano_y'])},
            'residual_px': {'dx': None if not np.isfinite(r['dx']) else float(r['dx']),
                            'dy': None if not np.isfinite(r['dy']) else float(r['dy'])},
            'validate_px': None if not np.isfinite(r['validate_px']) else
            round(float(r['validate_px']), 1)}


def monthly_visibility(g):
    """Per-month share of post-179 rows >= 10 Validate px off -- the series that shows the bug
    window opening and closing on the deploy dates."""
    p = g[g['era'] == 'post179']
    both = p['replayable_x'] & p['replayable_y']
    p = p[both]
    out = {}
    for month, m in p.groupby(p['time_created'].dt.strftime('%Y-%m')):
        out[month] = {'n': int(len(m)),
                      'pct_ge_10px': round(float(100.0 * (m['validate_px'] >= 10.0).sum() / len(m)), 2)}
    return out


def study_city(path, case_id=None):
    """Full per-city analysis for one rawLabels CSV. Returns (result dict, in-window miss frame
    for the pooled scatter sample, per-row repair frame, case-study dict for case_id or None)."""
    df = classify(era_replay_study.replay_frame(rawlabels.load_rawlabels(path)))
    df['validate_px'] = validate_px(df)
    case = case_study(df, case_id) if case_id is not None else None

    p = df[df['era'] == 'post179']
    in_window = p[p['time_created'] < era_replay_study.BUG_WINDOW_END]
    post_fix = p[p['time_created'] >= era_replay_study.BUG_WINDOW_END]

    both_w = in_window[in_window['replayable_x'] & in_window['replayable_y']]
    miss_w = both_w[both_w['klass'] != 'exact']
    rep, rep_summary = repair_frame(both_w)

    result = {
        'n_labels': int(len(df)),
        'era_counts': df['era'].value_counts().to_dict(),
        'in_window': {
            'n': int(len(in_window)),
            'class_counts': both_w['klass'].value_counts().to_dict(),
            'visibility': visibility_summary(in_window),
            'stale_x': stale_x_analysis(both_w),
            'dpr2_zoom_overlap': dpr2_zoom_overlap(both_w),
            'last_miss': str(miss_w['time_created'].max()) if len(miss_w) else None,
        },
        'post_fix': {
            'n': int(len(post_fix)),
            'class_counts': post_fix[post_fix['replayable_x'] & post_fix['replayable_y']]
            ['klass'].value_counts().to_dict(),
            'visibility': visibility_summary(post_fix),
        },
        'pre179_visibility': visibility_summary(df[df['era'] != 'post179']),
        'monthly_ge10px': monthly_visibility(df),
        'repair': rep_summary,
    }
    return result, miss_w, rep, case


def scatter_sample(miss_frames, per_city_cap=750):
    """A deterministic 1-in-k decimation of every city's in-window misses for the residual scatter
    figure: (dx_deg, dy_deg, klass) triples, all cities pooled. Decimation by row order, not RNG,
    so the committed JSON regenerates identically from the same corpus."""
    pts = []
    for miss_w in miss_frames:
        step = max(1, len(miss_w) // per_city_cap)
        sub = miss_w.iloc[::step]
        pts += [{'dx': round(float(dx), 3), 'dy': round(float(dy), 3), 'k': k}
                for dx, dy, k in zip(sub['dx_deg'], sub['dy_deg'], sub['klass'])
                if np.isfinite(dx) and np.isfinite(dy)]
    return pts


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir', help='directory of <city>.csv rawLabels exports')
    ap.add_argument('--write', metavar='JSON', help='write the summary JSON here')
    ap.add_argument('--repairs-dir', metavar='DIR',
                    help='write per-city <prefix>-repairs-<city>.csv.gz of repaired records here')
    ap.add_argument('--fetched', metavar='DATE', required=True,
                    help='the date the CSVs were fetched (rawLabels is a moving target; results '
                         'are only meaningful alongside their fetch date)')
    args = ap.parse_args()

    case_ids = {'teaneck-nj': 14955, 'chicago-il': 30652}
    summary = {'source': '/v3/api/rawLabels?filetype=csv',
               'fetched': args.fetched,
               'bug_window': [str(rawlabels.EVO179), str(era_replay_study.BUG_WINDOW_END)],
               'match_tol_px': MATCH_TOL_PX,
               'px_tiers': list(PX_TIERS),
               'cities': {}, 'case_studies': {}}

    miss_frames = []
    for path in sorted(glob.glob(os.path.join(args.csv_dir, '*.csv'))):
        city = os.path.splitext(os.path.basename(path))[0]
        print(f'-- {city}', flush=True)
        result, miss_w, rep, case = study_city(path, case_ids.get(city))
        summary['cities'][city] = result
        miss_frames.append(miss_w)
        if case is not None:
            summary['case_studies'][city] = case
        w = result['in_window']
        print(f"   in-window n={w['n']:7d}  classes={w['class_counts']}")
        print(f"   visibility >=10px: in-window {w['visibility'].get('pct_ge_10px')}%"
              f" | post-fix {result['post_fix']['visibility'].get('pct_ge_10px')}%"
              f" | repair: {result['repair'].get('pct_repaired')}% of {result['repair'].get('n')}")
        if args.repairs_dir and len(rep):
            prefix = args.fetched
            out_path = os.path.join(args.repairs_dir, f'{prefix}-repairs-{city}.csv.gz')
            with gzip.open(out_path, 'wt', newline='') as f:
                rep.to_csv(f, index=False)
            print(f'   wrote {out_path} ({len(rep):,} rows)')

    summary['scatter_sample'] = scatter_sample(miss_frames)

    if args.write:
        with open(args.write, 'w') as f:
            json.dump(summary, f, indent=1, default=str, allow_nan=False)
        print(f'wrote {args.write}')


if __name__ == '__main__':
    main()
