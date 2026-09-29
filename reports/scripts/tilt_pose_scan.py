"""Scan a pano store for per-pano rig attitude, and the facade planes that test which frame the depth
artifact is in (#54). Read-only over the store; writes two CSVs.

**Runs on the store host (makelab2), not here**: Python 3.9 / numpy 1.23, standard library + numpy
+ PIL only, and no imports from this repo - it is uploaded as a single file. The pieces it would
otherwise import are copied or replaced, each with a test (tests/test_tilt_pose_scan.py) that pins the copy to
its original: `is_downscaled_sidecar` (downloaders.common)
and `ground_plane` (downloaders.gsv.ground_plane_from_artifact).

What a pano on the store can carry, and what this reads from it:

* `<id>.jpg` - the stitched pano. Header dims, byte size, mtime (a 2019 mtime is the secondary
  marker of a 2019 scrape; the primary one is the .xml below).
* `<id>.depth.npz` - the v3 depth artifact the 2025-26 depth phase writes: heading/pitch/roll in
  radians (streetlevel's sign) and Google's plane list. Written only for panos alive at depth time.
* `<id>.xml` - left by the 2019 scrapes from Google's dead XML endpoint: `<projection_properties
  pano_yaw_deg tilt_yaw_deg tilt_pitch_deg/>`, the legacy era's rig tilt, present whether or not
  Google still serves the pano.

Outputs:

* `--out` pose CSV, one row per pano id seen (POSE_COLUMNS), degrees, pitch/roll wrapped to
  (-180, 180]. An unreadable npz or xml is a row with `npz_error`/`xml_error` set, never a crash.
* `--facades-out`, for the `--facades` sample: every plane whose normal is within 30 deg of
  horizontal (|n_z|/|n| < 0.5) with at least `--min-support` pixels, raw artifact-frame normal.
  The ground plane (same rule as gsv.ground_plane_from_artifact) goes in the pose CSV's `ground_*`.

    python3 tilt_pose_scan.py <store-root> --city seattle-wa --facades 5000 --seed 20260926 \\
        --out pose_seattle-wa.csv --facades-out facades_seattle-wa.csv [--resume]
    python3 tilt_pose_scan.py <store-root> --city seattle-wa --shard-sample 1/4 --part 0/4 ...   # one of 4
    python3 tilt_pose_scan.py <store-root> --ids corpus_pano_ids.csv --facades all ...
"""

import argparse
import csv
import datetime
import hashlib
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

POSE_COLUMNS = [
    'city', 'pano_id',
    'jpg_present', 'jpg_bytes', 'jpg_mtime_iso', 'jpg_width', 'jpg_height',
    'npz_present', 'npz_error', 'npz_format_version', 'heading_deg', 'pitch_deg', 'roll_deg',
    'xml_present', 'xml_error', 'xml_pano_yaw_deg', 'xml_tilt_yaw_deg', 'xml_tilt_pitch_deg',
    'xml_image_width', 'xml_image_height', 'xml_image_date', 'xml_mtime_iso',
    'facades_scanned', 'ground_index', 'ground_support', 'ground_elev_deg', 'ground_bearing_deg',
    'ground_dist_m',
]
FACADE_COLUMNS = ['city', 'pano_id', 'plane_index', 'support_px', 'n_x', 'n_y', 'n_z', 'd']

FACADE_MAX_VERTICALITY = 0.5     # |n_z|/|n| below this: the normal is within 30 deg of horizontal
DEFAULT_MIN_SUPPORT = 300

DEPTH_SUFFIX = '.depth.npz'
XML_SUFFIX = '.xml'

# ---- the one piece copied from downloaders/common.py (pinned by test_sidecar_is_not_a_pano) ----
_SIDECAR_SUFFIX = re.compile(r'\.w(\d+)\.jpg$')


def jpeg_size(path):
    """(width, height) from the JPEG header via Pillow's lazy open (no decode), or None.

    Deliberately not a copy of downloaders.common.jpeg_dimensions: the repo keeps one hand-rolled
    SOF scanner (tests/test_store_coverage.py), and Pillow is on the store host anyway.
    tests/test_tilt_pose_scan.py pins the two to the same answer. (The 2026-09-26 scan ran with an
    SOF copy that this replaced; the agreement test covers both.)"""
    try:
        with Image.open(path) as im:
            return im.size if im.format == 'JPEG' else None
    except Exception:  # noqa: BLE001 - a sweep must survive a truncated file
        return None


def is_downscaled_sidecar(filename):
    """Copy of downloaders.common.is_downscaled_sidecar: a `.w8192.jpg` display copy is not a pano."""
    return _SIDECAR_SUFFIX.search(os.path.basename(filename)) is not None
# ---- end copies ----


def ground_plane(indices, normals, distances, min_vertical=0.7):
    """Copy of downloaders.gsv.ground_plane_from_artifact's rule, returning
    (unit_normal, distance_m, plane_index, support) or None. Candidates come from the below-horizon
    rows only, ranked by pixel count, then verticality, then lowest index."""
    indices = np.asarray(indices)
    normals = np.asarray(normals, dtype=np.float64)
    distances = np.asarray(distances, dtype=np.float64)
    support = np.bincount(indices[(indices.shape[0] + 1) // 2:].ravel(), minlength=len(normals))
    best = None
    for index in range(1, len(normals)):
        count = int(support[index])
        if count == 0:
            continue
        length = float(np.linalg.norm(normals[index]))
        if length == 0.0:
            continue
        verticality = abs(float(normals[index][2])) / length
        if verticality < min_vertical:
            continue
        if best is None or (count, verticality) > best[0]:
            best = ((count, verticality), int(index), length)
    if best is None:
        return None
    (count, _), index, length = best
    return normals[index] / length, float(abs(distances[index]) / length), index, count


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _wrap(a):
    return -((-a + 180.0) % 360.0) + 180.0


def _cell(x, nd=6):
    """A CSV cell (blank for missing or non-finite). Not studyfmt.fmt: that one prints for people, and
    this file cannot import it on the store host."""
    if x is None:
        return ''
    if isinstance(x, float):
        if not math.isfinite(x):
            return ''
        return ('%.' + str(nd) + 'f') % x
    return str(x)


def facade_sample(ids, n, seed):
    """A seeded sample of n ids that does not depend on input order: rank by md5(seed:id)."""
    key = lambda i: hashlib.md5(('%s:%s' % (seed, i)).encode()).hexdigest()  # noqa: E731
    return sorted(sorted(set(ids), key=key)[:n])


def _normal_bearing_elevation(n):
    """Artifact frame (x left, y back, z down) -> RFU bearing/elevation. Mirrors
    tilt_geometry.artifact_normal_bearing_elevation, inlined to keep this file standalone."""
    r, f, u = -n[0], -n[1], -n[2]
    norm = math.sqrt(r * r + f * f + u * u)
    return math.degrees(math.atan2(r, f)), math.degrees(math.asin(max(-1.0, min(1.0, u / norm))))


def read_npz(path, want_planes):
    """-> (row fields, facade rows). Raises on an unreadable artifact."""
    out = {}
    facades = []
    with np.load(path) as art:
        out['npz_format_version'] = int(art['format_version']) if 'format_version' in art.files else ''
        for k in ('heading', 'pitch', 'roll'):
            v = float(art[k]) if k in art.files else float('nan')
            deg = math.degrees(v)
            out[k + '_deg'] = _wrap(deg) if k != 'heading' else deg % 360.0
        if want_planes:
            idx = np.asarray(art['plane_indices'])
            normals = np.asarray(art['planes_n'], dtype=np.float64)
            dists = np.asarray(art['planes_d'], dtype=np.float64)
            support = np.bincount(idx.ravel(), minlength=len(normals))
            for i in range(1, len(normals)):
                length = float(np.linalg.norm(normals[i]))
                if length == 0.0:
                    continue
                facades.append((i, int(support[i]), normals[i], float(dists[i]), abs(normals[i][2]) / length))
            g = ground_plane(idx, normals, dists)
            out['facades_scanned'] = 1
            if g is not None:
                n = -g[0] if g[0][2] < 0 else g[0]     # orient downward: +z is down in the artifact
                b, el = _normal_bearing_elevation(n)
                out.update(ground_index=g[2], ground_support=g[3], ground_elev_deg=el,
                           ground_bearing_deg=b, ground_dist_m=g[1])
    return out, facades


def read_xml(path):
    root = ET.parse(path).getroot()
    proj = root.find('.//projection_properties')
    data = root.find('.//data_properties')
    out = {}
    if proj is not None:
        for k in ('pano_yaw_deg', 'tilt_yaw_deg', 'tilt_pitch_deg'):
            v = proj.get(k)
            out['xml_' + k] = float(v) if v not in (None, '') else None
    if data is not None:
        for k in ('image_width', 'image_height', 'image_date'):
            out['xml_' + k] = data.get(k)
    return out


def scan_one(city, pano_id, shard_dir, want_planes, min_support):
    row = {'city': city, 'pano_id': pano_id}
    fac_rows = []
    jpg = os.path.join(shard_dir, pano_id + '.jpg')
    npz = os.path.join(shard_dir, pano_id + DEPTH_SUFFIX)
    xml = os.path.join(shard_dir, pano_id + XML_SUFFIX)
    try:
        st = os.stat(jpg)
        row.update(jpg_present=1, jpg_bytes=st.st_size, jpg_mtime_iso=_iso(st.st_mtime))
        dims = jpeg_size(jpg)
        if dims:
            row['jpg_width'], row['jpg_height'] = dims
    except OSError:
        row['jpg_present'] = 0
    if os.path.exists(npz):
        row['npz_present'] = 1
        try:
            fields, facades = read_npz(npz, want_planes)
            row.update(fields)
            for i, support, n, d, vert in facades:
                if vert < FACADE_MAX_VERTICALITY and support >= min_support:
                    fac_rows.append({'city': city, 'pano_id': pano_id, 'plane_index': i, 'support_px': support,
                                     'n_x': float(n[0]), 'n_y': float(n[1]), 'n_z': float(n[2]), 'd': d})
        except Exception as e:  # noqa: BLE001 - a bad artifact is a counted row, never a crash
            row['npz_error'] = type(e).__name__
            for k in ('npz_format_version', 'heading_deg', 'pitch_deg', 'roll_deg', 'facades_scanned',
                      'ground_index', 'ground_support', 'ground_elev_deg', 'ground_bearing_deg',
                      'ground_dist_m'):
                row.pop(k, None)
            fac_rows = []
    else:
        row['npz_present'] = 0
    if os.path.exists(xml):
        row['xml_present'] = 1
        try:
            row['xml_mtime_iso'] = _iso(os.stat(xml).st_mtime)
            row.update(read_xml(xml))
        except Exception as e:  # noqa: BLE001
            row['xml_error'] = type(e).__name__
    else:
        row['xml_present'] = 0
    return row, fac_rows


def _h(text):
    return int(hashlib.md5(text.encode()).hexdigest(), 16)


def shard_kept(shard, sample, seed):
    """--shard-sample 'N/D': keep a shard when md5(seed:shard) % D < N. Pano ids are random, so a
    shard sample is a random sample of panos - and it keeps the scan's cost proportional."""
    n, d = (int(x) for x in sample.split('/'))
    return _h('%s:%s' % (seed, shard)) % d < n


def shard_in_part(shard, part):
    """--part 'i/P': this process's disjoint share of the kept shards (i in 0..P-1)."""
    i, p = (int(x) for x in part.split('/'))
    return _h('part:' + shard) % p == i


def city_ids(city_dir, sample='1/1', seed='', part='0/1'):
    """Every pano id in a city dir with a .jpg (not a display copy), a .depth.npz, or a .xml,
    restricted to the kept shards of this part."""
    ids = []
    for shard in sorted(os.listdir(city_dir)):
        shard_path = os.path.join(city_dir, shard)
        if len(shard) != 2 or not os.path.isdir(shard_path):
            continue
        if not (shard_kept(shard, sample, seed) and shard_in_part(shard, part)):
            continue
        seen = set()
        for name in os.listdir(shard_path):
            if name.endswith(DEPTH_SUFFIX):
                pid = name[:-len(DEPTH_SUFFIX)]
            elif name.endswith('.jpg') and not is_downscaled_sidecar(name):
                pid = name[:-4]
            elif name.endswith(XML_SUFFIX):
                pid = name[:-len(XML_SUFFIX)]
            else:
                continue
            if pid[:2] == shard:
                seen.add(pid)
        ids.extend((shard, p) for p in sorted(seen))
    return ids


def _done(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline='', encoding='utf-8') as f:
        return {(r['city'], r['pano_id']) for r in csv.DictReader(f)}


def _writer(path, columns, resume):
    exists = resume and os.path.exists(path) and os.path.getsize(path) > 0
    f = open(path, 'a' if exists else 'w', newline='', encoding='utf-8')
    w = csv.DictWriter(f, fieldnames=columns, lineterminator='\n')
    if not exists:
        w.writeheader()
    return f, w


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('store_root')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--city')
    g.add_argument('--ids', help='CSV with city,pano_id columns')
    ap.add_argument('--facades', default='0', help="'all', or a count for a seeded sample of npz panos")
    ap.add_argument('--seed', default='20260926')
    ap.add_argument('--min-support', type=int, default=DEFAULT_MIN_SUPPORT)
    ap.add_argument('--out', required=True)
    ap.add_argument('--facades-out', required=True)
    ap.add_argument('--shard-sample', default='1/1', help="'N/D': a seeded N-in-D sample of shards (--city only)")
    ap.add_argument('--part', default='0/1', help="'i/P': this process's share of the kept shards")
    ap.add_argument('--resume', action='store_true')
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.city:
        work = [(args.city, pid) for _, pid in city_ids(os.path.join(args.store_root, args.city),
                                                        args.shard_sample, args.seed, args.part)]
    else:
        with open(args.ids, newline='', encoding='utf-8') as f:
            work = sorted({(r['city'], r['pano_id']) for r in csv.DictReader(f)})
    if args.facades == 'all':
        facade_set = None
    else:
        n = int(args.facades)
        npz_ids = [pid for city, pid in work
                   if os.path.exists(os.path.join(args.store_root, city, pid[:2], pid + DEPTH_SUFFIX))]
        facade_set = set(facade_sample(npz_ids, n, args.seed))
    done = _done(args.out) if args.resume else set()
    fp, pw = _writer(args.out, POSE_COLUMNS, args.resume)
    ff, fw = _writer(args.facades_out, FACADE_COLUMNS, args.resume)
    try:
        for i, (city, pid) in enumerate(work):
            if (city, pid) in done:
                continue
            want = facade_set is None or pid in facade_set
            row, facs = scan_one(city, pid, os.path.join(args.store_root, city, pid[:2]), want,
                                 args.min_support)
            pw.writerow({k: _cell(v) for k, v in row.items()})
            for fr in facs:
                fw.writerow({k: _cell(v, 7) for k, v in fr.items()})
            if (i + 1) % 1000 == 0:
                sys.stderr.write('%d/%d\n' % (i + 1, len(work)))
                sys.stderr.flush()
                fp.flush()
                ff.flush()
    finally:
        fp.close()
        ff.close()
    sys.stderr.write('done %d\n' % len(work))
    return 0


if __name__ == '__main__':
    sys.exit(main())
