"""Backup-store coverage: can the lab's pano store supply the imagery Google has dropped, and is
it in the frame the label was placed in?

Two questions the Phase 2 corpus depends on and that no desk source answers:

1. **Coverage.** The photometa census measured 47.9% pano survival at Google, so a Google-only
   corpus loses half its labels and skews new. The pre-registration therefore sources dead panos
   from a backup store. Nobody had measured what that store actually holds, so the over-draw
   factors in the corpus spec were calibrated against Google survival -- the wrong constraint if
   the store is the real source.

2. **Frame.** The photometa census's "0.0% dims drift" compared gsv_data's stored dims against
   GOOGLE's served dims. Neither of those is the JPEG a crop is cut from. This compares the store's
   own JPEG against both, which is what #77's dims preflight is actually about.

The sample is not drawn here: it is read straight out of the committed photometa census, so the
same 1,360 panos are probed and the two studies are directly cross-tabulatable (alive-at-Google x
present-on-store). Dimensions come from the JPEG's SOF header only -- no decode, no Pillow -- so a
15 TB store can be swept without reading pixel data.

Runs where the store is mounted (makelab2), not from a laptop:
    python store_coverage.py /m-makeabilitylab/makeabilitylab/sidewalk_panos/Panoramas \
        --census 2026-08-09-photometa-census.json --write 2026-08-10-store-coverage.json
"""

import argparse
import collections
import json
import os
import struct

# Start-of-frame markers whose payload carries the image dimensions. DHT/DAC/RST/SOS are excluded;
# 0xC4/0xC8/0xCC look like SOF numerically and are not.
SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})

# Markers that stand alone: no length field follows, so the scanner must not try to skip a segment.
STANDALONE = frozenset({0x01, 0xD8, 0xD9}) | frozenset(range(0xD0, 0xD8))


def jpeg_dimensions(path):
    """(width, height) from a JPEG's SOF header, or None if the file is not a readable JPEG.

    Header-only: a 10 MB equirectangular pano costs a few reads instead of a full decode, which is
    what makes a whole-store sweep practical. Returns None rather than raising, because a census
    must survive a truncated file at pano 1300 of 1400.
    """
    try:
        with open(path, 'rb') as f:
            if f.read(2) != b'\xff\xd8':
                return None
            while True:
                byte = f.read(1)
                while byte and byte != b'\xff':
                    byte = f.read(1)
                while byte == b'\xff':          # fill bytes: 0xFF may repeat before the marker
                    byte = f.read(1)
                if not byte:
                    return None
                marker = byte[0]
                if marker in STANDALONE:
                    continue
                header = f.read(2)
                if len(header) < 2:
                    return None
                seglen = struct.unpack('>H', header)[0]
                if marker in SOF_MARKERS:
                    body = f.read(5)
                    if len(body) < 5:
                        return None
                    height, width = struct.unpack('>HH', body[1:5])
                    return width, height
                if seglen < 2:
                    return None
                f.seek(seglen - 2, os.SEEK_CUR)
    except OSError:
        return None


def sample_from_census(census):
    """The probe list, taken verbatim from the committed photometa census's per-pano records so
    the two studies describe the same 1,360 panos."""
    rows = []
    for r in census['records']:
        rows.append({
            'pano_id': r['pano_id'], 'city': r['city'], 'era': r['era'],
            'alive_at_google': bool(r['found']),
            'stored_width': r['stored_width'], 'stored_height': r['stored_height'],
            'served_width': r['served_width'], 'served_height': r['served_height'],
        })
    return rows


def store_path(store_root, city, pano_id):
    """The store's layout: <city>/<pano_id[:2]>/<pano_id>.jpg."""
    return os.path.join(store_root, city, pano_id[:2], pano_id + '.jpg')


def probe(store_root, rows):
    """Per pano: is it on the store, how big is the file, and what frame is the JPEG actually in."""
    out = []
    for row in rows:
        path = store_path(store_root, row['city'], row['pano_id'])
        rec = dict(row, on_store=os.path.isfile(path),
                   store_width=None, store_height=None, file_bytes=None)
        if rec['on_store']:
            try:
                rec['file_bytes'] = os.path.getsize(path)
            except OSError:
                rec['file_bytes'] = None
            dims = jpeg_dimensions(path)
            if dims:
                rec['store_width'], rec['store_height'] = dims
        out.append(rec)
    return out


def _pct(a, b):
    return float(100.0 * a / b) if b else None


def summarize(records):
    """Coverage cross-tabbed by the census's alive/dead split, plus the frame comparison.

    Coverage is reported *by* alive-at-Google because that is the decision: the dead half is the
    half only the store can supply, so its coverage is the number the corpus spec turns on.
    """
    total = len(records)
    on = [r for r in records if r['on_store']]
    dead = [r for r in records if not r['alive_at_google']]
    alive = [r for r in records if r['alive_at_google']]

    def cov(rows):
        hit = sum(1 for r in rows if r['on_store'])
        return {'n': len(rows), 'on_store': hit, 'on_store_pct': _pct(hit, len(rows))}

    # Frame agreement, over panos on the store whose JPEG header parsed.
    read = [r for r in on if r['store_width'] and r['store_height']]
    def agree(w_key, h_key):
        pairs = [r for r in read if r[w_key] and r[h_key]]
        ok = sum(1 for r in pairs
                 if r['store_width'] == int(r[w_key]) and r['store_height'] == int(r[h_key]))
        return {'n': len(pairs), 'match': ok, 'match_pct': _pct(ok, len(pairs)),
                'differ': len(pairs) - ok}

    dims = collections.Counter((r['store_width'], r['store_height']) for r in read)
    mismatch = collections.Counter(
        (int(r['stored_width']), int(r['stored_height']), r['store_width'], r['store_height'])
        for r in read if r['stored_width'] and r['stored_height']
        and (r['store_width'] != int(r['stored_width'])
             or r['store_height'] != int(r['stored_height'])))
    sizes = sorted(r['file_bytes'] for r in on if r['file_bytes'])

    return {
        'n_sampled': total,
        'overall': cov(records),
        'dead_at_google': cov(dead),
        'alive_at_google': cov(alive),
        'dead_by_era': {era: cov([r for r in dead if r['era'] == era])
                        for era in sorted({r['era'] for r in dead})},
        'dead_by_city': {city: cov([r for r in dead if r['city'] == city])
                         for city in sorted({r['city'] for r in dead})},
        'headers_read': len(read),
        'headers_unreadable': len(on) - len(read),
        'frame_vs_gsv_data': agree('stored_width', 'stored_height'),
        'frame_vs_google_served': agree('served_width', 'served_height'),
        'store_dimensions': {f'{w}x{h}': c for (w, h), c in dims.most_common()},
        'frame_mismatches': {f'{sw}x{sh}->{w}x{h}': c
                             for (sw, sh, w, h), c in mismatch.most_common()},
        'file_mb': ({'p10': sizes[len(sizes) // 10] / 1048576,
                     'p50': sizes[len(sizes) // 2] / 1048576,
                     'p90': sizes[9 * len(sizes) // 10] / 1048576} if sizes else None),
        'dead_and_absent': [{'pano_id': r['pano_id'], 'city': r['city'], 'era': r['era']}
                            for r in dead if not r['on_store']],
    }


def write_json(result, path):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(result, f, indent=1, allow_nan=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('store_root')
    ap.add_argument('--census', required=True, help='the committed photometa census JSON')
    ap.add_argument('--probed', required=True, help='date of this probe (the store changes)')
    ap.add_argument('--write', metavar='JSON')
    args = ap.parse_args()

    with open(args.census, encoding='utf-8') as f:
        census = json.load(f)
    records = probe(args.store_root, sample_from_census(census))
    result = {'source': f'backup pano store at {args.store_root}',
              'probed': args.probed,
              'census': os.path.basename(args.census),
              'summary': summarize(records),
              'records': records}

    s = result['summary']
    print(f"sample {s['n_sampled']}  on store {s['overall']['on_store_pct']:.1f}%")
    print(f"  dead at Google : {s['dead_at_google']['on_store']}/{s['dead_at_google']['n']} "
          f"({s['dead_at_google']['on_store_pct']:.1f}%)")
    print(f"  alive at Google: {s['alive_at_google']['on_store']}/{s['alive_at_google']['n']} "
          f"({s['alive_at_google']['on_store_pct']:.1f}%)")
    print(f"  frame vs gsv_data: {s['frame_vs_gsv_data']['match_pct']:.1f}% match, "
          f"{s['frame_vs_gsv_data']['differ']} differ")
    print(f"  store dims: {s['store_dimensions']}")

    if args.write:
        write_json(result, args.write)
        print(f'wrote {args.write}')


if __name__ == '__main__':
    main()
