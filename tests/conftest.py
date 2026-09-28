"""Shared fixtures for the test suite.

The repo is not an installed package, so tests import modules (downloaders, config) straight from the repo root.
streetlevel itself is never exercised: tests install a stub module so the suite is network-free and runs without
streetlevel's heavy dependency tree.
"""

import base64
import logging
import os
import shutil
import signal
import struct
import sys
import tempfile
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

# The desk studies' gitignored download cache. Gitignored is exactly what makes a write into it dangerous:
# `git status` cannot see it, the fetcher skips any file already there (fetch_rawlabels.py), and every study
# globs `*.csv` over it - so one fake file written by a test becomes a sticky, invisible change to the corpus
# behind committed numbers (#165). It is watched by content stamp, not through git.
STUDY_CACHE = os.path.join(REPO_ROOT, 'reports', 'scripts', '.cache')


def snapshot_tree_state(repo_root=REPO_ROOT):
    """Record what a test run could leave behind in the repo: the study cache's files and `git status`.

    Returns `(cache, porcelain)`. `cache` maps each file under `<repo_root>/reports/scripts/.cache` to
    `(size, mtime_ns)`; `porcelain` is `git status --porcelain --untracked-files=all` as text, or None where
    git is unavailable or `repo_root` is not a checkout (a tarball, a test's tmp_path). Porcelain reports a
    path's state rather than its content, so a test that rewrites a file already dirty before the run goes
    unseen; the cache half, the one that motivated this, is stamped per file and does not have that gap.
    """
    import subprocess

    cache = {}
    for dirpath, _, filenames in os.walk(os.path.join(repo_root, 'reports', 'scripts', '.cache')):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            cache[os.path.relpath(path, repo_root)] = (st.st_size, st.st_mtime_ns)
    try:
        status = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=all'], cwd=repo_root,
                                capture_output=True, text=True, timeout=60)
        porcelain = status.stdout if status.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        porcelain = None
    return cache, porcelain


def tree_changes(before, after):
    """Describe every difference between two snapshot_tree_state() results, as a list of lines.

    Empty means the run left the tree as it found it.
    """
    changes = []
    cache_before, porcelain_before = before
    cache_after, porcelain_after = after
    for path in sorted(set(cache_before) | set(cache_after)):
        if path not in cache_before:
            changes.append(f'created: {path}')
        elif path not in cache_after:
            changes.append(f'deleted: {path}')
        elif cache_before[path] != cache_after[path]:
            changes.append(f'modified: {path}')
    if porcelain_before is not None and porcelain_after is not None:
        lines_before = set(porcelain_before.splitlines())
        for line in sorted(set(porcelain_after.splitlines()) - lines_before):
            changes.append(f'git status gained: {line}')
        for line in sorted(lines_before - set(porcelain_after.splitlines())):
            changes.append(f'git status lost: {line}')
    return changes


@pytest.fixture(scope='session', autouse=True)
def _the_suite_leaves_the_repo_as_it_found_it():
    """Fail the run if any test created, modified or deleted a file in the repo (#165).

    A session-scoped teardown, so a violation is reported as an error at the teardown of whichever test ran
    last - read the message, not that test's name, for the culprit's trail. Found by audit rather than by a
    failure: tests/test_fetch_rawlabels.py wrote an 11-byte richmond.csv into the real Mapillary study cache
    on every run, and nothing in the suite could see it.
    """
    before = snapshot_tree_state()
    yield
    changes = tree_changes(before, snapshot_tree_state())
    assert not changes, ('the test run changed the repo it was run from; a test is writing outside its '
                         'tmp_path:\n  ' + '\n  '.join(changes))


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


@pytest.fixture(autouse=True)
def _isolate_depth_host_state(monkeypatch, tmp_path_factory):
    """Keep the depth phase's three HOST-level side effects (#43) out of the suite.

    All are correct in production and all have to be neutralised here, and none is something an
    individual test would think to do:

    * **The pacer's floor is a real 0.25 s** in `config.py`, not the 0 it used to be, so any test driving
      download_depth_maps would sleep a jittered gap between every pano. Measured: the depth modules alone
      went from ~2 min to ~3m45 before this.
    * **The block latch defaults to the machine's temp directory**, deliberately (it is a fact about the
      host, not the store). So exactly ONE test exercising a blocked stop wrote a real latch and every
      later test's depth phase then stood itself down - eight unrelated failures from one line of shared
      state, in the direction that looks like a bug in the code under test.

    Tests that are *about* the pacer or the latch pass their own values, so this only ever removes an
    accidental dependency on the deployment defaults.
    """
    from downloaders import gsv

    monkeypatch.setattr(gsv, 'depth_min_request_interval', 0.0)
    monkeypatch.setattr(gsv, 'depth_start_interval', 0.0)
    # The minimum back-off too, or "pacing off" is not actually off: the pacer deliberately engages a real 1 s
    # gap on push-back even at a zero floor, so any test that drives transient failures through the phase would
    # sleep a second or two per pano. Measured: that alone took the suite from 2 to 7.5 minutes.
    monkeypatch.setattr(gsv, 'DEPTH_PACE_MIN_BACKOFF', 0.0)
    latch = tmp_path_factory.mktemp('depth-latch') / gsv.DEPTH_BLOCK_LATCH_FILENAME
    monkeypatch.setattr(gsv, 'default_block_latch_path', lambda: str(latch))
    # The pacer's earned standing is the third host-level side effect (#43): download_depth_maps writes it
    # at the end of every phase and reads it at the start of the next, so without this one test's earned
    # speed would be the next test's opening interval - and, with the real intervals above restored by a
    # test, its sleeps.
    state = tmp_path_factory.mktemp('depth-pace') / gsv.DEPTH_PACE_STATE_FILENAME
    monkeypatch.setattr(gsv, 'default_pace_state_path', lambda: str(state))
    # The width-alarm latch (#121) is the same shape of host state, and one test driving main() over a wide frame
    # without passing --width-alarm-latch would arm the real one, making every later first-sighting test see
    # "already alarmed".
    from downloaders import common
    width_latch = tmp_path_factory.mktemp('width-alarm') / common.WIDTH_ALARM_LATCH_FILENAME
    monkeypatch.setattr(common, 'default_width_alarm_latch_path', lambda: str(width_latch))


# Set by pytest_configure: the temp directory every child process of this session resolves as its own.
CHILD_TEMP_DIR = None
_TEMP_VARS = ('TMPDIR', 'TEMP', 'TMP')
_prior_temp_env = {}


def _give_children_a_session_temp_dir():
    """Point spawned children's temp directory at a fresh per-session one (#165).

    gsv's depth block latch and pacing state and scrape_queue's lock all default to tempfile.gettempdir(),
    deliberately - each is a fact about the host. _isolate_depth_host_state redirects them in THIS process,
    but monkeypatching does not cross a process boundary, so the runner tests that spawn DownloadRunner.py
    were taking the host's real pacing lock and reading its real latch. The children inherit os.environ
    (every spawn helper passes `dict(os.environ, ...)`), and tempfile reads TMPDIR, TEMP, TMP in that order.

    gettempdir() is called first on purpose: it caches the host's directory in this process before the
    variables change, so pytest's own tmp_path stays where it always was - and survives the rmtree of the
    session dir at unconfigure.
    """
    global CHILD_TEMP_DIR
    tempfile.gettempdir()
    CHILD_TEMP_DIR = tempfile.mkdtemp(prefix='sidewalk-tests-children-')
    for name in _TEMP_VARS:
        _prior_temp_env[name] = os.environ.get(name)
        os.environ[name] = CHILD_TEMP_DIR


def pytest_unconfigure(config):
    """Undo _give_children_a_session_temp_dir: restore the variables and remove what the children left."""
    global CHILD_TEMP_DIR
    for name, value in _prior_temp_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _prior_temp_env.clear()
    if CHILD_TEMP_DIR:
        shutil.rmtree(CHILD_TEMP_DIR, ignore_errors=True)
        CHILD_TEMP_DIR = None


def pytest_configure(config):
    """Give children a session temp dir (above), then extend coverage into them (#57).

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
    _give_children_a_session_temp_dir()
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
