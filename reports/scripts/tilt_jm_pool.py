"""Endpoint C's high-quality label pool and its batch draws (#54 / #158, 2026-09-29).

The first C draw had no validation filter: about a third of its pool was crowd-incorrect, unvalidated,
or disagreed with by a lead labeller, and the judge found sheets whose label he would have voted down.
This pool keeps only labels the two lead labellers vouch for, across every deployment:

* made by `jonfroehlich` or `mikey`, or validated Agree by either, and
* no Disagree from either of them,

restricted to GSV panos, the four study types and the live measurable rule (rawlabels.study_measurable).
`validation-study` is left out: it re-serves other deployments' panos.

    # 1. the pool, from a rawLabels sweep (fetch_rawlabels.py --all) with its `validations` column
    python tilt_jm_pool.py pool --rawlabels-dir .cache/rawlabels-all-2026-09-12 \\
        --out ../data/2026-09-29-tilt-jm-pool.csv.gz
    # 2. its pano ids, then tilt_pose_scan.py --ids on the store host -> ../data/2026-09-29-tilt-pose-jm.csv.gz
    python tilt_jm_pool.py ids --pool ../data/2026-09-29-tilt-jm-pool.csv.gz --out jm_ids.csv
    # 3. one batch: 6 per type per era arm, one label per pano, <= 4 per city per arm, md5(seed:uid) order
    python tilt_jm_pool.py draw --pool ... --pose ... --seed 20260929 --min-abs-t 4 --out <adjudication dir>
    python tilt_jm_pool.py draw ... --seed 20260930 --min-abs-t 6 --exclude <batch 1 dir> --out <batch 2 dir>
    # #191's beta batch: windows at 0.5 / 1.0 / 1.5 T, both C batches' panos excluded
    python tilt_jm_pool.py draw ... --design beta --seed beta20260929 --min-abs-t 5 --cell-cap 2 \
        --exclude <batch 1 dir> --exclude <batch 2 dir> --out <beta dir>

`draw` writes selection.csv and crop_jobs.csv straight into <out>/sealed/ (both name each window's role)
and draw.json beside it; then tilt_remote_crop.py cuts the panels on the store host and
`tilt_adjudicate.py sheets`'s build_sheets renders them.
"""
import argparse
import glob
import hashlib
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rawlabels  # noqa: E402
import tilt_adjudicate as ta  # noqa: E402

LEAD_LABELLERS = {'549187e0-82c9-4014-a48d-31f18083d575': 'jon',      # jonfroehlich
                  '18b26a38-24ab-402d-a64e-158fc0bb8a8a': 'mikey'}   # mikey (read from prod 2026-09-29)
EXCLUDED_CITIES = ('validation-study',)
PER_TYPE, CITY_CAP = 6, 4


def lead_votes(cell):
    """{'jon'|'mikey': 'Agree'|'Disagree'|'Unsure'} from a rawLabels `validations` JSON cell."""
    try:
        vs = json.loads(cell) if isinstance(cell, str) else []
    except ValueError:
        return {}
    return {LEAD_LABELLERS[v['user_id']]: v['validation'] for v in vs if v.get('user_id') in LEAD_LABELLERS}


def vouched(made, votes):
    """The pool rule: made or validated Agree by a lead labeller, and no Disagree from either."""
    return (made or 'Agree' in votes.values()) and 'Disagree' not in votes.values()


def city_pool(path, city):
    df = rawlabels.load_rawlabels(path)
    extra = pd.read_csv(path, usecols=['label_id', 'user_id', 'validations', 'pano_source'], dtype={'user_id': str})
    df = df.drop(columns=[c for c in ('user_id', 'pano_source') if c in df.columns]).merge(
        extra, on='label_id', how='left')
    df['city'] = city
    df = df[rawlabels.study_measurable(df) & df['label_type'].isin(ta.STUDY_TYPES)
            & (df['pano_source'].fillna('gsv') == 'gsv')].copy()
    votes = df['validations'].map(lead_votes)
    made = df['user_id'].isin(list(LEAD_LABELLERS))
    keep = pd.Series([vouched(m, v) for m, v in zip(made, votes)], index=df.index, dtype=bool)
    df = df[keep].copy()
    df['lead_made'] = made[keep].astype(int).to_numpy()
    df['lead_votes'] = votes[keep].map(lambda v: json.dumps(v, sort_keys=True)).to_numpy()
    df['label_uid'] = city + ':' + df['label_id'].astype(int).astype(str)
    return df.drop(columns=['validations'])


def build_pool(rawlabels_dir):
    parts = []
    for path in sorted(glob.glob(os.path.join(rawlabels_dir, '*.csv'))):
        city = os.path.splitext(os.path.basename(path))[0]
        if city not in EXCLUDED_CITIES:
            parts.append(city_pool(path, city))
    return pd.concat(parts, ignore_index=True)


def rank(seed, uid):
    return hashlib.md5(('%s:%s' % (seed, uid)).encode()).hexdigest()


def stratified_draw(cands, seed, per_type=PER_TYPE, city_cap=CITY_CAP, exclude_panos=(), cell_cap=None):
    """Per era arm, per_type labels of each study type, one label per pano (across both arms and any
    excluded batch), in md5(seed:label_uid) order.

    The spread rule is either `city_cap` per city per arm (batch 1), or - when `cell_cap` is given -
    `cell_cap` per city per (arm, type) followed by a fill pass without it for any stratum still short
    (batch 2: at |T| >= 6 deg the supply sits in a few cities, and a flat city cap starved the rare types
    while the common ones used up each city's slots)."""
    c = cands.assign(_rank=[rank(seed, u) for u in cands['label_uid']]).sort_values('_rank')
    picked, used = [], set(exclude_panos)
    for arm in ('legacy+mid', 'post179'):
        per_t, per_c = {}, {}
        rows = list(c[c['era_arm'] == arm].itertuples())
        for fill_pass in ((False, True) if cell_cap else (False,)):
            for r in rows:
                ck = (r.label_type, r.city) if cell_cap else r.city
                cap = cell_cap if cell_cap else city_cap
                if r.pano_id in used or per_t.get(r.label_type, 0) >= per_type \
                        or (not fill_pass and per_c.get(ck, 0) >= cap):
                    continue
                picked.append(r.Index)
                used.add(r.pano_id)
                per_t[r.label_type] = per_t.get(r.label_type, 0) + 1
                per_c[ck] = per_c.get(ck, 0) + 1
    return c.loc[picked].drop(columns='_rank').assign(source='store')


def draw(args):
    pool = pd.read_csv(args.pool, dtype={'pano_id': str, 'user_id': str})
    pose = pd.read_csv(args.pose, dtype={'pano_id': str}).drop_duplicates(['city', 'pano_id'], keep='last')
    cands = ta.candidates(pool, pose, args.min_abs_t)
    exclude = set()
    for d in args.exclude or ():
        exclude |= set(pd.read_csv(os.path.join(d, ta.SEALED, 'selection.csv'), dtype={'pano_id': str})['pano_id'])
    sel = stratified_draw(cands, args.seed, exclude_panos=exclude, cell_cap=args.cell_cap)
    sealed = os.path.join(args.out, ta.SEALED)
    os.makedirs(sealed, exist_ok=True)
    sel.to_csv(os.path.join(sealed, 'selection.csv'), index=False, lineterminator='\n')
    ta.crop_jobs(sel, args.seed, ta.DESIGNS[args.design]).to_csv(os.path.join(sealed, 'crop_jobs.csv'), index=False, lineterminator='\n')
    left = cands[~cands['pano_id'].isin(exclude)]
    spread = ({'cell_cap_per_city_per_arm_type': args.cell_cap, 'fill_pass': True} if args.cell_cap
              else {'city_cap': CITY_CAP})
    info = {'seed': args.seed, 'min_abs_t_deg': args.min_abs_t, 'design': args.design,
            'offsets_T': ta.DESIGNS[args.design], 'per_type': PER_TYPE, 'spread_rule': spread,
            'pool_labels': int(len(pool)), 'scanned_panos': int(len(pose)),
            'excluded_batches': [os.path.basename(os.path.normpath(d)) for d in args.exclude or ()],
            'eligible_by_arm_type': {'%s|%s' % k: int(v) for k, v in
                                     left.groupby(['era_arm', 'label_type'])['pano_id'].nunique().items()},
            'drawn_by_arm_type': {'%s|%s' % k: int(v) for k, v in sel.groupby(['era_arm', 'label_type']).size().items()},
            'drawn_by_city': {k: int(v) for k, v in sel['city'].value_counts().items()},
            'lead_made_drawn': int(sel['lead_made'].sum())}
    with open(os.path.join(args.out, 'draw.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(info, f, indent=1, sort_keys=True)
    print('drew %d labels over %d cities' % (len(sel), sel['city'].nunique()))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('pool')
    p.add_argument('--rawlabels-dir', required=True)
    p.add_argument('--out', required=True)
    i = sub.add_parser('ids')
    i.add_argument('--pool', required=True)
    i.add_argument('--out', required=True)
    d = sub.add_parser('draw')
    d.add_argument('--pool', required=True)
    d.add_argument('--pose', required=True)
    d.add_argument('--seed', required=True)
    d.add_argument('--min-abs-t', type=float, default=ta.MIN_ABS_T_DEG)
    d.add_argument('--exclude', action='append', help='an earlier batch folder whose panos are not redrawn')
    d.add_argument('--cell-cap', type=int, help='per city per (arm, type), then a fill pass; else a flat city cap')
    d.add_argument('--design', choices=sorted(ta.DESIGNS), default='c',
                   help="the windows' offsets in T: 'c' stored/leak/antileak, 'beta' 0.5/1.0/1.5 T (#191)")
    d.add_argument('--out', required=True)
    args = ap.parse_args(argv)
    if args.cmd == 'pool':
        pool = build_pool(args.rawlabels_dir)
        pool.to_csv(args.out, index=False, lineterminator='\n')
        print('%d labels on %d panos' % (len(pool), pool[['city', 'pano_id']].drop_duplicates().shape[0]))
    elif args.cmd == 'ids':
        pool = pd.read_csv(args.pool, dtype={'pano_id': str}, usecols=['city', 'pano_id'])
        pool.drop_duplicates().to_csv(args.out, index=False, lineterminator='\n')
    else:
        draw(args)


if __name__ == '__main__':
    main()
