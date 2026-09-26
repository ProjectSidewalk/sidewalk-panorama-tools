"""Endpoint C of the #54 tilt study: is the labelled feature at the stored pano_y, or shifted by the
rig tilt? A blind three-way forced choice.

For each drawn label, the production v2 crop window (CropRunner.crop_window_width +
compute_crop_box + extract_crop, the exact cut) is taken three times, centred at

    stored:    pano_y
    leak:      pano_y - T(b) * h / 180    (tilt_geometry.rig_pixel_from_gravity_pixel, first order)
    antileak:  pano_y + T(b) * h / 180

with T(b) = pitch cos b + roll sin b at the label's bearing b = (pano_x / w) * 360 - 180. All three
are the same size (the width is computed once, at the stored y), so the size cannot say which is
which; each carries a small ring where the label would sit if that window were right. The three are
pasted side by side in a random order per label, labelled A/B/C, with the label type and tags below.
The judge answers which ring sits on the labelled feature - or `none`.

"leak" is where the #4784 leak puts the feature, in the sign endpoint F1 measured; "antileak" is
the same shift the other way, so a sign error anywhere upstream cannot turn a real effect into a
null - it would show as "antileak".

The blind is structural, as in annotate_server.py: sheets are named by an opaque token, `tasks.json`
(what a judge may read) carries only the token, the label type and the tags, and the order lives in
`key.json`, which neither `--next` nor `--record` ever reads.

    python tilt_adjudicate.py select --corpus reports/data/2026-08-12-crop-corpus-gsv.csv.gz \\
        --pose .cache/tilt/pose_corpus.csv [--fill-rawlabels seattle-wa.csv --fill-city seattle-wa \\
        --fill-pose .cache/tilt/pose_seattle-wa.p0.csv ...] --out .cache/tilt/adjudication
    python tilt_remote_crop.py --jobs crop_jobs.csv --store <root> --out panels   # on the store host
    python tilt_adjudicate.py sheets --out .cache/tilt/adjudication --pano-root <cache>/panos \\
        --panel-dir .cache/tilt/adjudication/panels
    python tilt_adjudicate.py next   --out .cache/tilt/adjudication --judge jon
    python tilt_adjudicate.py record --out .cache/tilt/adjudication --judge jon <token> A|B|C|none
    python tilt_adjudicate.py score  --out .cache/tilt/adjudication --judge jon
"""

import argparse
import hashlib
import json
import math
import os
import sys

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
for p in (HERE, REPO_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import CropRunner  # noqa: E402
import rawlabels  # noqa: E402
import tilt_geometry as tg  # noqa: E402

STUDY_TYPES = ('CurbRamp', 'NoCurbRamp', 'Obstacle', 'SurfaceProblem')
MIN_ABS_T_DEG = 4.0
N_PER_ARM = 24
SEED = '20260926'
PANEL_W, PANEL_H = 480, 320
CHOICES = ('A', 'B', 'C', 'none')
WINDOW_NAMES = ('stored', 'leak', 'antileak')


def era_arm(era):
    return 'post179' if era == 'post179' else 'legacy+mid'


def attach_pose(pose):
    """Per pano: the scrape era of the JPEG and the pose that matches it.

    A pano with an .xml beside it is a 2019-22 stitch (the XML endpoint died in 2022), so its pixels
    are the ones that xml describes - even when a 2025-26 .depth.npz also exists, which may describe a
    since-re-rendered pano. Everything else is a modern stitch, posed by its npz if it has one."""
    p = pose.copy()
    xml = p['xml_present'].astype(int) == 1
    p['scrape_era'] = np.where(xml, 'xml', 'modern')
    xp, xr = tg.xml_tilt_to_pitch_roll(p['xml_pano_yaw_deg'].astype(float), p['xml_tilt_yaw_deg'].astype(float),
                                      p['xml_tilt_pitch_deg'].astype(float))
    npz_ok = (p['npz_present'].astype(int) == 1) & np.isfinite(p['pitch_deg'].astype(float)) \
        & np.isfinite(p['roll_deg'].astype(float))
    xml_ok = xml & np.isfinite(xp) & np.isfinite(xr)
    p['pose_source'] = np.where(xml, np.where(xml_ok, 'xml', 'none'), np.where(npz_ok, 'npz', 'none'))
    p['pose_pitch_deg'] = np.where(xml, xp, p['pitch_deg'].astype(float))
    p['pose_roll_deg'] = np.where(xml, xr, p['roll_deg'].astype(float))
    return p


def candidates(corpus, pose, min_abs_T=MIN_ABS_T_DEG):
    """Eligible labels: live measurable rule (never the CSV's stale column), one of the four study
    types, a pose matching the JPEG's scrape era, stored dims == JPEG dims, and |T(b)| >= min_abs_T."""
    df = corpus[rawlabels.study_measurable(corpus) & corpus['label_type'].isin(STUDY_TYPES)].copy()
    df = df.drop(columns=[c for c in ('pitch_deg', 'roll_deg') if c in df.columns])
    p = attach_pose(pose)[['city', 'pano_id', 'scrape_era', 'pose_source', 'pose_pitch_deg', 'pose_roll_deg',
                           'jpg_width', 'jpg_height']]
    df = df.merge(p, on=['city', 'pano_id'], how='inner', validate='many_to_one')
    df = df[df['pose_source'] != 'none']
    df = df[(df['jpg_width'].astype(float) == df['pano_width'].astype(float))
            & (df['jpg_height'].astype(float) == df['pano_height'].astype(float))]
    df = df.rename(columns={'pose_pitch_deg': 'pitch_deg', 'pose_roll_deg': 'roll_deg'})
    df['dbear'] = df['pano_x'] / df['pano_width'] * 360.0 - 180.0
    df['T_deg'] = tg.tilt_term_deg(df['dbear'].to_numpy(float), df['pitch_deg'].to_numpy(float),
                                   df['roll_deg'].to_numpy(float))
    df['era_arm'] = [era_arm(e) for e in df['era']]
    return df[np.abs(df['T_deg']) >= min_abs_T].reset_index(drop=True)


def _rank(seed, uid):
    return hashlib.md5(('%s:%s' % (seed, uid)).encode()).hexdigest()


def draw(cands, n_per_arm, seed):
    """Up to n_per_arm per era arm, ordered by md5(seed:label_uid). -> (selection, shortfall by arm)."""
    parts, short = [], {}
    for arm in ('legacy+mid', 'post179'):
        c = cands[cands['era_arm'] == arm].copy()
        c['_rank'] = [_rank(seed, u) for u in c['label_uid']]
        take = c.sort_values('_rank').head(n_per_arm).drop(columns='_rank')
        parts.append(take)
        short[arm] = max(0, n_per_arm - len(take))
    return pd.concat(parts, ignore_index=True), short


def draw_with_fill(primary, fill, n_per_arm, seed):
    """draw() from `primary` (the corpus), then top each arm up to n_per_arm from `fill` (the store-side
    Seattle sample), same ordering rule, never repeating a label. -> (selection with `source`, info)."""
    sel, short = draw(primary, n_per_arm, seed)
    sel = sel.assign(source='corpus')
    rest = fill[~fill['label_uid'].isin(sel['label_uid'])]
    parts, taken = [sel], {}
    for arm, k in short.items():
        c = rest[rest['era_arm'] == arm].copy()
        c['_rank'] = [_rank(seed, u) for u in c['label_uid']]
        top = c.sort_values('_rank').head(k).drop(columns='_rank').assign(source='store')
        parts.append(top)
        taken[arm] = int(len(top))
    return pd.concat(parts, ignore_index=True), {'shortfall_in_primary': short, 'from_fill': taken}


def window_centres(pano_y, T_deg, pano_height):
    shift = T_deg * pano_height / 180.0
    return {'stored': pano_y, 'leak': pano_y - shift, 'antileak': pano_y + shift}


def window_boxes(pano_x, pano_y, T_deg, w, h):
    """-> {name: (CropBox, (marker_x, marker_y) in crop pixels)}: the production cut, no pixels needed.
    One width for all three (computed at the stored y), so the window size cannot say which is which."""
    width = CropRunner.crop_window_width(pano_y, w, h)
    out = {}
    for name, y in window_centres(pano_y, T_deg, h).items():
        box = CropRunner.compute_crop_box(pano_x, y, width, w, h)
        out[name] = (box, CropRunner.label_position_in_crop(pano_x, y, box, w))
    return out


def cut_windows(pano, pano_x, pano_y, T_deg):
    """-> {name: (CropBox, crop image, (marker_x, marker_y) in crop pixels)} for the three hypotheses."""
    w, h = pano.size
    out = {}
    for name, (box, marker) in window_boxes(pano_x, pano_y, T_deg, w, h).items():
        out[name] = (box, CropRunner.extract_crop(pano, box.left, box.top, box.width, box.height), marker)
    return out


def crop_jobs(selection, seed=SEED):
    """One row per (label, window): the boxes tilt_remote_crop.py cuts on the store host."""
    rows = []
    for row in selection.to_dict('records'):
        boxes = window_boxes(float(row['pano_x']), float(row['pano_y']), float(row['T_deg']),
                             int(row['pano_width']), int(row['pano_height']))
        for name, (box, _) in boxes.items():
            rows.append({'token': _token(seed, row['label_uid']), 'window': name, 'city': row['city'],
                         'pano_id': row['pano_id'], 'left': box.left, 'top': box.top, 'width': box.width,
                         'height': box.height})
    return pd.DataFrame(rows)


def panel_image(crop):
    """The one downscale every panel goes through, here or on the store host (tilt_remote_crop)."""
    return crop.convert('RGB').resize((PANEL_W, PANEL_H), Image.LANCZOS)


def render_sheet(panels, caption):
    """panels: [(panel image PANEL_W x PANEL_H, (crop_w, crop_h), (mx, my) in crop pixels)], A/B/C order."""
    gap, top, bottom = 12, 28, 40
    sheet = Image.new('RGB', (3 * PANEL_W + 4 * gap, PANEL_H + top + bottom), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    for i, (im, crop_size, (mx, my)) in enumerate(panels):
        sx, sy = PANEL_W / crop_size[0], PANEL_H / crop_size[1]
        x0 = gap + i * (PANEL_W + gap)
        sheet.paste(im, (x0, top))
        cx, cy = x0 + mx * sx, top + my * sy
        for rad, col in ((10, (0, 0, 0)), (9, (255, 230, 0))):
            d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), outline=col, width=2)
        d.text((x0 + PANEL_W // 2 - 4, 6), 'ABC'[i], fill=(255, 255, 255))
    d.text((gap, PANEL_H + top + 12), caption, fill=(220, 220, 220))
    return sheet


def _token(seed, uid):
    return 't' + hashlib.sha256(('sheet:%s:%s' % (seed, uid)).encode()).hexdigest()[:10]


def build_sheets(selection, pano_root, out_dir, seed=SEED, panel_dir=None):
    """Write sheets/<token>.jpg, tasks.json (judge-facing) and key.json (never shown).

    A row whose `source` is 'store' takes its three panels from `panel_dir` (cut on the store host by
    tilt_remote_crop.py from crop_jobs' boxes); every other row is cut here from `pano_root`."""
    os.makedirs(os.path.join(out_dir, 'sheets'), exist_ok=True)
    rng = np.random.default_rng(int(hashlib.md5(('order:%s' % seed).encode()).hexdigest()[:8], 16))
    tasks, key = {}, {}
    CropRunner.raise_decompression_bomb_ceiling()
    for row in selection.sort_values('label_uid').to_dict('records'):
        token = _token(seed, row['label_uid'])
        if row.get('source') == 'store':
            boxes = window_boxes(float(row['pano_x']), float(row['pano_y']), float(row['T_deg']),
                                 int(row['pano_width']), int(row['pano_height']))
            wins = {}
            for n, (box, marker) in boxes.items():
                with Image.open(os.path.join(panel_dir, '%s_%s.png' % (token, n))) as im:
                    wins[n] = (box, im.convert('RGB'), marker)
        else:
            path = os.path.join(pano_root, row['city'], row['pano_id'][:2], row['pano_id'] + '.jpg')
            with Image.open(path) as pano:
                pano.load()
                wins = {n: (box, panel_image(crop), marker) for n, (box, crop, marker) in
                        cut_windows(pano, float(row['pano_x']), float(row['pano_y']), float(row['T_deg'])).items()}
        order = [WINDOW_NAMES[i] for i in rng.permutation(3)]
        tags = row.get('tags') if isinstance(row.get('tags'), str) else '[]'
        caption = '%s   tags: %s   -- which ring sits on the labelled feature? A / B / C / none' % (
            row['label_type'], tags)
        render_sheet([(wins[n][1], (wins[n][0].width, wins[n][0].height), wins[n][2]) for n in order],
                     caption).save(
            os.path.join(out_dir, 'sheets', token + '.jpg'), quality=90)
        tasks[token] = {'label_type': row['label_type'], 'tags': tags}
        key[token] = {'order': order, 'label_uid': row['label_uid'], 'pano_id': row['pano_id'],
                      'pano_x': float(row['pano_x']), 'pano_y': float(row['pano_y']),
                      'pano_width': float(row['pano_width']), 'pano_height': float(row['pano_height']),
                      'dbear': float(row['dbear']), 'T_deg': float(row['T_deg']),
                      'pitch_deg': float(row['pitch_deg']), 'roll_deg': float(row['roll_deg']),
                      'era': row['era'], 'era_arm': row['era_arm'], 'pose_source': row['pose_source'],
                      'scrape_era': row['scrape_era'], 'label_type': row['label_type'],
                      'source': row.get('source', 'corpus'),
                      'shifted_any': bool(any(wins[n][0].shifted for n in WINDOW_NAMES))}
    for name, obj in (('tasks.json', tasks), ('key.json', key)):
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8', newline='\n') as f:
            json.dump(obj, f, indent=1, sort_keys=True, allow_nan=False)
    return tasks


def _verdict_path(out_dir, judge):
    return os.path.join(out_dir, 'verdicts_%s.jsonl' % judge)


def load_verdicts(out_dir, judge):
    """{token: choice}; a later record for a token supersedes an earlier one."""
    path = _verdict_path(out_dir, judge)
    out = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    out[r['token']] = r['choice']
    return out


def _tasks(out_dir):
    with open(os.path.join(out_dir, 'tasks.json'), encoding='utf-8') as f:
        return json.load(f)


def next_unjudged(out_dir, judge):
    done = load_verdicts(out_dir, judge)
    for token in sorted(_tasks(out_dir)):
        if token not in done:
            return token
    return None


def record(out_dir, token, choice, judge):
    if choice not in CHOICES:
        raise ValueError('choice must be one of %s' % (CHOICES,))
    if token not in _tasks(out_dir):
        raise ValueError('unknown token %s' % token)
    with open(_verdict_path(out_dir, judge), 'a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps({'token': token, 'choice': choice, 'judge': judge}) + '\n')


def binom_sf(k, n, p):
    """P(X >= k) for X ~ Binomial(n, p): the one-sided exact test against chance."""
    return float(sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def score(verdicts, key):
    """Counts of stored / plus / minus / none per era arm; `none` stays in the denominator."""
    arms = {}
    for token, choice in verdicts.items():
        k = key[token]
        a = arms.setdefault(k['era_arm'], dict({w: 0 for w in WINDOW_NAMES}, n=0, none=0, none_tokens=[]))
        a['n'] += 1
        if choice == 'none':
            a['none'] += 1
            a['none_tokens'].append(token)
        else:
            a[k['order']['ABC'.index(choice)]] += 1
    for a in arms.values():
        for name in WINDOW_NAMES:
            a['share_' + name] = a[name] / a['n'] if a['n'] else None
            a['p_%s_vs_third' % name] = binom_sf(a[name], a['n'], 1 / 3.0)
        a['none_tokens'].sort()
    return {'arms': arms, 'n': sum(a['n'] for a in arms.values())}


def _read_pose(paths):
    pose = pd.concat([pd.read_csv(p, dtype={'pano_id': str}) for p in paths], ignore_index=True)
    return pose.drop_duplicates(['city', 'pano_id'], keep='last')


def select(args):
    """Stage 1: the draw. Writes selection.csv, draw.json, and crop_jobs.csv for the store-side rows."""
    corpus = pd.read_csv(args.corpus, dtype={'pano_id': str})
    cands = candidates(corpus, _read_pose(args.pose), args.min_abs_t)
    fill = cands.iloc[0:0]
    if args.fill_rawlabels:
        raw = rawlabels.load_rawlabels(args.fill_rawlabels)
        raw['city'] = args.fill_city
        raw['label_uid'] = raw['city'] + ':' + raw['label_id'].astype(int).astype(str)
        assert raw['label_uid'].is_unique
        fill = candidates(raw, _read_pose(args.fill_pose), args.min_abs_t)
    sel, info = draw_with_fill(cands, fill, args.n_per_arm, args.seed)
    os.makedirs(args.out, exist_ok=True)
    sel.to_csv(os.path.join(args.out, 'selection.csv'), index=False, lineterminator='\n')
    crop_jobs(sel[sel['source'] == 'store'], args.seed).to_csv(os.path.join(args.out, 'crop_jobs.csv'),
                                                               index=False, lineterminator='\n')
    draw_info = {'eligible_by_arm': {k: int(v) for k, v in cands['era_arm'].value_counts().items()},
                 'fill_eligible_by_arm': {k: int(v) for k, v in fill['era_arm'].value_counts().items()},
                 'drawn_by_arm': {k: int(v) for k, v in sel['era_arm'].value_counts().items()},
                 'drawn_by_source': {k: int(v) for k, v in sel['source'].value_counts().items()},
                 'min_abs_T_deg': args.min_abs_t, 'seed': args.seed, 'n_per_arm': args.n_per_arm,
                 'fill_city': args.fill_city if args.fill_rawlabels else None}
    draw_info.update(info)
    with open(os.path.join(args.out, 'draw.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(draw_info, f, indent=1, sort_keys=True)
    print(json.dumps(draw_info, sort_keys=True))


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('select')
    b.add_argument('--corpus', required=True)
    b.add_argument('--pose', required=True, action='append')
    b.add_argument('--fill-rawlabels')
    b.add_argument('--fill-city')
    b.add_argument('--fill-pose', action='append')
    b.add_argument('--out', required=True)
    b.add_argument('--seed', default=SEED)
    b.add_argument('--min-abs-t', type=float, default=MIN_ABS_T_DEG)
    b.add_argument('--n-per-arm', type=int, default=N_PER_ARM)
    sh = sub.add_parser('sheets')
    sh.add_argument('--out', required=True)
    sh.add_argument('--pano-root', required=True)
    sh.add_argument('--panel-dir')
    sh.add_argument('--seed', default=SEED)
    for name in ('next', 'score'):
        s = sub.add_parser(name)
        s.add_argument('--out', required=True)
        s.add_argument('--judge', required=True)
    r = sub.add_parser('record')
    r.add_argument('--out', required=True)
    r.add_argument('--judge', required=True)
    r.add_argument('token')
    r.add_argument('choice', choices=CHOICES)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == 'select':
        select(args)
    elif args.cmd == 'sheets':
        sel = pd.read_csv(os.path.join(args.out, 'selection.csv'), dtype={'pano_id': str})
        build_sheets(sel, args.pano_root, args.out, args.seed, panel_dir=args.panel_dir)
        print('wrote %d sheets' % len(sel))
    elif args.cmd == 'next':
        token = next_unjudged(args.out, args.judge)
        print('all judged' if token is None else os.path.join(args.out, 'sheets', token + '.jpg'))
    elif args.cmd == 'record':
        record(args.out, args.token, args.choice, args.judge)
    elif args.cmd == 'score':
        with open(os.path.join(args.out, 'key.json'), encoding='utf-8') as f:
            key = json.load(f)
        print(json.dumps(score(load_verdicts(args.out, args.judge), key), indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
