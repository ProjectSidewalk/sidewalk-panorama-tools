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

# --- a broken streetlevel fails CI instead of skipping -------------------------------------------------------

SIMULATED = "No module named 'pyproj' (simulated for #165)"


def _run_contract_module(tmp_path, *, shadow, require):
    """Run tests/test_streetlevel_api.py in a child pytest and return (returncode, output, junit root or None).

    shadow: put a `streetlevel` package first on the child's path that raises ModuleNotFoundError for a
    transitive dependency - what an unpinned pyproj/scipy/CoordinatesConverter that failed to install, or was
    dropped from streetlevel's own requirements, produces. ModuleNotFoundError specifically, because it is the
    case importorskip still skips: since pytest 9 a plain ImportError (a wheel that installed but will not
    load) propagates as a collection error on its own, measured 2026-09-27 on pytest 9.1.1.
    """
    env = dict(os.environ)
    env.pop('SIDEWALK_REQUIRE_STREETLEVEL', None)
    if require:
        env['SIDEWALK_REQUIRE_STREETLEVEL'] = '1'
    if shadow:
        _write(str(tmp_path / 'shadow' / 'streetlevel' / '__init__.py'),
               f'raise ModuleNotFoundError({SIMULATED!r}, name="pyproj")\n')
        env['PYTHONPATH'] = os.pathsep.join(p for p in (str(tmp_path / 'shadow'), env.get('PYTHONPATH')) if p)
    junit = tmp_path / 'junit.xml'
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', os.path.join('tests', 'test_streetlevel_api.py'), '-q',
         '-p', 'no:cacheprovider', '--junitxml', str(junit)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300)
    root = ET.parse(str(junit)).getroot() if junit.exists() else None
    if root is not None and root.tag == 'testsuites':
        root = root[0]
    return result.returncode, result.stdout + result.stderr, root


class TestStreetlevelMustImportWhenRequired:

    def test_without_the_variable_a_broken_import_skips_the_module(self, tmp_path):
        """The dev-box behaviour, kept: a machine without a compiler for pyfrpc still runs the suite."""
        code, output, root = _run_contract_module(tmp_path, shadow=True, require=False)
        # 5 is pytest's "no tests collected": a module-level skip leaves the child with nothing to run. In a
        # whole-suite run the other modules still run and the exit is 0 - the silence #165 is about.
        assert code in (0, 5), output
        assert root is not None and int(root.get('tests')) == int(root.get('skipped')), output

    def test_with_the_variable_a_broken_import_fails_the_run(self, tmp_path):
        """The CI behaviour: the same ImportError is now a collection error carrying its real message."""
        code, output, _ = _run_contract_module(tmp_path, shadow=True, require=True)
        assert code not in (0, 5), output  # a collection error, not a pass or a quiet skip
        assert SIMULATED in output

    def test_with_the_variable_the_contract_tests_are_collected_and_run(self, tmp_path):
        """Where streetlevel really imports, requiring it must mean every contract test ran - none skipped.
        CI sets the variable, so there this is the direct check that the module did not go quiet."""
        try:
            import streetlevel.streetview.api  # noqa: F401
        except ImportError:
            if os.environ.get('SIDEWALK_REQUIRE_STREETLEVEL', '') not in ('', '0'):
                raise
            pytest.skip('streetlevel is not importable here, and this run does not require it')
        code, output, root = _run_contract_module(tmp_path, shadow=False, require=True)
        assert code == 0, output
        assert root is not None, output
        assert int(root.get('tests')) >= 10, output
        assert int(root.get('skipped')) == 0, output


class TestTheWorkflowRequiresIt:
    """The variable does nothing unless CI sets it, and dropping it from the workflow fails no test above -
    they each set it themselves. So the workflow's pytest step is pinned here, by text: PyYAML is not a
    dependency, and a line-level check is all this needs."""

    WORKFLOW = os.path.join(REPO_ROOT, '.github', 'workflows', 'tests.yml')

    def _text(self):
        with open(self.WORKFLOW, encoding='utf-8') as f:
            return f.read()

    def test_the_pytest_step_sets_the_variable(self):
        lines = self._text().splitlines()
        step = next(i for i, line in enumerate(lines) if 'python -m pytest tests' in line)
        following = '\n'.join(lines[step + 1:step + 4])
        assert "SIDEWALK_REQUIRE_STREETLEVEL: '1'" in following, \
            'the CI pytest step must set SIDEWALK_REQUIRE_STREETLEVEL (#165)'

    def test_the_job_has_a_timeout(self):
        """GitHub's default is six hours of a held runner for one hung test."""
        assert any(line.strip().startswith('timeout-minutes:') for line in self._text().splitlines())
