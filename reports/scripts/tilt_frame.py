"""Which frame are the stored panorama tiles in? The vertical-edge-lean estimator (#54, endpoint F2).

In a gravity-levelled equirectangular pano every world-vertical edge (a pole, a door jamb, a building
corner) is a straight vertical column. In a pano whose rows follow the camera *rig* instead, the same
edges lean, by an amount that varies sinusoidally with bearing:

    lean(b) = pitch sin b - roll cos b      (tilt_geometry.vertical_lean_deg; degrees, CCW +)

So measuring the lean of strong near-vertical edges in 12 bearing bins, and regressing it on that
prediction, says which frame the pixels are in: slope ~1 -> rig-aligned, slope ~0 -> levelled.

**Runs on makelab2 as well as here** (Python 3.9 / numpy 1.23 / PIL 10; imports only tilt_geometry,
uploaded beside it). No `match`, no `X | None`.

The estimator, per pano:
  1. decode at reduced size (JPEG draft) to a fixed working width, grayscale;
  2. Sobel gradients over the band el_lo..el_hi (default -25..+35 deg: below it is road and car, above
     it mostly sky and wires), x wrapping at the seam;
  3. keep pixels whose gradient magnitude is in the top (100 - mag_percentile)% of the band and whose
     edge is within max_abs_lean_deg of vertical;
  4. per bearing bin, the magnitude-weighted mean lean; None when fewer than min_pixels qualify
     (undefined is not zero).

Known biases, both toward zero: the +-12 deg window truncates, and real scenes carry near-vertical
edges that are not world-vertical. So **calibration is part of the instrument**: every pano is also
measured after `warp_with_extra_tilt` adds a known +2 deg of pitch (and, for one pano in
`--calibrate-roll-every`, +2 deg of roll) by exact resampling, and the per-arm slope of
(after - before) on the predicted change is the attenuation `a` the study divides out.

    python tilt_frame.py --panos sel.csv (--store <root> | --pano-root <dir>) --out lean.csv \\
        [--calibrate] [--workers 4] [--resume]

sel.csv columns: city, pano_id, pitch_deg, roll_deg, arm. A pano is read from
<store>/<city>/<id[:2]>/<id>.jpg or <pano-root>/<city>/<id[:2]>/<id>.jpg (city may be blank).
"""

import argparse
import csv
import hashlib
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tilt_geometry as tg  # noqa: E402

Image.MAX_IMAGE_PIXELS = 16384 * 8192   # our own 134 MP panos; CropRunner.raise_decompression_bomb_ceiling's value

DEFAULT_WIDTH = 4096
N_BINS = 12
EL_LO, EL_HI = -25.0, 35.0
MAG_PERCENTILE = 95.0
MAX_ABS_LEAN_DEG = 12.0
MIN_PIXELS = 50
CAL_EXTRA_DEG = 2.0

OUT_COLUMNS = ['arm', 'city', 'pano_id', 'pitch_deg', 'roll_deg', 'variant', 'extra_pitch', 'extra_roll',
               'bin', 'bin_centre_deg', 'lean_deg', 'n_px', 'predicted_lean_deg']


def load_reduced_gray(path, width=DEFAULT_WIDTH):
    """Grayscale float32 at a fixed working width (2:1 assumed), decoded with the JPEG draft so a
    16384-wide pano costs ~1/16 of a full decode."""
    with Image.open(path) as im:
        im.draft('L', (width, width // 2))
        im = im.convert('L')
        if im.size != (width, width // 2):
            im = im.resize((width, width // 2), Image.BILINEAR)
        return np.asarray(im, dtype=np.float32)


def _band_rows(h, el_lo, el_hi):
    top = int(np.floor((0.5 - el_hi / 180.0) * h))
    bottom = int(np.ceil((0.5 - el_lo / 180.0) * h))
    return max(1, top), min(h - 1, bottom)


def lean_profile(gray, n_bins=N_BINS, el_lo=EL_LO, el_hi=EL_HI, mag_percentile=MAG_PERCENTILE,
                 max_abs_lean_deg=MAX_ABS_LEAN_DEG, min_pixels=MIN_PIXELS):
    """-> list of (bin_centre_bearing_deg, weighted_mean_lean_deg or None, n_pixels).
    Lean is counter-clockwise-positive as the image is viewed, the sign of tilt_geometry.vertical_lean_deg."""
    g = np.asarray(gray, dtype=np.float32)
    h, w = g.shape
    top, bottom = _band_rows(h, el_lo, el_hi)
    rows = g[top - 1:bottom + 1]
    left = np.roll(rows, 1, axis=1)
    right = np.roll(rows, -1, axis=1)
    dx = right - left                                  # d/dx, wrapping at the seam
    gx = dx[:-2] + 2 * dx[1:-1] + dx[2:]
    dy = rows[2:] - rows[:-2]                          # d/dy (y down)
    gy = np.roll(dy, 1, axis=1) + 2 * dy + np.roll(dy, -1, axis=1)
    mag = np.hypot(gx, gy)
    ang = np.degrees(np.arctan2(gy, gx))
    fold = (ang + 90.0) % 180.0 - 90.0                 # gradient direction, mod 180, in [-90, 90)
    lean = -fold                                       # y is down: a CCW edge has gradient angle -lean
    thresh = np.percentile(mag, mag_percentile) if mag.size else np.inf
    keep = (mag > thresh) & (np.abs(lean) < max_abs_lean_deg) & (mag > 0)
    cols = np.broadcast_to(np.arange(w), keep.shape)
    bins = (cols * n_bins) // w
    out = []
    for b in range(n_bins):
        sel = keep & (bins == b)
        n = int(sel.sum())
        centre = (b + 0.5) / n_bins * 360.0 - 180.0
        if n < min_pixels:
            out.append((centre, None, n))
            continue
        wts = mag[sel]
        out.append((centre, float(np.sum(wts * lean[sel]) / np.sum(wts)), n))
    return out


def predicted_lean(bin_centres_deg, pitch_deg, roll_deg):
    """The rig-aligned prediction at the bin centres: tilt_geometry.vertical_lean_deg."""
    return tg.vertical_lean_deg(np.asarray(bin_centres_deg, dtype=float), pitch_deg, roll_deg)


def warp_with_extra_tilt(gray, extra_pitch_deg, extra_roll_deg, el_lo=EL_LO - 10, el_hi=EL_HI + 10):
    """Resample `gray` as a rig tilted by (extra_pitch, extra_roll) relative to the image's own frame
    would see it: for each output pixel, its direction -> the input frame (tilt_geometry.rig_to_gravity
    with the extra pose) -> bilinear sample. Only rows within [el_lo, el_hi] are computed (the rest is
    left as in the input); used ONLY for calibration."""
    g = np.asarray(gray, dtype=np.float32)
    h, w = g.shape
    top, bottom = _band_rows(h, el_lo, el_hi)
    ys, xs = np.mgrid[top:bottom, 0:w]
    b, el = tg.bearing_elevation_from_pixel(xs + 0.5, ys + 0.5, w, h)
    bs, els = tg.rig_to_gravity(b, el, extra_pitch_deg, extra_roll_deg)
    sx, sy = tg.pixel_from_bearing_elevation(bs, els, w, h)
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


def calibrate(gray, extra=(CAL_EXTRA_DEG, 0.0), **profile_kwargs):
    """-> (profile_before, profile_after, predicted_delta per bin) for a known added tilt."""
    before = lean_profile(gray, **profile_kwargs)
    after = lean_profile(warp_with_extra_tilt(gray, extra[0], extra[1]), **profile_kwargs)
    centres = np.array([c for c, _, _ in before])
    return before, after, predicted_lean(centres, extra[0], extra[1])


def _pano_path(row, store, pano_root):
    root = store if store else pano_root
    city = row.get('city') or ''
    pid = row['pano_id']
    return os.path.join(root, city, pid[:2], pid + '.jpg') if city else os.path.join(root, pid[:2], pid + '.jpg')


def _roll_selected(pano_id, every):
    return every > 0 and int(hashlib.md5(pano_id.encode()).hexdigest(), 16) % every == 0


def measure_one(job):
    """Worker: every output row for one pano (base, and the calibration variants)."""
    row, path, width, do_cal, roll_every = job
    p, r = float(row['pitch_deg']), float(row['roll_deg'])
    try:
        gray = load_reduced_gray(path, width)
    except Exception as e:  # noqa: BLE001 - a missing/corrupt file is a counted row, not a crash
        return [dict(arm=row.get('arm', ''), city=row.get('city', ''), pano_id=row['pano_id'], pitch_deg=p,
                     roll_deg=r, variant='error:' + type(e).__name__, extra_pitch=0, extra_roll=0, bin='',
                     bin_centre_deg='', lean_deg='', n_px=0, predicted_lean_deg='')]
    variants = [('base', 0.0, 0.0)]
    if do_cal:
        variants.append(('cal_pitch', CAL_EXTRA_DEG, 0.0))
        if _roll_selected(row['pano_id'], roll_every):
            variants.append(('cal_roll', 0.0, CAL_EXTRA_DEG))
    out = []
    for name, ep, er in variants:
        img = gray if name == 'base' else warp_with_extra_tilt(gray, ep, er)
        prof = lean_profile(img)
        for i, (c, lean, n) in enumerate(prof):
            out.append(dict(arm=row.get('arm', ''), city=row.get('city', ''), pano_id=row['pano_id'],
                            pitch_deg=p, roll_deg=r, variant=name, extra_pitch=ep, extra_roll=er, bin=i,
                            bin_centre_deg=c, lean_deg='' if lean is None else round(lean, 5), n_px=n,
                            predicted_lean_deg=round(float(predicted_lean(c, p, r)), 5)))
    return out


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--panos', required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--store')
    g.add_argument('--pano-root')
    ap.add_argument('--out', required=True)
    ap.add_argument('--calibrate', action='store_true')
    ap.add_argument('--calibrate-roll-every', type=int, default=4)
    ap.add_argument('--width', type=int, default=DEFAULT_WIDTH)
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--resume', action='store_true')
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    with open(args.panos, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    done = set()
    exists = args.resume and os.path.exists(args.out) and os.path.getsize(args.out) > 0
    if exists:
        with open(args.out, newline='', encoding='utf-8') as f:
            done = {(r['city'], r['pano_id']) for r in csv.DictReader(f)}
    jobs = [(r, _pano_path(r, args.store, args.pano_root), args.width, args.calibrate, args.calibrate_roll_every)
            for r in rows if (r.get('city', ''), r['pano_id']) not in done]
    with open(args.out, 'a' if exists else 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLUMNS, lineterminator='\n')
        if not exists:
            w.writeheader()
        if args.workers > 1:
            from multiprocessing import Pool
            with Pool(args.workers) as pool:
                results = pool.imap_unordered(measure_one, jobs)
                for i, res in enumerate(results):
                    w.writerows(res)
                    if (i + 1) % 20 == 0:
                        f.flush()
                        sys.stderr.write('%d/%d\n' % (i + 1, len(jobs)))
                        sys.stderr.flush()
        else:
            for i, job in enumerate(jobs):
                w.writerows(measure_one(job))
                if (i + 1) % 20 == 0:
                    f.flush()
                    sys.stderr.write('%d/%d\n' % (i + 1, len(jobs)))
                    sys.stderr.flush()
    sys.stderr.write('done %d\n' % len(jobs))
    return 0


if __name__ == '__main__':
    sys.exit(main())
