"""PROTOTYPE for #237 Part 2 (planner's scratch, not for commit as-is): the tests that replace the tripwire."""
import importlib.metadata

import numpy as np
import pytest
from packaging.requirements import Requirement

from conftest import encode_depth_payload
from downloaders import gsv

PLANES = [{'n': [0.0, 0.0, 0.0], 'd': 0.0}, {'n': [2.0, 1.0, 0.0], 'd': 1.5 * np.sqrt(2)}]
ZENITH_INDICES = [1, 1, 0, 0]           # first index byte nonzero: the modelled-zenith class
ZENITH_EXPECTED = [-1.0, -1.0, 3.0, 1.0]  # streetlevel's x-mirrored order


def test_streetlevel_reads_the_depth_offset_as_one_byte():
    from streetlevel.streetview import depth
    payload = encode_depth_payload(PLANES, ZENITH_INDICES, 4, 1)
    header = depth.parse_header(depth.decode_b64(payload))
    assert header['offset'] == 8
    assert (header['header_size'], header['number_of_planes'], header['width'], header['height']) == (8, 2, 4, 1)


def test_streetlevels_parser_resolves_a_modelled_zenith_and_agrees_with_ours():
    from streetlevel.streetview import depth
    payload = encode_depth_payload(PLANES, ZENITH_INDICES, 4, 1)
    theirs = np.asarray(depth.parse(payload).data)
    assert theirs.shape == (1, 4)
    np.testing.assert_allclose(np.ravel(theirs), ZENITH_EXPECTED, rtol=1e-5)
    ours = gsv._compute_depth_raster(gsv._decode_depth_planes(payload))
    np.testing.assert_allclose(ours, theirs[:, ::-1], rtol=1e-5)


def test_parity_on_a_larger_grid_whose_first_index_is_nonzero():
    from streetlevel.streetview import depth
    planes_wire = [{'n': [0.0, 0.0, 0.0], 'd': 0.0}, {'n': [0.4, -1.1, 0.6], 'd': 3.7},
                   {'n': [0.0, 0.2, 1.0], 'd': 2.4}]
    payload = encode_depth_payload(planes_wire, [2, 1, 2, 2, 1, 0, 1, 2], width=4, height=2)
    ours = gsv._compute_depth_raster(gsv._decode_depth_planes(payload))
    theirs = np.asarray(depth.parse(payload).data)
    np.testing.assert_allclose(ours, theirs[:, ::-1], rtol=1e-5)


def test_the_installed_streetlevel_is_in_the_required_range():
    spec = Requirement('streetlevel>=0.13.0,<0.14').specifier
    assert spec.contains(importlib.metadata.version('streetlevel'))


def test_out_of_range_plane_index_still_fails_opaquely_upstream():
    """Evidence for keeping the in-repo decode: what does 0.13.0 do with an index past the plane list?"""
    from streetlevel.streetview import depth
    payload = encode_depth_payload(PLANES, [0, 5, 1, 0], 4, 1)
    with pytest.raises(gsv.DepthPayloadError):
        gsv._decode_depth_planes(payload)
    with pytest.raises(Exception) as info:
        depth.parse(payload)
    print('UPSTREAM RAISES', type(info.value).__name__, info.value)
