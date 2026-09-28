"""Tests for .coveragerc — the settings the CI coverage gate depends on (#57).

Nothing here measures coverage; these pin the *configuration*, because every way this setup fails is quiet.
`source` plus `omit` decides which files the number is an average over, and a directory that silently joins
or leaves that set moves the headline figure without moving any test. Three of the settings are load-bearing
in a way that is not obvious from reading them, so each gets a test that says why.
"""

import configparser
import fnmatch
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COVERAGERC = os.path.join(REPO_ROOT, '.coveragerc')

# The files the gate is an average over. Adding a production module means adding it here too - that is the
# point: a new module has to be a deliberate decision to measure or to omit, not a default of invisibility.
PRODUCTION_MODULES = {
    'CropRunner.py',
    'check_cvmetadata_schema.py',
    'DownloadRunner.py',
    'config.py',
    'cron_notify.py',
    'downloaders/__init__.py',
    'downloaders/common.py',
    'downloaders/gsv.py',
    'downloaders/mapillary.py',
    'downloaders/panoramax.py',
    'downloaders/store_sftp.py',
    'downscale_panos.py',
    'log_analyzer/analyze.py',
    'log_analyzer/roster.py',
    'migrate_depth_artifacts.py',
    'refetch_panos.py',
    'scrape_queue.py',
}

# Walked but never measured, and not worth listing in .coveragerc's omit: no .py lives under them.
_PRUNE_DIRS = {'.git', '__pycache__', '.pytest_cache'}


@pytest.fixture(scope='module')
def cfg():
    parser = configparser.ConfigParser()
    read = parser.read(COVERAGERC)
    assert read, f'.coveragerc not found at {COVERAGERC}'
    return parser


# Every omit pattern is written under the same root as `source`, so it means the same directory in a child
# running from tmp_path as in the parent (see test_every_omit_pattern_is_anchored_the_way_source_is).
OMIT_ROOT = '${SIDEWALK_COVERAGE_ROOT-.}'


def raw_omit_patterns(cfg):
    return [line.strip() for line in cfg['run']['omit'].splitlines()
            if line.strip() and not line.strip().startswith('#')]


def omit_patterns(cfg):
    """The omit patterns as repo-relative globs: the anchor stripped, which is what it expands to in the
    parent (`.`) and, relative to REPO_ROOT, in every child."""
    prefix = OMIT_ROOT + '/'
    return [p[len(prefix):] if p.startswith(prefix) else p for p in raw_omit_patterns(cfg)]


def is_omitted(relpath, patterns):
    """Whether coverage would drop relpath, the way coverage matches omit: fnmatch, where * spans separators."""
    return any(fnmatch.fnmatch(relpath, pattern) for pattern in patterns)


def measured_files(patterns):
    """Every .py under the repo that the omit list does not drop, as forward-slash relative paths."""
    found = set()
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for name in filenames:
            if not name.endswith('.py'):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), REPO_ROOT).replace(os.sep, '/')
            if not is_omitted(rel, patterns):
                found.add(rel)
    return found


class TestEveryProductionModuleIsMeasured:
    """`source = .` minus `omit` must resolve to exactly the set we think it does.

    The failure this prevents is not a red build, it is a green one: drop a directory into the repo and the
    coverage figure starts averaging over files nobody chose to measure, or stops averaging over files
    everybody assumed were measured. Neither shows up as a test failure anywhere else.
    """

    def test_the_measured_set_is_exactly_the_production_modules(self, cfg):
        measured = measured_files(omit_patterns(cfg))
        assert measured == PRODUCTION_MODULES, (
            'The set of files .coveragerc measures has changed.\n'
            f'  newly measured: {sorted(measured - PRODUCTION_MODULES)}\n'
            f'  no longer measured: {sorted(PRODUCTION_MODULES - measured)}\n'
            'Add the file to PRODUCTION_MODULES above if it should count toward the gate, or to '
            '.coveragerc\'s omit list if it should not. Do not "fix" this by deleting the assertion.')

    def test_the_check_would_fail_on_an_unlisted_module(self, cfg):
        """Guard the guard: the walk must actually flag a file no omit pattern covers.

        Without this, an omit list that accidentally matched everything would leave the test above comparing
        an empty set against an empty set and passing forever.
        """
        patterns = omit_patterns(cfg)
        assert not is_omitted('a_new_scraper.py', patterns)
        assert not is_omitted('newpackage/thing.py', patterns)
        # ... while the things we do omit stay omitted, so the matcher is not simply always-False.
        assert is_omitted('tests/test_docs.py', patterns)
        assert is_omitted('reports/scripts/rawlabels.py', patterns)
        assert is_omitted('flag_panos/json_to_csv.py', patterns)
        assert is_omitted('assets/make_banner.py', patterns)


class TestTheSettingsThatMakeSubprocessCoverageWork:
    """Three settings whose removal shows up as a *lower* number, not as an error (#57).

    The runners are driven as real subprocesses, so main(), the argparse type= validators and the budget
    carve-out only ever execute in a child. Measured with these on, DownloadRunner.py reads 97.6%; with
    subprocess capture broken it reads 87.9% and seven well-tested ranges look dead. Someone chasing that
    would write tests that already exist.
    """

    def test_parallel_is_on_so_children_do_not_clobber_the_parents_data(self, cfg):
        assert cfg['run'].getboolean('parallel') is True

    def test_source_survives_a_child_running_in_another_directory(self, cfg):
        """The subprocess helpers all spawn with cwd=tmp_path, and coverage resolves a relative source
        against each process's own CWD - so a bare `.` here means the children measure the temp directory.
        tests/conftest.py sets SIDEWALK_COVERAGE_ROOT to the repo root for exactly this reason.
        """
        source = cfg['run']['source'].strip()
        assert source == '${SIDEWALK_COVERAGE_ROOT-.}', (
            f'source is {source!r}. A bare "." silently measures tmp_path in every test subprocess; see '
            'pytest_configure in tests/conftest.py.')

    def test_every_omit_pattern_is_anchored_the_way_source_is(self, cfg):
        """The same CWD problem, for `omit`: coverage anchors a relative omit pattern to the process's CWD,
        so in a child spawned with cwd=tmp_path a bare `tests/*` means `<tmp_path>/tests/*`, and a repo file
        outside the production tree that the child imports gets measured. Not hypothetical (#171 review):
        test_suite_isolation's writer child loads tests/conftest.py from cwd=tmp_path, and CI's figure fell
        from 99.46% to 98.07% - 0.07 above the gate - on a conftest row nobody chose to measure.
        """
        unanchored = [p for p in raw_omit_patterns(cfg) if not p.startswith(OMIT_ROOT + '/')]
        assert not unanchored, (
            f'omit patterns not anchored to {OMIT_ROOT}: {unanchored}. A relative pattern stops excluding '
            'anything in a child whose CWD is not the repo root; see pytest_configure in tests/conftest.py.')

    def test_branch_coverage_is_on(self, cfg):
        """The finding that motivated the gate was an `if` that only ever went one way - three of the log
        analyzer's six alert rules never fired while every line around them was green."""
        assert cfg['run'].getboolean('branch') is True


class TestTheGateIsActuallyArmed:

    def test_fail_under_is_set(self, cfg):
        """A fail_under of 0 is the same as no gate, and reads like one that is working."""
        assert cfg['report'].getfloat('fail_under') > 0

    def test_exclude_also_is_used_rather_than_exclude_lines(self, cfg):
        """exclude_lines REPLACES coverage's own default (`# pragma: no cover`); exclude_also adds to it."""
        assert 'exclude_lines' not in cfg['report']
        assert 'exclude_also' in cfg['report']
