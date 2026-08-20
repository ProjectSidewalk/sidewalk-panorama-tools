"""Tests for flag_panos/json_to_csv.py (#52 item 6, last bullet).

The script converts the flag_panos web tool's JSON output to CSV. It was 16 lines with `CITY = 'amsterdam'`
hardcoded, two commented-out config lines, no argparse, no main() and no error handling - so it did all its
work at import, against whatever the CWD happened to be, and a missing input surfaced as a bare traceback.

These pin the same #52.1 contract the two runners already honour: importing the module does nothing, the
seams are build_parser() / main(argv), and the failure mode names the file.
"""

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SCRIPT = os.path.join(REPO_ROOT, 'flag_panos', 'json_to_csv.py')


@pytest.fixture
def json_to_csv():
    from flag_panos import json_to_csv as module
    return module


def write_inputs(directory, city, image_data=True, unretrievable=True):
    """Write whichever of the two flag_panos outputs the test wants, and return their stems."""
    written = []
    if image_data:
        path = directory / ('%s_pano_image_data.json' % city)
        path.write_text(json.dumps([{'pano_id': 'abc', 'width': 16384},
                                    {'pano_id': 'def', 'width': 13312}]))
        written.append(path)
    if unretrievable:
        path = directory / ('%s_unretrievable_panos.json' % city)
        path.write_text(json.dumps([{'pano_id': 'ghi'}]))
        written.append(path)
    return written


class TestTheModuleImportsWithNoSideEffects:
    """The #52.1 contract. This module used to run its whole conversion at import against the CWD, so
    importing it - which the test suite now does - either crashed or silently wrote files."""

    def test_importing_does_not_read_or_write_anything(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import importlib

        from flag_panos import json_to_csv as module
        importlib.reload(module)

        assert list(tmp_path.iterdir()) == []

    def test_the_seams_exist(self, json_to_csv):
        assert callable(json_to_csv.build_parser)
        assert callable(json_to_csv.main)


class TestTheParser:
    def test_city_is_required(self, json_to_csv):
        with pytest.raises(SystemExit) as e:
            json_to_csv.build_parser().parse_args([])
        assert e.value.code == 2

    def test_the_directory_defaults_to_the_cwd(self, json_to_csv):
        args = json_to_csv.build_parser().parse_args(['--city', 'amsterdam'])
        assert args.dir == '.'

    def test_any_city_works_not_just_amsterdam(self, json_to_csv):
        """`CITY = 'amsterdam'` was a module constant, so using it on another city meant editing the file."""
        args = json_to_csv.build_parser().parse_args(['--city', 'seattle'])
        assert args.city == 'seattle'


class TestConversion:
    def test_both_files_round_trip(self, json_to_csv, tmp_path, capsys):
        write_inputs(tmp_path, 'amsterdam')

        assert json_to_csv.main(['--city', 'amsterdam', '--dir', str(tmp_path)]) == 0

        image_csv = tmp_path / 'amsterdam_pano_image_data.csv'
        assert image_csv.is_file()
        rows = image_csv.read_text().strip().splitlines()
        assert rows[0].split(',') == ['pano_id', 'width']
        assert len(rows) == 3, 'header plus two records'
        assert (tmp_path / 'amsterdam_unretrievable_panos.csv').is_file()

    def test_a_city_with_only_one_of_the_two_still_converts(self, json_to_csv, tmp_path):
        """The web tool does not always produce both, and failing the whole run over the absent one would
        make the tool unusable in exactly the case it was written for."""
        write_inputs(tmp_path, 'seattle', unretrievable=False)

        assert json_to_csv.main(['--city', 'seattle', '--dir', str(tmp_path)]) == 0

        assert (tmp_path / 'seattle_pano_image_data.csv').is_file()
        assert not (tmp_path / 'seattle_unretrievable_panos.csv').exists()

    def test_no_inputs_at_all_names_what_it_looked_for(self, json_to_csv, tmp_path, capsys):
        """The old script raised a bare FileNotFoundError from inside a loop, naming one file and never
        saying which city or directory it had been asked about."""
        code = json_to_csv.main(['--city', 'nowhere', '--dir', str(tmp_path)])

        assert code == 1
        message = capsys.readouterr().err
        assert 'nowhere' in message
        assert str(tmp_path) in message

    def test_the_index_column_is_not_written(self, json_to_csv, tmp_path):
        """pandas writes a nameless index column by default, which would shift every field by one for any
        consumer reading positionally."""
        write_inputs(tmp_path, 'amsterdam', unretrievable=False)
        json_to_csv.main(['--city', 'amsterdam', '--dir', str(tmp_path)])

        header = (tmp_path / 'amsterdam_pano_image_data.csv').read_text().splitlines()[0]
        assert header == 'pano_id,width'


class TestRunAsAScript:
    def test_the_main_guard_runs_the_conversion(self, tmp_path):
        write_inputs(tmp_path, 'amsterdam', unretrievable=False)

        result = subprocess.run([sys.executable, SCRIPT, '--city', 'amsterdam', '--dir', str(tmp_path)],
                                capture_output=True, text=True, timeout=120)

        assert result.returncode == 0, result.stderr
        assert (tmp_path / 'amsterdam_pano_image_data.csv').is_file()

    def test_a_missing_city_exits_nonzero_for_a_shell_caller(self, tmp_path):
        result = subprocess.run([sys.executable, SCRIPT, '--city', 'nowhere', '--dir', str(tmp_path)],
                                capture_output=True, text=True, timeout=120)

        assert result.returncode == 1
