"""sidewalk_panorama_tools.gsv_tiles: the GSV tile fetcher, moved into the package (#222 step 2).

The moved functions' behaviour is pinned where it always was - tests/test_gsv_stitcher.py,
tests/test_gsv_photometa_zoom.py, tests/test_gsv_tile_contract.py and the runner and refetch suites - which
now patch the names on the module that looks them up. What is pinned here is the move itself: one definition
behind every re-export, gsv's own code reading the moved names through the package, the settings and the
configure() call that replaced config.py's import, the [gsv] extra, and the runtime check
(tests/conftest.py's mis_targeted_patches) that a patch lands where the name is looked up.
"""

import ast
import json
import os
import subprocess
import sys
from io import BytesIO

import pytest
from PIL import Image

import config
import conftest
from downloaders import common, gsv
from sidewalk_panorama_tools import gsv_tiles, store

REPO_ROOT = conftest.REPO_ROOT
GSV_SOURCE = os.path.join(REPO_ROOT, 'downloaders', 'gsv.py')


def run_isolated(code):
    """Run `code` in a fresh interpreter with the checkout on sys.path and nothing else imported for it."""
    prelude = 'import sys; sys.path.insert(0, %r)\n' % (REPO_ROOT,)
    return subprocess.run([sys.executable, '-I', '-c', prelude + code], capture_output=True, text=True,
                          timeout=120)


class TestOneDefinition:

    @pytest.mark.parametrize('name', gsv.RE_EXPORTED_FROM_GSV_TILES)
    def test_gsv_re_exports_the_package_object(self, name):
        assert getattr(gsv, name) is getattr(gsv_tiles, name)

    def test_the_seams_are_re_exports(self):
        assert set(gsv.GSV_COMPOSED_SEAMS) <= set(gsv.RE_EXPORTED_FROM_GSV_TILES)

    def test_every_re_export_is_listed_once(self):
        assert len(set(gsv.RE_EXPORTED_FROM_GSV_TILES)) == len(gsv.RE_EXPORTED_FROM_GSV_TILES)

    def test_every_name_gsv_imports_from_the_package_is_listed(self):
        """The list the identity checks walk is the import block itself, so a new re-export is checked."""
        tree = ast.parse(open(GSV_SOURCE, encoding='utf-8').read())
        imported = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                    and node.module == 'sidewalk_panorama_tools.gsv_tiles' for alias in node.names}
        assert imported == set(gsv.RE_EXPORTED_FROM_GSV_TILES)

    def test_one_black_fraction(self):
        assert gsv._black_fraction is gsv_tiles._black_fraction is store.black_fraction is common.black_fraction

    def test_probe_zoom_is_the_probe(self):
        assert gsv_tiles.probe_zoom is gsv_tiles._probe_zoom

    @pytest.mark.parametrize('name', gsv_tiles.__all__)
    def test_every_public_name_exists(self, name):
        assert hasattr(gsv_tiles, name)


class TestGsvReadsTheMovedNamesThroughThePackage:
    """gsv's own code calls a moved name qualified (gsv_tiles.X), so each name has one lookup site. The runtime
    check in conftest sees a patch on the wrong module; this sees the code that would make one necessary."""

    def test_no_bare_reference_to_a_moved_name_outside_the_seams(self):
        tree = ast.parse(open(GSV_SOURCE, encoding='utf-8').read())
        moved = set(gsv.RE_EXPORTED_FROM_GSV_TILES) - set(gsv.GSV_COMPOSED_SEAMS)
        bare = sorted((node.lineno, node.id) for node in ast.walk(tree)
                      if isinstance(node, ast.Name) and node.id in moved)
        assert bare == [], 'call these as gsv_tiles.<name>: %r' % (bare,)

    def test_the_seams_are_called_by_gsvs_own_binding(self):
        """The other half: download_single_pano and _frame_refusal must find the seams on gsv, which is
        where refetch_panos and the suite patch them."""
        tree = ast.parse(open(GSV_SOURCE, encoding='utf-8').read())
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert set(gsv.GSV_COMPOSED_SEAMS) <= called

    @pytest.mark.parametrize('name', ['headers_list', 'proxies', 'thread_count', '_proxies'])
    def test_gsv_keeps_no_binding_of_a_setting_the_package_owns(self, name):
        """A binding here would go stale on configure() and let a stale patch of it pass silently."""
        assert not hasattr(gsv, name)


class TestSettings:

    def test_gsv_configured_the_package_from_config_py(self):
        assert gsv_tiles.HEADERS == tuple(config.headers_list)
        assert gsv_tiles.PROXIES == gsv_tiles._normalize_proxies(config.proxies)
        assert gsv_tiles.CONCURRENCY == config.thread_count

    def test_config_py_and_the_package_default_carry_the_same_headers(self):
        """config.py stays the operator's file; the package's default is a copy, held equal here."""
        assert tuple(config.headers_list) == gsv_tiles.DEFAULT_HEADERS
        assert isinstance(gsv_tiles.DEFAULT_HEADERS, tuple)

    def test_the_package_defaults_without_downloaders(self):
        """Importing downloaders.gsv configures the package, so the defaults are read in a fresh process."""
        result = run_isolated(
            'import json, sidewalk_panorama_tools.gsv_tiles as t\n'
            'print(json.dumps([t.HEADERS is t.DEFAULT_HEADERS, t.PROXIES, t.CONCURRENCY,'
            ' "downloaders" in sys.modules, "config" in sys.modules]))\n')
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [True, {}, 8, False, False]

    @pytest.fixture
    def restored(self, monkeypatch):
        """configure() rebinds module globals; snapshot them through monkeypatch so nothing leaks."""
        for name in ('HEADERS', 'PROXIES', 'CONCURRENCY'):
            monkeypatch.setattr(gsv_tiles, name, getattr(gsv_tiles, name))

    def test_configure_sets_each_and_keeps_the_rest(self, restored):
        before = (gsv_tiles.HEADERS, gsv_tiles.PROXIES, gsv_tiles.CONCURRENCY)
        gsv_tiles.configure()
        assert (gsv_tiles.HEADERS, gsv_tiles.PROXIES, gsv_tiles.CONCURRENCY) == before
        gsv_tiles.configure(headers=[{'User-Agent': 'x'}])
        assert gsv_tiles.HEADERS == ({'User-Agent': 'x'},)
        assert gsv_tiles.PROXIES == before[1] and gsv_tiles.CONCURRENCY == before[2]
        gsv_tiles.configure(concurrency=3)
        assert gsv_tiles.CONCURRENCY == 3

    def test_configure_normalizes_the_proxies(self, restored):
        gsv_tiles.configure(proxies={'http': 'http://', 'https': 'http://proxy.example:3128'})
        assert gsv_tiles.PROXIES == {'http': None, 'https': 'http://proxy.example:3128'}

    @pytest.mark.parametrize('bad', [0, -1, 1.5, True, '8'])
    def test_a_concurrency_that_is_not_a_positive_whole_number_is_refused(self, restored, bad):
        with pytest.raises((ValueError, TypeError)):
            gsv_tiles.configure(concurrency=bad)
        assert gsv_tiles.CONCURRENCY == config.thread_count

    def test_no_headers_is_refused(self, restored):
        with pytest.raises(ValueError):
            gsv_tiles.configure(headers=[])

    def test_the_header_rotation_reads_the_setting_at_call_time(self, restored):
        gsv_tiles.configure(headers=[{'User-Agent': 'only'}])
        assert gsv_tiles._random_header() == {'User-Agent': 'only'}

    def test_a_reconfigured_proxy_reaches_the_sessions_left_in_gsv(self, restored, monkeypatch):
        """The stale-binding bug the qualified calls exist to prevent: the photometa (depth) session and the
        photosphere fetch live in gsv, and must follow configure() rather than an import-time copy."""
        gsv_tiles.configure(proxies={'http': 'http://', 'https': 'http://proxy.example:3128'})
        with gsv._depth_session() as session:
            assert session.proxies == {'https': 'http://proxy.example:3128'}


class TestTheExtra:

    def test_without_it_the_import_says_which_extra(self):
        result = run_isolated(
            'sys.modules["aiohttp"] = None\n'
            'try:\n'
            '    import sidewalk_panorama_tools.gsv_tiles\n'
            'except ImportError as e:\n'
            '    print(e)\n'
            '    print(type(e.__cause__).__name__)\n')
        assert result.returncode == 0, result.stderr
        message, cause = result.stdout.splitlines()
        assert message == ("sidewalk_panorama_tools.gsv_tiles needs the [gsv] extra: "
                           "pip install 'sidewalk-panorama-tools[gsv]'")
        assert cause in ('ImportError', 'ModuleNotFoundError')

    def test_the_package_does_not_import_it(self):
        result = run_isolated('import sidewalk_panorama_tools\n'
                              'print("sidewalk_panorama_tools.gsv_tiles" in sys.modules, "aiohttp" in sys.modules)\n')
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ['False', 'False']

    def test_the_extra_carries_requirements_txt_floors(self):
        """pyproject.toml's [gsv] extra and requirements.txt name the same four packages at the same floors."""
        import re
        with open(os.path.join(REPO_ROOT, 'pyproject.toml'), encoding='utf-8') as f:
            pyproject = f.read()
        extra = re.search(r'^gsv = \[(.*)\]$', pyproject, re.M)
        assert extra, 'pyproject.toml has no [gsv] extra'
        extra = sorted(s.strip().strip('"') for s in extra.group(1).split(','))
        with open(os.path.join(REPO_ROOT, 'requirements.txt'), encoding='utf-8') as f:
            floors = {line.split('>=')[0].lower(): line.strip() for line in f
                      if line.split('>=')[0].lower() in ('requests', 'urllib3', 'aiohttp', 'backoff')}
        assert extra == sorted(floors.values())


class TestThePatchTargetCheck:
    """conftest.mis_targeted_patches, driven directly: each patch is undone before the call phase ends, so the
    hook that runs it after every test sees a clean state."""

    def test_a_clean_state_reports_nothing(self):
        assert conftest.mis_targeted_patches() == []

    def test_a_package_name_patched_on_gsv_is_named(self, monkeypatch):
        monkeypatch.setattr(gsv, '_download_tiles', lambda tiles: None)
        problems = conftest.mis_targeted_patches()
        monkeypatch.undo()
        assert len(problems) == 1 and 'downloaders.gsv._download_tiles' in problems[0]

    def test_the_same_patch_on_the_package_is_fine(self, monkeypatch):
        monkeypatch.setattr(gsv_tiles, '_download_tiles', lambda tiles: None)
        problems = conftest.mis_targeted_patches()
        monkeypatch.undo()
        assert problems == []

    @pytest.mark.parametrize('seam', gsv.GSV_COMPOSED_SEAMS)
    def test_a_seam_patched_on_the_package_is_named(self, monkeypatch, seam):
        monkeypatch.setattr(gsv_tiles, seam, lambda *a: None)
        problems = conftest.mis_targeted_patches()
        monkeypatch.undo()
        assert any('gsv_tiles.%s' % seam in p for p in problems)

    @pytest.mark.parametrize('seam', gsv.GSV_COMPOSED_SEAMS)
    def test_a_seam_patched_on_gsv_is_fine(self, monkeypatch, seam):
        monkeypatch.setattr(gsv, seam, lambda *a: None)
        problems = conftest.mis_targeted_patches()
        monkeypatch.undo()
        assert problems == []

    def test_the_hook_fails_a_test_that_leaves_one(self, tmp_path):
        target = tmp_path / 'test_mis_targeted.py'
        target.write_text(
            'from downloaders import gsv\n'
            'def test_patches_the_re_export(monkeypatch):\n'
            '    monkeypatch.setattr(gsv, "_get_response", lambda *a, **k: None)\n')
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
        assert 'sidewalk_panorama_tools.gsv_tiles._get_response instead' in output, output


class TestTheOneUrlBuilder:
    """tile_url is the one place a cbk tile URL is built (#222 step 2), and it builds the string the three
    f-strings it replaced built. tests/test_gsv_photometa_zoom.py pins the probe's two URLs as well, and the
    stub helpers parse URLs back by their &zoom=, &x= and &y= fields, so the field order is part of the contract."""

    PANO = '-0BIbaGbb6Ao2ctRBhyX9w'

    def test_it_builds_todays_string_byte_for_byte(self):
        assert gsv_tiles.tile_url(self.PANO, 5, 3, 7) == (
            'https://maps.google.com/cbk?output=tile&cb_client=maps_sv&onerr=3&renderer=spherical&v=4'
            '&zoom=5&x=3&y=7&panoid=-0BIbaGbb6Ao2ctRBhyX9w')

    def test_the_fan_out_builds_with_it(self):
        tiles_x, tiles_y = gsv_tiles._tile_grid(16384, 8192, 5)
        expected = [(x, y, gsv_tiles.tile_url(self.PANO, 5, x, y)) for y in range(tiles_y) for x in range(tiles_x)]

        assert gsv_tiles._generate_tile_urls(self.PANO, 16384, 8192, 5) == expected

    def test_the_probe_and_the_frame_check_build_with_it(self, monkeypatch):
        black = BytesIO()
        Image.new('RGB', (16, 16), (0, 0, 0)).save(black, 'JPEG')
        requested = []

        def fake_get_response(url, session, stream=False):
            requested.append(url)
            return BytesIO(black.getvalue())

        monkeypatch.setattr(gsv_tiles, '_get_response', fake_get_response)

        assert gsv_tiles.probe_zoom(self.PANO) is None
        assert requested == [gsv_tiles.tile_url(self.PANO, 3, 0, 0), gsv_tiles.tile_url(self.PANO, 5, 0, 0)]

        del requested[:]
        with pytest.raises(gsv_tiles.FrameDisagreementError):      # all black: the grid's last column too
            gsv_tiles.frame_covers_pano(self.PANO, 16384, 8192, 5)
        tiles_x, tiles_y = gsv_tiles._tile_grid(16384, 8192, 5)
        built = {gsv_tiles.tile_url(self.PANO, 5, x, y) for x in range(tiles_x + 1) for y in range(tiles_y + 1)}
        assert requested and set(requested) <= built

    def test_nothing_else_spells_a_tile_url(self):
        """CBK_BASE_URL is read only by tile_url, and no other string in the package or in downloaders/gsv.py
        carries a tile query, so a second, drifting URL builder cannot be added without failing here."""
        for path in (gsv_tiles.__file__, GSV_SOURCE):
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read())
            builder = next((node for node in tree.body
                            if isinstance(node, ast.FunctionDef) and node.name == 'tile_url'), None)
            inside = set(map(id, ast.walk(builder))) if builder is not None else set()
            outside = [node for node in ast.walk(tree) if id(node) not in inside]
            reads = [node.lineno for node in outside if isinstance(node, ast.Name) and node.id == 'CBK_BASE_URL'
                     and isinstance(node.ctx, ast.Load)]
            queries = [node.lineno for node in outside if isinstance(node, ast.Constant)
                       and isinstance(node.value, str) and '&zoom=' in node.value]
            assert reads == [] and queries == [], (path, reads, queries)


class TestProductionCallersHaveOneLookupSite:
    """refetch_panos and DownloadRunner read a moved name through gsv only where gsv is the module that looks it
    up (the composed seams) or where no stub ever reaches (the exception classes, StitchedPano, pushback_reason).
    Any other `gsv.<moved name>` read is a second lookup site that conftest.mis_targeted_patches forbids
    patching, so a stub of the package's name would silently miss it (#235 review round 1)."""

    ALLOWED = set(gsv.GSV_COMPOSED_SEAMS) | {'FrameDisagreementError', 'TilePushbackError', 'StitchedPano',
                                             'pushback_reason'}

    @pytest.mark.parametrize('module', ['refetch_panos.py', 'DownloadRunner.py'])
    def test_every_gsv_read_of_a_moved_name_is_allowed(self, module):
        with open(os.path.join(REPO_ROOT, module), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        reads = {(node.attr, node.lineno) for node in ast.walk(tree)
                 if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'gsv'
                 and node.attr in gsv.RE_EXPORTED_FROM_GSV_TILES}
        assert reads, 'the walk found no gsv reads at all, so it is not looking where it thinks'
        assert sorted(r for r in reads if r[0] not in self.ALLOWED) == []
