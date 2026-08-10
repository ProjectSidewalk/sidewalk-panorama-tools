"""
** Crop Extractor for Project Sidewalk **

Given label metadata from the Project Sidewalk database, this script will extract JPEG crops of the features that have
been labeled. The required metadata should be obtained through an API endpoint on the Project Sidewalk server for a
given city, passed as an argument to this script. Alternatively, if you have a CSV containing this data (from running
the samples/getFullLabelList.sql script) you can pass in the name of that CSV file as an argument.

Additionally, you should have downloaded original panorama images from Street View using DownloadRunner.py. You will
need to supply the path to the folder containing these files.

The module imports with no side effects (the #52.1 contract): build_parser() / configure_logging() / run() / main()
are the seams, and `python3 CropRunner.py ...` behaviour lives under the __main__ guard.
"""

import argparse
import json
import logging
import logging.handlers
import math
import os
import sys

import pandas as pd
import requests
from PIL import Image, ImageDraw
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from downloaders.common import atomic_output_path

# The largest panos our own downloader writes are 16384x8192 = 134 MP, over Pillow's 89 MP
# DecompressionBombWarning default; bulk_extract_crops raises the ceiling to this rather than warning once
# per modern pano (or hard-failing at 2x the threshold). Kept as a named constant, not None: the store is
# trusted, but "no limit at all" would also swallow a genuinely corrupt header claiming absurd dimensions.
_MAX_PANO_PIXELS = 16384 * 8192

# What the crop loop actually reads off every label row. Only the CSV intake enforces them up front (a
# header typo is one error naming the file, not a KeyError 200k labels in); the JSON/server intake keeps
# whatever the payload had, and bulk_extract_crops counts a row missing any of them as one bad label.
REQUIRED_LABEL_COLUMNS = ('pano_id', 'pano_x', 'pano_y', 'label_type_id', 'label_id')


def raise_decompression_bomb_ceiling():
    """Let Pillow decode our own 134 MP panos without a DecompressionBombWarning on every one.

    Process-level policy, so main() calls it and bulk_extract_crops does not: this rewrites a PIL global
    that belongs to whoever imported us, and since #52.1 that can be another program. A library caller
    who wants the ceiling calls this itself; one who doesn't gets a warning, not a failure (Pillow only
    hard-fails above 2x the threshold, and 134 MP is under 2x the 89 MP default).
    """
    if Image.MAX_IMAGE_PIXELS is not None and Image.MAX_IMAGE_PIXELS < _MAX_PANO_PIXELS:
        Image.MAX_IMAGE_PIXELS = _MAX_PANO_PIXELS


def build_parser():
    parser = argparse.ArgumentParser()
    group_parser = parser.add_mutually_exclusive_group(required=True)
    group_parser.add_argument('-d', help='sidewalk_server_domain (preferred over metadata_file) - FDQN of SidewalkWebpage server to fetch label list from, i.e. sidewalk-columbus.cs.washington.edu')
    group_parser.add_argument('-f', help='metadata_file - path to file containing label_ids and their properties. It may be CSV or JSON. i.e. samples/labeldata.csv')
    parser.add_argument('-s', default='/tmp/download_dest/', help='pano_storage_directory - path to directory containing panoramas downloaded using DownloadRunner.py. default=/tmp/download_dest/')
    parser.add_argument('-o', default='/crops/', help='crop_output_directory - path to location for saving the crops. default=/crops/')
    parser.add_argument('--mark-label', action='store_true', help='Draw a dot at the label position in every crop. Debugging aid - deliberately OFF by default, because these crops are ML training data and a synthetic marker painted over the feature of interest is exactly what a model would learn instead of the feature.')
    return parser


def configure_logging(log_path):
    """Set up run-wide logging to log_path (crop.log next to the crops, not the CWD).

    The DownloadRunner shape (#49): rotation bounds growth, urllib3's and PIL's per-operation DEBUG chatter
    is capped at WARNING, and if the log file can't be opened we fall back to stderr with one loud warning
    rather than killing the run.
    """
    try:
        handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=3)
        fallback_error = None
    except OSError as e:
        handler = logging.StreamHandler()
        fallback_error = e
    handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    if fallback_error is not None:
        logging.warning("Could not open %s (%s); logging to stderr for this run", log_path, fallback_error)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('PIL').setLevel(logging.WARNING)


def request_session():
    """A hardened session for the metadata fetch - the DownloadRunner #51 shape.

    Both schemes are mounted so a redirect hop to http:// can't silently fall back to the retry-less default
    adapter, and trust_env is off so the run doesn't newly honour HTTP(S)_PROXY / REQUESTS_CA_BUNDLE on
    whatever box it lands on. read=0: retrying a slow admin query is just hammering it five more times.
    """
    session = requests.Session()
    session.trust_env = False
    retries = Retry(total=5, connect=5, read=0, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def fetch_label_ids_csv(metadata_csv_path):
    """
    Reads metadata from a csv. Useful for old csv formats of cvMetadata such as cv-metadata-seattle.csv.
    Dedupes on label_id.

    pano_id is dtype-pinned to str: an all-numeric (Mapillary) column otherwise infers int64 and crashes
    every pano_id[:2] shard slice - the same #46 intake bug DownloadRunner.fetch_pano_ids_csv fixed. The
    column guard exists because pd.read_csv ignores dtype keys for absent columns, so a header typo would
    otherwise surface as a KeyError deep in the crop loop instead of an error naming the file.
    """
    df_meta = pd.read_csv(metadata_csv_path, dtype={'pano_id': str})
    missing = [c for c in REQUIRED_LABEL_COLUMNS if c not in df_meta.columns]
    if missing:
        raise ValueError("%s is missing required column(s) %r; found %r"
                         % (metadata_csv_path, missing, list(df_meta.columns)))
    return df_meta.drop_duplicates(subset=['label_id']).to_dict('records')


def json_to_list(jsondata):
    """
    Transforms json like object to a list of dict to be read in bulk_extract_crops() to crop panos with label metadata
    :param jsondata: json object containing label ids and their associated properties
    :return: A list of dicts containing the following metadata: label_id, pano_id, label_type_id, agree_count,
    disagree_count, notsure_count, pano_width, pano_height, pano_x, pano_y, canvas_width, canvas_height, canvas_x,
    canvas_y, zoom, heading, pitch, camera_heading, camera_pitch, source
    """
    unique_label_ids = set()
    label_info = []

    for value in jsondata:
        label_id = value["label_id"]
        if label_id not in unique_label_ids:
            unique_label_ids.add(label_id)
            label_info.append(value)
        else:
            print("Duplicate label ID")
    return label_info


def fetch_cvMetadata_from_file(metadata_json_path):
    """
    Reads json file to extract labels.
    :param metadata_json_path: the path of the json file containing all label ids and their associated data.
    :return: A list of dicts, one per label (see json_to_list).
    """
    with open(metadata_json_path) as json_file:
        json_meta = json.load(json_file)
    return json_to_list(json_meta)


def fetch_cvMetadata_from_server(server_fdqn):
    """
    Fetch cvMetadata over HTTP and transform it into a list of dicts, one per label.

    Any request failure - connection, HTTP status, retries exhausted - is logged with the actual exception
    text and exits 1. (The old handler pair missed ConnectionError entirely and logged a placeholder-less
    'Retries: '.format(e) that dropped the exception, #48.)
    """
    url = 'https://' + server_fdqn + '/adminapi/labels/cvMetadata'
    try:
        print("Getting metadata from web server")
        with request_session() as session:
            # (connect, read) timeouts; generous read half because the server may buffer the whole JSON
            # before its first byte - the DownloadRunner /adminapi/panos rationale.
            response = session.get(url, timeout=(30, 600))
            response.raise_for_status()
            jsondata = response.json()
    except requests.exceptions.RequestException as e:
        logging.error('Fetching cvMetadata from %s failed: %s', url, e)
        print("Cannot fetch metadata from webserver. Check log file.")
        sys.exit(1)

    return json_to_list(jsondata)


def load_label_metadata(sidewalk_server_fdqn, label_metadata_file):
    """Dispatch to the right intake for -d / -f, with a clear error for an unrecognized -f extension
    (which used to fall through to a NameError, #48)."""
    if label_metadata_file is not None:
        extension = os.path.splitext(label_metadata_file)[1]
        if extension.lower() == ".csv":
            return fetch_label_ids_csv(label_metadata_file)
        if extension.lower() == ".json":
            return fetch_cvMetadata_from_file(label_metadata_file)
        sys.exit("CropRunner: unrecognized metadata file extension %r (expected .csv or .json): %s"
                 % (extension, label_metadata_file))
    return fetch_cvMetadata_from_server(sidewalk_server_fdqn)


def predict_crop_size(pano_y, pano_height):
    """
    As it stands, this algorithm:
    1. Converts `pano_y` and `pano_height` to the old version of `pano_y` that we had when this alg was written.
    2. Approximates the distance to label from camera using an experimentally determined formula.
    3. Predict an ideal crop size using an experimentally determined formula based on the estimated distance.

    Here is some context for the current formulae:
    https://github.com/ProjectSidewalk/sidewalk-cv-tools/issues/2#issuecomment-510609873
    https://github.com/ProjectSidewalk/SidewalkWebpage/issues/633#issuecomment-307283178

    There are some clear areas to improve this function:
    1. We have an updated distance estimation formula that takes into account zoom level:
       https://github.com/ProjectSidewalk/SidewalkWebpage/blob/develop/public/javascripts/SVLabel/src/SVLabel/label/Label.js#L17
    2. That distance estimation formula should be recreated given some of the bugs we've fixed in the past few years.
    """
    old_pano_y = pano_height / 2 - pano_y
    crop_size = 0
    distance = max(0, 19.80546390 + 0.01523952 * old_pano_y)

    if distance > 0:
        crop_size = 8725.6 * (distance ** -1.192)
    if crop_size > 1500 or distance == 0:
        crop_size = 1500
    if crop_size < 50:
        crop_size = 50

    return crop_size


def make_single_crop(pano, pano_x, pano_y, output_filename, draw_mark=False):
    """
    Makes a crop around the object of interest and saves it atomically.

    :param pano: an open PIL.Image, or a path to one. bulk_extract_crops opens each pano once and passes
                 the image (a 16384x8192 pano is ~250 MB decoded; re-opening per label decoded it once per
                 label); the path form is kept for one-off use.
    :param pano_x: x-pixel of label on the GSV image
    :param pano_y: y-pixel of label on the GSV image
    :param output_filename: name of file for saving
    :param draw_mark: if a dot should be drawn at the label position in the crop
    :return: none
    """
    close_after = False
    if not hasattr(pano, 'crop'):
        pano = Image.open(pano)
        close_after = True
    try:
        pano_height = pano.size[1]

        crop_size = predict_crop_size(pano_y, pano_height)
        top_left_x = pano_x - crop_size / 2
        top_left_y = pano_y - crop_size / 2
        cropped_square = pano.crop((top_left_x, top_left_y, top_left_x + crop_size, top_left_y + crop_size))

        if draw_mark:
            # Draw on the crop, never the source pano: the pano image is shared by every label on it, so a
            # mark on the source would leak this label's dot into its neighbours' crops. Pillow rounds the
            # crop box the same way round() does, hence the round() here to land on the true label pixel.
            draw = ImageDraw.Draw(cropped_square)
            r = 10
            centre_x = pano_x - round(top_left_x)
            centre_y = pano_y - round(top_left_y)
            draw.ellipse((centre_x - r, centre_y - r, centre_x + r, centre_y + r), fill=128)

        # The crop file is its own resume marker (bulk_extract_crops skips existing ones), so a mid-write
        # crash must not leave a truncated .jpg the next run trusts - same contract as every write in
        # downloaders/. format= is explicit because the temp path ends in .part, not .jpg.
        with atomic_output_path(output_filename) as tmp_path:
            cropped_square.save(tmp_path, format='JPEG')
    finally:
        if close_after:
            pano.close()


def bulk_extract_crops(labels_to_crop, path_to_gsv_scrapes, destination_dir, mark_label=False):
    """Extract one crop per label into <destination_dir>/<label_type_id>/<label_id>.jpg.

    Failure taxonomy: nothing here is fatal. A missing pano image is counted as missing_pano; a corrupt
    pano, malformed row, or failed write is counted as an error and logged; both leave the remaining labels
    running (#48 - one truncated JPEG used to kill a job tens of thousands of labels in). That includes the
    output side: a full store or a read-only mount is one counted error per label, not an exception out of
    this function with the counts lost. Crops on disk are the resume marker: existing ones are counted as
    skipped_existing and everything failed here is simply re-attempted on the next run.

    :return: counts dict; success + skipped_existing + missing_pano + errors == total, including on re-runs.
    """
    counts = {'total': len(labels_to_crop), 'success': 0, 'skipped_existing': 0,
              'missing_pano': 0, 'errors': 0}

    # Parse rows up front and group labels by pano (preserving first-seen order), so each pano JPEG is
    # decoded exactly once for all its labels.
    labels_by_pano = {}
    for row in labels_to_crop:
        try:
            raw_id = row['pano_id']
            # A blank CSV cell arrives as float nan, which str() would keep as the id 'nan' and quietly
            # send down a 'na/nan.jpg' shard path - the _normalize_pano_records lesson.
            if raw_id is None or (isinstance(raw_id, float) and math.isnan(raw_id)):
                raise ValueError("missing pano_id")
            # A JSON payload carries '' where a blank CSV cell carries nan; both are missing metadata, but
            # '' shards to the store root, so unguarded it would be filed as a pano we are still waiting
            # on rather than a bad row. Stripped because a hand-edited CSV can carry padding and no pano
            # id in either source has ever contained whitespace.
            pano_id = str(raw_id).strip()
            if not pano_id:
                raise ValueError("empty pano_id")
            pano_x = float(row['pano_x'])
            pano_y = float(row['pano_y'])
            label_type = int(row['label_type_id'])
            label_id = int(row['label_id'])
            if not (math.isfinite(pano_x) and math.isfinite(pano_y)):
                raise ValueError("non-finite label position (%r, %r)" % (row['pano_x'], row['pano_y']))
        except (KeyError, TypeError, ValueError) as e:
            counts['errors'] += 1
            logging.warning("Skipping malformed label row %r: %s", row, e)
            continue
        labels_by_pano.setdefault(pano_id, []).append((pano_x, pano_y, label_type, label_id))

    processed = counts['errors']
    made_dirs = set()
    for pano_id, labels in labels_by_pano.items():
        pano_img_path = os.path.join(path_to_gsv_scrapes, pano_id[:2], pano_id + ".jpg")

        if not os.path.exists(pano_img_path):
            counts['missing_pano'] += len(labels)
            processed += len(labels)
            print("Panorama image not found: %s (%d labels skipped)" % (pano_img_path, len(labels)))
            logging.warning("Skipped %d labels on pano %s due to missing image.", len(labels), pano_id)
            continue

        try:
            pano = Image.open(pano_img_path)
        except Exception as e:
            counts['errors'] += len(labels)
            processed += len(labels)
            logging.warning("Skipped %d labels on pano %s: cannot open %s (%s)",
                            len(labels), pano_id, pano_img_path, e)
            continue

        # Not `with pano:` - Image.__exit__ has been a no-op since Pillow 11, so the `with` form silently
        # stopped closing anything while requirements.txt still allows the older Pillow where it did.
        # close() is what actually releases the decoded buffer, which is the whole cost decode-once
        # accepts (~250 MB for a 16384x8192 pano).
        try:
            for pano_x, pano_y, label_type, label_id in labels:
                processed += 1
                print("Cropping label %d of %d (pano %s)" % (processed, counts['total'], pano_id))

                destination_folder = os.path.join(destination_dir, str(label_type))
                crop_destination = os.path.join(destination_folder, str(label_id) + ".jpg")

                if os.path.exists(crop_destination):
                    counts['skipped_existing'] += 1
                    continue
                try:
                    # Once per label type, not per label: exist_ok still costs a stat, and over sshfs
                    # that is a network round trip for each of a city's ~400k labels. Inside the try
                    # because an OSError here - a full store, a read-only mount, an sshfs drop - must be
                    # one counted error like any other write failure, not the end of the run (#48).
                    if destination_folder not in made_dirs:
                        os.makedirs(destination_folder, exist_ok=True)
                        made_dirs.add(destination_folder)
                    make_single_crop(pano, pano_x, pano_y, crop_destination, draw_mark=mark_label)
                except Exception as e:
                    counts['errors'] += 1
                    logging.warning("Failed to crop label %d on pano %s: %s", label_id, pano_id, e)
                    continue
                counts['success'] += 1
                logging.info('%s.jpg %s %s %s', label_id, pano_id, pano_x, pano_y)
        finally:
            pano.close()

    print("Finished.")
    print("%d crops extracted, %d already existed, %d skipped because the panorama image was missing, "
          "%d errors, of %d labels total."
          % (counts['success'], counts['skipped_existing'], counts['missing_pano'],
             counts['errors'], counts['total']))
    return counts


def run(sidewalk_server_fqdn, label_metadata_file, gsv_pano_path, crop_destination_path, mark_label=False):
    """Load the label metadata and extract every crop - the whole job, minus process-level setup.

    main() owns argv parsing, directory creation, and logging; this seam takes plain arguments so tests can
    drive the real intake -> crop loop in-process (the #52.1 shape).
    """
    print("Cropping labels")
    label_infos = load_label_metadata(sidewalk_server_fqdn, label_metadata_file)
    return bulk_extract_crops(label_infos, gsv_pano_path, crop_destination_path, mark_label=mark_label)


def main(argv=None):
    """Process-level setup, then run(): everything a `python3 CropRunner.py ...` invocation does.

    Exceptions propagate, argparse errors exit 2, and an unrecognized -f extension exits with a message -
    not the NameError it used to be.

    :return: 0, or 1 if any label errored. Deliberately not keyed on "did every label produce a crop":
             missing panos are the normal state of a city whose scrape is still catching up, while
             `errors` only ever counts things that should not have happened - a corrupt pano, a
             malformed row, a failed write - so it is the half worth waking someone for.
    """
    args = build_parser().parse_args(argv)

    # exist_ok: a re-run, or an operator pre-creating the dir, races on the exists check. Note this is not
    # a claim that two CropRunners may share an output dir: crops are written through a fixed
    # <label_id>.jpg.part, so concurrent runs over the same labels would fight over that temp path.
    os.makedirs(args.o, exist_ok=True)

    # crop.log lives next to the crops it describes, NOT the CWD (which under Docker/cron is wherever the
    # process happened to start and dies with it - the DownloadRunner #49 lesson).
    configure_logging(os.path.join(args.o, 'crop.log'))

    raise_decompression_bomb_ceiling()

    counts = run(sidewalk_server_fqdn=args.d, label_metadata_file=args.f, gsv_pano_path=args.s,
                 crop_destination_path=args.o, mark_label=args.mark_label)
    return 1 if counts['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
