"""Pins the parts of streetlevel's API that downloaders.gsv actually calls.

Every other test in this suite runs against conftest's stub, which swallows anything via **kwargs. That means
a renamed parameter or a moved attribute anywhere in the >=0.12.11,<0.13 range would pass the whole suite and
only surface on the scraper box. These tests import the real library instead, so CI (which installs
requirements.txt) catches the drift.

Since #56, production calls streetlevel's api + parse halves directly (gsv._fetch_pano_with_depth_planes:
one photometa request yields both the parsed pano and the raw plane payload that the high-level
find_panorama_by_id throws away), so the surface pinned here is api.find_panorama_by_id, the photometa
response shape, parse.parse_panorama_id_response, and the depth decode's column order.

Skipped when streetlevel isn't installed -- its pyfrpc dependency needs a compiler, so a dev box without one can
still run the rest of the suite.
"""

import dataclasses
import inspect

import numpy as np
import pytest

panorama = pytest.importorskip('streetlevel.streetview.panorama',
                               reason='streetlevel not installed (pyfrpc needs a compiler); CI installs it')
streetview = pytest.importorskip('streetlevel.streetview')
api = pytest.importorskip('streetlevel.streetview.api')
parse = pytest.importorskip('streetlevel.streetview.parse')

from conftest import encode_depth_payload  # noqa: E402
from downloaders import gsv  # noqa: E402


def test_api_find_panorama_by_id_still_takes_the_arguments_we_pass():
    params = inspect.signature(api.find_panorama_by_id).parameters
    # gsv._fetch_pano_with_depth_planes calls api.find_panorama_by_id(pano_id, download_depth=True,
    # locale='en', session=session).
    for name in ('panoid', 'download_depth', 'locale', 'session'):
        assert name in params, "streetlevel renamed or dropped api.find_panorama_by_id's %r parameter" % (name,)


def test_panorama_still_exposes_the_attributes_we_read():
    fields = {f.name for f in dataclasses.fields(panorama.StreetViewPanorama)}
    # _write_depth_artifact reads pano.depth / .heading / .pitch / .roll.
    assert {'depth', 'heading', 'pitch', 'roll'} <= fields, "streetlevel changed StreetViewPanorama's fields"


# Shared fixture for the two mirror pins below: a 4x1 grid where payload columns 1 and 2 hit the same plane
# from different azimuths and columns 0 and 3 have no plane. compute_depth_map casts rays in the horizontal
# plane (h=1 -> theta=pi/2), so with the decoder's phi formula payload column x looks along
# v = (cos, sin) of (w-x-0.5)/w*2pi + pi/2: x=1 -> (+.707, -.707), x=2 -> (-.707, -.707). Against the plane
# n=(2,1,0), d=1.5*sqrt(2) the decoder's t = |d / v.n| gives 3.0 for x=1 and 1.0 for x=2 - the azimuth
# *direction* is encoded in the magnitude. The normal must mix x and y: the decode's abs() makes |cos| and
# |sin| individually invariant under a phi-formula flip (phi -> 3pi - phi), so an axis-aligned plane cannot
# see one.
MIRROR_HEADER = {'width': 4, 'height': 1}
MIRROR_PLANES = [{'n': [0.0, 0.0, 0.0], 'd': 0.0},  # index 0 = "no plane", never dereferenced
                 {'n': [2.0, 1.0, 0.0], 'd': 1.5 * np.sqrt(2)}]
MIRROR_INDICES = [0, 1, 1, 0]
# Payload order decodes to [-1, 3.0, 1.0, -1]; streetlevel hands it back x-mirrored:
MIRROR_EXPECTED = [-1.0, 1.0, 3.0, -1.0]


def test_depth_decode_still_mirrors_x():
    """Pin that streetlevel's decode is x-mirrored relative to the payload: the value decoded for payload
    column x comes back in output column w-1-x (#58). gsv._write_depth_artifact compensates with a [:, ::-1]
    flip on write. If this test fails, streetlevel changed its column order - remove (or adjust) that flip,
    or every new artifact silently regains the mirror.

    Two x-reversals inside compute_depth_map jointly decide the orientation: the ray azimuth
    (phi = (w-x-0.5)/w*2pi + pi/2) and the write index (y*w + (w-x-1)). Flipping either one alone mirrors the
    output; flipping both is a numeric no-op. Because the fixture's magnitudes encode the ray azimuth (see
    MIRROR_HEADER above), this test fails under either single flip and stays green across a no-op refactor -
    it pins the end-to-end orientation, not one reversal's spelling.
    """
    # Internal module, imported here rather than importorskip'd at module scope: if upstream renames it, this
    # test must ERROR, not silently skip the whole file.
    from streetlevel.streetview import depth

    data = depth.compute_depth_map(MIRROR_HEADER, MIRROR_PLANES, MIRROR_INDICES)['data']

    # ravel(): compute_depth_map returns the array flat today, but the (h, w) reshape in parse() is the kind
    # of tidy-up that could move here; indexing the flat view keeps that from reading as a mirror change.
    np.testing.assert_allclose(np.ravel(data), MIRROR_EXPECTED, rtol=1e-6,
                               err_msg="streetlevel no longer mirrors x in its depth decode")
    assert np.ravel(data)[0] == depth.INFINITELY_FAR, "streetlevel changed its no-plane sentinel"


def test_depth_decode_mirrors_x_end_to_end():
    """Same pin as above, but through parse() on a synthetic base64 payload built from the documented wire
    layout (see conftest.encode_depth_payload) - so header parsing, index extraction, plane extraction,
    decode, and the (h, w) reshape are all covered. Output column c must hold the value decoded for payload
    index w-1-c. NB indices[0] must be 0 in payloads fed to streetlevel's parser: 0.12.10 reads the offset
    as a uint16 spanning bytes 7-8, so byte 8 (the first index) has to be zero for the offset to parse as 8
    under both that reading and 0.12.11's.
    """
    from streetlevel.streetview import depth

    payload = encode_depth_payload(MIRROR_PLANES, MIRROR_INDICES, MIRROR_HEADER['width'],
                                   MIRROR_HEADER['height'])

    depth_map = depth.parse(payload)

    assert depth_map.data.shape == (1, 4)
    # rtol 1e-5: the wire format stores the plane as float32, the decode computes in float64.
    np.testing.assert_allclose(np.ravel(depth_map.data), MIRROR_EXPECTED, rtol=1e-5,
                               err_msg="streetlevel no longer mirrors x in its depth decode")


def test_fetch_seam_extracts_pano_and_planes_from_the_raw_response(monkeypatch):
    """The whole-seam contract, network-free: api.find_panorama_by_id is stubbed to return a synthetic
    photometa response with the depth payload embedded at the documented msg path, and the REAL
    parse.parse_panorama_id_response runs on it. Pins, in one test: the arguments the seam passes to the api
    half, the response-code and payload paths in the msg, that one request yields both the parsed pano and
    the plane bundle, and that the decoded planes match what was embedded. If streetlevel moves any of it,
    this fails in CI instead of on the scraper box.
    """
    payload = encode_depth_payload(MIRROR_PLANES, MIRROR_INDICES, MIRROR_HEADER['width'],
                                   MIRROR_HEADER['height'])
    pano_id = 'testPanoIdAbCdEfGhIj_-'  # 22 chars: official-style, so the third-party date path stays cold
    msg = [
        [1],                                                    # response code: OK
        [None, pano_id],
        [None, None, None, [[[[256, 512]]], [512, 512]]],       # image sizes, tile size
        [],                                                     # address (absent)
        [],                                                     # copyright/uploader (absent)
        [[None,                                                 # msg[5][0]: location, orientation, depth
          [[None, None, 37.774, -122.419], [12.5], [180.0, 90.0, 0.0]],
          None, [], None,
          [None, [None, None, payload]]]],                      # msg[5][0][5][1][2]: the depth payload
        [],                                                     # dates/source (absent)
    ]
    response = [None, [msg]]
    captured = {}

    def fake_api_find(panoid, download_depth=False, locale='en', session=None):
        captured.update(panoid=panoid, download_depth=download_depth, session=session)
        return response

    monkeypatch.setattr(api, 'find_panorama_by_id', fake_api_find)
    sentinel_session = object()

    pano, planes = gsv._fetch_pano_with_depth_planes(pano_id, sentinel_session)

    assert captured == {'panoid': pano_id, 'download_depth': True, 'session': sentinel_session}
    assert pano.id == pano_id
    assert pano.lat == pytest.approx(37.774)
    assert pano.heading == pytest.approx(np.pi)
    assert pano.depth.data.shape == (MIRROR_HEADER['height'], MIRROR_HEADER['width'])
    np.testing.assert_allclose(np.ravel(pano.depth.data), MIRROR_EXPECTED, rtol=1e-5)
    np.testing.assert_array_equal(planes.indices,
                                  np.array(MIRROR_INDICES, dtype=np.uint8).reshape(1, 4))
    np.testing.assert_allclose(planes.normals, [p['n'] for p in MIRROR_PLANES], rtol=1e-6)
    np.testing.assert_allclose(planes.distances, [p['d'] for p in MIRROR_PLANES], rtol=1e-6)


def test_fetch_seam_returns_none_pair_when_the_pano_is_gone(monkeypatch):
    """Response code 2 (not found) must come back as (None, None) - the phase's 'unavailable' path."""
    monkeypatch.setattr(api, 'find_panorama_by_id', lambda *args, **kwargs: [None, [[[2]]]])

    assert gsv._fetch_pano_with_depth_planes('testPanoIdAbCdEfGhIj_-', object()) == (None, None)


def test_depth_map_still_exposes_data():
    # _write_depth_artifact reads pano.depth.data. Tolerate DepthMap becoming a plain class so this catches a
    # rename rather than a refactor.
    fields = set()
    if dataclasses.is_dataclass(panorama.DepthMap):
        fields = {f.name for f in dataclasses.fields(panorama.DepthMap)}
    assert 'data' in fields or hasattr(panorama.DepthMap, 'data'), "streetlevel renamed DepthMap.data"
