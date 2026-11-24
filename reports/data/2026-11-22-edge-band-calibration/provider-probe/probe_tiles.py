"""Ask the provider for the bottom tile row of the 18 unresolved panoramas (#179).

For each pano: three tiles of the last grid row (left, middle, right) and one control tile from the row above,
one request at a time, paced. Saves every body and writes probe.csv.

Usage: py -3.12 probe_tiles.py <unresolved.csv> <out-dir>
"""
import csv
import io
import os
import sys
import time

import numpy as np
import requests
from PIL import Image

BASE = 'https://maps.google.com/cbk?output=tile&cb_client=maps_sv&onerr=3&renderer=spherical&v=4'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'http://maps.google.com'}
src, out = sys.argv[1], sys.argv[2]
os.makedirs(os.path.join(out, 'tiles'), exist_ok=True)
rows = list(csv.DictReader(open(src, newline='')))
session = requests.Session()
with open(os.path.join(out, 'probe.csv'), 'w', newline='') as fh:
    w = csv.writer(fh)
    w.writerow(['city', 'pano_id', 'zoom', 'x', 'y', 'role', 'status', 'bytes', 'tile_w', 'tile_h',
                'black_frac', 'first_all_black_row', 'expected_black_from_row', 'max_below_expected'])
    for r in rows:
        width, height = int(r['width']), int(r['height'])
        zoom = 5 if width == 13312 else 3
        nx, ny = -(-width // 512), -(-height // 512)
        last = ny - 1
        real_rows_in_last = height - last * 512              # rows of the frame that fall in the last tile row
        band_px = round(float(r['bottom_band']) * height)
        expected = real_rows_in_last - band_px               # tile row where the stored band starts
        probes = [(0, last, 'bottom'), (nx // 2, last, 'bottom'), (nx - 1, last, 'bottom'), (nx // 2, last - 1, 'control')]
        for x, y, role in probes:
            url = '%s&zoom=%d&x=%d&y=%d&panoid=%s' % (BASE, zoom, x, y, r['pano_id'])
            try:
                resp = session.get(url, headers=HEADERS, timeout=30)
                status, body = resp.status_code, resp.content
            except requests.RequestException as e:
                w.writerow([r['city'], r['pano_id'], zoom, x, y, role, type(e).__name__] + [''] * 7)
                print(r['pano_id'], x, y, 'ERROR', type(e).__name__)
                time.sleep(2)
                continue
            stats = [''] * 6
            if status == 200 and body:
                open(os.path.join(out, 'tiles', '%s__%s_z%d_x%d_y%d.jpg' % (r['city'], r['pano_id'], zoom, x, y)), 'wb').write(body)
                try:
                    a = np.asarray(Image.open(io.BytesIO(body)).convert('L'))
                    allblack = (a == 0).all(axis=1)
                    nz = np.where(~allblack)[0]
                    first = int(nz.max()) + 1 if nz.size else 0
                    below = a[max(expected, 0):min(real_rows_in_last, a.shape[0])] if role == 'bottom' else a[:0]
                    stats = [a.shape[1], a.shape[0], round(float((a == 0).mean()), 4), first,
                             expected if role == 'bottom' else '', int(below.max()) if below.size else '']
                except Exception as e:  # noqa: BLE001 - a body that is not an image is itself the answer
                    stats = ['not-an-image:' + type(e).__name__] + [''] * 5
            w.writerow([r['city'], r['pano_id'], zoom, x, y, role, status, len(body)] + stats)
            fh.flush()
            print(r['city'], r['pano_id'], 'z%d x%d y%d' % (zoom, x, y), role, status, len(body), stats)
            time.sleep(1.5)
