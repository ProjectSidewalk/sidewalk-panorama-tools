"""
One-off, move-only migrator: a pre-#159 crop store into one city's store (#159).

Before #159, CropRunner's -o WAS the store: <crop-dir>/<label_type_id>/<label_id>.jpg, with crop_rule.json,
crop_provenance.csv and crop.log beside the shards. label_id restarts at 1 in every deployment, so that
layout let two cities' crops share file names. Since #159 -o is a root holding one store per city,
<crop-dir>/<city>/<label_type_id>/<label_id>.jpg, and CropRunner refuses a -o still in the old layout,
naming this script.

    python3 migrate_crop_store.py <crop-dir> --city <city_id> [--dry-run]

It MOVES, never copies, and never replaces: a file already at a destination is a collision, counted and
listed, and both files are left exactly where they are. Nothing is rewritten or deleted, so every crop and
every byte of the store's record ends up either in its place under <crop-dir>/<city>/ or where it was. It
is idempotent and resumable - a second run, or one after a run that died partway, finishes the job and
touches nothing else - and --dry-run writes nothing at all. One migrator per store at a time.

Like the depth migrator (migrate_depth_artifacts.py) this exists because nothing else ever revisits what is
on disk: CropRunner's resume marker is the crop file, so a store left in the old layout stays there.
"""

import argparse
import collections
import os

# Side-effect free on import (the #52.1 contract). Its predicates and names are reused rather than restated,
# so the two tools cannot disagree about what a shard or a store file is.
import CropRunner

MigrationSummary = collections.namedtuple(
    'MigrationSummary', ['dirs_moved', 'files_moved', 'store_files_moved', 'collisions', 'failed', 'left'])


def _shards(crop_dir):
    """The label-type shards directly under crop_dir, in a stable order."""
    with os.scandir(crop_dir) as listing:
        return sorted(entry.name for entry in listing if entry.is_dir() and CropRunner._is_numeric_name(entry.name))


def migrate_store(crop_dir, city, dry_run=False):
    """Move crop_dir's label-type shards (and, after them, its store files) under crop_dir/<city>/.

    :return: MigrationSummary. Under dry_run every count is a prediction and nothing is written.
    """
    counts = dict.fromkeys(MigrationSummary._fields, 0)
    store = os.path.join(crop_dir, city)
    verb = 'Would move' if dry_run else 'Moved'

    for name in _shards(crop_dir):
        source = os.path.join(crop_dir, name)
        destination = os.path.join(store, name)
        if not os.path.lexists(destination):
            # One rename for the whole shard - over sshfs, one round trip instead of one per crop.
            if dry_run:
                print("Would move directory %s -> %s (%d files)" % (source, destination, len(os.listdir(source))))
                counts['dirs_moved'] += 1
                continue
            try:
                os.makedirs(store, exist_ok=True)
                os.rename(source, destination)
            except OSError as e:
                counts['failed'] += 1
                print("FAILED %s -> %s: %s" % (source, destination, e))
                continue
            counts['dirs_moved'] += 1
            print("Moved directory %s -> %s" % (source, destination))
            continue

        # The destination shard exists (a CropRunner run under #159, or an earlier partial migration):
        # file by file, never over a file already there.
        with os.scandir(source) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
        for entry in entries:
            if entry.is_dir():
                counts['left'] += 1
                print("LEFT %s: a directory inside a label-type shard, which this tool never writes; not "
                      "moved" % entry.path)
                continue
            target = os.path.join(destination, entry.name)
            if os.path.lexists(target):
                counts['collisions'] += 1
                print("COLLISION %s -> %s: destination exists; both left as they are" % (entry.path, target))
                continue
            if not dry_run:
                try:
                    os.rename(entry.path, target)
                except OSError as e:
                    counts['failed'] += 1
                    print("FAILED %s -> %s: %s" % (entry.path, target, e))
                    continue
            counts['files_moved'] += 1
            print("%s file %s -> %s" % (verb, entry.path, target))
        if not dry_run and not os.listdir(source):
            os.rmdir(source)

    return MigrationSummary(**counts)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('crop_dir')
    parser.add_argument('--city', required=True)
    parser.add_argument('--dry-run', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    summary = migrate_store(args.crop_dir, args.city, dry_run=args.dry_run)
    return 1 if summary.collisions or summary.failed or summary.left else 0


if __name__ == '__main__':
    raise SystemExit(main())
