"""Placement-noise measurement from co-located duplicate labels — zero annotation cost.

When two users independently label the same physical object on the same pano, the angular spread
between their stored points measures end-to-end placement noise (click precision + genuine
disagreement about the object's centre) at identical viewpoint and imagery. That spread is the
noise floor the #54 placement study must budget against, and it prices the pre-registration's
power calculation.

Method: within each (pano_id, label_type), cluster labels by great-circle-ish angular proximity
(azimuth differences seam-wrapped and cos(elevation)-scaled); drop same-user repeats inside a
cluster (double-submits are not independent placements); every cross-user pair contributes one
per-axis difference. With iid per-axis noise sigma, a pair difference is N(0, 2*sigma^2), so the
robust estimator is sigma = 1.4826 * median|d| / sqrt(2) — insensitive to the misplacement tail,
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
import itertools
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

# Matched mode (see `matched_pairs`). An assignment that had to reach further than this to find a
# partner is reported as a rejection rather than folded into sigma: unequal per-user counts on a pano
# force a match for the surplus label, and one forced match across the frame moves a median. Generous
# on purpose -- real disagreement about a ramp's centre is a degree or two, and the sweep in the GSV
# corpus tops out at 2 deg, so 10 deg censors forced matches without censoring genuine spread.
MATCH_MAX_SEP_DEG = 10.0

# Cap on the exhaustive assignment search, per (pano, label_type, user pair). 5040 = 7!, so any
# realistic block is solved exactly; beyond it the search degrades to greedy and is counted.
ASSIGNMENT_MAX_MAPS = 5040


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


def _pair_record(cid, pano_id, label_type, az_i, el_i, az_j, el_j):
    """The per-axis difference for one pair, in the study's conventions: azimuth seam-wrapped and
    cos(elevation)-scaled so it is an on-sphere angle, elevation signed, both in degrees.

    Shared by `cluster_pairs` and `matched_pairs` deliberately. Two copies of this arithmetic would be
    two conventions to keep in step, and a sign or a missing cos here changes every sigma the study
    reports without failing anything.
    """
    signed = ((az_i - az_j + 180.0) % 360.0) - 180.0
    el_mean = (el_i + el_j) / 2
    d_az = signed * np.cos(np.radians(el_mean))
    d_el = el_i - el_j
    return {'cluster_id': cid, 'pano_id': pano_id, 'label_type': label_type, 'el_mean': el_mean,
            'd_az': d_az, 'd_el': d_el, 'd_total': float(np.hypot(d_az, d_el))}


PAIR_COLUMNS = ['cluster_id', 'pano_id', 'label_type', 'el_mean', 'd_az', 'd_el', 'd_total']


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
                rows.append(_pair_record(cid, g['pano_id'].iloc[0], g['label_type'].iloc[0],
                                         a[i], e[i], a[j], e[j]))
    return pd.DataFrame(rows, columns=PAIR_COLUMNS)


def _assign(cost):
    """Minimum-total-cost one-to-one assignment: returns [(row, col), ...] and whether it is exact.

    Exhaustive over injective maps from the smaller side into the larger, which is the *optimal*
    assignment, not a greedy approximation. That matters: greedy smallest-first can be arbitrarily
    wrong (take the globally closest pair first and the remaining labels are forced across the frame),
    and the whole point of matched mode is that pair identity is decided by design rather than by a
    radius.

    A designed crossed block puts a handful of labels of one type on one pano, so the search is tiny.
    Above ASSIGNMENT_MAX_MAPS candidate maps it falls back to greedy and says so, rather than hanging:
    the caller counts fallbacks into the result so a run that needed one is visible.

    scipy.optimize.linear_sum_assignment would be the tool for this, but scipy is not in
    requirements.txt and pulling it in for one small search would be the heavier choice.
    """
    k, m = cost.shape
    n_maps = 1
    for i in range(k):
        n_maps *= (m - i)
    if n_maps <= ASSIGNMENT_MAX_MAPS:
        best_total, best = None, None
        for perm in itertools.permutations(range(m), k):
            total = float(sum(cost[i, perm[i]] for i in range(k)))
            if best_total is None or total < best_total:
                best_total, best = total, perm
        return list(enumerate(best)), True

    taken_r, taken_c, out = set(), set(), []
    for i, j in sorted(((i, j) for i in range(k) for j in range(m)), key=lambda p: cost[p]):
        if i not in taken_r and j not in taken_c:
            taken_r.add(i)
            taken_c.add(j)
            out.append((i, j))
    return out, False


def matched_pairs(df, panos=None, max_sep_deg=MATCH_MAX_SEP_DEG):
    """Cross-user pairs for a *designed* crossed block: two labellers asked to label the same panos
    exhaustively, matched one-to-one by minimum total angular separation.

    Returns (pairs, diagnostics). `pairs` has the same columns `sigma_from_pairs` consumes, so both
    estimators feed the same summary.

    Why this exists alongside `cluster_labels`/`cluster_pairs`. The clustering estimator groups by
    angular proximity, which is the only thing available for *incidental* co-location in production
    data -- and it is why the study reports a radius sweep with no plateau: at any radius the pair
    population is a mixture of same-object noise and genuinely distinct neighbours (a corner's two
    curb ramps), so sigma_el runs 0.299 deg at 0.75 deg to 0.599 deg at 2 deg with no correct answer.

    When the block is designed, identity is known by construction rather than inferred from distance:
    if both labellers labelled every ramp in the pano, the k-th ramp for one is the k-th ramp for the
    other, and the assignment recovers that without a radius at all. That removes the mixture, so the
    sweep is unnecessary and a single sigma is meaningful.

    **This is valid ONLY on a designed block, and it is actively worse than clustering without one.**
    The assignment assumes both labellers were trying to label the same set of objects; where they were
    not, it force-pairs whatever is left. Measured on the six-city production corpus -- where
    co-location is incidental and users labelled overlapping-but-different subsets of each pano's
    objects -- it returns sigma_el 0.967 deg against the clustered estimate's 0.507 deg, because a
    corner's four curb ramps get paired across users almost arbitrarily. That is why `study()` computes
    this only when a pano list is supplied: a plausible-looking sigma from force-paired distinct
    objects is the exact failure this study already documents for the radius sweep, and it must not
    land in a committed artifact by default.

    `panos` restricts to the agreed list -- the point of the design is that both labellers covered the
    *same* panos, and on the 2026-08-11 Richmond block they overlapped on only 15 of 55/49, which is
    exactly the failure this argument is meant to make visible. `max_sep_deg` rejects an assignment
    that had to reach across the frame to find a partner (unequal counts force one), rather than
    letting a forced match inflate sigma; rejections are counted, never silently dropped.
    """
    d = df if panos is None else df[df['pano_id'].isin(set(panos))]
    d = d.copy()
    d['_az'], d['_el'] = _angular(d)

    rows, rejected, greedy, cid = [], 0, 0, 0
    for (pano_id, label_type), g in d.groupby(['pano_id', 'label_type'], sort=True):
        # One placement per user per object: a double-submit is not an independent placement, and the
        # earliest is the one the clustering estimator keeps too.
        g = g.sort_values('time_created')
        users = sorted(g['user_id'].dropna().unique())
        for ua, ub in itertools.combinations(users, 2):
            a = g[g['user_id'] == ua]
            b = g[g['user_id'] == ub]
            flip = len(a) > len(b)
            if flip:
                a, b = b, a
            if not len(a):
                continue
            az_a, el_a = a['_az'].to_numpy(), a['_el'].to_numpy()
            az_b, el_b = b['_az'].to_numpy(), b['_el'].to_numpy()
            daz = np.abs(az_a[:, None] - az_b[None, :])
            daz = np.minimum(daz, 360.0 - daz)
            mean_el = np.radians((el_a[:, None] + el_b[None, :]) / 2)
            cost = np.hypot(daz * np.cos(mean_el), el_a[:, None] - el_b[None, :])
            assignment, exact = _assign(cost)
            if not exact:
                greedy += 1
            for i, j in assignment:
                if max_sep_deg is not None and cost[i, j] > max_sep_deg:
                    rejected += 1
                    continue
                # `a`/`b` may have been swapped so the smaller side indexes the rows; undo that here
                # so the difference is always (first user, second user) in sorted user order and the
                # signs of d_az/d_el mean the same thing on every pano.
                first = (az_b[j], el_b[j]) if flip else (az_a[i], el_a[i])
                second = (az_a[i], el_a[i]) if flip else (az_b[j], el_b[j])
                rows.append(_pair_record(cid, pano_id, label_type,
                                         first[0], first[1], second[0], second[1]))
                cid += 1

    diagnostics = {
        'mode': 'matched',
        'max_sep_deg': max_sep_deg,
        'n_panos_considered': int(d['pano_id'].nunique()),
        'n_labels_considered': int(len(d)),
        'n_pairs_matched': len(rows),
        'n_rejected_beyond_max_sep': rejected,
        'n_greedy_fallbacks': greedy,
    }
    return pd.DataFrame(rows, columns=PAIR_COLUMNS), diagnostics


def shared_panos(df, min_users=2):
    """Pano ids carrying labels from at least `min_users` distinct users — the crossed block that
    actually exists, as opposed to the one that was intended.

    Worth its own function because the distinction is where the 2026-08-11 Richmond block went wrong:
    two labellers shared a *route*, each freely picking which perspective of each ramp to label, and
    ended up on 15 panos in common out of 55 and 49. Sharing a route does not share panos.
    """
    n = df.groupby('pano_id')['user_id'].nunique()
    return sorted(n[n >= min_users].index)


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


def read_pano_list(path):
    """One pano id per line; blank lines and `#` comments ignored. Ids stay strings — Mapillary ids
    are all-numeric and must not become ints (see rawlabels.load_rawlabels)."""
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if line:
                out.append(line)
    return out


def matched_study(df, panos, exclude_unlocated=True):
    """The designed-crossed-block measurement: matched pairs plus the sigma they imply.

    `panos` is required, not optional. Defaulting it to `shared_panos(df)` was the first design and it
    is a footgun: on incidental data it silently produces a sigma from force-paired distinct objects
    (see `matched_pairs`). Callers must state which panos were agreed.

    `exclude_unlocated` applies `rawlabels.has_located_referent`, which is part of what makes a pair
    comparable rather than a separate quality filter: a SurfaceProblem tagged brick/cobblestone could
    have been placed anywhere along a brick sidewalk, so two labellers' points on it differ by an
    arbitrary amount that is not placement noise. Counted, not silently dropped.
    """
    n_before = len(df)
    if exclude_unlocated:
        df = df[rawlabels.has_located_referent(df)]
    pairs, diag = matched_pairs(df, panos)
    diag['n_dropped_unlocated_referent'] = n_before - len(df)
    diag['n_panos_in_list'] = len(panos)
    diag['n_panos_with_two_users'] = len(shared_panos(df[df['pano_id'].isin(set(panos))]))
    diag['sigma'] = sigma_from_pairs(pairs)
    diag['by_label_type'] = {lt: sigma_from_pairs(g) for lt, g in pairs.groupby('label_type')}
    return diag


def study(csv_dir, pano_list=None):
    frames = []
    for path in sorted(glob.glob(os.path.join(csv_dir, '*.csv'))):
        city = os.path.splitext(os.path.basename(path))[0]
        df = load_city(path)
        df['city'] = city
        frames.append(df)
    allc = pd.concat(frames, ignore_index=True)

    out = {'primary_radius_deg': PRIMARY_RADIUS_DEG, 'n_labels': int(len(allc))}
    # Matched mode is opt-in and additive: absent a --pano-list the key is not emitted at all, so the
    # committed clustered numbers reproduce unchanged and no force-paired sigma can appear by default.
    if pano_list is not None:
        out['matched'] = matched_study(allc, pano_list)

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
    ap.add_argument('--pano-list', metavar='FILE',
                    help='one pano_id per line: the agreed crossed block for matched mode. Without '
                         'it, matched mode falls back to whatever panos two users happen to share, '
                         'which on a shared *route* is far fewer than intended.')
    args = ap.parse_args(argv)

    result = {'source': '/v3/api/rawLabels?filetype=csv', 'fetched': args.fetched}
    result.update(study(args.csv_dir,
                        read_pano_list(args.pano_list) if args.pano_list else None))

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

    if 'matched' in result:
        m = result['matched']
        print(f"matched mode: {m['n_panos_in_list']} panos in the block, "
              f"{m['n_panos_with_two_users']} of them with two labellers, "
              f"{m['n_pairs_matched']} pairs matched 1:1  "
              f"(dropped {m['n_dropped_unlocated_referent']} unlocated-referent labels, "
              f"rejected {m['n_rejected_beyond_max_sep']} beyond {m['max_sep_deg']:g} deg)")
        ms = m['sigma']
        print(f"  sigma_az {fmt(ms['sigma_az_deg'], '.3f')} deg  "
              f"sigma_el {fmt(ms['sigma_el_deg'], '.3f')} deg"
              f"   (no radius sweep: identity is by design)")
    else:
        print('matched mode: not run (pass --pano-list with the agreed crossed block)')

    if args.write:
        with open(args.write, 'w') as f:
            json.dump(result, f, indent=1, allow_nan=False)
        print(f'wrote {args.write}')


if __name__ == '__main__':
    main()
