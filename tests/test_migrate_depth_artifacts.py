"""Tests for migrate_depth_artifacts.py: the offline rewriter that brings pre-v2 (x-mirrored) depth
artifacts up to the v2 column order (#58).

A pre-v2 artifact stores streetlevel's raw decode - x-mirrored relative to the pano JPEG - and carries no
'format_version' field. The migrator must flip exactly those, stamp them, leave v2 stores byte-for-byte
alone (so it is safe to run on any store, any number of times), and touch nothing under --dry-run.
"""

import os

import numpy as np
import pytest

from conftest import make_pano
from downloaders import gsv
import migrate_depth_artifacts


# streetlevel decode order (v1 stored it raw); the JPEG order the migrator must produce is its x-flip.
V1_DEPTH = np.array([[2.0, 100.0], [3.0, 200.0]], dtype=np.float32)


def v1_path(storage, pano_id='abcdef'):
    shard = os.path.join(storage, pano_id[:2])
    os.makedirs(shard, exist_ok=True)
    return os.path.join(shard, pano_id + gsv.DEPTH_ARTIFACT_SUFFIX)


def write_v1(storage, pano_id='abcdef', depth=None):
    """Write an artifact exactly as the pre-#58 code did: raw streetlevel order, no format_version."""
    path = v1_path(storage, pano_id)
    with open(path, 'wb') as f:
        np.savez_compressed(f, depth=(V1_DEPTH if depth is None else depth),
                            heading=0.5, pitch=float('nan'), roll=1.5)
    return path


def write_v2(storage, pano_id='abcdef'):
    """Write an artifact exactly as the v2 (post-#58, pre-#56) code did: JPEG column order, format_version=2,
    no plane fields. Hand-rolled rather than written via the current writer, so this fixture stays the
    historical v2 format no matter how the live format evolves."""
    path = v1_path(storage, pano_id)
    with open(path, 'wb') as f:
        np.savez_compressed(f, depth=V1_DEPTH[:, ::-1], heading=0.5, pitch=float('nan'), roll=1.5,
                            format_version=2)
    return path


def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def test_v1_artifact_is_flipped_and_stamped(tmp_path):
    storage = str(tmp_path)
    path = write_v1(storage)

    summary = migrate_depth_artifacts.migrate_store(storage)

    assert (summary.scanned, summary.migrated, summary.skipped, summary.failed) == (1, 1, 0, 0)
    with np.load(path) as d:
        # The literal JPEG column order, not an expression restating the implementation.
        np.testing.assert_allclose(d['depth'], [[100.0, 2.0], [200.0, 3.0]])
        assert d['depth'].dtype == np.float32
        # A literal 2, NOT the current format version: this script implements exactly the v1 -> v2 x-flip.
        # It can never mint a v3 artifact - the plane fields v3 adds (#56) were not stored pre-v3, so they
        # can only come from a re-fetch.
        assert int(d['format_version']) == 2
        # The orientation scalars ride along unchanged.
        assert float(d['heading']) == pytest.approx(0.5)
        assert np.isnan(float(d['pitch']))
        assert float(d['roll']) == pytest.approx(1.5)
    # No leftover temp file from the atomic rewrite.
    assert sorted(os.listdir(os.path.dirname(path))) == [os.path.basename(path)]


def test_v2_artifact_is_untouched_byte_for_byte(tmp_path):
    """v2 artifacts are skipped - deliberately NOT upgraded to v3: the plane data v3 adds was never stored,
    so a v2 store can only reach v3 by deleting the artifacts (and their depth_log.csv rows) and re-fetching."""
    storage = str(tmp_path)
    path = write_v2(storage)
    before = read_bytes(path)

    summary = migrate_depth_artifacts.migrate_store(storage)

    assert (summary.scanned, summary.migrated, summary.skipped, summary.failed) == (1, 0, 1, 0)
    assert read_bytes(path) == before


def test_current_version_artifact_is_untouched_byte_for_byte(tmp_path):
    """Whatever the live writer produces today must also be left alone."""
    storage = str(tmp_path)
    pano = make_pano(V1_DEPTH.astype(np.float64), heading=0.5)
    gsv._write_depth_artifact(storage, 'ghijkl', pano, pano.planes)
    path = os.path.join(storage, 'gh', 'ghijkl' + gsv.DEPTH_ARTIFACT_SUFFIX)
    before = read_bytes(path)

    summary = migrate_depth_artifacts.migrate_store(storage)

    assert (summary.scanned, summary.migrated, summary.skipped, summary.failed) == (1, 0, 1, 0)
    assert read_bytes(path) == before


def test_dry_run_reports_but_touches_nothing(tmp_path):
    storage = str(tmp_path)
    v1 = write_v1(storage, 'abcdef')
    v2 = write_v2(storage, 'ghijkl')
    before = {path: read_bytes(path) for path in (v1, v2)}

    summary = migrate_depth_artifacts.migrate_store(storage, dry_run=True)

    # The counts still say what a real run would do; the bytes say it did not do it.
    assert (summary.scanned, summary.migrated, summary.skipped, summary.failed) == (2, 1, 1, 0)
    assert {path: read_bytes(path) for path in (v1, v2)} == before


def test_migration_is_idempotent(tmp_path):
    storage = str(tmp_path)
    path = write_v1(storage)

    first = migrate_depth_artifacts.migrate_store(storage)
    after_first = read_bytes(path)
    second = migrate_depth_artifacts.migrate_store(storage)

    assert first.migrated == 1
    assert (second.scanned, second.migrated, second.skipped, second.failed) == (1, 0, 1, 0)
    assert read_bytes(path) == after_first


def test_only_depth_artifacts_are_considered(tmp_path):
    """The store also holds the pano JPEGs and depth_log.csv; the migrator must not read or count them."""
    storage = str(tmp_path)
    write_v1(storage)
    jpg = os.path.join(storage, 'ab', 'abcdef.jpg')
    ledger = os.path.join(storage, gsv.DEPTH_LOG_FILENAME)
    with open(jpg, 'wb') as f:
        f.write(b'not a real jpeg')
    with open(ledger, 'w') as f:
        f.write('pano_id,status\nabcdef,saved\n')

    summary = migrate_depth_artifacts.migrate_store(storage)

    assert summary.scanned == 1
    assert read_bytes(jpg) == b'not a real jpeg'
    with open(ledger) as f:
        assert f.read() == 'pano_id,status\nabcdef,saved\n'


def test_main_warns_that_its_output_is_only_v2(tmp_path, monkeypatch, capsys):
    """A store this script has just "migrated" is still short the plane fields the scraper now writes, and no
    amount of re-running fixes that. Say so at the point someone is looking at the output, rather than leaving
    them to infer it from a version number (#56 review)."""
    storage = str(tmp_path)
    write_v1(storage)
    monkeypatch.setattr('sys.argv', ['migrate_depth_artifacts.py', storage])

    assert migrate_depth_artifacts.main() == 0

    out = capsys.readouterr().out
    assert 'this produces format v2' in out
    assert 'delete its .depth.npz AND its depth_log.csv row' in out


def test_main_stays_quiet_about_v3_when_nothing_was_migrated(tmp_path, monkeypatch, capsys):
    """The note is guidance for a store that just changed, not boilerplate on every no-op sweep."""
    storage = str(tmp_path)
    write_v2(storage)
    monkeypatch.setattr('sys.argv', ['migrate_depth_artifacts.py', storage])

    assert migrate_depth_artifacts.main() == 0

    assert 'this produces format v2' not in capsys.readouterr().out


def test_unreadable_artifact_is_counted_failed_and_does_not_stop_the_run(tmp_path):
    storage = str(tmp_path)
    corrupt = v1_path(storage, 'broken')
    with open(corrupt, 'wb') as f:
        f.write(b'this is not an npz')
    good = write_v1(storage, 'abcdef')

    summary = migrate_depth_artifacts.migrate_store(storage)

    assert (summary.scanned, summary.migrated, summary.failed) == (2, 1, 1)
    assert read_bytes(corrupt) == b'this is not an npz'  # left for a human, not clobbered
    with np.load(good) as d:
        np.testing.assert_allclose(d['depth'], [[100.0, 2.0], [200.0, 3.0]])


class TestAFailedRewriteLeavesTheOriginalArtifactIntact:
    """The migrator rewrites in place, over a store this repo cannot regenerate cheaply - a depth artifact
    costs one metadata request to Google and the backfill is a multi-month job. So the write goes through a
    .part and a rename, and the cleanup has to hold for a failure at either side of the first byte.

    Nothing exercised the failure path: every existing test writes successfully.
    """

    @staticmethod
    def fail_during_save(monkeypatch, when):
        """Make savez_compressed fail either after writing bytes, or before writing any."""
        def failing(file, **kwargs):
            if when == 'after':
                file.write(b'\x50\x4b\x03\x04 truncated')
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(migrate_depth_artifacts.np, 'savez_compressed', failing)

    @pytest.mark.parametrize('when', ['after', 'before'])
    def test_the_v1_artifact_survives_byte_for_byte(self, tmp_path, monkeypatch, when):
        storage = str(tmp_path)
        path = write_v1(storage, 'abcdef')
        original = read_bytes(path)

        self.fail_during_save(monkeypatch, when)
        summary = migrate_depth_artifacts.migrate_store(storage)

        assert (summary.scanned, summary.migrated, summary.failed) == (1, 0, 1)
        assert read_bytes(path) == original, 'a half-written rewrite replaced the artifact'
        assert not os.path.exists(path + '.part'), 'debris left behind for the next run to trip over'

    def test_the_original_error_is_what_migrate_store_counts(self, tmp_path, monkeypatch):
        """Failing before the first byte means the cleanup's own os.remove fails too. If that
        FileNotFoundError escaped it would replace the real cause - here, a full disk - with a message about
        a temp file, and migrate_store's per-artifact report would send the operator the wrong way."""
        storage = str(tmp_path)
        write_v1(storage, 'abcdef')
        self.fail_during_save(monkeypatch, 'before')

        with pytest.raises(OSError) as excinfo:
            migrate_depth_artifacts._migrate_artifact(v1_path(storage, 'abcdef'))

        assert excinfo.value.errno == 28

    def test_a_part_path_that_cannot_be_removed_does_not_mask_the_real_error(self, tmp_path):
        """The cleanup's own os.remove can fail too - here because undeletable debris is sitting at the .part
        path, which is also why the write failed. Letting that second error replace the first would report a
        temp-file problem instead of whatever actually stopped the rewrite."""
        storage = str(tmp_path)
        path = write_v1(storage, 'abcdef')
        original = read_bytes(path)
        # A directory cannot be opened for writing, and cannot be os.remove'd either.
        os.mkdir(path + '.part')

        with pytest.raises(OSError):
            migrate_depth_artifacts._migrate_artifact(path)

        assert read_bytes(path) == original
        assert os.path.isdir(path + '.part'), 'the cleanup should not have half-removed the debris'
