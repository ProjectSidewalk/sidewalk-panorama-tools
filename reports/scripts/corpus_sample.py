"""Phase 2 corpus draw: select the labels the crop-priors gold standard will be annotated on.

The pre-registration (reports/2026-08-09-crop-priors-prereg.md §3) freezes the spec; this module is
the implementation, and it is the last point at which a design mistake is cheap. After this, labels
have been annotated and a mis-provisioned stratum means a study column that turns out not to be
estimable *after* the labour is spent.

Four properties do the work, and each exists because the obvious alternative silently corrupts a
downstream estimate:

* **One rig per frame.** GSV, Mapillary and infra3d panos come from different cameras with different
  tilt distributions, and rawLabels' `pano_source` is the only authority on which is which. Every
  other study in this repo globs a directory and trusts the caller to have kept the cache trees
  apart; `load_frame` checks instead, because pooling rigs would move every population weight and
  nothing downstream could detect it.
* **Weights describe the frame, the draw describes the strata.** The draw is deliberately far from
  proportional — it fills thin cells and forces oversamples — so every corpus-level claim has to be
  reweighted back to the label population. Computing those weights from the draw instead of the frame
  would make the reweighting a tautology. `weight_coverage` then reports the share of the population
  living in cells the draw did not reach, which is the share for which a reweighted estimate is simply
  undefined.
* **Split by pano, never by label.** Labels sharing a pano share imagery, camera metadata and one
  labeller's placement habits. A label-wise 50/50 leaks eval into tune and Study 2's candidate
  comparison would be scored against data it was fitted on.
* **Forced strata are drawn first.** Cell fill is the flexible part; the within-pano contrast stratum,
  the resolution oversample and the replay-mismatch stratum are the parts the frame may be unable to
  supply. Drawing them first, and reporting `shortfalls` when the frame comes up short, is what keeps
  "not provisioned" from being discovered later and misread as "underpowered".

Usage (offline; reads the rawLabels cache):
    python corpus_sample.py <dir-of-city-csvs> --source gsv --fetched 2026-08-12 --seed 20260812 \\
        --write-corpus ../data/2026-08-12-crop-corpus-gsv.csv.gz \\
        --write-manifest ../data/2026-08-12-crop-corpus-gsv.json
"""

import argparse
import collections
import glob
import hashlib
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import era_replay_study  # noqa: E402
import mapillary_census  # noqa: E402
import offaxis_covariate  # noqa: E402
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402
from era_replay_study import QUALITY_LEVELS, era_quality  # noqa: E402,F401
from studyfmt import fmt, num  # noqa: E402

# Deployments that are not populations, and must not be in a frame whose whole purpose is to weight
# claims back to the labels Project Sidewalk actually holds:
#   validation-study -- a research deployment (10,809 labels), not a city anyone browses;
#   la-piedad-old    -- superseded by la-piedad, so including both double-counts one city.
# winterthur-infra3d needs no entry: it serves a 0-label export and drops out as empty.
NOT_A_POPULATION = frozenset({'validation-study', 'la-piedad-old'})

# §3's per-cell target over (band x era-quality x label type).
CELL_TARGET = 6

# §3: "one pano never contributes > 3 labels". A pano is one scene, one camera pose and usually one
# labeller; three labels from it are worth much less than three labels from three panos.
MAX_LABELS_PER_PANO = 3

# §3's forced oversamples. The resolution one exists because the deployed sizing formula is
# resolution-dependent (1.198x inflation on 8192-height panos) while non-8192 panos are only ~3% of
# the corpus -- drawn proportionally the stratum would be ~20 labels and could not resolve #32.
STANDARD_HEIGHT = 8192.0
RESOLUTION_TARGET = 60
MISMATCH_TARGET = 30

# §3's forced multi-label-pano stratum: 80 panos contributing 2-3 labels at separated bearings. Note
# this is a DRAW target and is deliberately above mapillary_census.WITHIN_PANO_PANOS_REQUIRED (60),
# which is the separate threshold at which §2.3 reports its robustness column "not estimable" --
# drawing to the gate exactly would leave no margin for labels lost at annotation to
# object-absent/ambiguous flags.
CONTRAST_PANOS_TARGET = 80

# Occlusion ("Can't see the sidewalk") marks the view rather than a thing in it and has no crop
# consumer, so §3 excludes it corpus-wide. This is a smaller set than the referent rule applied by
# `rawlabels.has_located_referent`, and the difference is load-bearing: see `prepare`.
CORPUS_EXCLUDED_TYPES = frozenset({'Occlusion'})

SPLIT_LEVELS = ('tune', 'eval')

# Columns written to the committed corpus file: the verbatim record every downstream study replays
# from, plus the derived strata. Written out in full so the corpus survives rawLabels drift -- a
# re-fetch will not reproduce it (labels accrue, gsv_data refreshes), and the annotation is spent
# against these rows and no others.
CORPUS_COLUMNS = (
    'label_uid', 'label_id', 'city', 'pano_id', 'label_type', 'user_id', 'severity', 'tags',
    'time_created', 'image_capture_date',
    'heading', 'pitch', 'zoom', 'canvas_x', 'canvas_y', 'canvas_width', 'canvas_height',
    'pano_x', 'pano_y', 'pano_width', 'pano_height',
    'camera_heading', 'camera_pitch', 'camera_roll', 'latitude', 'longitude',
    'agree_count', 'disagree_count', 'unsure_count',
    'era', 'quality', 'band', 'depression', 'dbear',
    'exact_x', 'exact_y', 'replay_mismatch', 'disputed', 'referent', 'measurable',
    'roles', 'split',
)


def provenance(paths):
    """Per-file sha256, size and label-row count, so a draw records exactly which bytes it read.

    rawLabels is a moving target: the six-city snapshot the Phase 1 artifacts were computed on differs
    from a re-fetch on every single file (cdmx gained 15 labels between 2026-08-09 and 2026-08-12).
    Without a hash there is no way to tell afterwards which fetch a corpus came from, and the corpus is
    the thing annotation labour is spent against.
    """
    out = []
    for path in sorted(paths):
        with open(path, 'rb') as f:
            payload = f.read()
        # Row count from the parsed frame rather than by counting newlines: rawLabels' `description`
        # and `tags` fields are quoted and can contain embedded newlines, so a byte-level count
        # over-reports. usecols keeps it cheap on a 143 MB city.
        n_rows = len(pd.read_csv(path, usecols=['label_id']))
        out.append({'city': os.path.splitext(os.path.basename(path))[0],
                    'file': os.path.basename(path),
                    'sha256': hashlib.sha256(payload).hexdigest(),
                    'bytes': len(payload),
                    'n_rows': int(n_rows)})
    return out


def load_frame(paths, source):
    """Load every city CSV of one imagery rig into one frame. Returns (frame, provenance, excluded).

    The rig is decided by `pano_source`, which rawLabels serves and which is authoritative — not by
    which directory the file sits in. That is a deliberate inversion of the convention the other
    studies follow: they glob a directory and trust the operator to have kept the three cache trees
    apart, which is a hazard `fetch_rawlabels.py`'s docstring spends three paragraphs on. Checking the
    column instead means pointing this at the all-deployments cache draws the GSV arm correctly
    rather than silently pooling Richmond's Mapillary rig and Zurich's infra3d rig into it.

    A deployment that is *wholly* another rig is excluded and RECORDED — the returned `excluded` list
    goes into the manifest, so nothing leaves the frame without appearing in the artifact. A single
    deployment carrying *more than one* rig raises instead: that means `pano_source` is not reliable
    for that city, and no per-row filter can be trusted to fix it.
    """
    frames, prov, excluded = [], [], []
    for path in sorted(paths):
        city = os.path.splitext(os.path.basename(path))[0]
        df = rawlabels.load_rawlabels(path)
        if df.empty:
            continue
        if city in NOT_A_POPULATION:
            excluded.append({'city': city, 'rig': None, 'n_rows': int(len(df)),
                             'reason': 'not a population'})
            continue
        if 'pano_source' not in df:
            raise ValueError(f'{city}: no pano_source column; re-fetch this export before drawing a '
                             f'corpus from it (the rig cannot be verified without it)')
        found = {str(s) for s in df['pano_source'].dropna().unique()}
        if len(found) > 1:
            raise ValueError(f'{city}: refusing to pool imagery rigs -- one deployment carries '
                             f'{sorted(found)}. pano_source is unreliable here; resolve it upstream '
                             f'rather than filtering per row.')
        if found and source not in found:
            excluded.append({'city': city, 'rig': sorted(found)[0], 'n_rows': int(len(df)),
                             'reason': f'different rig (drawing {source})'})
            continue
        df['city'] = city
        frames.append(df)
        prov.extend(provenance([path]))
    if not frames:
        return pd.DataFrame(), prov, excluded
    return pd.concat(frames, ignore_index=True), prov, excluded


def prepare(df):
    """Replay every row, attach the strata, and mark corpus eligibility and measurability.

    Two masks, not one, and the difference is the part the pre-registration predates:

    * `corpus_eligible` is §3's corpus -- 8 label types, Occlusion excluded as having no crop
      consumer. Study 2 sizes crops for all of them.
    * `measurable` additionally applies the referent rule (`rawlabels.has_located_referent`), which
      removes Crosswalk, NoSidewalk and region-tagged SurfaceProblem. Those are valid labels with real
      crop consumers; what they cannot be is a subject for a stored-vs-gold *displacement*, because a
      label correctly placed anywhere along an extended feature has no point to be displaced from.

    Reading the referent rule as "types to drop" would remove 22.6% of the label population from the
    sizing study, which is why the two masks are carried separately all the way into the manifest.
    """
    if 'city' not in df:
        raise ValueError('prepare() needs a `city` column: label_id is only unique WITHIN a '
                         'deployment (label ids restart at 1), so a multi-city frame has no scalar '
                         'label identity. load_frame() sets it.')
    out = era_replay_study.replay_frame(df)

    # The label's identity across deployments, and the reason it is not `label_id`. Measured over
    # three deployments: 90,369 of 316,735 rows share a label_id with a different city (seattle-wa 9
    # and oradell-nj 9 are different labels on different panos). Keying the draw on the bare integer
    # made one city's label displace another's -- it silently drew 449 labels instead of 763, 314
    # lost, and left 50 of the 98 occupied strata cells short (22 more never occupied at all) while
    # the frame held thousands of candidates for every one of them. pano_id, by
    # contrast, does NOT collide across cities (0 cases over the same frames), which is why the
    # per-pano cap and the tune/eval split are safe on it.
    out['label_uid'] = out['city'].astype(str) + ':' + out['label_id'].astype('int64').astype(str)

    out['quality'] = era_quality(out['time_created']).to_numpy()
    out['depression'] = pov_replay.depression_from_pano_y(out['pano_y'], out['pano_height'])
    out['band'] = offaxis_covariate.depression_band(out['depression']).astype(object).to_numpy()
    out['dbear'] = mapillary_census.delta_bearing(out)

    # There is deliberately NO in-frame guard on pano_y here, though §3 lists one. It is implied
    # TWICE over, and a mutation battery was what established which of the two actually does the work:
    #
    #  * exact_y implies it. replay_y = H/2 - round((H/2)·pov_pitch/90) with pov_pitch from an arcsin,
    #    so replay_y always lands in [0, H]; exact_y additionally requires a finite positive height and
    #    stored_y == replay_y exactly. So a stored pano_y outside the frame -- including the two
    #    corrupt negative rows found corpus-wide, Seattle 231546/233419 -- cannot replay at all.
    #  * The band guard implies it, and this is the one that fires first. depression =
    #    (y - H/2)·180/H, so y outside [0, H] is exactly depression outside [-90, 90], which is
    #    outside BAND_EDGES and lands as NaN. This is the same "band guard IS load-bearing" property
    #    offaxis_covariate.prepare documents, and it is why an in-frame term here is unreachable:
    #    a fixture built to exercise it gets rejected by the band before the frame check would run.
    #
    # Both implications are pinned as tests, because they are the justification for the absent term
    # rather than incidental facts -- if the projection ever let replay_y leave [0, H], or the bands
    # ever stopped spanning the full ±90, the guard would have to come back and those tests say so.
    # Same reasoning, and the same precedent, as the absent guards in offaxis_covariate.prepare.

    # exact_y and NOT exact_x: the record reproduces its own pano_y but not its pano_x, i.e. stale
    # only in viewport heading. 58% of record misses look like this, and the corpus keeps them --
    # §3's replay-mismatch stratum is defined over exactly these rows, so excluding them would leave
    # the stratum defined over rows the exclusion had already removed.
    out['replay_mismatch'] = out['exact_y'].to_numpy() & ~out['exact_x'].to_numpy()

    agree = out['agree_count'].fillna(0).to_numpy(float)
    disagree = out['disagree_count'].fillna(0).to_numpy(float)
    out['disputed'] = disagree > agree

    out['referent'] = rawlabels.has_located_referent(out).to_numpy()
    out['corpus_eligible'] = (
        out['exact_y'].to_numpy()
        & pd.notna(out['band']).to_numpy()
        # The quality guard is NOT implied by anything above it, unlike the absent pano_y term. A
        # label with no `time_created` replays fine and lands in a band; what it has no answer for is
        # which side of the 7.20.7 deploy it fell on, which is one of the three axes the draw
        # stratifies over. `era_quality` reports that as None rather than as the default level, and a
        # row with an unknown stratum has to leave the frame rather than fill a cell in it.
        & pd.notna(out['quality']).to_numpy()
        & (out['pano_id'].astype(str) != 'tutorial').to_numpy()
        & ~out['label_type'].isin(CORPUS_EXCLUDED_TYPES).to_numpy())
    out['measurable'] = out['corpus_eligible'].to_numpy() & out['referent'].to_numpy()
    return out


def cell_key(band, label_type):
    """The reweighting cell: §3 reweights to the population's depression x type distribution."""
    return f'{band}|{label_type}'


def population_weights(prepared):
    """The label population's share in each (band x type) cell, over the whole frame.

    Deliberately NOT computed on the draw. The draw fills thin cells to a flat target and forces
    oversamples, so its own distribution is an artefact of the design; the point of the weights is to
    map it back to the population the cropper actually serves.
    """
    g = prepared[prepared['corpus_eligible']]
    counts = collections.Counter(cell_key(b, t) for b, t in zip(g['band'], g['label_type']))
    total = sum(counts.values())
    if not total:
        return {}
    return {k: v / total for k, v in sorted(counts.items())}


def weight_coverage(weights, drawn):
    """How much of the weighted population the draw actually supports.

    A cell carrying population weight but no drawn label is not a rounding detail: a reweighted
    estimate is undefined for that share of the population, and averaging over the cells that *are*
    supported quietly redistributes the missing mass onto them. Reported as a number so the manifest
    states it rather than a reader assuming it is zero.
    """
    supported = {cell_key(b, t) for b, t in zip(drawn['band'], drawn['label_type'])}
    missing = {k: v for k, v in weights.items() if k not in supported}
    total = sum(weights.values())
    return {
        'cells_total': len(weights),
        'cells_supported': len(weights) - len(missing),
        'cells_unsupported': len(missing),
        'unsupported_population_pct': float(100.0 * sum(missing.values()) / total) if total else None,
        'unsupported_cells': {k: num(100.0 * v / total) for k, v in
                              sorted(missing.items(), key=lambda kv: -kv[1])} if total else {},
    }


def separated_pairs(group, separation_deg=mapillary_census.BEARING_SEPARATION_DEG):
    """`label_uid` pairs within one pano whose bearings clear the §2.3 gate, widest pair first.

    Widest first because the pair is what identifies the tilt term: at Δb separation 0 the two labels
    see the same slice of the rig's tilt and contribute nothing to the within-pano contrast, and the
    gate is a floor rather than a target.

    Returns uids, not label_ids, for the same reason everything else here does: a bare label_id does
    not identify a label across deployments.
    """
    b = group['dbear'].to_numpy(float)
    ids = group['label_uid'].to_numpy()
    pairs = []
    for i, j in itertools.combinations(range(len(group)), 2):
        sep = float(mapillary_census.bearing_separation(b[i], b[j]))
        if np.isfinite(sep) and sep >= separation_deg:
            pairs.append((sep, ids[i], ids[j]))
    return [(a, c) for _, a, c in sorted(pairs, key=lambda t: -t[0])]


def contrast_pano_count(drawn, separation_deg=mapillary_census.BEARING_SEPARATION_DEG):
    """Panos in the draw that carry the within-pano contrast, via the canonical §2.3 predicate.

    Correct but O(panos) in Python with an itertools pass per pano, which is fine for a ~700-row draw
    and hopeless for the GSV frame's 556,775 panos — see `contrast_panos_available` for the
    frame-scale version and why the two agree exactly.
    """
    return int(mapillary_census.within_pano_stratum(drawn, separation_deg)['n_panos_separated'])


def contrast_panos_available(df, separation_deg=mapillary_census.BEARING_SEPARATION_DEG):
    """Same count as `contrast_pano_count`, vectorized, for use on the whole frame.

    A fast replica pinned against the canonical predicate rather than a second opinion about it —
    the same arrangement `clamp_census.predict_crop_size` has with CropRunner's scalar original, and
    for the same reason: the canonical version takes minutes over half a million panos.

    Method: sort each pano's bearings, find its largest circular gap G, and take the arc the points
    occupy, S = 360 - G. Then "some pair is at least `separation_deg` apart" is exactly "S >=
    separation_deg", which holds because:

      * if S <= 180, the two ends of the arc ARE the widest pair, at circular distance S;
      * if all pairwise distances were under `separation_deg`, then fixing any point puts every other
        point within `separation_deg` of it, so S <= 2*separation_deg.

    Those two leave a gap only when 180 < S <= 2*separation_deg, which is empty for separation_deg
    <= 90 — hence the assertion. It is NOT a decorative bound: at separation_deg 150, three labels at
    Δb 0/120/240 give S = 240 and the span test would accept a pano whose widest pair is 120 deg.
    """
    if separation_deg > 90.0:
        raise ValueError(f'separation_deg {separation_deg} > 90: the span test is only equivalent to '
                         f'the pairwise predicate up to 90 deg; use contrast_pano_count instead')
    b = np.asarray(df['dbear'], float)
    ok = np.isfinite(b)
    if not ok.any():
        return 0
    codes = pd.factorize(np.asarray(df['pano_id'])[ok])[0]
    b = b[ok]
    order = np.lexsort((b, codes))
    k, a = codes[order], b[order]
    starts = np.flatnonzero(np.r_[True, k[1:] != k[:-1]])
    sizes = np.diff(np.r_[starts, len(k)])
    gaps = np.diff(a)
    if len(gaps):
        gaps[starts[1:] - 1] = -np.inf          # a diff spanning two panos is not a gap
    widest_gap = np.maximum.reduceat(np.r_[gaps, -np.inf], starts)
    wrap = 360.0 - (a[starts + sizes - 1] - a[starts])
    span = 360.0 - np.maximum(widest_gap, wrap)
    return int(np.sum((sizes >= 2) & (span >= separation_deg)))


def draw(prepared, seed, cell_target=CELL_TARGET):
    """The stratified draw. Returns the selected rows with a `roles` column naming why each is in.

    Order matters and is not cosmetic: the three forced strata run before cell fill because they are
    the parts a frame can fail to supply, and a row taken for a forced stratum still counts toward its
    cell. Running cell fill first would let it consume the per-pano budget of exactly the multi-label
    panos §2.3 needs.
    """
    rng = np.random.default_rng(seed)
    pool = prepared[prepared['corpus_eligible']].sort_values('label_uid', kind='stable')
    # A seeded permutation, so the draw depends on the seed and not on the order cities were globbed
    # in -- otherwise adding one deployment to the frame would reshuffle an unrelated stratum.
    pool = pool.iloc[rng.permutation(len(pool))]

    roles = collections.defaultdict(set)
    per_pano = collections.Counter()
    rows = {}
    # Running tallies rather than a rescan of `rows` per candidate: the forced-stratum loops used to
    # recount by iterating every selected row on every iteration, which is both quadratic and easy to
    # get wrong -- it was overshooting the resolution target because the rescan and the take path
    # disagreed about which rows counted.
    tally = collections.Counter()

    def take(row, role):
        uid = row['label_uid']
        if uid in roles:
            roles[uid].add(role)
            return True
        if per_pano[row['pano_id']] >= MAX_LABELS_PER_PANO:
            return False
        roles[uid].add(role)
        rows[uid] = row
        per_pano[row['pano_id']] += 1
        if row['pano_height'] != STANDARD_HEIGHT:
            tally['resolution'] += 1
        if row['replay_mismatch']:
            tally['mismatch'] += 1
        return True

    # 1. Within-pano contrast (§2.3): whole panos, two labels at separated bearings each.
    #    Pre-filtered to multi-label panos by a vectorized count before grouping: the GSV frame has
    #    556,775 panos, and materializing a DataFrame per pano for all of them cost more memory and
    #    wall-clock than the entire rest of the draw. The groupby is iterated lazily and abandoned at
    #    the target, so it touches a few hundred panos.
    sizes = pool['pano_id'].value_counts()
    multi = pool[pool['pano_id'].isin(sizes[sizes >= 2].index)]
    contrast_panos = 0
    for pid, g in multi.groupby('pano_id', sort=False):
        if contrast_panos >= CONTRAST_PANOS_TARGET:
            break
        pairs = separated_pairs(g)
        if not pairs:
            continue
        first, second = pairs[0]
        # drop=False: `take` reads row['label_uid'], so the column has to survive becoming the index.
        lookup = g.set_index('label_uid', drop=False)
        # Both halves or neither. A pair is the unit here -- one label alone carries no within-pano
        # contrast -- so taking the first and then finding the second refused would leave a row
        # tagged `contrast` that provides none, and `shortfalls` would count a stratum it does not
        # have. Unreachable as the strata are ordered today (this runs first, so per_pano is 0 for
        # every fresh pano and the cap cannot bind), which is exactly why it is asserted rather than
        # left to the ordering: reordering the strata is a one-line edit.
        if not take(lookup.loc[first], 'contrast'):
            continue
        if not take(lookup.loc[second], 'contrast'):
            raise AssertionError(
                f'pano {pid}: took one half of a contrast pair and the per-pano cap refused the '
                f'other. A lone contrast label is not a contrast pano; if a stratum now runs before '
                f'this one, take() needs a two-phase form rather than this guard.')
        contrast_panos += 1

    # 2. Resolution oversample (§3): every non-8192 served height, up to the target.
    for _, row in pool[pool['pano_height'] != STANDARD_HEIGHT].iterrows():
        if tally['resolution'] >= RESOLUTION_TARGET:
            break
        take(row, 'resolution')

    # 3. Replay-mismatch stratum (§3): the x_only rows, a projection-error covariate for Study 1.
    for _, row in pool[pool['replay_mismatch']].iterrows():
        if tally['mismatch'] >= MISMATCH_TARGET:
            break
        take(row, 'mismatch')

    # 4. Cell fill: top every occupied (band x quality x type) cell up to the target. A thin cell
    #    contributes what it has and is NOT topped up from a neighbour -- that would misreport the
    #    stratum a label was drawn from, which is the one thing a stratified design cannot survive.
    for _, g in pool.groupby(['band', 'quality', 'label_type'], observed=True, sort=False):
        have = sum(1 for uid in g['label_uid'] if uid in roles)
        for _, row in g.iterrows():
            if have >= cell_target:
                break
            if row['label_uid'] in roles:
                continue
            if take(row, 'cell'):
                have += 1

    if not rows:
        out = prepared.iloc[:0].copy()
        out['roles'] = pd.Series(dtype=object)
        return out

    out = pd.DataFrame(list(rows.values()))
    out['roles'] = [','.join(sorted(roles[uid])) for uid in out['label_uid']]
    return out.sort_values('label_uid', kind='stable').reset_index(drop=True)


def assign_split(drawn, seed):
    """Freeze the 50/50 tune/eval split, by pano and never by label (§2 Study 2).

    Two labels on one pano share imagery, camera pose and usually one labeller's habits, so splitting
    label-wise leaks the eval set into tuning: a sizing rule tuned on one label of a pano is already
    fitted to the pano the eval label is on.
    """
    out = drawn.copy()
    if out.empty:
        out['split'] = pd.Series(dtype=object)
        return out
    panos = np.array(sorted(out['pano_id'].unique()))
    order = np.random.default_rng(seed).permutation(len(panos))
    cut = len(panos) // 2
    tune = set(panos[order[:cut]])
    out['split'] = ['tune' if p in tune else 'eval' for p in out['pano_id']]
    return out


def shortfalls(drawn):
    """What the frame could not supply, per forced stratum.

    Reported rather than raised: a short stratum is a finding about the frame, and the run that
    discovers it should still produce a manifest saying so. A silent short corpus is how §2.3 would
    later be written up as "underpowered" when the truth is it was never provisioned.
    """
    achieved = {
        'contrast_panos': contrast_pano_count(drawn) if len(drawn) else 0,
        'resolution': int((drawn['pano_height'] != STANDARD_HEIGHT).sum()) if len(drawn) else 0,
        'mismatch': int(drawn['replay_mismatch'].sum()) if len(drawn) else 0,
    }
    required = {'contrast_panos': CONTRAST_PANOS_TARGET,
                'resolution': RESOLUTION_TARGET,
                'mismatch': MISMATCH_TARGET}
    return {k: {'achieved': achieved[k], 'required': required[k],
                'shortfall': max(0, required[k] - achieved[k])}
            for k in required}


def strata_table(drawn):
    """Achieved count per (band x quality x type) cell, for the manifest."""
    if drawn.empty:
        return {}
    g = drawn.groupby(['band', 'quality', 'label_type'], observed=True).size()
    return {f'{b}|{q}|{t}': int(n) for (b, q, t), n in g.items()}


# The six deployments Phase 1 measured every prior on, and which prereg §3 named as the corpus frame.
# Kept as a named reference rather than deleted: amendment 2 widened the frame, and the whole argument
# for widening is a comparison against exactly these six, so the comparison has to stay computable.
REFERENCE_CITIES = ('amsterdam', 'cdmx', 'columbus-oh', 'newberg-or', 'oradell-nj', 'seattle-wa')

# The three marginals the reweighting turns on. band x type is what §3 actually reweights over; the
# other two are reported because a frame can match on the reweighting key and still misrepresent the
# population badly on a dimension nobody weighted (era-quality, as it turned out).
COMPARISON_KEYS = {'label_type': ('label_type',), 'band': ('band',), 'quality': ('quality',),
                   'band_x_type': ('band', 'label_type')}


def distribution(prepared, keys):
    """Share of the corpus-eligible population in each level of `keys`, as percentages."""
    g = prepared[prepared['corpus_eligible']]
    counts = collections.Counter('|'.join(str(v) for v in vals)
                                 for vals in zip(*(g[k] for k in keys)))
    total = sum(counts.values())
    if not total:
        return {}
    return {k: 100.0 * v / total for k, v in sorted(counts.items())}


def total_variation_pct(a, b):
    """Total-variation distance between two percentage distributions, in percentage points.

    Half the summed absolute difference — the standard TV distance, so it reads as "the most any
    single reweighted claim could be shifted by using the wrong population", not as a sum of shifts.
    """
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b))


def frame_comparison(prepared, reference=REFERENCE_CITIES):
    """How the drawn frame's population differs from the six-city frame §3 originally named.

    This is the evidence for amendment 2, computed here rather than in a one-off script so the report
    quoting it can be checked against a committed artifact. `ratio` is frame/reference: above 1 means
    the six cities UNDER-represent that stratum relative to the population the cropper serves.
    """
    if 'city' not in prepared:
        return None
    ref = prepared[prepared['city'].isin(reference)]
    # Every count and every share here is under ONE filter, corpus-eligible, and says so in its key.
    # The first version mixed them -- raw row counts beside corpus-eligible percentages -- which is the
    # exact defect the six-city census hit when 81,667 eligible labels were quoted as 82,769 raw ones.
    n_ref = int(prepared['corpus_eligible'][ref.index].sum())
    n_frame = int(prepared['corpus_eligible'].sum())
    if not n_ref:
        # The reference cities are GSV deployments, so this comparison is meaningless for another rig
        # -- and it does not fail quietly, it fails *plausibly*: against a disjoint reference every
        # total-variation distance comes out at exactly 50.00 pp, which looks like a finding. The
        # Mapillary manifest shipped that once. Say "not applicable" instead of publishing 50.
        return {'reference_cities': sorted(reference), 'population': 'corpus_eligible',
                'applicable': False,
                'reason': 'none of the reference deployments are in this frame; the six-city '
                          'reference is GSV, so there is nothing to compare another rig against',
                'n_reference_corpus_eligible': 0,
                'n_frame_corpus_eligible': n_frame}
    out = {
        'applicable': True,
        'reference_cities': sorted(reference),
        'population': 'corpus_eligible',
        'n_reference_corpus_eligible': n_ref,
        'n_frame_corpus_eligible': n_frame,
        'reference_share_pct': num(100.0 * n_ref / n_frame) if n_frame else None,
        'total_variation_pct': {},
    }
    for name, keys in COMPARISON_KEYS.items():
        ref_d, frame_d = distribution(ref, keys), distribution(prepared, keys)
        out['total_variation_pct'][name] = num(total_variation_pct(ref_d, frame_d))
        if name != 'band_x_type':                 # 32 cells is a table; 120 is a data dump
            out[f'by_{name}'] = {
                k: {'reference_pct': num(ref_d.get(k, 0.0)), 'frame_pct': num(frame_d.get(k, 0.0)),
                    'ratio': num(frame_d.get(k, 0.0) / ref_d[k]) if ref_d.get(k) else None}
                for k in sorted(set(ref_d) | set(frame_d))}
    ref_cells = set(distribution(ref, ('band', 'quality', 'label_type')))
    frame_cells = set(distribution(prepared, ('band', 'quality', 'label_type')))
    # The property that LICENSES reweighting a six-city draw to the wider population, and the reason
    # widening the draw is a convenience rather than a necessity: the six cities already occupy every
    # stratum cell the full frame does, so no cell carrying population weight lacks reference support.
    out['strata_cells'] = {'reference': len(ref_cells), 'frame': len(frame_cells),
                           'only_in_frame': sorted(frame_cells - ref_cells),
                           'only_in_reference': sorted(ref_cells - frame_cells)}
    return out


def frame_summary(prepared):
    """What the frame holds, before any drawing -- the denominators every share in the report is
    stated against."""
    corpus = prepared[prepared['corpus_eligible']]
    return {
        'n_labels': int(len(prepared)),
        'n_corpus_eligible': int(len(corpus)),
        'n_measurable': int(prepared['measurable'].sum()),
        'n_panos': int(prepared['pano_id'].nunique()),
        'n_panos_corpus_eligible': int(corpus['pano_id'].nunique()),
        'n_cities': int(prepared['city'].nunique()) if 'city' in prepared else None,
        'by_quality': {q: int((corpus['quality'] == q).sum()) for q in QUALITY_LEVELS},
        'by_band': {b: int((corpus['band'] == b).sum()) for b in offaxis_covariate.BAND_LABELS},
        'by_label_type': {str(t): int(n) for t, n in
                          corpus['label_type'].value_counts().items()},
        'n_nonstandard_height': int((corpus['pano_height'] != STANDARD_HEIGHT).sum()),
        'n_replay_mismatch': int(corpus['replay_mismatch'].sum()),
        'n_contrast_panos_available': contrast_panos_available(corpus) if len(corpus) else 0,
    }


def draw_summary(drawn, weights):
    """What the draw achieved, including the reweighting coverage that licenses it."""
    if drawn.empty:
        return {'n_labels': 0}
    return {
        'n_labels': int(len(drawn)),
        'n_panos': int(drawn['pano_id'].nunique()),
        'n_measurable': int(drawn['measurable'].sum()),
        'labels_per_pano_max': int(drawn.groupby('pano_id').size().max()),
        'by_role': {r: int(drawn['roles'].str.contains(r).sum())
                    for r in ('cell', 'contrast', 'resolution', 'mismatch')},
        'by_quality': {q: int((drawn['quality'] == q).sum()) for q in QUALITY_LEVELS},
        'by_band': {b: int((drawn['band'] == b).sum()) for b in offaxis_covariate.BAND_LABELS},
        # The same bands under Study 1's filter, and not a redundant key: §5's power table is stated
        # per depression band, and the two studies read different populations. The corpus is 190.8
        # labels/band on average and the measurable subset is 146.0, so quoting the corpus figure at a
        # power claim about Study 1 overstates every band by 30% -- which is exactly the mistake the
        # first draft of amendment 2(d) made.
        'by_band_measurable': {b: int(((drawn['band'] == b) & drawn['measurable']).sum())
                               for b in offaxis_covariate.BAND_LABELS},
        'by_label_type': {str(t): int(n) for t, n in drawn['label_type'].value_counts().items()},
        'by_city': {str(c): int(n) for c, n in drawn['city'].value_counts().items()}
        if 'city' in drawn else {},
        'by_split': {s: int((drawn['split'] == s).sum()) for s in SPLIT_LEVELS}
        if 'split' in drawn else {},
        'n_disputed': int(drawn['disputed'].sum()),
        'shortfalls': shortfalls(drawn),
        'weight_coverage': weight_coverage(weights, drawn),
        'strata': strata_table(drawn),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir', help='directory of <city>.csv rawLabels exports')
    ap.add_argument('--source', default='gsv',
                    help="imagery rig to draw (default gsv). One rig per draw: the Mapillary arm is "
                         "drawn separately by the same code path, never pooled.")
    ap.add_argument('--fetched', required=True, metavar='DATE',
                    help='the date the CSVs were fetched (rawLabels is a moving target, so a re-fetch '
                         'will not reproduce this draw; the corpus file is the frozen artifact)')
    ap.add_argument('--seed', type=int, required=True,
                    help='draw seed, recorded in the manifest so the draw is reproducible from it')
    ap.add_argument('--cell-target', type=int, default=CELL_TARGET)
    ap.add_argument('--write-corpus', metavar='CSV_GZ')
    ap.add_argument('--write-manifest', metavar='JSON')
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.csv_dir, '*.csv')))
    if not paths:
        ap.error(f'no *.csv rawLabels exports found in {args.csv_dir} '
                 f'(fetch them with fetch_rawlabels.py)')

    frame, prov, excluded = load_frame(paths, args.source)
    if frame.empty:
        ap.error(f'no {args.source} labels found in {args.csv_dir}')
    print(f'frame: {len(prov)} deployments, {len(frame):,} labels', flush=True)
    for e in excluded:
        print(f"  excluded {e['city']}: {e['reason']} ({e['n_rows']:,} labels)")

    prepared = prepare(frame)
    weights = population_weights(prepared)
    drawn = assign_split(draw(prepared, seed=args.seed, cell_target=args.cell_target),
                         seed=args.seed)

    manifest = {
        'source': '/v3/api/rawLabels?filetype=csv',
        'fetched': args.fetched,
        'rig': args.source,
        'seed': args.seed,
        'prereg': 'reports/2026-08-09-crop-priors-prereg.md',
        'spec': {
            'cell_target': args.cell_target,
            'max_labels_per_pano': MAX_LABELS_PER_PANO,
            'resolution_target': RESOLUTION_TARGET,
            'mismatch_target': MISMATCH_TARGET,
            'contrast_panos_target': CONTRAST_PANOS_TARGET,
            'contrast_estimability_gate': mapillary_census.WITHIN_PANO_PANOS_REQUIRED,
            'bearing_separation_deg': mapillary_census.BEARING_SEPARATION_DEG,
            'standard_height': STANDARD_HEIGHT,
            'excluded_deployments': sorted(NOT_A_POPULATION),
            'corpus_excluded_types': sorted(CORPUS_EXCLUDED_TYPES),
        },
        'provenance': prov,
        'excluded_deployments': excluded,
        'frame': frame_summary(prepared),
        'frame_comparison': frame_comparison(prepared),
        'population_weights': {k: num(v) for k, v in weights.items()},
        'draw': draw_summary(drawn, weights),
    }

    f, d = manifest['frame'], manifest['draw']
    print(f"corpus-eligible {f['n_corpus_eligible']:,} of {f['n_labels']:,} labels "
          f"({f['n_measurable']:,} measurable)")
    print(f"drew {d['n_labels']:,} labels over {d['n_panos']:,} panos "
          f"(max {d['labels_per_pano_max']} per pano)")
    print(f"  roles: {d['by_role']}")
    print(f"  split: {d['by_split']}")
    for name, s in d['shortfalls'].items():
        flag = '' if not s['shortfall'] else f"  *** SHORT by {s['shortfall']} ***"
        print(f"  {name}: {s['achieved']}/{s['required']}{flag}")
    cov = d['weight_coverage']
    print(f"  reweighting: {cov['cells_supported']}/{cov['cells_total']} population cells supported, "
          f"{fmt(cov['unsupported_population_pct'], '.2f')}% of the population unsupported")
    fc = manifest['frame_comparison']
    if fc and not fc.get('applicable'):
        print(f"frame vs the six reference cities: not applicable ({fc['reason']})")
    elif fc:
        print(f"frame vs the six reference cities ({fmt(fc['reference_share_pct'], '.1f')}% of it): "
              f"TV " + '  '.join(f'{k} {fmt(v, ".2f")}pp'
                                 for k, v in fc['total_variation_pct'].items()))
        print(f"  strata cells: reference {fc['strata_cells']['reference']}, "
              f"frame {fc['strata_cells']['frame']}, "
              f"only in frame {len(fc['strata_cells']['only_in_frame'])}")

    if args.write_corpus:
        cols = [c for c in CORPUS_COLUMNS if c in drawn]
        drawn[cols].to_csv(args.write_corpus, index=False, compression='gzip')
        print(f'wrote {args.write_corpus}')
    if args.write_manifest:
        with open(args.write_manifest, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(manifest, fh, indent=1, allow_nan=False, default=str)
        print(f'wrote {args.write_manifest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
