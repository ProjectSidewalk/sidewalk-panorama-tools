"""The #186 consolidation: every crop-sizing rule scored on one committed gold, with intervals, out of sample,
per provider, and with the window sized at the gravity-frame y as well as the rig-frame y.

Report-only. Nothing here changes CropRunner: every 3:2 window is cut by `CropRunner.compute_crop_box` over
CropRunner's own sizing primitives, as `crop_sizing_v3.window_for` does, and production code never imports
this file. The criteria it answers were committed before it ran:
`reports/plans/2026-11-22-issue-186-crop-sizing-consolidation-plan.md` (commit `CRITERIA_COMMIT`).

Three subcommands:

    # 1. Copy the inputs out of a RampNet checkout and a sidewalk-auto-labeler checkout (read-only: every
    #    file is read with `git show <HEAD>:<path>`, so a dirty working tree cannot leak in).
    python reports/scripts/crop_sizing_consolidation.py extract --rampnet <RampNet> \\
        --labeler <sidewalk-auto-labeler> --out reports/data/2026-11-22-crop-sizing-consolidation

    # 2. The tilt arm's power simulation, alone. It reads the GSV gold's points, poses and pano sizes and
    #    never an apron extent; it was run and committed before any real apron was scored under a tilt
    #    configuration (the plan's "power first").
    python reports/scripts/crop_sizing_consolidation.py power \\
        --write reports/data/2026-11-22-crop-sizing-consolidation/power_first.json

    # 3. The study, from the committed extract only.
    python reports/scripts/crop_sizing_consolidation.py study \\
        --write reports/data/2026-11-22-crop-sizing-consolidation.json \\
        --per-ramp reports/data/2026-11-22-crop-sizing-consolidation/per_ramp.csv.gz

**Identity.** A gold ramp is `(city, pano_id, key)`; `pano_id` is the provider's and globally unique, and the
gold carries no Project Sidewalk `label_id`, so the `city:label_id` key the label studies need does not arise.

**The bootstrap** resamples panos within each city (B = 2000, `random.Random(186)`), represented as a
replicate-by-ramp matrix of draw counts, so every rule and configuration is scored on the same replicates and
every difference is paired. Percentiles are nearest rank, on the replicate's expanded list.
"""
import argparse
import csv
import functools
import gzip
import hashlib
import io
import json
import math
import os
import random
import re
import subprocess
import sys

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
for _p in (REPO_ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import CropRunner  # noqa: E402
import pano_pose  # noqa: E402
import crop_sizing_v2 as csv2  # noqa: E402
import crop_sizing_v3 as csv3  # noqa: E402
from studyfmt import fmt, num  # noqa: E402

DATA_DIR = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-22-crop-sizing-consolidation')
SUMMARY_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-22-crop-sizing-consolidation.json')
CRITERIA_FILE = 'reports/plans/2026-11-22-issue-186-crop-sizing-consolidation-plan.md'
CRITERIA_COMMIT = '1ee6229b3f9243e056252d9c00b58df73d0d4335'
CLAMP_CENSUS_JSON = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-09-clamp-census.json')

CITIES = ('richmond', 'sao_paulo', 'paterson', 'annapolis')
GSV_CITIES = ('sao_paulo', 'paterson')
PROVIDERS = ('gsv', 'mapillary')

# --- the extract -------------------------------------------------------------------------------------------

GOLD_CSV = 'gold.csv'
GOLD_COLUMNS = ('city', 'pano_id', 'key', 'status', 'point_x', 'point_y', 'cx', 'cy', 'w', 'h',
                'pano_width', 'pano_height', 'source', 'capture_date', 'camera_pitch', 'camera_roll')
EVAL_JSON = 'crop_window_eval_%s.json'
TILT_SUMMARY = 'pano_tilt_validation_summary.json'
TILT_ROWS = 'pano_tilt_validation_gsv_gold_panos.json'
PLAN_SNAPSHOT = 'PLAN-crop-clarity.working-notes-2c46e7f.md'
LABELER_COMMIT = '2c46e7f510f017811c8fccd82d0fd17dbc8c774f'
LABELER_PLAN_PATH = 'PLAN-crop-clarity.md'
# The snapshot is the file at LABELER_COMMIT with each of these substitutions made exactly once: committed files
# carry no machine's absolute paths. Each is a regex (a local drive-letter path ending in the RampNet benchmark
# folder) and its replacement, so the path itself is written nowhere here; provenance.json records the sha256 of
# each removed string instead (review 1 of PR #254, finding 1: an earlier version wrote the path out).
SNAPSHOT_REDACTIONS = ((r'(?<![A-Za-z0-9])[A-Za-z]:\\[^\s`]*?\\RampNet\\benchmark', '<RampNet>\\benchmark'),)
PROVENANCE_JSON = 'provenance.json'
PER_RAMP_CSV = 'per_ramp.csv.gz'
POWER_FIRST_JSON = 'power_first.json'
RAMPNET_PLACEHOLDER = '<RampNet>'
LABELER_PLACEHOLDER = '<sidewalk-auto-labeler>'

# --- the study's constants (the criteria file's operational definitions) -------------------------------

TOO_TIGHT_FILL = csv2.TOO_TIGHT_FILL          # 0.49, the absolute-judgement round's threshold
BAND = (0.28, 0.44)                           # the forced-choice rounds' preferred fill band
BAND_CENTRE_FILL = csv3.BAND_CENTRE_FILL      # 0.36
CLEARING_FLOOR = 0.45                         # the per-city clearing floor the v2/v3 reports read
N_BOOT = 2000
BOOT_SEED = 186
POWER_SEED = 1860
NOISE_SEED = 1861
CLICK_NOISE_DEG = 0.53                        # the tilt study's 12 px click sigma on a 4096-high pano
BETA_PRIMARY = 0.95
BETA_SENSITIVITY = (1.0, 0.88)
POWER_SIMS = 200
POWER_BOOT = 500
POWER_MULTIPLIERS = (1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)
POWER_TARGET = 0.80
POWER_FILL0 = 0.371                           # v2's pooled fill p50 in the v3 artifact; cancels in the metrics
WIDE_WIDTH_GRID = [round(3.0 + 0.1 * i, 1) for i in range(91)]
SCALE_GRID = [round(1.0 + 0.05 * i, 2) for i in range(101)]

# The rule configurations of the plan's section 2.1, by name. 3:2 families are v2 (scale), v3 (context width,
# metres) and A (option A's scale); square ones are v1-norm (scale) and geo (geo-v1.5's camera height, metres).
RULES = {
    'v2_x2.5': ('v2', 2.5), 'v2_x2.0': ('v2', 2.0), 'v2_x3.0': ('v2', 3.0),
    'v3_5.8m': ('v3', 5.8), 'v3_6.0m': ('v3', 6.0),
    'A_x2.2': ('A', 2.2), 'A_x2.5': ('A', 2.5),
    'v1norm_x1.0_square': ('v1-norm', 1.0), 'v1norm_x3.5_square': ('v1-norm', 3.5),
    'geo_2.5m_square': ('geo', 2.5), 'geo_1.7m_square': ('geo', 1.7),
}
SQUARE_FAMILIES = ('v1-norm', 'geo')

# Criterion 2's paired comparisons: (name, first, second). Each difference is first minus second.
PAIRS = (('v3_minus_v2', 'v3_5.8m', 'v2_x2.5'), ('A_minus_v2', 'A_x2.2', 'v2_x2.5'),
         ('v3_minus_A', 'v3_5.8m', 'A_x2.2'))
DISCRIMINATING = {'fill_log_sd': 'lower', 'fill_p90_over_p10': 'lower', 'r2': 'higher'}
CHECKS = ('frac_clearing_too_tight', 'containment')

BOOT_METRICS = ('fill_p10', 'fill_p50', 'fill_p90', 'fill_log_sd', 'fill_p90_over_p10',
                'frac_clearing_too_tight', 'containment', 'window_deg_p10', 'window_deg_p50',
                'window_deg_p90', 'stored_width_p50', 'vfill_p50', 'r2')

TILT_CONFIGS = ('S-rig', 'S-grav', 'Off', 'S-neg', 'S-grav-noise')
TILT_RULES = {'v2': ('v2', 2.5), 'v3': ('v3', 5.8)}
TILT_METRICS = ('fill_log_sd', 'r2')

# RampNet crop_window_eval.py's geo-v1.5 constants (8d8d8ee, lines 178-181) other than the camera height.
GEO_RAMP_W_M = 1.5
GEO_TARGET_RATIO = 0.125
GEO_D_MIN_M, GEO_D_MAX_M = 2.0, 40.0
# The keys of RampNet's per-rule summary this study reproduces (detection mode), and of its rescale sweep.
RAMPNET_SUMMARY_KEYS = ('n', 'containment', 'predicted_side_p50', 'context_ratio_p50', 'context_ratio_h_p50',
                        'context_ratio_v_p50', 'size_ratio_p10', 'size_ratio_p50', 'size_ratio_p90')
RAMPNET_SWEEP_KEYS = ('n', 'containment', 'ctx_p50', 'ctx_p90', 'side_p50', 'capped')

# Question 7: the four camera heights in play, and what each one is.
CAMERA_HEIGHTS = (
    {'name': 'geo-v1.5 GEO_CAM_H_M', 'value_m': 2.5, 'where': 'RampNet scripts/analysis/crop_window_eval.py',
     'what': "the flat-ground rule's one free parameter; it absorbs every error of the flat-ground model"},
    {'name': 'geo-v1.5 at 1.7 m', 'value_m': 1.7, 'where': 'RampNet docs/crop_window_eval.md, round 2',
     'what': 'the height at which geo-v1.5 reconciles with the Richmond gold: a fitted value, not a measurement'},
    {'name': 'V3_CAMERA_HEIGHT_M', 'value_m': CropRunner.V3_CAMERA_HEIGHT_M,
     'where': 'sidewalk_panorama_tools.ps_math.BLEND_CAMERA_HEIGHT_M',
     'what': "the lle #3 cotangent blend's calibration constant, fitted with the blend"},
    {'name': 'auto-labeler issue 40', 'value_m': 2.21, 'where': 'sidewalk-auto-labeler issue 40',
     'what': 'a measured median GSV camera height'},
)


# ---------------------------------------------------------------------------------------------------------
# Extract.

def _git_show(root, rev, path):
    return subprocess.run(['git', '-C', root, 'show', '%s:%s' % (rev, path)], capture_output=True,
                          check=True).stdout


def _git_head(root):
    return subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'], capture_output=True, text=True,
                          check=True).stdout.strip()


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _key_order(key):
    kind, _, idx = key.partition(':')
    return ({'det': 0, 'missed': 1}.get(kind, 2), int(idx) if idx.isdigit() else 0, key)


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, float):
        return repr(value)
    return str(value)


def gold_rows(boxes_by_city, records_by_city):
    """One row per item of each city's boxes.json (status kept), joined to its pano record. Attribution
    fields stay in RampNet. Rows are in city, pano_id, key order."""
    rows = []
    for city in CITIES:
        records = {}
        for line in records_by_city[city].decode('utf-8').splitlines():
            if line.strip():
                rec = json.loads(line)
                records[str(rec['pano']['panorama_id'])] = rec['pano']
        panos = json.loads(boxes_by_city[city].decode('utf-8'))['panos']
        for pano_id in sorted(panos):
            pano = records[pano_id]
            for key in sorted(panos[pano_id], key=_key_order):
                item = panos[pano_id][key]
                point = item.get('point') or {}
                rows.append({'city': city, 'pano_id': str(pano_id), 'key': key, 'status': item.get('status'),
                             'point_x': point.get('x'), 'point_y': point.get('y'),
                             'cx': item.get('cx'), 'cy': item.get('cy'), 'w': item.get('w'), 'h': item.get('h'),
                             'pano_width': int(pano['width']), 'pano_height': int(pano['height']),
                             'source': pano.get('source'), 'capture_date': pano.get('capture_date'),
                             'camera_pitch': pano.get('camera_pitch'), 'camera_roll': pano.get('camera_roll')})
    return rows


def gold_csv_bytes(rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(GOLD_COLUMNS)
    for row in rows:
        writer.writerow([_cell(row[c]) for c in GOLD_COLUMNS])
    return out.getvalue().encode('utf-8')


def _json_bytes(obj):
    return (json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def redact_snapshot(data):
    """(redacted bytes, [sha256 of each removed string]). Each SNAPSHOT_REDACTIONS pattern must match once."""
    text = data.decode('utf-8')
    removed = []
    for pattern, replacement in SNAPSHOT_REDACTIONS:
        found = re.findall(pattern, text)
        if len(found) != 1:
            raise ValueError('expected one match of %r in the snapshot, found %d' % (pattern, len(found)))
        removed.append(_sha256(found[0].encode('utf-8')))
        text = re.sub(pattern, lambda m: replacement, text)
    return text.encode('utf-8'), removed


def build_extract(rampnet_root, labeler_root):
    """{filename: bytes} for every extract file plus provenance.json. Reads only committed blobs."""
    rev = _git_head(rampnet_root)
    sources, files = [], {}

    def read(path):
        data = _git_show(rampnet_root, rev, path)
        sources.append({'repo': 'RampNet', 'commit': rev, 'path': path, 'sha256': _sha256(data)})
        return data

    boxes = {c: read('benchmark/%s/boxes.json' % c) for c in CITIES}
    records = {c: read('benchmark/%s/records.jsonl' % c) for c in CITIES}
    rows = gold_rows(boxes, records)
    files[GOLD_CSV] = gold_csv_bytes(rows)
    for city in CITIES:
        files[EVAL_JSON % city] = read('analysis_out/crop_window_eval_%s.json' % city)
    files[TILT_SUMMARY] = read('analysis_out/pano_tilt/validation_summary.json')
    validation = json.loads(read('analysis_out/pano_tilt/validation_gsv.json').decode('utf-8'))
    gsv_panos = sorted({r['pano_id'] for r in rows if r['source'] != 'mapillary' and r['status'] == 'boxed'})
    kept = sorted((r for r in validation['rows'] if r['pano_id'] in set(gsv_panos)), key=lambda r: r['pano_id'])
    files[TILT_ROWS] = _json_bytes({'method_version': validation.get('method_version'),
                                    'params': validation.get('params'),
                                    'filter': 'rows of validation_gsv.json whose pano_id is a pano of the boxed '
                                              'GSV gold (%d panos)' % len(gsv_panos),
                                    'rows': kept})
    plan = _git_show(labeler_root, LABELER_COMMIT, LABELER_PLAN_PATH)
    sources.append({'repo': 'sidewalk-auto-labeler', 'commit': LABELER_COMMIT, 'path': LABELER_PLAN_PATH,
                    'sha256': _sha256(plan)})
    files[PLAN_SNAPSHOT], removed = redact_snapshot(plan)
    files[PROVENANCE_JSON] = _json_bytes({
        'rampnet_commit': rev,
        'labeler_commit': LABELER_COMMIT,
        'generated_by': ('python reports/scripts/crop_sizing_consolidation.py extract --rampnet %s --labeler %s '
                         '--out reports/data/2026-11-22-crop-sizing-consolidation'
                         % (RAMPNET_PLACEHOLDER, LABELER_PLACEHOLDER)),
        'sources': sources,
        'extracts': {name: {'sha256': _sha256(data), 'bytes': len(data)} for name, data in sorted(files.items())},
        'gold_rows': len(rows),
        'snapshot_redactions': [{'pattern': a, 'to': b, 'removed_sha256': h}
                                for (a, b), h in zip(SNAPSHOT_REDACTIONS, removed)],
        'not_copied': 'the pano records\' attribution fields, which stay in RampNet',
    })
    return files


def write_extract(files, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for name, data in files.items():
        with open(os.path.join(out_dir, name), 'wb') as f:
            f.write(data)


# ---------------------------------------------------------------------------------------------------------
# The gold, as ramps.

def read_gold(path):
    with open(path, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def provider_of_row(row):
    return 'mapillary' if row['source'] == 'mapillary' else 'gsv'


def _float_or_none(cell):
    return float(cell) if cell != '' else None


def boxed_ramps(rows):
    """The boxed gold as ramp dicts in native pixels, keyed exactly as crop_sizing_v2.load_bundle builds them,
    plus city, kind, provider, pose and the normalised fields RampNet's scorer reads."""
    ramps = []
    for row in rows:
        if row['status'] != 'boxed':
            continue
        w, h = int(row['pano_width']), int(row['pano_height'])
        px, py = float(row['point_x']), float(row['point_y'])
        cx, cy, wn, hn = (float(row[k]) for k in ('cx', 'cy', 'w', 'h'))
        ramp = {'city': row['city'], 'pano_id': row['pano_id'], 'key': row['key'],
                'kind': row['key'].split(':')[0], 'provider': provider_of_row(row),
                'pano_w': w, 'pano_h': h, 'x': px * w, 'y': py * h, 'box_w': wn * w, 'box_h': hn * h,
                'box_cx': cx * w, 'box_cy': cy * h, 'depression_deg': (py - 0.5) * 180.0,
                'pitch': _float_or_none(row['camera_pitch']), 'roll': _float_or_none(row['camera_roll']),
                'norm': (px, py, cx, cy, wn, hn)}
        if ramp['provider'] != csv3.provider_of(ramp):
            raise ValueError('provider of %s:%s disagrees with its id shape' % (row['city'], row['pano_id']))
        ramps.append(ramp)
    return ramps


def load_ramps(data_dir=DATA_DIR):
    return boxed_ramps(read_gold(os.path.join(data_dir, GOLD_CSV)))


def pct(values, q):
    return csv2.pct(values, q)


# ---------------------------------------------------------------------------------------------------------
# Rules and windows.

def rule_fov_deg(spec, size_y, pano_h):
    """A 3:2 rule's window angle for a label whose window is sized at `size_y`."""
    family, k = spec
    if family == 'v2':
        deg = CropRunner.elevation_px_to_deg(CropRunner.predict_crop_size(size_y, pano_h) * k, pano_h)
        return min(max(deg, CropRunner.CROP_MIN_FOV_DEG), CropRunner.CROP_MAX_FOV_DEG)
    dep = CropRunner.label_depression_deg(size_y, pano_h)
    if family == 'v3':
        return csv3.v3_fov_at(dep, k)
    if family == 'A':
        return csv3.rule_a_fov_at(dep, k)
    raise ValueError('not a 3:2 rule: %r' % (spec,))


def square_side(spec, y, pano_w, pano_h):
    """RampNet crop_window_eval.py's square rules (8d8d8ee, lines 364-385), side in native pixels."""
    family, k = spec
    if family == 'v1-norm':
        return CropRunner.predict_crop_size(y, pano_h) * k
    if family == 'geo':
        depression = (y / pano_h - 0.5) * math.pi
        if depression <= 0:
            d = GEO_D_MAX_M
        else:
            d = min(max(k / math.tan(depression), GEO_D_MIN_M), GEO_D_MAX_M)
        return (GEO_RAMP_W_M / d) * (pano_w / (2 * math.pi)) / GEO_TARGET_RATIO
    raise ValueError('not a square rule: %r' % (spec,))


def square_window(x, y, side, pano_w, pano_h):
    """RampNet's square crop_window: (left, top, size, shifted), size capped at both pano dimensions."""
    size = min(int(round(side)), pano_w, pano_h)
    left = int(round(x - size / 2)) % pano_w
    ideal_top = int(round(y - size / 2))
    top = max(0, min(ideal_top, pano_h - size))
    return left, top, size, top != ideal_top


def cut_box(ramp, spec, centre=None, size_y=None):
    """The window a rule cuts for a ramp, centred on `centre` (default the gold point) and sized at `size_y`
    (default the point's y). Square rules come back as a CropBox too, height equal to width."""
    cx, cy = (ramp['x'], ramp['y']) if centre is None else centre
    sy = ramp['y'] if size_y is None else size_y
    w, h = ramp['pano_w'], ramp['pano_h']
    if spec[0] in SQUARE_FAMILIES:
        left, top, size, shifted = square_window(cx, cy, square_side(spec, sy, w, h), w, h)
        return CropRunner.CropBox(left, top, size, size, shifted)
    width = CropRunner.azimuth_deg_to_px(rule_fov_deg(spec, sy, h), w)
    return CropRunner.compute_crop_box(cx, cy, width, w, h)


# ---------------------------------------------------------------------------------------------------------
# The tilt model.

def modelled_stored_point(ramp, beta=BETA_PRIMARY, sign=1.0, multiplier=1.0):
    """Where a Project Sidewalk click on this gold point would be stored, under the leak model: the
    gravity-frame pixel of the rig-frame point, at `beta` times the record pose (`sign` -1 negates it, the
    power control; `multiplier` scales it, the power simulation). The roll is wrapped first: the record
    serves it in [0, 360), and beta must scale -0.04, not 359.96."""
    if ramp.get('pitch') is None or ramp.get('roll') is None:
        raise ValueError('ramp %s:%s %s has no pose' % (ramp.get('city'), ramp.get('pano_id'), ramp.get('key')))
    s = sign * multiplier * beta
    gx, gy = pano_pose.gravity_pixel_from_rig_pixel(ramp['x'], ramp['y'], ramp['pano_w'], ramp['pano_h'],
                                                    s * ramp['pitch'], s * float(pano_pose.wrap_deg(ramp['roll'])))
    return float(gx), float(gy)


def config_size_ys(ramps, config, beta=BETA_PRIMARY, noise_seed=NOISE_SEED, multiplier=1.0):
    """The y each configuration sizes the window at, one per ramp."""
    if config == 'S-rig':
        return [r['y'] for r in ramps]
    if config in ('S-grav', 'Off'):
        return [modelled_stored_point(r, beta, multiplier=multiplier)[1] for r in ramps]
    if config == 'S-neg':
        return [modelled_stored_point(r, beta, sign=-1.0, multiplier=multiplier)[1] for r in ramps]
    if config == 'S-grav-noise':
        rng = random.Random(noise_seed)
        return [modelled_stored_point(r, beta, multiplier=multiplier)[1]
                + CropRunner.elevation_deg_to_px(rng.gauss(0.0, CLICK_NOISE_DEG), r['pano_h']) for r in ramps]
    raise ValueError('unknown configuration %r' % (config,))


def tilt_boxes(ramps, spec, config, beta=BETA_PRIMARY, noise_seed=NOISE_SEED, multiplier=1.0):
    sizes = config_size_ys(ramps, config, beta, noise_seed, multiplier)
    if config == 'S-rig':
        for r in ramps:            # a tilt configuration is defined on posed ramps only, S-rig included
            if r.get('pitch') is None or r.get('roll') is None:
                raise ValueError('ramp %s:%s has no pose' % (r.get('city'), r.get('pano_id')))
    boxes = []
    for r, sy in zip(ramps, sizes):
        centre = modelled_stored_point(r, beta, multiplier=multiplier) if config == 'Off' else None
        boxes.append(cut_box(r, spec, centre=centre, size_y=sy))
    return boxes


# ---------------------------------------------------------------------------------------------------------
# Metrics: point estimates through crop_sizing_v3's scorer, replicates vectorised over a weight matrix.

def point_metrics(ramps, boxes):
    """crop_sizing_v3._score_boxes plus R² (log window degrees on log apron degrees) and vertical fill."""
    out = csv3._score_boxes(ramps, boxes)
    windows = [CropRunner.azimuth_px_to_deg(b.width, r['pano_w']) for r, b in zip(ramps, boxes)]
    out['r2'] = csv3._r2(np.log(windows), np.log([csv3.apron_deg(r) for r in ramps]))
    out['vfill_p50'] = num(pct([r['box_h'] / b.height for r, b in zip(ramps, boxes)], 50))
    return out


def ramp_arrays(ramps, boxes):
    width = np.array([b.width for b in boxes], float)
    height = np.array([b.height for b in boxes], float)
    box_w = np.array([r['box_w'] for r in ramps], float)
    pano_w = np.array([r['pano_w'] for r in ramps], float)
    fill = box_w / width
    window_deg = width / pano_w * 360.0
    return {
        'fill': fill, 'log_fill': np.log(fill),
        'vfill': np.array([r['box_h'] for r in ramps], float) / height,
        'clears': (fill <= TOO_TIGHT_FILL).astype(float),
        'contained': np.array([bool(csv2.box_inside_window(r, b.left, b.top, b.width, b.height))
                               for r, b in zip(ramps, boxes)], float),
        'window_deg': window_deg, 'log_window': np.log(window_deg),
        'log_apron': np.log(box_w / pano_w * 360.0),
        'stored': np.minimum(width, CropRunner.CROP_MAX_STORED_WIDTH),
    }


def weighted_percentile(x, W, q):
    """Nearest-rank q-th percentile of each replicate's expanded list (row b of W counts each value's copies)."""
    x = np.asarray(x, float)
    W = np.asarray(W)
    order = np.argsort(x, kind='stable')
    cum = np.cumsum(W[:, order], axis=1)
    total = cum[:, -1]
    k = np.rint(q / 100.0 * (total - 1)).astype(np.int64)
    k = np.clip(k, 0, np.maximum(total - 1, 0).astype(np.int64))
    out = x[order][(cum > k[:, None]).argmax(axis=1)]
    return np.where(total > 0, out, np.nan)


def _wsd(x, W):
    total = W.sum(1)
    with np.errstate(invalid='ignore', divide='ignore'):
        mean = (W @ x) / total
        var = (W * (x[None, :] - mean[:, None]) ** 2).sum(1) / (total - 1)
    return np.where(total > 1, np.sqrt(var), np.nan)


def _wr2(x, y, W):
    total = W.sum(1)
    with np.errstate(invalid='ignore', divide='ignore'):
        mx, my = (W @ x) / total, (W @ y) / total
        dx, dy = x[None, :] - mx[:, None], y[None, :] - my[:, None]
        sxy, sxx, syy = (W * dx * dy).sum(1), (W * dx * dx).sum(1), (W * dy * dy).sum(1)
        r2 = sxy * sxy / (sxx * syy)
    return np.where((sxx > 0) & (syy > 0), r2, np.nan)


def weighted_metrics(arrays, W):
    """Every metric in BOOT_METRICS for each row of the weight matrix W (replicates x ramps)."""
    W = np.asarray(W, float)
    total = W.sum(1)
    p10, p50, p90 = (weighted_percentile(arrays['fill'], W, q) for q in (10, 50, 90))
    with np.errstate(invalid='ignore', divide='ignore'):
        return {
            'fill_p10': p10, 'fill_p50': p50, 'fill_p90': p90,
            'fill_log_sd': _wsd(arrays['log_fill'], W),
            'fill_p90_over_p10': p90 / p10,
            'frac_clearing_too_tight': (W @ arrays['clears']) / total,
            'containment': (W @ arrays['contained']) / total,
            'window_deg_p10': weighted_percentile(arrays['window_deg'], W, 10),
            'window_deg_p50': weighted_percentile(arrays['window_deg'], W, 50),
            'window_deg_p90': weighted_percentile(arrays['window_deg'], W, 90),
            'stored_width_p50': weighted_percentile(arrays['stored'], W, 50),
            'vfill_p50': weighted_percentile(arrays['vfill'], W, 50),
            'r2': _wr2(arrays['log_window'], arrays['log_apron'], W),
        }


def pano_bootstrap_weights(ramps, n_boot, seed):
    """Replicates x ramps draw counts: each replicate draws, city by city, as many panos as the city has,
    with replacement; every ramp of a drawn pano gets the pano's draw count."""
    rng = random.Random(seed)
    cols = {}
    for j, r in enumerate(ramps):
        cols.setdefault((r['city'], r['pano_id']), []).append(j)
    by_city = {}
    for city, pano_id in sorted(cols):
        by_city.setdefault(city, []).append(pano_id)
    W = np.zeros((n_boot, len(ramps)), dtype=np.int32)
    for b in range(n_boot):
        for city in sorted(by_city):
            panos = by_city[city]
            counts = [0] * len(panos)
            for _ in range(len(panos)):
                counts[rng.randrange(len(panos))] += 1
            for pano_id, count in zip(panos, counts):
                if count:
                    W[b, cols[(city, pano_id)]] = count
    return W


def nearest_rank_interval(values):
    """(2.5th, 97.5th) nearest-rank percentiles of the finite values, or (None, None)."""
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals:
        return None, None
    return pct(vals, 2.5), pct(vals, 97.5)


def _interval(values):
    lo, hi = nearest_rank_interval(values)
    return [num(lo) if lo is not None else None, num(hi) if hi is not None else None]


def _n_undefined(values):
    return int(sum(1 for v in values if not math.isfinite(float(v))))


# ---------------------------------------------------------------------------------------------------------
# Verdicts.

def paired_verdict(diff_point, interval, city_diffs, better, min_agree):
    """Criterion 2 for a difference `first - second`: 'first' when the interval excludes 0 in first's favour
    and at least `min_agree` city differences share that sign, 'second' for the mirror, else 'none'.
    `better` is 'lower' or 'higher'. A city difference of exactly 0 (or undefined) does not agree."""
    lo, hi = interval
    if lo is None or hi is None:
        return 'none'
    favours_first = (hi < 0) if better == 'lower' else (lo > 0)
    favours_second = (lo > 0) if better == 'lower' else (hi < 0)

    def agree(first):
        want_negative = (better == 'lower') == first
        return sum(1 for c in city_diffs if c is not None and ((c < 0) if want_negative else (c > 0)))

    if favours_first and agree(True) >= min_agree:
        return 'first'
    if favours_second and agree(False) >= min_agree:
        return 'second'
    return 'none'


def tilt_rule_verdict(per_metric):
    """Criterion 6 for one rule, from the per-metric verdicts of S-grav (first) against S-rig (second)."""
    verdicts = set(per_metric.values())
    if 'first' in verdicts and 'second' not in verdicts:
        return 'g.y better'
    if 'second' in verdicts and 'first' not in verdicts:
        return 'r.y better'
    return 'not distinguishable'


# ---------------------------------------------------------------------------------------------------------
# Selection of a constant.

@functools.lru_cache(maxsize=None)
def _cut_width(family, k, size_y, pano_w, pano_h):
    """cut_box's width alone, which does not depend on the centre; cached, because every selection below
    re-cuts the same ramps at the same grid values."""
    return cut_box({'x': 0.0, 'y': size_y, 'pano_w': pano_w, 'pano_h': pano_h}, (family, k)).width


def _fills(ramps, family, k, size_ys=None):
    sizes = [r['y'] for r in ramps] if size_ys is None else size_ys
    return [r['box_w'] / _cut_width(family, k, s, r['pano_w'], r['pano_h']) for r, s in zip(ramps, sizes)]


def select_constant(ramps, family, grid, target, size_ys=None):
    """The grid value whose fill p50 is nearest `target`, the first on a tie (crop_sizing_v3's rule)."""
    p50s = [pct(_fills(ramps, family, k, size_ys), 50) for k in grid]
    return grid[min(range(len(grid)), key=lambda i: abs(p50s[i] - target))]


def v2_fill_p50(ramps, size_ys=None):
    return pct(_fills(ramps, 'v2', CropRunner.CROP_SIZE_SCALE, size_ys), 50)


def loco_constant(ramps, held_out, family, grid, criterion):
    """A constant chosen on every city but `held_out`: criterion 'match_v2' (v2 x2.5's fill p50 on the same
    ramps) or 'band' (0.36)."""
    others = [r for r in ramps if r['city'] != held_out]
    target = v2_fill_p50(others) if criterion == 'match_v2' else BAND_CENTRE_FILL
    return select_constant(others, family, grid, target)


def fill_matrix(ramps, family, grid, size_ys=None):
    return np.array([_fills(ramps, family, k, size_ys) for k in grid]).T


def replicate_p50s(F, W):
    """Each replicate's fill p50 at each grid value (replicates x grid)."""
    return np.stack([weighted_percentile(F[:, g], W, 50) for g in range(F.shape[1])], axis=1)


def replicate_selection(F, W, targets, grid, P=None):
    """The grid value each replicate selects: fill p50 nearest its target, first on a tie. `P` is
    replicate_p50s(F, W), passed in when several targets share it."""
    P = replicate_p50s(F, W) if P is None else P
    return np.asarray(grid)[np.argmin(np.abs(P - np.asarray(targets)[:, None]), axis=1)]


# ---------------------------------------------------------------------------------------------------------
# The isotonic ceiling, with weights.

def weighted_isotonic_r2(x, y, w):
    """crop_sizing_v3.depression_only_ceiling's isotonic R², for one replicate's draw counts `w`."""
    keep = w > 0
    x, y, w = x[keep], y[keep], w[keep].astype(float)
    if len(x) < 4:
        return float('nan')
    order = np.argsort(x, kind='stable')
    x, y, w = x[order], y[order], w[order]
    ybar = float((w * y).sum() / w.sum())
    ss_tot = float((w * (y - ybar) ** 2).sum())
    if ss_tot == 0:
        return float('nan')
    blocks = []                                   # [sum w*y, sum w, sum w*y*y]
    i, n = 0, len(x)
    xs, ys, ws = x.tolist(), y.tolist(), w.tolist()
    while i < n:
        j = i
        s = c = q = 0.0
        while j < n and xs[j] == xs[i]:
            s += ws[j] * ys[j]
            c += ws[j]
            q += ws[j] * ys[j] * ys[j]
            j += 1
        blocks.append([s, c, q])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s2, c2, q2 = blocks.pop()
            blocks[-1][0] += s2
            blocks[-1][1] += c2
            blocks[-1][2] += q2
        i = j
    sse = sum(q - s * s / c for s, c, q in blocks)
    return 1.0 - sse / ss_tot


# ---------------------------------------------------------------------------------------------------------
# The study's blocks.

def strata_masks(ramps):
    masks = {'pooled': np.ones(len(ramps), bool)}
    for city in CITIES:
        masks[city] = np.array([r['city'] == city for r in ramps])
    for provider in PROVIDERS:
        masks[provider] = np.array([r['provider'] == provider for r in ramps])
    return masks


def _subset(ramps, mask):
    return [r for r, m in zip(ramps, mask) if m]


def _sub_arrays(arrays, mask):
    return {k: v[mask] for k, v in arrays.items()}


def rules_block(ramps, W, boxes_by_rule):
    """Point estimates and intervals for every rule configuration and stratum."""
    masks = strata_masks(ramps)
    point, intervals, replicates = {}, {}, {}
    for name, spec in RULES.items():
        boxes = boxes_by_rule[name]
        arrays = ramp_arrays(ramps, boxes)
        point[name] = {'family': spec[0], 'constant': spec[1],
                       'shape': 'square' if spec[0] in SQUARE_FAMILIES else '3:2', 'strata': {}}
        intervals[name], replicates[name] = {}, {}
        for stratum, mask in masks.items():
            sub = _subset(ramps, mask)
            point[name]['strata'][stratum] = point_metrics(sub, [b for b, m in zip(boxes, mask) if m])
            reps = weighted_metrics(_sub_arrays(arrays, mask), W[:, mask])
            replicates[name][stratum] = reps
            intervals[name][stratum] = {k: _interval(v) for k, v in reps.items()}
            undefined = {k: _n_undefined(v) for k, v in reps.items() if _n_undefined(v)}
            if undefined:
                intervals[name][stratum]['undefined_replicates'] = undefined
    return point, intervals, replicates


def ceiling_block(ramps, W):
    masks = strata_masks(ramps)
    deps = np.array([csv3.depression(r) for r in ramps])
    log_apron = np.log([csv3.apron_deg(r) for r in ramps])
    point, intervals = {}, {}
    for stratum, mask in masks.items():
        point[stratum] = csv3.depression_only_ceiling(_subset(ramps, mask))
        reps = [weighted_isotonic_r2(deps[mask], log_apron[mask], W[b, mask]) for b in range(len(W))]
        intervals[stratum] = _interval(reps)
    return point, intervals


def paired_block(point, replicates, loco_folds):
    out = {}
    for name, first, second in PAIRS:
        metrics = {}
        for metric in list(DISCRIMINATING) + list(CHECKS):
            pooled = point[first]['strata']['pooled'][metric] - point[second]['strata']['pooled'][metric]
            reps = replicates[first]['pooled'][metric] - replicates[second]['pooled'][metric]
            cities = {c: num(point[first]['strata'][c][metric] - point[second]['strata'][c][metric])
                      for c in CITIES}
            entry = {'pooled': num(pooled), 'interval': _interval(reps), 'cities': cities}
            if metric in DISCRIMINATING:
                better = DISCRIMINATING[metric]
                entry['better'] = better
                entry['verdict'] = paired_verdict(pooled, entry['interval'], list(cities.values()), better, 3)
                folds = loco_folds[name][metric]
                entry['out_of_sample'] = {
                    'folds': folds,
                    'holds': all(d is not None and d != 0 and (d > 0) == (pooled > 0) for d in folds.values())}
            else:
                entry['ranks_nothing'] = True
            metrics[metric] = entry
        out[name] = {'first': first, 'second': second, 'metrics': metrics}
    return out


LOCO_RULES = (('v2', 'band', SCALE_GRID), ('v3', 'match_v2', WIDE_WIDTH_GRID), ('v3', 'band', WIDE_WIDTH_GRID),
              ('A', 'match_v2', SCALE_GRID), ('A', 'band', SCALE_GRID))


def loco_block(ramps, W):
    """Leave one city out: the constant chosen on the other three, and the held-out city's clearing and fill
    p50 at it, each with an interval from resampling the held-out city's panos."""
    out = {}
    pair_constants = {}
    for held in CITIES:
        mask = np.array([r['city'] == held for r in ramps])
        held_ramps = _subset(ramps, mask)
        rows = {}

        def scored(spec, label, constant, cross_validated=True):
            boxes = [cut_box(r, spec) for r in held_ramps]
            arrays = ramp_arrays(held_ramps, boxes)
            pm = point_metrics(held_ramps, boxes)
            reps = weighted_metrics(arrays, W[:, mask])
            clearing_iv = _interval(reps['frac_clearing_too_tight'])
            rows[label] = {'family': spec[0], 'constant': constant, 'cross_validated': cross_validated,
                           'clearing': pm['frac_clearing_too_tight'], 'clearing_interval': clearing_iv,
                           'misses_floor': clearing_iv[1] is not None and clearing_iv[1] < CLEARING_FLOOR,
                           'fill_p50': pm['fill_p50'], 'fill_p50_interval': _interval(reps['fill_p50']),
                           'in_band': BAND[0] <= pm['fill_p50'] <= BAND[1],
                           'fill_log_sd': pm['fill_log_sd'], 'fill_p90_over_p10': pm['fill_p90_over_p10'],
                           'r2': pm['r2']}
            return pm

        pm_v2 = scored(('v2', CropRunner.CROP_SIZE_SCALE), 'v2_x2.5_fixed', CropRunner.CROP_SIZE_SCALE,
                       cross_validated=False)
        chosen = {}
        for family, criterion, grid in LOCO_RULES:
            k = loco_constant(ramps, held, family, grid, criterion)
            chosen[(family, criterion)] = k
            scored((family, k), '%s_%s' % (family, criterion), k)
        out[held] = {'chosen_on': [c for c in CITIES if c != held], 'rows': rows}
        pair_constants[held] = {'v2': pm_v2, 'v3': rows['v3_match_v2'], 'A': rows['A_match_v2']}
    # The held-out city's paired differences, at the constants chosen on the other three under 'match_v2'.
    folds = {}
    for name, first, second in PAIRS:
        fam = {'v2_x2.5': 'v2', 'v3_5.8m': 'v3', 'A_x2.2': 'A'}
        folds[name] = {m: {} for m in DISCRIMINATING}
        for held in CITIES:
            a, b = pair_constants[held][fam[first]], pair_constants[held][fam[second]]
            for m in DISCRIMINATING:
                folds[name][m][held] = num(a[m] - b[m]) if a[m] is not None and b[m] is not None else None
    return out, folds


def selection_bootstrap_block(ramps, W):
    """The pooled V3_CONTEXT_WIDTH_M each replicate selects, under both criteria, on two grids."""
    v2_fills = np.array(_fills(ramps, 'v2', CropRunner.CROP_SIZE_SCALE))
    targets_v2 = weighted_percentile(v2_fills, W, 50)
    targets_band = np.full(len(W), BAND_CENTRE_FILL)
    out = {}
    for grid_name, grid in (('v3_study_grid', csv3.GRID_M), ('wide_grid', WIDE_WIDTH_GRID)):
        F = fill_matrix(ramps, 'v3', grid)
        P = replicate_p50s(F, W)
        block = {'grid_min_m': grid[0], 'grid_max_m': grid[-1], 'grid_step_m': 0.1}
        for criterion, targets in (('match_v2', targets_v2), ('band', targets_band)):
            sel = replicate_selection(F, W, targets, grid, P)
            vals = sel.tolist()
            block[criterion] = {
                'point': select_constant(ramps, 'v3', grid, v2_fill_p50(ramps) if criterion == 'match_v2'
                                         else BAND_CENTRE_FILL),
                'p2_5': pct(vals, 2.5), 'p50': pct(vals, 50), 'p97_5': pct(vals, 97.5),
                'share_5_8': num(float(np.mean(np.isclose(sel, 5.8)))),
                'share_6_0': num(float(np.mean(np.isclose(sel, 6.0))))}
        out[grid_name] = block
    return out


def d6_block(ramps, W):
    """Per-provider (then per-city) v3 width: band-centre criterion, and v2-matched beside it."""
    grid = WIDE_WIDTH_GRID
    F = fill_matrix(ramps, 'v3', grid)
    v2_fills = np.array(_fills(ramps, 'v2', CropRunner.CROP_SIZE_SCALE))
    groups = {'gsv': np.array([r['provider'] == 'gsv' for r in ramps]),
              'mapillary': np.array([r['provider'] == 'mapillary' for r in ramps])}
    for city in CITIES:
        groups[city] = np.array([r['city'] == city for r in ramps])
    out = {'grid_min_m': grid[0], 'grid_max_m': grid[-1]}
    p50s = {name: replicate_p50s(F[mask], W[:, mask]) for name, mask in groups.items()}
    for criterion in ('band', 'match_v2'):
        sel, point = {}, {}
        for name, mask in groups.items():
            Wm = W[:, mask]
            targets = (np.full(len(W), BAND_CENTRE_FILL) if criterion == 'band'
                       else weighted_percentile(v2_fills[mask], Wm, 50))
            sel[name] = replicate_selection(F[mask], Wm, targets, grid, p50s[name])
            sub = _subset(ramps, mask)
            point[name] = select_constant(sub, 'v3', grid, BAND_CENTRE_FILL if criterion == 'band'
                                          else v2_fill_p50(sub))
        block = {'widths': point, 'width_intervals': {k: _interval(v) for k, v in sel.items()}}
        for label, a, b in (('mapillary_minus_gsv', 'mapillary', 'gsv'),
                            ('annapolis_minus_richmond', 'annapolis', 'richmond')):
            # Rounded to the grid's 0.1 m, so a difference of two grid values reads as one.
            iv = _interval(np.round(sel[a] - sel[b], 1))
            block[label] = {'point': num(round(point[a] - point[b], 1)), 'interval': iv,
                            'differs': iv[0] is not None and (iv[0] > 0 or iv[1] < 0)}
        out[criterion] = block
    out['per_city_supported'] = bool(out['band']['mapillary_minus_gsv']['differs']
                                     and out['band']['annapolis_minus_richmond']['differs'])
    return out


def shape_block(ramps):
    aspects = [r['box_w'] / r['box_h'] for r in ramps if r['box_h'] > 0]
    by_city = {c: num(pct([r['box_w'] / r['box_h'] for r in ramps if r['city'] == c], 50)) for c in CITIES}
    return {'apron_aspect_w_over_h_p50': num(pct(aspects, 50)), 'apron_aspect_by_city_p50': by_city,
            'window_aspect_3_2': CropRunner.CROP_ASPECT_W_OVER_H, 'window_aspect_square': 1.0}


# ---------------------------------------------------------------------------------------------------------
# The tilt arm.

def _metric_reps(arrays, W, metric):
    if metric == 'fill_log_sd':
        return _wsd(arrays['log_fill'], W)
    if metric == 'r2':
        return _wr2(arrays['log_window'], arrays['log_apron'], W)
    return weighted_metrics(arrays, W)[metric]


def compare_configs(ramps, W, first_boxes, second_boxes, metrics=TILT_METRICS, min_agree=2):
    """first minus second, pooled over `ramps` and per city, with intervals and criterion-2 verdicts."""
    cities = sorted({r['city'] for r in ramps})
    a1, a2 = ramp_arrays(ramps, first_boxes), ramp_arrays(ramps, second_boxes)
    p1, p2 = point_metrics(ramps, first_boxes), point_metrics(ramps, second_boxes)
    out = {}
    for metric in metrics:
        better = DISCRIMINATING.get(metric, 'higher')
        reps = _metric_reps(a1, W, metric) - _metric_reps(a2, W, metric)
        city_d = {}
        for c in cities:
            m = [r['city'] == c for r in ramps]
            sub = _subset(ramps, m)
            q1 = point_metrics(sub, [b for b, k in zip(first_boxes, m) if k])[metric]
            q2 = point_metrics(sub, [b for b, k in zip(second_boxes, m) if k])[metric]
            city_d[c] = num(q1 - q2)
        iv = _interval(reps)
        out[metric] = {'pooled': num(p1[metric] - p2[metric]), 'interval': iv, 'cities': city_d, 'better': better,
                       'verdict': paired_verdict(p1[metric] - p2[metric], iv, list(city_d.values()), better,
                                                 min_agree)}
    return out


def power_simulation(ramps, spec, sigma_by_city, multipliers=POWER_MULTIPLIERS, n_sims=POWER_SIMS,
                     n_boot=POWER_BOOT, seed=POWER_SEED, beta=BETA_PRIMARY, fill0=POWER_FILL0):
    """Power of criterion 6's test (S-grav against S-rig), on the ramps' real points, poses and pano sizes with
    synthetic aprons: window width at g_m.y times fill0 times exp(sigma_city z). Reads no apron field."""
    noise = random.Random(seed)
    W = pano_bootstrap_weights(ramps, n_boot, seed + 1).astype(float)
    cities = sorted({r['city'] for r in ramps})
    city_masks = {c: np.array([r['city'] == c for r in ramps]) for c in cities}
    sigma = np.array([sigma_by_city[r['city']] for r in ramps])
    pano_w = np.array([r['pano_w'] for r in ramps], float)
    rig_w = np.array([cut_box(r, spec).width for r in ramps], float)
    rows = []
    for m in multipliers:
        sizes = config_size_ys(ramps, 'S-grav', beta, multiplier=m) if m != 0 else [r['y'] for r in ramps]
        grav_w = np.array([cut_box(r, spec, size_y=s).width for r, s in zip(ramps, sizes)], float)
        hits = 0
        for _ in range(n_sims):
            z = np.array([noise.gauss(0.0, 1.0) for _ in ramps])
            apron = grav_w * fill0 * np.exp(sigma * z)
            log_apron = np.log(apron / pano_w * 360.0)
            verdicts = {}
            for metric in TILT_METRICS:
                def stat(width, w_matrix):
                    if metric == 'fill_log_sd':
                        return _wsd(np.log(apron / width), w_matrix)
                    return _wr2(np.log(width / pano_w * 360.0), log_apron, w_matrix)
                ones = np.ones((1, len(ramps)))
                point = float(stat(grav_w, ones)[0] - stat(rig_w, ones)[0])
                reps = stat(grav_w, W) - stat(rig_w, W)
                city_d = []
                for c in cities:
                    mask = city_masks[c].astype(float)[None, :]
                    city_d.append(float(stat(grav_w, mask)[0] - stat(rig_w, mask)[0]))
                verdicts[metric] = paired_verdict(point, nearest_rank_interval(reps), city_d,
                                                  DISCRIMINATING[metric], len(cities))
            hits += tilt_rule_verdict(verdicts) == 'g.y better'
        rows.append({'multiplier': m, 'power': num(hits / n_sims), 'n_sims': n_sims})
    reached = [row['multiplier'] for row in rows if row['power'] >= POWER_TARGET]
    return {'rows': rows, 'mde_multiplier': min(reached) if reached else None, 'target_power': POWER_TARGET,
            'n_boot': n_boot, 'seed': seed, 'beta': beta, 'fill0': fill0,
            'sigma_by_city': {c: sigma_by_city[c] for c in cities}, 'n_ramps': len(ramps),
            'n_panos': len({(r['city'], r['pano_id']) for r in ramps})}


def committed_sigma(rule_family, v3_json=os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-26-crop-sizing-v3.json')):
    with open(v3_json, encoding='utf-8') as f:
        v3 = json.load(f)
    return {c: v3['cities'][c][rule_family]['fill_log_sd'] for c in GSV_CITIES}


def power_block(gsv_ramps):
    """The power simulation for both tilt rules. Strips every apron field first, so it cannot read one."""
    blind = [{k: v for k, v in r.items() if k not in ('box_w', 'box_h', 'box_cx', 'box_cy', 'norm')}
             for r in gsv_ramps]
    return {name: power_simulation(blind, spec, committed_sigma(name)) for name, spec in TILT_RULES.items()}


def tilt_block(gsv_ramps, W_gsv):
    shifts = {}
    for city in GSV_CITIES:
        sub = [r for r in gsv_ramps if r['city'] == city]
        dy = [abs(CropRunner.elevation_px_to_deg(g - r['y'], r['pano_h']))
              for r, g in zip(sub, config_size_ys(sub, 'S-grav'))]
        shifts[city] = {'n': len(sub), 'abs_dy_deg_p50': num(pct(dy, 50)), 'abs_dy_deg_p90': num(pct(dy, 90)),
                        'abs_dy_deg_max': num(max(dy))}
    out = {'n_ramps': len(gsv_ramps), 'n_panos': len({r['pano_id'] for r in gsv_ramps}),
           'beta': BETA_PRIMARY, 'shift': shifts, 'rules': {}}
    deps_r = np.array([csv3.depression(r) for r in gsv_ramps])
    deps_g = np.array([CropRunner.label_depression_deg(s, r['pano_h'])
                       for r, s in zip(gsv_ramps, config_size_ys(gsv_ramps, 'S-grav'))])
    log_apron = np.log([csv3.apron_deg(r) for r in gsv_ramps])
    ones = np.ones(len(gsv_ramps))
    out['isotonic_ceiling'] = {'at_r_y': num(weighted_isotonic_r2(deps_r, log_apron, ones)),
                               'at_g_y': num(weighted_isotonic_r2(deps_g, log_apron, ones))}
    for name, spec in TILT_RULES.items():
        boxes = {cfg: tilt_boxes(gsv_ramps, spec, cfg) for cfg in TILT_CONFIGS}
        configs = {}
        for cfg, bx in boxes.items():
            pm = point_metrics(gsv_ramps, bx)
            configs[cfg] = {k: pm[k] for k in ('n', 'fill_p50', 'fill_log_sd', 'fill_p90_over_p10', 'r2',
                                                'frac_clearing_too_tight', 'containment', 'window_deg_p50')}
        main = compare_configs(gsv_ramps, W_gsv, boxes['S-grav'], boxes['S-rig'])
        control = compare_configs(gsv_ramps, W_gsv, boxes['S-grav'], boxes['S-neg'])
        neg_vs_rig = compare_configs(gsv_ramps, W_gsv, boxes['S-neg'], boxes['S-rig'])
        noise = compare_configs(gsv_ramps, W_gsv, boxes['S-grav-noise'], boxes['S-rig'])
        off = compare_configs(gsv_ramps, W_gsv, boxes['Off'], boxes['S-grav'], metrics=('containment',))
        beta_rows = {}
        for beta in BETA_SENSITIVITY:
            grav_b = tilt_boxes(gsv_ramps, spec, 'S-grav', beta=beta)
            beta_rows['%.2f' % beta] = compare_configs(gsv_ramps, W_gsv, grav_b, boxes['S-rig'])
        ratio = [g.width / r.width for g, r in zip(boxes['S-grav'], boxes['S-rig'])]
        out['rules'][name] = {
            'spec': list(spec), 'configs': configs,
            'grav_minus_rig': main,
            'verdict': tilt_rule_verdict({m: main[m]['verdict'] for m in TILT_METRICS}),
            'grav_minus_neg': control,
            'control_verdict': tilt_rule_verdict({m: control[m]['verdict'] for m in TILT_METRICS}),
            'neg_minus_rig': neg_vs_rig,
            'noise_minus_rig': noise,
            'off_minus_grav_containment': dict(off['containment'], model_conditional=True),
            'beta_sensitivity_grav_minus_rig': beta_rows,
            'share_window_moves_over_10pct': num(sum(1 for q in ratio if abs(q - 1) > 0.10) / len(ratio)),
        }
    return out


def selection_at_gy_block(ramps):
    """V3_CONTEXT_WIDTH_M re-selected with the GSV ramps sized at g.y (beta 0.95), Mapillary unchanged."""
    sizes = [modelled_stored_point(r)[1] if r['provider'] == 'gsv' else r['y'] for r in ramps]
    target = v2_fill_p50(ramps, sizes)
    return {'target_fill_p50': num(target),
            'matched_context_width_m': select_constant(ramps, 'v3', csv3.GRID_M, target, sizes),
            'band_centre_width_m': select_constant(ramps, 'v3', csv3.GRID_M, BAND_CENTRE_FILL, sizes),
            'at_r_y': {'matched_context_width_m': select_constant(ramps, 'v3', csv3.GRID_M, v2_fill_p50(ramps)),
                       'band_centre_width_m': select_constant(ramps, 'v3', csv3.GRID_M, BAND_CENTRE_FILL)}}


# ---------------------------------------------------------------------------------------------------------
# Descriptive blocks with no gold.

def label_type_block(path=CLAMP_CENSUS_JSON):
    with open(path, encoding='utf-8') as f:
        census = json.load(f)
    h = CropRunner.V1_REF_HEIGHT
    rules = ('v2_x2.5', 'v2_x2.0', 'v2_x3.0', 'v3_5.8m', 'v3_6.0m', 'A_x2.2')
    out = {}
    for label_type, block in sorted(census['by_label_type'].items()):
        rows = []
        for q in ('p10', 'p50', 'p90', 'p99'):
            dep = block['depression_deg'][q]
            y = h / 2 + CropRunner.elevation_deg_to_px(dep, h)
            rows.append(dict({'percentile': q, 'depression_deg': num(dep)},
                             **{rule: num(rule_fov_deg(RULES[rule], y, h)) for rule in rules}))
        out[label_type] = {'n': block['n'], 'rows': rows}
    return {'source': os.path.relpath(path, REPO_ROOT).replace(os.sep, '/'), 'label_types': out}


def camera_height_block():
    """The four heights, and the identification limit: under v3 the window depends on W / h only, so on
    this gold a per-city width and a per-provider camera height are one degree of freedom."""
    h0 = CropRunner.V3_CAMERA_HEIGHT_M
    w0 = CropRunner.V3_CONTEXT_WIDTH_M
    worst = 0.0
    for h in (2.21, 2.5, 1.7):
        for i in range(0, 600):
            dep = i / 10.0
            a = CropRunner.geometric_window_fov_deg(CropRunner.blend_distance_m(dep, camera_height_m=h0),
                                                    context_width_m=w0)
            b = CropRunner.geometric_window_fov_deg(CropRunner.blend_distance_m(dep, camera_height_m=h),
                                                    context_width_m=w0 * h / h0)
            worst = max(worst, abs(a - b))
    return {'heights': [dict(c) for c in CAMERA_HEIGHTS],
            'w_over_h_identity_max_abs_deg': num(worst),
            'w_over_h_identity_checked': 'v3 window at (5.8 m, %.4f m) against (5.8 h / %.4f, h) for h in '
                                         '2.21, 2.5, 1.7 m, depressions 0-59.9 deg' % (h0, h0)}


# ---------------------------------------------------------------------------------------------------------
# Per-ramp rows, and the summary.

def per_ramp_rows(ramps, boxes_by_rule, gsv_ramps, tilt_boxes_by_rule):
    rows = []
    for name in RULES:
        for r, b in zip(ramps, boxes_by_rule[name]):
            rows.append((r, name, 'gold_point', '', b))
    for (name, cfg, beta), boxes in tilt_boxes_by_rule.items():
        for r, b in zip(gsv_ramps, boxes):
            rows.append((r, name, cfg, beta, b))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(['city', 'pano_id', 'key', 'rule', 'config', 'beta', 'window_left', 'window_top',
                     'window_width', 'window_height', 'fill', 'vfill', 'contained'])
    for r, name, cfg, beta, b in rows:
        writer.writerow([r['city'], r['pano_id'], r['key'], name, cfg, beta, b.left, b.top, b.width, b.height,
                         '%.6g' % (r['box_w'] / b.width), '%.6g' % (r['box_h'] / b.height),
                         int(bool(csv2.box_inside_window(r, b.left, b.top, b.width, b.height)))])
    return out.getvalue().encode('utf-8'), len(rows)


def gzip_bytes(raw):
    """gzip with no name and no timestamp. The compressed bytes can still differ between zlib builds, so the
    artifact records the sha256 of the uncompressed CSV."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=buf, mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


GOLD_KEYS = ('gold', 'reproduction', 'rules', 'rule_intervals', 'ceiling', 'ceiling_intervals', 'paired',
             'loco', 'selection_bootstrap', 'd6', 'shape', 'selection_at_gy')
GSV_KEYS = ('tilt', 'power')
NO_POSE_KEYS = ('no_pose',)
CENSUS_KEYS = ('label_types',)
NO_POPULATION_KEYS = ('constants', 'camera_heights')


def populations(summary):
    gold = summary.get('gold') or {}
    return {
        'gold': {'name': 'boxed gold ramps of the four RampNet benchmark bundles (gold.csv, status boxed)',
                 'n': gold.get('n_boxed'), 'covers': [k for k in GOLD_KEYS if k in summary],
                 'subsets': {'strata': 'pooled, each city, each provider; each block names its own n'}},
        'gold_gsv': {'name': 'the GSV part of the gold (Sao Paulo and Paterson), the only ramps with a pose',
                     'n': (summary.get('tilt') or {}).get('n_ramps'), 'covers': [k for k in GSV_KEYS if k in summary],
                     'note': 'power reads these ramps\' points, poses and pano sizes, never their aprons'},
        'mapillary_no_pose': {'name': 'the Mapillary part of the gold (Richmond and Annapolis), no pose',
                              'n': (summary.get('no_pose') or {}).get('n'),
                              'covers': [k for k in NO_POSE_KEYS if k in summary]},
        'clamp_census': {'name': 'production labels in the 2026-08-09 clamp census, by label type',
                         'source': (summary.get('label_types') or {}).get('source'),
                         'covers': [k for k in CENSUS_KEYS if k in summary]},
        'no_population': [k for k in NO_POPULATION_KEYS if k in summary],
    }


def _canonical(path):
    absolute = os.path.abspath(path)
    try:
        inside = os.path.commonpath([absolute, REPO_ROOT]) == REPO_ROOT
    except ValueError:
        inside = False
    return os.path.relpath(absolute, REPO_ROOT).replace(os.sep, '/') if inside else '<outside the repo>'


def build_summary(ramps, rows, n_boot=N_BOOT, power=None):
    pooled_v2 = v2_fill_p50(ramps)
    W = pano_bootstrap_weights(ramps, n_boot, BOOT_SEED)
    boxes_by_rule = {name: [cut_box(r, spec) for r in ramps] for name, spec in RULES.items()}
    point, intervals, replicates = rules_block(ramps, W, boxes_by_rule)
    ceiling, ceiling_iv = ceiling_block(ramps, W)
    loco, folds = loco_block(ramps, W)
    gsv_mask = np.array([r['provider'] == 'gsv' for r in ramps])
    gsv_ramps = _subset(ramps, gsv_mask)
    kinds = {}
    for r in ramps:
        kinds[r['kind']] = kinds.get(r['kind'], 0) + 1
    summary = {
        'constants': {'too_tight_fill': TOO_TIGHT_FILL, 'band': list(BAND), 'band_centre_fill': BAND_CENTRE_FILL,
                      'clearing_floor': CLEARING_FLOOR, 'n_boot': n_boot, 'boot_seed': BOOT_SEED,
                      'power_seed': POWER_SEED, 'noise_seed': NOISE_SEED, 'click_noise_deg': CLICK_NOISE_DEG,
                      'beta_primary': BETA_PRIMARY, 'beta_sensitivity': list(BETA_SENSITIVITY),
                      'rules': {k: list(v) for k, v in RULES.items()},
                      'wide_width_grid': [WIDE_WIDTH_GRID[0], WIDE_WIDTH_GRID[-1]],
                      'scale_grid': [SCALE_GRID[0], SCALE_GRID[-1]],
                      'v3_context_width_m': CropRunner.V3_CONTEXT_WIDTH_M,
                      'crop_size_scale': CropRunner.CROP_SIZE_SCALE},
        'gold': {'n_items': len(rows), 'n_boxed': len(ramps),
                 'n_cant': sum(1 for r in rows if r['status'] == 'cant'),
                 'by_city': {c: sum(1 for r in ramps if r['city'] == c) for c in CITIES},
                 'by_provider': {p: sum(1 for r in ramps if r['provider'] == p) for p in PROVIDERS},
                 'by_kind': dict(sorted(kinds.items())),
                 'panos_by_city': {c: len({r['pano_id'] for r in ramps if r['city'] == c}) for c in CITIES}},
        'reproduction': {'v2_pooled_fill_p50': num(pooled_v2),
                         'v3_matched_width_m': select_constant(ramps, 'v3', csv3.GRID_M, pooled_v2),
                         'v3_band_centre_width_m': select_constant(ramps, 'v3', csv3.GRID_M, BAND_CENTRE_FILL),
                         'option_a_matched_scale': select_constant(ramps, 'A', csv3.RULE_A_SCALE_GRID, pooled_v2)},
        'rules': point,
        'rule_intervals': intervals,
        'ceiling': ceiling,
        'ceiling_intervals': ceiling_iv,
        'paired': paired_block(point, replicates, folds),
        'loco': loco,
        'selection_bootstrap': selection_bootstrap_block(ramps, W),
        'd6': d6_block(ramps, W),
        'shape': shape_block(ramps),
        'selection_at_gy': selection_at_gy_block(ramps),
        'power': power if power is not None else power_block(gsv_ramps),
        'tilt': tilt_block(gsv_ramps, W[:, gsv_mask]),
        'no_pose': {'n': int((~gsv_mask).sum()), 'cities': sorted({r['city'] for r in ramps if r['provider'] != 'gsv'}),
                    'why': 'Mapillary records carry no pose; production files such a label as no_pose under '
                           '--tilt-correction'},
        'label_types': label_type_block(),
        'camera_heights': camera_height_block(),
    }
    summary['populations'] = populations(summary)
    return summary, boxes_by_rule, gsv_ramps


def tilt_boxes_for_rows(gsv_ramps):
    out = {}
    for name, spec in TILT_RULES.items():
        for cfg in TILT_CONFIGS:
            out[(name, cfg, BETA_PRIMARY)] = tilt_boxes(gsv_ramps, spec, cfg)
        for beta in BETA_SENSITIVITY:
            out[(name, 'S-grav', beta)] = tilt_boxes(gsv_ramps, spec, 'S-grav', beta=beta)
    return out


def input_hashes(data_dir):
    out = {}
    for name in (GOLD_CSV,) + tuple(EVAL_JSON % c for c in CITIES):
        with open(os.path.join(data_dir, name), 'rb') as f:
            out[name] = _sha256(f.read())
    return out


# ---------------------------------------------------------------------------------------------------------
# RampNet's square summaries (detection mode), for the reproduction check.

def _rn_quantile(values, q):
    vals = sorted(values)
    idx = q * (len(vals) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def _rn_box_px(ramp):
    _, _, cx, cy, w, h = ramp['norm']
    pw, ph = ramp['pano_w'], ramp['pano_h']
    x0, x1 = (cx - w / 2) * pw, (cx + w / 2) * pw
    y0, y1 = max(0.0, (cy - h / 2) * ph), min(float(ph), (cy + h / 2) * ph)
    return x0, y0, x1, y1


def _rn_margins(box_px, window, pano_w):
    x0, y0, x1, y1 = box_px
    left, top, size, _ = window
    rel = ((x0 - left) + pano_w / 2) % pano_w - pano_w / 2
    return rel, size - (rel + (x1 - x0)), y0 - top, (top + size) - y1


def _rn_required_side(box_px, px, py, pano_w):
    x0, y0, x1, y1 = box_px
    dx0 = ((px - x0) + pano_w / 2) % pano_w - pano_w / 2
    dx1 = ((x1 - px) + pano_w / 2) % pano_w - pano_w / 2
    return 2.0 * max(dx0, dx1, py - y0, y1 - py)


def rampnet_rows(ramps, spec):
    """RampNet's detection-mode rows for a square rule: det items, the window centred on the point."""
    rows = []
    for r in ramps:
        if r['kind'] != 'det':
            continue
        pw, ph = r['pano_w'], r['pano_h']
        px, py = r['norm'][0] * pw, r['norm'][1] * ph
        raw = square_side(spec, py, pw, ph)
        window = square_window(px, py, raw, pw, ph)
        bpx = _rn_box_px(r)
        size = window[2]
        bw, bh = bpx[2] - bpx[0], bpx[3] - bpx[1]
        req = _rn_required_side(bpx, px, py, pw)
        rows.append({'raw_side': raw, 'px': px, 'py': py, 'pw': pw, 'ph': ph, 'box_px': bpx, 'size': size,
                     'contained': min(_rn_margins(bpx, window, pw)) / size >= 0.0,
                     'ctx': max(bw, bh) / size, 'ctx_h': bw / size, 'ctx_v': bh / size,
                     'size_ratio': size / req if req > 0 else float('nan')})
    return rows


def rampnet_summary(ramps, spec):
    rows = rampnet_rows(ramps, spec)
    ratio = [r['size_ratio'] for r in rows if not math.isnan(r['size_ratio'])]
    return {'n': len(rows), 'containment': sum(r['contained'] for r in rows) / len(rows),
            'predicted_side_p50': _rn_quantile([float(r['size']) for r in rows], 0.5),
            'context_ratio_p50': _rn_quantile([r['ctx'] for r in rows], 0.5),
            'context_ratio_h_p50': _rn_quantile([r['ctx_h'] for r in rows], 0.5),
            'context_ratio_v_p50': _rn_quantile([r['ctx_v'] for r in rows], 0.5),
            'size_ratio_p10': _rn_quantile(ratio, 0.1), 'size_ratio_p50': _rn_quantile(ratio, 0.5),
            'size_ratio_p90': _rn_quantile(ratio, 0.9)}


def rampnet_sweep_row(ramps, spec, k):
    rows = rampnet_rows(ramps, spec)
    contained = capped = 0
    ctx, sides = [], []
    for r in rows:
        window = square_window(r['px'], r['py'], k * r['raw_side'], r['pw'], r['ph'])
        if k * r['raw_side'] > min(r['pw'], r['ph']):
            capped += 1
        contained += 1 if min(_rn_margins(r['box_px'], window, r['pw'])) >= 0 else 0
        x0, y0, x1, y1 = r['box_px']
        ctx.append(max(x1 - x0, y1 - y0) / window[2])
        sides.append(float(window[2]))
    return {'k': k, 'n': len(rows), 'containment': contained / len(rows), 'ctx_p50': _rn_quantile(ctx, 0.5),
            'ctx_p90': _rn_quantile(ctx, 0.9), 'side_p50': _rn_quantile(sides, 0.5), 'capped': capped}


# ---------------------------------------------------------------------------------------------------------
# The report's tables, computed from the artifact alone. main() prints them (`tables`), and the report test
# asserts every line appears in the report, so no table cell is typed by hand.

CITY_NAMES = {'richmond': 'Richmond', 'sao_paulo': 'São Paulo', 'paterson': 'Paterson', 'annapolis': 'Annapolis',
              'pooled': '**pooled**', 'gsv': 'GSV', 'mapillary': 'Mapillary'}
RULE_NAMES = {'v2_x2.5': 'v2 ×2.5 (shipped)', 'v2_x2.0': 'v2 ×2.0', 'v2_x3.0': 'v2 ×3.0',
              'v3_5.8m': 'v3 5.8 m (shipped)', 'v3_6.0m': 'v3 6.0 m', 'A_x2.2': 'option A ×2.2', 'A_x2.5': 'option A ×2.5',
              'v1norm_x1.0_square': 'v1-norm ×1.0, square', 'v1norm_x3.5_square': 'v1-norm ×3.5, square',
              'geo_2.5m_square': 'geo-v1.5 2.5 m, square', 'geo_1.7m_square': 'geo-v1.5 1.7 m, square'}
METRIC_NAMES = {'fill_log_sd': 'fill log-sd', 'fill_p90_over_p10': 'fill p90/p10', 'r2': 'R²',
                'frac_clearing_too_tight': 'clearing', 'containment': 'containment'}
PAIR_NAMES = {'v3_minus_v2': 'v3 − v2', 'A_minus_v2': 'option A − v2', 'v3_minus_A': 'v3 − option A'}


def _f(x, spec='.3f'):
    return fmt(x, spec)


def _p(x):
    return fmt(None if x is None else 100 * x, '.1f') + '%'


def _iv(iv, spec='.3f', pct_=False):
    lo, hi = iv
    if pct_:
        return '[%s, %s]' % (_p(lo), _p(hi))
    return '[%s, %s]' % (fmt(lo, spec), fmt(hi, spec))


def _signed(x, spec='+.3f'):
    return fmt(x, spec).replace('-', '−')


def _verdict_words(verdict, first, second):
    return {'first': '%s better' % first, 'second': '%s better' % second}.get(verdict, 'not distinguishable')


def rules_table(summary):
    lines = ['| rule | fill p50 | fill log-sd [95%] | fill p90/p10 | R² [95%] | clearing [95%] | containment | '
             'window p50 | vertical fill p50 |', '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for name in RULES:
        m = summary['rules'][name]['strata']['pooled']
        iv = summary['rule_intervals'][name]['pooled']
        lines.append('| %s | %s | %s %s | %s | %s %s | %s %s | %s | %s° | %s |' % (
            RULE_NAMES[name], _f(m['fill_p50']), _f(m['fill_log_sd']), _iv(iv['fill_log_sd']),
            _f(m['fill_p90_over_p10'], '.2f'), _f(m['r2']), _iv(iv['r2']), _p(m['frac_clearing_too_tight']),
            _iv(iv['frac_clearing_too_tight'], pct_=True), _f(m['containment']), _f(m['window_deg_p50'], '.1f'),
            _f(m['vfill_p50'])))
    return lines


def strata_table(summary):
    lines = ['| stratum | n | fill log-sd v2 / v3 / A | R² v2 / v3 / A | clearing v2 / v3 / A | isotonic ceiling [95%] |',
             '|---|---:|---:|---:|---:|---:|']
    for stratum in ('pooled',) + CITIES + PROVIDERS:
        ms = [summary['rules'][r]['strata'][stratum] for r in ('v2_x2.5', 'v3_5.8m', 'A_x2.2')]
        lines.append('| %s | %d | %s | %s | %s | %s %s |' % (
            CITY_NAMES[stratum], ms[0]['n'], ' / '.join(_f(m['fill_log_sd']) for m in ms),
            ' / '.join(_f(m['r2']) for m in ms), ' / '.join(_p(m['frac_clearing_too_tight']) for m in ms),
            _f(summary['ceiling'][stratum]['isotonic_r2']), _iv(summary['ceiling_intervals'][stratum])))
    return lines


def paired_table(summary):
    lines = ['| comparison | metric | pooled difference [95%] | Richmond / São Paulo / Paterson / Annapolis | '
             'criterion 2 | sign holds in all four held-out folds |', '|---|---|---:|---:|---|---|']
    for name, pair in summary['paired'].items():
        first, second = PAIR_NAMES[name].split(' − ')
        for metric in DISCRIMINATING:
            e = pair['metrics'][metric]
            lines.append('| %s | %s | %s %s | %s | %s | %s |' % (
                PAIR_NAMES[name], METRIC_NAMES[metric], _signed(e['pooled'], '+.4f'),
                _iv(e['interval'], '+.4f').replace('-', '−'),
                ' / '.join(_signed(e['cities'][c]) for c in CITIES),
                _verdict_words(e['verdict'], first, second), 'yes' if e['out_of_sample']['holds'] else 'no'))
    return lines


def loco_table(summary):
    labels = {'v2_x2.5_fixed': 'v2 ×2.5 (not cross-validated)', 'v2_band': 'v2, band centre',
              'v3_match_v2': 'v3, match v2', 'v3_band': 'v3, band centre', 'A_match_v2': 'option A, match v2',
              'A_band': 'option A, band centre'}
    lines = ['| held out | rule, criterion | constant | clearing [95%] | misses the 45% floor | fill p50 [95%] | '
             'p50 in 0.28-0.44 |', '|---|---|---:|---:|---|---:|---|']
    for held in CITIES:
        rows = summary['loco'][held]['rows']
        for label in labels:
            r = rows[label]
            constant = '%.1f m' % r['constant'] if r['family'] == 'v3' else '×%s' % fmt(r['constant'], 'g')
            lines.append('| %s | %s | %s | %s %s | %s | %s %s | %s |' % (
                CITY_NAMES[held], labels[label], constant,
                _p(r['clearing']), _iv(r['clearing_interval'], pct_=True), 'yes' if r['misses_floor'] else 'no',
                _f(r['fill_p50']), _iv(r['fill_p50_interval']), 'yes' if r['in_band'] else 'no'))
    return lines


def selection_table(summary):
    lines = ['| criterion | selected | replicates 2.5% / 50% / 97.5% | replicates selecting 5.8 m | selecting 6.0 m |',
             '|---|---:|---:|---:|---:|']
    block = summary['selection_bootstrap']['v3_study_grid']
    for criterion, label in (('match_v2', "match v2's fill p50"), ('band', 'band centre (fill p50 nearest 0.36)')):
        b = block[criterion]
        lines.append('| %s | %.1f m | %.1f / %.1f / %.1f m | %s | %s |' % (
            label, b['point'], b['p2_5'], b['p50'], b['p97_5'], _p(b['share_5_8']), _p(b['share_6_0'])))
    return lines


def d6_table(summary):
    d6 = summary['d6']
    lines = ['| group | band-centre width [95%] | v2-matched width [95%] |', '|---|---:|---:|']
    for g in PROVIDERS + CITIES:
        lines.append('| %s | %.1f m %s | %.1f m %s |' % (
            CITY_NAMES[g], d6['band']['widths'][g], _iv(d6['band']['width_intervals'][g], '.1f'),
            d6['match_v2']['widths'][g], _iv(d6['match_v2']['width_intervals'][g], '.1f')))
    for label, name in (('mapillary_minus_gsv', 'Mapillary − GSV'), ('annapolis_minus_richmond', 'Annapolis − Richmond')):
        a, b = d6['band'][label], d6['match_v2'][label]
        lines.append('| %s | %s m %s | %s m %s |' % (
            name, _signed(a['point'], '+.1f'), _iv(a['interval'], '+.1f').replace('-', '−'),
            _signed(b['point'], '+.1f'), _iv(b['interval'], '+.1f').replace('-', '−')))
    return lines


def power_table(summary):
    mults = [r['multiplier'] for r in summary['power']['v2']['rows']]
    lines = ['| rule | %s | minimum detectable multiplier |' % ' | '.join('×%g' % m for m in mults),
             '|---|%s---:|' % ('---:|' * len(mults))]
    for name, block in summary['power'].items():
        lines.append('| %s | %s | ×%s |' % (name, ' | '.join(fmt(r['power'], '.3f') for r in block['rows']),
                                            fmt(block['mde_multiplier'], 'g')))
    return lines


def tilt_config_table(summary):
    lines = ['| rule | configuration | fill p50 | fill log-sd | fill p90/p10 | R² | clearing | containment | window p50 |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for name, block in summary['tilt']['rules'].items():
        for cfg in TILT_CONFIGS:
            m = block['configs'][cfg]
            lines.append('| %s | %s | %s | %s | %s | %s | %s | %s | %s° |' % (
                name, cfg, _f(m['fill_p50']), _f(m['fill_log_sd']), _f(m['fill_p90_over_p10'], '.2f'), _f(m['r2']),
                _p(m['frac_clearing_too_tight']), _f(m['containment']), _f(m['window_deg_p50'], '.1f')))
    return lines


def tilt_diff_table(summary):
    lines = ['| rule | difference | metric | pooled [95%] | Paterson | São Paulo | criterion 2 |',
             '|---|---|---|---:|---:|---:|---|']
    rows = (('grav_minus_rig', 'S-grav − S-rig', 'S-grav', 'S-rig'), ('grav_minus_neg', 'S-grav − S-neg', 'S-grav', 'S-neg'),
            ('neg_minus_rig', 'S-neg − S-rig', 'S-neg', 'S-rig'), ('noise_minus_rig', 'S-grav-noise − S-rig', 'S-grav-noise', 'S-rig'))
    for name, block in summary['tilt']['rules'].items():
        for key, label, first, second in rows:
            for metric in TILT_METRICS:
                e = block[key][metric]
                lines.append('| %s | %s | %s | %s %s | %s | %s | %s |' % (
                    name, label, METRIC_NAMES[metric], _signed(e['pooled'], '+.4f'),
                    _iv(e['interval'], '+.4f').replace('-', '−'), _signed(e['cities']['paterson']),
                    _signed(e['cities']['sao_paulo']), _verdict_words(e['verdict'], first, second)))
        for beta, d in block['beta_sensitivity_grav_minus_rig'].items():
            for metric in TILT_METRICS:
                e = d[metric]
                lines.append('| %s | S-grav − S-rig at beta %s | %s | %s %s | %s | %s | %s |' % (
                    name, beta, METRIC_NAMES[metric], _signed(e['pooled'], '+.4f'),
                    _iv(e['interval'], '+.4f').replace('-', '−'), _signed(e['cities']['paterson']),
                    _signed(e['cities']['sao_paulo']), _verdict_words(e['verdict'], 'S-grav', 'S-rig')))
    return lines


def label_type_table(summary):
    lines = ['| label type | n | depression p10 / p50 / p90 / p99 | v2 window | v3 5.8 m window | option A ×2.2 window |',
             '|---|---:|---:|---:|---:|---:|']
    for label_type, block in summary['label_types']['label_types'].items():
        rows = block['rows']
        lines.append('| %s | {:,} | %s | %s | %s | %s |'.format(block['n']) % (
            label_type, ' / '.join('%.1f°' % r['depression_deg'] for r in rows),
            ' / '.join('%.1f°' % r['v2_x2.5'] for r in rows), ' / '.join('%.1f°' % r['v3_5.8m'] for r in rows),
            ' / '.join('%.1f°' % r['A_x2.2'] for r in rows)))
    return lines


def camera_table(summary):
    lines = ['| height | value | where | what it is |', '|---|---:|---|---|']
    for c in summary['camera_heights']['heights']:
        lines.append('| %s | %s m | %s | %s |' % (c['name'], fmt(c['value_m'], 'g'), c['where'], c['what']))
    return lines


REPORT_TABLES = (rules_table, strata_table, paired_table, loco_table, selection_table, d6_table, power_table,
                 tilt_config_table, tilt_diff_table, label_type_table, camera_table)


# ---------------------------------------------------------------------------------------------------------
# Printing, from the summary alone.

def print_summary(summary):
    rep = summary['reproduction']
    print('gold: %d items, %d boxed; v2 pooled fill p50 %s; v3 matched %s m, band %s m; option A x%s'
          % (summary['gold']['n_items'], summary['gold']['n_boxed'], fmt(rep['v2_pooled_fill_p50'], '.3f'),
             rep['v3_matched_width_m'], rep['v3_band_centre_width_m'], rep['option_a_matched_scale']))
    for name, pair in summary['paired'].items():
        for metric, e in pair['metrics'].items():
            if 'verdict' in e:
                print('%-12s %-18s pooled %s [%s, %s] cities %s -> %s' % (
                    name, metric, fmt(e['pooled'], '+.4f'), fmt(e['interval'][0], '+.4f'),
                    fmt(e['interval'][1], '+.4f'),
                    ' '.join(fmt(v, '+.3f') for v in e['cities'].values()), e['verdict']))
    for name, block in summary['tilt']['rules'].items():
        print('tilt %s: %s (control: %s)' % (name, block['verdict'], block['control_verdict']))
    for name, block in summary['power'].items():
        print('power %s: %s; MDE multiplier %s' % (
            name, ', '.join('x%g %s' % (r['multiplier'], fmt(r['power'], '.2f')) for r in block['rows']),
            block['mde_multiplier']))


def print_power(power):
    for name, block in power.items():
        print('power %s: %s; MDE multiplier %s' % (
            name, ', '.join('x%g %s' % (r['multiplier'], fmt(r['power'], '.2f')) for r in block['rows']),
            block['mde_multiplier']))


def _write_json(path, obj):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(obj, f, indent=1, sort_keys=True, allow_nan=False)
        f.write('\n')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    ex = sub.add_parser('extract', help='copy the inputs out of RampNet and sidewalk-auto-labeler checkouts')
    ex.add_argument('--rampnet', required=True, help='a RampNet checkout (read with git show, never written)')
    ex.add_argument('--labeler', required=True, help='a sidewalk-auto-labeler checkout holding %s' % LABELER_COMMIT)
    ex.add_argument('--out', default=DATA_DIR)
    pw = sub.add_parser('power', help="the tilt arm's power simulation alone")
    pw.add_argument('--data', default=DATA_DIR)
    pw.add_argument('--write')
    st = sub.add_parser('study', help='the study, from the committed extract')
    st.add_argument('--data', default=DATA_DIR)
    st.add_argument('--write')
    st.add_argument('--per-ramp')
    st.add_argument('--boot', type=int, default=N_BOOT)
    tb = sub.add_parser('tables', help="print the report's tables from a committed summary")
    tb.add_argument('--summary', default=SUMMARY_JSON)
    args = parser.parse_args(argv)

    if args.command == 'tables':
        with open(args.summary, encoding='utf-8') as f:
            summary = json.load(f)
        for table in REPORT_TABLES:
            print('\n'.join(table(summary)) + '\n')
        return 0

    if args.command == 'extract':
        files = build_extract(args.rampnet, args.labeler)
        write_extract(files, args.out)
        print('wrote %d files to %s' % (len(files), _canonical(args.out)))
        return 0

    rows = read_gold(os.path.join(args.data, GOLD_CSV))
    ramps = boxed_ramps(rows)
    gsv_ramps = [r for r in ramps if r['provider'] == 'gsv']
    if args.command == 'power':
        power = power_block(gsv_ramps)
        print_power(power)
        if args.write:
            _write_json(args.write, {'power': power, 'criteria_commit': CRITERIA_COMMIT,
                                     'generated_by': 'python reports/scripts/crop_sizing_consolidation.py power '
                                                     '--write %s' % _canonical(args.write)})
            print('wrote %s' % _canonical(args.write))
        return 0

    summary, boxes_by_rule, gsv_ramps = build_summary(ramps, rows, n_boot=args.boot)
    summary['meta'] = {'criteria_file': CRITERIA_FILE, 'criteria_commit': CRITERIA_COMMIT,
                       'inputs_sha256': input_hashes(args.data),
                       'generated_by': 'python reports/scripts/crop_sizing_consolidation.py study%s%s%s' % (
                           ' --write %s' % _canonical(args.write) if args.write else '',
                           ' --per-ramp %s' % _canonical(args.per_ramp) if args.per_ramp else '',
                           ' --boot %d' % args.boot if args.boot != N_BOOT else '')}
    print_summary(summary)
    if args.per_ramp:
        raw, n = per_ramp_rows(ramps, boxes_by_rule, gsv_ramps, tilt_boxes_for_rows(gsv_ramps))
        with open(args.per_ramp, 'wb') as f:
            f.write(gzip_bytes(raw))
        summary['meta']['per_ramp_rows'] = n
        summary['meta']['per_ramp_csv_sha256'] = _sha256(raw)
        print('wrote %s (%d rows)' % (_canonical(args.per_ramp), n))
    if args.write:
        _write_json(args.write, summary)
        print('wrote %s' % _canonical(args.write))
    return 0


if __name__ == '__main__':
    sys.exit(main())
