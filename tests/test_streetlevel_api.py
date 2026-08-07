"""Pins the parts of streetlevel's API that downloaders.gsv actually calls.

Every other test in this suite runs against conftest's stub, whose find_panorama_by_id swallows anything via
**kwargs. That means a renamed parameter or a moved attribute anywhere in the >=0.12.11,<0.13 range would pass the
whole suite and only surface on the scraper box. These tests import the real library instead, so CI (which
installs requirements.txt) catches the drift.

Skipped when streetlevel isn't installed -- its pyfrpc dependency needs a compiler, so a dev box without one can
still run the rest of the suite.
"""

import base64
import dataclasses
import inspect
import struct

import numpy as np
import pytest

panorama = pytest.importorskip('streetlevel.streetview.panorama',
                               reason='streetlevel not installed (pyfrpc needs a compiler); CI installs it')
streetview = pytest.importorskip('streetlevel.streetview')


def test_find_panorama_by_id_still_takes_the_arguments_we_pass():
    params = inspect.signature(streetview.find_panorama_by_id).parameters
    # gsv.download_depth_maps calls find_panorama_by_id(pano_id, download_depth=True, session=session).
    for name in ('panoid', 'download_depth', 'session'):
        assert name in params, "streetlevel renamed or dropped find_panorama_by_id's %r parameter" % (name,)


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
    layout - so header parsing, index extraction, plane extraction, decode, and the (h, w) reshape are all
    covered. Output column c must hold the value decoded for payload index w-1-c.
    """
    from streetlevel.streetview import depth

    # Wire layout: uint8 header_size=8 | uint16 number_of_planes | uint16 width | uint16 height | offset=8,
    # then w*h uint8 plane indices at byte 8, then 4 float32 (nx, ny, nz, d) per plane. indices[0] must be 0:
    # 0.12.10 reads offset as a uint16 spanning bytes 7-8, so byte 8 (the first index) has to be zero for the
    # offset to parse as 8 under both that reading and 0.12.11's.
    header = struct.pack('<BHHHB', 8, len(MIRROR_PLANES), MIRROR_HEADER['width'], MIRROR_HEADER['height'], 8)
    planes = b''.join(struct.pack('<ffff', *p['n'], p['d']) for p in MIRROR_PLANES)
    payload = base64.urlsafe_b64encode(header + bytes(MIRROR_INDICES) + planes).decode()

    depth_map = depth.parse(payload)

    assert depth_map.data.shape == (1, 4)
    # rtol 1e-5: the wire format stores the plane as float32, the decode computes in float64.
    np.testing.assert_allclose(np.ravel(depth_map.data), MIRROR_EXPECTED, rtol=1e-5,
                               err_msg="streetlevel no longer mirrors x in its depth decode")


def test_depth_map_still_exposes_data():
    # _write_depth_artifact reads pano.depth.data. Tolerate DepthMap becoming a plain class so this catches a
    # rename rather than a refactor.
    fields = set()
    if dataclasses.is_dataclass(panorama.DepthMap):
        fields = {f.name for f in dataclasses.fields(panorama.DepthMap)}
    assert 'data' in fields or hasattr(panorama.DepthMap, 'data'), "streetlevel renamed DepthMap.data"
