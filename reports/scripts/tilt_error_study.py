"""The #54 tilt study: which frame are the depth planes and the stored tiles in, is the labelled
feature at the stored pano_y, and what would a tilt leak cost a crop.

Two subcommands, so the analysis runs from committed data alone:

    # 1. export: gather the raw measurements from the gitignored cache into reports/data/
    python reports/scripts/tilt_error_study.py export \\
        --pose .cache/tilt/pose_seattle-wa.p0.csv ... --corpus-pose .cache/tilt/pose_corpus.csv \\
        --facades .cache/tilt/facades_seattle-wa.p0.csv ... --corpus-facades .cache/tilt/facades_corpus.csv \\
        --lean .cache/tilt/lean_corpus.csv --lean .cache/tilt/lean_store.csv \\
        --adjudication .cache/tilt/adjudication     # copied, sealed/ and all, to reports/data/
    # 1b. roll-census: how many rawLabels rows carry camera_roll (a gitignored rawLabels pull in,
    #     reports/data/2026-09-26-tilt-roll-census.json out)
    python reports/scripts/tilt_error_study.py roll-census --rawlabels-dir .cache/rawlabels-all-2026-09-12 \\
        --fetched 2026-09-12
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
import shutil
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
CENSUS_JSON = os.path.join(DATA, '2026-08-09-photometa-census.json')
ROLL_CENSUS = os.path.join(DATA, '2026-09-26-tilt-roll-census.json')
ARTIFACT = os.path.join(DATA, PREFIX + 'error-study.json')
ADJ_DIR = os.path.join(DATA, PREFIX + 'adjudication')
ADJ_PUBLIC = ('draw.json', 'tasks.json', tilt_adjudicate.KEY_HASH, 'README.md')
ADJ_SEALED = ('key.json', 'salt.txt', 'README.md')

# The sheets whose key and machine verdict the first version of this PR printed in a report figure
# (reports/figures/2026-09-26-tilt-adjudication-sheet.jpg at 3f2e404, since removed). Their verdicts
# are scored apart from the rest, for every judge, so an exposure can be read off the result.
EXPOSED_IN_FIGURE = ('t03d0af5355', 't082917fa83', 't0a328afad9', 't0bc9196f09', 't0f6bdfeae5',
                     't16ed4a628d', 't29b6860eda', 't2cef14d740')

BANDS = ('<5', '5-15', '15-30', '>30')
F1_RIG, F1_GRAVITY = (0.9, 1.1), (-0.1, 0.1)
F2_RIG, F2_GRAVITY = (0.7, 1.3), (-0.3, 0.3)
C_RULE_SHARE = 0.9        # plan section 1.2: >= 90% of ALL n (none included) for one window, else 'split'
RAMPNET_SIGMA_PX = 12.0      # RampNet's stage-one click sigma on a 4096-high pano (issue #54 comment)
RAMPNET_HEIGHT_PX = 4096.0
TAGGER_WINDOW_PX = 640.0     # sidewalk-tagger-ai: a fixed 640 x 640 pixel crop (2026-08-09 consumer-requirements report)
Z95 = 1.959963984540054
N_BOOT = 1000               # pano-cluster bootstrap of the calibrated F2 ratios (b and a resampled together)
BOOT_SEED = 20260926

# How the Seattle store sample was drawn (tilt_pose_scan.py; the scan ran as four parts over a seeded
# one-in-four shard sample, each part reading facades from a seeded subset of its artifacts). Recorded
# here because the scan's own arguments are not in any committed file.
SEATTLE_SAMPLING = {'seed': 20260926, 'n_shards': 4096, 'shard_sample': '1/4', 'n_parts': 4,
                    'facade_artifacts_per_part': 1500, 'facade_artifacts_total': 6000,
                    'full_scan_estimate_minutes': 95}


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


def _calibration_pairs(lean, variant):
    """(base, calibrated) lean pairs per (pano, bin), with the predicted change computed from the
    tilt the instrument says it ADDED (its extra_pitch / extra_roll columns), never an assumed +2."""
    base = lean[lean['variant'] == 'base'][['city', 'pano_id', 'bin', 'bin_centre_deg', 'lean_deg']]
    cal = lean[lean['variant'] == variant][['city', 'pano_id', 'bin', 'lean_deg', 'extra_pitch', 'extra_roll']]
    m = base.merge(cal, on=['city', 'pano_id', 'bin'], suffixes=('_b', '_c'), validate='one_to_one')
    m = m[np.isfinite(m['lean_deg_b']) & np.isfinite(m['lean_deg_c'])]
    pred = tg.vertical_lean_deg(m['bin_centre_deg'].to_numpy(float), m['extra_pitch'].to_numpy(float),
                                m['extra_roll'].to_numpy(float))
    return m.assign(pred=pred, d=(m['lean_deg_c'] - m['lean_deg_b']).to_numpy(float))


def calibration_slope(lean, variant):
    """Attenuation: slope of (lean after a known added tilt - lean before) on the predicted change."""
    m = _calibration_pairs(lean, variant)
    if m.empty:
        return None, 0
    return num(np.dot(m['d'], m['pred']) / np.dot(m['pred'], m['pred'])), int(m['pano_id'].nunique())


SATURATED_A = 0.1


def tile_frame_fit(lean, pose, n_boot=N_BOOT):
    """F2 on one arm: pano fixed effects (demean within pano), the two-coefficient fit, and the
    calibrated coefficients b / a with a from the +2 deg pitch (and roll) warps - on this arm's own
    panos only.

    What the report reads, in order of robustness:
    * `raw_min_z`, `raw_excludes_zero`: the RAW slopes. Gravity-levelled tiles predict a raw slope of
      0 whatever the calibration, so "not levelled" rests on these, in every arm including saturated.
    * `saturated`: the pitch calibration slope is below SATURATED_A, so an added 2 deg barely moves the
      measurement - the estimator is outside its linear range (leans beyond its +-12 deg window) and no
      calibrated value from that arm means anything.
    * the calibrated coefficients b / a: ESTIMATES. Why they fall short of 1 is open: a synthetic scene
      with world-leaning clutter shows noise, not that shortfall (tests/test_tilt_frame.py). Two CIs:
      `ci_*_calibrated` divides the raw CI by a (conditional on a); `ci_*_calibrated_boot` is a
      pano-cluster bootstrap that resamples b and a together, and is the one the report quotes.
    * `gravity_band_excluded`: both calibrated CIs above the gravity band; None when saturated."""
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
    a_p, n_ap = calibration_slope(lean, 'cal_pitch')
    a_r, n_ar = calibration_slope(lean, 'cal_roll')
    out = {'raw': raw, 'n_panos': int(key.nunique()), 'a_pitch': a_p, 'a_pitch_n_panos': n_ap,
           'a_roll': a_r, 'a_roll_n_panos': n_ar}
    z = [raw['beta_%s' % ax] / raw['se_%s' % ax] for ax in ('p', 'r') if raw['se_%s' % ax]]
    out['raw_min_z'] = num(min(z)) if len(z) == 2 else None
    out['raw_excludes_zero'] = bool(raw['ci_p'][0] > 0 and raw['ci_r'][0] > 0)
    for axis, a in (('p', a_p), ('r', a_r)):
        ok = a is not None and raw['beta_' + axis] is not None
        out['beta_%s_calibrated' % axis] = num(raw['beta_' + axis] / a) if ok else None
        out['ci_%s_calibrated' % axis] = [num(v / a) for v in raw['ci_' + axis]] if ok else [None, None]
    out['saturated'] = bool(a_p is None or a_p < SATURATED_A)
    boot = calibrated_bootstrap(y, xp, xr, key, lean, n_boot=n_boot) if n_boot else None
    for axis in ('p', 'r'):
        out['ci_%s_calibrated_boot' % axis] = boot[axis] if boot else [None, None]
    lows = [out['ci_p_calibrated'][0], out['ci_r_calibrated'][0]]
    out['gravity_band_excluded'] = (None if out['saturated'] else
                                    bool(all(v is not None and v > F2_GRAVITY[1] for v in lows)))
    return out


def _per_cluster_sums(values, clusters, order):
    """Sum `values` (n, ...) within each cluster, rows aligned to `order` (missing clusters -> 0)."""
    idx = pd.Index(order).get_indexer(np.asarray(clusters))
    keep = idx >= 0
    out = np.zeros((len(order),) + np.asarray(values).shape[1:])
    np.add.at(out, idx[keep], np.asarray(values)[keep])
    return out


def calibrated_bootstrap(y, xp, xr, key, lean, n_boot=N_BOOT, seed=BOOT_SEED, weights=None):
    """95% percentile CIs of b_p / a_pitch and b_r / a_roll under a pano-cluster bootstrap that
    resamples the raw fit and both calibration slopes together (the calibration panos are a subset,
    one in four for roll, so their sampling error is part of the ratio's). Exact per-pano sufficient
    statistics, so a replicate is a weighted sum, not a refit: with every weight 1 it reproduces
    tile_frame_fit's point values exactly (tests pin this). -> {'p': [lo, hi], 'r': [lo, hi]}."""
    key = np.asarray(key)
    pairs = {axis: _calibration_pairs(lean, variant) for axis, variant in (('p', 'cal_pitch'), ('r', 'cal_roll'))}
    cal_keys = {axis: (m['city'].astype(str) + '/' + m['pano_id']).to_numpy() for axis, m in pairs.items()}
    panos = np.unique(np.concatenate([key] + list(cal_keys.values())))
    X = np.column_stack([xp, xr])
    ok = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    XtX = _per_cluster_sums(X[ok][:, :, None] * X[ok][:, None, :], key[ok], panos)
    Xty = _per_cluster_sums(X[ok] * np.asarray(y)[ok][:, None], key[ok], panos)
    cal = {}
    for axis, m in pairs.items():
        k = cal_keys[axis]
        cal[axis] = (_per_cluster_sums((m['d'] * m['pred']).to_numpy(float), k, panos),
                     _per_cluster_sums((m['pred'] * m['pred']).to_numpy(float), k, panos))
    if weights is None:
        rng = np.random.default_rng(seed)
        w = rng.multinomial(len(panos), np.full(len(panos), 1.0 / len(panos)), size=n_boot).astype(float)
    else:
        w = np.asarray(weights, float)                  # (replicates, panos in np.unique order): a test seam
    beta = np.linalg.solve(np.einsum('bk,kij->bij', w, XtX), np.einsum('bk,ki->bi', w, Xty)[..., None])[..., 0]
    out = {}
    for i, axis in enumerate(('p', 'r')):
        num_, den = cal[axis]
        with np.errstate(divide='ignore', invalid='ignore'):
            ratio = beta[:, i] / ((w @ num_) / (w @ den))
        ratio = ratio[np.isfinite(ratio)]
        out[axis] = [num(np.percentile(ratio, 2.5)), num(np.percentile(ratio, 97.5))] if len(ratio) else [None, None]
    return out


# ---- C ---------------------------------------------------------------------------------------

def sign_test(leak, antileak):
    """The blind-robust contrast: the stored window always sits between the other two, so a judge can
    tell it apart by content, but nothing on a sheet says which outer window is which without T's
    sign. Under no leak, leak and antileak are exchangeable: one-sided exact sign test."""
    n = leak + antileak
    return {'leak': int(leak), 'antileak': int(antileak),
            'p_one_sided': num(tilt_adjudicate.binom_sf(leak, n, 0.5)) if n else None}


def posthoc_decisive_share(arm, name):
    """Share of the non-`none` verdicts that chose `name`; None when every verdict was `none`.
    A POST-HOC summary, not a rule: the pre-set rule (c_verdict) keeps `none` in the denominator."""
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


def rig_pixel_shift_stats(key):
    """How far the exact rig pixel (tilt_geometry.rig_pixel_from_gravity_pixel, both axes) sits from the
    leak window's centre, which shifts y only (y - T h/180, first order). The x term is first order in
    the tilt away from the horizon, so a crop-time correction must move both coordinates."""
    rows = []
    for k in key.values():
        w, h = k['pano_width'], k['pano_height']
        xr, yr = tg.rig_pixel_from_gravity_pixel(k['pano_x'], k['pano_y'], w, h, k['pitch_deg'], k['roll_deg'])
        dx = (float(xr) - k['pano_x'] + w / 2) % w - w / 2
        width = CropRunner.crop_window_width(k['pano_y'], w, h)
        dy = float(yr) - tilt_adjudicate.window_centres(k['pano_y'], k['T_deg'], h)['leak']
        rows.append((abs(dx), abs(dx) / width, abs(dy) / (width / CropRunner.CROP_ASPECT_W_OVER_H)))
    a = np.array(rows)
    return {'n': int(len(a)), 'max_abs_dx_px': num(a[:, 0].max()), 'max_dx_fraction_of_width': num(a[:, 1].max()),
            'median_dx_fraction_of_width': num(np.median(a[:, 1])),
            'max_dy_error_fraction_of_height': num(a[:, 2].max())}


def c_verdict(arm):
    if not arm['n']:
        return 'no data'
    for name in tilt_adjudicate.WINDOW_NAMES:
        if arm[name] / arm['n'] >= C_RULE_SHARE:
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
    others = [max(v) for k, v in alts.items() if k != best]
    return {'n_overlap': int(len(overlap)), 'best': best, 'min_miss_of_other_readings_deg': num(min(others)),
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
            'rampnet_sigma_multiple': num(t_p90_deg * RAMPNET_HEIGHT_PX / 180.0 / RAMPNET_SIGMA_PX),
            'tagger_640px_fraction_8192': num(t_p90_deg * 8192 / 180.0 / TAGGER_WINDOW_PX)}


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

TOP_LEVEL_META = {'generated_from', 'conventions', 'populations', 'wrong_turns', 'deviations', 'verdicts'}


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


F2_ARM_NAMES = {'corpus_xml': 'corpus, XML-era', 'corpus_modern': 'corpus, modern',
                'store_random_xml': 'store random, XML-era', 'store_random_modern': 'store random, modern',
                'store_top_xml': 'store top-tilt, XML-era', 'store_top_modern': 'store top-tilt, modern'}
S1_ERA_NAMES = {'xml': 'XML-era (xml)', 'modern': 'modern (npz)'}


def _c(n):
    return '{:,}'.format(n)


def _ci3(ci):
    return '[%s, %s]' % (fmt(ci[0], '.3f'), fmt(ci[1], '.3f'))


def _pct(x, spec='.1f'):
    return fmt(100 * x, spec) + '%'


def report_numbers(s):
    """Every number the markdown quotes, each IN THE CONTEXT it is quoted in - a whole table row, or
    the words around a number in prose - so a value that merely occurs somewhere else in the report
    (a bare '2', '14', '0.001') cannot satisfy it. Matched against the whitespace-collapsed markdown."""
    out = {}
    pops = s['populations']
    corpus = pops['corpus_panos']
    samp = pops['seattle_pose_sample']['sampling']
    s1 = s['s1_tilt_prior']
    out['method.sample'] = '%s two-character shards (%s pano ids)' % (_c(samp['n_shards']), _c(s1['n_pano_ids_sampled']))
    out['method.facades'] = 'a seeded %s artifacts per scan process (%s total)' % (
        _c(samp['facade_artifacts_per_part']), _c(samp['facade_artifacts_total']))
    out['method.seed'] = 'seed %d' % samp['seed']
    out['method.corpus'] = 'the %s panos of the 2026-08-12 study corpus in %d cities' % (_c(corpus['n_panos']),
                                                                                       corpus['n_cities'])
    out['wrong_turn.minutes'] = 'about %d minutes' % samp['full_scan_estimate_minutes']
    for arm, label in (('seattle', 'Seattle sample'), ('corpus', 'corpus, %d cities' % corpus['n_cities'])):
        f = s['f1_depth_frame'][arm]['fit']
        out['f1.%s' % arm] = '| %s | %s | %s | %s [%s, %s] | %s [%s, %s] | %s |' % (
            label, _c(f['n']), _c(f['n_clusters']), fmt(f['beta_p'], '.4f'), fmt(f['ci_p'][0], '.4f'),
            fmt(f['ci_p'][1], '.4f'), fmt(f['beta_r'], '.4f'), fmt(f['ci_r'][0], '.4f'), fmt(f['ci_r'][1], '.4f'),
            fmt(f['median_abs_resid_deg'], '.3f'))
    f2 = s['f2_tile_frame']
    for arm, v in f2.items():
        head = '| %s | %s | %s / %s (%s SE) | %s / %s |' % (
            F2_ARM_NAMES[arm], _c(v['n_panos']), fmt(v['raw']['beta_p'], '.3f'), fmt(v['raw']['beta_r'], '.3f'),
            fmt(v['raw_min_z'], '.1f'), fmt(v['a_pitch'], '.3f'), fmt(v['a_roll'], '.3f'))
        if v['saturated']:
            out['f2.%s' % arm] = head + ' saturated | saturated |'
        else:
            out['f2.%s' % arm] = head + ' %s %s | %s %s |' % (
                fmt(v['beta_p_calibrated'], '.3f'), _ci3(v['ci_p_calibrated_boot']),
                fmt(v['beta_r_calibrated'], '.3f'), _ci3(v['ci_r_calibrated_boot']))
    readable = [v for v in f2.values() if not v['saturated']]
    points = [x for v in readable for x in (v['beta_p_calibrated'], v['beta_r_calibrated'])]
    out['f2.minz'] = 'at least %s standard errors' % fmt(min(v['raw_min_z'] for v in f2.values()), '.1f')
    out['f2.range'] = 'from %s to %s' % (fmt(min(points), '.3f'), fmt(max(points), '.3f'))
    for era, name in (('xml', 'XML-era'), ('modern', 'modern')):
        r = f2['store_top_%s' % era]['pose_tilt_magnitude_range_deg']
        out['f2.top.%s' % era] = '%s-%s deg (%s)' % (fmt(r[0], '.1f'), fmt(r[1], '.1f'), name)
    s2 = s['s2_xml_npz']
    out['s2.n'] = 'fitted on %s Seattle panos carrying both files' % _c(s2['n_overlap'])
    out['s2.resid'] = 'median residuals against the npz pose are %s deg (pitch) and %s deg (roll)' % (
        fmt(s2['median_abs_dpitch_deg'], '.3f'), fmt(s2['median_abs_droll_deg'], '.3f'))
    out['s2.miss'] = 'misses by at least %s deg' % fmt(s2['min_miss_of_other_readings_deg'], '.2f')
    out['s2.share1'] = '%s of the overlap (%s panos)' % (_pct(s2['share_vector_diff_over_1deg']),
                                                         _c(s2['n_vector_diff_over_1deg']))
    ps = s['s2_ps_camera_pitch']
    out['ps.pitch'] = 'npz pitch to a median %s deg and the XML-derived pitch to %s deg' % (
        fmt(ps['npz']['median_abs_diff_deg'], '.3f'), fmt(ps['xml']['median_abs_diff_deg'], '.3f'))
    out['ps.roll'] = '%s of the %s corpus labels carry `camera_roll`' % (_c(ps['corpus_labels_with_camera_roll']),
                                                                         _c(ps['corpus_labels']))
    rc = ps['rawlabels_roll_census']
    out['ps.census'] = '%s of %s rawLabels rows across %d deployments (%s pull)' % (
        _c(rc['rows_with_camera_roll']), _c(rc['rows']), rc['n_deployments'], rc['fetched'])
    out['ps.census.src'] = 'all of them Mapillary (%s of %s Mapillary rows; %s of %s GSV rows)' % (
        _c(rc['by_source']['mapillary']['rows_with_camera_roll']), _c(rc['by_source']['mapillary']['rows']),
        _c(rc['by_source']['gsv']['rows_with_camera_roll']), _c(rc['by_source']['gsv']['rows']))
    out['s1.xmlposed'] = '%s posed panos are XML-era scrapes' % _c(s1['by_scrape_era']['xml']['n_panos'])
    out['s1.population'] = '%s posed of the %s sampled pano ids (%s have no pose' % (
        _c(s1['n_posed']), _c(s1['n_pano_ids_sampled']), _c(s1['n_unposed']))
    for era, v in s1['by_scrape_era'].items():
        out['s1.%s' % era] = '| %s | %s | %s / %s | %s / %s | %s / %s / %s |' % (
            S1_ERA_NAMES[era], _c(v['n_panos']), fmt(v['abs_pitch_p50_deg'], '.2f'), fmt(v['abs_pitch_p90_deg'], '.2f'),
            fmt(v['abs_roll_p50_deg'], '.2f'), fmt(v['abs_roll_p90_deg'], '.2f'), fmt(v['abs_T_p50_deg'], '.2f'),
            fmt(v['abs_T_p90_deg'], '.2f'), fmt(v['abs_T_p99_deg'], '.2f'))
    old = s1['superseded_prior']
    out['s1.old'] = '| photometa census, 2026-08-09 (live, all eras) | %s | %s / %s | %s / %s | not computed |' % (
        _c(old['n_panos']), fmt(old['abs_pitch_p50_deg'], '.2f'), fmt(old['abs_pitch_p90_deg'], '.2f'),
        fmt(old['abs_roll_p50_deg'], '.2f'), fmt(old['abs_roll_p90_deg'], '.2f'))
    out['s1.labels'] = 'bearings of all %s corpus labels' % _c(corpus['n_labels'])
    beta = s['s3_assumed_beta_range']
    out['s3.beta'] = 'b = %s' % fmt(beta['range'][0], '.3f')
    for row in s['s3_miscentering']:
        out['s3.%s' % row['band']] = '| %s | %s | %s | %s | %s | %s | %s | %s |' % (
            row['band'], fmt(row['T_p90_deg'], '.2f'), fmt(row['window_height_deg'], '.1f'),
            fmt(row['ceiling_shift_px_8192'], '.0f'), _pct(row['ceiling_fraction_of_height']),
            _pct(beta['fraction_of_height_by_band'][row['band']][0]), _pct(row['tagger_640px_fraction_8192'], '.0f'),
            fmt(row['rampnet_sigma_multiple'], '.1f'))
    rows = s['s3_miscentering']
    t = [r['T_p90_deg'] for r in rows]
    tag = [r['tagger_640px_fraction_8192'] for r in rows]
    sig = [r['rampnet_sigma_multiple'] for r in rows]
    out['s3.range.T'] = 'S3 p90 shifts (%s-%s deg at b = 1)' % (fmt(min(t), '.1f'), fmt(max(t), '.1f'))
    out['s3.range.tagger'] = '%s-%s of that side' % (fmt(100 * min(tag), '.0f'), _pct(max(tag), '.0f'))
    out['s3.range.sigma'] = '%s-%s of its click sigmas' % (fmt(min(sig), '.0f'), fmt(max(sig), '.0f'))
    c = s['c_adjudication']
    d = c['draw']
    out['c.draw'] = 'The corpus gave %d eligible legacy+mid labels and %d post179 ones' % (
        d['eligible_by_arm'].get('legacy+mid', 0), d['eligible_by_arm'].get('post179', 0))
    out['c.fill'] = '(%s and %s eligible there; %d and %d taken)' % (
        _c(d['fill_eligible_by_arm']['legacy+mid']), _c(d['fill_eligible_by_arm']['post179']),
        d['from_fill']['legacy+mid'], d['from_fill']['post179'])
    rp = c['rig_pixel_vs_leak_window']
    out['c.xshift'] = 'up to %s px (%s of the window width; median %s)' % (
        fmt(rp['max_abs_dx_px'], '.0f'), _pct(rp['max_dx_fraction_of_width']), _pct(rp['median_dx_fraction_of_width']))
    out['c.yerr'] = 'at most %s of the window height' % _pct(rp['max_dy_error_fraction_of_height'])
    out['c.nxml'] = '%d post179 labels' % len(c['post179_xml_posed_tokens'])
    for judge, j in c['judges'].items():
        lva = j['leak_vs_antileak']
        out['c.%s.lva' % judge] = 'leak %d, antileak %d' % (lva['leak'], lva['antileak'])
        out['c.%s.lva_p' % judge] = 'one-sided sign test p = %s' % fmt(lva['p_one_sided'], '.1e')
        for dname, words in (('leak_above', 'above the stored one'), ('leak_below', 'below it')):
            v = j['by_direction'][dname]
            out['c.%s.%s' % (judge, dname)] = '%s: leak %d, antileak %d of %d sheets' % (
                words, v['leak'], v['antileak'], v['n'])
        for arm, a in j['arms'].items():
            out['c.%s.%s' % (judge, arm)] = '| %s | %d | %d / %d / %d / %d | %s | %s |' % (
                arm, a['n'], a['stored'], a['leak'], a['antileak'], a['none'], a['verdict'],
                fmt(100 * a['posthoc_decisive_share_leak'], '.0f') + '%')
        e, u = j['exposed_in_figure'], j['not_exposed']
        out['c.%s.exposed' % judge] = 'the %d exposed sheets: %d / %d / %d / %d; the other %d: %d / %d / %d / %d' % (
            e['n'], e['stored'], e['leak'], e['antileak'], e['none'], u['n'], u['stored'], u['leak'], u['antileak'],
            u['none'])
        w = j['post179_without_xml_posed']
        out['c.%s.post179_noxml' % judge] = 'without them the post179 arm reads %d / %d / %d / %d of %d' % (
            w['stored'], w['leak'], w['antileak'], w['none'], w['n'])
    s4 = s['s4_extent_gold']
    out['s4'] = '%d pairs of a PS CurbRamp label and a RampNet gold box' % s4['n_pairs']
    sp = [v for k, v in s4['by_city'].items() if k.startswith('sao-paulo')]
    out['s4.sp'] = 'whose %d gold panos carry no PS labels' % sp[0]['gold_panos']
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
    assert len(args.adjudication) == 1, 'one adjudication folder expected'
    export_adjudication(args.adjudication[0], ADJ_DIR)
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
          % (len(pose), len(cpose), len(fac), len(lean), len(both),
             len(glob.glob(os.path.join(ADJ_DIR, 'sheets', '*.jpg')))))


def export_adjudication(src, dst):
    """Copy the judge-facing files, the sheets and sealed/ (key, salt, finished judges' verdicts).
    Never a key outside sealed/: the committed folder is the one a judge works in."""
    tilt_adjudicate.assert_blind(src)
    for sub in ('sheets', tilt_adjudicate.SEALED):
        os.makedirs(os.path.join(dst, sub), exist_ok=True)
    names = list(ADJ_PUBLIC) + [os.path.basename(p) for p in glob.glob(os.path.join(src, 'verdicts_*.jsonl'))]
    names += [os.path.join('sheets', os.path.basename(p)) for p in glob.glob(os.path.join(src, 'sheets', '*.jpg'))]
    names += [os.path.join(tilt_adjudicate.SEALED, n) for n in ADJ_SEALED]
    names += [os.path.join(tilt_adjudicate.SEALED, os.path.basename(p))
              for p in glob.glob(os.path.join(src, tilt_adjudicate.SEALED, 'verdicts_*.jsonl'))]
    for n in names:
        shutil.copyfile(os.path.join(src, n), os.path.join(dst, n))
    tilt_adjudicate.read_sealed_key(dst)       # the copy still matches its hash


def read_adjudication(adj_dir):
    """-> {'draw', 'key', 'verdicts': {judge: {token: choice}}, 'files': [paths read]}. The key is
    checked against the committed salted hash; verdicts come from sealed/ (judges who finished before
    the key was sealed) and from the working folder (judges since)."""
    sealed = os.path.join(adj_dir, tilt_adjudicate.SEALED)
    with open(os.path.join(adj_dir, 'draw.json'), encoding='utf-8') as f:
        draw = json.load(f)
    key = tilt_adjudicate.read_sealed_key(adj_dir)
    files = [os.path.join(adj_dir, 'draw.json'), os.path.join(adj_dir, tilt_adjudicate.KEY_HASH),
             os.path.join(sealed, 'key.json')]
    verdicts = {}
    for d in (sealed, adj_dir):
        for vp in sorted(glob.glob(os.path.join(d, 'verdicts_*.jsonl'))):
            judge = os.path.basename(vp)[len('verdicts_'):-len('.jsonl')]
            assert judge not in verdicts, 'judge %s has verdicts both sealed and unsealed' % judge
            verdicts[judge] = tilt_adjudicate.load_verdicts(d, judge)
            files.append(vp)
    return {'draw': draw, 'key': key, 'verdicts': verdicts, 'files': files}


def score_judge(verdicts, key, exposed=EXPOSED_IN_FIGURE):
    """tilt_adjudicate.score plus the by-direction split, the pre-set verdict per arm, the post-hoc
    decisive shares, and the same counts restricted to the exposed and the unexposed sheets. The
    per-token `none` list stays in the sealed verdict file, not in the artifact."""
    j = tilt_adjudicate.score(verdicts, key)
    j['by_direction'] = c_by_direction(verdicts, key)
    for a in j['arms'].values():
        a.pop('none_tokens', None)
        a['verdict'] = c_verdict(a)
        for name in ('leak', 'stored'):
            v = posthoc_decisive_share(a, name)
            a['posthoc_decisive_share_' + name] = num(v) if v is not None else None
        for k in list(a):
            if isinstance(a[k], float):
                a[k] = num(a[k])
    lva = {'leak': 0, 'antileak': 0}
    for a in j['arms'].values():
        lva['leak'] += a['leak']
        lva['antileak'] += a['antileak']
    j['leak_vs_antileak'] = sign_test(lva['leak'], lva['antileak'])
    for d in j['by_direction'].values():
        d['leak_vs_antileak_p'] = sign_test(d['leak'], d['antileak'])['p_one_sided']
    xml_post = sorted(t for t, k in key.items() if k['era_arm'] == 'post179' and k['scrape_era'] == 'xml')
    post = {t: c for t, c in verdicts.items() if key[t]['era_arm'] == 'post179' and t not in xml_post}
    j['post179_without_xml_posed'] = tilt_adjudicate.score(post, key)['arms'].get('post179')
    if j['post179_without_xml_posed']:
        j['post179_without_xml_posed'] = {k: v for k, v in j['post179_without_xml_posed'].items()
                                          if k in ('n', 'stored', 'leak', 'antileak', 'none')}
    for part, keep in (('exposed_in_figure', True), ('not_exposed', False)):
        sub = {t: c for t, c in verdicts.items() if (t in exposed) == keep}
        counts = {'n': 0, 'stored': 0, 'leak': 0, 'antileak': 0, 'none': 0}
        for t, c in sub.items():
            counts['n'] += 1
            counts['none' if c == 'none' else key[t]['order']['ABC'.index(c)]] += 1
        j[part] = counts
    return j


def roll_census(rawlabels_dir, fetched):
    """How many rawLabels rows carry camera_roll, per imagery source, over every deployment's CSV."""
    by_source, n_files = {}, 0
    for path in sorted(glob.glob(os.path.join(rawlabels_dir, '*.csv'))):
        n_files += 1
        df = pd.read_csv(path, usecols=['pano_source', 'camera_roll'], dtype={'pano_source': str})
        for src, g in df.groupby(df['pano_source'].fillna('unknown')):
            b = by_source.setdefault(src, {'rows': 0, 'rows_with_camera_roll': 0})
            b['rows'] += int(len(g))
            b['rows_with_camera_roll'] += int(g['camera_roll'].notna().sum())
    return {'fetched': fetched, 'n_deployments': n_files, 'by_source': by_source,
            'rows': sum(b['rows'] for b in by_source.values()),
            'rows_with_camera_roll': sum(b['rows_with_camera_roll'] for b in by_source.values())}


def analyze(args):
    corpus = read_csv(CORPUS_CSV)
    corpus['label_uid'] = label_uid(corpus)
    corpus['dbear'] = corpus['pano_x'] / corpus['pano_width'] * 360.0 - 180.0
    files = {k: os.path.join(DATA, PREFIX + k + ext) for k, ext in
             (('pose-seattle', '.csv.gz'), ('pose-corpus', '.csv.gz'), ('facades', '.csv.gz'),
              ('lean-measurements', '.csv.gz'), ('xml-npz-overlap', '.csv.gz'))}
    pose = read_csv(files['pose-seattle'])
    cpose = read_csv(files['pose-corpus'])
    fac = read_csv(files['facades'])
    lean = read_csv(files['lean-measurements'])
    overlap = read_csv(files['xml-npz-overlap'])
    adj = read_adjudication(ADJ_DIR)
    for path in adj['files']:
        files['adjudication/' + os.path.relpath(path, ADJ_DIR).replace(os.sep, '/')] = path

    s = {'generated_from': {k: {'file': os.path.relpath(v, DATA).replace(os.sep, '/'), 'md5': md5(v)}
                            for k, v in files.items()}}
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
            mag = np.hypot(sub_pose['pitch_deg'].to_numpy(float), sub_pose['roll_deg'].to_numpy(float))
            fit['pose_tilt_magnitude_range_deg'] = [num(mag.min()), num(mag.max())] if len(mag) else None
            fit['verdict'] = frame_verdict(fit['ci_p_calibrated'], fit['ci_r_calibrated'], rig2, F2_GRAVITY)
            fit['decision_rule'] = {'rig': list(rig2), 'gravity': list(F2_GRAVITY)}
            f2[name] = fit
            if arm_name != 'store_top':
                panels.append((name.replace('_', ' '),) + lean_panel(g, sub_pose))
    s['f2_tile_frame'] = f2

    # C
    judges = {}
    for name, v in sorted(adj['verdicts'].items()):
        judges[name] = score_judge(v, adj['key'])
        judges[name]['decision_bearing'] = name == 'jon'
    s['c_adjudication'] = {'judges': judges, 'draw': adj['draw'], 'n_sheets': len(adj['key']),
                           'decision_rule_share_of_all_n': C_RULE_SHARE,
                           'exposed_in_figure': list(EXPOSED_IN_FIGURE),
                           'post179_xml_posed_tokens': sorted(t for t, k in adj['key'].items()
                                                              if k['era_arm'] == 'post179' and k['scrape_era'] == 'xml'),
                           'rig_pixel_vs_leak_window': rig_pixel_shift_stats(adj['key']),
                           'decision_bearing_judge': 'jon',
                           'status': 'awaiting Jon' if 'jon' not in judges else 'adjudicated'}

    # S1-S3
    pose_att = pose[pose['pose_source'] != 'none']
    with open(CENSUS_JSON, encoding='utf-8') as f:
        census = json.load(f)['summary']['tilt']
    s['generated_from']['photometa-census'] = {'file': os.path.basename(CENSUS_JSON), 'md5': md5(CENSUS_JSON)}
    s['s1_tilt_prior'] = {'by_scrape_era': tilt_prior(pose, corpus['dbear']),
                          'bearing_mix': 'all %d corpus labels\' dbear' % len(corpus),
                          'n_pano_ids_sampled': int(len(pose)),
                          'n_posed': int((pose['pose_source'] != 'none').sum()),
                          'n_unposed': int((pose['pose_source'] == 'none').sum()),
                          'superseded_prior': {'source': '2026-08-09 photometa census, live Google photometa',
                                               'n_panos': census['n'],
                                               **{k: num(v) for k, v in census.items() if k != 'n'}}}
    s['s2_xml_npz'] = xml_npz_convention(overlap)
    s['s2_ps_camera_pitch'] = ps_camera_pitch_check(corpus, cpose)
    with open(ROLL_CENSUS, encoding='utf-8') as f:
        s['s2_ps_camera_pitch']['rawlabels_roll_census'] = json.load(f)
    s['generated_from']['roll-census'] = {'file': os.path.basename(ROLL_CENSUS), 'md5': md5(ROLL_CENSUS)}
    s['s3_miscentering'] = miscentering_table(corpus, pose_att)
    readable = [v for v in f2.values() if not v['saturated']]
    beta_lo = min(min(v['beta_p_calibrated'], v['beta_r_calibrated']) for v in readable)
    s['s3_assumed_beta_range'] = {
        'range': [num(beta_lo), 1.0],
        'status': 'ASSUMPTION, not a measurement: C yields no beta (a forced choice measures a direction), '
                  'so the range is F2\'s smallest readable calibrated point estimate up to the rig-aligned '
                  'ceiling; the shift scales linearly in beta',
        'fraction_of_height_by_band': {r['band']: [num(beta_lo * r['ceiling_fraction_of_height']),
                                                   r['ceiling_fraction_of_height']] for r in s['s3_miscentering']}}

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
        'seattle_pose_sample': {'keys': ['s1_tilt_prior', 's2_xml_npz', 's3_miscentering', 's3_assumed_beta_range'],
                                'frame': 'Seattle, seeded 1/4 shard sample, both scrape eras pooled for S3; '
                                         'S1/S3 T evaluated at corpus-label bearings (S3: per depression band)',
                                'sampling': SEATTLE_SAMPLING},
        'corpus_panos': {'keys': ['s2_ps_camera_pitch'], 'frame': 'the %d corpus panos x PS camera_pitch; '
                         'the roll census is every rawLabels row of one pull' % corpus['pano_id'].nunique(),
                         'n_panos': int(corpus[['city', 'pano_id']].drop_duplicates().shape[0]),
                         'n_cities': int(corpus['city'].nunique()), 'n_labels': int(len(corpus))},
        'rampnet_gold': {'keys': ['s4_extent_gold'], 'frame': 'RampNet sao_paulo/paterson gold x PS CurbRamp'},
    }
    s['wrong_turns'] = WRONG_TURNS
    s['deviations'] = DEVIATIONS
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
                              horizon_examples=_horizon_examples(cpose, args.pano_root))
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
    'The full Seattle store scan would have run about 95 minutes; it was stopped and replaced by a '
    'seeded one-in-four shard sample across four niced processes.',
    'The first corpus lean run used the pre-F1 sign in its calibration warps; it was re-run after the '
    'sign was fixed, and only the re-run is committed.',
    'The first version of this report read the calibrated F2 slopes as lower bounds, arguing that the '
    'warp rotates world-leaning clutter that the real tilt does not. The real tilt rotates the whole '
    'image, clutter included, and a many-scene synthetic shows noise, not a shortfall (#158 review); '
    'the calibrated values are estimates, and why they read below 1 is open.',
    'The top-tilt store arms saturate the lean estimator (calibration slope near zero) and cannot be '
    'calibrated; they are kept in the artifact and flagged, and only their raw slopes are read.',
    'The corpus alone yielded 21 labels at |T| >= 4 deg, only 2 of them post179; each arm was topped '
    'up to 24 from the Seattle store sample at the same threshold instead of widening to 3 deg.',
    'The first version committed the adjudication key beside the sheets, copied it into a second JSON, '
    'and printed it for 8 sheets in a report figure above the judging instructions (#158 review). The '
    'key is sealed now and those 8 sheets are scored apart.',
    'The first version headlined C as "the labelled feature sits at the rig pixel". By the pre-set rule '
    'both arms are split, and the one contrast a judge who knows the hypothesis cannot steer is leak '
    'against antileak (#158 review).',
]

DEVIATIONS = [
    'F1 was planned as a sweep of every Seattle depth artifact; it ran on the seeded one-in-four shard '
    'sample, with facades read from a seeded subset of each scan process\'s artifacts. Decided on the '
    'scan\'s run time, before any F1 fit.',
    'F2\'s store arms were planned at 300 panos per stratum and ran at 150, on the store decode cost; '
    'decided before any lean was measured.',
    'C\'s draw was topped up from the Seattle store sample at the same |T| threshold when the corpus '
    'gave too few eligible labels; decided after the eligibility count and before any sheet was judged.',
    'The plan asked S3 for a fitted-beta row beside the ceiling. C yields no beta (a forced choice '
    'measures a direction, not a slope), so S3 gives the ceiling and a range assumed from F2\'s '
    'calibrated estimates, labelled as an assumption.',
    'The plan treated the +2 deg warp as a calibration that recovers the slope. It is kept, but its '
    'calibrated values are reported as estimates with the shortfall open, and "not levelled" rests on '
    'the raw slopes.',
    '#54 asked for the #4784 signature directly, a sinusoid in bearing with amplitude set by the tilt; '
    'the plan replaced it with the forced choice at |T| >= 4 deg (plan section 1.3). The by-direction '
    'split is this report\'s partial evidence for the sign flip with bearing.',
    'The plan said to commit the adjudication key only after adjudication. It was committed early, '
    'and is now sealed with a salted hash in its place.',
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
    rc = sub.add_parser('roll-census')
    rc.add_argument('--rawlabels-dir', required=True)
    rc.add_argument('--fetched', required=True)
    a = sub.add_parser('analyze')
    a.add_argument('--write', default=ARTIFACT)
    a.add_argument('--figure-dir')
    a.add_argument('--pano-root', help='the gitignored corpus pano cache, for the horizon figure')
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    CropRunner.raise_decompression_bomb_ceiling()
    if args.cmd == 'export':
        export(args)
    elif args.cmd == 'roll-census':
        census = roll_census(args.rawlabels_dir, args.fetched)
        write_json(census, ROLL_CENSUS)
        print(json.dumps({k: v for k, v in census.items() if k != 'by_source'}))
    else:
        analyze(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
