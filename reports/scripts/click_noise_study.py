"""Placement-noise measurement from co-located duplicate labels â€” zero annotation cost.

When two users independently label the same physical object on the same pano, the angular spread
between their stored points measures end-to-end placement noise (click precision + genuine
disagreement about the object's centre) at identical viewpoint and imagery. That spread is the
noise floor the #54 placement study must budget against, and it prices the pre-registration's
power calculation.

Method: within each (pano_id, label_type), cluster labels by great-circle-ish angular proximity
(azimuth differences seam-wrapped and cos(elevation)-scaled); drop same-user repeats inside a
cluster (double-submits are not independent placements); every cross-user pair contributes one
per-axis difference. With iid per-axis noise sigma, a pair difference is N(0, 2*sigma^2), so the
robust estimator is sigma = 1.4826 * median|d| / sqrt(2) â€” insensitive to the misplacement tail,
which is real but is a different phenomenon than click noise.

Anchoring on stored pano_x/pano_y (not the canvas/POV record) is justified by the era replay
study (reports/2026-08-09-era-replay-study.md): stored pano coordinates are click-time truth in
every era. Rows with non-positive pano dims or out-of-frame pano_y are dropped (the 2 corrupt
negative-y rows, plus any frame junk).

The clustering radius trades off splitting noisy duplicates against merging genuinely distinct
neighbours (a corner's two curb ramps sit a few degrees apart). There is no correct single value,
so the study reports a radius sweep; if sigma grows materially with radius, merging is leaking in.

Usage:
    python click_noise_study.py reports/scripts/.cache/rawlabels --fetched 2026-08-09 \
        --write reports/data/2026-08-09-click-noise-summary.json
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rawlabels  # noqa: E402
from studyfmt import fmt  # noqa: E402

PRIMARY_RADIUS_DEG = 1.5
RADIUS_SWEEP = (0.75, 1.0, 1.5, 2.0)

# Depression of the cluster below the horizon, in bands chosen where the distance blend changes
# character: nearly-horizon (< 5 deg, far field), mid, steep (> 15 deg, near field).
#
# These start at 0, so a cluster sitting ABOVE the horizon (negative depression - a pedestrian
# signal high on a pole, say) falls outside all three and is silently absent from the band table
# while still counting in `overall`. The 2026-08-09 corpus has none: the band counts sum exactly to
# the 13,359 overall pairs, and tests/test_click_noise_study.py asserts that sum so a future run
# that does have them fails loudly instead of quietly under-reporting.
DEPRESSION_BANDS = ((0.0, 5.0), (5.0, 15.0), (15.0, 90.0))


def _angular(df):
    az = (df['pano_x'] / df['pano_width'] * 360.0) % 360.0
    el = (df['pano_height'] / 2 - df['pano_y']) * 90.0 / (df['pano_height'] / 2)
    return az, el


def cluster_labels(df, radius_deg):
    """Add a cluster_id column: connected components under angular distance <= radius_deg,
    within (pano_id, label_type). Azimuth is seam-wrapped and cos(elevation)-scaled."""
    df = df.copy()
    df['_az'], df['_el'] = _angular(df)
    cluster = np.full(len(df), -1, dtype=int)
    positions = {label: i for i, label in enumerate(df.index)}
    next_id = 0
    for _, g in df.groupby(['pano_id', 'label_type'], sort=False):
        rows = [positions[label] for label in g.index]
        n = len(rows)
        a = g['_az'].to_numpy()
        e = g['_el'].to_numpy()
        daz = np.abs(a[:, None] - a[None, :])
        daz = np.minimum(daz, 360.0 - daz)
        mean_el = np.radians((e[:, None] + e[None, :]) / 2)
        dist = np.hypot(daz * np.cos(mean_el), e[:, None] - e[None, :])
        adj = dist <= radius_deg
        comp = np.full(n, -1, dtype=int)
        for i in range(n):
            if comp[i] >= 0:
                continue
            stack = [i]
            comp[i] = next_id
            while stack:
                j = stack.pop()
                for k in np.nonzero(adj[j])[0]:
                    if comp[k] < 0:
                        comp[k] = next_id
                        stack.append(k)
            next_id += 1
        for i, r in enumerate(rows):
            cluster[r] = comp[i]
    df['cluster_id'] = cluster
    return df


def cluster_pairs(df):
    """One row per cross-user pair inside a cluster (same-user repeats dropped, earliest kept):
    signed d_az (cos-scaled), signed d_el, d_total, and the pair's mean elevation."""
    rows = []
    multi = df[df.groupby('cluster_id')['cluster_id'].transform('size') >= 2]
    for cid, g in multi.groupby('cluster_id'):
        g = g.sort_values('time_created').drop_duplicates('user_id', keep='first')
        if len(g) < 2:
            continue
        a = g['_az'].to_numpy()
        e = g['_el'].to_numpy()
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                signed = ((a[i] - a[j] + 180.0) % 360.0) - 180.0
                el_mean = (e[i] + e[j]) / 2
                d_az = signed * np.cos(np.radians(el_mean))
                d_el = e[i] - e[j]
                rows.append({'cluster_id': cid, 'pano_id': g['pano_id'].iloc[0],
                             'label_type': g['label_type'].iloc[0], 'el_mean': el_mean,
                             'd_az': d_az, 'd_el': d_el, 'd_total': float(np.hypot(d_az, d_el))})
    return pd.DataFrame(rows, columns=['cluster_id', 'pano_id', 'label_type', 'el_mean',
                                       'd_az', 'd_el', 'd_total'])


def sigma_from_pairs(pairs):
    """Robust per-axis noise sigma from pair differences (see module docstring), plus the raw
    separation quantiles a reader can sanity-check against."""
    if not len(pairs):
        return {'n_pairs': 0, 'n_clusters': 0, 'sigma_az_deg': None, 'sigma_el_deg': None,
                'd_total_p50': None, 'd_total_p90': None}
    f = 1.4826 / np.sqrt(2)
    return {
        'n_pairs': int(len(pairs)),
        'n_clusters': int(pairs['cluster_id'].nunique()),
        'sigma_az_deg': float(f * np.median(np.abs(pairs['d_az']))),
        'sigma_el_deg': float(f * np.median(np.abs(pairs['d_el']))),
        'd_total_p50': float(np.median(pairs['d_total'])),
        'd_total_p90': float(np.percentile(pairs['d_total'], 90)),
    }


def load_city(path):
    df = rawlabels.load_rawlabels(path)
    ok = (df['pano_width'].gt(0) & df['pano_height'].gt(0)
          & df['pano_x'].notna() & df['pano_y'].ge(0) & df['pano_y'].le(df['pano_height']))
    return df[ok]


def study(csv_dir):
    frames = []
    for path in sorted(glob.glob(os.path.join(csv_dir, '*.csv'))):
        city = os.path.splitext(os.path.basename(path))[0]
        df = load_city(path)
        df['city'] = city
        frames.append(df)
    allc = pd.concat(frames, ignore_index=True)

    out = {'primary_radius_deg': PRIMARY_RADIUS_DEG, 'n_labels': int(len(allc))}

    clustered = cluster_labels(allc, PRIMARY_RADIUS_DEG)
    pairs = cluster_pairs(clustered)
    out['overall'] = sigma_from_pairs(pairs)

    out['by_label_type'] = {lt: sigma_from_pairs(g)
                            for lt, g in pairs.groupby('label_type') if len(g) >= 30}

    by_band = {}
    dep = -pairs['el_mean']
    for lo, hi in DEPRESSION_BANDS:
        band = pairs[(dep >= lo) & (dep < hi)]
        by_band[f'{lo:g}-{hi:g}deg'] = sigma_from_pairs(band)
    out['by_depression_band'] = by_band

    out['radius_sweep'] = {}
    for r in RADIUS_SWEEP:
        p = cluster_pairs(cluster_labels(allc, r))
        out['radius_sweep'][f'{r:g}'] = sigma_from_pairs(p)

    # Sensitivity: validated-correct labels only (>= 2 votes, agree > disagree). If sigma drops a
    # lot here, the primary estimate is inflated by misplacements rather than click noise.
    v = allc[(allc['agree_count'] + allc['disagree_count'] >= 2)
             & (allc['agree_count'] > allc['disagree_count'])]
    out['validated_only'] = sigma_from_pairs(cluster_pairs(cluster_labels(v, PRIMARY_RADIUS_DEG)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir')
    ap.add_argument('--fetched', required=True)
    ap.add_argument('--write', metavar='JSON')
    args = ap.parse_args(argv)

    result = {'source': '/v3/api/rawLabels?filetype=csv', 'fetched': args.fetched}
    result.update(study(args.csv_dir))

    # sigma_* is None whenever a group has no cross-user pairs (sigma_from_pairs' documented
    # contract), which is not exotic: a single-city corpus, or a deliberately crossed block where the
    # two labellers picked different perspectives, reaches it immediately. Format-specing it directly
    # made this script die on Richmond before printing anything.
    o = result['overall']
    print(f"pairs {o['n_pairs']} clusters {o['n_clusters']}  "
          f"sigma_az {fmt(o['sigma_az_deg'], '.3f')} deg  sigma_el {fmt(o['sigma_el_deg'], '.3f')} deg")
    for lt, s in sorted(result['by_label_type'].items()):
        print(f"  {lt:22s} n={s['n_pairs']:6d}  az {fmt(s['sigma_az_deg'], '.3f')}  "
              f"el {fmt(s['sigma_el_deg'], '.3f')}")
    # These three printed the bare value, so they never raised -- but an empty band showed as the
    # literal 'None' in a column of numbers. Same helper, same three decimals as every other sigma.
    for band, s in result['by_depression_band'].items():
        print(f"  depression {band:9s} n={s['n_pairs']:6d}  az {fmt(s['sigma_az_deg'], '.3f')}  "
              f"el {fmt(s['sigma_el_deg'], '.3f')}")
    for r, s in result['radius_sweep'].items():
        print(f"  radius {r:5s} n={s['n_pairs']:6d}  sigma_el {fmt(s['sigma_el_deg'], '.3f')}")
    v = result['validated_only']
    print(f"  validated-only n={v['n_pairs']:6d}  sigma_el {fmt(v['sigma_el_deg'], '.3f')}")

    if args.write:
        with open(args.write, 'w') as f:
            json.dump(result, f, indent=1, allow_nan=False)
        print(f'wrote {args.write}')


if __name__ == '__main__':
    main()
