"""Did the re-fetch make the polar band SHARPER? A direct resolution measure for the `fover` pilot (#73).

    python reports/scripts/refetch_pilot_sharpness.py --old-store <production city dir> --new-store <pilot copy> \\
        --ledger <pilot copy>/refetch_log.csv [--write reports/data/<date>-fover-refetch-pilot-sharpness.json]

`refetch_panos.py --measure` records the mean absolute difference between the stored frame and the fresh
one, per band, with the horizon band as a control for our own JPEG round-trip. The first pilot showed that
control to be imperfect in a way the metric cannot correct for: JPEG re-encode error scales with texture,
the horizon band carries several times the texture of the road-surface bottom band, and so the "noise" read
at the horizon overstates the noise in the band being measured - the headline came out negative. And whole-
band MAE is a blunt instrument for resolution in the first place: a 2x upscale changes pixels only near
edges, and averaging over a smooth road surface buries that.

This asks the question the other way round, on the same panoramas. Resolution is high-frequency content,
so measure that directly: the variance of a 4-neighbour Laplacian over each band, in the stored file and in
the re-fetched one. An upscaled-from-256 band has had its highest octave removed, so its Laplacian variance
is lower than a native band's; a native band re-encoded at the same JPEG quality should have about the same.
The horizon band is again the control - both eras served it at full size, so its ratio is what "no change"
looks like - and this time it is like-for-like in the way the MAE control was not, because a ratio within a
band does not depend on that band's texture level.

Read what the control actually is, though, before leaning on it. This script compares two files on disk,
and `n_horizon_identical` counts the panoramas whose horizon band came back **bit-identical**: Google
returned the same tile bodies, the stitch is a paste, and our JPEG encode is deterministic, so the stored
file's horizon rows are reproduced exactly. For those panoramas the control is an identity rather than a
measurement - which is the strongest possible statement about the OTHER band (no encoder difference can be
hiding in a bottom-band ratio measured on the same pair) and no statement at all about how much a JPEG
re-encode on its own would move a low-texture band's Laplacian variance. The committed tile pair
(tests/test_refetch_pilot_report.py, section 5) is what settles that second question; this script does not.

Note also that this metric and `refetch_panos --measure` are not computed on the same pair. `--measure`
compares the stored file against the fresh stitch held in memory, BEFORE the save; this compares it against
the saved file. One JPEG encode apart, which is why the same band on the same panorama can read 1.36 luma
of difference there and bit-identical here.

Reported per panorama, in pixels as stored, and summarised as the median new/old ratio per band. Needs both
stores on the same machine and decodes two full frames per panorama, so it runs where the pilot ran.
"""

import argparse
import csv
import json
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.dirname(HERE)
REPO = os.path.dirname(REPORTS)
for _p in (HERE, REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from studyfmt import display_path, fmt, num, percentile  # noqa: E402
from refetch_panos import band_pixel_rows  # noqa: E402

Image.MAX_IMAGE_PIXELS = None       # a real pano is past Pillow's decompression-bomb ceiling


def laplacian_variance(strip):
    """Variance of the 4-neighbour Laplacian over `strip` (float32 luma), interior pixels only.

    The Laplacian is the classic no-reference sharpness measure: it responds to the highest spatial
    frequencies present, which are exactly what a 2x upscale cannot contain. The border row/column is
    dropped rather than padded so the statistic is not skewed by an artificial edge.
    """
    c = strip[1:-1, 1:-1]
    lap = 4.0 * c - strip[:-2, 1:-1] - strip[2:, 1:-1] - strip[1:-1, :-2] - strip[1:-1, 2:]
    return float(lap.var())


def band_luma(image, top, bottom):
    return np.asarray(image.crop((0, top, image.width, bottom)).convert('L'), dtype=np.float32)


def measure_pair(old_path, new_path):
    """Per-band Laplacian variance for the stored and re-fetched frames, or None for an unswept geometry or
    a size mismatch (nothing to compare like-for-like)."""
    with Image.open(old_path) as old, Image.open(new_path) as new:
        if old.size != new.size:
            return None
        bands = band_pixel_rows(new.height)
        if bands is None:
            return None
        out = {'width': new.width, 'height': new.height}
        for name, (top, bottom) in zip(('bottom', 'horizon'), bands):
            lv_old = laplacian_variance(band_luma(old, top, bottom))
            lv_new = laplacian_variance(band_luma(new, top, bottom))
            out[name] = {'rows_px': [top, bottom], 'lap_var_old': lv_old, 'lap_var_new': lv_new,
                         'ratio_new_over_old': (lv_new / lv_old) if lv_old > 0 else None}
    return out


def replaced_ids(ledger_path):
    with open(ledger_path, newline='') as f:
        return [row[0] for row in csv.reader(f) if len(row) == 2 and row[1] == 'replaced']


def stored_path(store, pano_id):
    return os.path.join(store, pano_id[:2], pano_id + '.jpg')


def summarise(records):
    """Median and p10/p90 of the new/old ratio per band, and how many panoramas moved in each direction.

    The decision-relevant comparison is bottom against horizon: the horizon ratio is what the re-fetch does
    to a band whose imagery did not change, so the bottom ratio net of it is the sharpening the re-fetch
    actually delivered. `n_bottom_sharper_than_horizon` counts panoramas where the band that was halved
    gained more than the band that was not.

    `n_equal_lap_var` per band is what keeps that reading honest, and is reported rather than left to a
    ratio rounded to 1.000: it counts the panoramas whose two frames give the same float32 variance to the
    last bit. Over a band of tens of millions of pixels that is not a coincidence - it means the band's
    pixels are identical - so those ratios are 1 by construction. A horizon `n_equal_lap_var` that is a
    large share of `measured` says the tile bodies came back unchanged and our encode is deterministic, NOT
    that an encode leaves the statistic alone. See the module docstring.
    """
    out = {'measured': len(records)}
    for band in ('bottom', 'horizon'):
        ratios = [r[band]['ratio_new_over_old'] for r in records if r[band]['ratio_new_over_old'] is not None]
        out[band] = {
            'ratio_p10': num(percentile(ratios, 0.10)) if ratios else None,
            'ratio_median': num(percentile(ratios, 0.50)) if ratios else None,
            'ratio_p90': num(percentile(ratios, 0.90)) if ratios else None,
            'n_sharper': sum(1 for v in ratios if v > 1.0),
            'n_equal_lap_var': sum(1 for r in records if r[band]['lap_var_old'] == r[band]['lap_var_new']),
        }
    paired = [(r['bottom']['ratio_new_over_old'], r['horizon']['ratio_new_over_old']) for r in records
              if r['bottom']['ratio_new_over_old'] is not None and r['horizon']['ratio_new_over_old'] is not None]
    out['n_bottom_sharper_than_horizon'] = sum(1 for b, h in paired if b > h)
    net = [b / h for b, h in paired if h > 0]
    out['bottom_over_horizon_ratio_median'] = num(percentile(net, 0.50)) if net else None
    return out


def build_parser():
    parser = argparse.ArgumentParser(description='Laplacian-variance sharpness, stored vs re-fetched, per band (#73).')
    parser.add_argument('--old-store', required=True, help='The store holding the ORIGINAL frames (production).')
    parser.add_argument('--new-store', required=True, help='The pilot copy, holding the re-fetched frames.')
    parser.add_argument('--ledger', required=True, help='refetch_log.csv from the pilot; its replaced rows are measured.')
    parser.add_argument('--write', default=None, metavar='PATH', help='Write the artifact here.')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    records = []
    for pano_id in replaced_ids(args.ledger):
        measured = measure_pair(stored_path(args.old_store, pano_id), stored_path(args.new_store, pano_id))
        if measured is None:
            print('skipped %s (size mismatch or unswept geometry)' % pano_id)
            continue
        measured['pano_id'] = pano_id
        records.append(measured)
        print('%s bottom %.3f horizon %.3f' % (pano_id, measured['bottom']['ratio_new_over_old'] or float('nan'),
                                              measured['horizon']['ratio_new_over_old'] or float('nan')))
    s = summarise(records)
    print('\n%d measured | Laplacian variance new/old, median: bottom %s  horizon %s | bottom sharper than '
          'horizon in %d | band unchanged (equal variance): bottom %d, horizon %d'
          % (s['measured'], fmt(s['bottom']['ratio_median'], '.3f'), fmt(s['horizon']['ratio_median'], '.3f'),
             s['n_bottom_sharper_than_horizon'],
             s['bottom']['n_equal_lap_var'], s['horizon']['n_equal_lap_var']))
    if args.write:
        payload = {
            'question': 'Did re-fetching a fover-era panorama make its polar band sharper, measured directly '
                        'rather than through a mean absolute difference? (#73)',
            'method': 'Variance of the 4-neighbour Laplacian over each band, stored frame vs re-fetched frame, '
                      'as a new/old ratio. The horizon band, served at full size in both eras, is the '
                      'control. n_equal_lap_var says how much of that control is an identity rather than a '
                      'measurement: where it counts a panorama, the band came back bit-identical, so no '
                      'encoder difference can be hiding in the same pair\'s bottom-band ratio - and nothing '
                      'here bounds what an encode alone would do to a low-texture band. Both frames are '
                      'files on disk, one JPEG encode further on than the pair refetch_panos --measure '
                      'compares.',
            'summary': s,
            'records': records,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.write)), exist_ok=True)
        with open(args.write, 'w') as f:
            json.dump(payload, f, indent=1, allow_nan=False)
        print('wrote %s' % display_path(args.write, REPO))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
