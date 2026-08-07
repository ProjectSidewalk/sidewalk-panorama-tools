# !/usr/bin/python3
"""Rewrite pre-v2 depth artifacts (x-mirrored, no 'format_version' field) into the v2 JPEG column order.

Artifacts written before the #58 fix store streetlevel's raw decode, which is horizontally flipped relative
to the pano JPEG, and carry no 'format_version' field. The scraper never revisits an existing artifact
(download_depth_maps short-circuits on file existence, and the ledger self-heal re-registers without
rewriting), so a store scraped before the fix keeps mirrored artifacts forever unless they are corrected
offline. This script scans a storage root for <pano_id[:2]>/<pano_id>.depth.npz files, flips every pre-v2
artifact's depth array in x, stamps format_version=2, and leaves v2+ artifacts byte-for-byte untouched -
so it is idempotent and safe to run (and re-run) on any store.

Usage:
    python3 migrate_depth_artifacts.py <storage_path> [--dry-run]
"""

import argparse
import os
from collections import namedtuple

import numpy as np

from downloaders.gsv import DEPTH_ARTIFACT_SUFFIX

# 'migrated' counts artifacts rewritten - or, under --dry-run, artifacts that would have been.
MigrationSummary = namedtuple('MigrationSummary', ['scanned', 'migrated', 'skipped', 'failed'])


def _find_depth_artifacts(storage_path):
    """Yield every depth artifact under storage_path, in a stable order.

    Walks the whole tree rather than assuming the shard layout, so artifacts survive even if a store was ever
    reorganised; everything else in the store (pano JPEGs, depth_log.csv, scrape logs) is left alone.
    """
    for dirpath, dirnames, filenames in os.walk(storage_path):
        dirnames.sort()
        for filename in sorted(filenames):
            if filename.endswith(DEPTH_ARTIFACT_SUFFIX):
                yield os.path.join(dirpath, filename)


def _needs_migration(path):
    """True for a pre-v2 artifact: no 'format_version' field, or one below 2.

    This script implements exactly the v1 -> v2 transform (the x-flip), so the threshold is a literal 2: a
    hypothetical future format would need its own migration, not a re-run of this one.
    """
    with np.load(path) as d:
        return 'format_version' not in d.files or int(d['format_version']) < 2


def _migrate_artifact(path):
    """Rewrite one pre-v2 artifact in place: depth flipped to the JPEG's column order, format_version stamped,
    every other field carried over unchanged."""
    with np.load(path) as d:
        contents = {name: d[name] for name in d.files}
    contents['depth'] = contents['depth'][:, ::-1].astype(np.float32)
    contents['format_version'] = 2

    tmp_path = path + '.part'
    try:
        # Same dance as gsv._write_depth_artifact: savez_compressed needs an open file object (given a bare
        # path it silently appends .npz), and the atomic rename means a crash mid-write can never leave a
        # truncated artifact where a good one used to be.
        with open(tmp_path, 'wb') as f:
            np.savez_compressed(f, **contents)
        os.chmod(tmp_path, 0o664)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def migrate_store(storage_path, dry_run=False):
    """Scan storage_path and bring every pre-v2 depth artifact up to v2; see the module docstring.

    @param storage_path Root of the pano store (the directory holding the 2-char shard dirs).
    @param dry_run      Report what would be rewritten without writing anything.
    @return             MigrationSummary(scanned, migrated, skipped, failed).
    """
    scanned, migrated, skipped, failed = 0, 0, 0, 0
    for path in _find_depth_artifacts(storage_path):
        scanned += 1
        try:
            if not _needs_migration(path):
                skipped += 1
                continue
            if not dry_run:
                _migrate_artifact(path)
            migrated += 1
            print("%s %s" % ('Would migrate' if dry_run else 'Migrated', path))
        except Exception as e:
            # A truncated or foreign file: report it and leave the bytes for a human rather than guessing, and
            # keep going - one bad artifact must not stop a sweep of a multi-terabyte store.
            failed += 1
            print("FAILED %s: %s" % (path, e))
    return MigrationSummary(scanned, migrated, skipped, failed)


def main():
    parser = argparse.ArgumentParser(
        description='Rewrite pre-v2 (x-mirrored) depth artifacts into the v2 JPEG column order. Idempotent: '
                    'v2 artifacts are never touched, so re-running on a migrated store changes nothing.')
    parser.add_argument('storage_path',
                        help='Root of the pano store - the directory holding the 2-char shard dirs and depth_log.csv.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Only report which artifacts would be rewritten; write nothing.')
    args = parser.parse_args()

    summary = migrate_store(args.storage_path, dry_run=args.dry_run)
    print("Scanned %d depth artifact(s): %d %s, %d already v2, %d failed."
          % (summary.scanned, summary.migrated,
             'would be migrated' if args.dry_run else 'migrated', summary.skipped, summary.failed))
    return 1 if summary.failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
