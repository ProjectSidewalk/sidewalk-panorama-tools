"""Pins the suite's own isolation from the machine it runs on (#165).

Four guards live in tests/conftest.py (and the workflow) and none of them is exercised by any production
test, so each is pinned here against the failure it exists for:

* the session-end check that the run left the repo - including the gitignored study cache - unchanged;
* the per-session temp directory every spawned child inherits, so a subprocess runner test never takes the
  host's real pacing lock or reads its real depth block latch;
* PytestRemovedIn10Warning as an error, so the next removal fails the PR that adds it;
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


def _remove_probe(path, cache_dir, remove_cache_dir):
    """Remove one file this module wrote into the study cache and, if the test created the cache dir, that
    dir too - but only once it is empty. Never a tree: see test_a_run_that_writes_into_the_study_cache_fails.
    The cache dir is a parameter so TestTheProbeCleanup can pin this against a tmp_path stand-in."""
    if os.path.exists(path):
        os.remove(path)
    if remove_cache_dir:
        try:
            os.rmdir(cache_dir)
        except OSError:
            pass  # not empty (someone else's write landed in it), or already gone


class TestTheProbeCleanup:
    """The end-to-end guard test below writes into the real study cache, so its cleanup is pinned here
    against a stand-in, in every state. Driven only through that test, the cleanup's two halves were pinned
    on CI alone or not at all: with a real cache already present (a dev box), the test never asks for the
    dir to go, so a whole-tree rmtree there is never reached; and a leftover EMPTY dir is invisible to the
    session guard, which stamps files, and to git, which ignores empty dirs (#171 final review, NIT 2)."""

    def _cache_with(self, tmp_path, *names):
        cache = tmp_path / '.cache'
        cache.mkdir()
        for name in names:
            (cache / name).write_text('x')
        return cache

    def test_a_dir_it_created_goes_once_the_probe_leaves_it_empty(self, tmp_path):
        cache = self._cache_with(tmp_path, 'probe.txt')
        _remove_probe(str(cache / 'probe.txt'), str(cache), remove_cache_dir=True)
        assert not cache.exists()

    def test_a_dir_it_created_stays_while_someone_elses_file_is_in_it(self, tmp_path):
        """Independent of whether the real cache existed: this is the whole-tree rmtree's failure shape."""
        cache = self._cache_with(tmp_path, 'probe.txt', 'bystander.csv')
        _remove_probe(str(cache / 'probe.txt'), str(cache), remove_cache_dir=True)
        assert sorted(os.listdir(cache)) == ['bystander.csv']

    def test_a_dir_it_did_not_create_stays_even_when_empty(self, tmp_path):
        cache = self._cache_with(tmp_path, 'probe.txt')
        _remove_probe(str(cache / 'probe.txt'), str(cache), remove_cache_dir=False)
        assert cache.is_dir() and os.listdir(cache) == []

    def test_a_probe_that_is_already_gone_is_not_an_error(self, tmp_path):
        cache = self._cache_with(tmp_path)
        _remove_probe(str(cache / 'probe.txt'), str(cache), remove_cache_dir=True)
        assert not cache.exists()


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

    def test_a_resized_cache_file_with_the_same_mtime_is_reported(self, tmp_path):
        """Different size, identical mtime_ns: what a same-second rewrite looks like on a coarse-mtime
        filesystem (FAT's 2 s, some SMB/sshfs mounts). The stamp is (size, mtime), not mtime alone."""
        path = str(tmp_path / 'reports' / 'scripts' / '.cache' / 'rawlabels' / 'seattle.csv')
        _write(path, 'a\n')
        st = os.stat(path)
        before = conftest.snapshot_tree_state(str(tmp_path))
        _write(path, 'a longer fake\n')
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
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

    def test_a_run_that_writes_into_the_study_cache_fails(self, tmp_path):
        """End to end, through the real session fixture: a child pytest whose one test drops a file into this
        checkout's study cache must exit nonzero naming it. The TestTreeChanges cases above pin the
        comparison; only this catches the fixture itself being disabled, unwired or never asserting.

        The probe is removed in `finally`, before this run's own guard looks - it is the one sanctioned
        write into the repo in the suite, and it lasts a few seconds. The cleanup removes exactly the probe,
        and the cache dir only if that leaves it empty: anything else in there during the window (a
        fetch_rawlabels.py download started in the same checkout, another session's probe) is someone
        else's, and a whole-tree rmtree would delete it with the parent's guard comparing "no cache" against
        "no cache" and seeing nothing (#171 review). The child plants a bystander file to stand in for that
        writer, and it must survive the cleanup.

        The child is not measured: it runs from tmp_path and has no production code in it, and a measured
        child here is what put tests/conftest.py into CI's coverage figure (#171 review; .coveragerc's omit
        patterns are now anchored too, so this is belt and braces)."""
        cache_rel = os.path.join('reports', 'scripts', '.cache')
        probe_rel = os.path.join(cache_rel, f'_suite_isolation_probe_{os.getpid()}.txt')
        probe = os.path.join(REPO_ROOT, probe_rel)
        bystander = os.path.join(REPO_ROOT, cache_rel, f'_suite_isolation_bystander_{os.getpid()}.txt')
        cache_existed = os.path.isdir(conftest.STUDY_CACHE)
        writer = tmp_path / 'test_writes_into_the_repo.py'
        writer.write_text(
            'import os\n\n'
            'def test_writes():\n'
            f'    os.makedirs(os.path.dirname({probe!r}), exist_ok=True)\n'
            f'    open({probe!r}, "w").write("x")\n'
            f'    open({bystander!r}, "w").write("someone else\'s")\n')
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            p for p in (os.path.join(REPO_ROOT, 'tests'), os.environ.get('PYTHONPATH')) if p))
        for name in ('COVERAGE_PROCESS_START', 'COVERAGE_FILE', 'SIDEWALK_COVERAGE_ROOT'):
            env.pop(name, None)
        try:
            try:
                result = subprocess.run(
                    [sys.executable, '-m', 'pytest', str(writer), '-q', '-p', 'no:cacheprovider',
                     '-p', 'conftest', '--rootdir', str(tmp_path)],
                    cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
            finally:
                _remove_probe(probe, conftest.STUDY_CACHE, not cache_existed)
            assert os.path.exists(bystander), 'the probe cleanup deleted a file it did not create'
        finally:
            _remove_probe(bystander, conftest.STUDY_CACHE, not cache_existed)
        output = result.stdout + result.stderr
        assert result.returncode != 0, output
        assert 'created: ' + probe_rel in output, output

    def test_the_guard_still_checks_a_tree_that_was_dirty_at_the_start(self):
        """CI always starts clean, so a guard that stood down on a dirty start would pass CI while doing
        nothing on a dev box, where a dirty checkout is the normal state (#171 review)."""
        snapshots = iter([
            ({}, ' M README.md\n'),
            ({os.path.join('reports', 'scripts', '.cache', 'x.csv'): (1, 1)}, ' M README.md\n'),
        ])
        guard = conftest.guard_the_tree(lambda: next(snapshots))
        next(guard)
        with pytest.raises(AssertionError) as raised:
            next(guard)
        message = str(raised.value)
        assert 'created: ' + os.path.join('reports', 'scripts', '.cache', 'x.csv') in message
        assert 'reported once' in message  # the next run will take this as its baseline

    def test_the_guard_passes_a_tree_left_as_it_was(self):
        guard = conftest.guard_the_tree(lambda: ({}, ' M README.md\n'))
        next(guard)
        with pytest.raises(StopIteration):
            next(guard)

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

    def test_unconfigure_restores_the_variables_and_removes_the_dir(self, monkeypatch, tmp_path):
        """Driven in-process against substitute state; monkeypatch puts the session's real state back.
        Matters for an in-process pytest.main() caller, and docs/testing.md claims the removal."""
        session_dir = tmp_path / 'children'
        (session_dir / 'left-by-a-child').mkdir(parents=True)
        monkeypatch.setattr(conftest, 'CHILD_TEMP_DIR', str(session_dir))
        monkeypatch.setattr(conftest, '_prior_temp_env', {'TMPDIR': None, 'TEMP': 'host-temp'})
        monkeypatch.setenv('TMPDIR', str(session_dir))
        monkeypatch.setenv('TEMP', str(session_dir))
        conftest.pytest_unconfigure(None)
        assert 'TMPDIR' not in os.environ
        assert os.environ['TEMP'] == 'host-temp'
        assert not session_dir.exists()
        assert conftest.CHILD_TEMP_DIR is None

# --- the next pytest's removals are errors now, not a warnings-summary line ----------------------------------

_INSTANCE_METHOD_CLASS_FIXTURE = '''import pytest


class TestShape:
    @pytest.fixture(scope='class')
    def value(self):
        return 1

    def test_uses_it(self, value):
        assert value == 1
'''


class TestTheNextPytestsRemovalsAreErrors:
    """fcadac1 made eight class-scoped instance-method fixtures classmethods, the shape pytest 10 removes (D3:
    no `pytest<10` bound). Nothing kept a ninth from arriving as one line in a warnings summary until pytest 10
    broke every PR at once, so conftest escalates the warning (#171 review)."""

    def test_conftest_escalates_it(self, pytestconfig):
        if not hasattr(pytest, 'PytestRemovedIn10Warning'):
            pytest.skip('this pytest has no PytestRemovedIn10Warning to escalate')
        assert 'error::pytest.PytestRemovedIn10Warning' in pytestconfig.getini('filterwarnings')

    def test_an_instance_method_class_fixture_fails_the_run(self, tmp_path):
        if not hasattr(pytest, 'PytestRemovedIn10Warning'):
            pytest.skip('this pytest has no PytestRemovedIn10Warning to escalate')
        target = tmp_path / 'test_instance_method_fixture.py'
        target.write_text(_INSTANCE_METHOD_CLASS_FIXTURE)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            p for p in (os.path.join(REPO_ROOT, 'tests'), os.environ.get('PYTHONPATH')) if p))
        for name in ('COVERAGE_PROCESS_START', 'COVERAGE_FILE', 'SIDEWALK_COVERAGE_ROOT'):
            env.pop(name, None)
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', str(target), '-q', '-p', 'no:cacheprovider', '-p', 'conftest',
             '--rootdir', str(tmp_path)],
            cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
        output = result.stdout + result.stderr
        assert result.returncode != 0, output
        assert 'PytestRemovedIn10Warning' in output, output


# --- a broken streetlevel fails CI instead of skipping -------------------------------------------------------

SIMULATED = "No module named 'pyproj' (simulated for #165)"


def _run_contract_module(tmp_path, *, shadow, require, error='ModuleNotFoundError'):
    """Run tests/test_streetlevel_api.py in a child pytest and return (returncode, output, junit root or None).

    shadow: put a `streetlevel` package first on the child's path that raises ModuleNotFoundError for a
    transitive dependency - what an unpinned pyproj/scipy/CoordinatesConverter that failed to install, or was
    dropped from streetlevel's own requirements, produces. ModuleNotFoundError specifically, because it is the
    case importorskip still skips: since pytest 9 a plain ImportError (a wheel that installed but will not
    load) propagates as a collection error on its own, measured 2026-09-27 on pytest 9.1.1. `error` switches
    the shadow to that plain ImportError, for the case where the variable must not turn it into a skip.

    require: the variable's value in the child, or None for unset (it is popped either way first, since CI
    sets it in this process's environment).
    """
    env = dict(os.environ)
    env.pop('SIDEWALK_REQUIRE_STREETLEVEL', None)
    if require is not None:
        env['SIDEWALK_REQUIRE_STREETLEVEL'] = require
    if shadow:
        _write(str(tmp_path / 'shadow' / 'streetlevel' / '__init__.py'),
               f'raise {error}({SIMULATED!r}, name="pyproj")\n')
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

    @pytest.mark.parametrize('require', [None, '', '0'], ids=['unset', 'empty', 'zero'])
    def test_without_the_variable_a_broken_import_skips_the_module(self, tmp_path, require):
        """The dev-box behaviour, kept: a machine without a compiler for pyfrpc still runs the suite. Empty
        and `0` are off, as D6 says."""
        code, output, root = _run_contract_module(tmp_path, shadow=True, require=require)
        # 5 is pytest's "no tests collected": a module-level skip leaves the child with nothing to run. In a
        # whole-suite run the other modules still run and the exit is 0 - the silence #165 is about.
        assert code in (0, 5), output
        assert root is not None and int(root.get('tests')) == int(root.get('skipped')), output

    @pytest.mark.parametrize('require', ['1', 'true'])
    def test_with_the_variable_a_broken_import_fails_the_run(self, tmp_path, require):
        """The CI behaviour: the same ImportError is now a collection error carrying its real message. Any
        value but empty or `0` turns it on (D6), so a workflow writing `true` is not silently off."""
        code, output, _ = _run_contract_module(tmp_path, shadow=True, require=require)
        assert code not in (0, 5), output  # a collection error, not a pass or a quiet skip
        assert SIMULATED in output

    def test_with_the_variable_a_plain_import_error_still_fails_the_run(self, tmp_path):
        """D5's other half: a wheel that installed but will not load. The switch must not soften it."""
        code, output, _ = _run_contract_module(tmp_path, shadow=True, require='1', error='ImportError')
        assert code not in (0, 5), output
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
        code, output, root = _run_contract_module(tmp_path, shadow=False, require='1')
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

    def test_a_collection_error_does_not_stop_the_rest_of_the_suite(self):
        """Under the variable a broken streetlevel is a collection error, and pytest's default on any
        collection error is to run nothing at all: every PR would show "1 error" and none of the ~3,400
        other results until someone pinned around it (#171 review). The run must still fail - it does,
        a collection error exits nonzero with or without the flag - but with the rest of the signal kept."""
        step = next(line for line in self._text().splitlines() if 'python -m pytest tests' in line)
        assert '--continue-on-collection-errors' in step

    def test_the_job_has_a_timeout(self):
        """GitHub's default is six hours of a held runner for one hung test."""
        assert any(line.strip().startswith('timeout-minutes:') for line in self._text().splitlines())
