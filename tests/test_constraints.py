"""Pins the dependency files and the two CI jobs built on them (#167, #237).

requirements.txt carries floors only. Before constraints.txt, CI resolved the latest of everything on every
run while production kept whatever resolved at build time, recorded nowhere, so CI never tested what
production runs. constraints.txt is the box's `pip freeze`. The gated CI job installs with `-c constraints.txt`,
and a weekly job that does not gate installs without it, so drift in the latest releases shows up without
blocking anyone.

Since #237 there are three files. streetlevel sits alone in requirements-nodeps.txt and is installed with
--no-deps, because the requirements it declares pull torch and the CUDA wheels (several GB) into a venv that
never imports them; the requirements it does import are listed in requirements.txt. constraints-ci.txt pins
what the gated job installs and the box does not have: the pytest runner and the CPU-only torch that
tests/test_torch_geometry.py needs.

Most checks here are textual (no network, no pip): a constraints file that no longer satisfies requirements.txt
fails the CI install too, but with a resolver error that names neither file's edit as the cause. A few look at
the environment the suite runs in (the installed streetlevel's declared requirements and source, and whether the
GPU stack is installed). Those skip where streetlevel is absent or the venv is shared with GPU work, unless
SIDEWALK_REQUIRE_STREETLEVEL is set, as both CI workflows set it.
"""

import ast
import fnmatch
import glob
import importlib
import importlib.metadata
import importlib.util
import os
import re
import sys

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSTRAINTS = os.path.join(REPO_ROOT, 'constraints.txt')
CI_CONSTRAINTS = os.path.join(REPO_ROOT, 'constraints-ci.txt')
REQUIREMENTS = os.path.join(REPO_ROOT, 'requirements.txt')
NODEPS = os.path.join(REPO_ROOT, 'requirements-nodeps.txt')
WORKFLOWS = os.path.join(REPO_ROOT, '.github', 'workflows')
GATED_WORKFLOW = os.path.join(WORKFLOWS, 'tests.yml')
LATEST_WORKFLOW = os.path.join(WORKFLOWS, 'tests-latest.yml')

# streetlevel's declared requirements, split by the #237 measurement (reports/2026-11-15-streetlevel-no-deps.md):
# what `from streetlevel import streetview` imports, which requirements.txt must list, and what it never imports,
# which no requirement file may list (pyequilib is the one that pulls torch).
STREETLEVEL_REACHED = {'aiohttp', 'requests', 'numpy', 'pillow', 'pyproj', 'scipy', 'bd09convertor',
                       'coordinatesconverter', 'pyexiv2'}
STREETLEVEL_UNREACHED = {'pyequilib', 'protobuf', 'pyfrpc', 'pycryptodome', 'cython', 'pytest', 'pytest-asyncio'}

# `pip freeze` writes `name==version`; anything else (an editable, a URL, a `@ file://` from a local build)
# would make the file unusable as a record of what PyPI served.
_PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!-]+)$')


def _require_streetlevel():
    """The convention tests/test_streetlevel_api.py follows: any value but empty or `0` turns it on."""
    return os.environ.get('SIDEWALK_REQUIRE_STREETLEVEL', '') not in ('', '0')


def _content_lines(path):
    with open(path, encoding='utf-8') as f:
        lines = [line.strip() for line in f]
    return [line for line in lines if line and not line.startswith('#')]


def _pins(path=CONSTRAINTS):
    pins = {}
    label = os.path.basename(path)
    for line in _content_lines(path):
        match = _PIN.match(line)
        assert match, '%s line is not a plain `name==version` pin: %r' % (label, line)
        name = canonicalize_name(match.group(1))
        assert name not in pins, '%s pins %s twice' % (label, name)
        pins[name] = match.group(2)
    return pins


def _requirement_names(path):
    return {canonicalize_name(Requirement(line).name) for line in _content_lines(path)}


def _runtime_requirements():
    """Everything the box installs: requirements.txt, then requirements-nodeps.txt with --no-deps."""
    return [Requirement(line) for path in (REQUIREMENTS, NODEPS) for line in _content_lines(path)]


def _is_gpu_stack(name):
    """The distributions #237 took off the box. torch is deliberately not one: CI installs its CPU build."""
    name = canonicalize_name(name)
    return name in ('triton', 'pyequilib') or name.startswith('nvidia-') or name.startswith('cuda-')


def heavy_distributions(names):
    """The GPU-stack names among `names`, canonicalised and sorted. `None` is skipped: a distribution whose
    metadata is broken reports no name, and one broken dist-info must not hide the rest.

    >>> heavy_distributions(['numpy', 'nvidia_cublas', None, 'Triton', 'torch'])
    ['nvidia-cublas', 'triton']
    """
    return sorted({canonicalize_name(name) for name in names if name is not None and _is_gpu_stack(name)})


def unresolved_imports(source, resolves, is_stdlib):
    """The imports in `source` that a venv without streetlevel's unreached requirements could miss.

    Returns `(name, lineno, kind)` for
      * every non-stdlib import inside a function or class body (kind `in-function`), relative ones included:
        it runs only when that function is called, so an import-time probe cannot see it;
      * every module-level absolute import that is not stdlib, not streetlevel itself, and for which
        `resolves(top_level_name)` is false (kind `unresolved`).
    A relative name keeps its dots (`..x`).

    >>> unresolved_imports('import os\\ndef f():\\n    import torch\\n', lambda n: True, lambda n: n == 'os')
    [('torch', 3, 'in-function')]
    """
    found = []

    def check(name, lineno, nested, relative):
        top = name.split('.')[0]
        if not relative and is_stdlib(top):
            return
        if nested:
            found.append((name, lineno, 'in-function'))
        elif not relative and top != 'streetlevel' and not resolves(top):
            found.append((name, lineno, 'unresolved'))

    def visit(node, nested):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    check(alias.name, child.lineno, nested, False)
            elif isinstance(child, ast.ImportFrom):
                check('.' * child.level + (child.module or ''), child.lineno, nested, child.level > 0)
            visit(child, nested or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                                      ast.Lambda)))

    visit(ast.parse(source), False)
    return found


def _resolves(top):
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError):
        return False


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestTheConstraintsFile:

    def test_it_exists_and_is_a_list_of_exact_pins(self):
        pins = _pins()
        # The box's freeze had 65 packages on 2026-10-06; #237 deleted 41 by hand, leaving 24. A refresh after
        # the #237 deploy stays near that; a handful means a truncated paste or a freeze of the wrong venv.
        assert len(pins) >= 20, 'constraints.txt pins only %d packages' % len(pins)

    def test_every_runtime_requirement_is_pinned(self):
        """A requirement with no pin is resolved fresh on every CI run, which is the drift this file removes."""
        pins = _pins()
        missing = [str(req) for req in _runtime_requirements() if canonicalize_name(req.name) not in pins]
        assert not missing, 'requirements files name packages constraints.txt does not pin: %s' % missing

    def test_every_pin_satisfies_requirements_txt(self):
        """Raising a floor or moving streetlevel's range without refreshing the pins must fail here, by name,
        not as a pip resolver conflict in CI."""
        pins = _pins()
        unsatisfied = []
        for req in _runtime_requirements():
            version = pins.get(canonicalize_name(req.name))
            if version is not None and not req.specifier.contains(version, prereleases=True):
                unsatisfied.append('%s pinned at %s' % (req, version))
        assert not unsatisfied, 'constraints.txt no longer satisfies the requirement files: %s' % unsatisfied

    def test_it_pins_no_torch_and_no_gpu_stack(self):
        """The box has none of them after #237. A refresh from a venv that still holds them (the old one, or one
        a normal streetlevel install rebuilt) would put them back and hide that the box regressed."""
        bad = sorted(name for name in _pins() if name == 'torch' or _is_gpu_stack(name))
        assert not bad, 'constraints.txt pins the GPU stack #237 removed from the box: %s' % bad


class TestTheCiPinFile:
    """constraints-ci.txt: what the gated job installs and the box does not (#237)."""

    def test_it_is_exact_pins_disjoint_from_the_box_pins_and_free_of_the_gpu_stack(self):
        ci = _pins(CI_CONSTRAINTS)
        assert ci, 'constraints-ci.txt pins nothing'
        both = sorted(set(ci) & set(_pins()))
        assert not both, 'pinned in both constraints.txt and constraints-ci.txt: %s' % both
        gpu = sorted(name for name in ci if _is_gpu_stack(name))
        assert not gpu, 'constraints-ci.txt pins the GPU stack: %s' % gpu

    def test_it_pins_the_test_runner_and_torch(self):
        """Why the file exists (#237 D2/D3): the gated job's pytest and its CPU torch stay at tested versions, so
        a red run means a change in this repo, not a new release. Without these two pins both would float."""
        missing = sorted({'pytest', 'torch'} - set(_pins(CI_CONSTRAINTS)))
        assert not missing, 'constraints-ci.txt no longer pins %s' % missing

    def test_every_gated_install_line_uses_it(self):
        lines = _install_lines(GATED_WORKFLOW)
        assert lines, 'tests.yml has no pip install step'
        missing = [line for line in lines if '-c constraints-ci.txt' not in line]
        assert not missing, 'tests.yml install steps without -c constraints-ci.txt: %s' % missing


class TestStreetlevelIsInstalledWithoutItsDependencies:
    """#237: streetlevel alone in requirements-nodeps.txt, installed --no-deps, its imported requirements in
    requirements.txt."""

    def test_it_has_its_own_requirement_file(self):
        assert 'streetlevel' not in _requirement_names(REQUIREMENTS), \
            'streetlevel is back in requirements.txt, where a normal install pulls pyequilib -> torch'
        assert os.path.isfile(NODEPS), 'requirements-nodeps.txt is missing'
        assert [canonicalize_name(Requirement(line).name) for line in _content_lines(NODEPS)] == ['streetlevel']

    def test_the_reached_requirements_are_listed(self):
        missing = sorted(STREETLEVEL_REACHED - _requirement_names(REQUIREMENTS))
        assert not missing, 'requirements.txt lacks what streetlevel imports: %s' % missing

    def test_the_unreached_requirements_are_not(self):
        listed = sorted(STREETLEVEL_UNREACHED & (_requirement_names(REQUIREMENTS) | _requirement_names(NODEPS)))
        assert not listed, 'a requirement file lists what streetlevel never imports: %s' % listed

    def test_the_two_sets_do_not_overlap(self):
        assert not STREETLEVEL_REACHED & STREETLEVEL_UNREACHED

    def test_they_are_what_the_installed_streetlevel_declares(self):
        """A release that declares a new requirement must be measured before --no-deps silently omits it."""
        try:
            declared = importlib.metadata.requires('streetlevel')
        except importlib.metadata.PackageNotFoundError:
            if _require_streetlevel():
                raise
            pytest.skip('streetlevel is not installed here, and this run does not require it')
        names = {canonicalize_name(Requirement(req).name) for req in declared or []}
        assert names == STREETLEVEL_REACHED | STREETLEVEL_UNREACHED, (
            'streetlevel now declares %s and no longer declares %s: measure again (the report named above) '
            'before changing either set' % (sorted(names - STREETLEVEL_REACHED - STREETLEVEL_UNREACHED),
                                            sorted((STREETLEVEL_REACHED | STREETLEVEL_UNREACHED) - names)))


class TestTheImportWalker:
    """unresolved_imports, on a literal source with known answers."""

    SOURCE = '\n'.join([
        'import os',                          # 1 stdlib: never reported
        'import not_a_real_module_237',       # 2 module level, unresolved
        'from . import sibling',              # 3 module-level relative: inside the package
        'import streetlevel.geo',             # 4 the package itself
        'def f():',                           # 5
        '    import json',                    # 6 stdlib, even in a function
        '    import heavy_thing',             # 7 in-function absolute
        '    from ..x import y',              # 8 in-function relative
        'class C:',                           # 9
        '    from os import path',            # 10 stdlib in a class body
        '    import other_heavy',             # 11 in a class body
    ]) + '\n'

    def test_it_finds_the_unresolved_and_the_lazy_imports(self):
        found = unresolved_imports(self.SOURCE, lambda top: top != 'not_a_real_module_237',
                                   lambda top: top in ('os', 'json'))
        assert found == [('not_a_real_module_237', 2, 'unresolved'), ('heavy_thing', 7, 'in-function'),
                         ('..x', 8, 'in-function'), ('other_heavy', 11, 'in-function')]


class TestTheInstalledStreetlevel:
    """What the installed streetlevel actually needs, checked in the venv the suite runs in."""

    def test_the_modules_this_repo_loads_import_nothing_lazily_or_unresolved(self):
        """--no-deps is safe only while every import streetlevel can reach on our call paths is at module level
        (so the hard import in tests/test_streetlevel_api.py and the deploy's import check see it) and
        resolves here."""
        try:
            importlib.import_module('streetlevel.streetview.api')
        except ModuleNotFoundError:
            if _require_streetlevel():
                raise
            pytest.skip('streetlevel is not importable here, and this run does not require it')
        stdlib = set(sys.stdlib_module_names)
        checked, found = 0, []
        for name, module in sorted(list(sys.modules.items()), key=lambda item: item[0]):
            if module is None or not (name == 'streetlevel' or name.startswith('streetlevel.')):
                continue
            path = getattr(module, '__file__', None)
            if not path or not path.endswith('.py'):
                continue
            found += [(name,) + hit for hit in unresolved_imports(_read(path), _resolves, stdlib.__contains__)]
            checked += 1
        assert checked >= 10, 'only %d streetlevel modules were loaded and checked' % checked
        assert not found, 'streetlevel imports these lazily or cannot resolve them here: %s' % found


class TestTheGpuStackIsNotInstalled:

    def test_heavy_distributions_reads_names_as_pip_does(self):
        names = ['numpy', 'nvidia_cublas', None, 'Triton', 'pyequilib', 'cuda-toolkit', 'torch', 'nvidia-nvtx',
                 'NVIDIA-NVTX', 'cudatext']
        assert heavy_distributions(names) == ['cuda-toolkit', 'nvidia-cublas', 'nvidia-nvtx', 'pyequilib',
                                              'triton']

    def test_this_environment_has_no_gpu_stack(self):
        """CI must install the --no-deps way the box does. A dev venv shared with GPU work skips instead."""
        heavy = heavy_distributions(dist.metadata['Name'] for dist in importlib.metadata.distributions())
        if heavy and not _require_streetlevel():
            pytest.skip('this venv holds the GPU stack (%d distributions), and this run does not require its '
                        'absence' % len(heavy))
        assert not heavy, 'the GPU stack #237 removed is installed: %s' % heavy


def _install_lines(path):
    """Every `pip install` a workflow runs: one-line `run:` steps, and the lines inside a block-scalar
    `run: |` / `run: >` step (an extra install there could upgrade a package past its pin). Comments that
    mention installing are not steps and are skipped."""
    lines = _read(path).splitlines()
    found = []
    i = 0
    while i < len(lines):
        line = lines[i]
        block = re.match(r'^(\s*)-?\s*run:\s*[|>][-+]?\s*(#.*)?$', line)
        if block:
            indent = len(line) - len(line.lstrip())
            i += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                body = lines[i].strip()
                if re.search(r'\bpip3? install\b', body) and not body.startswith('#'):
                    found.append(body)
                i += 1
            continue
        if re.match(r'^\s*-?\s*run:.*\bpip3? install\b', line):
            found.append(line.strip())
        i += 1
    return found


def _job_lines(path, job):
    """The lines of one job in a workflow: from `  <job>:` to the next two-space-indented key."""
    lines = _read(path).splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r'^  %s:\s*$' % re.escape(job), line)), None)
    assert start is not None, '%s has no job %r' % (os.path.basename(path), job)
    end = next((i for i in range(start + 1, len(lines)) if re.match(r'^  \S', lines[i])), len(lines))
    return lines[start:end]


_WORKFLOWS_WITH_STREETLEVEL = [pytest.param(GATED_WORKFLOW, 'pytest', id='tests.yml'),
                               pytest.param(LATEST_WORKFLOW, 'pytest-latest', id='tests-latest.yml')]


class TestTheWorkflowsInstallStreetlevelWithoutItsDependencies:

    @pytest.mark.parametrize('path, job', _WORKFLOWS_WITH_STREETLEVEL)
    def test_requirements_nodeps_is_installed_with_no_deps_and_nothing_else_is(self, path, job):
        lines = _install_lines(path)
        nodeps = [line for line in lines if 'requirements-nodeps.txt' in line]
        assert nodeps, '%s never installs requirements-nodeps.txt' % os.path.basename(path)
        assert all(re.search(r'(^|\s)--no-deps(\s|$)', line) for line in nodeps), nodeps
        # --no-deps on requirements.txt would drop every transitive dependency of everything in it.
        wide = [line for line in lines if '--no-deps' in line
                and re.search(r'(^|\s)-r\s+requirements(-dev)?\.txt(\s|$)', line)]
        assert not wide, wide

    @pytest.mark.parametrize('path, job', _WORKFLOWS_WITH_STREETLEVEL)
    def test_torch_is_imported_after_it_is_installed(self, path, job):
        """A torch that cannot import makes tests/test_torch_geometry.py skip, not fail, and the coverage gate
        passes without it; an explicit import step fails the job instead."""
        lines = _job_lines(path, job)
        installs = [i for i, line in enumerate(lines)
                    if re.match(r'^\s*-?\s*run:.*\bpip3? install\b.*\btorch\b.*--index-url', line)]
        assert installs, 'the %s job installs no CPU torch' % job
        imports = [i for i, line in enumerate(lines) if re.match(r'^\s*-?\s*run:\s*python -c "import torch', line)]
        assert any(i > installs[-1] for i in imports), \
            'the %s job never imports torch after installing it' % job


# The documents that show an install: a block that installs requirements.txt must also install
# requirements-nodeps.txt, the --no-deps way, or a reader following it gets a venv without streetlevel.
DOC_FILES = sorted([os.path.join(REPO_ROOT, name) for name in ('README.md', 'CONTRIBUTING.md', 'CLAUDE.md')]
                   + glob.glob(os.path.join(REPO_ROOT, 'docs', '*.md'))
                   + glob.glob(os.path.join(REPO_ROOT, '.claude', 'rules', '*.md')))
DOCS_THAT_SHOW_IT = ['README.md', 'CONTRIBUTING.md', 'CLAUDE.md', os.path.join('docs', 'downloader.md'),
                     os.path.join('docs', 'testing.md'), os.path.join('docs', 'ops.md')]
_INSTALLS_REQUIREMENTS = re.compile(r'\bpip3?\s+install\b.*(^|\s)-r\s+requirements\.txt(\s|$)')
_INSTALLS_NODEPS = re.compile(r'\bpip3?\s+install\b.*--no-deps\s+-r\s+requirements-nodeps\.txt(\s|$)')


def _fenced_blocks(path):
    """Each fenced code block in a Markdown file, as (line number of its opening fence, its lines)."""
    blocks, current, start = [], None, None
    for number, line in enumerate(_read(path).splitlines(), 1):
        if line.strip().startswith('```'):
            if current is None:
                current, start = [], number
            else:
                blocks.append((start, current))
                current = None
        elif current is not None:
            current.append(line)
    return blocks


def _rel(path):
    return os.path.relpath(path, REPO_ROOT)


class TestTheDocsShowBothInstallLines:

    def test_every_block_that_installs_requirements_txt_also_installs_streetlevel(self):
        stale = ['%s:%d' % (_rel(path), start) for path in DOC_FILES for start, lines in _fenced_blocks(path)
                 if any(_INSTALLS_REQUIREMENTS.search(line) for line in lines)
                 and not any(_INSTALLS_NODEPS.search(line) for line in lines)]
        assert not stale, 'code blocks that install requirements.txt but not requirements-nodeps.txt: %s' % stale

    def test_every_install_of_requirements_nodeps_has_no_deps(self):
        bad = ['%s:%d' % (_rel(path), number) for path in DOC_FILES
               for number, line in enumerate(_read(path).splitlines(), 1)
               if re.search(r'\bpip3?\s+install\b', line) and 'requirements-nodeps.txt' in line
               and '--no-deps' not in line]
        assert not bad, 'requirements-nodeps.txt installed without --no-deps: %s' % bad

    @pytest.mark.parametrize('name', DOCS_THAT_SHOW_IT)
    def test_the_install_pages_show_the_second_line(self, name):
        assert any(_INSTALLS_NODEPS.search(line) for line in _read(os.path.join(REPO_ROOT, name)).splitlines()), \
            '%s shows no `pip install --no-deps -r requirements-nodeps.txt` line' % name


OPS = os.path.join(REPO_ROOT, 'docs', 'ops.md')
DEPLOYING = '### Deploying'
BOX_MOVE = '### Moving the box to the --no-deps install (#237)'
# The one check that catches every wrong install the #244 review measured (line 2 without -c or without --no-deps,
# a by-name streetlevel install, the old one-line install, line 1 alone): the venv's freeze IS constraints.txt.
FREEZE_CHECK = "diff <({venv}/bin/pip freeze | sort -f) <(grep -v '^#' constraints.txt | grep . | sort -f)"
ROLLBACK = '[ -L .venv ] && unlink .venv; [ -e .venv ] || mv .venv-before-237 .venv'


def _ops_section(heading):
    """docs/ops.md from `heading` to the next heading of the same or a higher level."""
    lines = _read(OPS).splitlines()
    start = lines.index(heading)
    level = len(heading) - len(heading.lstrip('#'))
    fenced = False
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith('```'):
            fenced = not fenced
        elif not fenced and re.match(r'^#{1,%d} ' % level, lines[i]):   # a shell comment in a block is no heading
            return lines[start:i]
    return lines[start:]


def _first_block(section):
    start = next(i for i, line in enumerate(section) if line.strip().startswith('```'))
    end = next(i for i in range(start + 1, len(section)) if section[i].strip().startswith('```'))
    return section[start + 1:end]


class TestTheDeployInstructions:
    """docs/ops.md's Deploying block and the #237 move (#244 review): what they check, and that the move never
    leaves the box without a .venv."""

    @pytest.mark.parametrize('heading, venvs', [(DEPLOYING, ['.venv']), (BOX_MOVE, ['.venv-237', '.venv'])],
                             ids=['deploying', 'box-move'])
    def test_the_block_compares_the_freeze_with_constraints_txt_and_imports_streetlevel(self, heading, venvs):
        block = _first_block(_ops_section(heading))
        for venv in venvs:
            assert any(FREEZE_CHECK.format(venv=venv) in line for line in block), (heading, venv)
            assert any(line.startswith('%s/bin/python -c "from streetlevel import streetview' % venv)
                       for line in block), (heading, venv)

    def test_the_move_builds_beside_the_old_venv_and_swaps_in_one_command(self):
        block = _first_block(_ops_section(BOX_MOVE))
        assert not any(re.match(r'^python3 -m venv \.venv(\s|$)', line) for line in block), \
            'the move builds the new venv at .venv, so the box has none until the build finishes'
        build = next(i for i, line in enumerate(block) if line.startswith('python3 -m venv .venv-237'))
        swaps = [i for i, line in enumerate(block) if line.startswith('mv .venv ')]
        assert len(swaps) == 1, swaps
        assert block[swaps[0]].startswith('mv .venv .venv-before-237 && ln -s .venv-237 .venv'), block[swaps[0]]
        checks = [i for i, line in enumerate(block) if FREEZE_CHECK.format(venv='.venv-237') in line]
        assert build < min(checks) and max(checks) < swaps[0], 'the new venv must be checked before the swap'

    def test_the_rollback_works_from_any_state(self):
        assert any(ROLLBACK in line for line in _ops_section(BOX_MOVE))

    def test_every_name_the_move_leaves_in_the_checkout_is_ignored(self):
        """The Deploying block requires an empty `git status --short`; an untracked .venv-before-237/ (or a
        .venv that is a symlink, which `.venv/` does not match) would block every later deploy."""
        with open(os.path.join(REPO_ROOT, '.gitignore'), encoding='utf-8') as f:
            patterns = [line.strip() for line in f if line.strip() and not line.startswith('#')]

        for name in ('.venv', '.venv-237', '.venv-before-237'):
            # Each is checked as a plain file, as git sees a symlink: a pattern ending in / does not match it.
            assert any(not pattern.endswith('/') and fnmatch.fnmatch(name, pattern.lstrip('/'))
                       for pattern in patterns), name

    def test_the_depth_unavailable_remedy_reinstalls_streetlevel(self):
        row = next(line for line in _read(OPS).splitlines()
                   if line.startswith('| `WARNING - streetlevel is not importable`'))
        assert 'requirements-nodeps.txt' in row and '--no-deps' in row, row

    def test_the_deploying_section_says_the_resolver_error_is_expected(self):
        text = '\n'.join(_ops_section(DEPLOYING))
        assert "ERROR: pip's dependency resolver" in text and 'pyequilib' in text


class TestTheGatedJobInstallsThePins:

    def test_the_install_uses_the_constraints(self):
        lines = _install_lines(GATED_WORKFLOW)
        assert lines, 'tests.yml has no pip install step'
        assert all('-c constraints.txt' in line for line in lines), lines

    def test_the_pip_cache_is_keyed_on_the_constraints(self):
        """setup-python keys its pip cache on requirements files by default; a refresh of the pins must
        not be served the previous resolve, nor a changed requirements-nodeps.txt."""
        lines = _read(GATED_WORKFLOW).splitlines()
        start = next((i for i, line in enumerate(lines) if line.strip() == 'cache-dependency-path: |'), None)
        assert start is not None, 'tests.yml sets no cache-dependency-path block'
        indent = len(lines[start]) - len(lines[start].lstrip())
        block = []
        for line in lines[start + 1:]:
            if not line.strip() or len(line) - len(line.lstrip()) <= indent:
                break
            block.append(line.strip())
        for name in ('constraints.txt', 'constraints-ci.txt', 'requirements-nodeps.txt'):
            assert name in block, (name, block)


class TestTheWeeklyJobInstallsLatest:

    def test_it_installs_without_the_constraints(self):
        lines = _install_lines(LATEST_WORKFLOW)
        assert lines, 'tests-latest.yml has no pip install step'
        assert not any('constraints' in line for line in lines), lines

    def test_no_constraint_reaches_pip_through_the_environment(self):
        """pip reads PIP_CONSTRAINT from the environment exactly as it reads -c, so setting it on the job or a
        step would pin the weekly job silently while its install line still looks unconstrained."""
        assert not re.search(r'\bPIP_CONSTRAINT\b', _read(LATEST_WORKFLOW)), \
            'tests-latest.yml sets PIP_CONSTRAINT, which pins the install without a -c'

    def test_it_runs_on_a_schedule_and_never_gates_a_pull_request(self):
        text = _read(LATEST_WORKFLOW)
        assert re.search(r'^\s*schedule:', text, re.M), 'tests-latest.yml has no schedule trigger'
        # pull_request_target and workflow_run would put it on a PR as surely as pull_request does, and
        # workflow_call would let a gated workflow do the same by calling it.
        assert not re.search(r'^\s*(pull_request\w*|push|workflow_run|workflow_call|merge_group):', text, re.M), \
            'tests-latest.yml must not run on PRs or pushes: it is a drift report, not a gate'

    def test_nothing_switches_it_off(self):
        """An `if:` on the job or a step (`if: false`, or one that is never true on a schedule) would leave a
        green-looking workflow that tests nothing. The job has no condition, so none is allowed."""
        conditions = [line.strip() for line in _read(LATEST_WORKFLOW).splitlines()
                      if re.match(r'^\s*-?\s*if:', line)]
        assert not conditions, conditions

    @pytest.mark.parametrize('needle', ["SIDEWALK_REQUIRE_STREETLEVEL: '1'", 'timeout-minutes:'])
    def test_it_keeps_the_gated_jobs_rails(self, needle):
        """A streetlevel release that breaks the import is exactly the drift this job exists to catch, so the
        import must be hard here too (#165); and a hang must still fail in minutes."""
        assert needle in _read(LATEST_WORKFLOW)
