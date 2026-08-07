"""Regenerate the CBK tile fixtures and measurements in this directory.

    python tests/fixtures/tiles/capture.py

Hits Google directly, so it is a deliberate manual step, not part of the suite. Run it when Google's
behaviour needs re-checking, or when a fixture has to be replaced. It rewrites every .jpg here, plus
manifest.json and fover_band_map.json, and prints what it found.

Background: reports/2026-08-07-cbk-tile-resolution.md. The behaviours captured here are asserted by
tests/test_gsv_tile_contract.py, so a re-capture that changes them will fail the suite - which is the point.
Expect roughly 1800 tile requests and a couple of minutes.
"""

import asyncio
import hashlib
import io
import json
import os
import sys
import time
from collections import Counter

import aiohttp
import numpy as np
import requests
from PIL import Image

OUT = os.path.dirname(os.path.abspath(__file__))

# No fover here on purpose: this script has to be able to request BOTH ways, so the parameter is explicit at
# every call site rather than baked into the base URL.
BASE = 'https://maps.google.com/cbk?output=tile&cb_client=maps_sv&onerr=3&renderer=spherical&v=4'
MODERN = ('https://streetviewpixels-pa.googleapis.com/v1/tile?cb_client=maps_sv.tactile'
          '&panoid={p}&x={x}&y={y}&zoom={z}')
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
SESSION = requests.Session()

PANO = 'Svz6_7CwyijJ6RgjWROnCw'          # Seattle, 2022-09, 16384x8192, max zoom 5
OLD_PANO = 'cFou_FaIrbvqN0kcS5QuxA'      # Sydney, 2014-11, 13312x6656 -> zoom 3 is 3328x1664
SWEEP = [('Seattle 2022', PANO, 32, 16), ('NYC 2024', 'ywskIOsAFskiKHt5fwIUWA', 32, 16),
         ('Sydney 2014', OLD_PANO, 26, 13), ('Tokyo 2018', 'XlVh96-Z9lAI5tKrU2O4Yg', 26, 13)]

# The zoom-5 grid rows that were full-resolution when this was written, per geometry. Used only to label the
# halving-cost table; the sweep above measures the band itself.
HORIZON_BAND = {16: range(5, 11), 13: range(4, 9)}

MIXED_BLOCK = [(8, 10), (9, 10), (8, 11), (9, 11)]   # straddles the band boundary on a 32x16 grid
Z4_CELL = (4, 5)
FOVER_TILE = (4, 2)                                   # a polar row: inside the band fover halves


def url(pano, z, x, y, fover=''):
    return '%s%s&zoom=%d&x=%d&y=%d&panoid=%s' % (BASE, fover, z, x, y, pano)


def fetch(u, tries=6):
    for attempt in range(tries):
        try:
            body = SESSION.get(u, headers=UA, timeout=30).content
            Image.open(io.BytesIO(body)).size          # decodable?
            return body
        except Exception:
            time.sleep(1)
    raise RuntimeError('could not fetch %s' % u)


def image(body):
    return Image.open(io.BytesIO(body)).convert('RGB')


def grab(pano, z, x, y, want, fover='', tries=400):
    """Fetch until the body arrives at `want`. With fover the size is fixed per row, so this returns at once
    for a matching (row, parameter) pair and gives up if asked for an impossible combination."""
    for _ in range(tries):
        body = fetch(url(pano, z, x, y, fover))
        if image(body).size == want:
            return body
    raise RuntimeError('never saw %s at zoom %d (%d,%d) fover=%r' % (want, z, x, y, fover))


async def _afetch(session, u, tries=5):
    """Retried, so a transient fetch failure cannot show up as a phantom "mixed" row in the band map. The
    row structure is the finding here; a dropped connection is not part of it."""
    for attempt in range(tries):
        try:
            async with session.get(u, headers=UA) as r:
                return Image.open(io.BytesIO(await r.read())).size
        except Exception:
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _sweep(pano, cols, rows, fover):
    conn = aiohttp.TCPConnector(limit=16)
    async with aiohttp.ClientSession(connector=conn) as s:
        out = []
        for y in range(rows):
            sizes = Counter(await asyncio.gather(*[_afetch(s, url(pano, 5, x, y, fover))
                                                   for x in range(cols)]))
            out.append({'y': y, 'sizes': {str(k): v for k, v in sizes.items()}})
        return out


def row_glyphs(rows_data, cols):
    return ''.join('1' if r['sizes'].get('(256, 256)') == cols
                   else '2' if r['sizes'].get('(512, 512)') == cols else '?' for r in rows_data)


def halving_cost(im):
    """How much real detail halving this body would destroy: halve, re-expand, measure the difference."""
    g = im.convert('L')
    half = g.resize((g.width // 2, g.height // 2), Image.LANCZOS)
    return float(np.abs(np.asarray(g, float)
                        - np.asarray(half.resize(g.size, Image.LANCZOS), float)).mean())


def write(name, body):
    with open(os.path.join(OUT, name), 'wb') as f:
        f.write(body)
    return {'bytes': len(body), 'body_size': list(image(body).size)}


def main():
    manifest = {'pano': PANO, 'old_pano': OLD_PANO, 'z4_cell': list(Z4_CELL), 'tiles': {},
                'captured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'fover': {'finding': 'fover=1/2/3 makes CBK serve the polar rows of a zoom-5 grid at '
                                     '256x256; fover=0 or omitting it does not; onerr is innocent. '
                                     'Isolated by misaugstad on #73.',
                          'dropped_from_cbk_url': '2026-08-07'}}

    print('1. the same tile, with and without fover')
    with_fover = grab(PANO, 5, FOVER_TILE[0], FOVER_TILE[1], (256, 256), '&fover=2')
    without = grab(PANO, 5, FOVER_TILE[0], FOVER_TILE[1], (512, 512), '')
    modern = fetch(MODERN.format(p=PANO, z=5, x=FOVER_TILE[0], y=FOVER_TILE[1]))
    identical = hashlib.md5(without).hexdigest() == hashlib.md5(modern).hexdigest()
    print('   fover=2 %s | no fover %s | byte-identical to streetviewpixels-pa: %s'
          % (image(with_fover).size, image(without).size, identical))
    for name, body in (('z5_fover2_%d_%d.jpg' % FOVER_TILE, with_fover),
                       ('z5_nofover_%d_%d.jpg' % FOVER_TILE, without)):
        manifest['tiles'][name] = dict(write(name, body), pano=PANO, zoom=5,
                                       x=FOVER_TILE[0], y=FOVER_TILE[1],
                                       note='same grid position, one URL parameter apart; y=%d is inside '
                                            'the polar band fover halves (#73)' % FOVER_TILE[1])

    print('2. a mixed 2x2 block at the band boundary, plus the zoom-4 tile covering it')
    for (x, y) in MIXED_BLOCK:
        want = (512, 512) if y in HORIZON_BAND[16] else (256, 256)
        label = 'full' if want == (512, 512) else 'degraded'
        name = 'z5_%s_%d_%d.jpg' % (label, x, y)
        manifest['tiles'][name] = dict(write(name, grab(PANO, 5, x, y, want, '&fover=2')),
                                       pano=PANO, zoom=5, x=x, y=y)
        print('   %-24s %s' % (name, want))
    name = 'z4_cover_%d_%d.jpg' % Z4_CELL
    manifest['tiles'][name] = dict(write(name, fetch(url(PANO, 4, Z4_CELL[0], Z4_CELL[1]))),
                                   pano=PANO, zoom=4, x=Z4_CELL[0], y=Z4_CELL[1],
                                   note='covers the same region as the four zoom-5 tiles above; CBK zoom 4 '
                                        'is 8192 wide, so it is also the half-scale ground truth')

    print('3. a real black-padded edge tile and a real out-of-range blank')
    edge = fetch(url(OLD_PANO, 3, 6, 3))
    black_rows = int((np.asarray(image(edge).convert('L')).max(axis=1) == 0).sum())
    manifest['tiles']['z3_edge_bottom.jpg'] = dict(
        write('z3_edge_bottom.jpg', edge), pano=OLD_PANO, zoom=3, x=6, y=3,
        note='bottom-right tile of a 3328x1664 zoom-3 image; %d of its 512 rows are black padding, so '
             'Google pads short edge tiles rather than returning a true-size body' % black_rows)
    blank = fetch(url(OLD_PANO, 3, 20, 0))
    manifest['tiles']['z3_blank_out_of_range.jpg'] = dict(
        write('z3_blank_out_of_range.jpg', blank), pano=OLD_PANO, zoom=3, x=20, y=0,
        note='x=20 is far past the 7-column zoom-3 grid; Google still answers 200 OK with a valid, '
             'all-black image/jpeg - which is why an out-of-range grid cannot be detected tile by tile')
    print('   edge tile has %d black padding rows; blank tile extrema %s'
          % (black_rows, image(blank).convert('L').getextrema()))

    print('4. full zoom-5 grid sweeps, with fover=2')
    bands = {}
    for label, pano, cols, rows in SWEEP:
        data = asyncio.run(_sweep(pano, cols, rows, '&fover=2'))
        degraded = sum(r['sizes'].get('(256, 256)', 0) for r in data)
        bands[label] = {'pano_id': pano, 'grid': [cols, rows], 'row_map': row_glyphs(data, cols),
                        'degraded_tiles': degraded, 'total_tiles': cols * rows, 'rows': data}
        print('   %-13s %s  %d/%d degraded' % (label, bands[label]['row_map'], degraded, cols * rows))

    print('5. the same sweep without fover')
    data = asyncio.run(_sweep(PANO, 32, 16, ''))
    bands['Seattle 2022 (no fover)'] = {
        'pano_id': PANO, 'grid': [32, 16], 'row_map': row_glyphs(data, 32),
        'degraded_tiles': sum(r['sizes'].get('(256, 256)', 0) for r in data),
        'total_tiles': 512, 'rows': data}
    print('   Seattle no-fover %s  %d/512 degraded'
          % (bands['Seattle 2022 (no fover)']['row_map'],
             bands['Seattle 2022 (no fover)']['degraded_tiles']))

    print('6. what halving each row would cost')
    cols_sampled = (2, 8, 14, 20, 26)
    costs = {}
    for label, pano, _cols, rows in SWEEP[:1] + SWEEP[2:3]:
        per_row = {}
        for y in range(rows):
            cost = float(np.mean([halving_cost(image(fetch(url(pano, 5, x, y))))
                                  for x in cols_sampled]))
            per_row[y] = {'cost': round(cost, 4),
                          'zone': 'horizon_band_fover_left_alone' if y in HORIZON_BAND[rows]
                                  else 'polar_fover_halved_this'}
        pole = [v['cost'] for v in per_row.values() if v['zone'].startswith('polar')]
        hor = [v['cost'] for v in per_row.values() if v['zone'].startswith('horizon')]
        costs[label] = {'pano_id': pano, 'columns_sampled': list(cols_sampled), 'per_row': per_row,
                        'mean_polar_cost': round(float(np.mean(pole)), 4),
                        'mean_horizon_cost': round(float(np.mean(hor)), 4),
                        'horizon_is_x_more_costly_to_halve': round(float(np.mean(hor) / np.mean(pole)), 2)}
        print('   %-13s polar %.3f  horizon %.3f  -> horizon costs %.1fx more to halve'
              % (label, np.mean(pole), np.mean(hor), np.mean(hor) / np.mean(pole)))

    payload = {
        'captured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'legend': '1 = every tile in the row came back 256x256, 2 = every tile 512x512, ? = mixed',
        'note': 'Full zoom-5 grid sweeps. With fover the half-res rows form two polar bands; without it the '
                'band is gone. Row index runs top (y=0) to bottom.',
        'bands': bands,
        'halving_cost_by_row': {
            'metric': 'mean |pixel - (halve then double with LANCZOS)| on the luma of a full 512 body; how '
                      'much real detail halving that row would destroy',
            'why': 'fover halved exactly the polar rows. Equirectangular oversamples the poles, so those '
                   'rows carry the least real detail per pixel - the optimisation is well targeted, which '
                   'bounds how much the existing store actually lost (#73).',
            'measured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'panos': costs},
    }
    with open(os.path.join(OUT, 'fover_band_map.json'), 'w') as f:
        json.dump(payload, f, indent=1)
    with open(os.path.join(OUT, 'manifest.json'), 'w') as f:
        json.dump(manifest, f, indent=1)

    total = sum(os.path.getsize(os.path.join(OUT, n)) for n in os.listdir(OUT) if not n.endswith('.py'))
    print('\nwrote %d fixture files, %.0f KB total'
          % (len(manifest['tiles']) + 2, total / 1024.0))
    print('now run: python -m pytest tests/test_gsv_tile_contract.py')


if __name__ == '__main__':
    sys.exit(main())
