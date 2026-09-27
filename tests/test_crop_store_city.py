"""Tests for the crop store's city guard (#153 stopgap for #159).

CropRunner writes `<crop-dir>/<label_type_id>/<label_id>.jpg`, and `label_id` restarts at 1 in every
deployment. Two cities pointed at one `-o` therefore collide on file names: without `--force` city B's label
finds city A's crop and counts it `skipped_existing` (wrong data, reported as success), and with `--force` it
REPLACES city A's crop (lost data, reported as a re-cut). The layout fix is #159. Until then:

* `--city` is required, and must look like a city_id (`seattle-wa`, `cdmx`);
* `crop_rule.json` records the city the store belongs to - a fresh store, or one cut before this existed,
  adopts the first `--city` it is given;
* a run naming a DIFFERENT city is refused before anything is created, cut or overwritten - asserted on the
  bytes of the whole tree, not on the return value - and so is a run over a marker that cannot be read,
  since then the store's city cannot be confirmed;
* the provenance manifest carries the city on every row, so manifests concatenated across cities keep
  `(city, label_id)` as the key.
"""

import json

import pytest

# crop_runner and the autouse logging isolation are fixtures: importing them into this module's namespace is
# what makes pytest see them here.
from test_crop_runner import (  # noqa: F401
    _isolate_logging_state, crop_path, crop_runner, label_row, plant_stale_crop, put_pano, tree_snapshot,
    write_labels_csv)


def a_store(tmp_path):
    """A pano store holding one pano, and a label file with one label on it."""
    store = tmp_path / 'store'
    put_pano(store, 'testpano0001')
    csv_file = tmp_path / 'labels.csv'
    write_labels_csv(csv_file, [label_row(label_id=1, label_type_id=1)])
    return store, csv_file


def crop(crop_runner, csv_file, store, out, city, *extra):
    return crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out), '--city', city, *extra])


def marker(crop_runner, out):
    with open(str(out / crop_runner.CROP_RULE_MARKER), encoding='utf-8') as f:
        return json.load(f)


class TestTheFlag:
    def test_it_is_required(self, crop_runner, tmp_path):
        store, csv_file = a_store(tmp_path)
        with pytest.raises(SystemExit) as e:
            crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(tmp_path / 'crops')])
        assert e.value.code == 2
        assert not (tmp_path / 'crops').exists()

    @pytest.mark.parametrize('city', ['seattle-wa', 'cdmx', 'st-louis-mo', 'new-taipei-tw'])
    def test_a_city_id_is_accepted(self, crop_runner, city):
        assert crop_runner.build_parser().parse_args(['-f', 'x.csv', '-s', 's', '-o', 'o', '--city', city]).city == city

    @pytest.mark.parametrize('city', ['', 'Seattle', 'seattle wa', 'seattle_wa', '-seattle', 'seattle-',
                                      '../seattle', 'sidewalk-sea.cs.washington.edu'])
    def test_anything_else_is_refused(self, crop_runner, city):
        """Not a path component yet, but it will be under #159, and it is compared as a string: a city
        spelled two ways would be two cities."""
        with pytest.raises(SystemExit) as e:
            crop_runner.build_parser().parse_args(['-f', 'x.csv', '-s', 's', '-o', 'o', '--city', city])
        assert e.value.code == 2


class TestTheStoreRemembersItsCity:
    def test_a_fresh_store_records_it(self, crop_runner, tmp_path):
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        assert marker(crop_runner, out)['city'] == 'seattle-wa'

    def test_a_store_cut_before_the_guard_adopts_the_first_city_it_is_given(self, crop_runner, tmp_path):
        """No migration: every existing store has a marker with no city, and must keep working."""
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        m = marker(crop_runner, out)
        del m['city']
        (out / crop_runner.CROP_RULE_MARKER).write_text(json.dumps(m), encoding='utf-8')
        assert crop(crop_runner, csv_file, store, out, 'chicago-il') == 0
        assert marker(crop_runner, out)['city'] == 'chicago-il'

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
        crop_runner.bulk_extract_crops(crop_runner.load_label_metadata(None, str(csv_file)), str(store), str(out))
        assert marker(crop_runner, out)['city'] == 'seattle-wa'


class TestAnotherCitysStoreIsRefused:
    @pytest.mark.parametrize('extra', [(), ('--force',)], ids=['plain', 'force'])
    def test_nothing_in_the_store_changes(self, crop_runner, tmp_path, extra):
        """The whole point: under --force the other city's crop would be REPLACED, and without it the other
        city's crop would be counted as this city's. Either way, nothing may be touched - crop.log
        included, since the refusal must not itself write into a store it refuses."""
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        before = tree_snapshot(out)
        assert crop(crop_runner, csv_file, store, out, 'chicago-il', *extra) == crop_runner.EXIT_REFUSED_DESTINATION
        assert tree_snapshot(out) == before

    def test_a_planted_crop_survives_a_forced_run_byte_for_byte(self, crop_runner, tmp_path):
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        assert crop(crop_runner, csv_file, store, out, 'seattle-wa') == 0
        original = plant_stale_crop(out, label_type_id=1, label_id=1)
        crop(crop_runner, csv_file, store, out, 'chicago-il', '--force')
        with open(crop_path(out, 1, 1), 'rb') as f:
            assert f.read() == original

    def test_it_says_which_city_the_store_belongs_to(self, crop_runner, tmp_path, capsys):
        store, csv_file = a_store(tmp_path)
        out = tmp_path / 'crops'
        crop(crop_runner, csv_file, store, out, 'seattle-wa')
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
        (out / crop_runner.CROP_RULE_MARKER).write_text(content, encoding='utf-8')
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
        with open(str(out / crop_runner.PROVENANCE_MANIFEST), newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        assert rows and all(row['city'] == 'seattle-wa' for row in rows)
