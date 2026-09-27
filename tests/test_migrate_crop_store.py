"""Tests for migrate_crop_store.py: the one-off, move-only migrator from the pre-#159 flat crop store,
`<crop-dir>/<label_type_id>/<label_id>.jpg`, to one city's store, `<crop-dir>/<city>/<label_type_id>/...`.

The contract, asserted on the filesystem rather than on return values: every file ends up either moved to
its place under `<crop-dir>/<city>/` or exactly where it was; nothing is ever copied, rewritten, replaced or
deleted - a file already at a destination is a COLLISION and both are left as they are; `--dry-run` writes
nothing at all; and a second run, or a run after a partial one, finishes the job and changes nothing else.
"""

import collections
import os

import pytest

import migrate_crop_store
# tree_snapshot is the same "nothing was written" instrument the CropRunner tests use; crop_runner and the
# logging isolation are fixtures, imported so pytest sees them here.
from test_crop_runner import _isolate_logging_state, crop_runner, tree_snapshot  # noqa: F401

CITY = 'seattle-wa'


def plant(root, relpath, data=None):
    """A file at root/relpath whose bytes name it, so a moved file can be told from a planted one."""
    path = os.path.join(str(root), *relpath.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(relpath.encode('utf-8') if data is None else data)
    return path


def read_bytes(path):
    with open(str(path), 'rb') as f:
        return f.read()


def contents(root):
    """The multiset of every file's bytes under root: a move-only migration must leave it unchanged."""
    return collections.Counter(v for v in tree_snapshot(root).values() if v is not None)


def flat_store(root):
    """A pre-#159 store: two label-type shards directly under the root."""
    for relpath in ('1/1.jpg', '1/2.jpg', '2/3.jpg'):
        plant(root, relpath)
    return root


def migrate(root, dry_run=False):
    return migrate_crop_store.migrate_store(str(root), CITY, dry_run=dry_run)


class TestTheShardsMove:
    def test_a_shard_with_no_destination_moves_whole(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        summary = migrate(root)
        assert sorted(tree_snapshot(root)) == sorted([
            CITY, os.path.join(CITY, '1'), os.path.join(CITY, '1', '1.jpg'), os.path.join(CITY, '1', '2.jpg'),
            os.path.join(CITY, '2'), os.path.join(CITY, '2', '3.jpg')])
        assert read_bytes(root / CITY / '1' / '2.jpg') == b'1/2.jpg'
        assert (summary.dirs_moved, summary.files_moved, summary.collisions, summary.failed) == (2, 0, 0, 0)

    def test_into_an_existing_shard_it_moves_file_by_file(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/9.jpg')
        summary = migrate(root)
        assert sorted(os.listdir(str(root / CITY / '1'))) == ['1.jpg', '2.jpg', '9.jpg']
        assert not (root / '1').exists()
        assert (summary.dirs_moved, summary.files_moved, summary.collisions) == (1, 2, 0)

    def test_a_file_already_at_the_destination_is_never_replaced_and_its_source_stays(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/1.jpg', data=b'already here')
        summary = migrate(root)
        assert read_bytes(root / CITY / '1' / '1.jpg') == b'already here'
        assert read_bytes(root / '1' / '1.jpg') == b'1/1.jpg'
        assert summary.collisions == 1
        assert (root / CITY / '1' / '2.jpg').is_file()

    def test_nothing_is_lost_or_duplicated(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/1.jpg', data=b'already here')
        before = contents(root)
        migrate(root)
        assert contents(root) == before

    def test_a_subdirectory_inside_a_shard_is_left_and_listed(self, tmp_path, capsys):
        """Nothing this tool writes; a person put it there, and a person decides."""
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/9.jpg')
        plant(root, '1/extra/a.jpg')
        summary = migrate(root)
        assert (root / '1' / 'extra' / 'a.jpg').is_file()
        assert summary.left == 1
        assert os.path.join(str(root), '1', 'extra') in capsys.readouterr().out

    def test_directories_that_are_not_shards_are_not_touched(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        plant(root, 'chicago-il/1/1.jpg')
        plant(root, 'figures/overview.png')
        plant(root, 'notes.txt')
        migrate(root)
        for relpath in ('chicago-il/1/1.jpg', 'figures/overview.png', 'notes.txt'):
            assert read_bytes(os.path.join(str(root), *relpath.split('/'))) == relpath.encode('utf-8')


class TestADryRunWritesNothing:
    def test_the_tree_is_unchanged(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/1.jpg', data=b'already here')
        before = tree_snapshot(root)
        migrate(root, dry_run=True)
        assert tree_snapshot(root) == before

    def test_not_even_the_city_directory(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        migrate(root, dry_run=True)
        assert not (root / CITY).exists()

    def test_it_predicts_what_a_real_run_does(self, tmp_path, capsys):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/1.jpg', data=b'already here')
        predicted = migrate(root, dry_run=True)
        printed = capsys.readouterr().out
        assert 'Would move directory %s -> %s (1 files)' % (os.path.join(str(root), '2'),
                                                              os.path.join(str(root), CITY, '2')) in printed
        assert 'COLLISION' in printed
        assert predicted == migrate(root)


class TestItIsResumable:
    def test_a_second_run_changes_nothing(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        migrate(root)
        before = tree_snapshot(root)
        summary = migrate(root)
        assert tree_snapshot(root) == before
        assert (summary.dirs_moved, summary.files_moved, summary.store_files_moved, summary.collisions,
                summary.failed, summary.left) == (0, 0, 0, 0, 0, 0)

    def test_a_run_killed_partway_is_finished_by_the_next(self, tmp_path, monkeypatch):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/9.jpg')
        before = contents(root)
        real_rename = os.rename
        calls = []

        def killed_on_the_second(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_rename(src, dst)

        monkeypatch.setattr(migrate_crop_store.os, 'rename', killed_on_the_second)
        with pytest.raises(KeyboardInterrupt):
            migrate(root)
        monkeypatch.setattr(migrate_crop_store.os, 'rename', real_rename)
        summary = migrate(root)
        assert summary.collisions == 0 and summary.failed == 0
        assert contents(root) == before
        assert sorted(os.listdir(str(root))) == [CITY]


# ---------------------------------------------------------------------------
# The store's own files, and whose store it is
# ---------------------------------------------------------------------------

STORE_FILES = ('crop_rule.json', 'crop_provenance.csv', 'crop_provenance.pre-city.csv', 'crop.log',
               'crop.log.1', 'crop.log.3')


def body(name):
    """Bytes that name the file and are also a readable marker, so crop_rule.json passes the city check."""
    return ('{"file": "%s"}' % name).encode('utf-8')


def write_marker(path, **fields):
    import json
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), 'w', encoding='utf-8') as f:
        json.dump(dict({'crop_rule_version': 'v2'}, **fields), f)


class TestTheStoreFilesMoveWithIt:
    @pytest.mark.parametrize('name', STORE_FILES)
    def test_each_moves_unchanged(self, tmp_path, name):
        root = flat_store(tmp_path / 'crops')
        plant(root, name, data=body(name))
        summary = migrate(root)
        assert read_bytes(root / CITY / name) == body(name)
        assert not (root / name).exists()
        assert summary.store_files_moved == 1

    @pytest.mark.parametrize('name', STORE_FILES)
    def test_each_collides_rather_than_replaces(self, tmp_path, name):
        root = flat_store(tmp_path / 'crops')
        plant(root, name, data=body(name))
        plant(root, CITY + '/' + name, data=b'{"already": "here"}')
        summary = migrate(root)
        assert read_bytes(root / CITY / name) == b'{"already": "here"}'
        assert read_bytes(root / name) == body(name)
        assert summary.collisions == 1 and summary.store_files_moved == 0

    def test_other_files_at_the_root_stay(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        for name in ('notes.txt', 'crop.log.old', 'crop_rule.json.bak'):
            plant(root, name)
        migrate(root)
        for name in ('notes.txt', 'crop.log.old', 'crop_rule.json.bak'):
            assert (root / name).is_file()

    def test_the_store_files_move_after_the_shards(self, tmp_path, monkeypatch):
        """The marker is what CropRunner's refusal and this tool's city check read, so it is the last thing
        to leave the root: a run that dies partway leaves the root still marked as a flat store."""
        root = flat_store(tmp_path / 'crops')
        plant(root, 'crop_rule.json', data=b'{}')
        order = []
        real_rename = os.rename

        def recording(src, dst):
            order.append(os.path.basename(src))
            return real_rename(src, dst)

        monkeypatch.setattr(migrate_crop_store.os, 'rename', recording)
        migrate(root)
        assert order[-1] == 'crop_rule.json' and order[:2] == ['1', '2']


class TestTheCityIsChecked:
    @pytest.mark.parametrize('where', ['root', 'store'])
    def test_a_marker_naming_another_city_is_refused_with_nothing_touched(self, crop_runner, tmp_path, where):
        root = flat_store(tmp_path / 'crops')
        write_marker(root / 'crop_rule.json' if where == 'root' else root / CITY / 'crop_rule.json',
                     city='chicago-il')
        before = tree_snapshot(root)
        with pytest.raises(crop_runner.CropStoreCityError):
            migrate(root)
        assert tree_snapshot(root) == before

    @pytest.mark.parametrize('content', [b'{not json', b'[1, 2]', b'{"city": 7}'], ids=['torn', 'not-object', 'not-string'])
    def test_a_marker_that_cannot_be_read_is_refused_with_nothing_touched(self, crop_runner, tmp_path, content):
        root = flat_store(tmp_path / 'crops')
        plant(root, 'crop_rule.json', data=content)
        before = tree_snapshot(root)
        with pytest.raises(crop_runner.CropStoreCityError):
            migrate(root)
        assert tree_snapshot(root) == before

    def test_a_marker_with_no_city_or_the_same_city_passes(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        write_marker(root / 'crop_rule.json')
        assert migrate(root).store_files_moved == 1
        root2 = flat_store(tmp_path / 'crops2')
        write_marker(root2 / 'crop_rule.json', city=CITY)
        assert migrate(root2).store_files_moved == 1

    def test_another_citys_store_at_the_root_is_untouched(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        for name in ('1/1.jpg', 'crop_rule.json', 'crop.log'):
            plant(root, 'chicago-il/' + name)
        before = {k: v for k, v in tree_snapshot(root).items() if k.startswith('chicago-il')}
        migrate(root)
        assert {k: v for k, v in tree_snapshot(root).items() if k.startswith('chicago-il')} == before


class TestCropRunnerTakesTheMigratedStore:
    def test_end_to_end_every_crop_is_skipped_not_recut(self, crop_runner, tmp_path, monkeypatch):
        """A store cut before #159, migrated, then cropped: CropRunner accepts the root, adopts the city
        and finds every crop already there."""
        from test_crop_runner import label_row, put_pano, write_labels_csv
        import logging
        panos = tmp_path / 'panos'
        put_pano(panos, 'testpano0001')
        labels = tmp_path / 'labels.csv'
        write_labels_csv(labels, [label_row(label_id=i, pano_x=100 + 40 * i) for i in (1, 2, 3)])
        root = tmp_path / 'crops'
        # The flat store, as a pre-#159 run left it: cut straight into what is now the root, marker with
        # no city, crop.log beside it.
        crop_runner.bulk_extract_crops(crop_runner.load_label_metadata(None, str(labels)), str(panos), str(root))
        plant(root, 'crop.log', data=b'an old run\n')
        before = {k: v for k, v in tree_snapshot(root).items() if k.endswith('.jpg')}

        summary = migrate(root)
        assert summary.collisions == 0 and summary.failed == 0

        seen = {}
        real = crop_runner.bulk_extract_crops

        def spy(*args, **kwargs):
            seen['counts'] = real(*args, **kwargs)
            return seen['counts']

        monkeypatch.setattr(crop_runner, 'bulk_extract_crops', spy)
        try:
            code = crop_runner.main(['-f', str(labels), '-s', str(panos), '-o', str(root), '--city', CITY])
        finally:
            root_logger = logging.getLogger()
            for handler in [h for h in root_logger.handlers if isinstance(h, logging.FileHandler)]:
                root_logger.removeHandler(handler)
                handler.close()
        assert code == 0
        assert seen['counts']['skipped_existing'] == 3 and seen['counts']['success'] == 0
        after = {k[len(CITY) + 1:]: v for k, v in tree_snapshot(root).items() if k.endswith('.jpg')}
        assert after == before
        assert read_bytes(root / CITY / 'crop.log').startswith(b'an old run\n')


# ---------------------------------------------------------------------------
# The command line: python3 migrate_crop_store.py <crop-dir> --city <city_id> [--dry-run]
# ---------------------------------------------------------------------------

def run_main(root, *extra, city=CITY):
    return migrate_crop_store.main([str(root), '--city', city, *extra])


class TestTheCommandLine:
    def test_a_clean_migration_exits_0_and_says_what_it_did(self, tmp_path, capsys):
        root = flat_store(tmp_path / 'crops')
        plant(root, 'crop_rule.json', data=b'{}')
        assert run_main(root) == 0
        printed = capsys.readouterr().out
        assert ('2 type directories moved whole, 0 files moved one by one, 1 store files moved, '
                '0 collisions left in place, 0 left for a person, 0 failed') in printed

    def test_it_names_the_next_cropper_command_and_warns_consumers(self, tmp_path, capsys):
        root = flat_store(tmp_path / 'crops')
        run_main(root)
        printed = capsys.readouterr().out
        assert '-o %s --city %s' % (root, CITY) in printed
        assert os.path.join(str(root), CITY, '<label_type_id>') in printed

    def test_nothing_to_do_is_exit_0(self, tmp_path):
        root = tmp_path / 'crops'
        root.mkdir()
        assert run_main(root) == 0
        assert run_main(flat_store(root)) == 0
        assert run_main(root) == 0

    def test_a_collision_is_exit_1(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/1.jpg', data=b'already here')
        assert run_main(root) == 1

    def test_a_predicted_collision_is_exit_1_under_dry_run(self, tmp_path, capsys):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/1.jpg', data=b'already here')
        before = tree_snapshot(root)
        assert run_main(root, '--dry-run') == 1
        assert tree_snapshot(root) == before
        printed = capsys.readouterr().out
        assert ('1 type directories would be moved whole, 1 files would be moved one by one, 0 store files '
                'would be moved, 1 collisions would be left in place') in printed

    def test_a_directory_left_inside_a_shard_is_exit_1(self, tmp_path):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/9.jpg')
        plant(root, '1/extra/a.jpg')
        assert run_main(root) == 1

    def test_one_failed_rename_is_counted_and_the_rest_still_move(self, tmp_path, monkeypatch, capsys):
        root = flat_store(tmp_path / 'crops')
        plant(root, CITY + '/1/9.jpg')
        real_rename = os.rename

        def fail_one(src, dst):
            if os.path.basename(src) == '1.jpg':
                raise PermissionError(13, 'Permission denied', src)
            return real_rename(src, dst)

        monkeypatch.setattr(migrate_crop_store.os, 'rename', fail_one)
        assert run_main(root) == 1
        assert (root / '1' / '1.jpg').is_file()
        assert (root / CITY / '1' / '2.jpg').is_file() and (root / CITY / '2' / '3.jpg').is_file()
        assert '1 failed' in capsys.readouterr().out

    @pytest.mark.parametrize('argv', [[], ['--city', 'seattle-wa'], ['X', '--city', 'Seattle'],
                                      ['X', '--city', 'atlantis-ga']],
                             ids=['nothing', 'no-dir', 'malformed-city', 'unknown-city'])
    def test_usage_errors_exit_2(self, tmp_path, argv):
        argv = [str(tmp_path / 'crops') if a == 'X' else a for a in argv]
        with pytest.raises(SystemExit) as e:
            migrate_crop_store.main(argv)
        assert e.value.code == 2

    def test_a_crop_dir_that_does_not_exist_is_exit_2(self, tmp_path):
        with pytest.raises(SystemExit) as e:
            run_main(tmp_path / 'nowhere')
        assert e.value.code == 2
        assert not (tmp_path / 'nowhere').exists()

    def test_a_production_shaped_root_is_exit_3_with_nothing_touched(self, tmp_path, capsys, caplog):
        import logging
        from test_crop_runner import make_production_store
        root = make_production_store(tmp_path / 'prod')
        plant(root, '1/5.jpg')
        before = tree_snapshot(root)
        with caplog.at_level(logging.ERROR):
            assert run_main(root) == 3
        assert tree_snapshot(root) == before
        assert 'Refusing' in capsys.readouterr().out
        assert any(r.levelno == logging.ERROR and 'Refusing' in r.getMessage() for r in caplog.records)

    def test_another_citys_marker_is_exit_3_with_nothing_touched(self, tmp_path, capsys):
        root = flat_store(tmp_path / 'crops')
        write_marker(root / 'crop_rule.json', city='chicago-il')
        before = tree_snapshot(root)
        assert run_main(root) == 3
        assert tree_snapshot(root) == before
        assert 'chicago-il' in capsys.readouterr().out

    def test_it_runs_as_a_script(self, tmp_path):
        import subprocess
        import sys
        root = flat_store(tmp_path / 'crops')
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrate_crop_store.py')
        proc = subprocess.run([sys.executable, script, str(root), '--city', CITY], capture_output=True, text=True,
                              timeout=120, cwd=str(tmp_path))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (root / CITY / '1' / '1.jpg').is_file()
