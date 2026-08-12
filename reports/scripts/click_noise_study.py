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

Two populations, not one. Every clustered figure is computed on all labels; matched mode and its
`comparable_only` companion are computed on labels with a located referent
(`rawlabels.has_located_referent`, which drops Crosswalk/NoSidewalk/Occlusion and brick-tagged
SurfaceProblems -- 100,636 of 436,348 on the six-city corpus). `study()['populations']` states which
figure sits on which frame, because a sigma from one compared against a sigma from the other is a
comparison across corpora, and both used to be printed in one column with nothing saying so.

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

# How close two labels by the SAME user must be, in matched mode, to be read as one placement
# repeated rather than two nearby objects.
#
# Deliberately far below PRIMARY_RADIUS_DEG. Matched mode exists precisely to measure objects that
# sit closer together than the clustering radius -- reusing 1.5 deg here would collapse the very
# blocks it was built for (two ramps 1.0 deg apart are two placements, and a test pins that). A
# same-user repeat is a near-exact duplicate: the measured within-user click precision is ~0.3 deg
# per axis, and the observed double-submits sit at ~0.05 deg. 0.25 deg separates those cleanly.
#
# This is a convention, not a measurement, and it is the only place matched mode makes one. Two
# genuinely distinct objects closer together than this would be merged; nothing in the record can
# tell that case from a repeat, which is the same honest limit dpr2/zoom_desync attribution has.
REPEAT_RADIUS_DEG = 0.25

# Cap on the exhaustive assignment search, per (pano, label_type, user pair). 5040 = 7!, so any
# realistic block is solved exactly; beyond it the search degrades to greedy and is counted.
ASSIGNMENT_MAX_MAPS = 5040

# Which frame each figure in `study()`'s output is computed on. Two populations, 100,636 labels apart
# on the six-city corpus: most figures run on every row, while matched mode runs on the
# referent-filtered subset (matched_study's exclude_unlocated). They used to sit in one dict and print
# in one column with nothing saying so, which is how a matched sigma and a clustered sigma came to be
# quoted as a single comparison.
#
# Grouped by population rather than by estimator, because the two do not line up: `comparable_only` is
# the *clustered* estimator run on the frame *matched* mode uses, and that is exactly the figure the
# defect was missing. Naming the members here rather than in prose means `study()` can publish the
# mapping and a test can assert every emitted figure is claimed by exactly one side.
FIGURES_ON_ALL_LABELS = ('overall', 'by_label_type', 'by_depression_band', 'radius_sweep',
                         'validated_only')
FIGURES_ON_COMPARABLE = ('comparable_only', 'matched')


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


def _drop_same_user_repeats(g, radius_deg):
    """Keep one placement per user per object: drop a label that repeats an EARLIER label by the
    same user within `radius_deg`.

    matched_pairs claimed to do this and did not -- `sort_values('time_created')` orders rows and
    enforces nothing, while the clustered estimator does the real work with
    drop_duplicates('user_id', keep='first') inside each cluster. Without it, a double-submit
    becomes a surplus label that the one-to-one assignment must pair with something, so it forces a
    match against a genuinely different object: u1 clicking one ramp twice (az 10.0, 10.05) against
    u2's two ramps (10.2, 14.0) produced 2 pairs and sigma_az 2.175 deg where the clustered
    estimator, which dedupes, reports one pair at 0.20 deg.

    Radius-based rather than a blanket drop_duplicates('user_id'): the group here is a whole
    (pano, label_type), which legitimately holds several distinct objects -- a corner's four curb
    ramps are four placements by one user, not three repeats. And the radius is REPEAT_RADIUS_DEG,
    not the clustering radius: matched mode exists to measure objects closer together than the
    clustering radius, so reusing it here would collapse exactly the blocks the mode was built for.
    """
    keep = []
    for user, sub in g.groupby('user_id', sort=False):
        kept_az, kept_el = [], []
        for label, row in sub.iterrows():
            az, el = row['_az'], row['_el']
            duplicate = False
            for a, e in zip(kept_az, kept_el):
                daz = abs(az - a)
                daz = min(daz, 360.0 - daz)
                mean_el = np.radians((el + e) / 2)
                if np.hypot(daz * np.cos(mean_el), el - e) <= radius_deg:
                    duplicate = True
                    break
            if not duplicate:
                keep.append(label)
                kept_az.append(az)
                kept_el.append(el)
    return g.loc[[label for label in g.index if label in set(keep)]]


def matched_pairs(df, panos=None, max_sep_deg=MATCH_MAX_SEP_DEG,
                  repeat_radius_deg=REPEAT_RADIUS_DEG):
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
    not, it force-pairs whatever is left, because a corner's four curb ramps get paired across users
    almost arbitrarily. Measured on the six-city production corpus, sigma_el (deg):

                            all 436,348 labels    comparable 335,712 labels
        clustered (r=1.5)         0.507                     0.507
        matched (no block)        0.967                     0.921

    Both halves of that comparison have to name a column, because this mode runs referent-filtered
    (`matched_study`) while every clustered figure in `study()` runs on all labels -- an earlier
    one-line version of this docstring quoted 0.967 against 0.507 without saying which frame either
    came from. The columns are what `study()['populations']` now records. Reading it: the population
    costs at most 0.046 deg of sigma_el and the estimator costs 0.41-0.46, so the gap is the estimator
    on either frame -- which is the claim this paragraph is making, now with the confound measured
    rather than assumed away.

    The clustered row is the committed `overall` and `comparable_only`. The matched row is a
    diagnostic (measured 2026-08-12 by calling `matched_pairs(allc)` and `matched_study(allc,
    every_pano)` on the 2026-08-09 corpus) and is deliberately in no artifact: `study()` computes this
    only when a pano list is supplied, since a plausible-looking sigma from force-paired distinct
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
        # One placement per user per object: a double-submit is not an independent placement, and
        # the earliest is the one the clustering estimator keeps too.
        g = _drop_same_user_repeats(g.sort_values('time_created'), repeat_radius_deg)
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


def replayable_geometry(df):
    """Rows whose pano geometry can be turned into an angle at all.

    Split out of load_city so every consumer of the angular estimators applies the SAME predicate.
    mapillary_census.crossed_block reached them straight from a rawLabels frame and skipped this,
    so a row whose pano metadata never resolved produced a NaN cost -- which max_sep_deg cannot
    reject, because NaN > 10.0 is False -- and the NaN travelled into sigma_az_deg / d_total_p50
    and aborted json.dump(allow_nan=False) on the run's last line.
    """
    return (df['pano_width'].gt(0) & df['pano_height'].gt(0)
            & df['pano_x'].notna() & df['pano_y'].ge(0) & df['pano_y'].le(df['pano_height']))


def load_city(path):
    return rawlabels.load_rawlabels(path).pipe(lambda d: d[replayable_geometry(d)])


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

    # The two estimators do not run on the same labels, and that belongs in the artifact rather than
    # in a reader's head. `matched` goes through matched_study, which applies has_located_referent;
    # every clustered figure below is computed on the full frame. On the six-city corpus those two
    # populations are 100,636 labels apart, and it is not a random 23%: the rule drops Crosswalk,
    # NoSidewalk and Occlusion outright -- three of the arms `by_label_type` reports, and two of the
    # three loosest in azimuth. Printed side by side with nothing recording the difference, the two
    # sigmas read as one comparison, which is how "matched 0.967 vs clustered 0.507" came to be
    # quoted as if both halves were over the same corpus.
    comparable = rawlabels.has_located_referent(allc)
    out = {
        'primary_radius_deg': PRIMARY_RADIUS_DEG,
        'n_labels': int(len(allc)),
        'populations': {
            # Each side names the keys it covers, so a consumer holding only the JSON can tell which
            # frame any figure came from, and a figure added later without being claimed here fails
            # a test rather than joining the artifact under no population at all.
            'all_labels': {'n_labels': int(len(allc)), 'referent_filtered': False,
                           'figures': list(FIGURES_ON_ALL_LABELS)},
            'comparable': {'n_labels': int(comparable.sum()), 'referent_filtered': True,
                           'figures': [k for k in FIGURES_ON_COMPARABLE
                                       if k != 'matched' or pano_list is not None]},
            'n_dropped_unlocated_referent': int((~comparable).sum()),
            'dropped_by_label_type': {str(t): int(n) for t, n in
                                      allc.loc[~comparable, 'label_type'].value_counts().items()},
        },
    }
    # Matched mode is opt-in and additive: absent a --pano-list the key is not emitted at all, so the
    # committed clustered numbers reproduce unchanged and no force-paired sigma can appear by default.
    if pano_list is not None:
        out['matched'] = matched_study(allc, pano_list)

    clustered = cluster_labels(allc, PRIMARY_RADIUS_DEG)
    pairs = cluster_pairs(clustered)
    out['overall'] = sigma_from_pairs(pairs)

    # The like-for-like companion to `matched`: same estimator, same radius, on the frame matched
    # mode actually uses. It is also the floor #54 should budget against in its own right -- the
    # placement study measures displacement, so it can only run on labels that have a referent to be
    # displaced from, and the arms this drops are the ones with no compact object to centre on.
    out['comparable_only'] = sigma_from_pairs(cluster_pairs(
        cluster_labels(allc[comparable], PRIMARY_RADIUS_DEG)))

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
    p = result['populations']
    print(f"pairs {o['n_pairs']} clusters {o['n_clusters']}  "
          f"sigma_az {fmt(o['sigma_az_deg'], '.3f')} deg  sigma_el {fmt(o['sigma_el_deg'], '.3f')} deg"
          f"  [all {p['all_labels']['n_labels']} labels]")
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
    # Printed with its population, and next to the sigma it is the like-for-like partner of: every
    # line above is over all labels, this one and `matched` below are over the referent-filtered
    # subset. Two sigmas in one column of output with no populations attached is the defect.
    c = result['comparable_only']
    print(f"  comparable-only n={c['n_pairs']:6d}  sigma_el {fmt(c['sigma_el_deg'], '.3f')}"
          f"   [referent-filtered: {p['comparable']['n_labels']} labels, "
          f"-{p['n_dropped_unlocated_referent']} {p['dropped_by_label_type']}]")

    if 'matched' in result:
        m = result['matched']
        print(f"matched mode: {m['n_panos_in_list']} panos in the block, "
              f"{m['n_panos_with_two_users']} of them with two labellers, "
              f"{m['n_pairs_matched']} pairs matched 1:1  "
              f"(dropped {m['n_dropped_unlocated_referent']} unlocated-referent labels, "
              f"rejected {m['n_rejected_beyond_max_sep']} beyond {fmt(m['max_sep_deg'], 'g')} deg)")
        ms = m['sigma']
        print(f"  sigma_az {fmt(ms['sigma_az_deg'], '.3f')} deg  "
              f"sigma_el {fmt(ms['sigma_el_deg'], '.3f')} deg"
              f"   (no radius sweep: identity is by design)")
        # Name the number it should be compared against. `overall` is the one directly above it in
        # this output and is the WRONG comparison -- different population.
        print(f"  compare against comparable-only sigma_el {fmt(c['sigma_el_deg'], '.3f')} "
              f"(same {p['comparable']['n_labels']}-label frame), not the "
              f"{fmt(o['sigma_el_deg'], '.3f')} above (all {p['all_labels']['n_labels']})")
    else:
        print('matched mode: not run (pass --pano-list with the agreed crossed block)')

    if args.write:
        # newline='\n' like every other study script: the default on Windows writes CRLF, so a
        # regeneration there rewrites all 300 lines of the artifact and buries the real diff.
        with open(args.write, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(result, f, indent=1, allow_nan=False)
        print(f'wrote {args.write}')


if __name__ == '__main__':
    main()
