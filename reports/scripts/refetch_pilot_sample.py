"""Draw the seeded subset of a `fover` re-fetch work-list that a pilot runs on (#73).

    python reports/scripts/refetch_pilot_sample.py reports/data/2026-08-19-fover-refetch-worklist-seattle.csv.gz \\
        --n 200 --seed 20260905 --write reports/data/<date>-fover-refetch-pilot-worklist-seattle.csv.gz

`refetch_panos.py --sample N` draws its own random N, but it draws them inside the run, so nothing on disk
says which N a pilot touched. A pilot run against a COPY of the store - the shape the first pilot took, so
that a swap decision could be made on the numbers rather than on the originals - needs the draw up front:
the drawn ids are what gets copied out of the store, and the drawn work-list is what the pass is pointed at.
Committing that draw, and the seed that produced it, is what makes the pilot's artifact reproducible from a
fresh clone.

The draw keeps the input file's own order rather than the random one, so the committed subset is a stable,
diffable artifact. refetch_panos.py shuffles its candidates regardless, so the order carries no processing
meaning either way.
"""

import argparse
import collections
import csv
import gzip
import os
import random


def read_rows(path):
    """The work-list's rows, as dicts of strings, in file order. csv rather than pandas: pano_id must never
    take its type from what the ids happen to look like (#46)."""
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt', newline='', encoding='utf8') as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def draw(rows, n, seed):
    """`n` rows of `rows` chosen by random.Random(seed).sample, returned in the input's order.

    Exactly this call, and no other sequence of draws from the generator, so the same seed reproduces the
    same subset: tests/test_refetch_pilot_sample.py asserts the committed subset against it.
    """
    chosen = random.Random(seed).sample(rows, n)
    position = {id(row): i for i, row in enumerate(rows)}
    return sorted(chosen, key=lambda row: position[id(row)])


def write_rows(path, rows, fieldnames):
    with gzip.open(path, 'wt', newline='', encoding='utf8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def frame_breakdown(rows):
    return collections.Counter('%sx%s' % (r.get('width'), r.get('height')) for r in rows)


def build_parser():
    parser = argparse.ArgumentParser(description='Draw the seeded subset of a work-list a pilot runs on (#73).')
    parser.add_argument('worklist', help='A work-list from pano_y_histogram.py --write-worklist (csv or csv.gz).')
    parser.add_argument('--n', type=int, default=200, help='Rows to draw. Default 200.')
    parser.add_argument('--seed', type=int, required=True,
                        help='Seed for the draw. Required, and recorded with the artifact, so the draw can be '
                             'reproduced rather than merely described.')
    parser.add_argument('--write', default=None, metavar='PATH',
                        help='Write the subset here, gzipped CSV. Without it, only the breakdown is printed.')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    rows, fieldnames = read_rows(args.worklist)
    if args.n > len(rows):
        raise SystemExit('asked for %d rows but %s has %d' % (args.n, args.worklist, len(rows)))
    subset = draw(rows, args.n, args.seed)
    print('%d of %d rows, seed %d; frames: %s'
          % (len(subset), len(rows), args.seed,
             ', '.join('%s=%d' % kv for kv in frame_breakdown(subset).most_common())))
    if args.write:
        write_rows(args.write, subset, fieldnames)
        print('wrote %s' % os.path.relpath(args.write) if not os.path.isabs(args.write) else args.write)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
