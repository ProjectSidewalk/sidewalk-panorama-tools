"""Convert the flag_panos web tool's JSON output to CSV, for one city.

The tool (index.html / index.js, see README.md) writes `<city>_pano_image_data.json` and
`<city>_unretrievable_panos.json`. This turns whichever of them exist into CSVs beside them.

The module imports with no side effects (the #52.1 contract the two runners honour): build_parser() and
main(argv) are the seams, and `python3 flag_panos/json_to_csv.py --city <city>` behaviour lives under the
__main__ guard. It used to do all of its work at module scope against `CITY = 'amsterdam'` and the CWD, so
using it on another city meant editing the file, importing it ran it, and a missing input surfaced as a bare
traceback naming one filename and neither the city nor the directory it had looked in (#52 item 6).
"""

import argparse
import csv
import json
import os
import sys

# The two artifacts the web tool produces. Suffixes, not whole names, because the city is a parameter now.
INPUT_SUFFIXES = ('_pano_image_data', '_unretrievable_panos')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--city', required=True,
                        help="City id the flag_panos filenames are prefixed with, i.e. amsterdam")
    parser.add_argument('--dir', default='.',
                        help='Directory holding the JSON files; the CSVs are written beside them. default=.')
    return parser


def convert(directory, city):
    """Convert every INPUT_SUFFIXES file present for `city`; return the list of CSV paths written.

    Absent inputs are skipped rather than fatal - the web tool does not always produce both, and failing the
    whole run over the missing one would make this unusable in the case it exists for. main() is what decides
    that finding NOTHING is an error.
    """
    written = []
    for suffix in INPUT_SUFFIXES:
        json_path = os.path.join(directory, '%s%s.json' % (city, suffix))
        if not os.path.isfile(json_path):
            continue
        csv_path = os.path.join(directory, '%s%s.csv' % (city, suffix))
        with open(json_path, encoding='utf-8') as f:
            records = json.load(f)
        write_csv(csv_path, records)
        written.append(csv_path)
    return written


def write_csv(csv_path, records):
    """Write a list of dicts as a CSV whose columns are the union of their keys, in first-seen order.

    The union, rather than the first record's keys, because the web tool writes one object shape per file
    today but nothing enforces that, and a DictWriter fed a key it doesn't know about raises.

    Written with csv rather than pandas (#72), which had two failure modes here. read_json inferred each
    column's type from the values, so one record missing `image_width` retyped the whole column and the
    records that did carry 16384 were written as 16384.0; and an all-numeric pano_id column inferred int64
    (the #46 class - this site never had the dtype pin the two runners did). newline='' because the writer
    emits \\r\\n itself, and letting the file object translate that again doubles every line ending.
    """
    fieldnames = list(dict.fromkeys(key for record in records for key in record))
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def main(argv=None):
    """:return: 0, or 1 if the city had none of the expected inputs in the given directory."""
    args = build_parser().parse_args(argv)

    written = convert(args.dir, args.city)
    if not written:
        print("json_to_csv: no flag_panos JSON for city %r in %s (looked for %s)"
              % (args.city, os.path.abspath(args.dir),
                 ', '.join('%s%s.json' % (args.city, s) for s in INPUT_SUFFIXES)),
              file=sys.stderr)
        return 1

    for path in written:
        print("Wrote %s" % path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
