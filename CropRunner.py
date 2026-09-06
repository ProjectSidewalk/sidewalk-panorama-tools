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
import collections
import csv
import json
import logging
import logging.handlers
import math
import os
import sys

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

# The crop window compute_crop_box resolves. `shifted` rides along rather than being recomputed by
# callers: it is derived from the same rounding that produced `top`, so the two cannot drift apart.
CropBox = collections.namedtuple('CropBox', ['left', 'top', 'width', 'height', 'shifted'])

# Which sizing rule cut a crop store. Stamped into the run summary because crops are derived data with
# no other provenance on disk: a store cut under one rule and topped up under another is otherwise
# indistinguishable from a consistent one, and every consumer here trains on whole directories.
CROP_RULE_VERSION = 'v2'

# ---------------------------------------------------------------------------
# Sizing rule v2. Measured, not guessed - see reports/2026-08-19-crop-sizing-v2.md for the four-city
# extent gold and the three blind human rounds these five numbers come out of.

# The 2013 pano-y -> distance -> size regression, and the pano height it was fit on. The formula is
# unchanged; what v2 fixes is that it used to be fed native pixels regardless of resolution.
V1_REF_HEIGHT = 6656.0
V1_DIST_INTERCEPT = 19.80546390
V1_DIST_SLOPE = 0.01523952
V1_SIZE_COEF = 8725.6
V1_SIZE_EXP = -1.192
V1_SIZE_MIN = 50.0
V1_SIZE_MAX = 1500.0

# The window the formula asks for is about the size of the ramp itself, so it ships scaled. x2.5 is
# where two independent instruments overlap: absolute judgement puts "too tight" above fill 0.49, which
# needs at least x1.95, and two forced-choice rounds peak at fill 0.28-0.44, i.e. x2-x3.
CROP_SIZE_SCALE = 2.5

# The scaled window is clamped as an angle, not as pixels, because the whole point of v1-norm is that a
# window means the same thing on a 2048-px pano as on a 16384-px one. The floor keeps a far-field crop
# from collapsing to a postage stamp; the ceiling stops a near-field one from swallowing a quarter of
# the sphere. Measured over 482 gold ramps, the ceiling binds on 9-15% of them and the floor never does.
CROP_MIN_FOV_DEG = 8.0
CROP_MAX_FOV_DEG = 90.0

# Windows are cut 3:2 rather than square. Curb-ramp aprons run ~3:1 in equirectangular pixels, so the
# top and bottom of a square window are sky and road; measured, framing quality reads the same at 1:1,
# 3:2 and 2:1 because it is the ramp against the window WIDTH that binds. 3:2 is also the shape the
# rest of Project Sidewalk already assumes - stored crops and share images are 1440x960 and the label
# canvas is 720x480 - so a square window would be stretched 1.5x by ImageController on write.
CROP_ASPECT_W_OVER_H = 1.5

# Crops are stored at min(window, 1440) px wide. A ceiling, not a target: a window narrower than that
# is written at its own size and never upscaled, because the ramp carries a fixed number of source
# pixels and stretching them adds bytes and blur, not detail.
#
# Note what this does and does not change here. v1 wrote the cut window unresized - this file has never
# contained a resize - so against v1 the cap is a REDUCTION at the near field: a 90 deg window on a
# 16384x8192 pano is 4096 px and is now stored at 1440, where v1's 1500 px square was stored whole.
# More world at lower magnification, which is the trade the report argues; it is not the removal of an
# upscale this tool was doing.
#
# The upscale it does remove is one level out. ImageController scales anything it is handed to
# 1440x960 unconditionally (getScaledInstance, no aspect preservation), so a narrower window handed to
# it is upsampled into the stored file - modelled over the gold at a median 4.14x with 89.5% above 2x
# under v1's window sizes, worst where the imagery is weakest (97.0% of Richmond, 98.5% of Annapolis).
# Nothing in this repo takes that path today; the server-side CropService in SidewalkWebpage#4865 is
# what will, which is why 1440 is the number and not something arbitrary.
CROP_MAX_STORED_WIDTH = 1440

# Written into the crop directory so a store says which rule cut it. See write_rule_marker.
CROP_RULE_MARKER = 'crop_rule.json'


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
    group_parser.add_argument('-d', help='sidewalk_server_domain (preferred over metadata_file) - FQDN of SidewalkWebpage server to fetch label list from, i.e. sidewalk-columbus.cs.washington.edu')
    group_parser.add_argument('-f', help='metadata_file - path to file containing label_ids and their properties. It may be CSV or JSON. i.e. samples/labeldata.csv')
    # Required, with no defaults (#52 item 6). -o used to default to the filesystem root and -s to the
    # Docker container's scratch path - and the container runs DownloadRunner, not this script. A forgotten
    # flag should name itself, not quietly put an ML training corpus somewhere nobody thinks to look.
    parser.add_argument('-s', required=True, help='pano_storage_directory - path to directory containing panoramas downloaded using DownloadRunner.py')
    parser.add_argument('-o', required=True, help='crop_output_directory - path to location for saving the crops')
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
    Dedupes on label_id, keeping the first row per id.

    Read with csv, not pandas (#72), so no field's type depends on what the values happen to look like -
    the inference that gave an all-numeric (Mapillary) pano_id column int64 and crashed every pano_id[:2]
    shard slice (#46). Every cell arrives as a str and the crop loop coerces at use, which is why this
    intake needs no per-field conversion of its own; unlike the downloader's, blank cells stay '' rather
    than becoming None, because every consumer here is inside the loop's try/except and handles both.

    utf-8-sig for the Excel BOM, and the column guard because a header typo would otherwise surface as a
    KeyError deep in the crop loop instead of an error naming the file.
    """
    unique_label_ids = set()
    labels = []
    with open(metadata_csv_path, newline='', encoding='utf-8-sig') as csv_file:
        reader = csv.DictReader(csv_file)
        # fieldnames is None for an empty file, and `c not in None` is a TypeError.
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_LABEL_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError("%s is missing required column(s) %r; found %r"
                             % (metadata_csv_path, missing, reader.fieldnames))
        for row in reader:
            # Surplus fields land under the key None. pandas did something worse with the same input -
            # it consumed the first column as the frame's index, shifting every field by one.
            label = {key: value for key, value in row.items() if key is not None}
            label_id = label.get('label_id')
            if label_id in unique_label_ids:
                continue
            unique_label_ids.add(label_id)
            labels.append(label)
    return labels


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


def fetch_cvMetadata_from_server(server_fqdn):
    """
    Fetch cvMetadata over HTTP and transform it into a list of dicts, one per label.

    Any request failure - connection, HTTP status, retries exhausted - is logged with the actual exception
    text and exits 1. (The old handler pair missed ConnectionError entirely and logged a placeholder-less
    'Retries: '.format(e) that dropped the exception, #48.)
    """
    url = 'https://' + server_fqdn + '/adminapi/labels/cvMetadata'
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


def _absent(value):
    """True for a field the row does not actually carry: absent, JSON null, or a blank CSV cell.

    The blank cell is the case that has to be spelled out. '' is not None, so treating only None as
    missing would both skip the width/height fallback below AND hand '' to float(), turning a row that
    simply doesn't claim dimensions into a counted malformed-row error - and main() exits 1 on errors, so
    a blank dims column would fail an otherwise clean run.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def _metadata_dims(row):
    """The pano dimensions a label row claims, or None if it doesn't carry them.

    cvMetadata calls them pano_width/pano_height (null for third-party photospheres); the old CSV export
    calls them width/height. Raises ValueError on non-numeric values so the caller's malformed-row
    handling applies.
    """
    raw_width = row.get('pano_width')
    raw_height = row.get('pano_height')
    if _absent(raw_width) or _absent(raw_height):
        raw_width, raw_height = row.get('width'), row.get('height')
    if _absent(raw_width) or _absent(raw_height):
        return None
    width, height = float(raw_width), float(raw_height)
    if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
        return None
    return int(width), int(height)


def load_label_metadata(sidewalk_server_fqdn, label_metadata_file):
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
    return fetch_cvMetadata_from_server(sidewalk_server_fqdn)


# ---------------------------------------------------------------------------
# Equirectangular units. Every conversion between angles and pixels in this module goes through the
# four functions below, and the anisotropy is written here and nowhere else.
#
# The confusion these exist to prevent is not hypothetical. On an equirectangular raster a fraction of
# WIDTH and a fraction of HEIGHT are different units - 1.0 of width is 360 deg of azimuth, 1.0 of
# height is 180 deg of elevation - and production panos are 2:1 (W = 2H), which makes degrees-per-pixel
# equal on the two axes and therefore makes a wrong axis look right. #78 records a live instance in
# label-latlng-estimation: a depth panel captioned "the same window" as the photo beside it was
# stretched by exactly 2, because the correction was written twice and the two factors cancelled.
# Nothing threw, and the figure was published.
#
# So the axis is in the name. `azimuth_deg_to_px(deg, pano_height)` is visibly the wrong call rather
# than an answer that is quietly half or double; and because these are the only place the constants
# 360 and 180 appear, a factor of two cannot be introduced at a call site at all.
#
# Not a projection. Cutting an axis-aligned window out of an equirectangular raster involves no
# reprojection - these are unit conversions along one axis each, which is what makes them this small.


def azimuth_deg_to_px(deg, pano_width):
    """Degrees of azimuth -> horizontal pixels on a pano `pano_width` wide (1.0 of width = 360 deg)."""
    return deg / 360.0 * pano_width


def azimuth_px_to_deg(px, pano_width):
    """Horizontal pixels -> degrees of azimuth. The exact inverse of `azimuth_deg_to_px`."""
    return px / pano_width * 360.0


def elevation_deg_to_px(deg, pano_height):
    """Degrees of elevation -> vertical pixels on a pano `pano_height` high (1.0 of height = 180 deg)."""
    return deg / 180.0 * pano_height


def elevation_px_to_deg(px, pano_height):
    """Vertical pixels -> degrees of elevation. The exact inverse of `elevation_deg_to_px`."""
    return px / pano_height * 180.0


def _reference_crop_size(ref_y_offset):
    """The 2013 regression evaluated in the 6656-px-high space it was fit in.

    Two experimentally determined steps, unchanged since sidewalk-cv-tools#2: a linear map from the
    label's offset above the horizon to a camera-to-label distance, then a power law from that distance
    to a crop size, clamped to [50, 1500] px. `ref_y_offset` is already in reference space - putting the
    conversion in the caller is what keeps this function honest about the one coordinate frame its
    constants mean anything in.

    :param ref_y_offset: pixels above the horizon, expressed on a 6656-px-high pano.
    :return: crop size in reference pixels.
    """
    distance = max(0.0, V1_DIST_INTERCEPT + V1_DIST_SLOPE * ref_y_offset)
    size = V1_SIZE_COEF * distance ** V1_SIZE_EXP if distance > 0 else 0.0
    if size > V1_SIZE_MAX or distance == 0:
        size = V1_SIZE_MAX
    if size < V1_SIZE_MIN:
        size = V1_SIZE_MIN
    return size


def predict_crop_size(pano_y, pano_height):
    """Resolution-normalised `predict_crop_size`: the size the regression asks for, in native pixels.

    The constants above were fit on GSV panoramas 6656 px high, and for a decade this function fed them
    native pixels from panos of any height - so the same ramp at the same place in the world asked for a
    different window depending only on how large the pano happened to be served. Measured on 2048-px
    panos the error is 1.97x. The fix is to convert into reference space, evaluate there (including the
    [50, 1500] clamp, which is also a reference-space quantity), and scale the answer back:

        ref_offset = (pano_height / 2 - pano_y) * (V1_REF_HEIGHT / pano_height)

    Upstream's own docstring says step 1 "converts pano_y to the old version of pano_y that we had when
    this alg was written" - that conversion is what was missing, so this is the faithful reading of the
    formula rather than a new one. Bit-identical to the old behaviour at pano_height == 6656.

    This is the sizing rule on its own. It is deliberately NOT what the cropper cuts: see
    crop_window_width, which scales and clamps it. Callers wanting the window want that one.

    :return: crop size in native pixels of this pano.
    """
    ref_offset = (pano_height / 2 - pano_y) * (V1_REF_HEIGHT / pano_height)
    return _reference_crop_size(ref_offset) * (pano_height / V1_REF_HEIGHT)


def crop_window_fov_deg(pano_y, pano_height):
    """The sizing rule as what it actually is: an ANGLE. Degrees of the sphere the window spans.

    Two steps, each one measured number from reports/2026-08-19-crop-sizing-v2.md:

    1. **Scale by CROP_SIZE_SCALE.** The regression predicts something close to the ramp's own extent,
       which as a crop reads as "too tight" - it is a size estimate, not a framing decision.
    2. **Clamp between CROP_MIN_FOV_DEG and CROP_MAX_FOV_DEG.** Clamping in degrees is what makes the
       rule resolution-independent; a fixed pixel clamp is the defect v2 exists to fix.

    Split out of crop_window_width (#78) so the angular quantity has a name and can be asserted on
    directly. Everything the rule decides happens here, in degrees; converting to this pano's pixels
    is one call to azimuth_deg_to_px in crop_window_width and nothing else.

    The conversion IN is the elevation one: predict_crop_size is a height-normalised length (its
    constants were fit on 6656-px-high panos and it scales by pano_height), so its pixels are vertical
    pixels and elevation_px_to_deg is the honest reading of them. The angle is then a span of the
    sphere, and crop_window_width turns it back into pixels on the axis the window actually lies
    along - the horizontal one - with azimuth_deg_to_px. Production panos are 2:1, where the two
    conversions agree to the bit, which is how the elevation form served as the width unnoticed until
    #106's review.

    :return: the window's angular span in degrees, in [CROP_MIN_FOV_DEG, CROP_MAX_FOV_DEG].
    """
    deg = elevation_px_to_deg(predict_crop_size(pano_y, pano_height) * CROP_SIZE_SCALE, pano_height)
    return min(max(deg, CROP_MIN_FOV_DEG), CROP_MAX_FOV_DEG)


def crop_window_width(pano_y, pano_width, pano_height):
    """The window width rule v2 actually cuts, in native pixels: crop_window_fov_deg as an azimuthal span.

    A width is horizontal, so the conversion is azimuth_deg_to_px against pano_width. The elevation
    form gives the same number on a 2:1 pano and half of it on a square one - the axis slip the unit
    primitives exist to make visible, and the one #106's review caught in this function. The angle
    itself comes from the regression through the elevation conversion; crop_window_fov_deg says why.

    The 3:2 window is cut by WIDTH (compute_crop_box derives the height), because the ramp against the
    window's width is what decides whether a crop reads as too tight.

    Not clamped to the pano here: compute_crop_box owns the "a window cannot exceed the image" cap,
    because that is a property of the image rather than of the rule, and keeping it there means the
    reported window is the one that was cut.
    """
    return azimuth_deg_to_px(crop_window_fov_deg(pano_y, pano_height), pano_width)


def compute_crop_box(pano_x, pano_y, crop_width, pano_width, pano_height):
    """Integer 3:2 crop window for an equirectangular pano: x wraps at the seam, y clamps by shifting.

    On an equirectangular pano, column 0 and column width are the same place in the world, so a window
    near either edge reaches across the seam (#47) - extract_crop pastes the two segments. The poles are
    NOT adjacent, so the window shifts vertically to stay inside rather than wrapping or zero-padding:
    no crop ever contains synthetic black, at the price of the label sitting off-centre vertically when
    it is within height/2 of the top or bottom edge.

    The window is 3:2 (CROP_ASPECT_W_OVER_H) and capped so it fits the pano on both axes at that shape:
    width is capped at pano_width AND at pano_height * 1.5, which is what keeps the derived height
    inside the image without silently changing the aspect. The width cap is load-bearing, not symmetry:
    a window wider than the pano makes extract_crop's second segment read past the far edge, where
    Pillow zero-fills - the #47 black, back again. Integers throughout: Pillow's float-box crop
    banker's-rounds each edge independently, which made output dimensions vary with the centre's parity.

    Callers must not re-derive `shifted` from pano_y: it is reported here, off the same rounding that
    produced `top`, so a second copy cannot drift out of step with the geometry it describes.

    This function does not validate pano_y. An out-of-frame y clamps to a pole and yields a window the
    label is not inside - bulk_extract_crops rejects those rows before they reach here. pano_x needs no
    such check: column 0 and column pano_width are the same place in the world, so the modulo below is
    the correct reading of any finite x.

    :param crop_width: requested window WIDTH in native pixels, per crop_window_width.
    :return: CropBox(left, top, width, height, shifted) - integers, 0 <= left < pano_width,
             0 <= top <= pano_height - height, and shifted True when the window moved to stay inside.
    """
    width = int(round(min(crop_width, pano_width, pano_height * CROP_ASPECT_W_OVER_H)))
    height = int(round(width / CROP_ASPECT_W_OVER_H))
    left = int(round(pano_x - width / 2)) % pano_width
    ideal_top = int(round(pano_y - height / 2))
    top = max(0, min(ideal_top, pano_height - height))
    return CropBox(left, top, width, height, top != ideal_top)


def label_position_in_crop(pano_x, pano_y, box, pano_width, scale=1.0):
    """Where the labelled pixel lands inside the crop cut at `box`. The registration, as a function.

    This is the inverse of compute_crop_box, and #78 is why it is a named function rather than three
    lines at the one call site that needed them. A crop is derived data whose only claim to be "about"
    a label is this mapping; the mapping was previously re-derived inside the --mark-label branch, so
    the code that DEMONSTRATED registration was also the only code that computed it, and a caption is
    not a test. Anything that wants to know where in a crop the click was - the mark, a consumer
    plotting the click, the regression test for #54's tilt correction - asks here.

    Three things it carries, each of which is wrong by default if a caller rolls its own:

    * **the seam.** `left` is normalized into [0, pano_width) and the window may run past the far edge,
      so a label at x = 20 in a window starting at 13000 is at 20 - 13000 + pano_width, not at -12980.
      The modulo is the correct reading of any finite x (column 0 and column pano_width are the same
      place in the world), which is also why x is never bounds-checked upstream.
    * **the vertical shift.** compute_crop_box slides a window that would run off a pole back inside,
      so the label is NOT at the crop's centre on those - `box.top`, not `pano_y - height / 2`. The
      point marks the label, not the middle of the picture.
    * **the storage rescale.** downscale_for_storage caps a wide window at CROP_MAX_STORED_WIDTH, so a
      position in cut-window pixels is not a position in the stored file. Pass
      `scale = stored_width / box.width` to get file pixels; the default 1.0 is cut-window pixels.

    Float, deliberately: the caller decides how to round, and a mark that rounds is not a measurement
    that rounds. Not bounds-checked either - a caller that hands in a label the window does not contain
    gets a position outside the crop, which is the honest answer and is what the out_of_frame preflight
    exists to prevent reaching here.

    :param box: the CropBox compute_crop_box returned for this label.
    :param scale: stored width / box.width, when asking about a downscaled crop.
    :return: (x, y) in pixels of the crop, as floats.
    """
    return (((pano_x - box.left) % pano_width) * scale, (pano_y - box.top) * scale)


def extract_crop(pano, left, top, width, height):
    """Extract the (left, top, width, height) window from an equirectangular pano, pasting two segments
    when the window crosses the seam."""
    pano_width = pano.size[0]
    if left + width <= pano_width:
        return pano.crop((left, top, left + width, top + height))
    out = Image.new(pano.mode, (width, height))
    first_width = pano_width - left
    out.paste(pano.crop((left, top, pano_width, top + height)), (0, 0))
    out.paste(pano.crop((0, top, width - first_width, top + height)), (first_width, 0))
    return out


def downscale_for_storage(crop):
    """Cap a cut window at CROP_MAX_STORED_WIDTH, never stretching one that is already narrower.

    The ramp inside a crop carries however many source pixels the imagery gave it, and no resampling
    adds more - so upscaling a narrow window to hit a fixed output size buys bytes and blur. Returned
    unchanged when it already fits, so the common far-field case does no work and loses nothing.
    """
    width, height = crop.size
    if width <= CROP_MAX_STORED_WIDTH:
        return crop
    scale = CROP_MAX_STORED_WIDTH / width
    return crop.resize((CROP_MAX_STORED_WIDTH, max(1, int(round(height * scale)))), Image.LANCZOS)


def write_rule_marker(destination_dir):
    """Record which sizing rule cut this crop store, in the store, and warn if it disagrees.

    A crop directory is derived data with no other provenance: a JPEG does not say what geometry
    produced it, and existing crops are the resume marker so they are never re-cut. That makes a MIXED
    store the ordinary consequence of upgrading the rule -- run against a store cut under v1 and the
    new crops are 3:2 while the old ones stay square, and the directory looks exactly like a
    consistent one to a consumer that trains on all of it.

    The version therefore has to live next to the crops rather than in a line of stdout that scrolls
    past on a cron run. A disagreement is a warning and not an error: the mixed store is a real thing
    an operator may be deliberately topping up, and refusing to run would strand it. What must not
    happen is that it goes unrecorded.

    :return: the rule version already on disk, or None if this is a fresh store.
    """
    path = os.path.join(destination_dir, CROP_RULE_MARKER)
    previous = None
    try:
        with open(path, encoding='utf-8') as f:
            previous = json.load(f).get('crop_rule_version')
    except (OSError, ValueError):
        pass

    if previous is not None and previous != CROP_RULE_VERSION:
        message = ("Crop store %s was cut under sizing rule %s and this run uses %s. Existing crops "
                   "are never re-cut, so this store now holds both geometries; delete it to re-cut "
                   "under %s." % (destination_dir, previous, CROP_RULE_VERSION, CROP_RULE_VERSION))
        print(message)
        logging.warning(message)

    with atomic_output_path(path) as tmp_path:
        with open(tmp_path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump({'crop_rule_version': CROP_RULE_VERSION,
                       'crop_size_scale': CROP_SIZE_SCALE,
                       'crop_min_fov_deg': CROP_MIN_FOV_DEG,
                       'crop_max_fov_deg': CROP_MAX_FOV_DEG,
                       'crop_aspect_w_over_h': CROP_ASPECT_W_OVER_H,
                       'crop_max_stored_width': CROP_MAX_STORED_WIDTH,
                       'previous_crop_rule_version': previous},
                      f, indent=1, sort_keys=True)
    return previous


def make_single_crop(pano, pano_x, pano_y, output_filename, draw_mark=False):
    """
    Makes a crop around the object of interest and saves it atomically.

    Geometry per compute_crop_box: x wraps at the equirectangular seam, y clamps by shifting, so the
    crop is real imagery edge to edge (#47).

    :param pano: an open PIL.Image, or a path to one. bulk_extract_crops opens each pano once and passes
                 the image (a 13312x6656 pano is ~250 MB decoded and a 16384x8192 one 384 MB; re-opening
                 per label decoded it once per label); the path form is kept for one-off use.
    :param pano_x: x-pixel of label on the GSV image
    :param pano_y: y-pixel of label on the GSV image
    :param output_filename: name of file for saving
    :param draw_mark: if a dot should be drawn at the label position in the crop
    :return: the CropBox that was cut, so the caller can count a de-centred (shifted) crop without
             recomputing the geometry.
    """
    close_after = False
    if not hasattr(pano, 'crop'):
        pano = Image.open(pano)
        close_after = True
    try:
        pano_width, pano_height = pano.size

        box = compute_crop_box(pano_x, pano_y, crop_window_width(pano_y, pano_width, pano_height),
                               pano_width, pano_height)
        cropped = downscale_for_storage(extract_crop(pano, box.left, box.top, box.width, box.height))

        if draw_mark:
            # Draw on the crop, never the source pano: the pano image is shared by every label on it, so a
            # mark on the source would leak this label's dot into its neighbours' crops. Where the dot
            # goes is label_position_in_crop's answer and not this branch's - the seam modulo and the
            # vertical shift are properties of the geometry, not of drawing. Drawn after the downscale
            # with the scale passed through, so the dot is a fixed size in the file rather than shrinking
            # with the window it happened to be cut from.
            draw = ImageDraw.Draw(cropped)
            r = 10
            centre_x, centre_y = label_position_in_crop(pano_x, pano_y, box, pano_width,
                                                        scale=cropped.size[0] / box.width)
            draw.ellipse((centre_x - r, centre_y - r, centre_x + r, centre_y + r), fill=128)

        # The crop file is its own resume marker (bulk_extract_crops skips existing ones), so a mid-write
        # crash must not leave a truncated .jpg the next run trusts - same contract as every write in
        # downloaders/. format= is explicit because the temp path ends in .part, not .jpg.
        with atomic_output_path(output_filename) as tmp_path:
            cropped.save(tmp_path, format='JPEG')
        return box
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

    NOTE for re-runs on an existing store: a crop already on disk is never re-cut, so a store cropped
    before the #47 seam fix keeps its black-padded crops. Delete them to pick the fix up.

    :return: counts dict. The disjoint outcomes reconcile, including on re-runs:

                 success + skipped_existing + missing_pano + dims_mismatch + out_of_frame + errors
                     == total

             shifted_vertically is NOT one of them - it annotates a success whose window had to move to
             stay inside the pano, so the crop exists but the label is off-centre in it. Adding a bucket
             here without adding it to that sum is exactly how the invariant went stale before;
             tests/test_crop_runner.py asserts the sum from the dict rather than from this docstring.
    """
    counts = {'total': len(labels_to_crop), 'success': 0, 'skipped_existing': 0,
              'missing_pano': 0, 'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0,
              'errors': 0}

    # Before any crop is cut, so a run that dies partway still leaves the store saying what it holds.
    os.makedirs(destination_dir, exist_ok=True)
    write_rule_marker(destination_dir)

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
            meta_dims = _metadata_dims(row)
        except (KeyError, TypeError, ValueError) as e:
            counts['errors'] += 1
            logging.warning("Skipping malformed label row %r: %s", row, e)
            continue
        labels_by_pano.setdefault(pano_id, []).append((pano_x, pano_y, label_type, label_id, meta_dims))

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
        # accepts (~250 MB for a 13312x6656 pano, 384 MB for a 16384x8192 one).
        try:
            for pano_x, pano_y, label_type, label_id, meta_dims in labels:
                processed += 1
                print("Cropping label %d of %d (pano %s)" % (processed, counts['total'], pano_id))

                # Store integrity: the metadata's pano dims describe the CURRENT pano, and the image on
                # disk was stitched to whatever /adminapi/panos reported when it was downloaded. A
                # disagreement means the store is stale relative to the metadata (or, on the Mapillary
                # path, that thumb_original_url served something other than the recorded size) - so the
                # stored pixel coordinates would land in the wrong frame. Loud skip, never silent poison.
                #
                # This does NOT catch a label whose pano_x/pano_y went stale under a pano that was
                # re-served at a new resolution: the dims field is a per-pano join and gets refreshed
                # along with the pano, so such a row presents perfectly consistent dims. Measured over
                # 438,410 labels / 172,790 panos, no pano carries two frames - see
                # reports/2026-08-10-crop-geometry-review.md. Separating those rows needs the POV replay,
                # not a dims comparison (#54).
                if meta_dims is not None and meta_dims != pano.size:
                    counts['dims_mismatch'] += 1
                    logging.warning(
                        "Label %d on pano %s: metadata says %dx%d but the stored image is %dx%d; "
                        "skipping rather than mis-centring the crop",
                        label_id, pano_id, meta_dims[0], meta_dims[1], pano.size[0], pano.size[1])
                    continue

                # A pano_y outside the image cannot be recovered: the poles are not adjacent, so
                # compute_crop_box clamps to one, and the result is clean imagery of a place the label is
                # not in - a quieter failure than the black bar it replaced, and one --mark-label cannot
                # even reveal (the dot lands off-crop). pano_x gets no such check on purpose: column 0 and
                # column width are the same place in the world, so any finite x is read correctly by the
                # seam modulo, and rows storing pano_x == pano_width crop fine.
                if not 0 <= pano_y < pano.size[1]:
                    counts['out_of_frame'] += 1
                    logging.warning(
                        "Label %d on pano %s: pano_y %s is outside the %dx%d image; skipping rather "
                        "than clamping it to a pole", label_id, pano_id, pano_y,
                        pano.size[0], pano.size[1])
                    continue

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
                    box = make_single_crop(pano, pano_x, pano_y, crop_destination,
                                           draw_mark=mark_label)
                except Exception as e:
                    counts['errors'] += 1
                    logging.warning("Failed to crop label %d on pano %s: %s", label_id, pano_id, e)
                    continue
                counts['success'] += 1
                if box.shifted:
                    # The crop is real imagery containing the label, but the label is not at its
                    # centre. Counted rather than merely logged: a consumer that assumes centring
                    # needs a number, and #54 wants it as a per-label covariate.
                    counts['shifted_vertically'] += 1
                    logging.info("Label %d on pano %s: window shifted to stay inside the pano "
                                 "(top=%d), so the label sits %d px from the crop's centre",
                                 label_id, pano_id, box.top,
                                 abs(int(pano_y - box.top - box.height / 2)))
                logging.info('%s.jpg %s %s %s', label_id, pano_id, pano_x, pano_y)
        finally:
            pano.close()

    print("Finished.")
    # Echoed here as well as written to <crop-dir>/crop_rule.json, because the summary is what an
    # operator reads and the marker is what a consumer reads. The marker is the one that matters: a
    # line of stdout scrolls past on a cron run, and a crop store carries no other provenance.
    print("Crop sizing rule %s (recorded in %s)." % (CROP_RULE_VERSION, CROP_RULE_MARKER))
    print("%d crops extracted, %d already existed, %d skipped because the panorama image was missing, "
          "%d skipped on a metadata/image dimension mismatch, %d skipped for a label position outside "
          "the image, %d errors, of %d labels total."
          % (counts['success'], counts['skipped_existing'], counts['missing_pano'],
             counts['dims_mismatch'], counts['out_of_frame'], counts['errors'], counts['total']))
    if counts['shifted_vertically']:
        print("%d of those crops were shifted to stay inside the pano, so their label is not at the "
              "crop's centre." % counts['shifted_vertically'])
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

    # crop.log lives next to the crops it describes, NOT the CWD (which under cron is wherever the process
    # happened to start - the DownloadRunner #49 lesson).
    configure_logging(os.path.join(args.o, 'crop.log'))

    raise_decompression_bomb_ceiling()

    counts = run(sidewalk_server_fqdn=args.d, label_metadata_file=args.f, gsv_pano_path=args.s,
                 crop_destination_path=args.o, mark_label=args.mark_label)
    return 1 if counts['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
