"""Bayonne end to end: the first Panoramax panos through CropRunner (#111, item 2).

`bayonne-fr` is the one Project Sidewalk city whose imagery is Panoramax. Nothing before this run had cut a crop
from a Panoramax pano: the provenance manifest (#111, PRs 249/250) was built and tested on synthetic rows. This
script does the two halves of the check that are not CropRunner's own runs:

    snapshot   GET /adminapi/panos and /adminapi/labels/cvMetadata once each, commit the response bytes
               gzipped (sha256 of the uncompressed bytes in snapshot.json), and record the platform.
    measure    read the scratch pano store, the crop stores and the run captures, and write the small text
               artifacts the report quotes: per-pano and per-crop tables, the manifest gzipped, the captures
               with their scratch paths replaced, and a summary JSON carrying the report's table rows.
               With --sheets it also lays the --mark-label crops out as contact sheets for the visual
               verdict. Imagery is read, never copied into the repo.

Without --write, `measure` prints the table rows and writes nothing under reports/ (the suite's conftest fails a
run that changes the repo, and so should a dry look).

    python reports/scripts/bayonne_crop_check.py snapshot --host sidewalk-bayonne.cs.washington.edu \\
        --out reports/data/2026-11-17-bayonne-panoramax-crop-check --plain <scratch>/out
    python reports/scripts/bayonne_crop_check.py measure --snapshot reports/data/2026-11-17-bayonne-panoramax-crop-check \\
        --store <scratch>/store/bayonne-fr --crops <scratch>/crops/bayonne-fr \\
        --tilt-crops <scratch>/crops-tilt/bayonne-fr --marked-crops <scratch>/crops-marked/bayonne-fr \\
        --runs <scratch>/out [--sheets <scratch>/sheets] [--write]

The two attribution columns of the manifest are read from `CropRunner.PROVENANCE_FIELDS[1:]` and never spelled
here; this script's own output calls them `attribution_1` / `attribution_2`.
"""

import argparse
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sys
import urllib.request
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (REPO_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PIL import Image, ImageDraw  # noqa: E402

import CropRunner  # noqa: E402
import non_gsv_levelling  # noqa: E402
from downloaders.common import walk_store_panos  # noqa: E402
from studyfmt import fmt, num, percentile  # noqa: E402

CITY = 'bayonne-fr'
DATA_NAME = '2026-11-17-bayonne-panoramax-crop-check'
DATA_DIR = os.path.join(REPO_ROOT, 'reports', 'data', DATA_NAME)
SUMMARY_PATH = os.path.join(REPO_ROOT, 'reports', 'data', DATA_NAME + '.json')
USER_AGENT = 'sidewalk-panorama-tools desk study (#111; https://github.com/ProjectSidewalk/sidewalk-panorama-tools)'

SOURCE_FIELD = CropRunner.PROVENANCE_FIELDS[0]
ATTRIBUTION_FIELDS = CropRunner.PROVENANCE_FIELDS[1:]

# The 2026-10-06 levelling report's three pose classes, under the short names the plan's verdict rule uses.
POSE_CLASS = {'pose-nonzero': 'posed', 'pose-zero': 'zero', 'pose-absent': 'none'}
POSE_CLASSES = ('posed', 'zero', 'none')

VERDICTS = ('on-mark', 'in-crop-off-mark', 'not-visible', 'unclear')
DIRECTIONS = ('up', 'down', 'left', 'right')
MIN_JUDGEABLE = 20

# CropRunner's stdout captures, as the plan's commands wrote them (each ends with an `exit=N` line), and the
# names they are committed under.
CAPTURES = (('download.stdout.txt', 'run-download.stdout.txt'),
            ('crop.stdout.txt', 'run-crop.stdout.txt'),
            ('crop-rerun.stdout.txt', 'run-crop-rerun.stdout.txt'),
            ('crop-tilt.stdout.txt', 'run-crop-tilt.stdout.txt'),
            ('crop-marked.stdout.txt', 'run-crop-marked.stdout.txt'))
RUN1_MANIFEST_COPY = 'crop_provenance.run1.csv'   # copied after run 1, before run 2 (M8)
NO_POSE_REASON = 'no .xml and no .depth.npz beside the pano'


# ------------------------------------------------------------------------------------------------ helpers

def label_uid(row):
    """`bayonne-fr:<label_id>`: label_id restarts in every city, so it is never quoted alone."""
    return '%s:%d' % (CITY, int(row['label_id']))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, 'rb') as f:
        return sha256_bytes(f.read())


def gzip_bytes(data):
    """Deterministic gzip (mtime 0), so re-running `measure` over the same files rewrites identical bytes."""
    return gzip.compress(data, mtime=0)


def read_gz_json(path):
    with gzip.open(path, 'rb') as f:
        return json.loads(f.read().decode('utf-8'))


def _text(value):
    return '' if value is None else str(value)


# ------------------------------------------------------------------------------------------------ snapshot

def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=600) as response:
        return response.read()


def streetlevel_imports():
    try:
        import streetlevel  # noqa: F401
        return True
    except Exception:
        return False


def snapshot(host, out_dir, plain_dir=None):
    """Two GETs; the bytes gzipped into `out_dir`, the uncompressed cvMetadata into `plain_dir` for `-f`."""
    os.makedirs(out_dir, exist_ok=True)
    taken_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    bodies = {'panos.json': fetch('https://%s/adminapi/panos' % host),
              'cvmetadata.json': fetch('https://%s/adminapi/labels/cvMetadata' % host)}
    panos = json.loads(bodies['panos.json'].decode('utf-8'))
    labels = json.loads(bodies['cvmetadata.json'].decode('utf-8'))
    for name, body in bodies.items():
        with open(os.path.join(out_dir, name + '.gz'), 'wb') as f:
            f.write(gzip_bytes(body))
    if plain_dir:
        os.makedirs(plain_dir, exist_ok=True)
        with open(os.path.join(plain_dir, 'cvmetadata.json'), 'wb') as f:
            f.write(bodies['cvmetadata.json'])
    record = {
        'taken_at_utc': taken_at, 'host': host, 'city': CITY,
        'counts': {'panos': len(panos), 'panos_labelled': sum(1 for p in panos if p.get('has_labels')),
                   'labels': len(labels), 'label_panos': len({r['pano_id'] for r in labels})},
        'sha256': {name: sha256_bytes(body) for name, body in bodies.items()},
        'platform': {'python': sys.version.split()[0], 'implementation': platform.python_implementation(),
                     'os': platform.platform(terse=True), 'streetlevel_imports': streetlevel_imports()},
    }
    _write_json(os.path.join(out_dir, 'snapshot.json'), record)
    return record


def _write_json(path, obj):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(obj, f, indent=2, sort_keys=True, allow_nan=False)
        f.write('\n')


# ---------------------------------------------------------------------------------------------- detectors

def pose_classes(panos):
    """{pano_id: 'posed' | 'zero' | 'none'}, by the 2026-10-06 report's own `pose_class`, not restated."""
    return {p['pano_id']: POSE_CLASS[non_gsv_levelling.pose_class(p)] for p in panos}


def dims_coverage(labels):
    """Which rows carry a frame the cropper's dims preflight can test (`CropRunner._metadata_dims`).

    A row without one is cut without the check, so `dims_mismatch` 0 says nothing about it; it is listed."""
    untested = []
    for row in labels:
        try:
            dims = CropRunner._metadata_dims(row)
        except ValueError:
            dims = None
        if dims is None:
            untested.append(label_uid(row))
    return {'usable': len(labels) - len(untested), 'total': len(labels), 'untested': untested}


def read_ledger(store):
    """pano_id_log.csv -> {pano_id: verdict}; header skipped, two- and three-field rows alike, last row wins."""
    out = {}
    path = os.path.join(store, 'pano_id_log.csv')
    if not os.path.exists(path):
        return out
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0] != 'pano_id':     # the header row, when the ledger has one
                out[row[0]] = row[1]
    return out


def _all_jpegs(store):
    for folder, _, files in os.walk(store):
        for name in files:
            if name.lower().endswith('.jpg'):
                yield os.path.relpath(os.path.join(folder, name), store).replace(os.sep, '/')


def store_check(store, panos, labels):
    """M1-M3 over the labelled panos: ledger verdicts, the shard layout, and the stored frame against the app's.

    `walk_store_panos` is the store's one definition of a panorama, and it skips a file outside its shard by
    design, so the misplaced check walks every .jpg itself and lists what the definition did not yield."""
    ledger = read_ledger(store)
    walked = {os.path.relpath(path, store).replace(os.sep, '/') for path in walk_store_panos(store)}
    walked_ids = {os.path.splitext(os.path.basename(p))[0] for p in walked}
    outside = sorted(p for p in _all_jpegs(store) if p not in walked)
    app = {p['pano_id']: (p.get('width'), p.get('height')) for p in panos}
    label_frames = {}
    for row in labels:
        label_frames.setdefault(row['pano_id'], set()).add((row.get('pano_width'), row.get('pano_height')))
    rows, mismatch, missing = [], [], []
    for pano_id in sorted(label_frames):
        path = os.path.join(store, pano_id[:2], pano_id + '.jpg')
        stored = (None, None)
        size = digest = None
        if os.path.isfile(path):
            with Image.open(path) as im:
                stored = im.size
            size, digest = os.path.getsize(path), sha256_file(path)
        else:
            missing.append(pano_id)
        frames = label_frames[pano_id]
        label_w, label_h = next(iter(frames)) if len(frames) == 1 else (None, None)
        if size is not None and (stored != app.get(pano_id) or frames != {stored}):
            mismatch.append(pano_id)
        rows.append({'pano_id': pano_id, 'shard': pano_id[:2], 'bytes': size, 'sha256': digest,
                     'stored_w': stored[0], 'stored_h': stored[1], 'app_w': app.get(pano_id, (None,))[0],
                     'app_h': app.get(pano_id, (None, None))[1], 'label_w': label_w, 'label_h': label_h,
                     'ledger': ledger.get(pano_id, '')})
    downloaded = {i for i, v in ledger.items() if v == '1'}
    return {'rows': rows, 'outside_shard': outside, 'frame_mismatch': mismatch, 'missing': missing,
            'walk_equals_ledger': walked_ids == downloaded, 'ledger_downloaded': len(downloaded),
            'ledger_verdicts': dict(Counter(ledger.values())),
            'labelled_not_downloaded': sorted(set(label_frames) - downloaded)}


# Rule v2, transcribed a second time from reports/2026-08-19-crop-sizing-v2.md and CropRunner's docstrings. It
# must NOT import or read CropRunner: its only job is to be an independent derivation of the window, so that
# M5(b) is two derivations agreeing rather than one derivation compared with itself. The arithmetic follows the
# cropper's order of operations, because a different order can move a .5 rounding by one pixel.
V2_REF_HEIGHT = 6656.0
V2_DIST_INTERCEPT = 19.80546390
V2_DIST_SLOPE = 0.01523952
V2_SIZE_COEF = 8725.6
V2_SIZE_EXP = -1.192
V2_SIZE_MIN = 50.0
V2_SIZE_MAX = 1500.0
V2_SCALE = 2.5
V2_MIN_DEG = 8.0
V2_MAX_DEG = 90.0
V2_ASPECT = 1.5


def standalone_v2_window(pano_x, pano_y, pano_width, pano_height):
    """(left, top, width, height, shifted) of the v2 window, from the published rule alone."""
    ref_offset = (pano_height / 2 - pano_y) * (V2_REF_HEIGHT / pano_height)
    distance = max(0.0, V2_DIST_INTERCEPT + V2_DIST_SLOPE * ref_offset)
    size = V2_SIZE_COEF * distance ** V2_SIZE_EXP if distance > 0 else 0.0
    if size > V2_SIZE_MAX or distance == 0:
        size = V2_SIZE_MAX
    if size < V2_SIZE_MIN:
        size = V2_SIZE_MIN
    native = size * (pano_height / V2_REF_HEIGHT)
    deg = min(max(native * V2_SCALE / pano_height * 180.0, V2_MIN_DEG), V2_MAX_DEG)
    width = int(round(min(deg / 360.0 * pano_width, pano_width, pano_height * V2_ASPECT)))
    height = int(round(width / V2_ASPECT))
    left = int(round(pano_x - width / 2)) % pano_width
    ideal_top = int(round(pano_y - height / 2))
    top = max(0, min(ideal_top, pano_height - height))
    return (left, top, width, height, top != ideal_top)


def cropper_box(row):
    """The window CropRunner cuts for this row (the loop reads pano_x/pano_y as floats)."""
    width, height = CropRunner._metadata_dims(row)
    x, y = float(row['pano_x']), float(row['pano_y'])
    return CropRunner.compute_crop_box(x, y, CropRunner.crop_window_width(y, width, height), width, height)


def expected_stored_size(box):
    """The stored file's size for a cut window: `downscale_for_storage` caps the width and never upscales."""
    cap = CropRunner.CROP_MAX_STORED_WIDTH
    if box.width <= cap:
        return box.width, box.height
    return cap, max(1, int(round(box.height * cap / box.width)))


def transcription_disagreements(labels):
    """label_uids whose standalone window differs from CropRunner's (M5(b))."""
    out = []
    for row in labels:
        width, height = CropRunner._metadata_dims(row)
        mine = standalone_v2_window(float(row['pano_x']), float(row['pano_y']), width, height)
        if mine != tuple(cropper_box(row)):
            out.append(label_uid(row))
    return out


def window_rows(labels, crops_store):
    """One row per label (crops.csv): the window, both derivations, and the crop file as found on disk."""
    disagree = set(transcription_disagreements(labels))
    rows = []
    for row in sorted(labels, key=lambda r: int(r['label_id'])):
        width, height = CropRunner._metadata_dims(row)
        box = cropper_box(row)
        mine = standalone_v2_window(float(row['pano_x']), float(row['pano_y']), width, height)
        type_id = CropRunner.resolve_label_type_id(row)
        path = os.path.join(crops_store, str(type_id), '%d.jpg' % int(row['label_id']))
        file_w = file_h = size = digest = None
        if os.path.isfile(path):
            with Image.open(path) as im:
                file_w, file_h = im.size
            size, digest = os.path.getsize(path), sha256_file(path)
        exp_w, exp_h = expected_stored_size(box)
        rows.append({
            'label_uid': label_uid(row), 'label_id': int(row['label_id']), 'pano_id': row['pano_id'],
            'label_type_id': type_id, 'pano_w': width, 'pano_h': height, 'pano_x': row['pano_x'],
            'pano_y': row['pano_y'],
            'fov_deg': round(CropRunner.crop_window_fov_deg(float(row['pano_y']), height), 4),
            'left': box.left, 'top': box.top, 'width': box.width, 'height': box.height,
            'crosses_seam': box.left + box.width > width, 'shifted': box.shifted,
            'downscaled': box.width > CropRunner.CROP_MAX_STORED_WIDTH,
            'expected_w': exp_w, 'expected_h': exp_h, 'standalone_w': mine[2], 'standalone_h': mine[3],
            'transcription_agrees': label_uid(row) not in disagree,
            'file_w': file_w, 'file_h': file_h, 'file_bytes': size, 'file_sha256': digest,
            'size_ok': (file_w, file_h) == (exp_w, exp_h)})
    uids = [r['label_uid'] for r in rows]
    assert len(uids) == len(set(uids)), 'label_uid is not unique'
    return rows


def manifest_check(manifest_rows, labels, city):
    """M7 and M9: the manifest against the served rows, joined on (city, label_id) inside the one city."""
    served = {}
    for row in labels:
        key = (city, int(row['label_id']))
        if key in served:
            raise ValueError('served rows are not unique on %s:%s' % key)
        served[key] = row
    keys = [(r['city'], int(r['label_id'])) for r in manifest_rows]
    mismatch, equal = [], 0
    for r, key in zip(manifest_rows, keys):
        s = served.get(key)
        fields = (SOURCE_FIELD,) + ATTRIBUTION_FIELDS
        if s is None or any(r.get(f, '') == '' or r.get(f) != _text(s.get(f)) for f in fields):
            mismatch.append('%s:%d' % key)
        else:
            equal += 1
    return {'rows': len(manifest_rows), 'unique': len(set(keys)) == len(keys),
            'city_ok': all(r['city'] == city for r in manifest_rows),
            'attribution_equal': equal, 'attribution_mismatch': mismatch,
            'attribution_pairs': len({tuple(r.get(f, '') for f in ATTRIBUTION_FIELDS) for r in manifest_rows}),
            'source_values': sorted({r.get(SOURCE_FIELD, '') for r in manifest_rows}),
            'rule_versions': sorted({r['crop_rule_version'] for r in manifest_rows}),
            'tilt_columns': sorted({tuple(r[c] for c in CropRunner.TILT_PROVENANCE_COLUMNS)
                                    for r in manifest_rows}),
            'not_in_snapshot': sorted('%s:%d' % k for k in set(keys) - set(served)),
            'not_in_manifest': sorted('%s:%d' % k for k in set(served) - set(keys))}


def visual_verdict(rows):
    """The plan's section 2.6 rule, fixed before the run.

    FAIL (systematic misregistration) if, over the judgeable crops (every verdict but `unclear`): none is
    `on-mark`; or `not-visible` is at least half; or `in-crop-off-mark` is at least half AND one direction holds
    at least three quarters of them AND the same direction holds at least three quarters of the off-mark crops
    on the `zero` and `none` pose classes (there being at least one). A consistent offset on `posed` panos alone
    is the rig tilt the 2026-10-06 report predicts: a finding, never a failure. Under MIN_JUDGEABLE judgeable
    crops the outcome is `inconclusive`."""
    for r in rows:
        if r['verdict'] not in VERDICTS:
            raise ValueError('%s: unknown verdict %r' % (r.get('label_uid'), r['verdict']))
        if r['verdict'] == 'in-crop-off-mark' and r.get('direction') not in DIRECTIONS:
            raise ValueError('%s: off-mark needs a direction, got %r' % (r.get('label_uid'), r.get('direction')))
    tallies = {v: sum(1 for r in rows if r['verdict'] == v) for v in VERDICTS}
    by_pose = {pc: {v: sum(1 for r in rows if r['pose_class'] == pc and r['verdict'] == v) for v in VERDICTS}
               for pc in POSE_CLASSES}
    off = [r for r in rows if r['verdict'] == 'in-crop-off-mark']
    directions = {d: sum(1 for r in off if r['direction'] == d) for d in DIRECTIONS}
    judgeable = len(rows) - tallies['unclear']
    reasons = []
    if judgeable < MIN_JUDGEABLE:
        outcome = 'inconclusive'
    else:
        if tallies['on-mark'] == 0:
            reasons.append('no crop is on-mark')
        if 2 * tallies['not-visible'] >= judgeable:
            reasons.append('not-visible is at least half of the judgeable crops')
        if off and 2 * len(off) >= judgeable:
            top, k = max(directions.items(), key=lambda item: item[1])
            unposed = [r for r in off if r['pose_class'] in ('zero', 'none')]
            k_unposed = sum(1 for r in unposed if r['direction'] == top)
            if 4 * k >= 3 * len(off) and unposed and 4 * k_unposed >= 3 * len(unposed):
                reasons.append('off-mark is at least half, and %d of %d lie %s, %d of %d on unposed panos'
                               % (k, len(off), top, k_unposed, len(unposed)))
        outcome = 'FAIL' if reasons else 'PASS'
    return {'outcome': outcome, 'reasons': reasons, 'judgeable': judgeable, 'tallies': tallies,
            'by_pose_class': by_pose, 'off_mark_directions': directions}


SUMMARY_LINE = re.compile(
    r'^(\d+) crops extracted, (\d+) already existed, (\d+) skipped because the panorama image was missing, '
    r'(\d+) skipped on a metadata/image dimension mismatch, (\d+) skipped for a label position outside the '
    r'image, (\d+) withheld for a mostly black window, (\d+) skipped for no pano pose, (\d+) errors, of (\d+) '
    r'labels total\.$', re.M)
SUMMARY_KEYS = ('success', 'skipped_existing', 'missing_pano', 'dims_mismatch', 'out_of_frame', 'black_content',
                'no_pose', 'errors', 'total')
EXIT_LINE = re.compile(r'^exit=(\d+)\s*$', re.M)


def parse_crop_summary(text):
    """The counts from CropRunner's last summary line in a capture; ValueError if there is none."""
    matches = SUMMARY_LINE.findall(text)
    if not matches:
        raise ValueError('no CropRunner summary line in the capture')
    return dict(zip(SUMMARY_KEYS, (int(v) for v in matches[-1])))


def parse_exit(text):
    """The `exit=N` line the plan's commands append to each capture."""
    matches = EXIT_LINE.findall(text)
    if not matches:
        raise ValueError('no exit= line in the capture')
    return int(matches[-1])


def _path_pattern(path):
    parts = [re.escape(p) for p in re.split(r'[\\/]+', path.rstrip('\\/')) if p]
    return re.compile(r'[\\/]+'.join(parts), re.I)


def scrub(text, replacements):
    """Replace each local path (either slash form, any case) with its token, longest path first."""
    for path, token in sorted(replacements, key=lambda item: -len(item[0])):
        text = _path_pattern(path).sub(lambda _m, t=token: t, text)
    return text


def pov_residuals(labels):
    """M11: how `pano_x` sits against the app's view record, per zoom.

    base = heading - camera_heading + 180 is where a GSV-style frame puts the canvas centre; the residual
    r = azimuth(pano_x) - base (wrapped) should then be a per-zoom gain times the click's offset from the canvas
    centre in canvas widths. Fitted through the origin per zoom; the fit's leftover says whether the records
    are self-consistent. It says nothing about which column of the served image is pano_x = 0.

    The served zoom is a float (0.998, 1.07, 1.99 ...). A row is binned at its nearest integer zoom z, and its
    offset scaled by 2^-(zoom - z), the view halving per zoom step, so each bin's gain is the gain at z."""
    by_zoom = {}
    for row in labels:
        try:
            values = [float(row[k]) for k in ('heading', 'camera_heading', 'canvas_x', 'canvas_width', 'pano_x',
                                              'pano_width', 'zoom')]
        except (KeyError, TypeError, ValueError):
            continue
        heading, camera_heading, canvas_x, canvas_width, pano_x, pano_width, zoom = values
        nearest = int(round(zoom))
        base = heading - camera_heading + 180.0
        r = (pano_x / pano_width * 360.0 - base + 180.0) % 360.0 - 180.0
        f = (canvas_x - canvas_width / 2.0) / canvas_width * 2.0 ** -(zoom - nearest)
        by_zoom.setdefault(nearest, []).append((f, r))
    out = {}
    for zoom, pairs in sorted(by_zoom.items()):
        ff = sum(f * f for f, _ in pairs)
        gain = sum(f * r for f, r in pairs) / ff if ff else None
        raw = [abs(r) for _, r in pairs]
        fit = [abs(r - gain * f) for f, r in pairs] if gain is not None else []

        def stats(values):
            return {'min': num(min(values)) if values else None, 'median': num(percentile(values, 0.5))
                    if values else None, 'max': num(max(values)) if values else None}
        out[str(zoom)] = {'n': len(pairs), 'gain_deg_per_canvas': None if gain is None else num(gain),
                          'raw_abs_deg': stats(raw), 'fit_residual_abs_deg': stats(fit)}
    return out


# ------------------------------------------------------------------------------------------ contact sheets

TILE_W, TILE_H, CAPTION_H, PER_SHEET, COLUMNS = 600, 400, 22, 9, 3


def contact_sheets(marked_store, labels, poses, flags, out_dir):
    """The --mark-label crops, 9 to a sheet, each captioned `bayonne-fr:<id>`, its type and pose class."""
    os.makedirs(out_dir, exist_ok=True)
    ordered = sorted(labels, key=lambda r: int(r['label_id']))
    paths = []
    for start in range(0, len(ordered), PER_SHEET):
        chunk = ordered[start:start + PER_SHEET]
        rows = (len(chunk) + COLUMNS - 1) // COLUMNS
        sheet = Image.new('RGB', (COLUMNS * TILE_W, rows * (TILE_H + CAPTION_H)), (255, 255, 255))
        draw = ImageDraw.Draw(sheet)
        for i, row in enumerate(chunk):
            x0, y0 = (i % COLUMNS) * TILE_W, (i // COLUMNS) * (TILE_H + CAPTION_H)
            type_id = CropRunner.resolve_label_type_id(row)
            path = os.path.join(marked_store, str(type_id), '%d.jpg' % int(row['label_id']))
            if os.path.isfile(path):
                with Image.open(path) as im:
                    im = im.convert('RGB')
                    im.thumbnail((TILE_W - 4, TILE_H - 4))
                    sheet.paste(im, (x0 + 2, y0 + CAPTION_H))
            caption = '%s %s %s %s' % (label_uid(row), row.get('label_type'), poses.get(row['pano_id'], '?'),
                                       flags.get(label_uid(row), ''))
            draw.text((x0 + 4, y0 + 4), caption, fill=(0, 0, 0))
        name = os.path.join(out_dir, 'sheet_%02d.jpg' % (start // PER_SHEET + 1))
        sheet.save(name, quality=90)
        paths.append(name)
    return paths


def write_verdict_template(path, labels, poses):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(['label_uid', 'label_type', 'pose_class', 'verdict', 'direction', 'note'])
        for row in sorted(labels, key=lambda r: int(r['label_id'])):
            w.writerow([label_uid(row), row.get('label_type'), poses.get(row['pano_id']), '', '', ''])


def read_verdicts(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------------------------------------- measure

def _read_text(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()


def _manifest(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _count_jpegs(store):
    return sum(1 for folder, _, files in os.walk(store) for n in files if n.endswith('.jpg'))


def _mb(n):
    return fmt(n / 1e6, '.1f')


def collect(args):
    """Everything `measure` reports, as one dict."""
    panos = read_gz_json(os.path.join(args.snapshot, 'panos.json.gz'))
    labels = read_gz_json(os.path.join(args.snapshot, 'cvmetadata.json.gz'))
    snap = json.loads(_read_text(os.path.join(args.snapshot, 'snapshot.json')))
    poses = pose_classes(panos)
    store = store_check(args.store, panos, labels)
    windows = window_rows(labels, args.crops)
    manifest_path = os.path.join(args.crops, CropRunner.PROVENANCE_MANIFEST)
    manifest = manifest_check(_manifest(manifest_path), labels, CITY)
    run1_copy = os.path.join(args.runs, RUN1_MANIFEST_COPY)
    marker = json.loads(_read_text(os.path.join(args.crops, CropRunner.CROP_RULE_MARKER)))
    captures = {name: _read_text(os.path.join(args.runs, name)) for name, _ in CAPTURES}
    runs = {}
    for name, _ in CAPTURES[1:]:
        runs[name] = {'counts': parse_crop_summary(captures[name]), 'exit': parse_exit(captures[name])}
    tilt_manifest = os.path.join(args.tilt_crops, CropRunner.PROVENANCE_MANIFEST)
    tilt_log = os.path.join(args.tilt_crops, 'crop.log')
    tilt = {'crops_written': _count_jpegs(args.tilt_crops),
            'manifest_rows': len(_manifest(tilt_manifest)) if os.path.exists(tilt_manifest) else None,
            'reason_lines': _read_text(tilt_log).count(NO_POSE_REASON) if os.path.exists(tilt_log) else 0}
    verdict_path = os.path.join(args.snapshot, 'visual-verdicts.csv')
    visual = visual_verdict(read_verdicts(verdict_path)) if os.path.exists(verdict_path) else None
    labelled = [p for p in panos if p.get('has_labels')]
    frames = Counter((r['stored_w'], r['stored_h']) for r in store['rows'])
    fovs = sorted(r['fov_deg'] for r in windows)
    cap = CropRunner.CROP_MAX_FOV_DEG
    return {
        'snapshot': snap, 'panos': panos, 'labels': labels, 'poses': poses, 'store': store, 'windows': windows,
        'manifest': manifest, 'manifest_path': manifest_path, 'marker': marker, 'captures': captures,
        'runs': runs, 'tilt': tilt, 'visual': visual, 'dims': dims_coverage(labels),
        'pov': pov_residuals(labels),
        'run1_manifest_unchanged': (sha256_file(run1_copy) == sha256_file(manifest_path)
                                    if os.path.exists(run1_copy) else None),
        'facts': {
            'panos_total': len(panos), 'panos_labelled': len(labelled), 'labels': len(labels),
            'label_panos': len({r['pano_id'] for r in labels}),
            'pose_classes_labelled': dict(Counter(poses[p['pano_id']] for p in labelled)),
            'label_pose_classes': dict(Counter(poses.get(r['pano_id']) for r in labels)),
            'label_types': dict(Counter(r.get('label_type') for r in labels)),
            'frames': {'%dx%d' % k: v for k, v in frames.items() if k[0] is not None},
            'store_bytes': sum(r['bytes'] or 0 for r in store['rows']),
            'shards': len({r['shard'] for r in store['rows']}),
            'uuid_ids': sum(1 for r in store['rows'] if re.fullmatch(r'[0-9a-f-]{36}', r['pano_id'])),
            'fov_min': fovs[0] if fovs else None, 'fov_median': percentile(fovs, 0.5) if fovs else None,
            'fov_max': fovs[-1] if fovs else None,
            'at_cap_by_width': dict(Counter(str(r['pano_w']) for r in windows if r['fov_deg'] >= cap)),
            'seam': sum(r['crosses_seam'] for r in windows), 'shifted': sum(r['shifted'] for r in windows),
            'downscaled': sum(r['downscaled'] for r in windows),
            'widest_window': max((r['width'] for r in windows), default=None),
            'size_ok': sum(r['size_ok'] for r in windows),
            'transcription_agrees': sum(r['transcription_agrees'] for r in windows),
            'crop_files': _count_jpegs(args.crops),
        },
    }


def table_rows(c):
    """The report's tables, row by row, exactly as the markdown carries them (a test checks each one)."""
    f, s, m, mk = c['facts'], c['store'], c['manifest'], c['marker']
    run1, run2 = c['runs']['crop.stdout.txt'], c['runs']['crop-rerun.stdout.txt']
    tilt_run, marked = c['runs']['crop-tilt.stdout.txt'], c['runs']['crop-marked.stdout.txt']
    n = f['labels']
    yes = {True: 'yes', False: 'no', None: 'not measured'}
    rows = [
        '| panos served, all / labelled | %d / %d |' % (f['panos_total'], f['panos_labelled']),
        '| labels served, on distinct panos | %d, on %d |' % (n, f['label_panos']),
        '| labelled panos with ledger verdict `downloaded=1` | %d of %d |' % (
            sum(1 for r in s['rows'] if r['ledger'] == '1'), len(s['rows'])),
        '| labelled panos not downloaded | %d |' % len(s['labelled_not_downloaded']),
        '| stored files outside a two-char shard | %d |' % len(s['outside_shard']),
        '| `walk_store_panos()` ids equal the ledger\'s `1` ids | %s |' % yes[s['walk_equals_ledger']],
        '| distinct shards over the labelled panos | %d |' % f['shards'],
        '| labelled pano ids that are 36-char UUIDs | %d of %d |' % (f['uuid_ids'], len(s['rows'])),
        '| stored bytes, labelled panos | %s MB |' % _mb(f['store_bytes']),
        '| stored frame equals `/adminapi/panos` and cvMetadata | %d of %d |' % (
            len(s['rows']) - len(s['frame_mismatch']) - len(s['missing']), len(s['rows'])),
    ]
    rows += ['| stored frame %s | %d panos |' % (k, v) for k, v in sorted(c['facts']['frames'].items())]
    rows += ['| rows with usable dims | %d of %d |' % (c['dims']['usable'], c['dims']['total'])]
    rows += ['| `%s` | %d of %d |' % (k, run1['counts'][k], run1['counts']['total']) if k == 'success'
             else '| `%s` | %d |' % (k, run1['counts'][k]) for k in SUMMARY_KEYS[:-1]]
    rows += [
        '| run 1 crop files on disk, manifest rows | %d, %d |' % (f['crop_files'], m['rows']),
        '| run 1 exit code | %d |' % run1['exit'],
        '| window fov min / median / max (deg) | %s / %s / %s |' % (
            fmt(f['fov_min'], '.1f'), fmt(f['fov_median'], '.1f'), fmt(f['fov_max'], '.1f')),
    ]
    rows += ['| windows at the 90 deg cap on %s-wide panos | %d |' % (k, v)
             for k, v in sorted(f['at_cap_by_width'].items())]
    rows += [
        '| windows crossing the seam | %d |' % f['seam'],
        '| windows shifted vertically | %d |' % f['shifted'],
        '| windows downscaled for storage | %d |' % f['downscaled'],
        '| widest window (px) | %s |' % f['widest_window'],
        '| crop file size equals the window\'s stored size | %d of %d |' % (f['size_ok'], n),
        '| standalone transcription gives the same window | %d of %d |' % (f['transcription_agrees'], n),
        '| manifest rows, `(city, label_id)` unique | %d, %s |' % (m['rows'], yes[m['unique']]),
        '| `city` is `%s` on every row | %s |' % (CITY, yes[m['city_ok']]),
        '| source and both attribution fields non-empty and equal to the served row | %d of %d |' % (
            m['attribution_equal'], m['rows']),
        '| distinct attribution pairs | %d |' % m['attribution_pairs'],
        '| source values | %s |' % ', '.join('`%s`' % v for v in m['source_values']),
        '| `crop_rule_version` | %s |' % ', '.join(m['rule_versions']),
        '| tilt columns | %s |' % '; '.join(' / '.join(v or 'blank' for v in t) for t in m['tilt_columns']),
        '| marker city, rule | %s, %s |' % (mk.get('city'), mk.get('crop_rule_version')),
        '| marker no-known-gap flag, known gaps | %s, %s |' % (
            json.dumps(mk.get(CropRunner.MANIFEST_NO_KNOWN_GAP)), mk.get(CropRunner.MANIFEST_KNOWN_GAPS)),
        '| run 2 `skipped_existing` | %d of %d |' % (run2['counts']['skipped_existing'], run2['counts']['total']),
        '| run 2 exit code | %d |' % run2['exit'],
        '| manifest bytes unchanged by run 2 | %s |' % yes[c['run1_manifest_unchanged']],
        '| labels in run 1\'s manifest not in the snapshot / the reverse | %d / %d |' % (
            len(m['not_in_snapshot']), len(m['not_in_manifest'])),
        '| tilt run `no_pose` | %d of %d |' % (tilt_run['counts']['no_pose'], tilt_run['counts']['total']),
        '| tilt run crops written, manifest rows | %d, %s |' % (c['tilt']['crops_written'],
                                                               c['tilt']['manifest_rows']),
        '| tilt run `crop.log` lines naming the reason | %d |' % c['tilt']['reason_lines'],
        '| tilt run exit code | %d |' % tilt_run['exit'],
        '| marked run `success` | %d of %d |' % (marked['counts']['success'], marked['counts']['total']),
    ]
    for zoom, z in c['pov'].items():
        rows.append('| zoom %s | %d | %s | %s | %s |' % (
            zoom, z['n'], fmt(z['gain_deg_per_canvas'], '.1f'), fmt(z['raw_abs_deg']['max'], '.1f'),
            fmt(z['fit_residual_abs_deg']['max'], '.2f')))
    v = c['visual']
    if v is not None:
        rows += ['| `%s` | %d |' % (k, v['tallies'][k]) for k in VERDICTS]
        rows += ['| %s | %s |' % (pc, ' | '.join(str(v['by_pose_class'][pc][k]) for k in VERDICTS))
                 for pc in POSE_CLASSES]
        rows += ['| off-mark directions | %s |' % ', '.join('%s %d' % kv for kv in v['off_mark_directions'].items()),
                 '| judgeable crops | %d |' % v['judgeable'],
                 '| outcome | **%s** |' % v['outcome']]
    return rows


def write_artifacts(c, args, out_dir, summary_path):
    """The committed files. Small text only; every scratch path replaced; no imagery."""
    os.makedirs(out_dir, exist_ok=True)
    replacements = [(args.store, '<store>'), (args.crops, '<crops>'), (args.tilt_crops, '<tilt-crops>'),
                    (args.marked_crops, '<marked-crops>'), (args.runs, '<out>'), (REPO_ROOT, '<repo>')]
    if args.scratch:
        replacements.append((args.scratch, '<scratch>'))

    def put_text(name, text):
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8', newline='\n') as f:
            f.write(scrub(text.replace('\r\n', '\n'), replacements))

    for source, target in CAPTURES:
        put_text(target, c['captures'][source])
    for source, target in ((os.path.join(args.crops, 'crop.log'), 'run-crop.log.txt'),
                           (os.path.join(args.tilt_crops, 'crop.log'), 'run-crop-tilt.log.txt'),
                           (os.path.join(args.store, 'scrape.log'), 'run-scrape.log.txt')):
        if os.path.exists(source):
            put_text(target, _read_text(source))
    shutil.copyfile(os.path.join(args.store, 'pano_id_log.csv'), os.path.join(out_dir, 'pano_id_log.csv'))
    shutil.copyfile(os.path.join(args.store, 'log.csv'), os.path.join(out_dir, 'download-log-row.csv'))
    shutil.copyfile(os.path.join(args.crops, CropRunner.CROP_RULE_MARKER),
                    os.path.join(out_dir, CropRunner.CROP_RULE_MARKER))
    with open(c['manifest_path'], 'rb') as f:
        manifest_bytes = f.read()
    with open(os.path.join(out_dir, 'crop_provenance.csv.gz'), 'wb') as f:
        f.write(gzip_bytes(manifest_bytes))
    snap = dict(c['snapshot'])
    # The store's manifest is read after run 2; run 1's copy is not committed, so its hash goes beside it, and
    # "manifest bytes unchanged by run 2" can be checked from the committed files.
    hashes = {CropRunner.PROVENANCE_MANIFEST: sha256_bytes(manifest_bytes)}
    run1_copy = os.path.join(args.runs, RUN1_MANIFEST_COPY)
    if os.path.exists(run1_copy):
        hashes[RUN1_MANIFEST_COPY] = sha256_file(run1_copy)
    snap['sha256'] = dict(snap['sha256'], **hashes)
    _write_json(os.path.join(out_dir, 'snapshot.json'), snap)
    for name, rows in (('crops.csv', c['windows']), ('panos.csv', c['store']['rows'])):
        with open(os.path.join(out_dir, name), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator='\n')
            w.writeheader()
            w.writerows(rows)
    summary = {
        'city': CITY, 'snapshot_taken_at_utc': c['snapshot']['taken_at_utc'], 'platform': c['snapshot']['platform'],
        'facts': {k: (num(v) if isinstance(v, float) else v) for k, v in c['facts'].items()},
        'store': {k: c['store'][k] for k in ('outside_shard', 'frame_mismatch', 'missing', 'walk_equals_ledger',
                                             'ledger_downloaded', 'ledger_verdicts', 'labelled_not_downloaded')},
        'dims': c['dims'], 'runs': c['runs'], 'tilt': c['tilt'],
        'manifest': {k: v for k, v in c['manifest'].items()},
        'attribution_1_values': sorted({r.get(ATTRIBUTION_FIELDS[0]) or '' for r in c['labels']}),
        'attribution_2_values': sorted({r.get(ATTRIBUTION_FIELDS[1]) or '' for r in c['labels']}),
        'run1_manifest_unchanged_by_run2': c['run1_manifest_unchanged'],
        'marker': {k: c['marker'].get(k) for k in ('city', 'crop_rule_version', CropRunner.MANIFEST_NO_KNOWN_GAP,
                                                   CropRunner.MANIFEST_KNOWN_GAPS)},
        'pov_residuals': c['pov'], 'visual': c['visual'], 'table_rows': table_rows(c),
    }
    summary['manifest']['tilt_columns'] = [list(t) for t in summary['manifest']['tilt_columns']]
    _write_json(summary_path, summary)
    return summary


def measure(args):
    c = collect(args)
    for row in table_rows(c):
        print(row)
    if args.sheets:
        flags = {}
        for r in c['windows']:
            marks = [m for m, on in (('seam', r['crosses_seam']), ('cap90', r['fov_deg'] >= CropRunner.CROP_MAX_FOV_DEG),
                                     ('shift', r['shifted'])) if on]
            flags[r['label_uid']] = ','.join(marks)
        paths = contact_sheets(args.marked_crops, c['labels'], c['poses'], flags, args.sheets)
        write_verdict_template(os.path.join(args.sheets, 'visual-verdicts.template.csv'), c['labels'], c['poses'])
        print('%d contact sheets in %s' % (len(paths), args.sheets))
    if args.write:
        write_artifacts(c, args, args.snapshot, SUMMARY_PATH)
        print('wrote %s and %s' % (args.snapshot, SUMMARY_PATH))
    return c


# ------------------------------------------------------------------------------------------------- the CLI

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('snapshot', help='GET the two endpoints once each and commit the bytes gzipped')
    s.add_argument('--host', required=True)
    s.add_argument('--out', default=DATA_DIR)
    s.add_argument('--plain', help='also write the uncompressed cvmetadata.json here, for CropRunner -f')
    m = sub.add_parser('measure', help='read the scratch stores and captures; print, and with --write commit')
    m.add_argument('--snapshot', default=DATA_DIR)
    m.add_argument('--store', required=True)
    m.add_argument('--crops', required=True, help='run 1 and 2 crop store, <crop-dir>/bayonne-fr')
    m.add_argument('--tilt-crops', required=True)
    m.add_argument('--marked-crops', required=True)
    m.add_argument('--runs', required=True, help='the directory holding the stdout captures')
    m.add_argument('--scratch', help='the scratch root, replaced by <scratch> in every committed capture')
    m.add_argument('--sheets', help='write contact sheets of the marked crops here (never under reports/)')
    m.add_argument('--write', action='store_true')
    return parser


def run(args):
    if args.command == 'snapshot':
        record = snapshot(args.host, args.out, args.plain)
        print(json.dumps(record['counts']))
        return 0
    measure(args)
    return 0


def main(argv=None):
    return run(build_parser().parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
