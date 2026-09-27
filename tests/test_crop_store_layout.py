"""Tests for #159: one crop store per city, `<crop-dir>/<city>/<label_type_id>/<label_id>.jpg`.

`label_id` restarts at 1 in every deployment, so a crop's file name is only unique within one city. #153's
stopgap made the store remember its city and refuse another; this lifts the premise that one `-o` is one
city. `-o` is now a root holding one self-contained store per city, `--city` names the store, and `--city`
must be a city the fleet knows, since it is now a directory name and a typo would quietly start a new store.

No network. Panos are synthetic JPEGs in a tmp store; label metadata is a `-f` CSV.
"""

import csv
import os

import pytest

# crop_runner and the autouse logging isolation are fixtures: importing them into this module's namespace is
# what makes pytest see them here.
from test_crop_runner import (  # noqa: F401
    REPO_ROOT, _isolate_logging_state, crop_runner, label_row, put_pano, write_labels_csv)
from test_csv_intake import imported_names


def write_cities(path, rows):
    """A cities.csv in log_analyzer/cities.csv's shape: city_id,display_name."""
    with open(str(path), 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f, lineterminator='\n')
        writer.writerow(['city_id', 'display_name'])
        writer.writerows(rows)
    return path


@pytest.fixture
def roster(crop_runner, tmp_path, monkeypatch):
    """CropRunner reads its city roster from a tmp file. Semantics are tested against this, never against
    the committed list, so adding or retiring a city cannot move these tests."""
    path = write_cities(tmp_path / 'cities.csv', [
        ('seattle-wa', 'Seattle, WA'),
        ('chicago-il', 'Chicago, IL'),
        ('#crowdstudy', 'not-monitored: study deployment'),
    ])
    monkeypatch.setattr(crop_runner, 'CITIES_FILE', str(path))
    return path


def parse(crop_runner, city):
    return crop_runner.build_parser().parse_args(['-f', 'x.csv', '-s', 's', '-o', 'o', '--city', city])


class TestTheCityIsOneTheFleetKnows:
    """`--city` names a directory under `-o`, so a regex-valid typo (`seatle-wa`) would open a new, empty
    store and cut every crop again beside the real one. It is checked against `log_analyzer/cities.csv`,
    the roster the rest of the repo keeps, read as a file."""

    def test_a_listed_city_is_accepted(self, crop_runner, roster):
        assert parse(crop_runner, 'chicago-il').city == 'chicago-il'

    def test_an_unlisted_city_exits_2_and_creates_nothing(self, crop_runner, roster, tmp_path, capsys):
        store = tmp_path / 'store'
        put_pano(store, 'testpano0001')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row()])
        out = tmp_path / 'crops'
        with pytest.raises(SystemExit) as e:
            crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out), '--city', 'atlantis-ga'])
        assert e.value.code == 2
        assert not out.exists()
        err = capsys.readouterr().err
        assert 'atlantis-ga' in err and str(roster) in err

    def test_a_commented_out_row_is_not_a_city(self, crop_runner, roster):
        """A `#` row is a deployment deliberately not scraped here: no panos, so nothing to crop."""
        write_cities(roster, [('seattle-wa', 'Seattle, WA'), ('#chicago-il', 'retired')])
        with pytest.raises(SystemExit) as e:
            parse(crop_runner, 'chicago-il')
        assert e.value.code == 2

    def test_membership_is_read_from_city_id_not_display_name(self, crop_runner, roster):
        write_cities(roster, [('seattle-wa', 'atlantis-ga')])
        with pytest.raises(SystemExit):
            parse(crop_runner, 'atlantis-ga')

    def test_the_roster_is_read_when_the_flag_is_parsed(self, crop_runner, roster):
        """Not cached at import: a city added to the file is accepted by the next parse."""
        with pytest.raises(SystemExit):
            parse(crop_runner, 'atlantis-ga')
        write_cities(roster, [('atlantis-ga', 'Atlantis, GA')])
        assert parse(crop_runner, 'atlantis-ga').city == 'atlantis-ga'

    def test_a_malformed_city_is_refused_before_the_roster_is_read(self, crop_runner, roster, monkeypatch):
        monkeypatch.setattr(crop_runner, 'CITIES_FILE', str(roster) + '.missing')
        with pytest.raises(SystemExit):
            parse(crop_runner, 'Seattle')

    @pytest.mark.parametrize('content', [None, 'name,display_name\nseattle-wa,x\n'],
                             ids=['missing', 'no-city_id-column'])
    def test_a_roster_that_cannot_be_read_fails_closed_naming_it(self, crop_runner, roster, capsys,
                                                                   monkeypatch, content):
        """Fails closed: a missing roster accepting every regex-valid city is exactly the typo gap."""
        path = str(roster) + '.other'
        if content is not None:
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(content)
        monkeypatch.setattr(crop_runner, 'CITIES_FILE', path)
        with pytest.raises(SystemExit) as e:
            parse(crop_runner, 'seattle-wa')
        assert e.value.code == 2
        assert path in capsys.readouterr().err

    def test_cropper_never_imports_the_analyzer(self):
        """log_analyzer imports pandas, which no production module may (tests/test_csv_intake.py). The
        roster is read as a file with csv, and never copied into a constant, which would drift."""
        with open(os.path.join(REPO_ROOT, 'CropRunner.py'), encoding='utf-8') as f:
            assert 'log_analyzer' not in imported_names(f.read())

    def test_the_default_roster_is_the_committed_one(self, crop_runner):
        assert os.path.normcase(os.path.abspath(crop_runner.CITIES_FILE)) == os.path.normcase(
            os.path.join(REPO_ROOT, 'log_analyzer', 'cities.csv'))


class TestTheCommittedRosterIsSafeAsDirectoryNames:
    """Pins on the real file, because every active id is now a directory name under `-o`."""

    @pytest.fixture
    def active_ids(self, crop_runner):
        ids = crop_runner.known_city_ids(crop_runner.CITIES_FILE)
        assert len(ids) > 40
        return ids

    def test_every_active_id_is_a_valid_city_id(self, crop_runner, active_ids):
        assert all(crop_runner._CITY_ID.fullmatch(city) for city in active_ids)

    def test_none_is_all_digits(self, crop_runner, active_ids):
        """An all-digit name at the root is what marks a pre-#159 flat store (its label-type shards)."""
        assert not any(crop_runner._is_numeric_name(city) for city in active_ids)

    def test_none_is_a_label_type_name(self, crop_runner, active_ids):
        """A directory named for a label type is what refuses -o as the production canvas-capture store."""
        folded = {name.casefold() for name in crop_runner.LABEL_TYPE_IDS_BY_NAME}
        assert not any(city.casefold() in folded for city in active_ids)

    def test_commented_rows_are_not_active(self, crop_runner, active_ids):
        assert 'crowdstudy' not in active_ids and not any(city.startswith('#') for city in active_ids)


# ---------------------------------------------------------------------------
# The layout: <crop-dir>/<city>/<label_type_id>/<label_id>.jpg
# ---------------------------------------------------------------------------

SEATTLE, CHICAGO = 'seattle-wa', 'chicago-il'


def city_run(crop_runner, tmp_path, out, city, *extra, pano_id=None, color=(255, 255, 255), put=True):
    """One CropRunner invocation for `city`: its own pano store and label file, one label (id 1, type 1).

    pano_id defaults per city, since pano ids do not collide across cities while label ids do. Each real run
    is its own process, so the previous in-process run's crop.log handler is detached first."""
    import logging
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
        root.removeHandler(handler)
        handler.close()
    pano_id = pano_id or (city[:2] + 'pano0001')
    store = tmp_path / ('panos-' + city)
    if put:
        put_pano(store, pano_id, color=color)
    csv_file = tmp_path / ('labels-' + city + '.csv')
    write_labels_csv(csv_file, [label_row(pano_id=pano_id, label_id=1, label_type_id=1)])
    return crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out), '--city', city, *extra])


def read_bytes(path):
    with open(str(path), 'rb') as f:
        return f.read()


def manifest(out, city, crop_runner):
    with open(os.path.join(str(out), city, crop_runner.PROVENANCE_MANIFEST), newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


class TestOneStorePerCity:
    def test_the_crop_lands_under_the_city(self, crop_runner, tmp_path):
        out = tmp_path / 'crops'
        assert city_run(crop_runner, tmp_path, out, SEATTLE) == 0
        assert os.listdir(str(out)) == [SEATTLE]
        assert os.path.isfile(os.path.join(str(out), SEATTLE, '1', '1.jpg'))

    def test_the_store_files_live_in_the_city_store(self, crop_runner, tmp_path):
        """Each city's store is self-contained: its own rule marker, manifest and log, so two cities cut
        under different rule versions stay representable."""
        out = tmp_path / 'crops'
        city_run(crop_runner, tmp_path, out, SEATTLE)
        store = os.path.join(str(out), SEATTLE)
        for name in (crop_runner.CROP_RULE_MARKER, crop_runner.PROVENANCE_MANIFEST, 'crop.log'):
            assert os.path.isfile(os.path.join(store, name)), name
            assert not os.path.exists(os.path.join(str(out), name)), name

    def test_a_rerun_skips_the_existing_crop(self, crop_runner, tmp_path, monkeypatch):
        out = tmp_path / 'crops'
        assert city_run(crop_runner, tmp_path, out, SEATTLE) == 0
        seen = {}
        real = crop_runner.bulk_extract_crops

        def spy(*args, **kwargs):
            seen['counts'] = real(*args, **kwargs)
            return seen['counts']

        monkeypatch.setattr(crop_runner, 'bulk_extract_crops', spy)
        assert city_run(crop_runner, tmp_path, out, SEATTLE) == 0
        assert seen['counts']['skipped_existing'] == 1 and seen['counts']['success'] == 0


class TestTwoCitiesShareOneRoot:
    """The failure #159 exists for: Seattle's label 1 and Chicago's label 1 are different labels on
    different panos, and used to be the same file."""

    @pytest.mark.parametrize('extra', [(), ('--force',)], ids=['plain', 'force'])
    def test_neither_city_touches_the_others_crop(self, crop_runner, tmp_path, monkeypatch, extra):
        out = tmp_path / 'crops'
        assert city_run(crop_runner, tmp_path, out, SEATTLE) == 0
        seattle_crop = os.path.join(str(out), SEATTLE, '1', '1.jpg')
        before = read_bytes(seattle_crop)
        seen = {}
        real = crop_runner.bulk_extract_crops

        def spy(*args, **kwargs):
            seen['counts'] = real(*args, **kwargs)
            return seen['counts']

        monkeypatch.setattr(crop_runner, 'bulk_extract_crops', spy)
        assert city_run(crop_runner, tmp_path, out, CHICAGO, *extra, color=(200, 30, 30)) == 0
        assert read_bytes(seattle_crop) == before
        assert seen['counts']['success'] == 1 and seen['counts']['skipped_existing'] == 0
        chicago_crop = os.path.join(str(out), CHICAGO, '1', '1.jpg')
        assert read_bytes(chicago_crop) != before

    def test_each_manifest_carries_only_its_own_city(self, crop_runner, tmp_path):
        out = tmp_path / 'crops'
        city_run(crop_runner, tmp_path, out, SEATTLE)
        city_run(crop_runner, tmp_path, out, CHICAGO)
        seattle, chicago = manifest(out, SEATTLE, crop_runner), manifest(out, CHICAGO, crop_runner)
        assert {row['city'] for row in seattle} == {SEATTLE}
        assert {row['city'] for row in chicago} == {CHICAGO}

    def test_combined_manifests_are_keyed_on_city_and_label_id(self, crop_runner, tmp_path):
        """label_id alone collides - both cities' label is 1 - and the composite does not."""
        out = tmp_path / 'crops'
        city_run(crop_runner, tmp_path, out, SEATTLE)
        city_run(crop_runner, tmp_path, out, CHICAGO)
        rows = manifest(out, SEATTLE, crop_runner) + manifest(out, CHICAGO, crop_runner)
        assert len({(row['city'], row['label_id']) for row in rows}) == len(rows) == 2
        assert len({row['label_id'] for row in rows}) == 1

    def test_a_forced_run_counts_only_its_own_stale_crops(self, crop_runner, tmp_path, monkeypatch):
        """stale_kept stats the crop path; with Seattle's label 1 on disk, a Chicago --force run whose pano
        is missing must not count Seattle's crop as one Chicago kept."""
        out = tmp_path / 'crops'
        city_run(crop_runner, tmp_path, out, SEATTLE)
        seen = {}
        real = crop_runner.bulk_extract_crops

        def spy(*args, **kwargs):
            seen['counts'] = real(*args, **kwargs)
            return seen['counts']

        monkeypatch.setattr(crop_runner, 'bulk_extract_crops', spy)
        assert city_run(crop_runner, tmp_path, out, CHICAGO, '--force', put=False) == 0
        assert seen['counts']['missing_pano'] == 1 and seen['counts']['stale_kept'] == 0
