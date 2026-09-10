"""Capture date and pose for the pilot's 78 swapped panoramas, via photometa (#114).

    python rerender_photometa.py --ledger <pilot copy>/refetch_log.csv --pilot-json <pilot.json> --write out.json
"""
import argparse
import csv
import json
import math
import time

from streetlevel import streetview


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--ledger', required=True)
    p.add_argument('--pilot-json', required=True)
    p.add_argument('--write', required=True)
    p.add_argument('--interval', type=float, default=1.0)
    a = p.parse_args()
    with open(a.pilot_json) as f:
        pilot = {r['pano_id']: r['horizon']['mae_old_vs_new'] for r in json.load(f)['records']}
    with open(a.ledger, newline='') as f:
        ids = [r[0] for r in csv.reader(f) if len(r) == 2 and r[1] == 'replaced']

    def deg(v):
        return None if v is None else math.degrees(float(v))

    out = []
    for i, pid in enumerate(ids):
        rec = {'pano_id': pid, 'pilot_horizon_mae': pilot.get(pid),
               'rerendered': pilot.get(pid) is not None and pilot[pid] > 3.0}
        try:
            pano = streetview.find_panorama_by_id(pid, download_depth=False)
            if pano is None:
                rec.update(found=False)
            else:
                d = getattr(pano, 'date', None)
                big = pano.image_sizes[-1]
                rec.update(found=True, capture_date=(f'{d.year:04d}-{d.month:02d}' if d else None),
                           served_width=int(big.x), served_height=int(big.y),
                           heading_deg=deg(getattr(pano, 'heading', None)),
                           pitch_deg=deg(getattr(pano, 'pitch', None)), roll_deg=deg(getattr(pano, 'roll', None)),
                           lat=getattr(pano, 'lat', None), lon=getattr(pano, 'lon', None),
                           source=getattr(pano, 'source', None), copyright=getattr(pano, 'copyright_message', None),
                           n_historical=len(getattr(pano, 'historical', None) or []))
        except Exception as e:  # noqa: BLE001 - recorded, never fatal
            rec.update(found=None, error=f'{type(e).__name__}: {e}')
        out.append(rec)
        print(i + 1, pid, rec.get('capture_date'), rec.get('found'), rec.get('error', ''), flush=True)
        time.sleep(a.interval)
    with open(a.write, 'w') as f:
        json.dump(out, f, indent=1)


if __name__ == '__main__':
    main()
