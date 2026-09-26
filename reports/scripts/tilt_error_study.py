"""The #54 tilt study: which frame are the depth planes and the stored tiles in, is the labelled
feature at the stored pano_y, and what would a tilt leak cost a crop.

Two subcommands, so the analysis runs from committed data alone:

    # 1. export: gather the raw measurements from the gitignored cache into reports/data/
    python reports/scripts/tilt_error_study.py export \\
        --pose .cache/tilt/pose_seattle-wa.p0.csv ... --corpus-pose .cache/tilt/pose_corpus.csv \\
        --facades .cache/tilt/facades_seattle-wa.p0.csv ... --corpus-facades .cache/tilt/facades_corpus.csv \\
        --lean .cache/tilt/lean_corpus.csv --lean .cache/tilt/lean_store.csv \\
        --adjudication .cache/tilt/adjudication
    # 2. analyze: committed data -> reports/data/2026-09-26-tilt-error-study.json + figures
    python reports/scripts/tilt_error_study.py analyze [--figure-dir reports/figures]

Endpoints (reports/2026-09-26-tilt-error-study.md has the design and the verdicts):

* F1 - facade-plane normals from the .depth.npz artifacts: el_f = b_p(pitch cos b_f) + b_r(roll sin b_f).
* F2 - vertical-edge lean of the stored JPEG (tilt_frame.py), pano fixed effects, calibrated.
* C  - the blind forced choice (tilt_adjudicate.py), per era arm and per judge.
* S1 - the tilt prior by scrape era; S2 - the xml<->npz convention; S3 - the mis-centering ceiling;
  S4 - the RampNet extent-gold overlap count.

Conventions (CLAUDE.md "Desk studies"): studyfmt.fmt/num/percentile only; label_uid = city:label_id;
pano_id read as str; every merge validates; every figure in the artifact is claimed by exactly one
entry of `populations`; json written with allow_nan=False.
"""

import argparse
import glob
import gzip
import hashlib
import json
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
for p in (HERE, REPO_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import CropRunner  # noqa: E402
import pov_replay  # noqa: E402
import tilt_adjudicate  # noqa: E402
import tilt_geometry as tg  # noqa: E402
from studyfmt import fmt, num  # noqa: E402

DATA = os.path.join(REPO_ROOT, 'reports', 'data')
FIGURES = os.path.join(REPO_ROOT, 'reports', 'figures')
PREFIX = '2026-09-26-tilt-'
CORPUS_CSV = os.path.join(DATA, '2026-08-12-crop-corpus-gsv.csv.gz')
ARTIFACT = os.path.join(DATA, PREFIX + 'error-study.json')

BANDS = ('<5', '5-15', '15-30', '>30')
F1_RIG, F1_GRAVITY = (0.9, 1.1), (-0.1, 0.1)
F2_RIG, F2_GRAVITY = (0.7, 1.3), (-0.3, 0.3)
C_DECISIVE_SHARE = 0.9
RAMPNET_SIGMA_PX = 12.0      # RampNet's stage-one click sigma on a 4096-high pano (issue #54 comment)
RAMPNET_HEIGHT_PX = 4096.0
Z95 = 1.959963984540054


# ---- small helpers ----------------------------------------------------------------------------

def label_uid(df):
    """city:label_id - label_id restarts at 1 in every deployment, so it is not an identity alone."""
    uid = df['city'].astype(str) + ':' + df['label_id'].astype(int).astype(str)
    assert uid.is_unique, 'label_uid is not unique'
    return uid


def read_csv(path, **kw):
    kw.setdefault('dtype', {})
    kw['dtype'] = dict(kw['dtype'], pano_id=str)
    return pd.read_csv(path, **kw)


def write_json(obj, path):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(obj, f, allow_nan=False, indent=1, sort_keys=True)
        f.write('\n')


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def band_of(depression_deg):
    d = np.asarray(depression_deg, dtype=float)
    return np.where(d < 5, '<5', np.where(d < 15, '5-15', np.where(d < 30, '15-30', '>30')))


def _ci(ci):
    return [num(ci[0]), num(ci[1])]


# ---- the regression ---------------------------------------------------------------------------

def fit_two_coefficient(y, x_p, x_r, cluster):
    """OLS y = b_p x_p + b_r x_r (no intercept: every quantity here flips sign with the plane
    normal's arbitrary orientation, so an intercept would not be invariant), CR1 cluster-robust
    standard errors by `cluster`, normal-approximation 95% CIs (clusters number in the hundreds)."""
    y = np.asarray(y, float)
    X = np.column_stack([np.asarray(x_p, float), np.asarray(x_r, float)])
    cl = np.asarray(cluster)
    ok = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    y, X, cl = y[ok], X[ok], cl[ok]
    n, k = X.shape
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ X.T @ y
    u = y - X @ beta
    groups, inv = np.unique(cl, return_inverse=True)
    g = len(groups)
    scores = np.zeros((g, k))
    np.add.at(scores, inv, X * u[:, None])
    meat = scores.T @ scores
    corr = (g / (g - 1.0)) * ((n - 1.0) / (n - k)) if g > 1 and n > k else float('nan')
    V = corr * xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.diag(V))
    return {'beta_p': num(beta[0]), 'beta_r': num(beta[1]), 'se_p': num(se[0]), 'se_r': num(se[1]),
            'ci_p': _ci((beta[0] - Z95 * se[0], beta[0] + Z95 * se[0])),
            'ci_r': _ci((beta[1] - Z95 * se[1], beta[1] + Z95 * se[1])),
            'n': int(n), 'n_clusters': int(g), 'resid_sd': num(np.std(u, ddof=k)) if n > k else None}


def frame_verdict(ci_p, ci_r, rig, gravity):
    """'rig' when both CIs sit inside `rig`, 'gravity' inside `gravity`, else 'undecided'."""
    def inside(ci, lo_hi):
        return ci[0] is not None and ci[1] is not None and lo_hi[0] <= ci[0] and ci[1] <= lo_hi[1]
    if inside(ci_p, rig) and inside(ci_r, rig):
        return 'rig'
    if inside(ci_p, gravity) and inside(ci_r, gravity):
        return 'gravity'
    return 'undecided'


# ---- F1: facade planes ------------------------------------------------------------------------

def facade_table(facades, pose):
    """Per facade: bearing/elevation of its normal (artifact frame -> RFU) and the artifact's OWN
    pose (npz pitch/roll - never an xml pose, which may describe a different rendering)."""
    p = pose[['city', 'pano_id', 'pitch_deg', 'roll_deg']]
    f = facades.merge(p, on=['city', 'pano_id'], how='inner', validate='many_to_one')
    b, el = tg.artifact_normal_bearing_elevation(f[['n_x', 'n_y', 'n_z']].to_numpy(float))
    f = f.assign(bearing_deg=b, elevation_deg=el)
    f['x_p'] = f['pitch_deg'] * np.cos(np.radians(f['bearing_deg']))
    f['x_r'] = f['roll_deg'] * np.sin(np.radians(f['bearing_deg']))
    return f[np.isfinite(f['pitch_deg']) & np.isfinite(f['roll_deg'])]


def facade_frame_fit(facades, pose):
    f = facade_table(facades, pose)
    fit = fit_two_coefficient(f['elevation_deg'], f['x_p'], f['x_r'], f['city'] + '/' + f['pano_id'])
    resid = f['elevation_deg'] - (fit['beta_p'] * f['x_p'] + fit['beta_r'] * f['x_r'])
    fit['median_abs_resid_deg'] = num(np.median(np.abs(resid))) if len(f) else None
    fit['median_abs_elevation_deg'] = num(np.median(np.abs(f['elevation_deg']))) if len(f) else None
    return fit


# ---- F2: tile lean ----------------------------------------------------------------------------

def _demean(values, groups):
    s = pd.Series(np.asarray(values, float))
    return (s - s.groupby(np.asarray(groups)).transform('mean')).to_numpy()


def calibration_slope(lean, variant, extra):
    """Attenuation: slope of (lean after a known added tilt - lean before) on the predicted change."""
    base = lean[lean['variant'] == 'base'][['city', 'pano_id', 'bin', 'bin_centre_deg', 'lean_deg']]
    cal = lean[lean['variant'] == variant][['city', 'pano_id', 'bin', 'lean_deg']]
    m = base.merge(cal, on=['city', 'pano_id', 'bin'], suffixes=('_b', '_c'), validate='one_to_one')
    m = m[np.isfinite(m['lean_deg_b']) & np.isfinite(m['lean_deg_c'])]
    if m.empty:
        return None, 0
    pred = tg.vertical_lean_deg(m['bin_centre_deg'].to_numpy(float), extra[0], extra[1])
    d = (m['lean_deg_c'] - m['lean_deg_b']).to_numpy(float)
    return num(np.dot(d, pred) / np.dot(pred, pred)), int(m['pano_id'].nunique())


SATURATED_A = 0.1


def tile_frame_fit(lean, pose):
    """F2 on one arm: pano fixed effects (demean within pano), the two-coefficient fit, and the
    calibrated coefficients b / a with a from the +2 deg pitch (and roll) warps - on this arm's own
    panos only.

    Two flags, both read by the report. `gravity_band_excluded`: both calibrated CIs sit above the
    gravity band's upper edge, i.e. the tiles are not gravity-levelled. `saturated`: the pitch
    calibration slope is below SATURATED_A, meaning an added 2 deg barely moves the measurement - the
    estimator is outside its linear range (leans beyond its +-12 deg window), and no calibrated value
    from that arm means anything. The calibrated values themselves are lower bounds: the warp rotates
    world-leaning clutter along with world-vertical edges (tests/test_tilt_frame.py pins this)."""
    keep = pose[['city', 'pano_id']].drop_duplicates()
    lean = lean.merge(keep, on=['city', 'pano_id'], how='inner', validate='many_to_one')
    base = lean[lean['variant'] == 'base'].drop(columns=[c for c in ('pitch_deg', 'roll_deg') if c in lean.columns])
    m = base.merge(pose[['city', 'pano_id', 'pitch_deg', 'roll_deg']], on=['city', 'pano_id'], how='inner',
                   validate='many_to_one')
    m = m[np.isfinite(m['lean_deg'].astype(float))]
    c = np.radians(m['bin_centre_deg'].to_numpy(float))
    key = m['city'].astype(str) + '/' + m['pano_id']
    y = _demean(m['lean_deg'], key)
    xp = _demean(-m['pitch_deg'].to_numpy(float) * np.sin(c), key)
    xr = _demean(m['roll_deg'].to_numpy(float) * np.cos(c), key)
    raw = fit_two_coefficient(y, xp, xr, key)
    a_p, n_ap = calibration_slope(lean, 'cal_pitch', (2.0, 0.0))
    a_r, n_ar = calibration_slope(lean, 'cal_roll', (0.0, 2.0))
    out = {'raw': raw, 'n_panos': int(key.nunique()), 'a_pitch': a_p, 'a_pitch_n_panos': n_ap,
           'a_roll': a_r, 'a_roll_n_panos': n_ar}
    for axis, a in (('p', a_p), ('r', a_r)):
        ok = a is not None and raw['beta_' + axis] is not None
        out['beta_%s_calibrated' % axis] = num(raw['beta_' + axis] / a) if ok else None
        out['ci_%s_calibrated' % axis] = [num(v / a) for v in raw['ci_' + axis]] if ok else [None, None]
    lows = [out['ci_p_calibrated'][0], out['ci_r_calibrated'][0]]
    out['gravity_band_excluded'] = bool(all(v is not None and v > F2_GRAVITY[1] for v in lows))
    out['saturated'] = bool(a_p is None or a_p < SATURATED_A)
    return out


# ---- C ---------------------------------------------------------------------------------------

def decisive_share(arm, name):
    """Share of the non-`none` verdicts that chose `name`; None when every verdict was `none`."""
    decisive = arm['n'] - arm['none']
    return arm[name] / decisive if decisive else None


def c_by_direction(verdicts, key):
    """Verdicts split by whether the leak window sits above (T > 0: y - T h/180 is higher in the
    frame) or below the stored one."""
    out = {d: {'n': 0, 'stored': 0, 'leak': 0, 'antileak': 0, 'none': 0} for d in ('leak_above', 'leak_below')}
    for token, choice in verdicts.items():
        k = key[token]
        d = out['leak_above' if k['T_deg'] > 0 else 'leak_below']
        d['n'] += 1
        d['none' if choice == 'none' else k['order']['ABC'.index(choice)]] += 1
    return out


def c_verdict(arm):
    if not arm['n']:
        return 'no data'
    for name in tilt_adjudicate.WINDOW_NAMES:
        if arm[name] / arm['n'] >= C_DECISIVE_SHARE:
            return name
    return 'split'


# ---- S1, S2, S3 ------------------------------------------------------------------------------

def tilt_prior(pose_att, dbear):
    """|pitch|, |roll| quantiles per scrape era, and |T| at the corpus's empirical bearing mix."""
    out = {}
    b = np.radians(np.asarray(dbear, float))
    for era, g in pose_att[pose_att['pose_source'] != 'none'].groupby('scrape_era'):
        p = g['pose_pitch_deg'].to_numpy(float)
        r = g['pose_roll_deg'].to_numpy(float)
        Ts = (p[:, None] * np.cos(b)[None, :] + r[:, None] * np.sin(b)[None, :]).ravel()
        T = np.abs(Ts)
        row = {'n_panos': int(len(g)), 'pose_source': sorted(g['pose_source'].unique().tolist())}
        for name, v in (('abs_pitch', np.abs(p)), ('abs_roll', np.abs(r)), ('abs_T', T)):
            for q in (50, 90, 99):
                row['%s_p%d_deg' % (name, q)] = num(np.percentile(v, q))
        row['sd_T_deg'] = num(np.std(Ts))
        row['mean_pitch_deg'] = num(np.mean(p))
        row['mean_roll_deg'] = num(np.mean(r))
        out[era] = row
    return out


XML_ALTERNATIVES = [(sp, sr, swap) for swap in (False, True) for sp in (-1, 1) for sr in (-1, 1)]


def _alt_name(sp, sr, swap):
    f, g = ('sin', 'cos') if swap else ('cos', 'sin')
    return 'pitch=%sm*%s(dir), roll=%sm*%s(dir)' % ('-' if sp < 0 else '', f, '-' if sr < 0 else '', g)


def xml_npz_convention(overlap):
    """Score the eight sign/axis readings of the xml triple against the npz pose on panos with both."""
    m = overlap['xml_tilt_pitch_deg'].to_numpy(float)
    d = np.radians(overlap['xml_tilt_yaw_deg'].to_numpy(float) - overlap['xml_pano_yaw_deg'].to_numpy(float))
    P, R = overlap['pitch_deg'].to_numpy(float), overlap['roll_deg'].to_numpy(float)
    alts = {}
    for sp, sr, swap in XML_ALTERNATIVES:
        cs, sn = (np.sin(d), np.cos(d)) if swap else (np.cos(d), np.sin(d))
        alts[_alt_name(sp, sr, swap)] = (float(np.median(np.abs(sp * m * cs - P))),
                                         float(np.median(np.abs(sr * m * sn - R))))
    best = min(alts, key=lambda k: sum(alts[k]))
    xp, xr = tg.xml_tilt_to_pitch_roll(overlap['xml_pano_yaw_deg'], overlap['xml_tilt_yaw_deg'],
                                       overlap['xml_tilt_pitch_deg'])
    vec = np.hypot(np.asarray(xp) - P, np.asarray(xr) - R)
    return {'n_overlap': int(len(overlap)), 'best': best,
            'alternatives': {k: {'median_abs_dpitch_deg': num(v[0]), 'median_abs_droll_deg': num(v[1])}
                             for k, v in alts.items()},
            'median_abs_dpitch_deg': num(np.median(np.abs(np.asarray(xp) - P))),
            'median_abs_droll_deg': num(np.median(np.abs(np.asarray(xr) - R))),
            'p90_vector_diff_deg': num(np.percentile(vec, 90)),
            'share_vector_diff_over_1deg': num(np.mean(vec > 1.0)),
            'n_vector_diff_over_1deg': int(np.sum(vec > 1.0))}


def ps_camera_pitch_check(corpus, pose_att):
    """PS's stored camera_pitch against the store pose, per pose source; and whether PS stores roll."""
    g = corpus.groupby(['city', 'pano_id'], as_index=False).agg(camera_pitch=('camera_pitch', 'median'))
    m = g.merge(pose_att[['city', 'pano_id', 'pose_source', 'pose_pitch_deg']], on=['city', 'pano_id'],
                how='inner', validate='one_to_one')
    out = {'corpus_labels': int(len(corpus)),
           'corpus_labels_with_camera_roll': int(corpus['camera_roll'].notna().sum())}
    for src in ('npz', 'xml'):
        x = m[(m['pose_source'] == src) & m['camera_pitch'].notna()]
        d = (x['camera_pitch'] - x['pose_pitch_deg']).to_numpy(float)
        out[src] = {'n_panos': int(len(x)), 'median_abs_diff_deg': num(np.median(np.abs(d))) if len(x) else None,
                    'corr': num(np.corrcoef(x['camera_pitch'], x['pose_pitch_deg'])[0, 1]) if len(x) > 2 else None}
    return out


def miscentering_row(band, depression_deg, t_p90_deg, pano_height=8192):
    """The ceiling (b = 1) mis-centering at a band's representative depression, as a fraction of the
    v2 window's HEIGHT (a 3:2 window: height = fov / 1.5) - the vertical extent a shift eats into."""
    y = pano_height / 2 + depression_deg * pano_height / 180.0
    fov = CropRunner.crop_window_fov_deg(y, pano_height)
    height = fov / CropRunner.CROP_ASPECT_W_OVER_H
    return {'band': band, 'depression_deg': num(depression_deg),
            'distance_m': num(float(pov_replay.predict_blend_distance(depression_deg))),
            'window_width_deg': num(fov), 'window_height_deg': num(height), 'T_p90_deg': num(t_p90_deg),
            'ceiling_shift_deg': num(t_p90_deg), 'ceiling_shift_px_8192': num(t_p90_deg * 8192 / 180.0),
            'ceiling_fraction_of_height': num(t_p90_deg / height),
            'rampnet_sigma_multiple': num(t_p90_deg * RAMPNET_HEIGHT_PX / 180.0 / RAMPNET_SIGMA_PX)}


def miscentering_table(corpus, pose_att):
    """Per depression band: |T| p90 over (the band's corpus bearings) x (every posed Seattle pano)."""
    posed = pose_att[pose_att['pose_source'] != 'none']
    p = posed['pose_pitch_deg'].to_numpy(float)
    r = posed['pose_roll_deg'].to_numpy(float)
    rows = []
    bands = band_of(corpus['depression'])
    for band in BANDS:
        sel = corpus[bands == band]
        b = np.radians(sel['dbear'].to_numpy(float))
        T = np.abs(p[:, None] * np.cos(b)[None, :] + r[:, None] * np.sin(b)[None, :]).ravel()
        row = miscentering_row(band, float(np.median(sel['depression'])), float(np.percentile(T, 90)))
        row['n_labels'] = int(len(sel))
        rows.append(row)
    return rows


# ---- S4 --------------------------------------------------------------------------------------

S4_MIN_PAIRS = 30


def extent_gold_pairs(city, boxes, labels):
    """PS CurbRamp labels whose stored point pairs with a RampNet gold box: x inside the box's span,
    y within one box-height of its bottom edge. RampNet's boxes.json stores each detection's box
    normalised to the pano (cx, cy, w, h); only `status == 'boxed'` detections carry one.
    -> [{label_uid, pano_id, det, dy_box_heights}]."""
    out = []
    lab = labels[labels['label_type'] == 'CurbRamp']
    panos = boxes['panos']
    for row in lab.itertuples():
        dets = panos.get(str(row.pano_id))
        if not dets:
            continue
        x = row.pano_x / row.pano_width
        y = row.pano_y / row.pano_height
        for name, d in sorted(dets.items()):
            if d.get('status') != 'boxed':
                continue
            bottom = d['cy'] + d['h'] / 2
            if abs(x - d['cx']) <= d['w'] / 2 and abs(y - bottom) <= d['h']:
                out.append({'label_uid': '%s:%d' % (city, int(row.label_id)), 'pano_id': str(row.pano_id),
                            'det': name, 'dy_box_heights': num((y - bottom) / d['h'])})
                break
    return out


# ---- populations and the report contract ------------------------------------------------------

TOP_LEVEL_META = {'generated_from', 'conventions', 'populations', 'wrong_turns', 'verdicts'}


def check_populations(summary):
    """Every figure-bearing top-level key is claimed by exactly one population."""
    claimed = {}
    for name, pop in summary['populations'].items():
        for k in pop['keys']:
            assert k not in claimed, '%s claimed by both %s and %s' % (k, claimed[k], name)
            claimed[k] = name
    figures = set(summary) - TOP_LEVEL_META
    assert figures == set(claimed), 'unclaimed %s / claimed-but-absent %s' % (
        sorted(figures - set(claimed)), sorted(set(claimed) - figures))


def report_numbers(s):
    """Every number the markdown quotes, formatted exactly as quoted."""
    out = {}
    for arm, v in s['f1_depth_frame'].items():
        f = v['fit']
        out['f1.%s.n' % arm] = '{:,}'.format(f['n'])
        out['f1.%s.panos' % arm] = '{:,}'.format(f['n_clusters'])
        out['f1.%s.bp' % arm] = fmt(f['beta_p'], '.4f')
        out['f1.%s.br' % arm] = fmt(f['beta_r'], '.4f')
        out['f1.%s.cip' % arm] = '[%s, %s]' % (fmt(f['ci_p'][0], '.4f'), fmt(f['ci_p'][1], '.4f'))
        out['f1.%s.cir' % arm] = '[%s, %s]' % (fmt(f['ci_r'][0], '.4f'), fmt(f['ci_r'][1], '.4f'))
        out['f1.%s.resid' % arm] = fmt(f['median_abs_resid_deg'], '.3f')
    for arm, v in s['f2_tile_frame'].items():
        out['f2.%s.panos' % arm] = '{:,}'.format(v['n_panos'])
        out['f2.%s.bp' % arm] = fmt(v['raw']['beta_p'], '.3f')
        out['f2.%s.br' % arm] = fmt(v['raw']['beta_r'], '.3f')
        out['f2.%s.ap' % arm] = fmt(v['a_pitch'], '.3f')
        if v['saturated']:
            continue            # its calibrated values mean nothing; the report does not quote them
        out['f2.%s.bpc' % arm] = fmt(v['beta_p_calibrated'], '.3f')
        out['f2.%s.brc' % arm] = fmt(v['beta_r_calibrated'], '.3f')
        out['f2.%s.cipc' % arm] = '[%s, %s]' % tuple(fmt(x, '.3f') for x in v['ci_p_calibrated'])
        out['f2.%s.circ' % arm] = '[%s, %s]' % tuple(fmt(x, '.3f') for x in v['ci_r_calibrated'])
    s2 = s['s2_xml_npz']
    out['s2.n'] = '{:,}'.format(s2['n_overlap'])
    out['s2.dp'] = fmt(s2['median_abs_dpitch_deg'], '.3f')
    out['s2.dr'] = fmt(s2['median_abs_droll_deg'], '.3f')
    out['s2.share1'] = fmt(100 * s2['share_vector_diff_over_1deg'], '.1f') + '%'
    ps = s['s2_ps_camera_pitch']
    out['ps.npz'] = fmt(ps['npz']['median_abs_diff_deg'], '.3f')
    out['ps.xml'] = fmt(ps['xml']['median_abs_diff_deg'], '.3f')
    out['ps.roll'] = '{:,}'.format(ps['corpus_labels_with_camera_roll'])
    for era, v in s['s1_tilt_prior']['by_scrape_era'].items():
        out['s1.%s.n' % era] = '{:,}'.format(v['n_panos'])
        for k in ('abs_pitch_p50_deg', 'abs_pitch_p90_deg', 'abs_roll_p50_deg', 'abs_roll_p90_deg',
                  'abs_T_p50_deg', 'abs_T_p90_deg', 'abs_T_p99_deg'):
            out['s1.%s.%s' % (era, k)] = fmt(v[k], '.2f')
    for row in s['s3_miscentering']:
        for k, spec in (('T_p90_deg', '.2f'), ('window_height_deg', '.1f'), ('ceiling_shift_px_8192', '.0f'),
                        ('rampnet_sigma_multiple', '.1f')):
            out['s3.%s.%s' % (row['band'], k)] = fmt(row[k], spec)
        out['s3.%s.frac' % row['band']] = fmt(100 * row['ceiling_fraction_of_height'], '.1f') + '%'
    c = s['c_adjudication']
    for arm, n in c['draw']['eligible_by_arm'].items():
        out['c.eligible.%s' % arm] = str(n)
    for arm, n in c['draw']['fill_eligible_by_arm'].items():
        out['c.fill_eligible.%s' % arm] = '{:,}'.format(n)
    for arm, n in c['draw']['from_fill'].items():
        out['c.from_fill.%s' % arm] = str(n)
    for judge, j in c['judges'].items():
        for arm, a in j['arms'].items():
            out['c.%s.%s' % (judge, arm)] = '%d / %d / %d / %d' % (a['stored'], a['leak'], a['antileak'], a['none'])
            out['c.%s.%s.decisive' % (judge, arm)] = fmt(100 * a['decisive_share_leak'], '.0f') + '%'
        for d, v in j['by_direction'].items():
            out['c.%s.%s' % (judge, d)] = '%d of %d' % (v['leak'], v['n'])
    out['s4'] = str(s['s4_extent_gold']['n_pairs'])
    return out


# ---- export and analyze -----------------------------------------------------------------------

def _concat(paths):
    return pd.concat([read_csv(p) for p in paths], ignore_index=True) if paths else pd.DataFrame()


POSE_EXPORT = ['city', 'pano_id', 'scrape_era', 'pose_source', 'pose_pitch_deg', 'pose_roll_deg', 'npz_present',
               'pitch_deg', 'roll_deg', 'heading_deg', 'xml_present', 'xml_pano_yaw_deg', 'xml_tilt_yaw_deg',
               'xml_tilt_pitch_deg', 'xml_image_date', 'jpg_present', 'jpg_width', 'jpg_height', 'jpg_mtime_iso',
               'ground_elev_deg', 'ground_bearing_deg', 'ground_dist_m']


def _to_gz(df, path):
    with gzip.GzipFile(path, 'wb', mtime=0) as gz:
        gz.write(df.to_csv(index=False, lineterminator='\n', float_format='%.6g').encode('utf-8'))


def export(args):
    pose = tilt_adjudicate.attach_pose(_concat(args.pose).drop_duplicates(['city', 'pano_id']))
    cpose = tilt_adjudicate.attach_pose(read_csv(args.corpus_pose))
    _to_gz(pose[POSE_EXPORT].sort_values(['city', 'pano_id']), os.path.join(DATA, PREFIX + 'pose-seattle.csv.gz'))
    _to_gz(cpose[POSE_EXPORT].sort_values(['city', 'pano_id']), os.path.join(DATA, PREFIX + 'pose-corpus.csv.gz'))
    fac = pd.concat([_concat(args.facades).assign(source='seattle'),
                     read_csv(args.corpus_facades).assign(source='corpus')], ignore_index=True)
    _to_gz(fac.sort_values(['source', 'city', 'pano_id', 'plane_index']), os.path.join(DATA, PREFIX + 'facades.csv.gz'))
    lean = _concat(args.lean).drop(columns=['pitch_deg', 'roll_deg', 'predicted_lean_deg'], errors='ignore')
    _to_gz(lean.sort_values(['arm', 'city', 'pano_id', 'variant', 'bin']),
           os.path.join(DATA, PREFIX + 'lean-measurements.csv.gz'))
    both = pose[(pose['xml_present'] == 1) & (pose['npz_present'] == 1)]
    _to_gz(both[['city', 'pano_id', 'pitch_deg', 'roll_deg', 'xml_pano_yaw_deg', 'xml_tilt_yaw_deg',
                 'xml_tilt_pitch_deg', 'xml_image_date', 'jpg_mtime_iso']].sort_values('pano_id'),
           os.path.join(DATA, PREFIX + 'xml-npz-overlap.csv.gz'))
    adj = {'draw': {}, 'key': {}, 'verdicts': {}}
    for d in args.adjudication:
        with open(os.path.join(d, 'key.json'), encoding='utf-8') as f:
            key = json.load(f)
        with open(os.path.join(d, 'draw.json'), encoding='utf-8') as f:
            adj['draw'][os.path.basename(d.rstrip('/\\'))] = json.load(f)
        adj['key'].update(key)
        for vp in sorted(glob.glob(os.path.join(d, 'verdicts_*.jsonl'))):
            judge = os.path.basename(vp)[len('verdicts_'):-len('.jsonl')]
            adj['verdicts'].setdefault(judge, {}).update(tilt_adjudicate.load_verdicts(d, judge))
    write_json(adj, os.path.join(DATA, PREFIX + 'adjudication.json'))
    s4 = {'cities': {}, 'min_pairs_to_analyse': S4_MIN_PAIRS}
    for spec in args.rampnet or []:
        city, boxes_path, labels_path = spec.split('=', 1)[0], *spec.split('=', 1)[1].split(',')
        with open(boxes_path, encoding='utf-8') as f:
            boxes = json.load(f)
        labels = read_csv(labels_path)
        pairs = extent_gold_pairs(city, boxes, labels)
        s4['cities'][city] = {'gold_panos': len(boxes['panos']),
                              'ps_labels_on_gold_panos': int(labels['pano_id'].isin(boxes['panos']).sum()),
                              'n_pairs': len(pairs), 'pairs': pairs}
    s4['n_pairs'] = sum(c['n_pairs'] for c in s4['cities'].values())
    write_json(s4, os.path.join(DATA, PREFIX + 's4-extent-gold.json'))
    print('exported %d pose rows, %d corpus pose rows, %d facades, %d lean rows, %d overlap, %d sheets'
          % (len(pose), len(cpose), len(fac), len(lean), len(both), len(adj['key'])))


def analyze(args):
    corpus = read_csv(CORPUS_CSV)
    corpus['label_uid'] = label_uid(corpus)
    corpus['dbear'] = corpus['pano_x'] / corpus['pano_width'] * 360.0 - 180.0
    files = {k: os.path.join(DATA, PREFIX + k + ext) for k, ext in
             (('pose-seattle', '.csv.gz'), ('pose-corpus', '.csv.gz'), ('facades', '.csv.gz'),
              ('lean-measurements', '.csv.gz'), ('xml-npz-overlap', '.csv.gz'), ('adjudication', '.json'))}
    pose = read_csv(files['pose-seattle'])
    cpose = read_csv(files['pose-corpus'])
    fac = read_csv(files['facades'])
    lean = read_csv(files['lean-measurements'])
    overlap = read_csv(files['xml-npz-overlap'])
    with open(files['adjudication'], encoding='utf-8') as f:
        adj = json.load(f)

    s = {'generated_from': {k: {'file': os.path.basename(v), 'md5': md5(v)} for k, v in files.items()}}
    s['generated_from']['corpus'] = {'file': os.path.basename(CORPUS_CSV), 'md5': md5(CORPUS_CSV)}
    s['conventions'] = {
        'bearing': 'degrees clockwise from the pano forward (heading) direction, (-180, 180]',
        'label_bearing': 'dbear = (pano_x / pano_width) * 360 - 180, from stored pixels alone',
        'artifact_axes': 'x = +left, y = +backward, z = +down (docs/depth.md ray formula)',
        'pose_sign': 'streetlevel: pitch = 90 - raw, roll = raw, degrees, wrapped to (-180, 180]',
        'tilt_term': 'T(b) = pitch cos b + roll sin b; rig elevation of the gravity horizon = +T(b) (measured, F1)',
        'lean': 'vertical-edge lean, CCW-positive as viewed = roll cos b - pitch sin b if rig-aligned',
        'xml_conversion': 'pitch = -m cos(tilt_yaw - pano_yaw), roll = -m sin(...), m = tilt_pitch_deg',
    }

    # F1
    f1 = {}
    for arm, src, pdf in (('seattle', 'seattle', pose), ('corpus', 'corpus', cpose)):
        npz = pdf[pdf['npz_present'] == 1]
        fit = facade_frame_fit(fac[fac['source'] == src], npz)
        f1[arm] = {'fit': fit, 'verdict': frame_verdict(fit['ci_p'], fit['ci_r'], F1_RIG, F1_GRAVITY),
                   'decision_rule': {'rig': list(F1_RIG), 'gravity': list(F1_GRAVITY)}}
    s['f1_depth_frame'] = f1
    rig2 = F2_RIG   # tilt_geometry's sign is F1's measured one, so rig-aligned tiles read +1

    # F2
    f2, panels = {}, []
    posed = {'corpus': cpose, 'store': pose}
    for arm_name, g in lean.groupby('arm'):
        src = 'corpus' if arm_name == 'corpus' else 'store'
        pp = posed[src].rename(columns={'pitch_deg': 'npz_pitch_deg', 'roll_deg': 'npz_roll_deg'})
        pp = pp.rename(columns={'pose_pitch_deg': 'pitch_deg', 'pose_roll_deg': 'roll_deg'})
        for era in ('xml', 'modern'):
            sub_pose = pp[(pp['scrape_era'] == era) & (pp['pose_source'] != 'none')]
            if src == 'store':
                sub_pose = sub_pose[sub_pose['pano_id'].isin(g['pano_id'])]
            fit = tile_frame_fit(g, sub_pose)
            if fit['n_panos'] == 0:
                continue
            name = '%s_%s' % (arm_name, era)
            fit['verdict'] = frame_verdict(fit['ci_p_calibrated'], fit['ci_r_calibrated'], rig2, F2_GRAVITY)
            fit['decision_rule'] = {'rig': list(rig2), 'gravity': list(F2_GRAVITY)}
            f2[name] = fit
            if arm_name != 'store_top':
                panels.append((name.replace('_', ' '),) + lean_panel(g, sub_pose))
    s['f2_tile_frame'] = f2

    # C
    judges = {j: tilt_adjudicate.score(v, adj['key']) for j, v in sorted(adj['verdicts'].items())}
    for name, j in judges.items():
        j['by_direction'] = c_by_direction(adj['verdicts'][name], adj['key'])
        j['decision_bearing'] = name == 'jon'
        for a in j['arms'].values():
            a['verdict'] = c_verdict(a)
            a['decisive_share_leak'] = num(decisive_share(a, 'leak')) if decisive_share(a, 'leak') is not None else None
            a['decisive_share_stored'] = (num(decisive_share(a, 'stored'))
                                          if decisive_share(a, 'stored') is not None else None)
            for k in list(a):
                if isinstance(a[k], float):
                    a[k] = num(a[k])
    assert len(adj['draw']) == 1, 'one adjudication draw expected'
    draw = next(iter(adj['draw'].values()))
    s['c_adjudication'] = {'judges': judges, 'draw': draw, 'n_sheets': len(adj['key']),
                           'decisive_share': C_DECISIVE_SHARE,
                           'decision_bearing_judge': 'jon',
                           'status': 'awaiting Jon' if 'jon' not in judges else 'adjudicated'}

    # S1-S3
    pose_att = pose[pose['pose_source'] != 'none']
    s['s1_tilt_prior'] = {'by_scrape_era': tilt_prior(pose, corpus['dbear']),
                          'bearing_mix': 'all %d corpus labels\' dbear' % len(corpus)}
    s['s2_xml_npz'] = xml_npz_convention(overlap)
    s['s2_ps_camera_pitch'] = ps_camera_pitch_check(corpus, cpose)
    s['s3_miscentering'] = miscentering_table(corpus, pose_att)

    # S4
    with open(os.path.join(DATA, PREFIX + 's4-extent-gold.json'), encoding='utf-8') as f:
        s4 = json.load(f)
    s['s4_extent_gold'] = {'n_pairs': s4['n_pairs'], 'analysed': s4['n_pairs'] >= S4_MIN_PAIRS,
                           'by_city': {c: {k: v for k, v in d.items() if k != 'pairs'}
                                       for c, d in s4['cities'].items()}}
    s['generated_from']['s4-extent-gold'] = {'file': PREFIX + 's4-extent-gold.json',
                                             'md5': md5(os.path.join(DATA, PREFIX + 's4-extent-gold.json'))}

    s['populations'] = {
        'seattle_npz_facades': {'keys': ['f1_depth_frame'], 'frame': 'Seattle depth artifacts in a seeded 1/4 '
                                'shard sample (facades for a seeded subset) + every corpus artifact'},
        'lean_panos': {'keys': ['f2_tile_frame'], 'frame': 'corpus panos on disk + store-side Seattle sample'},
        'adjudication_draw': {'keys': ['c_adjudication'], 'frame': 'corpus labels, live measurable rule, '
                              'pose matching the scrape era, |T| threshold per draw'},
        'seattle_pose_sample': {'keys': ['s1_tilt_prior', 's2_xml_npz', 's3_miscentering'],
                                'frame': 'Seattle, seeded 1/4 shard sample; S3 bearings from the corpus'},
        'corpus_panos': {'keys': ['s2_ps_camera_pitch'], 'frame': 'the 661 corpus panos x PS camera_pitch'},
        'rampnet_gold': {'keys': ['s4_extent_gold'], 'frame': 'RampNet sao_paulo/paterson gold x PS CurbRamp'},
    }
    s['wrong_turns'] = WRONG_TURNS
    check_populations(s)
    write_json(s, args.write)
    print('F1 seattle b_p %s b_r %s (%s)' % (fmt(f1['seattle']['fit']['beta_p'], '.4f'),
                                              fmt(f1['seattle']['fit']['beta_r'], '.4f'), f1['seattle']['verdict']))
    for name, v in f2.items():
        print('F2 %-16s n=%4d raw %s/%s a %s cal %s/%s (%s)' % (
            name, v['n_panos'], fmt(v['raw']['beta_p'], '.3f'), fmt(v['raw']['beta_r'], '.3f'),
            fmt(v['a_pitch'], '.3f'), fmt(v['beta_p_calibrated'], '.3f'), fmt(v['beta_r_calibrated'], '.3f'),
            v['verdict']))
    for j, v in judges.items():
        for arm, a in v['arms'].items():
            print('C %s %s: stored %d leak %d antileak %d none %d (%s)' % (j, arm, a['stored'], a['leak'], a['antileak'],
                                                                        a['none'], a['verdict']))
    if args.figure_dir:
        import tilt_figures
        seattle_fac = facade_table(fac[fac['source'] == 'seattle'], pose[pose['npz_present'] == 1])
        tilt_figures.make_all(s, seattle_fac, panels, args.figure_dir,
                              horizon_examples=_horizon_examples(cpose, args.pano_root),
                              sheets=_sheet_examples(adj, args.sheets_dir, 'claude-opus-5-5'))
    return s


def lean_panel(lean, pose):
    """Pano-demeaned (predicted, measured) base-variant leans for one arm's figure panel."""
    base = lean[lean['variant'] == 'base'].merge(pose[['city', 'pano_id', 'pitch_deg', 'roll_deg']],
                                                 on=['city', 'pano_id'], how='inner', validate='many_to_one')
    base = base[np.isfinite(base['lean_deg'])]
    pred = tg.vertical_lean_deg(base['bin_centre_deg'].to_numpy(float), base['pitch_deg'].to_numpy(float),
                                base['roll_deg'].to_numpy(float))
    key = base['city'] + '/' + base['pano_id']
    return _demean(pred, key), _demean(base['lean_deg'], key)


def _horizon_examples(cpose, pano_root):
    """The most-tilted corpus pano of each scrape era that is on disk here."""
    if not pano_root:
        return None
    out = []
    posed = cpose[cpose['pose_source'] != 'none'].assign(
        mag=lambda d: np.hypot(d['pose_pitch_deg'], d['pose_roll_deg']))
    for era in ('xml', 'modern'):
        for row in posed[posed['scrape_era'] == era].sort_values('mag', ascending=False).itertuples():
            path = os.path.join(pano_root, row.city, row.pano_id[:2], row.pano_id + '.jpg')
            if os.path.exists(path) and row.mag < 8:
                out.append((path, row.pose_pitch_deg, row.pose_roll_deg,
                            '%s  %s scrape, %s pose: pitch %.2f, roll %.2f.  white = straight rig horizon, '
                            'orange = where the gravity horizon runs' % (
                                row.pano_id, era, row.pose_source, row.pose_pitch_deg, row.pose_roll_deg)))
                break
    return out


def _sheet_examples(adj, sheets_dir, judge, n=8):
    """n adjudication sheets, alternating era arms, captioned with the (unblinded) verdict."""
    if not sheets_dir or judge not in adj['verdicts']:
        return None
    paths, caps = [], []
    tokens = sorted(adj['key'])
    for arm in ('legacy+mid', 'post179') * (n // 2):
        for t in tokens:
            k = adj['key'][t]
            if k['era_arm'] == arm and t not in [os.path.basename(p)[:-4] for p in paths]:
                ch = adj['verdicts'][judge][t]
                chosen = 'none' if ch == 'none' else k['order']['ABC'.index(ch)]
                paths.append(os.path.join(sheets_dir, t + '.jpg'))
                caps.append('%s | %s | T = %+.1f deg | key: A=%s B=%s C=%s | preliminary machine verdict: %s (%s)'
                            % (t, arm, k['T_deg'], k['order'][0], k['order'][1], k['order'][2], ch, chosen))
                break
    return paths, caps


WRONG_TURNS = [
    'The issue assumed per-pano tilt was fetchable only for panos Google still serves; the 2019-22 '
    'scrapes left the dead XML endpoint\'s tilt beside every pano they wrote, alive or not.',
    'The plan put the depth artifact\'s z axis up; the documented ray formula puts it down, and the '
    'plan\'s pose sign (pitch > 0 = nose up) was reversed with it. F1 measured slopes of -1.00 against '
    'that hypothesis before the module was switched to the measured sign.',
    'Instrument (b), RampNet extent gold as tilt gold, was void: RampNet points are pixel-frame '
    'detections, not Project Sidewalk clicks, so they carry no tilt term by construction.',
    'The planner\'s first depth-frame test used the ground plane, which is confounded by road grade '
    '(the rig sits on the road); facade planes were the fix.',
    'SidewalkWebpage 5174 shifted labels by the full camera_pitch, but the tilt at a label is '
    'T(b) = pitch cos b + roll sin b, near zero for labels beside the car, and PS stores no roll.',
    'The full Seattle store scan would have run about 95 minutes; it was stopped and replaced by a '
    'seeded one-in-four shard sample across four niced processes.',
    'The first corpus lean run used the pre-F1 sign in its calibration warps; it was re-run after the '
    'sign was fixed, and only the re-run is committed.',
    'The warp calibration the plan specified does not divide out world-leaning clutter, so the '
    'calibrated F2 slopes are lower bounds, not estimates (tests/test_tilt_frame.py pins this).',
    'The top-tilt store arm (10-21 deg) saturates the lean estimator, calibration slope near zero, and '
    'cannot be read; it is kept in the artifact and flagged, not used.',
    'The corpus alone yielded 21 labels at |T| >= 4 deg, only 2 of them post179; each arm was topped '
    'up to 24 from the Seattle store sample at the same threshold instead of widening to 3 deg.',
]


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    e = sub.add_parser('export')
    e.add_argument('--pose', action='append', required=True)
    e.add_argument('--corpus-pose', required=True)
    e.add_argument('--facades', action='append', required=True)
    e.add_argument('--corpus-facades', required=True)
    e.add_argument('--lean', action='append', required=True)
    e.add_argument('--adjudication', action='append', required=True)
    e.add_argument('--rampnet', action='append',
                   help='city=<RampNet boxes.json>,<rawLabels csv>, e.g. paterson-nj=...boxes.json,...paterson-nj.csv')
    a = sub.add_parser('analyze')
    a.add_argument('--write', default=ARTIFACT)
    a.add_argument('--figure-dir')
    a.add_argument('--pano-root', help='the gitignored corpus pano cache, for the horizon figure')
    a.add_argument('--sheets-dir', help='the adjudication sheets, for the example-sheet figure')
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    CropRunner.raise_decompression_bomb_ceiling()
    if args.cmd == 'export':
        export(args)
    else:
        analyze(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
