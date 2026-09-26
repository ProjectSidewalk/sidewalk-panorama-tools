"""Tests for reports/scripts/tilt_pose_scan.py — the store-side pose/facade scan the #54 study runs on
makelab2. It cannot import the repo there, so the pieces it copies (the JPEG header reader, the
sidecar rule, the ground-plane rule) are each pinned here against the original they copy."""

import ast
import csv
import os
import sys

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

from conftest import make_pano  # noqa: E402
from downloaders import common, gsv  # noqa: E402
import tilt_pose_scan as scan  # noqa: E402

SAMPLE_JPG = os.path.join(REPO_ROOT, 'samples', 'sample_pano.jpg')

XML = ('<?xml version="1.0" encoding="UTF-8" ?><panorama><data_properties image_width="16384" '
       'image_height="8192" image_date="2018-10" pano_id="{pid}"><copyright>x</copyright>'
       '</data_properties><projection_properties projection_type="spherical" pano_yaw_deg="269.22998" '
       'tilt_yaw_deg="110.909996" tilt_pitch_deg="4.73"/></panorama>')


def _planes(h=16, w=32):
    """Plane 1: ground (z-dominant) under the bottom half; plane 2: a facade (|n_z|/|n| = 0.4) over
    a 20x... block; plane 3: steep but not a facade (0.6); plane 4: a facade with too little support."""
    idx = np.zeros((h, w), dtype=np.uint8)
    idx[h // 2:, :] = 1
    idx[2:7, 0:30] = 2          # 150 px
    idx[7, 0:30] = 3            # 30 px: over the 20-px support floor the filter test uses
    idx[0, 0:5] = 4
    fac = np.array([0.9165, 0.0, 0.4])
    steep = np.array([0.8, 0.0, 0.6])
    from types import SimpleNamespace
    return SimpleNamespace(indices=idx, normals=np.array([[0, 0, 0], [0.02, 0.0, -1.0], fac, steep, fac]),
                           distances=np.array([0.0, -2.5, 7.0, 9.0, 4.0]))


def _depth_for(planes):
    d = np.where(planes.indices == 0, -1.0, 5.0)
    return d[:, ::-1].astype(np.float64)   # the writer un-mirrors; hand it the mirrored raster


@pytest.fixture
def store(tmp_path):
    city = tmp_path / 'seattle-wa'
    for shard in ('ab', 'cd'):
        (city / shard).mkdir(parents=True)
    # pano with an npz written by the real writer, and a jpg
    planes = _planes()
    pano = make_pano(_depth_for(planes), heading=np.radians(15.0), pitch=np.radians(-0.4),
                     roll=np.radians(359.0), planes=planes)
    gsv._write_depth_artifact(str(city), 'abNPZ', pano, planes)
    with open(SAMPLE_JPG, 'rb') as f:
        data = f.read()
    (city / 'ab' / 'abNPZ.jpg').write_bytes(data)
    (city / 'ab' / 'abNPZ.w8192.jpg').write_bytes(data)          # decoy: a display copy
    (city / 'cd' / 'cdXML.jpg').write_bytes(data)
    (city / 'cd' / 'cdXML.xml').write_text(XML.format(pid='cdXML'), encoding='utf-8')
    (city / 'cd' / 'cdBAD.depth.npz').write_bytes(b'PK\x03\x04truncated')
    return tmp_path


def _read(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _run(store, tmp_path, *extra):
    out = tmp_path / 'pose.csv'
    fac = tmp_path / 'fac.csv'
    rc = scan.main([str(store), '--city', 'seattle-wa', '--facades', 'all', '--out', str(out),
                    '--facades-out', str(fac)] + list(extra))
    return rc, {r['pano_id']: r for r in _read(out)}, _read(fac)


def test_reads_pose_scalars_in_degrees_wrapped(store, tmp_path):
    rc, rows, _ = _run(store, tmp_path)
    assert rc == 0
    r = rows['abNPZ']
    assert r['npz_present'] == '1' and r['npz_error'] == ''
    assert float(r['heading_deg']) == pytest.approx(15.0)
    assert float(r['pitch_deg']) == pytest.approx(-0.4)
    assert float(r['roll_deg']) == pytest.approx(-1.0)
    assert r['npz_format_version'] == str(gsv.DEPTH_ARTIFACT_FORMAT_VERSION)


def test_reads_the_xml_triple(store, tmp_path):
    _, rows, _ = _run(store, tmp_path)
    r = rows['cdXML']
    assert r['xml_present'] == '1' and r['npz_present'] == '0'
    assert float(r['xml_pano_yaw_deg']) == pytest.approx(269.22998)
    assert float(r['xml_tilt_yaw_deg']) == pytest.approx(110.909996)
    assert float(r['xml_tilt_pitch_deg']) == pytest.approx(4.73)
    assert r['xml_image_width'] == '16384' and r['xml_image_date'] == '2018-10'


def test_sidecar_is_not_a_pano(store, tmp_path):
    names = ['a.jpg', 'a.w8192.jpg', 'a.depth.npz', 'b.w16384.jpg', 'c.xml']
    for n in names:
        assert scan.is_downscaled_sidecar(n) == common.is_downscaled_sidecar(n)
    _, rows, _ = _run(store, tmp_path)
    assert set(rows) == {'abNPZ', 'cdXML', 'cdBAD'}


def test_jpeg_header_reader_agrees_with_common(tmp_path):
    assert scan.jpeg_size(SAMPLE_JPG) == common.jpeg_dimensions(SAMPLE_JPG)
    assert scan.jpeg_size(SAMPLE_JPG) is not None
    bad = tmp_path / 'x.jpg'
    bad.write_bytes(b'not a jpeg')
    assert scan.jpeg_size(str(bad)) is None and common.jpeg_dimensions(str(bad)) is None


def test_jpeg_dims_are_read(store, tmp_path):
    _, rows, _ = _run(store, tmp_path)
    w, h = common.jpeg_dimensions(SAMPLE_JPG)
    assert (rows['cdXML']['jpg_width'], rows['cdXML']['jpg_height']) == (str(w), str(h))
    assert rows['cdBAD']['jpg_present'] == '0'


def test_truncated_npz_is_an_error_row_not_a_crash(store, tmp_path):
    rc, rows, _ = _run(store, tmp_path)
    assert rc == 0
    assert rows['cdBAD']['npz_present'] == '1'
    assert rows['cdBAD']['npz_error'] != ''
    assert rows['cdBAD']['pitch_deg'] == ''


def test_facade_filter(store, tmp_path):
    _, _, fac = _run(store, tmp_path, '--min-support', '20')
    got = {int(r['plane_index']) for r in fac if r['pano_id'] == 'abNPZ'}
    assert got == {2}     # 0.4 in; 0.6 (30 px, so only verticality excludes it) out; ground out; 5-px facade out
    _, _, fac = _run(store, tmp_path, '--min-support', '300')
    assert [r for r in fac if r['pano_id'] == 'abNPZ'] == []


def test_ground_rule_matches_gsv_helper(store, tmp_path):
    _, rows, _ = _run(store, tmp_path)
    with np.load(str(store / 'seattle-wa' / 'ab' / 'abNPZ.depth.npz')) as art:
        expected = gsv.ground_plane_from_artifact(art)
        mine = scan.ground_plane(np.asarray(art['plane_indices']), np.asarray(art['planes_n']),
                                 np.asarray(art['planes_d']))
    assert mine[2] == expected[2]
    assert mine[1] == pytest.approx(expected[1])
    assert int(rows['abNPZ']['ground_index']) == expected[2]
    assert float(rows['abNPZ']['ground_dist_m']) == pytest.approx(expected[1], rel=1e-4)
    # the stored ground normal is oriented downward: elevation < 0
    assert float(rows['abNPZ']['ground_elev_deg']) < -80


def test_ground_rule_ignores_an_overhead_plane():
    """An awning above the horizon, more horizontal and with more pixels than the road, must not win:
    gsv.ground_plane_from_artifact only counts below-horizon rows, and so must the copy."""
    idx = np.zeros((8, 10), dtype=np.uint8)
    idx[0:4, :] = 2              # 40 px of perfectly horizontal overhead plane
    idx[5:8, :] = 1              # 30 px of cambered road
    normals = np.array([[0, 0, 0], [0.1, 0.0, 1.0], [0.0, 0.0, 1.0]])
    dists = np.array([0.0, 2.5, -3.0])
    art = {'plane_indices': idx, 'planes_n': normals, 'planes_d': dists}
    assert gsv.ground_plane_from_artifact(art)[2] == 1
    assert scan.ground_plane(idx, normals, dists)[2] == 1


def test_resume_skips_done_ids(store, tmp_path):
    _run(store, tmp_path)
    rc, rows, _ = _run(store, tmp_path, '--resume')
    with open(tmp_path / 'pose.csv', encoding='utf-8') as f:
        assert sum(1 for _ in f) == 1 + 3
    with open(tmp_path / 'fac.csv', encoding='utf-8') as f:
        n_fac = sum(1 for _ in f)
    _run(store, tmp_path, '--resume')
    with open(tmp_path / 'fac.csv', encoding='utf-8') as f:
        assert sum(1 for _ in f) == n_fac


def test_ids_mode_reads_city_and_pano(store, tmp_path):
    ids = tmp_path / 'ids.csv'
    ids.write_text('city,pano_id\nseattle-wa,cdXML\nseattle-wa,zzMISSING\n', encoding='utf-8')
    out = tmp_path / 'p.csv'
    scan.main([str(store), '--ids', str(ids), '--facades', 'all', '--out', str(out),
               '--facades-out', str(tmp_path / 'f.csv')])
    rows = {r['pano_id']: r for r in _read(out)}
    assert rows['cdXML']['xml_present'] == '1'
    assert rows['zzMISSING']['jpg_present'] == '0' and rows['zzMISSING']['npz_present'] == '0'


def test_seeded_facade_sample_is_deterministic():
    ids = ['p%03d' % i for i in range(100)]
    a = scan.facade_sample(ids, 10, 7)
    assert a == scan.facade_sample(list(reversed(ids)), 10, 7)
    assert len(a) == 10


@pytest.mark.parametrize('name', ['tilt_geometry.py', 'tilt_pose_scan.py', 'tilt_frame.py', 'tilt_remote_crop.py'])
def test_runs_under_python39_syntax(name):
    """makelab2 runs Python 3.9; a `match` or an `X | None` annotation would fail there after upload."""
    path = os.path.join(SCRIPTS, name)
    if not os.path.exists(path):
        pytest.skip('%s not written yet' % name)
    with open(path, encoding='utf-8') as f:
        ast.parse(f.read(), feature_version=(3, 9))


def test_py39_check_would_catch_a_310_construct():
    with pytest.raises(SyntaxError):
        ast.parse('match x:\n    case 1:\n        pass\n', feature_version=(3, 9))


def test_shard_sample_and_parts_partition_the_kept_shards():
    """--shard-sample N/D keeps a seeded subset of shards; --part i/P splits it disjointly, so P
    processes together cover exactly the sample, each shard once."""
    shards = ['%s%s' % (a, b) for a in 'abcdefgh' for b in 'ABCDEFGH']
    kept = [s for s in shards if scan.shard_kept(s, '1/4', 'seed')]
    assert 8 <= len(kept) <= 24
    parts = [[s for s in kept if scan.shard_in_part(s, '%d/3' % i)] for i in range(3)]
    assert sorted(sum(parts, [])) == sorted(kept)
    assert all(scan.shard_kept(s, '1/1', 'seed') for s in shards)


def test_city_mode_honours_the_shard_sample(store, tmp_path):
    out = tmp_path / 'p.csv'
    keep_ab = next(seed for seed in ('s%d' % i for i in range(200))
                   if scan.shard_kept('ab', '1/2', seed) and not scan.shard_kept('cd', '1/2', seed))
    scan.main([str(store), '--city', 'seattle-wa', '--shard-sample', '1/2', '--seed', keep_ab,
               '--out', str(out), '--facades-out', str(tmp_path / 'f.csv')])
    assert {r['pano_id'] for r in _read(out)} == {'abNPZ'}
