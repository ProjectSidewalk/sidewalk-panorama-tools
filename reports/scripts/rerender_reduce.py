"""Reduce rerender_probe.json + rerender_photometa.json into the tables for #114."""
import json
import statistics as st
import sys

SP = sys.argv[1]
probe = json.load(open(SP + '/rerender_probe.json'))
meta = {r['pano_id']: r for r in json.load(open(SP + '/rerender_photometa.json'))}
recs = [r for r in probe['records'] if 'error' not in r]
errs = [r for r in probe['records'] if 'error' in r]
print('measured', len(recs), 'errors', errs)


def med(v):
    v = [x for x in v if x is not None]
    return st.median(v) if v else None


def row(r):
    h = r['horizon']
    b = r['bottom']
    s = h['shift']
    m = meta.get(r['pano_id'], {})
    return (r['pilot_horizon_mae'] or 0, r['pano_id'], r['width'], r['label'],
            h['mae'], s['n_locked'], s['n_windows'], s['median_dy_sub'], s['median_dx_sub'], s['max_abs_sub'],
            s['n_locked_agree'], s['n_locked_moved'], s['peak_median'],
            h['affine']['gain'], h['affine']['offset'], h['affine']['mae_after'],
            h['lap_ratio'], h['lap_ratio_gain_corrected'], b['mae'], b['lap_ratio_gain_corrected'],
            m.get('capture_date'), m.get('pitch_deg'), m.get('roll_deg'))


for grp, name in ((True, 'RE-RENDERED'), (False, 'SAME-RENDERING (null)')):
    rs = sorted([r for r in recs if r['rerendered'] == grp], key=lambda r: -(r['pilot_horizon_mae'] or 0))
    print('\n=== %s n=%d ===' % (name, len(rs)))
    labels = {}
    for r in rs:
        labels[r['label']] = labels.get(r['label'], 0) + 1
    print('labels:', labels)
    print('pilotMAE pano                   width label                 hMAE lock  sub(dy,dx)     maxsub agree moved peak  gain   offs  resid  lap  lapc  bMAE bLapc  capture  pitch  roll')
    for r in rs:
        t = row(r)
        print('%6.2f %s %5d %-20s %5.2f %2d/%2d (%+5.2f,%+5.2f) %5.2f %2d %2d %4.2f %5.3f %+5.1f %5.2f %4.2f %4.2f %5.2f %4.2f %s %+5.2f %+6.2f'
              % (t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7] or 0, t[8] or 0, t[9] or 0, t[10], t[11], t[12] or 0,
                 t[13], t[14], t[15], t[16] or 0, t[17] or 0, t[18], t[19] or 0, t[20], t[21] or 0, t[22] or 0))
    print('medians: hMAE %.2f  peak %.2f  |sub| max %.2f  affine resid %.2f  lap_corr %.2f  bottom lap_corr %.2f'
          % (med([r['horizon']['mae'] for r in rs]), med([r['horizon']['shift']['peak_median'] for r in rs]),
             med([r['horizon']['shift']['max_abs_sub'] for r in rs]), med([r['horizon']['affine']['mae_after'] for r in rs]),
             med([r['horizon']['lap_ratio_gain_corrected'] for r in rs]), med([r['bottom']['lap_ratio_gain_corrected'] for r in rs])))

# per-window detail for the re-rendered ones: does the shift vary with x (heading/pitch/roll) ?
print('\n=== per-window sub-pixel shifts, re-rendered, horizon band (row y: dx values across x; then dy) ===')
for r in sorted([r for r in recs if r['rerendered']], key=lambda r: -(r['pilot_horizon_mae'] or 0)):
    wins = r['horizon']['windows']
    ys = sorted(set(w['y'] for w in wins))
    print(r['pano_id'], r['label'])
    for y in ys:
        ws = [w for w in wins if w['y'] == y]
        print('  y=%5d dx: %s' % (y, ' '.join('%+5.1f' % w['dx_sub'] if w['peak'] >= 0.1 else '  ---' for w in ws)))
        print('          dy: %s' % (' '.join('%+5.1f' % w['dy_sub'] if w['peak'] >= 0.1 else '  ---' for w in ws)))
        print('        peak: %s' % (' '.join('%5.2f' % w['peak'] for w in ws)))
        print('  mae un/sh: %s' % (' '.join('%4.1f/%4.1f' % (w['mae_unshifted'], w['mae_shifted']) for w in ws)))
