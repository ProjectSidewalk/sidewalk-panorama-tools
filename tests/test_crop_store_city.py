"""Tests for the crop store's recorded city (#153's stopgap, kept under #159's layout).

`label_id` restarts at 1 in every deployment, so a crop's file name is unique only within one city. #159 puts
each city in its own store, `<crop-dir>/<city>/`, and this is the guard that stays on top of that layout:

* `--city` is required, and must look like a city_id (`seattle-wa`, `cdmx`) - and, since #159, be an active
  row of `log_analyzer/cities.csv` (`tests/test_crop_store_layout.py`);
* the store's `crop_rule.json` records the city it belongs to - a fresh store, or one cut before this existed,
  adopts the `--city` it is given;
* a store recorded as a DIFFERENT city's - one renamed or copied under this city's name - is refused before
  anything is created, cut or overwritten, asserted on the bytes of the whole tree, and so is a store whose
  marker cannot be read, since then its city cannot be confirmed;
* the provenance manifest carries the city on every row, so manifests concatenated across cities keep
  `(city, label_id)` as the key.
"""

import json
import logging
import os

import pytest

# crop_runner and the autouse logging isolation are fixtures: importing them into this module's namespace is
# what makes pytest see them here.
from test_crop_runner import (  # noqa: F401
    _isolate_logging_state, city_store, crop_path, crop_runner, label_row, plant_stale_crop, put_pano,
    tree_snapshot, write_labels_csv)


def a_store(tmp_path):
    """A pano store holding one pano, and a label file with one label on it."""
    store = tmp_path / 'store'
    put_pano(store, 'testpano0001')
    csv_file = tmp_path / 'labels.csv'
    write_labels_csv(csv_file, [label_row(label_id=1, label_type_id=1)])
    return store, csv_file


def crop(crop_runner, csv_file, store, out, city, *extra):
    """One CropRunner invocation. Each real one is its own process, so the previous in-process run's crop.log
    handler is detached first - otherwise a refusal's message would land in the log of the store it refuses,
    through a handler the refused run never opened."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
        root.removeHandler(handler)
        handler.close()
    return crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out), '--city', city, *extra])


def marker(crop_runner, out, city='seattle-wa'):
    with open(str(city_store(out, city) / crop_runner.CROP_RULE_MARKER), encoding='utf-8') as f:
        return json.load(f)


def seattle_store_renamed_to_chicago(crop_runner, tmp_path):
    """Seattle's store, moved under Chicago's name: the one way a store recorded as another city's can still
    turn up at <crop-dir>/<city>/ now that every city has its own directory."""
    store, csv_file = a_store(tmp_path)
    out = tmp_path / 'crops'
    assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
    # The run's crop.log handler is still open, and Windows will not rename a directory holding an open file.
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
        root.removeHandler(handler)
        handler.close()
    os.rename(str(city_store(out, 'seattle-wa')), str(city_store(out, 'chicago-il')))
    return store, csv_file, out


class TestTheFlag:
    def test_it_is_required(self, crop_runner, tmp_path):
        store, csv_file = a_store(tmp_path)
        with pytest.raises(SystemExit) as e:
            crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(tmp_path / 'crops')])
        assert e.value.code == 2
        assert not (tmp_path / 'crops').exists()

    @pytest.mark.parametrize('city', ['seattle-wa', 'cdmx', 'st-louis-mo', 'new-taipei-tw'])
    def test_a_city_id_is_accepted(self, crop_runner, tmp_path, monkeypatch, city):
        """The spelling rule, against a roster naming all four: which cities the committed roster lists is
        tests/test_crop_store_layout.py's business."""
        roster = tmp_path / 'cities.csv'
        roster.write_text('city_id\nseattle-wa\ncdmx\nst-louis-mo\nnew-taipei-tw\n', encoding='utf-8')
        monkeypatch.setattr(crop_runner, 'CITIES_FILE', str(roster))
        assert crop_runner.build_parser().parse_args(['-f', 'x.csv', '-s', 's', '-o', 'o', '--city', city]).city == city

    @pytest.mark.parametrize('city', ['', 'Seattle', 'seattle wa', 'seattle_wa', '-seattle', 'seattle-',
                                      '../seattle', 'sidewalk-sea.cs.washington.edu'])
    def test_anything_else_is_refused(self, crop_runner, city):
        """A path component (#159), and compared as a string: a city spelled two ways would be two cities."""
        with pytest.raises(SystemExit) as e:
            crop_runner.build_parser().parse_args(['-f', 'x.csv', '-s', 's', '-o', 'o', '--city', city])
        assert e.value.code == 2


class TestTheStoreRemembersItsCity:
    def test_a_fresh_store_records_it(self, crop_runner, tmp_path):
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        assert marker(crop_runner, out)['city'] == 'seattle-wa'

    def test_a_store_cut_before_the_guard_adopts_the_city_it_is_given(self, crop_runner, tmp_path):
        """No migration for a store already named for its city: its marker has no city, and it must keep
        working."""
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        m = marker(crop_runner, out)
        del m['city']
        (city_store(out) / crop_runner.CROP_RULE_MARKER).write_text(json.dumps(m), encoding='utf-8')
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        assert marker(crop_runner, out)['city'] == 'seattle-wa'

    def test_the_same_city_runs_as_before(self, crop_runner, tmp_path):
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa', '--force') == 0
        assert marker(crop_runner, out)['city'] == 'seattle-wa'

    def test_a_run_below_main_keeps_the_recorded_city(self, crop_runner, tmp_path):
        """bulk_extract_crops rewrites the marker too; called without a city it must carry the recorded one
        forward, not erase it - erasing it would re-open the store to the next city that comes along."""
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        crop_runner.bulk_extract_crops(crop_runner.load_label_metadata(None, str(csv_file)), str(store),
                                       str(city_store(out)))
        assert marker(crop_runner, out)['city'] == 'seattle-wa'


class TestAStoreRecordedAsAnotherCitysIsRefused:
    """#159 lifts the stopgap's premise - one -o, one city: Chicago beside Seattle under one -o is now just
    two stores (tests/test_crop_store_layout.py). What the recorded city still catches is a store under the
    wrong name, renamed or copied there by hand."""

    @pytest.mark.parametrize('extra', [(), ('--force',)], ids=['plain', 'force'])
    def test_nothing_in_the_store_changes(self, crop_runner, tmp_path, extra):
        """Under --force Seattle's crop would be REPLACED, and without it Seattle's crop would be counted as
        Chicago's. Either way nothing may be touched - crop.log included, since the refusal must not itself
        write into a store it refuses."""
        store, csv_file, out = seattle_store_renamed_to_chicago(crop_runner, tmp_path)
        before = tree_snapshot(out)
        assert crop(crop_runner, csv_file, store, out, 'chicago-il', *extra) == crop_runner.EXIT_REFUSED_DESTINATION
        assert tree_snapshot(out) == before

    def test_a_planted_crop_survives_a_forced_run_byte_for_byte(self, crop_runner, tmp_path):
        store, csv_file, out = seattle_store_renamed_to_chicago(crop_runner, tmp_path)
        original = plant_stale_crop(city_store(out, 'chicago-il'), label_type_id=1, label_id=1)
        crop(crop_runner, csv_file, store, out, 'chicago-il', '--force')
        with open(crop_path(city_store(out, 'chicago-il'), 1, 1), 'rb') as f:
            assert f.read() == original

    def test_the_crop_loop_refuses_too(self, crop_runner, tmp_path):
        """bulk_extract_crops is a public seam (the studies call it), and it guards itself the way it guards
        against the production store, rather than trusting that main() ran first."""
        store, csv_file, out = seattle_store_renamed_to_chicago(crop_runner, tmp_path)
        before = tree_snapshot(out)
        with pytest.raises(crop_runner.CropStoreCityError):
            crop_runner.bulk_extract_crops(crop_runner.load_label_metadata(None, str(csv_file)), str(store),
                                           str(city_store(out, 'chicago-il')), force=True, city='chicago-il')
        assert tree_snapshot(out) == before

    def test_it_says_which_city_the_store_belongs_to(self, crop_runner, tmp_path, capsys):
        store, csv_file, out = seattle_store_renamed_to_chicago(crop_runner, tmp_path)
        capsys.readouterr()
        crop(crop_runner, csv_file, store, out, 'chicago-il')
        said = capsys.readouterr()
        text = said.out + said.err
        assert 'seattle-wa' in text and 'chicago-il' in text

    @pytest.mark.parametrize('content', ['{not json', '[1, 2]', '{"city": 7}'], ids=['torn', 'not-object', 'not-string'])
    def test_a_marker_that_cannot_be_read_is_refused_and_left_alone(self, crop_runner, tmp_path, content):
        """Unreadable is not "no city recorded": adopting there would hand the store to whichever city ran
        next. Refused, and the file is left for a person to look at."""
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        crop(crop_runner, csv_file, store, out, 'seattle-wa')
        (city_store(out) / crop_runner.CROP_RULE_MARKER).write_text(content, encoding='utf-8')
        before = tree_snapshot(out)
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa', '--force') == crop_runner.EXIT_REFUSED_DESTINATION
        assert tree_snapshot(out) == before


class TestTheManifestNamesTheCity:
    def test_it_is_the_first_column(self, crop_runner):
        assert crop_runner.PROVENANCE_COLUMNS[:3] == ('city', 'label_id', 'pano_id')

    def test_every_row_carries_it(self, crop_runner, tmp_path):
        import csv
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        with open(str(city_store(out) / crop_runner.PROVENANCE_MANIFEST), newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        assert rows and all(row['city'] == 'seattle-wa' for row in rows)
