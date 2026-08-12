"""Mapillary census: does the GSV-derived cropper-study machinery work on Mapillary imagery, and what
would a Mapillary stratum buy the #54 tilt endpoint?

The pre-registration puts Mapillary explicitly out of scope (§6, "GSV only in the study corpus"). That
was registered when no Mapillary deployment had launched and nothing about it had been measured. This
census measures it, on Richmond -- the first launched Mapillary city.

The question is not "is there enough data" (there is not, yet). It is whether the *instruments*
transfer, because three of them looked GSV-specific and each is load-bearing:

  * `pov_replay.get_3d_fov` is the GSV viewer's zoom->fov ladder. If the Mapillary viewer used a
    different fov model, the canvas-px -> degrees conversion the amendment rests on would have no
    Mapillary analogue.
  * `pov_if_centered` -> `exact_y` is the eligibility rule. If the front end ran a different
    canvas->pano projection for Mapillary, exact_y would not be stricter there, it would be
    meaningless.
  * `depression_from_pano_y` assumes a gravity-aligned equirectangular pano -- which is why stored
    pano_y carries no tilt term for GSV. Mapillary is crowd-sourced and need not be aligned.

The replay settles the first two at once: if stored pano_x/pano_y reproduce exactly from the stored
canvas/POV record, the front end must be running the same projection with the same fov ladder, because
fov sets the focal length. `replay` below is therefore the census's central measurement.

What Mapillary uniquely offers is in `tilt`: rawLabels carries `camera_roll` for Mapillary rows and
carries it for 0% of GSV rows, so the pre-registration's endpoint-2 sample -- which must come from
photometa and is therefore selected on Google survival (47.9%, era-graded) -- has an unselected
counterpart here.

Usage (offline once fetch_rawlabels.py has run):
    python mapillary_census.py ../scripts/.cache/rawlabels-mapillary --fetched 2026-08-11 \
        --gsv-dir ../scripts/.cache/rawlabels \
        --write ../data/2026-08-11-mapillary-census.json
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
import click_noise_study  # noqa: E402
import era_replay_study  # noqa: E402
import offaxis_covariate  # noqa: E402
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402
from studyfmt import fmt, num  # noqa: E402

# The pre-registration's §2.3 provisioning gate: the within-pano tilt contrast is reported "not
# estimable" below this many panos carrying >= 2 study labels at separated bearings.
WITHIN_PANO_PANOS_REQUIRED = 60
BEARING_SEPARATION_DEG = 60.0

# §5's residual sd, used to price SE(beta) at an achieved n. Not re-measured here -- it comes from the
# click-noise floor and gold-annotation gate, neither of which this census touches.
SIGMA_RESID_DEG = 0.59

# §2.2's decision rule needs a 95% CI inside a band of half-width 0.3, so SE must clear 0.3/1.96.
SE_REQUIRED = 0.3 / 1.96

# One physical curb ramp labelled from several panos should cluster within this radius. Generous: the
# stored lat/lng is itself an estimate (label-latlng-estimation, median 0.41 m at best).
OBJECT_RADIUS_M = 8.0
EARTH_R_M = 6371000.0


def imagery_source(df):
    """Which imagery each label sits on.

    `pano_source` is served by rawLabels and is authoritative -- 'mapillary' for all 267 Richmond
    rows, 'gsv' across the GSV cities. An earlier revision asserted no such column existed and
    reconstructed it from `pano_id` shape, which meant the census inferred by heuristic a fact the
    endpoint states, and the premise test ("the corpus is entirely Mapillary") was checking the
    heuristic against itself.

    The id shape is still reported, for two reasons. It subdivides what the column cannot: GSV ids
    are 22-char URL-safe base64 while Google *user photospheres* are longer `CAoS...` ids, and those
    are different capture rigs. And comparing the two gives the heuristic something to be wrong
    against -- `n_disagreeing_with_id_shape` is 0 on both corpora today, and would not be if a
    deployment ever served an all-numeric GSV id or a 22-char Mapillary one.

    `by_source` is None for a cached export predating the column, rather than silently falling back
    to the heuristic under the same key.
    """
    ids = df['pano_id'].astype(str)

    def shape(s):
        if s.isdigit():
            return 'mapillary'
        if len(s) == 22:
            return 'gsv'
        return 'gsv_photosphere' if s.startswith('CAoS') else 'unknown'

    by_shape = ids.map(shape)
    out = {'n_labels': int(len(df)),
           'by_id_shape': {k: int(v) for k, v in by_shape.value_counts().items()}}
    if 'pano_source' not in df:
        out['by_source'] = None
        out['n_disagreeing_with_id_shape'] = None
        return out
    served = df['pano_source'].astype(str)
    out['by_source'] = {k: int(v) for k, v in served.value_counts().items()}
    # A photosphere is GSV imagery, so it agrees with a served 'gsv'.
    normalised = by_shape.replace({'gsv_photosphere': 'gsv'})
    out['n_disagreeing_with_id_shape'] = int((normalised != served).sum())
    return out


def _histogram(keys):
    """Count occurrences into a dict, summing on key collision rather than overwriting.

    The obvious `{f'{v:g}': n for v, n in series.value_counts().items()}` loses labels: distinct floats
    can format to the same string, and the later entry replaces the earlier one. Richmond has
    `2.999999999999998` alongside `3.0` and two spellings of `1.9925` -- legacy client float noise --
    so the zoom histogram silently reported 264 of 267 labels. That is exactly the class of defect the
    off-axis review found in a census that read as exhaustive, so every histogram here sums to the
    label count and a test asserts it.
    """
    out = {}
    for key in keys:
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def replay(df):
    """The census's central measurement: does the verbatim GSV projection reproduce stored pano_x/y?

    An exact replay is only possible if the front end ran this same projection with this same fov
    ladder, so a 100% rate settles both the eligibility rule and the fov question at once.
    """
    out = era_replay_study.replay_frame(df)
    n = int(len(out))
    misses_y = out.loc[out['replayable_y'] & ~out['exact_y'], 'dy'].abs()
    misses_x = out.loc[out['replayable_x'] & ~out['exact_x'], 'dx'].abs()
    return {
        'n_labels': n,
        'replayable_x': int(out['replayable_x'].sum()),
        'replayable_y': int(out['replayable_y'].sum()),
        'exact_x': int(out['exact_x'].sum()),
        'exact_y': int(out['exact_y'].sum()),
        'exact_x_pct': num(100.0 * out['exact_x'].sum() / n) if n else None,
        'exact_y_pct': num(100.0 * out['exact_y'].sum() / n) if n else None,
        'max_abs_dx_px': num(misses_x.max()) if len(misses_x) else 0.0,
        'max_abs_dy_px': num(misses_y.max()) if len(misses_y) else 0.0,
        # Zoom is rounded to 4 dp first: the legacy client's truncation leaves values like
        # 2.999999999999998 that are the ladder stop 3, not an off-ladder zoom.
        'canvas_frames': _histogram(_frame_key(w, h) for w, h in
                                    zip(df['canvas_width'], df['canvas_height'])),
        'zoom_values': _histogram(f'{z:g}' for z in df['zoom'].round(4)),
        'pano_frames': _histogram(_frame_key(w, h) for w, h in
                                  zip(df['pano_width'], df['pano_height'])),
    }


def _frame_key(w, h):
    """A frame label for the dimension histograms, tolerating rows with no dimensions.

    `int(w)` raises on NaN, and NaN is the state rawlabels deliberately preserves for labels whose
    pano metadata never resolved -- 84 to 1,761 rows per city among the six GSV cities. Counting
    them as 'unresolved' keeps the histogram a partition of the frame, which is what makes it
    readable as a census; raising threw the whole run away instead.
    """
    if not (np.isfinite(w) and np.isfinite(h)):
        return 'unresolved'
    return f'{int(w)}x{int(h)}'


def delta_bearing(df):
    """§2.2's Δb, from stored pixels alone: label bearing − camera heading collapses to
    (pano_x / pano_width)·360 − 180 because the pano raster is heading-centred. No camera_heading
    term, per §1's standing constraint."""
    return (df['pano_x'] / df['pano_width']) * 360.0 - 180.0


def tilt(df, sigma_resid_deg=SIGMA_RESID_DEG):
    """Endpoint 2's design inputs, and the SE they imply at the achieved n.

    Not an estimate of β — that needs gold annotation, which does not exist yet. This is the *design*
    side: how much the two tilt regressors vary, which is what sets SE(β) = σ_resid / (sd · √n).
    """
    n = int(len(df))
    db = np.radians(delta_bearing(df))
    tp = df['camera_pitch'].astype(float) * np.cos(db)
    tr = df['camera_roll'].astype(float) * np.sin(db)
    sd_p, sd_r = float(tp.std(ddof=1)) if n > 1 else float('nan'), \
        float(tr.std(ddof=1)) if n > 1 else float('nan')
    se = lambda sd: num(sigma_resid_deg / (sd * np.sqrt(n))) if n and np.isfinite(sd) and sd else None
    return {
        'n_labels': n,
        'n_panos': int(df['pano_id'].nunique()),
        'camera_pitch_available_pct': num(100.0 * df['camera_pitch'].notna().mean()) if n else None,
        'camera_roll_available_pct': num(100.0 * df['camera_roll'].notna().mean()) if n else None,
        'camera_pitch_deg': _spread(df.groupby('pano_id')['camera_pitch'].first()),
        'camera_roll_deg': _spread(df.groupby('pano_id')['camera_roll'].first()),
        'sd_pitch_term_deg': num(sd_p),
        'sd_roll_term_deg': num(sd_r),
        'sd_combined_term_deg': num(float((tp + tr).std(ddof=1))) if n > 1 else None,
        'sigma_resid_deg': sigma_resid_deg,
        'se_beta_pitch': se(sd_p),
        'se_beta_roll': se(sd_r),
        'se_required': SE_REQUIRED,
        'decision_rule_reachable': bool(
            se(sd_p) is not None and se(sd_r) is not None
            and max(se(sd_p), se(sd_r)) < SE_REQUIRED),
    }


def _spread(series):
    a = np.asarray(series.dropna(), float)
    if not a.size:
        return None
    return {'n': int(a.size), 'min': num(a.min()), 'max': num(a.max()),
            'sd': num(a.std(ddof=1)) if a.size > 1 else None,
            'abs_p90': num(np.percentile(np.abs(a), 90))}


def within_pano_stratum(df, separation_deg=BEARING_SEPARATION_DEG):
    """§2.3's provisioning count: panos carrying >= 2 labels whose pairwise |ΔΔb| clears the gate.

    This is the binding constraint on a Mapillary tilt stratum, and it is a *pano* count — three labels
    on one pano at separated bearings are worth far more here than three labels on three panos.
    """
    ok = multi = 0
    for _, g in df.groupby('pano_id'):
        if len(g) < 2:
            continue
        multi += 1
        b = delta_bearing(g).to_numpy()
        if any(abs(((x - y + 180.0) % 360.0) - 180.0) >= separation_deg
               for x, y in itertools.combinations(b, 2)):
            ok += 1
    return {'n_panos': int(df['pano_id'].nunique()),
            'n_panos_multi_label': multi,
            'n_panos_separated': ok,
            'separation_deg': separation_deg,
            'required': WITHIN_PANO_PANOS_REQUIRED,
            'estimable': bool(ok >= WITHIN_PANO_PANOS_REQUIRED),
            'shortfall_panos': max(0, WITHIN_PANO_PANOS_REQUIRED - ok)}


def multi_perspective(df, label_type='CurbRamp', radius_m=OBJECT_RADIUS_M):
    """How many physical objects are labelled from more than one pano.

    Two consequences, opposite in sign. For endpoint 2 it is a gain: the same object seen from several
    panos varies both the rig tilt and Δb with identity held fixed. For endpoint 1 it is a caution —
    those labels are not independent observations, and the pre-registration clusters by *pano*, which
    does not absorb an object appearing in several panos. Effective n is nearer the object count than
    the label count.
    """
    g = df[df['label_type'] == label_type].dropna(subset=['latitude', 'longitude'])
    if not len(g):
        # The full key set, not a short dict: main() reads n_objects_multi_pano unconditionally and
        # raised KeyError after the entire census, and the short dict landed in the artifact too, so
        # a JSON consumer reading that key hit the same wall.
        return {'label_type': label_type, 'n_labels': 0, 'n_objects': 0,
                'n_objects_multi_pano': 0, 'n_objects_both_users': 0,
                'panos_per_object': None, 'radius_m': radius_m}
    pts = np.radians(g[['latitude', 'longitude']].to_numpy())
    assigned = -np.ones(len(g), int)
    nxt = 0
    for i in range(len(g)):
        if assigned[i] >= 0:
            continue
        d = EARTH_R_M * np.hypot(pts[:, 0] - pts[i, 0],
                                 (pts[:, 1] - pts[i, 1]) * np.cos(pts[i, 0]))
        assigned[(d <= radius_m) & (assigned < 0)] = nxt
        nxt += 1
    sizes = pd.DataFrame({'cluster': assigned, 'pano_id': g['pano_id'].to_numpy(),
                          'user_id': g['user_id'].to_numpy()}).groupby('cluster').agg(
        panos=('pano_id', 'nunique'), users=('user_id', 'nunique'))
    return {
        'label_type': label_type,
        'radius_m': radius_m,
        'n_labels': int(len(g)),
        'n_objects': int(nxt),
        'n_objects_multi_pano': int((sizes['panos'] >= 2).sum()),
        'n_objects_both_users': int((sizes['users'] >= 2).sum()),
        'panos_per_object': {str(k): int(v) for k, v in
                             sizes['panos'].value_counts().sort_index().items()},
    }


def crossed_block(df):
    """What a cross-user placement-noise estimate can actually be built from.

    Both estimators are reported because the gap between them is the finding: the clustering estimator
    needs the two clicks within a radius, the matched estimator only needs them to be on the same pano
    and the same type. On the Richmond block that is 2 pairs versus 6 — and both are far short of the
    ~150 a sigma needs, because sharing a *route* is not sharing panos.
    """
    # Both filters, and the geometry one first: has_located_referent decides whether a displacement
    # is *meaningful*, replayable_geometry whether it is *computable*. Skipping the latter let a row
    # with unresolved pano metadata produce a NaN cost that max_sep_deg cannot reject (NaN > 10.0 is
    # False), which then reached the artifact and aborted the write on the run's last line.
    df = df[click_noise_study.replayable_geometry(df)]
    comparable = df[rawlabels.has_located_referent(df)]
    shared = click_noise_study.shared_panos(comparable)
    by_user = comparable.groupby('user_id')['pano_id'].nunique().to_dict()

    clustered = click_noise_study.cluster_pairs(click_noise_study.cluster_labels(
        comparable, click_noise_study.PRIMARY_RADIUS_DEG))
    matched, diag = click_noise_study.matched_pairs(comparable, shared)
    return {
        'n_users': int(comparable['user_id'].nunique()),
        'panos_per_user': {str(u): int(v) for u, v in sorted(by_user.items())},
        'n_panos_shared_by_two_users': len(shared),
        'n_dropped_unlocated_referent': int(len(df) - len(comparable)),
        'clustered': {'radius_deg': click_noise_study.PRIMARY_RADIUS_DEG,
                      **click_noise_study.sigma_from_pairs(clustered)},
        # Labelled at the sigma, not three levels up. matched_study refuses a DEFAULTED pano list
        # because deriving one from shared_panos silently yields a sigma over force-paired distinct
        # objects; this call derives one on purpose -- the census's finding is that Richmond has no
        # designed crossed block -- so the number is real but must never be read as a
        # designed-block measurement. A consumer holding only this dict can now tell.
        'matched': {**diag, **click_noise_study.sigma_from_pairs(matched),
                    'panos_agreed': False,
                    'sigma_caveat': 'incidental co-location: the pano list was derived from '
                                    'shared_panos, not agreed between labellers, so pairs may '
                                    'cross distinct objects and this sigma is an upper bound'},
    }


def referent_exclusion(df):
    """How much the referent-quality rule removes, and via which arm.

    Both arms come from `rawlabels`, which is also what filters the corpus. This function used to
    re-implement the tag arm -- the same list comprehension over REGION_TAGS, transcribed -- so it
    reported the size of its own copy of the rule rather than of the rule applied. Nothing would have
    caught the divergence: `pool_referent_exclusion` asserts only that the arms sum to the total.
    """
    keep = rawlabels.has_located_referent(df)
    by_type = df['label_type']
    region = rawlabels.region_tag_mask(df)
    return {
        'n_labels': int(len(df)),
        'n_comparable': int(keep.sum()),
        'n_excluded': int((~keep).sum()),
        'excluded_no_referent_type': int(by_type.isin(rawlabels.NO_REFERENT_TYPES).sum()),
        'excluded_region_tag': int(region.sum()),
        # Split by type, because the arms are sized very differently in the two corpora and a single
        # total hides which rule is doing the work: Crosswalk carries the Richmond arm, NoSidewalk the
        # GSV one. Types absent from the corpus are omitted rather than reported as 0.
        'excluded_by_type': {str(t): int(k) for t, k in
                             sorted(by_type[by_type.isin(rawlabels.NO_REFERENT_TYPES)]
                                    .value_counts().items())},
        'rule': {'no_referent_types': sorted(rawlabels.NO_REFERENT_TYPES),
                 'region_tags': sorted('+'.join(p) for p in rawlabels.REGION_TAGS)},
    }


def geometry(df):
    """Depression bands and off-axis spread, so the Mapillary corpus can be placed against §2.1's
    strata and against the off-axis covariate's own distribution."""
    prep = offaxis_covariate.prepare(df)
    g = prep[prep['eligible']]
    return {
        'n_eligible': int(len(g)),
        'by_band': {b: int(k) for b, k in g['band'].value_counts().items()},
        'depression_deg': _spread(g['depression']),
        'offaxis_v_deg': _spread(g['offaxis_v']),
        'n_at_pitch_floor': int(g['at_floor'].sum()),
        'n_above_horizon': int((g['depression'] < 0).sum()),
    }


def gsv_contrast(csv_dir):
    """The one asymmetry that makes a Mapillary stratum worth having: camera_roll availability.

    §5 prices endpoint 2 at n ~= 310 rather than 650 because camera_roll is empty in 100% of GSV
    rawLabels rows, so pitch/roll must come from photometa, which only answers for panos still served
    by Google. Measured here rather than quoted.

    The referent exclusion is measured per GSV city for a different reason: the rule was *derived* from
    Richmond, but the corpus it will actually be applied to is the GSV one, where its arms are sized
    completely differently. NoSidewalk is absent from Richmond entirely and is the largest arm here.
    Reporting it only for Richmond would understate the rule by two orders of magnitude.
    """
    out = {}
    for path in sorted(glob.glob(os.path.join(csv_dir, '*.csv'))):
        city = os.path.splitext(os.path.basename(path))[0]
        df = rawlabels.load_rawlabels(path)
        out[city] = {
            'n_labels': int(len(df)),
            'camera_pitch_available_pct': num(100.0 * df['camera_pitch'].notna().mean()),
            'camera_roll_available_pct': num(100.0 * df['camera_roll'].notna().mean()),
            'referent_exclusion': referent_exclusion(df),
            'labels_by_type': {str(t): int(k) for t, k in df['label_type'].value_counts().items()},
        }
    return out


def pool_referent_exclusion(per_city):
    """Sum the per-city referent counts into one corpus figure, so the report quotes a computed total
    rather than one added up by hand. Every arm is disjoint — the tag arm is keyed on SurfaceProblem,
    which is not in NO_REFERENT_TYPES — so a plain sum is correct, and the reconciliation is asserted
    here rather than trusted.
    """
    keys = ['n_labels', 'n_comparable', 'n_excluded', 'excluded_no_referent_type', 'excluded_region_tag']
    total = {k: int(sum(c['referent_exclusion'][k] for c in per_city.values())) for k in keys}
    by_type = {}
    for city in per_city.values():
        for t, n in city['referent_exclusion']['excluded_by_type'].items():
            by_type[t] = by_type.get(t, 0) + int(n)
    total['excluded_by_type'] = dict(sorted(by_type.items()))
    assert total['n_comparable'] + total['n_excluded'] == total['n_labels']
    assert total['excluded_no_referent_type'] + total['excluded_region_tag'] == total['n_excluded']
    assert sum(by_type.values()) == total['excluded_no_referent_type']
    return total


def census(df):
    return {
        'imagery_source': imagery_source(df),
        'replay': replay(df),
        'tilt': tilt(df),
        'within_pano_stratum': within_pano_stratum(df),
        'multi_perspective': multi_perspective(df),
        'crossed_block': crossed_block(df),
        'referent_exclusion': referent_exclusion(df),
        'geometry': geometry(df),
        'labels_by_type': {str(t): int(k) for t, k in df['label_type'].value_counts().items()},
        'date_range': [str(df['time_created'].min().date()), str(df['time_created'].max().date())],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir', help='directory of Mapillary-city rawLabels exports')
    ap.add_argument('--fetched', required=True, metavar='DATE',
                    help='the date the CSVs were fetched (rawLabels is a moving target, and Richmond '
                         'is actively being labelled)')
    ap.add_argument('--gsv-dir', metavar='DIR',
                    help='the six-city GSV cache, for the camera_roll availability contrast')
    ap.add_argument('--write', metavar='JSON')
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.csv_dir, '*.csv')))
    if not paths:
        ap.error(f'no *.csv rawLabels exports found in {args.csv_dir} '
                 f'(fetch them with fetch_rawlabels.py, which writes Mapillary cities to '
                 f'reports/scripts/.cache/rawlabels-mapillary/)')

    result = {'source': '/v3/api/rawLabels?filetype=csv', 'fetched': args.fetched, 'cities': {}}
    frames = []
    for path in paths:
        city = os.path.splitext(os.path.basename(path))[0]
        print(f'-- {city}', flush=True)
        df = rawlabels.load_rawlabels(path)
        frames.append(df)
        result['cities'][city] = census(df)

    pooled = census(pd.concat(frames, ignore_index=True))
    result['pooled'] = pooled
    if args.gsv_dir:
        result['gsv_contrast'] = gsv_contrast(args.gsv_dir)
        result['gsv_referent_exclusion'] = pool_referent_exclusion(result['gsv_contrast'])

    r, t, w = pooled['replay'], pooled['tilt'], pooled['within_pano_stratum']
    print(f"\nsource: {pooled['imagery_source']['by_source']}")
    print(f"replay: exact_x {r['exact_x']}/{r['n_labels']} "
          f"({fmt(r['exact_x_pct'], '.1f')}%)  exact_y {r['exact_y']}/{r['n_labels']} "
          f"({fmt(r['exact_y_pct'], '.1f')}%)  max|dy| {fmt(r['max_abs_dy_px'], '.0f')} px")
    print(f"tilt:   camera_roll available {fmt(t['camera_roll_available_pct'], '.0f')}%  "
          f"sd(pitch term) {fmt(t['sd_pitch_term_deg'], '.2f')}  "
          f"sd(roll term) {fmt(t['sd_roll_term_deg'], '.2f')}")
    print(f"        SE(b_p) {fmt(t['se_beta_pitch'], '.3f')}  SE(b_r) {fmt(t['se_beta_roll'], '.3f')}"
          f"  (need < {t['se_required']:.3f}) -> decision rule "
          f"{'reachable' if t['decision_rule_reachable'] else 'NOT reachable'}")
    verdict = 'estimable' if w['estimable'] else f"short by {w['shortfall_panos']} panos"
    print(f"§2.3:   {w['n_panos_separated']}/{w['required']} panos with >= 2 labels "
          f">= {w['separation_deg']:g} deg apart -> {verdict}")
    c = pooled['crossed_block']
    print(f"crossed: {c['n_panos_shared_by_two_users']} shared panos, "
          f"{c['clustered']['n_pairs']} clustered pairs vs {c['matched']['n_pairs']} matched")
    m = pooled['multi_perspective']
    print(f"objects: {m['n_labels']} {m['label_type']} labels -> {m['n_objects']} objects, "
          f"{m['n_objects_multi_pano']} seen from >= 2 panos")
    e = pooled['referent_exclusion']
    print(f"referent: {e['n_comparable']}/{e['n_labels']} comparable "
          f"({fmt(num(100.0 * e['n_excluded'] / e['n_labels']), '.1f')}% excluded) {e['excluded_by_type']}")
    if 'gsv_referent_exclusion' in result:
        g = result['gsv_referent_exclusion']
        print(f"          GSV: {g['n_comparable']}/{g['n_labels']} comparable "
              f"({fmt(num(100.0 * g['n_excluded'] / g['n_labels']), '.1f')}% excluded) "
              f"{g['excluded_by_type']} + {g['excluded_region_tag']} region-tagged")

    if args.write:
        with open(args.write, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(result, f, indent=1, allow_nan=False)
        print(f'wrote {args.write}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
