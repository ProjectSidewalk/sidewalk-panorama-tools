"""Era/projection replay study: does the exact front-end math reproduce each city's stored
pano_x/pano_y, and where it doesn't, is the residual camera-metadata drift or projection error?

Per city (rawLabels CSV, see rawlabels.py), every row's pano_x/pano_y is recomputed with
reports/scripts/pov_replay.py — the verbatim production projection — from the row's own stored
inputs, and compared in the row's own pano frame. Expectations inherited from the sibling repo
(label-latlng-estimation reports/2026-08-06-pov-inversion.md), which this study extends to cities
that work never touched:

- pano_y must replay exactly wherever it replays at all (no free camera input);
- post-179 pano_x must replay exactly (written live by this math);
- earlier pano_x misses must carry the per-pano-constant camera_heading-drift signature
  (within-pano sigma at rounding-noise level, across-pano sigma much larger).

What the cropper studies take from this: per-label era + replay agreement is a zero-annotation
projection-error covariate for the #54 placement study, and the fixed-frame census (rows whose
recorded dims are the legacy 13312x6656 viewer frame) bounds how many stored coordinates live in a
frame that need not match a freshly downloaded image (#77's dims preflight).

Usage:
    python era_replay_study.py <dir-of-city-csvs> [--write reports/data/<date>-era-replay-summary.json]
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402

# The legacy fixed viewer frame. A row carrying exactly these dims is ambiguous by dims alone —
# some real GSV panos are natively 13312x6656 — which is why the replay, not the dims, is the
# era evidence.
FIXED_FRAME = (13312.0, 6656.0)

# The placement-record bug window inside post179 (found by this study, Seattle daily series):
# from evolution 179's deploy until SidewalkWebpage 7.20.7 (version bump 2024-09-25; Seattle's
# last bad day IS 2024-09-25, first clean day 2024-09-28). 7.20.7's changeset rebuilt the Explore
# submission path from staged batch lists to per-label immediate submission (commits c789837f0,
# 59627bbc8, 610f31dee of 2024-09-19/20), which is what stopped the corruption. Rows in this
# window have a ~5% (y) / ~13% (x, Seattle) chance that the stored canvas/POV tuple does not
# reproduce pano_x/pano_y; the evidence says pano_x/pano_y is the click-time truth and the
# canvas/POV record is the corrupted side (see reports/2026-08-09-era-replay-study.md).
BUG_WINDOW_END = pd.Timestamp('2024-09-26', tz='UTC')

# The pre-registration's §3 era-QUALITY stratum, which is not `rawlabels.add_era`'s three-level
# `era`: it splits post179 at BUG_WINDOW_END, because the two halves differ in exactly the property
# the corpus stratifies on — whether the stored canvas/POV record can be trusted to be the
# click-time record. Ordered oldest-first so a manifest or report table reads chronologically.
QUALITY_LEVELS = ('legacy', 'mid', 'window', 'post_fix')


def era_quality(time_created):
    """Bucket timestamps into QUALITY_LEVELS: legacy < LEGACY_END <= mid < EVO179 <= window <
    BUG_WINDOW_END <= post_fix.

    Lives here rather than beside `add_era` in rawlabels because it needs BUG_WINDOW_END, which this
    module owns and measured; rawlabels importing it back would be circular. Every boundary is
    lower-inclusive, matching `add_era` and `window_split`, so a label's `era` and its `quality` can
    never disagree about which side of a deploy it fell on.
    """
    t = pd.Series(pd.to_datetime(time_created, utc=True))
    quality = pd.Series('mid', index=t.index, dtype=object)
    quality[t < rawlabels.LEGACY_END] = 'legacy'
    quality[(t >= rawlabels.EVO179) & (t < BUG_WINDOW_END)] = 'window'
    quality[t >= BUG_WINDOW_END] = 'post_fix'
    return quality


def wrapped_dx(stored, replay, width):
    """Seam-aware pixel difference stored - replay on a cylindrical x axis, in (-w/2, w/2]."""
    stored = np.asarray(stored, float)
    replay = np.asarray(replay, float)
    width = np.asarray(width, float)
    d = (stored - replay + width / 2) % width - width / 2
    return np.where(d == -width / 2, width / 2, d)


def frame_pov(df):
    """The click's own POV for every row of a rawlabels frame: (pov_heading, pov_pitch) in degrees.

    The single place that reads a frame's canvas dims and runs `pov_replay.pov_if_centered` over
    them. Every study that needs the replayed POV goes through here rather than repeating the
    per-row-vs-default canvas fallback — that fallback existed twice (here and in
    offaxis_covariate.offaxis_offsets) and the two copies had to be kept in step by hand, with
    nothing failing if they drifted. Unmasked: NaN in, NaN out; callers decide what a non-finite
    POV means (see `replay_frame`'s ok_x/ok_y).
    """
    cw = df['canvas_width'] if 'canvas_width' in df else pov_replay.CANVAS_W
    ch = df['canvas_height'] if 'canvas_height' in df else pov_replay.CANVAS_H
    pov_h, pov_p = pov_replay.pov_if_centered(
        df['canvas_x'], df['canvas_y'], df['heading'], df['pitch'], df['zoom'], cw, ch)
    return np.asarray(pov_h, float), np.asarray(pov_p, float)


def replay_frame(df):
    """Run the exact replay over a rawlabels frame; returns a copy with pov_heading/pov_pitch,
    replay_x/replay_y, the residuals (dx wrapped px / dx_deg, dy px / dy_deg),
    replayable_x/replayable_y masks, and exact_x/exact_y flags. x and y are masked independently: a
    row without camera_heading still checks pano_y, which consumes no camera metadata.

    pov_heading/pov_pitch are carried out as columns so a downstream study needing the click's own
    POV reads it instead of re-projecting: `offaxis_covariate.prepare` used to run the full gnomonic
    projection a second time over the same 438k rows, and derived its covariate from one call while
    eligibility came from the other.
    """
    out = df.copy()
    pov_h, pov_p = frame_pov(df)

    w = np.asarray(df['pano_width'], float)
    h = np.asarray(df['pano_height'], float)
    cam_h = np.asarray(df['camera_heading'], float)
    stored_x = np.asarray(df['pano_x'], float)
    stored_y = np.asarray(df['pano_y'], float)

    pov_ok = np.isfinite(pov_h) & np.isfinite(pov_p)
    ok_x = pov_ok & np.isfinite(cam_h) & np.isfinite(w) & (w > 0) & np.isfinite(stored_x)
    ok_y = pov_ok & np.isfinite(h) & (h > 0) & np.isfinite(stored_y)

    # pano_xy_from_pov int-casts both outputs, so feed it NaN-free dummies and mask after.
    safe = lambda a, fill: np.where(np.isfinite(a), a, fill)
    rx, ry = pov_replay.pano_xy_from_pov(safe(pov_h, 0.0), safe(pov_p, 0.0),
                                         safe(cam_h, 0.0), safe(w, 8192.0), safe(h, 4096.0))

    dx = np.where(ok_x, wrapped_dx(stored_x, rx, safe(w, 8192.0)), np.nan)
    dy = np.where(ok_y, stored_y - ry, np.nan)

    out['pov_heading'] = pov_h
    out['pov_pitch'] = pov_p
    out['replay_x'] = np.where(ok_x, rx, np.nan)
    out['replay_y'] = np.where(ok_y, ry, np.nan)
    out['dx'] = dx
    out['dy'] = dy
    out['dx_deg'] = dx * 360.0 / w
    out['dy_deg'] = dy * 180.0 / h
    out['replayable_x'] = ok_x
    out['replayable_y'] = ok_y
    out['exact_x'] = ok_x & (dx == 0)
    out['exact_y'] = ok_y & (dy == 0)
    return out


def drift_decomposition(df):
    """The dispositive drift test, on x-mismatched rows of panos with >= 2 mismatches: dx_deg is
    the implied camera_heading delta (served minus production), so metadata drift is constant
    within a pano while projection error would vary with canvas position and POV."""
    m = df[(~df['exact_x']) & np.isfinite(df['dx_deg'])]
    sizes = m.groupby('pano_id')['dx_deg'].size()
    multi = sizes[sizes >= 2].index
    if len(multi) == 0:
        return {'n_panos': 0, 'n_labels': 0,
                'median_within_pano_sigma_deg': None, 'across_pano_sigma_deg': None}
    grouped = m[m['pano_id'].isin(multi)].groupby('pano_id')['dx_deg']
    within = grouped.std(ddof=1)
    means = grouped.mean()
    return {
        'n_panos': int(len(multi)),
        'n_labels': int(sizes[sizes >= 2].sum()),
        'median_within_pano_sigma_deg': float(within.median()),
        'across_pano_sigma_deg': float(means.std(ddof=1)) if len(means) > 1 else 0.0,
    }


def _quantiles(series):
    a = np.asarray(series, float)
    return {'n': int(a.size), 'p50': float(np.percentile(a, 50)),
            'p90': float(np.percentile(a, 90)), 'p99': float(np.percentile(a, 99)),
            'max': float(a.max())}


def summarize_eras(df):
    """Per-era agreement: counts, exact rates over the replayable rows, and residual quantiles
    over the misses (x in degrees — resolution-independent; y in pixels — it should never miss)."""
    out = {}
    for era, g in df.groupby('era'):
        n_x = int(g['replayable_x'].sum())
        n_y = int(g['replayable_y'].sum())
        x_misses = g.loc[g['replayable_x'] & ~g['exact_x'], 'dx_deg'].abs()
        y_misses = g.loc[g['replayable_y'] & ~g['exact_y'], 'dy'].abs()
        out[era] = {
            'n': int(len(g)),
            'replayable_x': n_x,
            'replayable_y': n_y,
            'exact_x_pct': float(100.0 * g['exact_x'].sum() / n_x) if n_x else None,
            'exact_y_pct': float(100.0 * g['exact_y'].sum() / n_y) if n_y else None,
            'abs_dx_deg_of_misses': _quantiles(x_misses) if len(x_misses) else None,
            'abs_dy_px_of_misses': _quantiles(y_misses) if len(y_misses) else None,
        }
    return out


def window_split(df):
    """post179 broken down by the placement-record bug window: agreement rates for rows created
    in [EVO179, BUG_WINDOW_END) versus after. The 'after' rates are the proof that current
    clients write a fully self-consistent record."""
    p = df[df['era'] == 'post179']
    out = {}
    for name, g in (('in_window', p[p['time_created'] < BUG_WINDOW_END]),
                    ('post_fix', p[p['time_created'] >= BUG_WINDOW_END])):
        n_x = int(g['replayable_x'].sum())
        n_y = int(g['replayable_y'].sum())
        out[name] = {
            'n': int(len(g)),
            'exact_x_pct': float(100.0 * g['exact_x'].sum() / n_x) if n_x else None,
            'exact_y_pct': float(100.0 * g['exact_y'].sum() / n_y) if n_y else None,
        }
    return out


def monthly_series(df):
    """Per-month exact_x/exact_y percentages over post179 rows — the series that localizes any
    client-behaviour boundary to a deploy date."""
    p = df[df['era'] == 'post179']
    months = p['time_created'].dt.strftime('%Y-%m')
    out = {}
    for month, g in p.groupby(months):
        n_x = int(g['replayable_x'].sum())
        n_y = int(g['replayable_y'].sum())
        out[month] = {'n': int(len(g)),
                      'exact_x_pct': round(100.0 * float(g['exact_x'].sum()) / n_x, 2) if n_x else None,
                      'exact_y_pct': round(100.0 * float(g['exact_y'].sum()) / n_y, 2) if n_y else None}
    return out


def study_city(path):
    """Full per-city analysis dict for one rawLabels CSV."""
    df = replay_frame(rawlabels.load_rawlabels(path))
    fixed = (df['pano_width'] == FIXED_FRAME[0]) & (df['pano_height'] == FIXED_FRAME[1])
    canvas_724 = (df['canvas_width'] == 720.0) & (df['canvas_height'] == 480.0)
    return {
        'n_labels': int(len(df)),
        'date_range': [str(df['time_created'].min().date()), str(df['time_created'].max().date())],
        'era_counts': df['era'].value_counts().to_dict(),
        'missing': {
            'pano_dims': int(df['pano_width'].isna().sum()),
            'camera_heading': int(df['camera_heading'].isna().sum()),
            'camera_pitch': int(df['camera_pitch'].isna().sum()),
            'camera_roll': int(df['camera_roll'].isna().sum()),
            'pano_xy': int(df['pano_x'].isna().sum()),
        },
        'fixed_frame_rows': {'n': int(fixed.sum()),
                             'by_era': df.loc[fixed, 'era'].value_counts().to_dict()},
        'nonstandard_canvas_rows': int((~canvas_724).sum()),
        'eras': summarize_eras(df),
        'post179_bug_window': window_split(df),
        'post179_monthly': monthly_series(df),
        'drift_signature_pre179': drift_decomposition(df[df['era'] != 'post179']),
        # The same dispositive test on the post-fix rows. Post-fix x still misses 1-6% of the time
        # while y is ~100% exact, which the report attributes to gsv_data camera_heading refresh --
        # an attribution that was originally argued from the pre-179 decomposition alone. Added
        # 2026-08-10 in review; the committed 2026-08-09 summary predates it and does not carry the
        # key, so consumers must treat it as optional until the next fetch.
        'drift_signature_post_fix': drift_decomposition(
            df[(df['era'] == 'post179') & (df['time_created'] >= BUG_WINDOW_END)]),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir', help='directory of <city>.csv rawLabels exports')
    ap.add_argument('--write', metavar='JSON', help='write the summary JSON here')
    ap.add_argument('--fetched', metavar='DATE', required=True,
                    help='the date the CSVs were fetched (rawLabels is a moving target: labels '
                         'accrue and gsv_data refreshes, so results are only meaningful '
                         'alongside their fetch date)')
    args = ap.parse_args()

    summary = {'source': '/v3/api/rawLabels?filetype=csv',
               'fetched': args.fetched,
               'boundaries': {'legacy_end': str(rawlabels.LEGACY_END),
                              'evo179': str(rawlabels.EVO179)},
               'cities': {}}
    for path in sorted(glob.glob(os.path.join(args.csv_dir, '*.csv'))):
        city = os.path.splitext(os.path.basename(path))[0]
        print(f'-- {city}', flush=True)
        result = study_city(path)
        summary['cities'][city] = result
        for era, s in result['eras'].items():
            print(f"   {era:8s} n={s['n']:7d}  exact_x={s['exact_x_pct'] if s['exact_x_pct'] is None else round(s['exact_x_pct'], 2)}%"
                  f"  exact_y={s['exact_y_pct'] if s['exact_y_pct'] is None else round(s['exact_y_pct'], 2)}%")
        drift = result['drift_signature_pre179']
        print(f"   drift: {drift['n_panos']} panos, within-sigma "
              f"{drift['median_within_pano_sigma_deg']}, across-sigma {drift['across_pano_sigma_deg']}")

    if args.write:
        with open(args.write, 'w') as f:
            json.dump(summary, f, indent=1, default=str, allow_nan=False)
        print(f'wrote {args.write}')


if __name__ == '__main__':
    main()
