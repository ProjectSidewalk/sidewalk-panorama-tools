"""Pins constraints.txt, the production box's frozen dependency set, and the two CI jobs built on it (#167).

requirements.txt carries floors only. Before constraints.txt, CI resolved the latest of everything on every
run while production kept whatever resolved at build time, recorded nowhere, so CI never tested what
production runs. constraints.txt is the box's `pip freeze`. The gated CI job installs with `-c constraints.txt`,
and a weekly job that does not gate installs without it, so drift in the latest releases shows up without
blocking anyone.

The checks here are textual (no network, no pip): a constraints file that no longer satisfies requirements.txt
fails the CI install too, but with a resolver error that names neither file's edit as the cause.
"""

import os
import re

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSTRAINTS = os.path.join(REPO_ROOT, 'constraints.txt')
REQUIREMENTS = os.path.join(REPO_ROOT, 'requirements.txt')
WORKFLOWS = os.path.join(REPO_ROOT, '.github', 'workflows')
GATED_WORKFLOW = os.path.join(WORKFLOWS, 'tests.yml')
LATEST_WORKFLOW = os.path.join(WORKFLOWS, 'tests-latest.yml')

# `pip freeze` writes `name==version`; anything else (an editable, a URL, a `@ file://` from a local build)
# would make the file unusable as a record of what PyPI served.
_PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!-]+)$')


def _content_lines(path):
    with open(path, encoding='utf-8') as f:
        lines = [line.strip() for line in f]
    return [line for line in lines if line and not line.startswith('#')]


def _pins():
    pins = {}
    for line in _content_lines(CONSTRAINTS):
        match = _PIN.match(line)
        assert match, 'constraints.txt line is not a plain `name==version` pin: %r' % (line,)
        name = canonicalize_name(match.group(1))
        assert name not in pins, 'constraints.txt pins %s twice' % (name,)
        pins[name] = match.group(2)
    return pins


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestTheConstraintsFile:

    def test_it_exists_and_is_a_list_of_exact_pins(self):
        pins = _pins()
        # The box's freeze had 65 packages on 2026-10-06. Any real refresh stays in that range; a handful
        # means a truncated paste or a freeze of the wrong venv.
        assert len(pins) >= 30, 'constraints.txt pins only %d packages' % len(pins)

    def test_every_runtime_requirement_is_pinned(self):
        """A requirement with no pin is resolved fresh on every CI run, which is the drift this file removes."""
        pins = _pins()
        missing = [str(req) for req in map(Requirement, _content_lines(REQUIREMENTS))
                   if canonicalize_name(req.name) not in pins]
        assert not missing, 'requirements.txt names packages constraints.txt does not pin: %s' % missing

    def test_every_pin_satisfies_requirements_txt(self):
        """Raising a floor or moving streetlevel's range in requirements.txt without refreshing the pins
        must fail here, by name, not as a pip resolver conflict in CI."""
        pins = _pins()
        unsatisfied = []
        for req in map(Requirement, _content_lines(REQUIREMENTS)):
            version = pins.get(canonicalize_name(req.name))
            if version is not None and not req.specifier.contains(version, prereleases=True):
                unsatisfied.append('%s pinned at %s' % (req, version))
        assert not unsatisfied, 'constraints.txt no longer satisfies requirements.txt: %s' % unsatisfied


def _install_lines(path):
    """The `run:` steps that install, not comments that mention installing."""
    return [line for line in _read(path).splitlines()
            if re.match(r'^\s*-?\s*run:.*\bpip install\b', line)]


class TestTheGatedJobInstallsThePins:

    def test_the_install_uses_the_constraints(self):
        lines = _install_lines(GATED_WORKFLOW)
        assert lines, 'tests.yml has no pip install step'
        assert all('-c constraints.txt' in line for line in lines), lines

    def test_the_pip_cache_is_keyed_on_the_constraints(self):
        """setup-python keys its pip cache on requirements files by default; a refresh of the pins must
        not be served the previous resolve."""
        lines = _read(GATED_WORKFLOW).splitlines()
        start = next((i for i, line in enumerate(lines) if line.strip() == 'cache-dependency-path: |'), None)
        assert start is not None, 'tests.yml sets no cache-dependency-path block'
        indent = len(lines[start]) - len(lines[start].lstrip())
        block = []
        for line in lines[start + 1:]:
            if not line.strip() or len(line) - len(line.lstrip()) <= indent:
                break
            block.append(line.strip())
        assert 'constraints.txt' in block, block


class TestTheWeeklyJobInstallsLatest:

    def test_it_installs_without_the_constraints(self):
        lines = _install_lines(LATEST_WORKFLOW)
        assert lines, 'tests-latest.yml has no pip install step'
        assert not any('constraints' in line for line in lines), lines

    def test_it_runs_on_a_schedule_and_never_gates_a_pull_request(self):
        text = _read(LATEST_WORKFLOW)
        assert re.search(r'^\s*schedule:', text, re.M), 'tests-latest.yml has no schedule trigger'
        assert not re.search(r'^\s*(pull_request|push):', text, re.M), \
            'tests-latest.yml must not run on PRs or pushes: it is a drift report, not a gate'

    @pytest.mark.parametrize('needle', ["SIDEWALK_REQUIRE_STREETLEVEL: '1'", 'timeout-minutes:'])
    def test_it_keeps_the_gated_jobs_rails(self, needle):
        """A streetlevel release that breaks the import is exactly the drift this job exists to catch, so the
        import must be hard here too (#165); and a hang must still fail in minutes."""
        assert needle in _read(LATEST_WORKFLOW)
