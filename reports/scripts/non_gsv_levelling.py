"""Are Mapillary and Panoramax panoramas gravity-levelled, and where does their pose live? (#190)

A desk check, scoped to two questions:

* **Q1 - levelling.** Is the served equirectangular image gravity-levelled, or are its rows in the camera
  rig's frame, the way GSV's are (#54/#158, F2)?
* **Q2 - pose.** Does a per-image pitch/roll exist, and where?

The instrument is the #54 F2 one, reused rather than restated: `tilt_frame.lean_profile` measures the
magnitude-weighted lean of near-vertical edges in 12 bearing bins. In a levelled pano that profile is flat;
in a rig-frame pano it is the sinusoid `roll cos b - pitch sin b` (streetlevel's sign, `pano_pose`). Here
there is no assumed pose sign to regress on, so each profile is fitted freely,

    lean(b) = c0 + A cos b + B sin b

Two readings of that fit, for the pictures that carry a `pers:pitch` / `pers:roll`:

1. **The levelling test (the verdict).** Resample the picture into the gravity frame using its own stored
   pose (`level_with_pose`) and re-measure. A rig-frame picture with a correct pose flattens; a levelled
   picture gets worse, because levelling it re-tilts it. This needs no gain calibration, and it is exactly
   the question a crop-time correction asks. It is run under both roll signs, since the docs do not fix
   how `pers:roll` relates to streetlevel's roll; the sign that flattens is the measured convention.
2. **The raw slopes (the convention, cross-checked).** The through-origin slope of B on `pers:pitch` and of
   A on `pers:roll` across pictures: ~0 for levelled pixels; for rig-frame pixels k_pitch > 0 (pers:pitch
   positive-up, streetlevel pitch = -pers:pitch) and k_roll carrying the roll sign. Not divided by F2's
   warp calibration, whose local gain is inflated by the geometry's own nonlinearity at 10-20 deg (see
   `pooled_slopes`); `a` is still recorded per picture.

Pictures with no pose are summarised by their raw lean amplitude hypot(A, B), read against the posed
pictures': a levelled image reads near the instrument's noise, a rig-frame one reads its tilt.

**Mapillary is docs-only here.** Its Graph API (`computed_rotation`) and its image URLs need an access
token; none was used. What is measured for Mapillary is what Project Sidewalk itself stores per pano
(`camera_pitch` / `camera_roll`, from `/adminapi/panos`), which SidewalkWebpage derives from
`computed_rotation`.

Two steps, so the network half runs once and the analysis is reproducible from committed files:

    python reports/scripts/non_gsv_levelling.py fetch            # ~50 sequential requests, 2 s apart
    python reports/scripts/non_gsv_levelling.py analyze \\
        --write reports/data/2026-10-06-non-gsv-levelling.json \\
        --figure reports/figures/2026-10-06-panoramax-horizons.jpg

`fetch` writes the raw API responses, gzipped, under reports/data/2026-10-06-non-gsv-levelling/, and the
`sd` (2048-wide) JPEGs under its images/ (all committed; Licence Ouverte / Etalab 2.0, see ATTRIBUTION.txt
there), and records each image's sha256 and size in sample.json. `analyze` needs no network: it reads only
committed files, and refuses an image whose bytes are not the ones recorded (a re-fetch after Panoramax
re-processes a picture would otherwise be measured silently as the same pixels). Every number in reports/2026-10-06-non-gsv-levelling.md comes from the artifact
`analyze` writes, and tests/test_non_gsv_levelling_study.py asserts that.
"""

import argparse
import gzip
import hashlib
import json
import math
import os
import sys
import time
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tilt_frame as tf                                  # noqa: E402  (after the sys.path bootstrap)
import tilt_geometry as tg                               # noqa: E402
from studyfmt import display_path, fmt, num, percentile  # noqa: E402

CITIES = {
    # city_id -> (host, source). Hosts as written elsewhere in this repo; ids from log_analyzer/cities.csv.
    'bayonne-fr': ('sidewalk-bayonne.cs.washington.edu', 'panoramax'),
    'richmond-va': ('sidewalk-richmond.cs.washington.edu', 'mapillary'),
    'laurens-ia': ('sidewalk-laurens.cs.washington.edu', 'mapillary'),
}
PANORAMAX_API = 'https://api.panoramax.xyz/api'
RAW_DIR = os.path.join(REPO_ROOT, 'reports', 'data', '2026-10-06-non-gsv-levelling')
IMAGE_DIR = os.path.join(REPO_ROOT, 'reports', 'scripts', '.cache', 'non_gsv_levelling')
USER_AGENT = 'sidewalk-panorama-tools desk study (#190; https://github.com/ProjectSidewalk/sidewalk-panorama-tools)'
PAUSE_S = 2.0

# Pictures per pose class, drawn by hash. Bayonne's adminapi rows fall in three classes (measured
# 2026-10-06): a non-zero pose, an explicit 0/0 pose, and no pose at all. Only the first can be levelled
# with; the other two are read for whether they are already level.
SAMPLE_SIZES = {'pose-nonzero': 14, 'pose-zero': 5, 'pose-absent': 5}
SEED = 190

WORK_WIDTH = 2048       # the sd asset's width; the profile is computed at the width it was served at
MAX_ABS_LEAN_DEG = 30.0  # F2 used 12; Bayonne pitches reach 20 deg, so a 12 deg window would truncate them
MIN_DEFINED_BINS = 6
CAL_EXTRA_DEG = tf.CAL_EXTRA_DEG
N_BOOT = 2000


def pct(values, q):
    """studyfmt.percentile as an artifact value: None for an empty series (undefined, not zero; `num` itself
    refuses None), else `num` of it."""
    v = percentile(values, q)
    return None if v is None else num(v)


# ---------------------------------------------------------------------------------------------- selection

def hash_key(pano_id):
    """A stable, content-free ordering: a sample drawn by it is the same on every machine and every run."""
    return hashlib.sha1(('%d:%s' % (SEED, pano_id)).encode('utf-8')).hexdigest()


def pose_class(pano):
    """'pose-nonzero', 'pose-zero' (pitch 0 AND roll present and 0) or 'pose-absent' (no roll stored).

    PS writes `camera_pitch: pers:pitch || 0` and `camera_roll: pers:roll ?? undefined` for Panoramax
    (PanoramaxViewer.js), so an absent pose reads pitch 0, roll null. The zero class is kept apart because PS cannot tell an explicit `pers:pitch: 0` from an absent one (it
    writes `|| 0`), but it CAN tell an explicit roll of 0, and a stored 0/0 is either a measured level
    picture or a default nobody measured - which is one of the things this study reads off the pixels."""
    pitch = pano.get('camera_pitch')
    roll = pano.get('camera_roll')
    if roll is None and not pitch:
        return 'pose-absent'
    if not pitch and not roll:
        return 'pose-zero'
    return 'pose-nonzero'


def select_sample(panos, sizes=None):
    """-> list of (pano_id, stratum): the first `sizes[stratum]` pictures of each pose class in hash order."""
    sizes = SAMPLE_SIZES if sizes is None else sizes
    ordered = sorted(panos, key=lambda p: hash_key(p['pano_id']))
    out = []
    for stratum, n in sizes.items():
        out.extend((p['pano_id'], stratum) for p in [p for p in ordered if pose_class(p) == stratum][:n])
    return out


# ----------------------------------------------------------------------------------------- STAC readers

def stac_pose(item):
    """-> (pers_pitch, pers_roll, pers_yaw) from a STAC item's properties; None for an absent value.
    Absent is not zero: an item that never carried a pitch is not one measured to be level."""
    props = item.get('properties') or {}

    def _get(key):
        v = props.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return _get('pers:pitch'), _get('pers:roll'), _get('pers:yaw')


def exif_pose(item):
    """-> (PosePitchDegrees, PoseRollDegrees) as written into the uploaded file's XMP, or None each."""
    exif = (item.get('properties') or {}).get('exif') or {}

    def _get(key):
        try:
            return float(exif[key])
        except (KeyError, TypeError, ValueError):
            return None
    return _get('Xmp.GPano.PosePitchDegrees'), _get('Xmp.GPano.PoseRollDegrees')


def camera_of(item):
    io = (item.get('properties') or {}).get('pers:interior_orientation') or {}
    return '%s %s' % (io.get('camera_manufacturer', '?'), io.get('camera_model', '?'))


# ------------------------------------------------------------------------------------------ the instrument

def fit_sinusoid(profile):
    """Least-squares lean(b) = c0 + A cos b + B sin b over the defined bins of a `lean_profile`.

    -> dict(c0, A, B, amp, n_bins), or None when fewer than MIN_DEFINED_BINS bins are defined (undefined is
    not zero: a picture whose edges all fell outside the window has no measured lean, not a flat one)."""
    pts = [(c, v) for c, v, _ in profile if v is not None]
    if len(pts) < MIN_DEFINED_BINS:
        return None
    b = np.radians([c for c, _ in pts])
    y = np.array([v for _, v in pts], dtype=float)
    X = np.column_stack([np.ones_like(b), np.cos(b), np.sin(b)])
    (c0, A, B), *_ = np.linalg.lstsq(X, y, rcond=None)
    return {'c0': float(c0), 'A': float(A), 'B': float(B), 'amp': float(math.hypot(A, B)), 'n_bins': len(pts)}


def attenuation(before, after, extra_pitch=CAL_EXTRA_DEG, extra_roll=0.0):
    """The instrument's gain on one picture: slope of (after - before) on the change the warp predicts,
    through the origin, over bins defined in both. None when no bin is."""
    d, p = [], []
    for (c, v0, _), (_, v1, _) in zip(before, after):
        if v0 is None or v1 is None:
            continue
        d.append(v1 - v0)
        p.append(float(tg.vertical_lean_deg(c, extra_pitch, extra_roll)))
    d, p = np.array(d), np.array(p)
    if not len(p) or not np.any(p):
        return None
    return float(np.dot(d, p) / np.dot(p, p))


def measure_picture(gray, max_abs_lean_deg=MAX_ABS_LEAN_DEG):
    """-> dict(profile, fit, a) for one grayscale equirectangular image."""
    kw = {'max_abs_lean_deg': max_abs_lean_deg}
    before = tf.lean_profile(gray, **kw)
    after = tf.lean_profile(tf.warp_with_extra_tilt(gray, CAL_EXTRA_DEG, 0.0), **kw)
    return {'profile': before, 'fit': fit_sinusoid(before), 'a': attenuation(before, after)}


def level_with_pose(gray, pitch_deg, roll_deg, el_lo=tf.EL_LO - 10, el_hi=tf.EL_HI + 10):
    """Resample a rig-frame equirectangular image into the gravity frame, given its pose in streetlevel's
    sign (pano_pose): each output pixel is a gravity direction, `gravity_to_rig` says where the rig saw it,
    and that pixel is sampled bilinearly. Only the rows the lean band reads are computed.

    This is the crop-time correction's question asked of the pixels: if the stored pose is right, levelling
    with it must flatten the lean profile. (`tilt_frame.warp_with_extra_tilt` is the opposite direction -
    it ADDS a tilt for calibration - so it is not reused here.)"""
    g = np.asarray(gray, dtype=np.float32)
    h, w = g.shape
    top, bottom = tf._band_rows(h, el_lo, el_hi)
    ys, xs = np.mgrid[top:bottom, 0:w]
    b, el = tg.bearing_elevation_from_pixel(xs + 0.5, ys + 0.5, w, h)
    br, elr = tg.gravity_to_rig(b, el, pitch_deg, roll_deg)
    sx, sy = tg.pixel_from_bearing_elevation(br, elr, w, h)
    sx = sx - 0.5
    sy = np.clip(sy - 0.5, 0, h - 1.000001)
    x0 = np.floor(sx).astype(int)
    y0 = np.floor(sy).astype(int)
    fx = (sx - x0).astype(np.float32)
    fy = (sy - y0).astype(np.float32)
    x0 %= w
    x1 = (x0 + 1) % w
    y1 = np.minimum(y0 + 1, h - 1)
    v = (g[y0, x0] * (1 - fx) * (1 - fy) + g[y0, x1] * fx * (1 - fy)
         + g[y1, x0] * (1 - fx) * fy + g[y1, x1] * fx * fy)
    out = g.copy()
    out[top:bottom] = v
    return out


def pers_to_streetlevel(pers_pitch, pers_roll, roll_sign, pitch_sign=-1.0):
    """(pitch, roll) in pano_pose's sign from Panoramax's `pers:*`. Pitch: `pers:pitch` is positive-up
    (GPano's PosePitchDegrees, "degrees above the horizon"), streetlevel's is positive nose-down, so it
    flips (pitch_sign -1, the default; +1 exists only so the levelling test can try the other reading).
    Roll's relation is what this study measures, hence the explicit sign."""
    return pitch_sign * pers_pitch, roll_sign * pers_roll


# The four readings of `pers:*` the levelling test tries. '+1'/'-1' are the roll sign under the documented
# pitch sign (streetlevel pitch = -pers:pitch); the 'pitch-flipped' pair reads pers:pitch as nose-down.
LEVEL_KEYS = {'+1': (-1.0, 1.0), '-1': (-1.0, -1.0),
              'pitch-flipped +1': (1.0, 1.0), 'pitch-flipped -1': (1.0, -1.0)}


def levelling_test(gray, pers_pitch, pers_roll, max_abs_lean_deg=MAX_ABS_LEAN_DEG):
    """-> {reading: amplitude of the lean profile after levelling with the stored pose under that reading
    of its signs (LEVEL_KEYS)}. A rig-frame image with a correct pose flattens under exactly one reading; a
    levelled image gets WORSE under all of them (levelling it re-tilts it by the pose)."""
    out = {}
    for key, (pitch_sign, roll_sign) in LEVEL_KEYS.items():
        p, r = pers_to_streetlevel(pers_pitch, pers_roll, roll_sign, pitch_sign)
        fit = fit_sinusoid(tf.lean_profile(level_with_pose(gray, p, r), max_abs_lean_deg=max_abs_lean_deg))
        out[key] = num(fit['amp']) if fit else None
    return out


def through_origin_slope(x, y):
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    den = float(np.dot(x, x))
    return float(np.dot(x, y) / den) if den > 0 else None


def pooled_slopes(rows, n_boot=N_BOOT, seed=SEED):
    """Across posed pictures: the through-origin slope of the fitted B on `pers:pitch` (k_pitch) and of A on
    `pers:roll` (k_roll), with a picture-cluster bootstrap CI. Raw, not divided by a calibration: the warp
    calibration's local gain is inflated by the geometry's own nonlinearity at Bayonne's 10-20 deg tilts
    (a = 1.30 on noiseless synthetic poles at 18 deg, tests/test_non_gsv_levelling_study.py), so the
    levelling test, which needs no gain, carries the verdict and these carry the convention.

    Levelled pixels give k ~ 0. Rig-frame pixels give k_pitch > 0 (`pers:pitch` positive-up, B = -pitch_sl)
    and k_roll whose SIGN is the `pers:roll` convention.

    rows: dicts with A, B, pers_pitch, pers_roll."""
    rows = [r for r in rows if r.get('A') is not None and r['pers_pitch'] is not None and r['pers_roll'] is not None]
    if len(rows) < 3:
        return None

    def _est(sample):
        return (through_origin_slope([r['pers_pitch'] for r in sample], [r['B'] for r in sample]),
                through_origin_slope([r['pers_roll'] for r in sample], [r['A'] for r in sample]))

    kb, ka = _est(rows)
    rng = np.random.default_rng(seed)
    boots = [_est([rows[i] for i in rng.integers(0, len(rows), len(rows))]) for _ in range(n_boot)]
    kbs = np.array([b[0] for b in boots if b[0] is not None])
    kas = np.array([b[1] for b in boots if b[1] is not None])
    return {
        'n': len(rows),
        'k_pitch': num(kb), 'k_pitch_ci95': [num(np.percentile(kbs, 2.5)), num(np.percentile(kbs, 97.5))],
        'k_roll': num(ka), 'k_roll_ci95': [num(np.percentile(kas, 2.5)), num(np.percentile(kas, 97.5))],
    }


# ----------------------------------------------------------------------------------- PS-side pose summary

def ps_pose_summary(panos):
    """What Project Sidewalk's `/adminapi/panos` stores per pano, for one city."""
    pitches = [p.get('camera_pitch') for p in panos]
    rolls = [p.get('camera_roll') for p in panos]
    has_roll = [r for r in rolls if r is not None]
    nonzero_pitch = [x for x in pitches if x]
    labelled = [p for p in panos if p.get('has_labels')]
    return {
        'panos': len(panos),
        'labelled': len(labelled),
        'with_roll': len(has_roll),
        'with_nonzero_pitch': len(nonzero_pitch),
        'with_null_pitch_and_roll': sum(1 for p in panos
                                        if p.get('camera_pitch') is None and p.get('camera_roll') is None),
        'by_pose_class': {k: sum(1 for p in panos if pose_class(p) == k) for k in SAMPLE_SIZES},
        'labelled_by_pose_class': {k: sum(1 for p in labelled if pose_class(p) == k) for k in SAMPLE_SIZES},
        'abs_pitch_p90': pct([abs(x) for x in nonzero_pitch], 0.9),
        'abs_roll_p90': pct([abs(x) for x in has_roll], 0.9),
        'abs_pitch_max': num(max((abs(x) for x in nonzero_pitch), default=None)) if nonzero_pitch else None,
        'abs_roll_max': num(max((abs(x) for x in has_roll), default=None)) if has_roll else None,
        'sources': sorted({str(p.get('source')) for p in panos}),
        'widths': sorted({p.get('width') for p in panos if p.get('width') is not None}),
    }


# ------------------------------------------------------------------------------------------------ network

def _get(url, binary=False):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    time.sleep(PAUSE_S)
    return body if binary else json.loads(body.decode('utf-8'))


def _write_gz(path, obj):
    with gzip.open(path, 'wt', encoding='utf-8') as f:
        json.dump(obj, f, sort_keys=True, allow_nan=False)


def _read_gz(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        return json.load(f)


def fetch(args):
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    n_req = 0
    panos = {}
    for city, (host, _) in CITIES.items():
        path = os.path.join(RAW_DIR, 'adminapi-panos-%s.json.gz' % city)
        if not os.path.exists(path) or args.refresh:
            _write_gz(path, _get('https://%s/adminapi/panos' % host))
            n_req += 1
        panos[city] = _read_gz(path)
    sample = select_sample(panos['bayonne-fr'])
    items_path = os.path.join(RAW_DIR, 'panoramax-items.json.gz')
    items = _read_gz(items_path) if os.path.exists(items_path) and not args.refresh else {}
    for pano_id, _ in sample:
        if pano_id not in items:
            items[pano_id] = _get('%s/pictures/%s' % (PANORAMAX_API, pano_id))
            n_req += 1
    _write_gz(items_path, items)
    for pano_id, _ in sample:
        dest = os.path.join(IMAGE_DIR, '%s.sd.jpg' % pano_id)
        if not os.path.exists(dest):
            body = _get(items[pano_id]['assets']['sd']['href'], binary=True)
            n_req += 1
            with open(dest, 'wb') as f:
                f.write(body)
    entries = []
    for i, s in sample:
        entry = {'pano_id': i, 'stratum': s}
        entry.update(image_record(os.path.join(IMAGE_DIR, '%s.sd.jpg' % i)))
        entries.append(entry)
    with open(os.path.join(RAW_DIR, 'sample.json'), 'w', encoding='utf-8') as f:
        json.dump({'seed': SEED, 'fetched': time.strftime('%Y-%m-%d'), 'sample': entries}, f, indent=1)
    print('fetch: %d requests, %d pictures' % (n_req, len(sample)))


def image_record(path):
    """{'sha256', 'bytes'} of one image, recorded in sample.json so the pixels measured are pinned."""
    with open(path, 'rb') as f:
        body = f.read()
    return {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}


def verify_image(path, entry):
    """Raise ValueError unless the file on disk is the one sample.json recorded.

    A re-fetch after Panoramax re-processes a picture (blurring, re-encoding) would otherwise be measured
    silently as if it were the same pixels; an entry with no recorded hash is refused for the same reason."""
    if 'sha256' not in entry or 'bytes' not in entry:
        raise ValueError('%s: sample.json records no sha256/bytes for it' % entry.get('pano_id'))
    got = image_record(path)
    if got != {'sha256': entry['sha256'], 'bytes': entry['bytes']}:
        raise ValueError('%s: image on disk (%s, %d bytes) is not the one recorded (%s, %d bytes)' % (
            entry.get('pano_id'), got['sha256'][:12], got['bytes'], entry['sha256'][:12], entry['bytes']))


# --------------------------------------------------------------------------------------------------- figure

def horizon_rows(width, height, pers_pitch, pers_roll, roll_sign):
    """Rig-image rows of the gravity horizon, under the convention `analyze` measured
    (streetlevel pitch = -pers:pitch, streetlevel roll = roll_sign * pers:roll)."""
    xs = np.arange(width) + 0.5
    _, ys = tg.rig_pixel_from_gravity_pixel(xs, np.full(width, height / 2.0), width, height,
                                            -pers_pitch, roll_sign * pers_roll)
    return ys


def draw_figure(panels, out_path, roll_sign, thumb_w=768):
    """panels: list of (gray-or-rgb path, title, pers_pitch or None, pers_roll or None). Writes one small
    JPEG: each thumbnail with its mid-line (where a levelled horizon sits, dashed) and, when the picture
    carries a pose, the horizon that pose predicts on a rig-frame image (solid)."""
    from PIL import Image, ImageDraw
    thumb_h = thumb_w // 2
    pad = 22
    canvas = Image.new('RGB', (thumb_w, len(panels) * (thumb_h + pad)), 'white')
    d = ImageDraw.Draw(canvas)
    for i, (path, title, pp, pr) in enumerate(panels):
        y0 = i * (thumb_h + pad) + pad
        with Image.open(path) as im:
            canvas.paste(im.convert('RGB').resize((thumb_w, thumb_h), Image.BILINEAR), (0, y0))
        d.text((4, y0 - pad + 5), title, fill='black')
        for x in range(0, thumb_w, 12):
            d.line([(x, y0 + thumb_h // 2), (x + 6, y0 + thumb_h // 2)], fill=(255, 255, 255), width=2)
        if pp is not None and pr is not None:
            ys = horizon_rows(thumb_w, thumb_h, pp, pr, roll_sign)
            d.line([(x, y0 + float(y)) for x, y in enumerate(ys)], fill=(255, 40, 40), width=2)
    canvas.save(out_path, quality=80)


# --------------------------------------------------------------------------------------------------- analyze

def analyze(args):
    panos = {c: _read_gz(os.path.join(RAW_DIR, 'adminapi-panos-%s.json.gz' % c)) for c in CITIES}
    items = _read_gz(os.path.join(RAW_DIR, 'panoramax-items.json.gz'))
    with open(os.path.join(RAW_DIR, 'sample.json'), encoding='utf-8') as f:
        sample = json.load(f)
    ps_by_id = {p['pano_id']: p for p in panos['bayonne-fr']}

    pictures = []
    for entry in sample['sample']:
        pid, stratum = entry['pano_id'], entry['stratum']
        item = items[pid]
        pp, pr, py = stac_pose(item)
        ep, er = exif_pose(item)
        path = os.path.join(IMAGE_DIR, '%s.sd.jpg' % pid)
        verify_image(path, entry)
        gray = tf.load_reduced_gray(path, width=WORK_WIDTH)
        m = measure_picture(gray)
        fit = m['fit'] or {}
        ps = ps_by_id.get(pid, {})
        row = {
            'pano_id': pid, 'stratum': stratum, 'camera': camera_of(item),
            'producer': (item.get('properties') or {}).get('geovisio:producer'),
            'has_labels': bool(ps.get('has_labels')),
            'width': ps.get('width'),
            'pers_pitch': pp, 'pers_roll': pr, 'pers_yaw': py,
            'exif_pose_pitch': ep, 'exif_pose_roll': er,
            'ps_camera_pitch': ps.get('camera_pitch'), 'ps_camera_roll': ps.get('camera_roll'),
            'A': num(fit.get('A')), 'B': num(fit.get('B')), 'amp': num(fit.get('amp')),
            'n_bins': fit.get('n_bins'), 'a': num(m['a']),
            'profile': [[num(c), num(v), n] for c, v, n in m['profile']],
            'amp_levelled': None,
        }
        if stratum == 'pose-nonzero' and pp is not None and pr is not None:
            row['amp_levelled'] = levelling_test(gray, pp, pr)
        pictures.append(row)

    agg = aggregate(pictures)
    lev, best = agg['levelling_test'], agg['roll_sign_that_levels']
    groups = {k: [p for p in pictures if p['stratum'] == k and p['A'] is not None] for k in SAMPLE_SIZES}
    posed = groups['pose-nonzero']
    slopes, a_all = agg['slopes_posed'], agg['a_median_all']
    result = {
        'question': '#190 Q1/Q2: are Mapillary and Panoramax served panos gravity-levelled, and where is the pose?',
        'parameters': {'work_width': WORK_WIDTH, 'max_abs_lean_deg': MAX_ABS_LEAN_DEG,
                       'cal_extra_pitch_deg': CAL_EXTRA_DEG, 'n_boot': N_BOOT, 'seed': SEED,
                       'min_defined_bins': MIN_DEFINED_BINS, 'sample_sizes': SAMPLE_SIZES,
                       'flattened_rule': 'amp_after < 0.5 * amp_before'},
        'fetched': sample.get('fetched'),
        'ps_pose_by_city': {c: ps_pose_summary(v) for c, v in panos.items()},
        'bayonne_labelled_posed_tilt': labelled_tilt_amplitudes(panos['bayonne-fr']),
        'panoramax': dict(agg, pictures=pictures),
    }
    if args.write:
        with open(args.write, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=1, sort_keys=True, allow_nan=False)
            f.write('\n')
        print('wrote %s' % display_path(args.write, REPO_ROOT))

    sl = slopes or {}
    print('posed n=%s  k_pitch=%s %s  k_roll=%s %s  a(all)=%s' % (
        sl.get('n'), fmt(sl.get('k_pitch'), '.3f'), sl.get('k_pitch_ci95'), fmt(sl.get('k_roll'), '.3f'),
        sl.get('k_roll_ci95'), fmt(a_all, '.3f')))
    for sign, v in lev.items():
        print('levelling with roll sign %s: amp %s -> %s (max %s), flattened %d of %d' % (
            sign, fmt(v['amp_before_median'], '.2f'), fmt(v['amp_after_median'], '.2f'),
            fmt(v['amp_after_max'], '.2f'), v['flattened'], v['n']))
    for k, v in result['panoramax']['lean_amplitude_by_class'].items():
        print('%s: n=%d lean amplitude median=%s range %s-%s' % (
            k, v['n'], fmt(v['median'], '.2f'), fmt(v['min'], '.2f'), fmt(v['max'], '.2f')))

    if args.figure:
        roll_sign = 1.0 if best == '+1' else -1.0
        by_tilt = sorted(posed, key=lambda p: -math.hypot(p['pers_pitch'], p['pers_roll']))
        chosen = (by_tilt[:2] + sorted(groups['pose-zero'], key=lambda p: p['pano_id'])[:1]
                  + sorted(groups['pose-absent'], key=lambda p: p['pano_id'])[:1])
        panels = []
        for p in chosen:
            if p['stratum'] == 'pose-nonzero':
                title = 'pers:pitch %+.1f, pers:roll %+.1f; red = the horizon that pose predicts' % (
                    p['pers_pitch'], p['pers_roll'])
            else:
                title = '%s, %s px wide; lean amplitude %.1f deg' % (p['stratum'], p['width'], p['amp'])
            panels.append((os.path.join(IMAGE_DIR, '%s.sd.jpg' % p['pano_id']), title,
                           p['pers_pitch'] if p['stratum'] == 'pose-nonzero' else None,
                           p['pers_roll'] if p['stratum'] == 'pose-nonzero' else None))
        draw_figure(panels, args.figure, roll_sign)
        print('wrote %s' % display_path(args.figure, REPO_ROOT))
    return result


def summarise_levelling(posed):
    """Per reading of the pose's signs (LEVEL_KEYS): the lean amplitude before and after levelling with the
    stored pose, and how many pictures it flattened (after < half of before). Medians, not means: a picture
    whose edges are mostly trees should not move the verdict."""
    lev = {}
    for sign in LEVEL_KEYS:
        pairs = [(p['amp'], p['amp_levelled'][sign]) for p in posed
                 if p.get('amp_levelled') and p['amp_levelled'].get(sign) is not None and p['amp'] is not None]
        lev[sign] = {
            'n': len(pairs),
            'amp_before_median': pct([x for x, _ in pairs], 0.5),
            'amp_after_median': pct([y for _, y in pairs], 0.5),
            'amp_after_max': num(max(y for _, y in pairs)) if pairs else None,
            'flattened': sum(1 for x, y in pairs if y < 0.5 * x),
            'lowered': sum(1 for x, y in pairs if y < x),
        }
    return lev


def amplitude_summary(group):
    """n, median, min and max of the raw lean amplitude over a group of measured rows."""
    amps = [p['amp'] for p in group if p['amp'] is not None]
    return {'n': len(amps), 'median': pct(amps, 0.5),
            'min': num(min(amps)) if amps else None, 'max': num(max(amps)) if amps else None}


def aggregate(pictures):
    """Everything in the artifact's `panoramax` block except the per-picture rows, from those rows alone.

    Pure, so a test re-derives the committed block from the committed rows without any pixels."""
    groups = {k: [p for p in pictures if p['stratum'] == k and p['A'] is not None] for k in SAMPLE_SIZES}
    posed = groups['pose-nonzero']
    lev = summarise_levelling(posed)
    a_vals = [p['a'] for p in pictures if p['a'] is not None]
    out = {
        'sample_size': len(pictures),
        'measured_by_class': {k: len(v) for k, v in groups.items()},
        'cameras': sorted({p['camera'] for p in pictures}),
        'cameras_by_class': {k: sorted({p['camera'] + ' ' + str(p['width']) for p in v}) for k, v in groups.items()},
        'producers': sorted({str(p['producer']) for p in pictures}),
        'a_median_all': num(float(np.median(a_vals))) if a_vals else None,
        'slopes_posed': pooled_slopes(posed),
        'levelling_test': lev,
        'roll_sign_that_levels': choose_roll_sign(lev),
        'lean_amplitude_by_class': {k: amplitude_summary(v) for k, v in groups.items()},
    }
    out.update(stac_exif_counts(pictures))
    return out


def choose_roll_sign(lev):
    """The roll sign ('+1' or '-1', under the documented pitch sign) whose levelling leaves the SMALLEST
    median amplitude; None when neither was measured. The pitch-flipped readings are a check on the pitch
    sign, not candidates here."""
    cands = [k for k in ('+1', '-1') if lev.get(k, {}).get('amp_after_median') is not None]
    return min(cands, key=lambda k: lev[k]['amp_after_median']) if cands else None


def stac_exif_counts(pictures):
    """How the STAC pose, PS's copy of it and the uploaded file's EXIF relate, over the sampled rows.

    `stac_nonzero_but_exif_pose_zero_or_absent` counts a non-zero STAC pose whose EXIF GPano pose is 0.0
    OR absent: either way the camera did not write the pose the catalog serves."""
    nonzero = [p for p in pictures if p['pers_pitch'] or p['pers_roll']]
    return {
        'stac_pose_present': sum(1 for p in pictures if p['pers_pitch'] is not None),
        'stac_pose_nonzero': len(nonzero),
        'stac_pose_equals_ps_pose': sum(1 for p in pictures
                                        if p['pers_pitch'] is not None and p['ps_camera_pitch'] == p['pers_pitch']
                                        and p['ps_camera_roll'] == p['pers_roll']),
        'stac_nonzero_but_exif_pose_zero_or_absent': sum(
            1 for p in nonzero if p['exif_pose_pitch'] in (None, 0.0) and p['exif_pose_roll'] in (None, 0.0)),
    }


def labelled_tilt_amplitudes(panos):
    """The tilt amplitude hypot(pitch, roll) of each LABELLED `pose-nonzero` pano in PS's own rows: the upper
    bound on the `pano_y` leak a label on it can carry (the leak at a label is T(b) at its bearing, which is
    at most this). Sorted, with the median and how many reach 10 deg."""
    amps = sorted(math.hypot(p.get('camera_pitch') or 0.0, p.get('camera_roll') or 0.0)
                  for p in panos if p.get('has_labels') and pose_class(p) == 'pose-nonzero')
    return {
        'n': len(amps),
        'amplitudes_deg': [num(round(a, 2)) for a in amps],
        'median_deg': pct(amps, 0.5),
        'max_deg': num(max(amps)) if amps else None,
        'at_least_10_deg': sum(1 for a in amps if a >= 10.0),
        'at_least_3_deg': sum(1 for a in amps if a >= 3.0),
    }


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    f = sub.add_parser('fetch', help='the network half: adminapi panos, STAC items, sd images')
    f.add_argument('--refresh', action='store_true', help='re-fetch the JSON even if it is on disk')
    a = sub.add_parser('analyze', help='the offline half')
    a.add_argument('--write', help='the JSON artifact')
    a.add_argument('--figure', help='the small horizon figure (JPEG)')
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return fetch(args) if args.cmd == 'fetch' else analyze(args)


if __name__ == '__main__':
    main()
