"""Pins the parts of streetlevel's API that downloaders.gsv actually calls.

Every other test in this suite runs against conftest's stub, whose find_panorama_by_id swallows anything via
**kwargs. That means a renamed parameter or a moved attribute anywhere in the >=0.12.11,<0.13 range would pass the
whole suite and only surface on the scraper box. These tests import the real library instead, so CI (which
installs requirements.txt) catches the drift.

Skipped when streetlevel isn't installed -- its pyfrpc dependency needs a compiler, so a dev box without one can
still run the rest of the suite.
"""

import dataclasses
import inspect

import pytest

panorama = pytest.importorskip('streetlevel.streetview.panorama',
                               reason='streetlevel not installed (pyfrpc needs a compiler); CI installs it')
streetview = pytest.importorskip('streetlevel.streetview')
depth = pytest.importorskip('streetlevel.streetview.depth')


def test_find_panorama_by_id_still_takes_the_arguments_we_pass():
    params = inspect.signature(streetview.find_panorama_by_id).parameters
    # gsv.download_depth_maps calls find_panorama_by_id(pano_id, download_depth=True, session=session).
    for name in ('panoid', 'download_depth', 'session'):
        assert name in params, "streetlevel renamed or dropped find_panorama_by_id's %r parameter" % (name,)


def test_panorama_still_exposes_the_attributes_we_read():
    fields = {f.name for f in dataclasses.fields(panorama.StreetViewPanorama)}
    # _write_depth_artifact reads pano.depth / .heading / .pitch / .roll.
    assert {'depth', 'heading', 'pitch', 'roll'} <= fields, "streetlevel changed StreetViewPanorama's fields"


def test_depth_decode_still_mirrors_x():
    """Pin that streetlevel's decoder x-mirrors the payload: compute_depth_map writes the value it computes
    for payload column x to output column w-1-x, leaving the array horizontally flipped relative to the pano
    JPEG (#58). gsv._write_depth_artifact compensates with a [:, ::-1] flip on write. If this test fails,
    streetlevel changed its column order - remove (or adjust) that flip, or every new artifact silently
    regains the mirror.
    """
    # A 2x1 grid whose two columns decode differently: payload column 0 has no plane (-> -1), payload
    # column 1 hits a vertical plane 2 m away head-on (-> 2.0). Only the column order is under test.
    header = {'width': 2, 'height': 1}
    planes = [{'n': [0.0, 0.0, 1.0], 'd': 0.0},  # index 0 = "no plane", never dereferenced
              {'n': [1.0, 0.0, 0.0], 'd': 2.0}]
    indices = [0, 1]

    data = depth.compute_depth_map(header, planes, indices)['data']

    assert data[0] == pytest.approx(2.0), "streetlevel no longer mirrors x in its depth decode"
    assert data[1] == depth.INFINITELY_FAR, "streetlevel no longer mirrors x in its depth decode"


def test_depth_map_still_exposes_data():
    # _write_depth_artifact reads pano.depth.data. Tolerate DepthMap becoming a plain class so this catches a
    # rename rather than a refactor.
    fields = set()
    if dataclasses.is_dataclass(panorama.DepthMap):
        fields = {f.name for f in dataclasses.fields(panorama.DepthMap)}
    assert 'data' in fields or hasattr(panorama.DepthMap, 'data'), "streetlevel renamed DepthMap.data"
