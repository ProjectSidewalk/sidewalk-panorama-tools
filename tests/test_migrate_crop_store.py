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
from test_crop_runner import tree_snapshot  # noqa: F401 - the same "nothing was written" instrument

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
