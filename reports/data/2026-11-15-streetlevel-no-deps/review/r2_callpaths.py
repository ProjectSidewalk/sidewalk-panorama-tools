"""Review of the #237 plan, step 2: run the REAL streetlevel call paths production uses, offline.

The suite stubs api.find_panorama_by_id itself (tests/test_streetlevel_api.py monkeypatches it), so the body of
streetlevel's find_panorama_by_id -> build_find_panorama_by_id_request_url -> util.get_json never runs in CI.
This script runs it, with a fake requests-like session (no network), through the repo's own seams:

    gsv._fetch_image_levels(pano_id, session)          (image phase, #74; download_depth=False)
    gsv._fetch_pano_with_depth_planes(pano_id, session) (depth phase; download_depth=True)
    gsv.download_depth_maps' availability import       (from streetlevel import streetview)

Every import attempted while doing so is recorded (a wrapper on builtins.__import__), and any attempt to import
one of streetlevel's seven unreached requirements is reported. Run from the repo root with the venv's python:

    cd ~/claude-work/237/repo && <venv>/bin/python -I <this file>
"""

import base64
import builtins
import json
import os
import struct
import sys

sys.path.insert(0, os.getcwd())

UNREACHED_MODULES = {'equilib', 'pyequilib', 'torch', 'google', 'pyfrpc', 'Crypto', 'Cython', 'cython',
                     'pytest', 'pytest_asyncio', 'triton'}
attempted = set()
_real_import = builtins.__import__


def _recording_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name:
        attempted.add(name.split('.')[0])
    return _real_import(name, globals, locals, fromlist, level)


builtins.__import__ = _recording_import


class FakeResponse:
    def __init__(self, body):
        self.text = ")]}'" + json.dumps(body)   # streetlevel strips the first 4 characters
        self.status_code = 200

    def json(self):
        return json.loads(self.text[4:])


class FakeSession:
    """Answers every GET with a canned photometa body; records the URLs and headers it was asked for."""

    def __init__(self, body):
        self.body = body
        self.calls = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, headers, kwargs))
        return FakeResponse(self.body)


def payload(planes, indices, width, height):
    """The documented wire layout (tests/conftest.py encode_depth_payload, restated so pytest is not needed)."""
    header = struct.pack('<BHHHB', 8, len(planes), width, height, 8)
    plane_bytes = b''.join(struct.pack('<ffff', *p['n'], p['d']) for p in planes)
    return base64.urlsafe_b64encode(header + bytes(indices) + plane_bytes).decode()


def main():
    before = set(sys.modules)
    from downloaders import gsv  # the production module, imported as DownloadRunner does
    from streetlevel import streetview  # noqa: F401  (download_depth_maps' availability probe)

    pano_id = 'testPanoIdAbCdEfGhIj_-'
    planes = [{'n': [0.0, 0.0, 0.0], 'd': 0.0}, {'n': [2.0, 1.0, 0.0], 'd': 2.1213}]
    msg = [
        [1],
        [None, pano_id],
        [None, None, None, [[[[256, 512]]], [512, 512]]],
        [], [],
        [[None, [[None, None, 37.774, -122.419], [12.5], [180.0, 90.0, 0.0]], None, [], None,
          [None, [None, None, payload(planes, [1, 1, 0, 0], 4, 1)]]]],
        [],
    ]
    session = FakeSession([None, [msg]])

    levels = gsv._fetch_image_levels(pano_id, session)
    print('image phase  _fetch_image_levels ->', levels)
    pano, depth_planes = gsv._fetch_pano_with_depth_planes(pano_id, session)
    print('depth phase  _fetch_pano_with_depth_planes -> heading=%.4f raster=%s planes=%d'
          % (pano.heading, pano.depth.data.ravel().tolist(), len(depth_planes.distances)))
    gone = FakeSession([None, [[[2]]]])
    print('pano gone    _fetch_pano_with_depth_planes ->', gsv._fetch_pano_with_depth_planes(pano_id, gone))
    for url, headers, kwargs in session.calls:
        print('request      %s... headers=%s kwargs=%s' % (url[:90], sorted(headers), sorted(kwargs)))

    loaded_sl = sorted(m for m in sys.modules if m == 'streetlevel' or m.startswith('streetlevel.'))
    print('streetlevel modules loaded (%d): %s' % (len(loaded_sl), ', '.join(loaded_sl)))
    new_top = sorted({m.split('.')[0] for m in set(sys.modules) - before})
    print('top-level modules newly loaded (%d): %s' % (len(new_top), ', '.join(new_top)))
    hit = sorted(attempted & UNREACHED_MODULES)
    print('imports of an unreached requirement ATTEMPTED: %s' % (hit or 'none'))
    present = sorted(m for m in UNREACHED_MODULES if m in sys.modules)
    print('unreached requirement modules in sys.modules: %s' % (present or 'none'))
    return 1 if hit or present else 0


if __name__ == '__main__':
    sys.exit(main())
