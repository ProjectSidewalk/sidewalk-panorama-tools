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
import logging
import os
import re

# Side-effect free on import (the #52.1 contract). Its predicates and names are reused rather than restated,
# so the two tools cannot disagree about what a shard or a store file is.
import CropRunner

MigrationSummary = collections.namedtuple(
    'MigrationSummary', ['dirs_moved', 'files_moved', 'store_files_moved', 'collisions', 'failed', 'left'])

# crop.log's rotated segments, as logging.handlers.RotatingFileHandler names them.
_ROTATED_LOG = re.compile(r'crop\.log\.\d+')


def _shards(crop_dir):
    """The label-type shards directly under crop_dir, in a stable order."""
    with os.scandir(crop_dir) as listing:
        return sorted(entry.name for entry in listing if entry.is_dir() and CropRunner._is_numeric_name(entry.name))


def _store_files(crop_dir):
    """The store's own files directly under crop_dir, in the order they move: the log and its rotated
    segments, then the manifests, then crop_rule.json LAST. The marker is what CropRunner's legacy refusal
    and this tool's city check read, so a run that dies partway leaves the root still marked as a flat
    store - refused by CropRunner, and checked again by the next run of this one."""
    with os.scandir(crop_dir) as listing:
        names = {entry.name for entry in listing if not entry.is_dir()}
    logs = sorted(name for name in names if _ROTATED_LOG.fullmatch(name))
    ordered = logs + ['crop.log', CropRunner.PROVENANCE_MANIFEST_PRE_CITY, CropRunner.PROVENANCE_MANIFEST,
                      CropRunner.CROP_RULE_MARKER]
    return [name for name in ordered if name in names]


def _move_file(source, target, dry_run, counts, key, make_parent=False):
    """One file, never over another. Counts under `key` on a move (or a predicted one). make_parent
    creates the target's directory first - only where it may not exist, since over sshfs even an
    exist_ok makedirs is a round trip, and a shard's files number in the tens of thousands."""
    if os.path.lexists(target):
        counts['collisions'] += 1
        print("COLLISION %s -> %s: destination exists; both left as they are" % (source, target))
        return
    if not dry_run:
        try:
            if make_parent:
                os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(source, target)
        except OSError as e:
            counts['failed'] += 1
            print("FAILED %s -> %s: %s" % (source, target, e))
            return
    counts[key] += 1
    print("%s file %s -> %s" % ('Would move' if dry_run else 'Moved', source, target))


def migrate_store(crop_dir, city, dry_run=False):
    """Move crop_dir's label-type shards, then its store files, under crop_dir/<city>/.

    Refuses, before touching anything, a crop_dir that looks like the production canvas-capture store
    (CropRunner.refuse_production_crop_store - its root holds city directories too, and nothing in it can
    be regenerated), and a crop_dir or crop_dir/<city>/ whose crop_rule.json records another city or
    cannot be read (CropRunner.check_store_city): moving Chicago's store into seattle-wa/ would be the
    collision #159 exists to end, made permanent. A marker with no city - every store cut before #153's
    stopgap - passes, and CropRunner adopts the city on its next run.

    Everything else at the root - other cities' stores, notes, figures - is left alone.

    :return: MigrationSummary. Under dry_run every count is a prediction and nothing is written.
    :raises CropRunner.ProductionCropStoreError, CropRunner.CropStoreCityError: with nothing touched.
    """
    CropRunner.refuse_production_crop_store(crop_dir)
    store = os.path.join(crop_dir, city)
    CropRunner.check_store_city(crop_dir, city)
    CropRunner.check_store_city(store, city)

    counts = dict.fromkeys(MigrationSummary._fields, 0)

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
            _move_file(entry.path, os.path.join(destination, entry.name), dry_run, counts, 'files_moved')
        # Only once empty: a collision or a left directory keeps the shard, and its crops, where they are.
        if not dry_run and not os.listdir(source):
            os.rmdir(source)

    for name in _store_files(crop_dir):
        _move_file(os.path.join(crop_dir, name), os.path.join(store, name), dry_run, counts, 'store_files_moved',
                   make_parent=True)

    return MigrationSummary(**counts)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Move a pre-#159 crop store (<crop-dir>/<label_type_id>/<label_id>.jpg) under "
                    "<crop-dir>/<city>/. Moves only - never copies, replaces or deletes; a file already at "
                    "a destination is listed as a collision and both are left where they are.")
    parser.add_argument('crop_dir', help='the directory CropRunner used as -o before #159; it stays the -o '
                                         'after, holding one store per city')
    # CropRunner's own argparse type: well-formed, and an active row of log_analyzer/cities.csv, so a typo
    # cannot move a store under a directory CropRunner would never be pointed at.
    parser.add_argument('--city', required=True, type=CropRunner.city_id,
                        help='the city whose crops these are (seattle-wa, cdmx - log_analyzer/cities.csv)')
    parser.add_argument('--dry-run', action='store_true',
                        help='print every move and collision the real run would make, and write nothing')
    return parser


def summary_line(summary, dry_run):
    """The counts in words, in the form a dry run predicts and a real run reports."""
    would = ' would be' if dry_run else ''
    return ("%d type directories%s moved whole, %d files%s moved one by one, %d store files%s moved, "
            "%d collisions%s left in place, %d left for a person, %d failed."
            % (summary.dirs_moved, would, summary.files_moved, would, summary.store_files_moved, would,
               summary.collisions, would, summary.left, summary.failed))


def main(argv=None):
    """:return: 0 when the store is migrated (or there was nothing to move); 1 when anything was left where it
             was - a collision, a directory inside a shard, a failed rename - predicted ones included under
             --dry-run, since CropRunner keeps refusing the root until a person settles each; 2 on a usage
             error, a crop dir that does not exist included; 3 when the root is refused - it looks like the
             production canvas-capture store, or its crop_rule.json (or <city>/'s) records another city or
             cannot be read - with nothing touched."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not os.path.isdir(args.crop_dir):
        parser.error("%s is not a directory" % args.crop_dir)
    try:
        summary = migrate_store(args.crop_dir, args.city, dry_run=args.dry_run)
    except (CropRunner.ProductionCropStoreError, CropRunner.CropStoreCityError) as e:
        # Both channels, CropRunner's pattern: logging is not configured, so this reaches stderr through the
        # root logger's last-resort handler.
        print("migrate_crop_store: %s" % e)
        logging.error('%s', e)
        return CropRunner.EXIT_REFUSED_DESTINATION
    print(summary_line(summary, args.dry_run))
    incomplete = summary.collisions or summary.failed or summary.left
    if incomplete:
        print("Everything listed above is still where it was. CropRunner refuses %s until the root holds no "
              "label-type directory or store file of its own: settle each by hand, then re-run this."
              % args.crop_dir)
    elif not args.dry_run:
        print("Next: python3 CropRunner.py (-d <fqdn> | -f <file>) -s <pano-dir> -o %s --city %s"
              % (args.crop_dir, args.city))
    print("Consumers that read %s must now read %s, or glob %s and key on (city, label_id)."
          % (os.path.join(args.crop_dir, '<label_type_id>'),
             os.path.join(args.crop_dir, args.city, '<label_type_id>'),
             os.path.join(args.crop_dir, '*', '<label_type_id>')))
    return 1 if incomplete else 0


if __name__ == '__main__':
    raise SystemExit(main())
