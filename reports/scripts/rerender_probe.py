"""What did Google's re-render change: geometry, tone, or sharpness? (#114)

    python rerender_probe.py --old-store <production city dir> --new-store <pilot copy> \
        --ledger <pilot copy>/refetch_log.csv --pilot-json <2026-09-05-fover-refetch-pilot.json> \
        --write rerender_probe.json [--workers 6]

For every panorama the pilot swapped (78), compares the stored frame against the re-fetched one per band
(bottom = polar rows below the horizon band; horizon = the control band CBK served at full size in both eras):

* shift: phase correlation on a grid of 1024x1024 windows across the band, so a uniform translation
  (heading change) reads as a consistent shift and a warp (pitch/roll change) reads as windows that lock
  but disagree. Each window also records the MAE with and without the estimated shift applied, so a
  spurious peak cannot pass as a real displacement.
* tone: a least-squares affine fit new ~= a*old + b on subsampled luma, and the residual MAE after it.
  A re-grade is a change the fit removes; a re-render is one it does not.
* sharpness: variance of the 4-neighbour Laplacian, new/old, as in refetch_pilot_sharpness.py.

The 59 panoramas the pilot classed as same-rendering (horizon MAE <= 3.0 in the committed artifact) run
through the same code and are the null: what "nothing changed" looks like on every statistic.
"""
import argparse
import csv
import json
import os
from multiprocessing import Pool

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
TILE = 512
# (first full-res row, last full-res row) of the zoom-5 grid, from tests/fixtures/tiles/fover_band_map.json
BAND_ROWS = {8192: (5, 10), 6656: (4, 8)}
WIN = 1024
LOCK_PEAK = 0.10          # a window whose phase-correlation peak is below this did not find a correspondence
RERENDERED_HORIZON_MAE = 3.0


def band_pixel_rows(h):
    rows = BAND_ROWS.get(h)
    if rows is None:
        return None
    first, last = rows
    return {'bottom': ((last + 1) * TILE, h), 'horizon': (first * TILE, (last + 1) * TILE)}


def phase_correlate(a, b):
    """(dy, dx, peak) with b ~= np.roll(a, (dy, dx)): content in b sits at old position + shift."""
    h, w = a.shape
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    A = np.fft.rfft2((a - a.mean()) * win)
    B = np.fft.rfft2((b - b.mean()) * win)
    R = A * np.conj(B)
    R /= (np.abs(R) + 1e-9)
    r = np.fft.irfft2(R, s=(h, w))
    py, px = np.unravel_index(int(np.argmax(r)), r.shape)
    py = int(py)
    px = int(px)
    # sub-pixel refinement: parabola through the peak and its two neighbours on each axis (wrapped)
    def _sub(m, c, pl):
        den = m - 2.0 * c + pl
        return float((m - pl) / (2.0 * den)) if den < 0 else 0.0
    sy = _sub(r[(py - 1) % h, px], r[py, px], r[(py + 1) % h, px])
    sx = _sub(r[py, (px - 1) % w], r[py, px], r[py, (px + 1) % w])
    dy = py
    dx = px
    if dy > h // 2:
        dy -= h
    if dx > w // 2:
        dx -= w
    return -dy, -dx, float(r.max()), -(dy + sy), -(dx + sx)


def _selftest():
    rng = np.random.default_rng(0)
    base = rng.random((256, 256)).astype(np.float32)
    base = np.cumsum(np.cumsum(base, 0), 1)          # smooth-ish, so the peak is unambiguous
    shifted = np.roll(base, (7, -11), axis=(0, 1))
    dy, dx, peak, sdy, sdx = phase_correlate(base, shifted)
    assert (dy, dx) == (7, -11), (dy, dx, peak)
    assert abs(sdy - 7) < 0.25 and abs(sdx + 11) < 0.25, (sdy, sdx)


def shifted_mae(a, b, dy, dx):
    """MAE between a and b after moving b back by (dy, dx), on the overlap."""
    h, w = a.shape
    ys = slice(max(0, dy), h + min(0, dy))
    xs = slice(max(0, dx), w + min(0, dx))
    ys0 = slice(max(0, -dy), h + min(0, -dy))
    xs0 = slice(max(0, -dx), w + min(0, -dx))
    return float(np.abs(a[ys0, xs0] - b[ys, xs]).mean())


def laplacian_variance(s, chunk=256):
    """Variance of the 4-neighbour Laplacian over interior pixels, accumulated in row chunks so a
    16384-wide band never allocates a full-band temporary (the one-shot form peaks near 1 GB per band)."""
    h = s.shape[0]
    n = 0
    total = 0.0
    total_sq = 0.0
    for y0 in range(1, h - 1, chunk):
        y1 = min(y0 + chunk, h - 1)
        c = s[y0:y1, 1:-1]
        lap = 4.0 * c - s[y0 - 1:y1 - 1, 1:-1] - s[y0 + 1:y1 + 1, 1:-1] - s[y0:y1, :-2] - s[y0:y1, 2:]
        lap = lap.astype(np.float64, copy=False)
        n += lap.size
        total += float(lap.sum())
        total_sq += float((lap * lap).sum())
    mean = total / n
    return total_sq / n - mean * mean


def mae_and_mean(o, n, chunk=256):
    """Whole-band MAE and mean signed difference (new - old), accumulated in row chunks."""
    cnt = 0
    tot_abs = 0.0
    tot = 0.0
    for y0 in range(0, o.shape[0], chunk):
        d = (n[y0:y0 + chunk] - o[y0:y0 + chunk]).astype(np.float64, copy=False)
        cnt += d.size
        tot_abs += float(np.abs(d).sum())
        tot += float(d.sum())
    return tot_abs / cnt, tot / cnt


def window_grid(width, top, bottom, n_x=8):
    """Window origins: n_x across the width, and as many rows of WIN as fit the band (max 2), 64 px in."""
    xs = [min(max(0, int((k + 0.5) * width / n_x) - WIN // 2), width - WIN) for k in range(n_x)]
    ys = []
    y = top + 64
    while y + WIN <= bottom and len(ys) < 2:
        ys.append(y)
        y += WIN + 64
    return [(y, x) for y in ys for x in xs]


def measure_band(old, new, top, bottom):
    o = np.asarray(old.crop((0, top, old.width, bottom)).convert('L'), dtype=np.float32)
    n = np.asarray(new.crop((0, top, new.width, bottom)).convert('L'), dtype=np.float32)
    out = {'rows_px': [top, bottom]}
    out['mae'], out['mean_signed_diff'] = mae_and_mean(o, n)
    # tone: affine fit on a subsample
    os_ = o[::4, ::4].ravel()
    ns_ = n[::4, ::4].ravel()
    A = np.stack([os_, np.ones_like(os_)], axis=1)
    (a, b), *_ = np.linalg.lstsq(A, ns_, rcond=None)
    resid = ns_ - (a * os_ + b)
    out['affine'] = {'gain': float(a), 'offset': float(b), 'mae_before': float(np.abs(ns_ - os_).mean()),
                     'mae_after': float(np.abs(resid).mean())}
    out['lap_var_old'] = laplacian_variance(o)
    out['lap_var_new'] = laplacian_variance(n)
    out['lap_ratio'] = (out['lap_var_new'] / out['lap_var_old']) if out['lap_var_old'] > 0 else None
    # a tone gain g scales Laplacian variance by g^2 with no change in detail, so judge sharpness net of it
    out['lap_ratio_gain_corrected'] = float(out['lap_ratio'] / (float(a) * float(a))) if (out['lap_ratio'] is not None and a != 0) else None
    # shift: windows (coordinates relative to the band strip)
    wins = []
    for (y, x) in window_grid(old.width, 0, bottom - top):
        wo = o[y:y + WIN, x:x + WIN]
        wn = n[y:y + WIN, x:x + WIN]
        dy, dx, peak, sdy, sdx = phase_correlate(wo, wn)
        unsh = float(np.abs(wn - wo).mean())
        wins.append({'y': y + top, 'x': x, 'dy': dy, 'dx': dx, 'dy_sub': sdy, 'dx_sub': sdx, 'peak': peak,
                     'mae_unshifted': unsh,
                     'mae_shifted': shifted_mae(wo, wn, dy, dx) if (dy or dx) else unsh})
    locked = [w for w in wins if w['peak'] >= LOCK_PEAK]
    summ = {'n_windows': len(wins), 'n_locked': len(locked)}
    if locked:
        dys = np.array([w['dy'] for w in locked])
        dxs = np.array([w['dx'] for w in locked])
        mdy = float(np.median(dys))
        mdx = float(np.median(dxs))
        summ.update({'median_dy': mdy, 'median_dx': mdx,
                     'median_dy_sub': float(np.median([w['dy_sub'] for w in locked])),
                     'median_dx_sub': float(np.median([w['dx_sub'] for w in locked])),
                     'max_abs_sub': float(max(max(abs(w['dy_sub']), abs(w['dx_sub'])) for w in locked)),
                     'max_abs_dy': int(np.abs(dys).max()), 'max_abs_dx': int(np.abs(dxs).max()),
                     'n_locked_moved': int(((np.abs(dys) >= 1) | (np.abs(dxs) >= 1)).sum()),
                     'n_locked_agree': int(((np.abs(dys - mdy) <= 1) & (np.abs(dxs - mdx) <= 1)).sum()),
                     'peak_median': float(np.median([w['peak'] for w in locked]))})
    else:
        summ.update({'median_dy': None, 'median_dx': None, 'median_dy_sub': None, 'median_dx_sub': None,
                     'max_abs_sub': None, 'max_abs_dy': None, 'max_abs_dx': None,
                     'n_locked_moved': 0, 'n_locked_agree': 0, 'peak_median': None})
    out['shift'] = summ
    out['windows'] = wins
    return out


def classify(rec):
    """One primary label per panorama from the horizon band (the control band, and the textured one)."""
    h = rec['horizon']
    s = h['shift']
    if s['n_locked'] == 0:
        return 'no-lock'
    moved = s['n_locked_moved'] / s['n_locked']
    agree = s['n_locked_agree'] / s['n_locked']
    if moved >= 0.5 and agree >= 0.75 and max(abs(s['median_dy_sub']), abs(s['median_dx_sub'])) >= 1.0:
        return 'shifted'
    if moved >= 0.5 and agree < 0.75:
        return 'warped'
    if not rec['rerendered']:
        return 'same'
    af = h['affine']
    regraded = af['mae_before'] > 0 and af['mae_after'] / af['mae_before'] <= 0.5 and \
        (abs(af['gain'] - 1) >= 0.02 or abs(af['offset']) >= 1.0)
    sharpened = h['lap_ratio_gain_corrected'] is not None and h['lap_ratio_gain_corrected'] >= 1.2
    if sharpened and regraded:
        return 'sharpened+regraded'
    if sharpened:
        return 'sharpened'
    if regraded:
        return 'regraded'
    return 'changed-unclassified'


def measure_one(args):
    pano_id, old_path, new_path, rerendered, pilot_horizon_mae = args
    with Image.open(old_path) as old, Image.open(new_path) as new:
        if old.size != new.size:
            return {'pano_id': pano_id, 'error': 'size mismatch'}
        bands = band_pixel_rows(new.height)
        if bands is None:
            return {'pano_id': pano_id, 'error': 'unswept geometry'}
        rec = {'pano_id': pano_id, 'width': new.width, 'height': new.height,
               'rerendered': rerendered, 'pilot_horizon_mae': pilot_horizon_mae}
        for name in ('horizon', 'bottom'):
            top, bottom = bands[name]
            rec[name] = measure_band(old, new, top, bottom)
    rec['label'] = classify(rec)
    return rec


def replaced_ids(ledger):
    with open(ledger, newline='') as f:
        return [r[0] for r in csv.reader(f) if len(r) == 2 and r[1] == 'replaced']


def stored_path(store, pid):
    return os.path.join(store, pid[:2], pid + '.jpg')


def _med(vals):
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else None


def summarise(records):
    ok = [r for r in records if 'error' not in r]
    groups = {'rerendered': [r for r in ok if r['rerendered']], 'same': [r for r in ok if not r['rerendered']]}
    out = {'measured': len(ok), 'errors': [r['pano_id'] for r in records if 'error' in r],
           'thresholds': {'lock_peak': LOCK_PEAK, 'window_px': WIN, 'rerendered_horizon_mae': RERENDERED_HORIZON_MAE}}
    for g, rs in groups.items():
        labels = {}
        for r in rs:
            labels[r['label']] = labels.get(r['label'], 0) + 1
        gs = {'n': len(rs), 'labels': labels}
        for band in ('horizon', 'bottom'):
            sh = [r[band]['shift'] for r in rs]
            gs[band] = {
                'mae_median': _med([r[band]['mae'] for r in rs]),
                'mean_signed_diff_median': _med([r[band]['mean_signed_diff'] for r in rs]),
                'affine_mae_after_median': _med([r[band]['affine']['mae_after'] for r in rs]),
                'affine_gain_median': _med([r[band]['affine']['gain'] for r in rs]),
                'affine_offset_median': _med([r[band]['affine']['offset'] for r in rs]),
                'lap_ratio_median': _med([r[band]['lap_ratio'] for r in rs]),
                'lap_ratio_gain_corrected_median': _med([r[band]['lap_ratio_gain_corrected'] for r in rs]),
                'locked_fraction_median': _med([s['n_locked'] / s['n_windows'] for s in sh]),
                'peak_median': _med([s['peak_median'] for s in sh]),
                'max_abs_shift_px_max': max([max(s['max_abs_dy'] or 0, s['max_abs_dx'] or 0) for s in sh]) if sh else None,
                'max_abs_sub_shift_median': _med([s['max_abs_sub'] for s in sh]),
                'n_with_any_locked_window_moved': sum(1 for s in sh if s['n_locked_moved'] > 0),
            }
        out[g] = gs
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--old-store', required=True)
    p.add_argument('--new-store', required=True)
    p.add_argument('--ledger', required=True)
    p.add_argument('--pilot-json', required=True)
    p.add_argument('--write', required=True)
    p.add_argument('--workers', type=int, default=3)
    a = p.parse_args(argv)
    _selftest()
    with open(a.pilot_json) as f:
        pilot = {r['pano_id']: r['horizon']['mae_old_vs_new'] for r in json.load(f)['records']}
    jobs = []
    for pid in replaced_ids(a.ledger):
        hm = pilot.get(pid)
        jobs.append((pid, stored_path(a.old_store, pid), stored_path(a.new_store, pid),
                     hm is not None and hm > RERENDERED_HORIZON_MAE, hm))
    records = []
    with Pool(a.workers) as pool:
        for rec in pool.imap_unordered(measure_one, jobs):
            records.append(rec)
            if 'error' in rec:
                print('%s ERROR %s' % (rec['pano_id'], rec['error']), flush=True)
                continue
            h = rec['horizon']
            print('%s %s %-20s horizon mae %6.2f lap %.2f lock %2d/%2d shift(dy,dx)=(%s,%s) sub(%.2f,%.2f) affine a=%.3f b=%+.1f resid %.2f'
                  % (rec['pano_id'], 'RE ' if rec['rerendered'] else 'same', rec['label'], h['mae'], h['lap_ratio'] or -1,
                     h['shift']['n_locked'], h['shift']['n_windows'], h['shift']['median_dy'], h['shift']['median_dx'],
                     h['shift']['median_dy_sub'] or 0.0, h['shift']['median_dx_sub'] or 0.0,
                     h['affine']['gain'], h['affine']['offset'], h['affine']['mae_after']), flush=True)
    records.sort(key=lambda r: (not r.get('rerendered', False), -(r.get('pilot_horizon_mae') or 0)))
    s = summarise(records)
    with open(a.write, 'w') as f:
        json.dump({'question': 'Did the re-render move features (re-stitch), change tone (re-grade), or change '
                               'sharpness in place? (#114)',
                   'method': __doc__, 'summary': s, 'records': records}, f, indent=1, allow_nan=False, default=float)
    print(json.dumps(s, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
