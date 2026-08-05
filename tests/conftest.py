"""Shared fixtures for the test suite.

The repo is not an installed package, so tests import modules (downloaders, config) straight from the repo root.
streetlevel itself is never exercised: tests install a stub module so the suite is network-free and runs without
streetlevel's heavy dependency tree.
"""

import os
import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@pytest.fixture
def fake_streetview(monkeypatch):
    """Install a stub streetlevel.streetview module and return it for per-test find_panorama_by_id stubbing."""
    streetview = types.ModuleType('streetlevel.streetview')

    def _unstubbed(*args, **kwargs):
        raise AssertionError("test must stub find_panorama_by_id")

    streetview.find_panorama_by_id = _unstubbed
    streetlevel = types.ModuleType('streetlevel')
    streetlevel.streetview = streetview
    monkeypatch.setitem(sys.modules, 'streetlevel', streetlevel)
    monkeypatch.setitem(sys.modules, 'streetlevel.streetview', streetview)
    return streetview


def make_pano(depth_array=None, heading=1.25, pitch=0.02, roll=-0.01):
    """Build an object shaped like streetlevel's StreetViewPanorama for the attributes the code reads."""
    depth = None if depth_array is None else SimpleNamespace(data=depth_array)
    return SimpleNamespace(depth=depth, heading=heading, pitch=pitch, roll=roll)


def default_depth_array():
    """A small depth grid with ground distances and a -1 sky pixel, in streetlevel's float64 dtype."""
    return np.array([[-1.0, 4.5], [3.25, 10.0]], dtype=np.float64)
