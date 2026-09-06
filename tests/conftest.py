"""Shared fixtures for the test suite.

The repo is not an installed package, so tests import modules (downloaders, config) straight from the repo root.
streetlevel itself is never exercised: tests install a stub module so the suite is network-free and runs without
streetlevel's heavy dependency tree.
"""

import base64
import logging
import os
import signal
import struct
import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# The scraper only ever runs on Linux in production, but the suite should stay usable on a Windows dev box, so
# assertions about POSIX file modes are skipped rather than failed there.
posix_only = pytest.mark.skipif(os.name != 'posix', reason='POSIX file modes are unavailable on Windows')


@pytest.fixture(autouse=True)
def _isolate_process_state():
    """Snapshot and restore the process-wide state a runner's main() mutates, around every test.

    DownloadRunner.main(), refetch_panos.main() and their configure_logging() all add a handler to the root
    logger, set its level, cap urllib3's, and install a SIGTERM handler. Nothing removes any of it, so a test
    that drives main() in-process would otherwise leak a RotatingFileHandler pointed into a tmp_path pytest
    is about to delete - measured at four such handlers after one module - into every test that follows.
    Suite-wide rather than per module, because the next module to grow a main() test would otherwise have to
    rediscover this.
    """
    root = logging.getLogger()
    prior_handlers = list(root.handlers)
    prior_level = root.level
    prior_urllib3_level = logging.getLogger('urllib3').level
    prior_sigterm = signal.getsignal(signal.SIGTERM)
    yield
    for handler in list(root.handlers):
        if handler not in prior_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(prior_level)
    logging.getLogger('urllib3').setLevel(prior_urllib3_level)
    signal.signal(signal.SIGTERM, prior_sigterm)


def pytest_configure(config):
    """Extend coverage into the subprocesses several test modules spawn (#57).

    The runners are driven as real subprocesses - `main()`, the argparse `type=` validators, the budget
    carve-out prints and both `__main__` guards only ever execute in a child - so without this the coverage
    report calls a few hundred well-tested lines dead and sends the next person off writing tests that
    already exist. That is a worse failure than having no number at all.

    coverage ships a .pth that measures any interpreter it starts in, but only when COVERAGE_PROCESS_START
    names a config file; pytest-cov does not set it. So set it here, and only when this process is itself
    being measured - otherwise every subprocess in an ordinary run would litter .coverage.* files. The
    children inherit it because the helpers spawn with `dict(os.environ, ...)` rather than a scrubbed env.

    `parallel = True` in .coveragerc is the other half: without it each child would overwrite the parent's
    data file instead of adding to it.
    """
    if os.environ.get('COVERAGE_PROCESS_START'):
        return
    try:
        import coverage
    except ImportError:
        return
    if coverage.Coverage.current() is not None:
        os.environ['COVERAGE_PROCESS_START'] = os.path.join(REPO_ROOT, '.coveragerc')
        # The other two halves of the same CWD problem, because every one of these helpers spawns with
        # cwd=tmp_path: coverage resolves both the data file and a relative `source` against the running
        # process's CWD. Without the first the children measure correctly and then drop their data in a
        # directory pytest deletes; without the second they measure the temp directory instead of the repo.
        os.environ['COVERAGE_FILE'] = os.path.join(REPO_ROOT, '.coverage')
        os.environ['SIDEWALK_COVERAGE_ROOT'] = REPO_ROOT


@pytest.fixture
def fake_streetview(monkeypatch):
    """Install a stub streetlevel.streetview module and return it for per-test find_panorama_by_id stubbing.

    Production code reaches streetlevel through the gsv._fetch_pano_with_depth_planes seam (one photometa
    request -> (pano, planes), #56), so the fixture also adapts that seam onto the stub: tests keep authoring
    the familiar find_panorama_by_id, and the adapter reads the planes bundle off the pano object (make_pano
    attaches one consistent with its depth array).
    """
    streetview = types.ModuleType('streetlevel.streetview')

    def _unstubbed(*args, **kwargs):
        raise AssertionError("test must stub find_panorama_by_id")

    streetview.find_panorama_by_id = _unstubbed
    streetlevel = types.ModuleType('streetlevel')
    streetlevel.streetview = streetview
    monkeypatch.setitem(sys.modules, 'streetlevel', streetlevel)
    monkeypatch.setitem(sys.modules, 'streetlevel.streetview', streetview)

    from downloaders import gsv

    def _seam_adapter(pano_id, session):
        pano = streetview.find_panorama_by_id(pano_id, download_depth=True, session=session)
        return pano, (getattr(pano, 'planes', None) if pano is not None else None)

    monkeypatch.setattr(gsv, '_fetch_pano_with_depth_planes', _seam_adapter)
    return streetview


def make_pano(depth_array=None, heading=1.25, pitch=0.02, roll=-0.01, planes='auto'):
    """Build an object shaped like streetlevel's StreetViewPanorama for the attributes the code reads.

    planes: the DepthPlanes-shaped bundle the fetch seam returns alongside the pano. 'auto' derives one
    consistent with depth_array (see default_planes); None means the payload carried no plane data.
    """
    depth = None if depth_array is None else SimpleNamespace(data=depth_array)
    if isinstance(planes, str) and planes == 'auto':
        planes = None if depth_array is None else default_planes(depth_array)
    return SimpleNamespace(depth=depth, heading=heading, pitch=pitch, roll=roll, planes=planes)


def default_depth_array():
    """A small depth grid with ground distances and a -1 sky pixel, in streetlevel's float64 dtype."""
    return np.array([[-1.0, 4.5], [3.25, 10.0]], dtype=np.float64)


def default_planes(depth_array):
    """A plane bundle consistent with depth_array: index 0 (no plane) exactly where depth is -1, plane 1 - a
    ground-like plane - everywhere else.

    depth_array is in streetlevel's (x-mirrored) order; plane indices come from the raw payload, whose column
    order is the x-flip of that (#58), so the indices here are flipped to payload order to keep the
    invariant (plane_indices == 0) == (stored depth == -1) that the artifact writer preserves.
    """
    flipped = np.asarray(depth_array)[..., ::-1]
    return SimpleNamespace(indices=np.where(flipped == -1, 0, 1).astype(np.uint8),
                           normals=np.array([[0.0, 0.0, 0.0], [0.0, 0.0, -1.0]], dtype=np.float32),
                           distances=np.array([0.0, 2.5], dtype=np.float32))


def encode_depth_payload(planes, indices, width, height):
    """Encode a synthetic GSV depth payload from the documented wire layout.

    Layout: uint8 header_size=8 | uint16 number_of_planes | uint16 width | uint16 height | uint8 offset=8,
    then width*height uint8 per-pixel plane indices, then 4 float32 (nx, ny, nz, d) per plane, all
    little-endian, urlsafe-base64 encoded. NB streetlevel reads the offset as a uint16 spanning bytes 7-8 -
    still true in 0.12.11 - so a payload fed to ITS parser needs indices[0] == 0 for the offset to parse as 8
    under both that reading and the true wire format's (see tests/test_streetlevel_api.py).

    @param planes  [{'n': [nx, ny, nz], 'd': d}, ...] including the never-dereferenced index-0 entry.
    @param indices Flat iterable of width*height per-pixel plane indices, payload order.
    """
    header = struct.pack('<BHHHB', 8, len(planes), width, height, 8)
    plane_bytes = b''.join(struct.pack('<ffff', *p['n'], p['d']) for p in planes)
    return base64.urlsafe_b64encode(header + bytes(indices) + plane_bytes).decode()
