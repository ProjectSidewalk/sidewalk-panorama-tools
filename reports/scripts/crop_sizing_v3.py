"""Sizing rule v2 vs v3, scored against the same hand-drawn curb-ramp extents the v2 study used (#32).

Rule v3 swaps the one open half of #32 - the distance estimator - and nothing else: the window becomes
the angle a fixed span of world (`V3_CONTEXT_WIDTH_M`) subtends at the lle #3 cotangent-blend distance,
instead of the 2013 linear distance fed through a power law. The clamps, the 3:2 shape and the storage
cap are v2's. The gold is the RampNet benchmark's (four bundles, 658 boxed aprons), so this script
takes the bundle roots as arguments and commits its summary, which the tests pin:

    python reports/scripts/crop_sizing_v3.py \
        --bundle richmond=D:/Git/RampNet/benchmark/richmond \
        --bundle sao_paulo=D:/Git/RampNet/benchmark/sao_paulo \
        --bundle paterson=D:/Git/RampNet/benchmark/paterson \
        --bundle annapolis=D:/Git/RampNet/benchmark/annapolis \
        --write reports/data/2026-09-26-crop-sizing-v3.json

**The selection criterion for the one fitted constant**, stated here because it is the method lesson
from v2: `V3_CONTEXT_WIDTH_M` is the grid value (0.1 m steps, 4.0-8.0 m) whose pooled fill p50 is
closest to rule v2's pooled fill p50, computed live on the same ramps. That anchors v3 to the elicited
forced-choice band that selected v2 (fill 0.28-0.44), and makes every other column a like-for-like
comparison at the same median crop. The band-centre criterion (p50 closest to 0.36) is reported beside
it as a robustness row. The discriminating metrics are the ones that are NOT monotone in crop size -
`fill_log_sd`, `fill_p90_over_p10` and the extent R-squared; `frac_clearing_too_tight` and
`containment` are monotone in window size (a constant 90 deg window maximises both) and ride along as
checks only.

**Identity.** Gold ramps are keyed by `(city, pano_id, key)`. `pano_id` is the provider's (GSV or
Mapillary) and globally unique, so the `label_uid = city:label_id` convention the rawLabels studies need
does not arise here: nothing in this study carries a Project Sidewalk `label_id` at all.

**Every window is cut through the production functions**: `CropRunner.compute_crop_box` over
`CropRunner.crop_window_width(..., sizing_rule=rule)`. The context-width sweep calls
`CropRunner.geometric_window_fov_deg(CropRunner.blend_distance_m(dep), context_width_m=W)` explicitly -
it never monkeypatches the module constant.
"""
import argparse
import json
import math
import os
import statistics
import subprocess
import sys

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
for _p in (REPO_ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import CropRunner  # noqa: E402
from crop_sizing_v2 import TOO_TIGHT_FILL, box_inside_window, load_bundle, pct  # noqa: E402
from studyfmt import fmt, num  # noqa: E402

CLAMP_CENSUS_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-09-clamp-census.json')

# The sweep grid for V3_CONTEXT_WIDTH_M, in metres. Wide enough that both criteria land inside it.
GRID_STEP_M = 0.1
GRID_M = [round(4.0 + GRID_STEP_M * i, 1) for i in range(41)]

# The centre of the forced-choice band (fill 0.28-0.44) that selected v2 - the robustness criterion.
BAND_CENTRE_FILL = 0.36

# log() of a depression near zero diverges; the parametric fit floors it here (0.3 deg is under a
# tenth of the corpus's p10 and above every label that is not at the horizon).
PARAMETRIC_DEP_FLOOR_DEG = 0.3

# Depression bands for "what moves", in degrees. (lo, hi): lo <= dep < hi, None is open.
BANDS = [(None, 2.0), (2.0, 5.0), (5.0, 10.0), (10.0, 15.0), (15.0, 20.0), (20.0, 30.0),
         (30.0, 40.0), (40.0, None)]

# The scale grid option A is median-matched on, the same way V3_CONTEXT_WIDTH_M is.
RULE_A_SCALE_GRID = [round(1.5 + 0.05 * i, 2) for i in range(41)]

DISTANCE_TABLE_DEG = [0.5, 5.0, 10.0, 15.0, 20.0, 30.0, 35.0, 45.0]


# ---------------------------------------------------------------------------
# Geometry, all of it through CropRunner.

def depression(ramp):
    return CropRunner.label_depression_deg(ramp['y'], ramp['pano_h'])


def legacy_distance_m(depression_deg):
    """The 2013 linear distance v1/v2 size from, evaluated at this depression.

    `_reference_crop_size` works in a 6656-px frame on the offset ABOVE the horizon, so a depression is
    `-elevation_deg_to_px(dep, 6656)` there. Clipped at 0 exactly as the production line is.
    """
    ref_offset = -CropRunner.elevation_deg_to_px(depression_deg, CropRunner.V1_REF_HEIGHT)
    return max(0.0, CropRunner.V1_DIST_INTERCEPT + CropRunner.V1_DIST_SLOPE * ref_offset)


def legacy_zero_crossing_deg():
    """The depression at which the 2013 line reaches 0 m, after which the 1500-px clamp sizes the crop."""
    return CropRunner.elevation_px_to_deg(CropRunner.V1_DIST_INTERCEPT / CropRunner.V1_DIST_SLOPE,
                                          CropRunner.V1_REF_HEIGHT)


def v3_fov_at(depression_deg, width_m):
    return CropRunner.geometric_window_fov_deg(CropRunner.blend_distance_m(depression_deg),
                                               context_width_m=width_m)


def v2_fov_at(depression_deg, pano_height=CropRunner.V1_REF_HEIGHT):
    y = pano_height / 2.0 + CropRunner.elevation_deg_to_px(depression_deg, pano_height)
    return CropRunner.crop_window_fov_deg(y, pano_height, sizing_rule='v2')


def rule_a_fov_at(depression_deg, scale=CropRunner.CROP_SIZE_SCALE):
    """Option A, not shipped: the blend distance fed into v2's own power law, times `scale`.

    The obvious minimal edit, kept as a comparison - scored at v2's x2.5 and at the scale that matches
    v2's median fill, so it is compared with v3 like for like. It keeps the -1.192 exponent that was fit
    with the linear distance inside it.
    """
    distance = CropRunner.blend_distance_m(depression_deg)
    size = (CropRunner.V1_SIZE_COEF * distance ** CropRunner.V1_SIZE_EXP if distance > 0
            else CropRunner.V1_SIZE_MAX)
    size = min(max(size, CropRunner.V1_SIZE_MIN), CropRunner.V1_SIZE_MAX)
    deg = CropRunner.elevation_px_to_deg(size * scale, CropRunner.V1_REF_HEIGHT)
    return min(max(deg, CropRunner.CROP_MIN_FOV_DEG), CropRunner.CROP_MAX_FOV_DEG)


def window_for(ramp, rule=None, width_m=None, fov_fn=None):
    """The CropBox actually cut for this ramp under a rule, an explicit v3 width, or an angle function."""
    w, h = ramp['pano_w'], ramp['pano_h']
    if rule is not None:
        width_px = CropRunner.crop_window_width(ramp['y'], w, h, sizing_rule=rule)
    else:
        fov = v3_fov_at(depression(ramp), width_m) if fov_fn is None else fov_fn(depression(ramp))
        width_px = CropRunner.azimuth_deg_to_px(fov, w)
    return CropRunner.compute_crop_box(ramp['x'], ramp['y'], width_px, w, h)


def apron_deg(ramp):
    return CropRunner.azimuth_px_to_deg(ramp['box_w'], ramp['pano_w'])


# ---------------------------------------------------------------------------
# Scoring.

def _score_boxes(ramps, boxes):
    fills, degs, stored = [], [], []
    contained = fits = 0
    for ramp, box in zip(ramps, boxes):
        fills.append(ramp['box_w'] / box.width)
        degs.append(CropRunner.azimuth_px_to_deg(box.width, ramp['pano_w']))
        stored.append(min(box.width, CropRunner.CROP_MAX_STORED_WIDTH))
        contained += bool(box_inside_window(ramp, box.left, box.top, box.width, box.height))
        fits += bool(ramp['box_w'] <= box.width and ramp['box_h'] <= box.height)
    n = len(ramps)
    logs = [math.log(f) for f in fills]
    return {
        'n': n,
        'fill_p10': num(pct(fills, 10)), 'fill_p50': num(pct(fills, 50)), 'fill_p90': num(pct(fills, 90)),
        'fill_log_sd': num(statistics.stdev(logs)) if n > 1 else None,
        'fill_p90_over_p10': num(pct(fills, 90) / pct(fills, 10)),
        'frac_clearing_too_tight': num(sum(1 for f in fills if f <= TOO_TIGHT_FILL) / n),
        'containment': num(contained / n),
        'fits_by_size': num(fits / n),
        'window_deg_p10': num(pct(degs, 10)), 'window_deg_p50': num(pct(degs, 50)),
        'window_deg_p90': num(pct(degs, 90)),
        'stored_width_p50': pct(stored, 50), 'stored_width_p90': pct(stored, 90),
    }


def score_rule(ramps, rule):
    """A rule as CropRunner ships it (`sizing_rule=rule`, so v3 at the module's V3_CONTEXT_WIDTH_M)."""
    return _score_boxes(ramps, [window_for(r, rule=rule) for r in ramps])


def score_context_width(ramps, width_m):
    """Rule v3 at an explicit context width, without touching the module constant."""
    return _score_boxes(ramps, [window_for(r, width_m=width_m) for r in ramps])


def score_fov_fn(ramps, fov_fn):
    return _score_boxes(ramps, [window_for(r, fov_fn=fov_fn) for r in ramps])


def context_width_sweep(ramps, widths_m):
    rows = []
    for width_m in widths_m:
        s = score_context_width(ramps, width_m)
        rows.append({'context_width_m': width_m, 'fill_p50': s['fill_p50'],
                     'fill_log_sd': s['fill_log_sd'], 'frac_clearing_too_tight': s['frac_clearing_too_tight'],
                     'containment': s['containment']})
    return rows


def matched_context_width(ramps, target_fill_p50, widths_m):
    """The grid width whose fill p50 is nearest `target_fill_p50` (the first, on a tie); None if no ramps."""
    if not ramps or not widths_m:
        return None
    sweep = context_width_sweep(ramps, widths_m)
    return min(sweep, key=lambda row: abs(row['fill_p50'] - target_fill_p50))['context_width_m']


def _r2(x, y):
    """Squared Pearson correlation - the R-squared of the one-regressor least-squares line - or None."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 2 or np.var(x) == 0 or np.var(y) == 0:
        return None
    return num(np.corrcoef(x, y)[0, 1] ** 2)


def extent_fit(ramps, rule=None, width_m=None, fov_fn=None):
    """How well the window tracks the apron: R-squared of log(window_deg) against log(apron_deg).

    The window is a function of depression alone, so this is the share of the aprons' log-width
    variance a depression-only rule of this shape explains. None when either side is constant.
    """
    windows = [CropRunner.azimuth_px_to_deg(window_for(r, rule=rule, width_m=width_m, fov_fn=fov_fn).width,
                                            r['pano_w']) for r in ramps]
    return {'r2': _r2(np.log(windows), np.log([apron_deg(r) for r in ramps])), 'n': len(ramps)}


def _isotonic_fit(x, y):
    """Least-squares non-decreasing fit of y on x (pool adjacent violators), ties in x pooled first."""
    order = np.argsort(x, kind='stable')
    xs, ys = np.asarray(x, float)[order], np.asarray(y, float)[order]
    blocks = []                                    # [sum, count] per block, in x order
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[i]:
            j += 1
        blocks.append([float(ys[i:j + 1].sum()), j - i + 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
        i = j + 1
    fitted = np.concatenate([np.full(c, s / c) for s, c in blocks])
    out = np.empty_like(fitted)
    out[order] = fitted
    return out


def depression_only_ceiling(ramps):
    """What ANY depression-only rule could reach on this gold, two ways.

    * `isotonic_r2`: the best non-decreasing function of depression, fitted to log(apron_deg). v2 and v3
      are both monotone in depression and extent_fit's R-squared is the best affine map of the log
      window, which is itself monotone - so this is a true in-sample upper bound on both. It is the
      ceiling the tests hold the rules under.
    * `parametric_r2`: log(apron_deg) ~ [log(max(dep, 0.3)), dep, 1], three parameters. The smooth fit
      the plan proposed; it is NOT a bound on the rules (a 2 atan(W/2d) window is outside its span),
      and it is reported to show that.
    """
    deps = np.array([depression(r) for r in ramps])
    log_apron = np.log([apron_deg(r) for r in ramps])
    ss_tot = float(((log_apron - log_apron.mean()) ** 2).sum())
    if len(ramps) < 4 or ss_tot == 0:
        return {'isotonic_r2': None, 'parametric_r2': None, 'n_params': 3, 'n': len(ramps)}
    iso = _isotonic_fit(deps, log_apron)
    design = np.column_stack([np.log(np.maximum(deps, PARAMETRIC_DEP_FLOOR_DEG)), deps, np.ones(len(deps))])
    coef, *_ = np.linalg.lstsq(design, log_apron, rcond=None)
    return {'isotonic_r2': num(1 - float(((log_apron - iso) ** 2).sum()) / ss_tot),
            'parametric_r2': num(1 - float(((log_apron - design @ coef) ** 2).sum()) / ss_tot),
            'n_params': 3, 'n': len(ramps)}


def distance_exponent(ramps, estimator):
    """Log-log slope of apron angular width against an estimated distance. Geometry says -1.

    `estimator` is 'legacy' (the 2013 line) or 'blend' (lle #3). Ramps where the legacy line has
    already reached 0 m (below 35.1 deg) have no log and are excluded; `n` says how many were used.
    """
    fn = {'legacy': legacy_distance_m, 'blend': CropRunner.blend_distance_m}[estimator]
    pairs = [(fn(depression(r)), apron_deg(r)) for r in ramps]
    pairs = [(d, a) for d, a in pairs if d > 0]
    if len(pairs) < 2:
        return {'slope': None, 'r2': None, 'n': len(pairs)}
    x = np.log([d for d, _ in pairs])
    y = np.log([a for _, a in pairs])
    if np.var(x) == 0:
        return {'slope': None, 'r2': None, 'n': len(pairs)}
    slope = float(np.polyfit(x, y, 1)[0])
    return {'slope': num(slope), 'r2': _r2(x, y), 'n': len(pairs)}


def distance_table(depressions, width_m):
    return [{'depression_deg': dep,
             'legacy_m': num(legacy_distance_m(dep)),
             'blend_m': num(CropRunner.blend_distance_m(dep)),
             'v2_window_deg': num(v2_fov_at(dep)),
             'v3_window_deg': num(v3_fov_at(dep, width_m))} for dep in depressions]


def _band_name(lo, hi):
    if lo is None:
        return '<%g' % hi
    if hi is None:
        return '>=%g' % lo
    return '%g-%g' % (lo, hi)


def window_change(ramps, width_m):
    """How far v3 (at `width_m`) moves each gold ramp's window from v2's, as a ratio of the angles.

    Angles, not the cut integer widths, so the ratio is the rules' and not a pixel rounding.
    """
    ratios = [v3_fov_at(depression(r), width_m) / v2_fov_at(depression(r)) for r in ramps]
    bands = []
    for lo, hi in BANDS:
        in_band = [q for r, q in zip(ramps, ratios)
                   if (lo is None or depression(r) >= lo) and (hi is None or depression(r) < hi)]
        bands.append({'band': _band_name(lo, hi), 'n': len(in_band),
                      'ratio_p50': num(pct(in_band, 50)) if in_band else None})
    return {'ratio_p10': num(pct(ratios, 10)), 'ratio_p50': num(pct(ratios, 50)),
            'ratio_p90': num(pct(ratios, 90)),
            'frac_over_10pct': num(sum(1 for q in ratios if abs(q - 1) > 0.10) / len(ratios)),
            'frac_over_20pct': num(sum(1 for q in ratios if abs(q - 1) > 0.20) / len(ratios)),
            'bands': bands}


def rule_a_block(by_city, pooled, target_fill_p50):
    """Option A at v2's scale and at its own median-matched scale, pooled and per city."""
    def scored(ramps, scale):
        fn = lambda dep: rule_a_fov_at(dep, scale)  # noqa: E731
        return dict(score_fov_fn(ramps, fn), r2=extent_fit(ramps, fov_fn=fn)['r2'])

    matched = min(RULE_A_SCALE_GRID,
                  key=lambda k: abs(score_fov_fn(pooled, lambda dep: rule_a_fov_at(dep, k))['fill_p50']
                                    - target_fill_p50))
    return {'scale_grid_min': RULE_A_SCALE_GRID[0], 'scale_grid_max': RULE_A_SCALE_GRID[-1],
            'matched_scale': matched,
            'pooled_at_v2_scale': scored(pooled, CropRunner.CROP_SIZE_SCALE),
            'pooled_matched': scored(pooled, matched),
            'cities_matched': {c: scored(r, matched) for c, r in sorted(by_city.items()) if r}}


def cap_onset_deg(fov_fn, lo=0.0, hi=89.0, tol=1e-6):
    """The shallowest depression at which a rule's window reaches CROP_MAX_FOV_DEG (bisection)."""
    if fov_fn(hi) < CropRunner.CROP_MAX_FOV_DEG:
        return None
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if fov_fn(mid) >= CropRunner.CROP_MAX_FOV_DEG:
            hi = mid
        else:
            lo = mid
    return hi


def production_block(width_m, path=CLAMP_CENSUS_JSON):
    """Where production's curb-ramp labels sit, and what v3 does to a window at each percentile."""
    with open(path, encoding='utf-8') as f:
        census = json.load(f)
    deps = census['by_label_type']['CurbRamp']['depression_deg']
    rows = []
    for q in ('p10', 'p50', 'p90', 'p99'):
        dep = deps[q]
        rows.append({'percentile': q, 'depression_deg': num(dep), 'v2_window_deg': num(v2_fov_at(dep)),
                     'v3_window_deg': num(v3_fov_at(dep, width_m)),
                     'ratio': num(v3_fov_at(dep, width_m) / v2_fov_at(dep))})
    return {'source': os.path.relpath(path, REPO_ROOT).replace(os.sep, '/'),
            'label_type': 'CurbRamp', 'n': census['by_label_type']['CurbRamp']['n'], 'rows': rows}


def city_block(ramps, width_m):
    return {
        'n': len(ramps),
        'provider': 'mapillary' if all(r['pano_id'].isdigit() for r in ramps) else 'gsv',
        'pano_heights': sorted({r['pano_h'] for r in ramps}),
        'ramp_width_deg_p50': num(pct([apron_deg(r) for r in ramps], 50)),
        'v2': score_rule(ramps, 'v2'),
        'v3': score_context_width(ramps, width_m),
        'r2': {'v2': extent_fit(ramps, rule='v2')['r2'],
               'v3': extent_fit(ramps, width_m=width_m)['r2'],
               'ceiling': depression_only_ceiling(ramps)},
        'exponent': {'legacy': distance_exponent(ramps, 'legacy'),
                     'blend': distance_exponent(ramps, 'blend')},
    }


def rampnet_commit(path):
    try:
        return subprocess.run(['git', '-C', path, 'rev-parse', 'HEAD'], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build_summary(by_city, bundles, argv):
    pooled = [r for ramps in by_city.values() for r in ramps]
    v2_pooled = score_rule(pooled, 'v2')
    matched = matched_context_width(pooled, v2_pooled['fill_p50'], GRID_M)
    band = matched_context_width(pooled, BAND_CENTRE_FILL, GRID_M)
    summary = {
        'meta': {'rampnet_commit': rampnet_commit(next(iter(bundles.values()))),
                 'bundles': {c: p.replace('\\', '/') for c, p in sorted(bundles.items())},
                 'generated_by': 'python reports/scripts/crop_sizing_v3.py ' + ' '.join(argv)},
        'constants': {
            'v2': {'scale': CropRunner.CROP_SIZE_SCALE, 'min_fov_deg': CropRunner.CROP_MIN_FOV_DEG,
                   'max_fov_deg': CropRunner.CROP_MAX_FOV_DEG,
                   'aspect_w_over_h': CropRunner.CROP_ASPECT_W_OVER_H,
                   'max_stored_width': CropRunner.CROP_MAX_STORED_WIDTH},
            'v3': {'camera_height_m': CropRunner.V3_CAMERA_HEIGHT_M, 'blend_deg': CropRunner.V3_BLEND_DEG,
                   'dist_cap_m': CropRunner.V3_DIST_CAP_M,
                   'context_width_m': CropRunner.V3_CONTEXT_WIDTH_M}},
        'selection': {
            'criterion': 'grid context width whose pooled fill p50 is nearest rule v2 pooled fill p50',
            'target_fill_p50': v2_pooled['fill_p50'], 'grid_step_m': GRID_STEP_M,
            'grid_min_m': GRID_M[0], 'grid_max_m': GRID_M[-1],
            'matched_context_width_m': matched,
            'band_centre_fill': BAND_CENTRE_FILL, 'band_centre_width_m': band,
            'sweep': context_width_sweep(pooled, GRID_M)},
        'cities': {c: city_block(r, matched) for c, r in sorted(by_city.items()) if r},
        'pooled': city_block(pooled, matched),
        'distance_table': distance_table(DISTANCE_TABLE_DEG, matched),
        'window_change': window_change(pooled, matched),
        'rule_a_blend_powerlaw': rule_a_block(by_city, pooled, v2_pooled['fill_p50']),
        'rule_geometry': {'legacy_zero_crossing_deg': num(legacy_zero_crossing_deg()),
                          'v2_cap_onset_deg': num(cap_onset_deg(v2_fov_at)),
                          'v3_cap_onset_deg': num(cap_onset_deg(lambda d: v3_fov_at(d, matched))),
                          'v3_horizon_window_deg': num(v3_fov_at(0.0, matched)),
                          'v2_horizon_window_deg': num(v2_fov_at(0.0)),
                          'min_fov_deg': CropRunner.CROP_MIN_FOV_DEG},
        'production': production_block(matched),
    }
    summary['population'] = {'name': 'all boxed gold ramps in the four bundles', 'n': len(pooled),
                             'covers': sorted(k for k in summary if k not in ('meta', 'population'))}
    return summary


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bundle', action='append', required=True, metavar='CITY=PATH',
                        help='benchmark bundle with boxes.json + records.jsonl (repeatable)')
    parser.add_argument('--write', help='where to write the summary JSON')
    args = parser.parse_args(argv)

    bad = [spec for spec in args.bundle if '=' not in spec]
    if bad:
        parser.error(f'--bundle wants CITY=PATH; got {bad}')
    bundles = dict(spec.split('=', 1) for spec in args.bundle)
    by_city = {city: load_bundle(path) for city, path in bundles.items()}
    if not any(by_city.values()):
        parser.error('no boxed gold ramps found in the given bundles')

    summary = build_summary(by_city, bundles, argv)
    sel = summary['selection']
    print("v2 pooled fill p50 %s -> matched V3_CONTEXT_WIDTH_M %s m (band-centre criterion: %s m)"
          % (fmt(sel['target_fill_p50'], '.3f'), fmt(sel['matched_context_width_m'], '.1f'),
             fmt(sel['band_centre_width_m'], '.1f')))
    if sel['matched_context_width_m'] != CropRunner.V3_CONTEXT_WIDTH_M:
        print("WARNING: CropRunner.V3_CONTEXT_WIDTH_M is %s, not the matched %s - set it and re-run."
              % (CropRunner.V3_CONTEXT_WIDTH_M, sel['matched_context_width_m']))
    for name in sorted(summary['cities']) + ['pooled']:
        block = summary['pooled'] if name == 'pooled' else summary['cities'][name]
        print("%-10s n=%3d | fill p50 %s/%s  log-sd %s/%s  p90/p10 %s/%s  R2 %s/%s (ceiling %s)"
              % (name, block['n'], fmt(block['v2']['fill_p50'], '.3f'), fmt(block['v3']['fill_p50'], '.3f'),
                 fmt(block['v2']['fill_log_sd'], '.3f'), fmt(block['v3']['fill_log_sd'], '.3f'),
                 fmt(block['v2']['fill_p90_over_p10'], '.2f'), fmt(block['v3']['fill_p90_over_p10'], '.2f'),
                 fmt(block['r2']['v2'], '.3f'), fmt(block['r2']['v3'], '.3f'),
                 fmt(block['r2']['ceiling']['isotonic_r2'], '.3f')))
    wc = summary['window_change']
    print("v3/v2 window ratio p10 %s p50 %s p90 %s; %s move >10%%, %s >20%%"
          % (fmt(wc['ratio_p10'], '.3f'), fmt(wc['ratio_p50'], '.3f'), fmt(wc['ratio_p90'], '.3f'),
             fmt(wc['frac_over_10pct'], '.3f'), fmt(wc['frac_over_20pct'], '.3f')))

    if args.write:
        with open(args.write, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(summary, f, indent=1, sort_keys=True, allow_nan=False)
            f.write('\n')
        print("wrote %s" % args.write)
    return 0


if __name__ == '__main__':
    sys.exit(main())
