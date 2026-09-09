"""What the Panoramax catalog actually serves, measured before Bayonne opens (#110).

Three questions the downloader's design turns on, none of which could be answered from the API docs:

1. **How does a picture id resolve, and what does absence look like?** A permanent `downloaded=0` row is
   forever (#41), so it has to rest on a body that AFFIRMS the picture is gone rather than on one that
   merely fails to mention it (#99). Two endpoints answer the same question differently, and only one of
   them can carry a verdict.
2. **Is everything in a Project Sidewalk city's bbox a 360 panorama?** It is a federated commons, not a
   fleet: anyone can contribute, and the answer decides whether the downloader needs a projection guard.
3. **Can the served frame's dimensions be known from the item?** `pano_data.width`/`height` come from the
   viewer, and the cropper's `dims_mismatch` preflight refuses any label whose metadata disagrees with the
   file on the store - so if the viewer and this downloader read different fields, every label in the city
   is skipped.

Keyless and read-only. The bbox default is Bayonne, the first Panoramax city
(SidewalkWebpage#5185, opening 2026-09-18).

    python reports/scripts/panoramax_api_census.py \
        --write reports/data/2026-09-08-panoramax-api.json

Every number in reports/2026-09-08-panoramax-api.md comes from the artifact this writes, and
tests/test_panoramax_api_census.py asserts that - a report table is the one place in this repo where a
plausible number has no compiler and no test behind it.
"""

import argparse
import collections
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from downloaders.common import jpeg_dimensions          # noqa: E402  (after the sys.path bootstrap)
from studyfmt import display_path, fmt, num             # noqa: E402

CATALOG_API_BASE = 'https://api.panoramax.xyz/api'
# Bayonne. Wide enough to include the whole commune and the contributor tracks that run through it, which
# is the point: the flat/360 mix is a property of the AREA a city covers, not of a curated collection.
BAYONNE_BBOX = '-1.52,43.45,-1.42,43.52'
PANORAMIC_FIELD_OF_VIEW_DEG = 360
# A syntactically valid UUID chosen to be absent. v4 with an all-zero body: the catalog has to answer it
# the same way it answers a real id it has never held, and it can never collide with a real picture.
ABSENT_UUID = '00000000-0000-4000-8000-000000000000'


def get_json(url, timeout=60):
    """The response body and status for a GET, without raising on a 4xx.

    A 404 is a MEASUREMENT here, not an error - the whole first question is what the catalog's absence
    answer looks like - so urllib's default "raise on any error status" would throw away the finding.
    """
    request = urllib.request.Request(url, headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {'_not_json': body[:200]}


def search(bbox, limit, base=CATALOG_API_BASE):
    """Every picture the catalog lists in `bbox`, following `rel: next` until `limit` is reached."""
    features, url = [], '%s/search?bbox=%s&limit=%d' % (base, bbox, min(limit, 1000))
    while url and len(features) < limit:
        _, page = get_json(url)
        features.extend(page.get('features') or [])
        url = next((link['href'] for link in page.get('links') or [] if link.get('rel') == 'next'), None)
    return features[:limit]


def field_of_view(item):
    """The item's declared horizontal fov, or None. Nested under `properties`, unlike `assets` - which is
    the asymmetry that made the downloader's first version of this read the top level and see nothing."""
    interior = (item.get('properties') or {}).get('pers:interior_orientation')
    return interior.get('field_of_view') if isinstance(interior, dict) else None


def sensor_dims(item):
    """`sensor_array_dimensions` as (width, height), or None. This is what PanoramaxViewer writes into
    `pano_data.width`/`height`, so it is what the cropper will compare the stored file against."""
    interior = (item.get('properties') or {}).get('pers:interior_orientation')
    if not isinstance(interior, dict):
        return None
    dims = interior.get('sensor_array_dimensions')
    if not (isinstance(dims, (list, tuple)) and len(dims) == 2):
        return None
    return int(dims[0]), int(dims[1])


def tile_matrix_dims(item):
    """The served frame's dimensions derived from the tile pyramid, or None.

    #110 recorded that the STAC item carries no dimensions. It does, indirectly: the tile matrix has to
    describe the full-resolution frame for the viewer's tiles adapter to address it, so
    matrixWidth x tileWidth IS the hd frame. That makes it an independent second opinion on
    sensor_array_dimensions, which is the only reason the agreement below is worth measuring.
    """
    tms = (item.get('properties') or {}).get('tiles:tile_matrix_sets')
    if not isinstance(tms, dict) or not isinstance(tms.get('geovisio'), dict):
        return None
    matrices = tms['geovisio'].get('tileMatrix')
    if not matrices:
        return None
    m = matrices[0]
    return int(m['matrixWidth'] * m['tileWidth']), int(m['matrixHeight'] * m['tileHeight'])


def asset_host(item, asset='hd'):
    href = ((item.get('assets') or {}).get(asset) or {}).get('href') or ''
    return href.split('/')[2] if href.startswith('https://') else None


def classify(features):
    """The bbox composition: how many pictures are panoramic, and what each arm is made of.

    Split by fov rather than by producer or collection, because fov is the only property that says whether
    the pixels can be treated as equirectangular - and a downloader can read it per picture without
    knowing anything about who contributed what.
    """
    panoramic = [f for f in features if field_of_view(f) == PANORAMIC_FIELD_OF_VIEW_DEG]
    other = [f for f in features if field_of_view(f) != PANORAMIC_FIELD_OF_VIEW_DEG]

    def arm(items):
        return {
            'count': len(items),
            'licenses': dict(collections.Counter((f.get('properties') or {}).get('license')
                                                 for f in items).most_common()),
            'producers': dict(collections.Counter((f.get('properties') or {}).get('geovisio:producer')
                                                  for f in items).most_common()),
            'asset_hosts': dict(collections.Counter(asset_host(f) for f in items).most_common()),
            'field_of_view_values': dict(collections.Counter(str(field_of_view(f))
                                                             for f in items).most_common()),
        }

    return {'examined': len(features), 'panoramic': arm(panoramic), 'other': arm(other),
            'panoramic_share_pct': num(100.0 * len(panoramic) / len(features)) if features else None}


def dimension_agreement(features):
    """Do `sensor_array_dimensions` and the tile matrix describe the same frame, over the panoramic arm?

    They are written by different parts of the catalog, and the viewer writes the first into `pano_data`
    while the downloader stores pixels whose size is the second. Any disagreement is a label skipped by
    the cropper's dims_mismatch preflight, so the interesting number is not the frame size but whether the
    two ever differ.
    """
    agree, disagree, missing, sizes, examples = 0, 0, 0, collections.Counter(), []
    for f in features:
        if field_of_view(f) != PANORAMIC_FIELD_OF_VIEW_DEG:
            continue
        sensor, matrix = sensor_dims(f), tile_matrix_dims(f)
        if matrix is not None:
            sizes['%dx%d' % matrix] += 1
        if sensor is None or matrix is None:
            missing += 1
        elif sensor == matrix:
            agree += 1
        else:
            disagree += 1
            if len(examples) < 5:
                examples.append({'id': f.get('id'), 'sensor': list(sensor), 'tile_matrix': list(matrix)})
    return {'agree': agree, 'disagree': disagree, 'not_stated': missing,
            'frame_sizes': dict(sizes.most_common()), 'disagreements': examples}


def probe_id_resolution(pano_id, base=CATALOG_API_BASE):
    """How a known id answers at each of the two endpoints that could resolve it."""
    picture_status, picture_body = get_json('%s/pictures/%s' % (base, pano_id))
    search_status, search_body = get_json('%s/search?ids=%s' % (base, pano_id))
    return {
        'pano_id': pano_id,
        'pictures_endpoint': {'status': picture_status, 'names_the_picture':
                              picture_body.get('id') == pano_id,
                              'hd_href': ((picture_body.get('assets') or {}).get('hd') or {}).get('href')},
        'search_endpoint': {'status': search_status,
                            'features_returned': len(search_body.get('features') or [])},
    }


def probe_absence(base=CATALOG_API_BASE, absent_uuid=ABSENT_UUID):
    """The finding the whole ledger design rests on: absence is a 404 at one endpoint and a 200 at the
    other, and only the 404 can ever found a permanent verdict (#99)."""
    picture_status, picture_body = get_json('%s/pictures/%s' % (base, absent_uuid))
    search_status, search_body = get_json('%s/search?ids=%s' % (base, absent_uuid))
    return {
        'absent_uuid': absent_uuid,
        'pictures_endpoint': {'status': picture_status, 'body': picture_body},
        'search_endpoint': {'status': search_status,
                            'features_returned': len(search_body.get('features') or [])},
    }


def probe_hd_headers(features, per_size=3, timeout=180):
    """Download the first bytes of some hd assets and read the frame size out of the JPEG itself.

    The one check the metadata cannot do for itself. `sensor_array_dimensions` and the tile matrix can
    agree with each other and both still be wrong about what the server sends, and a stored file whose
    size is not what pano_data claims is a label skipped per label, forever. Sampled per distinct frame
    size rather than at random, because a size that occurs 130 times out of 677 would otherwise mostly not
    be looked at.
    """
    by_size, checks = collections.defaultdict(list), []
    for f in features:
        matrix = tile_matrix_dims(f)
        if field_of_view(f) == PANORAMIC_FIELD_OF_VIEW_DEG and matrix is not None:
            by_size[matrix].append(f)
    for size, items in sorted(by_size.items()):
        for item in items[:per_size]:
            href = ((item.get('assets') or {}).get('hd') or {}).get('href')
            if not href:
                continue
            with urllib.request.urlopen(href, timeout=timeout) as response:
                data = response.read()
            tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.hd_probe.jpg')
            try:
                with open(tmp, 'wb') as out:
                    out.write(data)
                served = jpeg_dimensions(tmp)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            checks.append({'id': item.get('id'), 'host': asset_host(item),
                           'tile_matrix': list(size), 'jpeg_header': list(served) if served else None,
                           'bytes': len(data), 'agrees': list(size) == (list(served) if served else None)})
    return {'checked': len(checks), 'agree': sum(1 for c in checks if c['agrees']), 'checks': checks}


def study(features, id_resolution, absence, hd_headers, bbox):
    return {
        'bbox': bbox,
        'catalog': CATALOG_API_BASE,
        'composition': classify(features),
        'dimensions': dimension_agreement(features),
        'id_resolution': id_resolution,
        'absence': absence,
        'hd_headers': hd_headers,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bbox', default=BAYONNE_BBOX, help='minlon,minlat,maxlon,maxlat (default: Bayonne)')
    parser.add_argument('--limit', type=int, default=1000, help='pictures to examine (default: 1000)')
    parser.add_argument('--hd-checks', type=int, default=3,
                        help='hd assets to download per distinct frame size (default: 3; 0 to skip)')
    parser.add_argument('--write', metavar='PATH', help='write the artifact JSON here')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    features = search(args.bbox, args.limit)
    if not features:
        print('No pictures returned for bbox %s — nothing to measure.' % args.bbox)
        return 1

    panoramic = next((f for f in features if field_of_view(f) == PANORAMIC_FIELD_OF_VIEW_DEG), None)
    result = study(
        features,
        probe_id_resolution(panoramic['id']) if panoramic else None,
        probe_absence(),
        probe_hd_headers(features, per_size=args.hd_checks) if args.hd_checks else None,
        args.bbox,
    )

    composition, dims = result['composition'], result['dimensions']
    print('Examined %d pictures in bbox %s' % (composition['examined'], args.bbox))
    print('  360 panoramas: %d (%s%%)   other: %d'
          % (composition['panoramic']['count'], fmt(composition['panoramic_share_pct'], '.1f'),
             composition['other']['count']))
    print('  360 licences:  %s' % composition['panoramic']['licenses'])
    print('  other fovs:    %s' % composition['other']['field_of_view_values'])
    print('  frame sizes:   %s' % dims['frame_sizes'])
    print('  sensor dims vs tile matrix: %d agree, %d disagree, %d not stated'
          % (dims['agree'], dims['disagree'], dims['not_stated']))
    if result['hd_headers']:
        print('  hd JPEG headers: %d of %d agree with the tile matrix'
              % (result['hd_headers']['agree'], result['hd_headers']['checked']))
    print('  absent uuid: /pictures -> %s, /search?ids -> %s with %d features'
          % (result['absence']['pictures_endpoint']['status'],
             result['absence']['search_endpoint']['status'],
             result['absence']['search_endpoint']['features_returned']))

    if args.write:
        with open(args.write, 'w', encoding='utf-8') as f:
            # allow_nan=False: a NaN here is readable by Python and by nothing else (see
            # tests/test_committed_data_files.py), so the run must die rather than write one.
            json.dump(result, f, indent=2, sort_keys=True, allow_nan=False)
            f.write('\n')
        print('Wrote %s' % display_path(args.write, REPO_ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
