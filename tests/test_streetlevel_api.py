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


def test_find_panorama_by_id_still_takes_the_arguments_we_pass():
    params = inspect.signature(streetview.find_panorama_by_id).parameters
    # gsv.download_depth_maps calls find_panorama_by_id(pano_id, download_depth=True, session=session).
    for name in ('panoid', 'download_depth', 'session'):
        assert name in params, "streetlevel renamed or dropped find_panorama_by_id's %r parameter" % (name,)


def test_panorama_still_exposes_the_attributes_we_read():
    fields = {f.name for f in dataclasses.fields(panorama.StreetViewPanorama)}
    # _write_depth_artifact reads pano.depth / .heading / .pitch / .roll.
    assert {'depth', 'heading', 'pitch', 'roll'} <= fields, "streetlevel changed StreetViewPanorama's fields"


def test_depth_map_still_exposes_data():
    # _write_depth_artifact reads pano.depth.data. Tolerate DepthMap becoming a plain class so this catches a
    # rename rather than a refactor.
    fields = set()
    if dataclasses.is_dataclass(panorama.DepthMap):
        fields = {f.name for f in dataclasses.fields(panorama.DepthMap)}
    assert 'data' in fields or hasattr(panorama.DepthMap, 'data'), "streetlevel renamed DepthMap.data"
