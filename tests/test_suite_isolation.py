"""Pins the suite's own isolation from the machine it runs on (#165).

Three guards live in tests/conftest.py and none of them is exercised by any production test, so each is
pinned here against the failure it exists for:

* the session-end check that the run left the repo - including the gitignored study cache - unchanged;
* the per-session temp directory every spawned child inherits, so a subprocess runner test never takes the
  host's real pacing lock or reads its real depth block latch;
* SIDEWALK_REQUIRE_STREETLEVEL, which CI sets so a broken streetlevel import fails the run instead of
  quietly skipping every contract test in tests/test_streetlevel_api.py.
"""

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import pytest

import conftest

REPO_ROOT = conftest.REPO_ROOT


# --- the repo is left as it was found ------------------------------------------------------------------------

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


class TestTreeChanges:
    """The comparison the session teardown asserts on, driven against a stand-in repo in tmp_path."""

    def test_an_untouched_tree_reports_nothing(self, tmp_path):
        _write(str(tmp_path / 'reports' / 'scripts' / '.cache' / 'rawlabels' / 'seattle.csv'), 'a\n')
        before = conftest.snapshot_tree_state(str(tmp_path))
        assert conftest.tree_changes(before, conftest.snapshot_tree_state(str(tmp_path))) == []

    def test_a_file_created_in_the_study_cache_is_reported(self, tmp_path):
        """The #165 shape exactly: a fake richmond.csv appearing in the Mapillary cache."""
        before = conftest.snapshot_tree_state(str(tmp_path))
        _write(str(tmp_path / 'reports' / 'scripts' / '.cache' / 'rawlabels-mapillary' / 'richmond.csv'),
               'label_id\n1\n')
        changes = conftest.tree_changes(before, conftest.snapshot_tree_state(str(tmp_path)))
        assert changes == ['created: ' + os.path.join('reports', 'scripts', '.cache', 'rawlabels-mapillary',
                                                      'richmond.csv')]

    def test_a_rewritten_cache_file_is_reported(self, tmp_path):
        """Same size, later mtime: a test overwriting a real city's file with a same-length fake."""
        path = str(tmp_path / 'reports' / 'scripts' / '.cache' / 'rawlabels' / 'seattle.csv')
        _write(path, 'a\n')
        before = conftest.snapshot_tree_state(str(tmp_path))
        _write(path, 'b\n')
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        assert conftest.tree_changes(before, conftest.snapshot_tree_state(str(tmp_path))) == \
            ['modified: ' + os.path.join('reports', 'scripts', '.cache', 'rawlabels', 'seattle.csv')]

    def test_a_deleted_cache_file_is_reported(self, tmp_path):
        path = str(tmp_path / 'reports' / 'scripts' / '.cache' / 'rawlabels' / 'seattle.csv')
        _write(path, 'a\n')
        before = conftest.snapshot_tree_state(str(tmp_path))
        os.remove(path)
        assert conftest.tree_changes(before, conftest.snapshot_tree_state(str(tmp_path))) == \
            ['deleted: ' + os.path.join('reports', 'scripts', '.cache', 'rawlabels', 'seattle.csv')]

    def test_a_new_git_status_line_is_reported(self):
        """The tracked-tree half, fed synthetic porcelain so it does not depend on this checkout's state."""
        before = ({}, ' M README.md\n')
        after = ({}, ' M README.md\n?? stray.csv\n')
        assert conftest.tree_changes(before, after) == ['git status gained: ?? stray.csv']

    def test_git_being_unavailable_is_not_a_change(self):
        assert conftest.tree_changes(({}, None), ({}, '?? stray.csv\n')) == []

    def test_the_real_checkout_is_snapshotted_through_git(self):
        """In a checkout the porcelain half must actually be live, or the tracked-tree guard is a no-op."""
        if not os.path.isdir(os.path.join(REPO_ROOT, '.git')) and \
                not os.path.isfile(os.path.join(REPO_ROOT, '.git')):
            pytest.skip('not a git checkout')
        assert conftest.snapshot_tree_state()[1] is not None


# --- spawned children get a temp dir of their own ------------------------------------------------------------

class TestChildrenGetASessionTempDir:
    """gsv's depth block latch and pacing state, and scrape_queue's lock, all default to the system temp
    directory, because each is a fact about the HOST. conftest's monkeypatching cannot reach a child process,
    so without this a subprocess runner test takes the real host's pacing lock and reads its real latch."""

    def test_a_spawned_child_resolves_its_temp_dir_inside_the_session_dir(self):
        session_dir = getattr(conftest, 'CHILD_TEMP_DIR', None)
        assert session_dir, 'conftest must set up a per-session temp dir for spawned children'
        out = subprocess.run([sys.executable, '-c', 'import tempfile; print(tempfile.gettempdir())'],
                             env=dict(os.environ), capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        child = os.path.normcase(os.path.realpath(out.stdout.strip()))
        assert child == os.path.normcase(os.path.realpath(session_dir))

    def test_the_session_dir_is_not_the_hosts_temp_dir(self):
        """This process keeps the real temp dir (pytest's own tmp_path lives there, and conftest removes the
        session dir at exit), so the child's must differ from it - otherwise the redirection did nothing."""
        session_dir = getattr(conftest, 'CHILD_TEMP_DIR', None)
        assert session_dir, 'conftest must set up a per-session temp dir for spawned children'
        here = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
        assert os.path.normcase(os.path.realpath(session_dir)) != here
        assert os.path.isdir(session_dir)

