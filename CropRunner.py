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
import datetime
import io
import json
import logging
import logging.handlers
import math
import os
import re
import sys

import requests
from PIL import Image, ImageDraw
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import pano_pose
# Handed to pano_pose.resolve_pano_pose, so a --tilt-correction run never imports per pano (#193 review).
# Not a new dependency: downloaders/__init__.py imports gsv, so the line below loads it already.
from downloaders.gsv import DEPTH_ARTIFACT_SUFFIX
from downloaders.common import atomic_output_path, black_fraction, raise_decompression_bomb_ceiling  # noqa: F401

# raise_decompression_bomb_ceiling is imported, not defined here, and re-exported under this module's name so
# `CropRunner.raise_decompression_bomb_ceiling()` keeps working for reports/scripts/annotation_tiles.py and
# crop_sizing_v2.py. It moved to downloaders/common.py with #115, when DownloadRunner needed the same policy
# for the Mapillary display copy and CropRunner stopped being the only entry point that opens a 134 MP file.

# What the crop loop actually reads off every label row. Only the CSV intake enforces them up front (a
# header typo is one error naming the file, not a KeyError 200k labels in); the JSON/server intake passes
# every element of the array through as it came - json_to_list indexes nothing (#164) - and
# bulk_extract_crops counts a row missing any of them, or an element that is not an object at all, as one
# bad label.
REQUIRED_LABEL_COLUMNS = ('pano_id', 'pano_x', 'pano_y', 'label_id')

# The label's type arrives under one of two names, and a row needs exactly one of them (#123).
# cvMetadata served `label_type_id`, an int, until SidewalkWebpage#4103 replaced the label_type lookup
# table with a Postgres enum (released v11.11.0, 2026-09-02): the query now selects labelTypeName and
# every deployment serves `label_type`, a name like 'CurbRamp', in both JSON and CSV. Measured against
# sidewalk-sea 2026-09-18 - see samples/cvmetadata-seattle.csv, which is that response.
#
# Both are accepted rather than just the new one. Every archived export carries `label_type_id`, including
# the two documented -f examples in samples/, and a store is re-cut from whatever export produced it.
LABEL_TYPE_COLUMNS = ('label_type_id', 'label_type')

# The upstream enum, verbatim from SidewalkWebpage app/models/label/LabelTypeTable.scala. The id is what
# names the crop's output directory, so this map is the only thing standing between a name-serving endpoint
# and a store sharded by a string.
#
# 8/'Problem' is here although docs/api-fields.md's table skips it. "8 is skipped" is true of labels in the
# wild, not of the enum - and a map that omits a name the endpoint can emit turns a real row into an error.
LABEL_TYPE_IDS_BY_NAME = {
    'CurbRamp': 1,
    'NoCurbRamp': 2,
    'Obstacle': 3,
    'SurfaceProblem': 4,
    'Other': 5,
    'Occlusion': 6,
    'NoSidewalk': 7,
    'Problem': 8,
    'Crosswalk': 9,
    'Signal': 10,
}

# The same fact keyed the other way, for validating an id that arrived as an id. Derived, never written
# out a second time: two hand-maintained copies of one upstream enum is the drift this map exists to stop.
LABEL_TYPE_NAMES_BY_ID = {id_: name for name, id_ in LABEL_TYPE_IDS_BY_NAME.items()}

# The crop window compute_crop_box resolves. `shifted` rides along rather than being recomputed by
# callers: it is derived from the same rounding that produced `top`, so the two cannot drift apart.
CropBox = collections.namedtuple('CropBox', ['left', 'top', 'width', 'height', 'shifted'])

# Which sizing rule cut a crop store. Stamped into the run summary because crops are derived data with
# no other provenance on disk: a store cut under one rule and topped up under another is otherwise
# indistinguishable from a consistent one, and every consumer here trains on whole directories.
#
# This is the DEFAULT rule, and it stays v2 until switching is decided deliberately: v3 moves ~40% of the
# 658 gold ramps' windows by more than 10%, so flipping it means re-cutting every store whole (--force,
# #83), which belongs in the #84 recrop campaign. --sizing-rule selects between the rules below.
CROP_RULE_VERSION = 'v2'
CROP_RULE_VERSIONS = ('v2', 'v3')

# Which distance estimator each rule sizes from; recorded in crop_rule.json beside the version.
CROP_RULE_DISTANCE_ESTIMATOR = {'v2': 'linear-2013', 'v3': 'lle3-cotangent-blend'}

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

# ---------------------------------------------------------------------------
# Sizing rule v3 (opt-in, #32): the window is the angle a fixed span of world subtends at the label's
# distance, and the distance comes from the lle #3 cotangent blend instead of the 2013 linear line.
# See reports/2026-09-26-crop-sizing-v3.md. The clamps, the 3:2 shape and the storage cap are v2's.
#
# The lle #3 Stage 4 calibration (label-latlng-estimation data/modern-truth-summary.json ->
# final_coefficients), transcribed. reports/scripts/pov_replay.py carries the same three numbers and
# tests/test_crop_runner.py::TestBlendDistanceMatchesTheStudyPort pins the two equal on a grid.
# Production code must not import reports/scripts, which is why this is a copy and not an import.
V3_CAMERA_HEIGHT_M = 2.341219672825709
V3_BLEND_DEG = 11.25
V3_DIST_CAP_M = 50.0

# Frontal width of world, in metres, the window spans at the label's distance. The one fitted v3
# constant: the value whose pooled median fill on the 658 gold aprons equals rule v2's, so the two
# rules are compared at the same median crop and differ only in how the window tracks depression.
# Deliberately not CROP_SIZE_SCALE x an object width: two multiplicative constants are one degree of
# freedom, and 2 atan(W / 2d) is exact where scaling an angle is only its small-angle approximation.
# Fit 2026-09-26 (reports/data/2026-09-26-crop-sizing-v3.json -> selection.matched_context_width_m;
# the planning pilot's 5.8 reproduced exactly). The band-centre criterion (fill p50 nearest 0.36)
# gives 6.0; the report says why the matched value is the one shipped.
V3_CONTEXT_WIDTH_M = 5.8

# ---------------------------------------------------------------------------
# The tilt correction (opt-in, #191). A GSV label's stored pano_x/pano_y is in gravity-levelled pixels
# while the stored tiles are in the rig's own frame, so under --tilt-correction the crop centre moves
# to the rig pixel (pano_pose.corrected_pixel), by beta times the rig transform - beta scaling the pose.
# The window is still SIZED at the stored pano_y; the corrected point only positions it
# (docs/cropper.md, "The tilt correction (opt-in, #191)").
#
# Beta per POSE RECORD, keyed on PanoPose.source: the pose the correction reads, not the label's era. The
# #191 2026-10-01 fit (https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/191#issuecomment-5922041299)
# measured, on the same 3,518 pairs, beta about 0.88 under the xml pose and about 0.95 under the npz pose, so the
# era gap was the pose record's. The record is also always known when a correction is applied, where the era
# is unknown for every -d label (cvMetadata serves no time_created).
#
# Both stay 1.0 here, NOT the measured priors: endpoint C found the direction 79 : 0 and the #191 beta batch
# read beta = 1 in both arms, and choosing the default is its own decision (#197). The priors are named so
# whoever sets them knows where they came from; they are not applied. Also open: the per-crop provenance
# column (#196), which must land before the flag is turned on for any consumer store, and re-fitting the
# sizing rules on corrected coordinates (#186).
TILT_BETA_BY_POSE_SOURCE = {pano_pose.POSE_SOURCE_XML: 1.0, pano_pose.POSE_SOURCE_NPZ: 1.0}
TILT_POSE_SOURCES = (pano_pose.POSE_SOURCE_XML, pano_pose.POSE_SOURCE_NPZ)

# crop_rule.json's name for each pose record's beta; 0.0 when the correction is off (beta 0 is the identity).
TILT_BETA_MARKER_KEYS = {pano_pose.POSE_SOURCE_XML: 'tilt_beta_xml_pose',
                         pano_pose.POSE_SOURCE_NPZ: 'tilt_beta_npz_pose'}

# Written into the crop directory so a store says which rule cut it. See write_rule_marker.
CROP_RULE_MARKER = 'crop_rule.json'

# How far below the directory it is given refuse_production_crop_store looks. Two, because the production
# store's root keeps its captures two directories down (<city-id>/<LabelType>/crop_<id>.png) - so pointing
# -o at the root, at one city, or at one label type directory are all within reach - and no further,
# because each level is a directory listing and the store is a network mount. main() calls it on -o AND
# on the city store <-o>/<city>/ (#159): from -o a production city directory sits at depth 1, where the
# LabelType-name signal is not read, so one whose type directories are still empty is caught only there.
PRODUCTION_STORE_SCAN_DEPTH = 2

# main()'s exit status when it refuses the destination. Not 1, which means "some labels errored, and the
# next run retries them": this run never looked at a label, and re-running it changes nothing.
EXIT_REFUSED_DESTINATION = 3

# The fleet's city roster, which --city must name an active row of (#159): --city is a directory name under
# -o, so a well-formed typo would quietly open a new, empty store beside the real one. Read as a FILE with
# csv, never by importing log_analyzer (which imports pandas - no production module may), and never copied
# into a constant here, which would drift from the roster the analyzer and docs/ops.md keep. Read at parse
# time, so tests can point it elsewhere.
CITIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log_analyzer', 'cities.csv')

# The per-crop provenance manifest (#111), beside the marker: one row per crop, appended as it lands. A
# crop is a bare JPEG and every consumer of this store is an ML dataset, so where its pixels came from -
# and, for Mapillary and Panoramax imagery, under which licence - has to travel with it. See
# ProvenanceManifest.
PROVENANCE_MANIFEST = 'crop_provenance.csv'

# The label-row fields copied into the manifest verbatim when the row carries them, and written EMPTY when
# it does not - never inferred from anything else (the #99 rule). cvMetadata sends none of the three today
# (docs/api-fields.md); older CSV exports carry `source` and `copyright`. `copyright` is the app's own name
# for the producer credit (pano_data.copyright, which ImageryAttribution.line reads beside
# pano_data.license, SidewalkWebpage#5202), so a value that arrives under it needs no renaming here.
PROVENANCE_FIELDS = ('source', 'copyright', 'license')

PROVENANCE_COLUMNS = ('city', 'label_id', 'pano_id') + PROVENANCE_FIELDS + ('crop_rule_version',)

# The manifest's header before rows carried a city (#159 step 1), and where a manifest still under it is
# moved so the city-bearing rows never land beneath it. See set_aside_pre_city_manifest.
PRE_CITY_PROVENANCE_COLUMNS = ('label_id', 'pano_id') + PROVENANCE_FIELDS + ('crop_rule_version',)
PROVENANCE_MANIFEST_PRE_CITY = 'crop_provenance.pre-city.csv'
MANIFEST_PRE_CITY = 'provenance_manifest_pre_city'

# crop_rule.json's answer to "may the manifest be read as covering every crop here?" (#153 M3). Named for
# what it records - that no run has KNOWN of a crop without a row - because that is all a marker can
# record: coverage itself is the manifest's rows against the crops on disk. See write_rule_marker.
MANIFEST_NO_KNOWN_GAP = 'provenance_manifest_no_known_gap'

# ---------------------------------------------------------------------------
# The systemic-failure alarm (#136). When cvMetadata changed shape (#123) every label errored, and the
# run's bookkeeping was entirely correct about it: errors == total, the invariant reconciled, main()
# exited 1. What was wrong was the SHAPE of the signal - 260,000 identical per-row WARNINGs, where
# "everything failed" differs from "three labels failed" only in length. And CropRunner is hand-run
# rather than on cron, so the exit code reaches nobody by itself.
#
# The fraction of a run's labels that must have errored before the summary says so in its own words.
# Not a tuned number: a half is the point where errors stop being a minority outcome - more of the run
# failed than survived - which no per-row cause explains at scale. Two alternatives were considered and
# rejected. Firing only at 100% is tuned to the one incident we have seen and is defeated by a single
# label of noise: one missing pano among 260,000 failures would silence it. Firing at, say, 10% would
# put the alarm inside the range an ordinary bad night can reach - a corrupt slice of the store is a
# genuinely per-row fault, and the loop is built to survive exactly that.
SYSTEMIC_ERROR_FRACTION = 0.5

# The one string to grep for, in crop.log or in a terminal scrollback.
SYSTEMIC_FAILURE_BANNER = 'SYSTEMIC FAILURE'

# bulk_extract_crops' counts, beside 'total'. Every label lands in exactly one DISJOINT_OUTCOMES bucket, so
# those sum to total on every path; a COUNT_ANNOTATIONS entry qualifies a label already in one of them and
# is deliberately outside that sum - shifted_vertically and recut annotate a success, stale_kept a label
# under --force that left an old crop in place because the run did not write it: a dims_mismatch or
# out_of_frame skip (#153 m2), a missing_pano, the errors of a pano that cannot be opened (#153 final
# F3) or decoded, or a black_content withhold (#164). A new key goes in exactly one of the two, and
# tests/test_crop_runner.py asserts the dict holds nothing else. black_content is disjoint, not an
# annotation: a withheld label is not a success, and like dims_mismatch it is something the run refused
# to trust rather than an error it made. no_pose (#191) is disjoint for the same reason: under
# --tilt-correction a pano with no pose beside it is skipped, like missing_pano, not guessed.
DISJOINT_OUTCOMES = ('success', 'skipped_existing', 'missing_pano', 'dims_mismatch', 'out_of_frame',
                     'black_content', 'no_pose', 'errors')
COUNT_ANNOTATIONS = ('shifted_vertically', 'recut', 'stale_kept')

# The content check (#164): a cut window more than this fraction exactly-black (luma 0) is not written and
# is counted black_content. The #47 guarantee is about geometry - no crop contains synthetic black the
# cropper made - and says nothing about black the STORED pano already holds: a stitch that ran past what
# Google serves (#156's D4 shape, ~34% black along its right and bottom 19%) or a pre-#68 fallback. Those
# pass the stitcher's own guard (gsv.STITCH_MAX_BLACK_FRACTION, also 0.5 but of the whole pano), so a
# label inside the band was cut as a black crop and counted success.
#
# Why 0.5, measured on a 2048x1024 grey pano with its bottom 30% black, JPEG q95: under rule v2 a label at
# y=512 cuts 0.000 black, 650 -> 0.241, 700 -> 0.452, 716 -> 0.499, 730 -> 0.540, 900 (window shifted) ->
# 0.900; under v3 (#32), whose windows are narrower there, 0.000, 0.199, 0.441, 0.498, 0.540, 0.900.
# Inside a large black JPEG region luma is exactly 0, and ringing is confined to the rows next to the
# edge, so a label inside a band whose window is NOT shifted gives at least half black rows. A SHIFTED
# window gives the band's depth over the window's height instead, wherever the label sits: at the nadir
# the window is a third of the pano tall under BOTH rules (v3 hits the same CROP_MAX_FOV_DEG ceiling from
# ~39 deg of depression, and the shift starts at ~60 deg), so a bottom band is caught only when deeper than H/6
# (16.7%; the D4 band is 18.75%, 0.56 black), and a thinner one is written as success, up to half black
# (tests pin both; the table is in docs/cropper.md). A window more black than imagery is not imagery. Exact zero over half a
# window is also not a night scene or a black car: JPEG noise keeps those off 0. The strict `>` matches
# the stitcher; a label within the ringing margin of a band edge (y=716 above) is written, knowingly.
CROP_MAX_BLACK_FRACTION = 0.5

# ---------------------------------------------------------------------------
# Bounding crop.log under a systemic fault (#139). The crop loop logs one WARNING per failed label, which
# is right for the three bad labels of an ordinary run and ruinous for the #123 shape: ~260,000
# malformed rows, each warning carrying the whole row repr'd (~300-500 B), is ~100 MB through the
# 10 MB x 3 rotation. That rotates away every earlier run's history AND ~70% of the flood's own lines.
#
# Two bounds, both needed. The per-line one keeps an ordinary bad row's detail readable; the per-run
# one is what actually bounds the file, since even a 100-byte line times 260,000 is 26 MB.
#
# Lines of one KIND (a malformed row, a failed write, an unopenable pano, a dims mismatch, a label out
# of frame, a mostly black window, an unrecorded provenance row) logged per run before the rest are
# suppressed. Per kind, not shared, so a flood of one cannot silence the first lines of another, which
# may be the actual cause. Eight kinds (a pano with no pose, #191, is the eighth) x 100 lines x ~300 B
# is ~240 KB, a fiftieth of one rotation
# segment. Suppression is of
# LINES only: every label is still counted, which is what the summary, the #136 alarm and the exit
# code read.
LOG_WARNINGS_PER_KIND = 100

# The longest a repr'd label row, or an exception's text, may run inside one warning before it is
# clipped with '...'. An ordinary cvMetadata row repr's to ~300-500 B, so a whole ordinary row does not
# fit - the identifying fields are named separately at the front of the line for exactly that reason.
LOG_ROW_REPR_MAX_CHARS = 200

# The same, for one identifying field (label_id, pano_id) named at the front of the line.
LOG_ID_MAX_CHARS = 60


# A city_id as the app and log_analyzer/cities.csv spell it: lowercase ASCII words joined by single hyphens.
_CITY_ID = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')


def known_city_ids(path):
    """The active city_ids in a cities.csv-shaped roster: every non-blank city_id not commented out with '#'.

    To log_analyzer a '#' row means "not monitored"; here it means "not croppable". The two agree today
    (every '#' row is a deployment not scraped here, so it has no panos to crop), which is why one roster
    serves both - but commenting a city out to quiet the analyzer also stops it being cropped
    (docs/ops.md, Adding a city).

    :raises OSError: if the file cannot be read.
    :raises ValueError: if it has no city_id column.
    """
    with open(path, encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        if 'city_id' not in (reader.fieldnames or ()):
            raise ValueError("no city_id column")
        ids = set()
        for row in reader:
            city = (row.get('city_id') or '').strip()
            if city and not city.startswith('#'):
                ids.add(city)
        return ids


def city_id(value):
    """argparse type for --city. Strict, because the city is compared as a string - a city spelled two ways
    would be two cities - and because it is a path component (#159): <crop-dir>/<city>/.

    Well-formed first, then an active row of CITIES_FILE, read now. A roster that cannot be read fails
    closed (exit 2 naming it): accepting every well-formed name there is exactly the typo gap this closes."""
    if not _CITY_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("%r is not a city_id: lowercase letters and digits joined by single "
                                         "hyphens, as in log_analyzer/cities.csv (seattle-wa, cdmx)" % value)
    try:
        known = known_city_ids(CITIES_FILE)
    except (OSError, ValueError, csv.Error) as e:
        raise argparse.ArgumentTypeError("cannot read the city roster %s (%s), so cannot confirm %r is a city; "
                                         "nothing has been written" % (CITIES_FILE, e, value))
    if value not in known:
        raise argparse.ArgumentTypeError(
            "%r is not an active city in %s. --city names the crop store's directory, so a misspelling would "
            "start a new store; a city missing from the roster is added there first (docs/ops.md, Adding a "
            "city, step 3)" % (value, CITIES_FILE))
    return value


def build_parser():
    parser = argparse.ArgumentParser()
    group_parser = parser.add_mutually_exclusive_group(required=True)
    group_parser.add_argument('-d', help='sidewalk_server_domain (preferred over metadata_file) - FQDN of SidewalkWebpage server to fetch label list from, i.e. sidewalk-columbus.cs.washington.edu')
    group_parser.add_argument('-f', help='metadata_file - path to file containing label_ids and their properties. It may be CSV or JSON. i.e. samples/labeldata.csv')
    # Required, with no defaults (#52 item 6). -o used to default to the filesystem root and -s to the
    # Docker container's scratch path - and the container runs DownloadRunner, not this script. A forgotten
    # flag should name itself, not quietly put an ML training corpus somewhere nobody thinks to look.
    parser.add_argument('-s', required=True, help='pano_storage_directory - path to directory containing panoramas downloaded using DownloadRunner.py')
    parser.add_argument('-o', required=True, help='crop_output_directory - the root holding one crop store per city; this run writes into <crop-dir>/<city>/ only (#159)')
    parser.add_argument('--mark-label', action='store_true', help='Draw a dot at the label position in every crop. Debugging aid - deliberately OFF by default, because these crops are ML training data and a synthetic marker painted over the feature of interest is exactly what a model would learn instead of the feature.')
    parser.add_argument('--city', required=True, type=city_id, help="The city_id these labels belong to (seattle-wa, cdmx): an active row of log_analyzer/cities.csv, read at startup. Required: label_id restarts at 1 in every deployment, so crops are only unique per city. The first run records it in crop_rule.json, and a run naming a different city is refused before anything is cut, so no city can overwrite another city's crops (#159). Recorded on every provenance row.")
    parser.add_argument('--force', action='store_true', help='Re-cut a label whose crop already exists instead of skipping it (#83) - the repair for a store cut under an older sizing rule. Each crop is replaced atomically, so a failed write leaves the old one in place. For the ML crop store this tool writes ONLY; a destination that looks like the production canvas-capture store is refused either way.')
    parser.add_argument('--sizing-rule', choices=CROP_RULE_VERSIONS, default=CROP_RULE_VERSION,
                        help='Which crop sizing rule to cut with. v2 is the default, and has been since #88 (stores '
                             'cut before that are v1, #83). v3 sizes from the lle #3 cotangent distance instead of '
                             'the 2013 linear one (#32); it moves ~40%% of the 658 gold ramps\' windows by more '
                             'than 10%%, so a store cut under v2 should be re-cut whole with --force, not topped up. '
                             'Recorded in crop_rule.json and on every provenance row either way.')
    parser.add_argument('--tilt-correction', action='store_true',
                        help='Move each crop centre from the stored (gravity-levelled) label pixel to the rig '
                             'pixel of the stored tiles, on both axes, by beta per pose record (#191, #54). OFF by '
                             'default. Needs a pose beside each pano (<id>.xml for a 2019-22 scrape, else its '
                             '.depth.npz); a pano with neither is skipped as no_pose, never guessed. A corrected '
                             'crop is a different crop, recorded in crop_rule.json: re-cut a store whole with '
                             '--force rather than topping it up.')
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
    Dedupes on _label_id_key, keeping the first row per key - the same key json_to_list uses, the int the
    crop loop files under, so '7', '07' and ' 7' are one label and a row with no usable label_id (blank, or
    anything int() refuses) is never collapsed into another. Deduping on the raw cell let '7' and '07' both
    through to one <type>/7.jpg, cut twice under --force with two provenance rows (#170 ops-4). This is not
    the inference #72 removed: int() is applied explicitly to one column for the key only, and the row keeps
    its raw str. As in json_to_list, keep-first holds whether or not the first row is usable, so a malformed
    '7' row shadows a good '07' one (ops-7). The dropped rows are reported in one line (#183 M1).

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
    dropped = []
    with open(metadata_csv_path, newline='', encoding='utf-8-sig') as csv_file:
        reader = csv.DictReader(csv_file)
        # fieldnames is None for an empty file, and `c not in None` is a TypeError.
        fieldnames = reader.fieldnames or []
        missing = [c for c in REQUIRED_LABEL_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError("%s is missing required column(s) %r; found %r"
                             % (metadata_csv_path, missing, reader.fieldnames))
        # The type column is an either/or, so it cannot ride in the list above: naming 'label_type_id'
        # as missing would send a reader looking for a column no current deployment sends (#123).
        if not any(c in fieldnames for c in LABEL_TYPE_COLUMNS):
            raise ValueError("%s has none of the label type column(s) %r; found %r"
                             % (metadata_csv_path, list(LABEL_TYPE_COLUMNS), reader.fieldnames))
        for row in reader:
            # Surplus fields land under the key None. pandas did something worse with the same input -
            # it consumed the first column as the frame's index, shifting every field by one.
            label = {key: value for key, value in row.items() if key is not None}
            key = _label_id_key(label.get('label_id'))
            if key is not None:
                if key in unique_label_ids:
                    dropped.append(label.get('label_id'))
                    continue
                unique_label_ids.add(key)
            labels.append(label)
    _report_dropped_duplicates(dropped, metadata_csv_path)
    return labels


def _label_id_key(raw):
    """The dedupe key for a row's label_id, in both intakes: the int the crop loop will file its crop under, or None.

    The loop names a crop int(row['label_id']), so the key is that same int(): 1, "1", 1.0 and " 1" are one
    label, as are "07" and 7. Deduping on the raw value let 1 and "1" both through, and under --force both
    re-cut the same file (#164). None means "no usable id" - absent, null, blank, or something int() refuses
    - and such rows are never deduped against each other: each is its own bad label, counted once by the
    loop, and collapsing two would hide one.
    """
    if _absent(raw):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return None


# How many of the dropped duplicates' raw ids _report_dropped_duplicates names.
DUPLICATE_EXAMPLE_IDS = 5


def _report_dropped_duplicates(dropped, source):
    """One line, on stdout and in the log, for the rows an intake's dedupe dropped - or nothing if none.

    A dropped row leaves `total` smaller than the file, and a differently spelled id ('07' after '7') is
    the likeliest to be a real data conflict rather than an export artefact, so the drop must not be
    silent (#183 M1). One summary, never a line per row: json_to_list used to print "Duplicate label ID"
    per row on stdout only, unbounded on a duplicate-heavy payload - the flood #139 bounded in crop.log.
    Both channels, per the print/logging rule: stdout is what cron mails tonight, the log what is there
    next week.
    """
    if not dropped:
        return
    examples = ', '.join(_clip(repr(raw), LOG_ID_MAX_CHARS) for raw in dropped[:DUPLICATE_EXAMPLE_IDS])
    more = ', ...' if len(dropped) > DUPLICATE_EXAMPLE_IDS else ''
    message = ("%s: dropped %d rows as duplicate label_ids (the first row per id is kept), e.g. %s%s"
               % (source or 'label metadata', len(dropped), examples, more))
    logging.warning(message)
    print(message)


def json_to_list(jsondata, source=None):
    """
    Transforms json like object to a list of dict to be read in bulk_extract_crops() to crop panos with label metadata
    :param jsondata: json object containing label ids and their associated properties
    :param source: the file path or URL it came from, named in the refusal below
    :return: A list of dicts containing the following metadata: label_id, pano_id, label_type, agree_count,
    disagree_count, unsure_count, pano_width, pano_height, pano_x, pano_y, canvas_width, canvas_height, canvas_x,
    canvas_y, zoom, heading, pitch, camera_heading, camera_pitch, camera_roll
    :raises ValueError: the top level is not an array. A 200 carrying an error object would otherwise be
        iterated as its keys; one error naming the source, like the CSV intake's header check, is the
        honest answer.

    Nothing here indexes a row (#164): every element is passed through, and the crop loop counts one that
    is not an object, or lacks a field it needs, as one malformed row - the CSV intake's behaviour. Rows are
    deduped on _label_id_key, the int the loop files under - the CSV intake's key too since #170 ops-4.

    The dedupe keeps the FIRST row with a key, whether or not that row is usable, so a malformed row shadows
    a good one sharing its int: [{"label_id": 7, "pano_x": "bad"}, {"label_id": "7", ...}] is one error and
    no crop. Before #164 the raw keys 7 and "7" were distinct and the good row was cut; keep-first on a bad
    first row was already the rule for identical raw keys, so this only widens it (#170 review).

    Measured against sidewalk-sea 2026-09-18 (#123): `label_type` is a name and replaced the older
    `label_type_id`; `unsure_count` was documented here as `notsure_count`; `camera_roll` is served and
    was undocumented; `source` is NOT sent by this endpoint, despite the older exports in samples/ having
    a column by that name. Nor does it send `copyright` or `license`; the provenance manifest (#111)
    copies all three when a row carries them and writes them empty otherwise.
    """
    if not isinstance(jsondata, list):
        raise ValueError("%s: expected a JSON array of label rows, got %s: %s"
                         % (source or 'label metadata', type(jsondata).__name__,
                            _clip(repr(jsondata), LOG_ROW_REPR_MAX_CHARS)))
    unique_label_ids = set()
    label_info = []
    dropped = []

    for value in jsondata:
        key = _label_id_key(value.get('label_id') if isinstance(value, dict) else None)
        if key is None:
            label_info.append(value)
        elif key not in unique_label_ids:
            unique_label_ids.add(key)
            label_info.append(value)
        else:
            dropped.append(value.get('label_id'))
    _report_dropped_duplicates(dropped, source)
    return label_info


def fetch_cvMetadata_from_file(metadata_json_path):
    """
    Reads json file to extract labels.
    :param metadata_json_path: the path of the json file containing all label ids and their associated data.
    :return: A list of dicts, one per label (see json_to_list).
    """
    with open(metadata_json_path) as json_file:
        json_meta = json.load(json_file)
    return json_to_list(json_meta, source=metadata_json_path)


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
        # Inside the try (#164): a 200 whose body is not an array - an error object, say - is a failed
        # fetch like any other, not a traceback.
        return json_to_list(jsondata, source=url)
    except requests.exceptions.RequestException as e:
        logging.error('Fetching cvMetadata from %s failed: %s', url, e)
        print("Cannot fetch metadata from webserver. Check log file.")
        sys.exit(1)
    except ValueError as e:
        logging.error('cvMetadata from %s is not usable: %s', url, e)
        print("The webserver's metadata is not a list of labels. Check log file.")
        sys.exit(1)


def _absent(value):
    """True for a field the row does not actually carry: absent, JSON null, or a blank CSV cell.

    The blank cell is the case that has to be spelled out. '' is not None, so treating only None as
    missing would both skip the width/height fallback below AND hand '' to float(), turning a row that
    simply doesn't claim dimensions into a counted malformed-row error - and main() exits 1 on errors, so
    a blank dims column would fail an otherwise clean run.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def _exact_label_type_id(raw):
    """The integer a label type id cell states, exactly, or ValueError.

    int() is the obvious reading and is too permissive to sit behind a guarantee about which directory a
    crop lands in. Measured on the shipped inputs it accepted: 3.7 -> 3 (silent truncation), True -> 1,
    b'3' -> 3, '+3' -> 3, and '1_0' -> 10, because Python reads underscores as digit grouping. Every one
    of those lands on a REAL label type, so the enum-membership check downstream cannot see them: the row
    is filed under a type it never claimed, counted as a success, with nothing on disk to say so.

    A CSV cell is always a str and a JSON export gives int or float, so the accepted shapes are an exact
    int (never a bool - bool is an int subclass, and True would otherwise be Curb Ramp), a float that is
    exactly integral, and a string of plain ASCII digits. `str.isdigit()` is deliberately not used: it is
    True for superscripts and for other scripts' digits, which int() then happily converts.

    3.0 is accepted and 3.7 is not, which is the line worth drawing rather than rejecting every float: a
    JSON export that went through a layer typing its numbers as floats states the id exactly, and failing
    it would break real archived data for no safety gained, while a non-integral value is the truncation
    this exists to stop.
    """
    if isinstance(raw, bool):
        raise ValueError('label_type_id is a bool, not an id: %r' % (raw,))
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw.is_integer():
            return int(raw)
        raise ValueError('label_type_id is not a whole number: %r' % (raw,))
    if isinstance(raw, str):
        text = raw.strip()
        if text and all(c in '0123456789' for c in text):
            return int(text)
    raise ValueError('label_type_id is not a plain integer: %r' % (raw,))


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
# 360 and 180 appear (a test walks the token stream), a call site cannot write the conversion itself.
# What that guard does not see is a bare factor of two: `azimuth_deg_to_px(deg, 2 * pano_height)`
# passes it, and on a 2:1 pano is even correct. It is a tripwire for the common slip, not a proof -
# the axis in the name and the tests on a non-2:1 pano are what carry the rest.
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


def label_depression_deg(pano_y, pano_height):
    """Depression of the labelled pixel below the horizon, in degrees, positive down (0 at the horizon).

    The one place a stored pano_y becomes an angle under rule v3. A #54 tilt correction, once measured,
    is an addend to this value and nowhere else: blend_distance_m and geometric_window_fov_deg take the
    corrected angle unchanged. Exact for a gravity-aligned equirectangular pano, and resolution-free
    because it goes through the elevation primitive.

    >>> label_depression_deg(512, 1024)
    0.0
    """
    return elevation_px_to_deg(pano_y - pano_height / 2.0, pano_height)


def blend_distance_m(depression_deg, camera_height_m=None, blend_deg=V3_BLEND_DEG,
                     cap_m=V3_DIST_CAP_M):
    """Camera-to-label ground distance in metres: the lle #3 horizon-saturating cotangent blend.

    At depressions of `blend_deg` or more (the 11.25-degree ray and every steeper one, nearer the
    camera) it is plain trigonometry, camera_height / tan(depression). At shallower depressions - towards
    the horizon, where the cotangent diverges and a degree of click noise is metres of distance - it is
    the straight line matching the cotangent's value AND slope at the blend point, so the curve is smooth
    there and saturates at the horizon (23.85 m with the shipped calibration) rather than diverging. A
    label above the horizon gets the horizon's distance. Clipped to [0, cap_m]; with the shipped
    calibration the 50 m cap never binds, since the blend tops out at the horizon's 23.85 m.

    A scalar port of reports/scripts/pov_replay.predict_blend_distance, pinned equal to it by a test. The
    tail's slope is written with math.radians(1.0) - d(cot)/d(degree) - rather than a literal, because
    the equirectangular constants are allowed in this module only inside the four unit primitives.

    `camera_height_m=None` means V3_CAMERA_HEIGHT_M, read at call time rather than bound at definition,
    so a test that patches the module constant reaches this function.

    >>> round(blend_distance_m(45.0), 3)
    2.341
    """
    camera_height_m = V3_CAMERA_HEIGHT_M if camera_height_m is None else camera_height_m
    a_rad = math.radians(blend_deg)
    if depression_deg >= blend_deg:
        distance = camera_height_m / math.tan(math.radians(depression_deg))
    else:
        value_at_blend = camera_height_m / math.tan(a_rad)
        slope = -camera_height_m * math.radians(1.0) / math.sin(a_rad) ** 2
        distance = value_at_blend + slope * (max(depression_deg, 0.0) - blend_deg)
    return min(max(distance, 0.0), cap_m)


def geometric_window_fov_deg(distance_m, context_width_m=None):
    """Rule v3's window: the angle a `context_width_m` frontal span of world subtends at `distance_m`.

    2 atan(W / 2d), clamped to [CROP_MIN_FOV_DEG, CROP_MAX_FOV_DEG]; a distance of zero or less (the label
    is under the camera) is the ceiling. Takes a DISTANCE, not a pixel, so a measured distance - the depth
    artifact is the #32 follow-up - plugs in here without touching anything else.

    Two consequences of the shipped constants, pinned by tests: the floor is unreachable (the horizon
    saturates the distance, so the narrowest window is the horizon's), and the ceiling binds exactly
    where the distance falls to W / 2. `context_width_m=None` means V3_CONTEXT_WIDTH_M, read at call time
    (as blend_distance_m reads its camera height) so a patched module constant reaches it.
    """
    context_width_m = V3_CONTEXT_WIDTH_M if context_width_m is None else context_width_m
    if distance_m <= 0:
        return CROP_MAX_FOV_DEG
    deg = math.degrees(2.0 * math.atan(context_width_m / (2.0 * distance_m)))
    return min(max(deg, CROP_MIN_FOV_DEG), CROP_MAX_FOV_DEG)


def crop_window_fov_deg(pano_y, pano_height, sizing_rule=CROP_RULE_VERSION):
    """The sizing rule as what it actually is: an ANGLE. Degrees of the sphere the window spans.

    `sizing_rule` selects between the rules in CROP_RULE_VERSIONS; an unknown one is a ValueError.

    Rule v3 (opt-in, #32) is three named steps - label_depression_deg, blend_distance_m,
    geometric_window_fov_deg - composed here and nowhere else; a test pins the composition so a refactor
    that fuses them cannot land silently. Everything below describes rule v2, the default.

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
    if sizing_rule == 'v2':
        deg = elevation_px_to_deg(predict_crop_size(pano_y, pano_height) * CROP_SIZE_SCALE, pano_height)
        return min(max(deg, CROP_MIN_FOV_DEG), CROP_MAX_FOV_DEG)
    if sizing_rule == 'v3':
        return geometric_window_fov_deg(blend_distance_m(label_depression_deg(pano_y, pano_height)))
    raise ValueError("unknown sizing rule %r; expected one of %r" % (sizing_rule, CROP_RULE_VERSIONS))


def crop_window_width(pano_y, pano_width, pano_height, sizing_rule=CROP_RULE_VERSION):
    """The window width the selected rule cuts, in native pixels: crop_window_fov_deg as an azimuthal span.

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
    return azimuth_deg_to_px(crop_window_fov_deg(pano_y, pano_height, sizing_rule), pano_width)


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
      One scale for both axes, knowingly: downscale_for_storage rounds the stored height on its own,
      so the file's true y scale is stored_height / box.height, which differs from `scale` by at most
      half a pixel at the bottom edge. That is inside the mark's radius and beside the point for #54,
      whose comparison is in pano coordinates - but it is a rounding, not an exact inverse, and a
      consumer that wants the exact file row should scale y by the stored height itself.

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


class ProductionCropStoreError(Exception):
    """The crop destination looks like the production canvas-capture store.

    See refuse_production_crop_store."""


class CropStoreCityError(Exception):
    """The crop destination belongs to another city, or its marker cannot say which. See check_store_city."""


def check_store_city(destination_dir, city):
    """Refuse a crop store recorded as another city's, before anything in it is touched (#153, for #159).

    Crops are <label_type_id>/<label_id>.jpg inside a store and label_id restarts at 1 in every deployment,
    so two cities in one store collide on file names. Without --force the second city's label finds the
    first city's crop and counts it skipped_existing - wrong data reported as a success. With --force it
    REPLACES the first city's crop, and nothing records that it happened. #159 gives every city its own
    store, <crop-dir>/<city>/, which main() composes from --city; this is what still catches a store under
    the wrong name - one renamed or copied into another city's directory - and a caller below main() that
    hands bulk_extract_crops another city's store.

    Passes when there is nothing to disagree with: no store yet, no crop_rule.json yet, or a marker written
    before the city was recorded (the run then adopts `city` - write_rule_marker records it). Refuses when
    the recorded city differs, and when a marker exists but cannot be read or holds a city that is not a
    string: "unreadable" is not "no city", and adopting there would hand the store to whichever city ran
    next. The file is left for a person to look at.

    :raises CropStoreCityError: with a message naming both cities, or the unreadable marker.
    """
    path = os.path.join(destination_dir, CROP_RULE_MARKER)
    try:
        with open(path, encoding='utf-8') as f:
            marker = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        raise CropStoreCityError(
            "Cannot read %s (%s), so cannot confirm which city the crop store %s belongs to; refusing to cut "
            "into it as %s. Nothing has been written. Repair or restore the file - if the store is %s's, its "
            "'city' key should say so - then re-run." % (path, e, destination_dir, city, city)) from e
    recorded = marker.get('city') if isinstance(marker, dict) else None
    if not isinstance(marker, dict) or ('city' in marker and recorded is not None and not isinstance(recorded, str)):
        raise CropStoreCityError(
            "%s does not hold a readable city (%r), so cannot confirm which city the crop store %s belongs "
            "to; refusing to cut into it as %s. Nothing has been written. Repair the file, then re-run."
            % (path, marker, destination_dir, city))
    if recorded is not None and recorded != city:
        raise CropStoreCityError(
            "The crop store %s holds %s's crops (per %s) and this run is for %s. label_id restarts in every "
            "city, so cutting here would mix the two cities' crops under the same file names, and --force "
            "would overwrite %s's. Nothing has been written. Point -o at %s's own store (#159)."
            % (destination_dir, recorded, CROP_RULE_MARKER, city, recorded, city))


class CropStoreUnlistableError(OSError):
    """_store_holds_crops could not list the crop store or one of its shards (#153 m1, final F6).

    Its own class so main() can report exactly this - on both channels, exit 1 - without also catching
    every other OSError a run can raise."""


# Windows errors a directory listing raises for a directory this user cannot list that are NOT a
# PermissionError. ERROR_NOT_READY (21) already arrives as one - Python maps it to EACCES - but
# ERROR_CANT_ACCESS_FILE (1920), which some drive-root system directories give, arrives as a plain OSError
# (EINVAL). Both are listed so the intent does not rest on that mapping.
_UNLISTABLE_WINERRORS = frozenset({21, 1920})


def _is_unlistable(error):
    """Whether an OSError from listing a directory means "this user cannot list it", as opposed to an
    I/O fault, which both scanners let propagate as itself."""
    return isinstance(error, PermissionError) or getattr(error, 'winerror', None) in _UNLISTABLE_WINERRORS


# Compared case-folded: the store can sit on, or be copied through, a case-insensitive filesystem.
_LABEL_TYPE_NAMES_FOLDED = frozenset(name.casefold() for name in LABEL_TYPE_IDS_BY_NAME)


def _is_numeric_name(name):
    """A label-type shard's name: ASCII digits only. The one spelling both scanners use - the production
    guard to skip this tool's own shards, _store_holds_crops to find them - so they cannot disagree about
    which directories those are. isascii() first because str.isdigit() also accepts other scripts' digits
    and superscripts, none of which this tool ever writes."""
    return name.isascii() and name.isdigit()


def _is_label_type_shard(name):
    """A directory name this tool writes a shard under: str(label_type_id) for a type the enum has. Narrower
    than _is_numeric_name, which stays the scanners' test - a root holds no all-digit directory of its own
    either way - but the migrator moves only these, so a '2024' of figures or a hand-made '01' is never
    filed into a city's store as if it were crops (#159 review)."""
    return _is_numeric_name(name) and name == str(int(name)) and int(name) in LABEL_TYPE_NAMES_BY_ID


def _is_canvas_capture(name):
    """crop_<labelId>.png, the production store's file name. Our own crop_rule.json shares the prefix,
    which is why the extension is part of the test and the prefix alone is not."""
    folded = name.casefold()
    return folded.startswith('crop_') and folded.endswith('.png')


def refuse_production_crop_store(destination_dir):
    """Raise ProductionCropStoreError if destination_dir looks like the store SidewalkWebpage serves.

    There are two crop stores and only one of them is this tool's. SidewalkWebpage serves the Gallery,
    the label cards and the social preview from `<root>/<city-id>/<LabelType>/crop_<labelId>.png`: a
    canvas capture the BROWSER took at label time - the annotator's own viewport, zoom and the imagery
    Google served that day. It is not a function of anything we still hold, so none of it can be
    regenerated and a deleted one is gone. This tool writes `<crop-dir>/<city>/<label_type_id>/<label_id>.jpg`
    (#159), cut from the pano store and reproducible at will. #83's scope comment has the full comparison.

    Since #159 both layouts are city-first, so depth no longer separates them; the names do. Ours has
    all-digit type directories and <label_id>.jpg files, theirs LabelType-named directories and
    crop_<labelId>.png files. Scanned from -o, our city directories sit at depth 1 and their numeric
    shards are skipped, so a formula root passes; a production root is caught by its captures at depth 2.
    The one shape the root-level scan misses is a production city directory whose LabelType directories
    are still EMPTY - the name signal is read at depth 0 only - which is why main() scans the city store
    as well.

    The two layouts happen to be disjoint on every name component - a type NAME against a numeric id,
    a `crop_` prefix, .png against .jpg - so CropRunner pointed at the production store could not
    overwrite a capture today. That is a coincidence of naming, not a guard. It protects nothing
    against a re-cut campaign that deletes "the crop store" before re-cutting it, which is the actual
    risk --force invites, and it would stop protecting anything the day either side renames a file.

    So this REFUSES rather than warns. A warning is right for write_rule_marker's mixed store, which
    an operator may be topping up deliberately and can repair by re-cutting; nothing here is
    repairable, a warning scrolls past on a long run, and the mistake it would be warning about is
    made by the same operator who is not reading the output.

    Refused when either signal is present:

    * an immediate subdirectory named for a label type (LABEL_TYPE_IDS_BY_NAME, case-folded) rather
      than for a numeric id - catches -o at a city directory even before a capture exists in it;
    * a `crop_*.png` file in the destination or up to PRODUCTION_STORE_SCAN_DEPTH directories below
      it - catches -o at a label type directory (depth 0) and at the store root (depth 2).

    A directory named for a label type refuses even when it is EMPTY, deliberately: a city directory
    holds its type directories before it holds a single capture, and an ordinary folder that happens to
    be called `Other` or `Signal` is the accepted cost - the message names it, and renaming it is cheap.

    A directory the scan cannot LIST (a PermissionError: lost+found at an ext4 volume's root, System
    Volume Information at a Windows drive's, or its WinError 1920 - see _is_unlistable) is refused too,
    naming it (#153 m1), whether the listing fails to open or fails partway through (#153 final F6).
    What cannot be read
    cannot be ruled out, and skipping it would pass a store the guard never looked at; crashing, which
    it used to, was exit 1 with a traceback before logging existed.

    Cheap by construction, because a formula store is ~400k files on a network mount: bounded depth,
    os.scandir, an early exit on the first hit, and no descent into all-digit directories. Those are
    this tool's own type shards; the production layout never puts a capture in one, and listing them
    would make the guard the most expensive thing a re-run does. A destination that does not exist
    yet has nothing in it to protect and passes.

    Called before ANYTHING is written: by bulk_extract_crops before it creates the destination or
    writes the rule marker, and by main() - on -o and again on <-o>/<city>/ - before it creates the store
    or opens crop.log inside it.
    """
    pending = [(destination_dir, 0)]
    while pending:
        directory, depth = pending.pop()
        found = None
        # The whole listing is inside the try, not just the scandir call (#153 final F6): iterating it,
        # and an entry's is_dir(), can raise the same errors partway through.
        try:
            with os.scandir(directory) as listing:
                for entry in listing:
                    if entry.is_dir():
                        if depth == 0 and entry.name.casefold() in _LABEL_TYPE_NAMES_FOLDED:
                            found = ("a directory named for label type %r (this tool names them by "
                                     "numeric id)" % entry.name)
                            break
                        if depth < PRODUCTION_STORE_SCAN_DEPTH and not _is_numeric_name(entry.name):
                            pending.append((entry.path, depth + 1))
                    elif _is_canvas_capture(entry.name):
                        found = "a canvas capture, %s" % entry.path
                        break
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as e:
            if not _is_unlistable(e):
                raise
            raise ProductionCropStoreError(
                "Refusing to write crops into %s: %s cannot be read (%s), so it cannot be ruled out as "
                "part of the production crop store SidewalkWebpage serves, whose canvas captures cannot "
                "be regenerated. Point -o at a directory whose contents this user can list, or at a new "
                "directory." % (destination_dir, directory, e.strerror or e)) from e
        if found is not None:
            raise ProductionCropStoreError(
                "Refusing to write crops into %s: it holds %s, the layout of the production crop "
                "store SidewalkWebpage serves (<city-id>/<LabelType>/crop_<labelId>.png). Those "
                "are canvas captures taken at label time and cannot be regenerated. CropRunner "
                "writes <crop-dir>/<city>/<label_type_id>/<label_id>.jpg - point -o at a root of "
                "formula crop stores, or at a new directory." % (destination_dir, found))

def _store_holds_crops(destination_dir):
    """True if any label-type shard (<destination_dir>/<digits>/) holds a crop (a *.jpg).

    Stops at the first crop found, so on a populated store it costs a directory listing or two rather than
    a walk - which matters over sshfs, where a full walk of a city's ~400k crops is a round trip each. A
    `.jpg.part` is a write that never landed, and crop.log / crop_rule.json sit at the root, so neither is
    mistaken for one.

    Raises CropStoreUnlistableError, naming the directory, if the store or a shard cannot be listed
    (#153 m1) - by this user (see _is_unlistable), at the scandir call or partway through the listing:
    skipping it could record a store that already held crops as having no known gap, so the run stops
    here - before any crop is cut, like a manifest that cannot be opened. main() reports it on both
    channels and exits 1 (#153 final F6). Any other OSError propagates as itself.
    """
    try:
        with os.scandir(destination_dir) as listing:
            for entry in listing:
                if not (entry.is_dir() and _is_numeric_name(entry.name)):
                    continue
                with os.scandir(entry.path) as shard:
                    # This walks the CROP store, not the pano store, so walk_store_panos() is the wrong
                    # tool and its sidecar hazard does not exist here: crop shards hold <label_id>.jpg
                    # and, mid-write, <label_id>.jpg.part, never a .w8192.jpg. Any crop is enough - even
                    # a mis-named one would mean "the store already held crops", the only question asked.
                    if any(crop.name.endswith('.jpg') for crop in shard):
                        return True
    except OSError as e:
        if not _is_unlistable(e):
            raise
        raise CropStoreUnlistableError(
            e.errno, "Cannot list %s to tell whether crop store %s already holds crops, which the "
            "provenance record needs (%s); nothing has been cut. Make it readable, then re-run."
            % (e.filename, destination_dir, e.strerror)) from e
    return False


class LegacyCropStoreError(Exception):
    """-o is a crop store in the pre-#159 flat layout, not a root of per-city stores. See
    refuse_legacy_crop_root."""


# The store-level files a pre-#159 store keeps directly in -o. Any one of them there means -o IS a store.
# crop.log alone is not a signal: it is what any directory a run was pointed at could hold, and a root
# never writes one of its own - but the migrator moves it with the rest.
LEGACY_ROOT_FILES = (CROP_RULE_MARKER, PROVENANCE_MANIFEST, PROVENANCE_MANIFEST_PRE_CITY)


# The kinds of hit legacy_layout_signal reports. Only the first two make -o a pre-#159 store; the third is a
# directory a person put there, which the migrator leaves, so its remedy is different (final review M1).
LEGACY_LABEL_TYPE_DIRECTORY = 'label_type_directory'
LEGACY_STORE_FILE = 'store_file'
LEGACY_STRAY_DIGIT_DIRECTORY = 'stray_digit_directory'

LegacySignal = collections.namedtuple('LegacySignal', ['kind', 'description'])


def legacy_layout_signal(crop_dir):
    """What marks crop_dir as a pre-#159 flat store - a LegacySignal(kind, description) - or None.

    One listing of crop_dir, stopping at the first hit: an all-digit directory (a label-type shard - a
    root under the new layout holds only city directories, and no city_id is all digits, which
    tests/test_crop_store_layout.py pins against log_analyzer/cities.csv) or a LEGACY_ROOT_FILES file.
    An all-digit directory that is not a label type's (_is_label_type_shard) is still a signal, but of its
    own kind, LEGACY_STRAY_DIGIT_DIRECTORY: the migrator leaves it where it is, so a person has to move it.
    A crop_dir that does not exist yet holds nothing and gives no signal.
    """
    try:
        with os.scandir(crop_dir) as listing:
            for entry in listing:
                if entry.is_dir():
                    if _is_label_type_shard(entry.name):
                        return LegacySignal(LEGACY_LABEL_TYPE_DIRECTORY,
                                            "a label-type directory, %s" % entry.path)
                    if _is_numeric_name(entry.name):
                        return LegacySignal(LEGACY_STRAY_DIGIT_DIRECTORY,
                                            "an all-digit directory that is not a label type, %s" % entry.path)
                elif entry.name in LEGACY_ROOT_FILES:
                    return LegacySignal(LEGACY_STORE_FILE, "a crop store's own file, %s" % entry.path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return None


def city_named_directory(path, city):
    """The directory `path` names, if that directory is named `city` - so already <crop-dir>/<city>/ for -o
    its parent - else None.

    Compared after normalising (a trailing slash's basename is '', and 'x/1/..' is 'x') and again after
    resolving links, so `-o /srv/crops/current` pointing at seattle-wa/ counts. The spelled path wins when
    both match, so the parent named back to the operator is the one they typed.
    """
    normalized = os.path.normpath(os.path.abspath(path))
    if os.path.basename(normalized) == city:
        return normalized
    resolved = os.path.realpath(normalized)
    if os.path.basename(resolved) == city:
        return resolved
    return None


def refuse_legacy_crop_root(crop_dir, city):
    """Raise LegacyCropStoreError if crop_dir is a pre-#159 flat store rather than a root of city stores.

    Before #159, -o WAS the store: <crop-dir>/<label_type_id>/<label_id>.jpg, with crop_rule.json and
    crop_provenance.csv beside the shards. Taking such a directory as a root would start <crop-dir>/<city>/
    beside the old shards and cut every crop again - two copies of one city's store, with nothing to say
    which is current, and whatever other city's crops the flat store mixed in left where they were. So it
    is refused, --force included, and the message says what to run: migrate_crop_store.py, which moves
    the store under <city>/ without replacing anything, or - when -o is already named for the city, the
    README's form - nothing at all, since pointing -o at the parent makes it <crop-dir>/<city>/ as it is.

    A half-migrated root (some shards moved, some not) is refused too, correctly: a person decides each
    collision the migrator left, and cropping into the half that moved would hide the rest.

    Called by main() before the city store is created or crop.log opened, so the refusal writes nothing.
    """
    signal = legacy_layout_signal(crop_dir)
    if signal is None:
        return
    found = signal.description
    if signal.kind == LEGACY_STRAY_DIGIT_DIRECTORY:
        # Not evidence of a flat store: a root of city stores cannot hold it either (no city_id is all
        # digits), but the migrator leaves it where it is, so offering the migrator would end in LEFT and
        # exit 1 with nothing moved. The scan stops at the first hit, so whether the root is ALSO a pre-#159
        # store is not known yet; the re-run says, with its own remedy.
        raise LegacyCropStoreError(
            "%s holds %s. Since #159, -o holds only one store per city and no city_id is all digits, so the run "
            "refuses it; migrate_crop_store.py leaves such a directory where it is, so move it out of -o by "
            "hand, then re-run - if -o is also a flat crop store from before #159, the re-run says how to "
            "move that. Nothing has been written." % (crop_dir, found))
    command = "python3 migrate_crop_store.py %s --city %s --dry-run" % (crop_dir, city)
    named_for_city = city_named_directory(crop_dir, city)
    if named_for_city is not None:
        # The migrator is deliberately not offered here: it refuses a root named for --city (it would nest
        # the store as <city>/<city>/), and on a store several cities were cut into it would file every
        # crop under this one - it cannot tell one city's crop from another's.
        raise LegacyCropStoreError(
            "%s looks like %s's crop store already - it is named for the city and holds %s. Since #159, -o is "
            "the directory that HOLDS one store per city, so point -o at its parent, %s, and nothing moves. "
            "(If more than one city was ever cut into it, no tool here can separate them: see docs/cropper.md, "
            "One store, one city.) Nothing has been written." % (crop_dir, city, found,
                                                                   os.path.dirname(named_for_city)))
    raise LegacyCropStoreError(
        "%s holds %s: it is a crop store in the layout before #159, when -o was the store, not a root that "
        "holds one store per city (<crop-dir>/<city>/<label_type_id>/<label_id>.jpg). Cropping here would "
        "start a second copy of the store beside it. Move it into place first - it moves files, never "
        "replaces one, and lists anything it cannot move: `%s`, then the same without --dry-run. It files "
        "every crop under --city, so run it only on a store that holds %s's crops alone; for one that more "
        "than one city was cut into, see docs/cropper.md, One store, one city. Nothing has been written."
        % (crop_dir, found, command, city))


class ProvenanceManifestHeaderError(Exception):
    """The provenance manifest on disk carries a header this run must not append under. See
    set_aside_pre_city_manifest."""


# How much of the manifest's first line is read to judge its header. Either header is under 80 bytes; a
# first line longer than this is not one of them.
_MANIFEST_HEADER_READ_LIMIT = 4096


def set_aside_pre_city_manifest(destination_dir):
    """Check the manifest's header before anything is appended under it, and move a pre-city one aside.

    ProvenanceManifest writes a header only into an EMPTY file, so it trusts whatever header a non-empty
    one carries. When `city` became the first column (#153's stopgap for #159) that trust was wrong for
    every manifest already on disk: seven-field rows appended under the six-field header read with every
    column shifted by one, and nothing raised. So:

    * the current header (or no file, or an empty one, or a torn header with no newline at all - the
      manifest's own open rewrites that) passes untouched;
    * the pre-city header (PRE_CITY_PROVENANCE_COLUMNS) is MOVED, whole, to PROVENANCE_MANIFEST_PRE_CITY,
      and the run then starts a fresh manifest. Its rows are never rewritten or given a city: a root cut
      before #159 may hold more than one city's crops, and a city written onto those rows would be a guess;
    * anything else raises ProvenanceManifestHeaderError, and so does a pre-city manifest when the
      set-aside name is already taken - never replaced, since that would lose the older file.

    Called before write_rule_marker, which then finds no manifest and records a known gap if the store
    holds crops - which it will, since the set-aside rows described crops that are still on disk.

    :return: PROVENANCE_MANIFEST_PRE_CITY if a manifest was moved aside, else None.
    :raises ProvenanceManifestHeaderError: before anything is written.
    """
    path = os.path.join(destination_dir, PROVENANCE_MANIFEST)
    try:
        with open(path, 'rb') as f:
            first_line = f.readline(_MANIFEST_HEADER_READ_LIMIT)
    except OSError:
        # Absent: nothing to check. Unreadable: ProvenanceManifest's own open raises on it just after the
        # marker is written, before any crop - the existing failure for a store that cannot record provenance.
        return None
    if first_line == _csv_line(PROVENANCE_COLUMNS):
        return None
    if not first_line.endswith(b'\n') and len(first_line) < _MANIFEST_HEADER_READ_LIMIT:
        # Empty, or a header torn before its newline: nothing after it can be a row, and the open cuts it.
        return None
    if first_line != _csv_line(PRE_CITY_PROVENANCE_COLUMNS):
        raise ProvenanceManifestHeaderError(
            "%s begins with a header this run does not recognise (%r; expected %s). Appending under it "
            "would misfile every column, so nothing has been cut. Move the file aside or restore it, then "
            "re-run." % (path, first_line[:200], ','.join(PROVENANCE_COLUMNS)))
    aside = os.path.join(destination_dir, PROVENANCE_MANIFEST_PRE_CITY)
    if os.path.lexists(aside):
        raise ProvenanceManifestHeaderError(
            "%s still has the header written before rows carried a city, and %s already exists, so it "
            "cannot be set aside without replacing that file. Nothing has been cut. Merge or rename one of "
            "the two by hand, then re-run." % (path, aside))
    os.rename(path, aside)
    message = ("%s had the header written before rows carried a city; moved it, unchanged, to %s and "
               "started a new manifest. Crops listed only in the old file have no row in the new one."
               % (path, PROVENANCE_MANIFEST_PRE_CITY))
    print(message)
    logging.warning('%s', message)
    return PROVENANCE_MANIFEST_PRE_CITY


class ProvenanceManifest:
    """<crop-dir>/<city>/crop_provenance.csv, appended one row per crop as it lands (#111).

    The contract is the two nightly ledgers' (DownloadRunner's pano_id_log.csv, gsv's depth_log.csv): one
    handle held for the run and a row on disk per item, so a run killed at any point leaves a truthful
    partial file - a header, then rows in which every row describes a crop this tool cut, and a kill can
    leave at most the crop in flight without its row (a torn row is cut back at the next open and recorded
    as a gap). Append-only: a row is never rewritten, so every re-cut appends a row and the last one for a
    label describes the file.

    One CropRunner per store: nothing locks the manifest, and a second run's first open would cut this
    run's in-flight row as torn.

    **A row is written whole or not at all (#153 M2).** The handle is UNBUFFERED and each row is one
    encoded line handed to the OS, so there is no buffer in which a failed row can wait to be flushed by
    the next successful one - which is how rows the run reported as unrecorded used to turn up in the
    file. When an append fails the handle is dropped and reopened at once (#153 final F4), and reopening
    cuts the file back to its last complete line, so a write that failed partway cannot leave half a row
    in the middle of the file with whole ones after it, nor at the end of a run whose LAST append tore.
    If that reopen fails, the next row's record() reopens instead.

    **The repair cuts back; it never closes off (#153 n2).** A last line with no newline - a crash
    mid-append, or the partial write above - is truncated away rather than terminated with a '\\n': when
    the tear fell inside a quoted field (a comma in `copyright`), an appended newline sits inside the
    open quote and the csv reader swallows every following row into it. The torn row's crop is on disk
    and nothing can record it now, so losing the fragment loses nothing that was usable. `torn_rows_cut`
    counts the rows cut this way by the FIRST open - a previous run killed mid-append - which
    bulk_extract_crops reads as a known gap. A fragment cut by a reopen is this run's own failed append,
    already counted as unrecorded, so it is not counted again here. If nothing is left - a
    torn header - the header is written again, as for an empty file: the header is written when the
    file is EMPTY, not merely when it is new, since a crash between creating it and writing the header
    leaves zero bytes and "the file exists" would then skip the header for good.

    Opening it can raise, exactly like write_rule_marker's write just before it in the same directory:
    that is a store that cannot record provenance at all, and it fails before any crop is cut rather
    than producing crops with no record. A failed append for ONE crop raises out of record() and not out
    of the loop - bulk_extract_crops counts it - because by then the crop is already on disk.
    """

    def __init__(self, destination_dir, city=None, sizing_rule=CROP_RULE_VERSION):
        # The city goes on every row, so manifests concatenated across cities keep (city, label_id) as the key:
        # label_id alone restarts in every deployment. Empty only for a caller below main() that has none.
        self.city = '' if city is None else city
        # And the rule that cut the row's crop - the one THIS run cuts with (--sizing-rule, #32), not the
        # module default, since a v3 run's crops are not v2's.
        self.sizing_rule = sizing_rule
        self.path = os.path.join(destination_dir, PROVENANCE_MANIFEST)
        self._file = None
        self.torn_rows_cut = int(self._open(first=True))

    def _open(self, first=False):
        """Cut any torn tail back to the last complete line, then open for unbuffered appends.

        On the FIRST open, a torn row is recorded as a gap in crop_rule.json BEFORE it is cut (#153 final
        F1): the fragment is the only evidence that a crop lost its row, so recording it at the end of
        the run would lose it to anything that ends a run without a finally - SIGKILL, an OOM on a 384 MB
        decode, a SIGTERM (CropRunner installs no handler) - or to this open itself raising just after the
        cut. A record that cannot be written raises out of here with nothing cut, which stops the run
        before any crop: the torn row is then still on disk for the next run to find.

        :return: True if the first open cut a torn ROW (a torn header costs no crop its row)."""
        keep, cut_row = 0, False
        if os.path.exists(self.path):
            with open(self.path, 'r+b') as f:
                size = f.seek(0, os.SEEK_END)
                keep = _end_of_last_line(f, size)
                if keep != size:
                    # A torn HEADER (keep == 0) cost no crop its row; anything after a header did. A tear
                    # a later reopen finds is this run's own failed append, already counted unrecorded.
                    if keep and first:
                        _record_manifest_gap(os.path.dirname(self.path))
                        cut_row = True
                    f.truncate(keep)
        handle = open(self.path, 'ab', buffering=0)
        try:
            if not keep:
                _write_all(handle, _csv_line(PROVENANCE_COLUMNS))
        except BaseException:
            _close_quietly(handle)
            raise
        self._file = handle
        return cut_row

    def record(self, label_id, pano_id, provenance):
        """Append one crop's row. `provenance` is PROVENANCE_FIELDS' values, in order.

        Raises if the row did not reach the file; the handle is dropped first, so nothing of the row
        survives to be written later, and then reopened at once, which cuts any part of the row that did
        land (#153 final F4). Cutting here rather than at the next row's open means no run ends torn - a
        tear left by the run's LAST append used to be cut by the next run's first open and reported as a
        previous run killed mid-append, about a row this run had already reported as unrecorded. The
        reopen is best-effort: if it fails, the handle stays dropped and the next call reopens instead."""
        line = _csv_line((self.city, label_id, pano_id) + tuple(provenance) + (self.sizing_rule,))
        if self._file is None:
            self._open()
        try:
            _write_all(self._file, line)
        except BaseException:
            _close_quietly(self._file)
            self._file = None
            try:
                self._open()
            except Exception:
                pass
            raise

    def close(self):
        handle, self._file = self._file, None
        if handle is not None:
            handle.close()


def _csv_line(values):
    """One manifest row, encoded. '\\n', the ledgers' pin: csv.writer's excel default is '\\r\\n', which
    hands every grep a trailing carriage return on the last column."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator='\n').writerow(values)
    return buffer.getvalue().encode('utf-8')


def _write_all(handle, data):
    """Hand all of data to an unbuffered handle. A raw write may take only part of it; a zero or None
    return is treated as the failure it is rather than looped on."""
    view = memoryview(data)
    while view:
        written = handle.write(view)
        if not written:
            raise OSError("short write to %s" % PROVENANCE_MANIFEST)
        view = view[written:]


def _close_quietly(handle):
    """Close a handle already known to be failing. Its own error adds nothing to the one being raised."""
    try:
        handle.close()
    except Exception:
        pass


def _end_of_last_line(f, size):
    """The offset just past the last b'\\n' in the first `size` bytes of f, or 0 if there is none.

    Reads backwards in chunks, so the ordinary case - a file ending in '\\n' - costs one one-byte read,
    and a torn row costs one chunk, not a read of the whole manifest."""
    if not size:
        return 0
    f.seek(size - 1)
    if f.read(1) == b'\n':
        return size
    end = size
    while end > 0:
        start = max(0, end - 65536)
        f.seek(start)
        found = f.read(end - start).rfind(b'\n')
        if found >= 0:
            return start + found + 1
        end = start
    return 0


def _provenance_value(value):
    """A provenance field as the manifest writes it: the value as given, or '' when the row does not state
    one - absent, null, a blank cell, or a NaN from a caller that built its rows with pandas. Never a
    default: an empty cell is the honest record of 'not stated'."""
    if _absent(value) or (isinstance(value, float) and math.isnan(value)):
        return ''
    return str(value)


# Which recorded constants each rule actually reads, for write_rule_marker's same-id check. v3 does not
# use CROP_SIZE_SCALE and v2 uses none of the v3_* numbers, so a change to those is not a mixed store.
# The tilt correction (#191) moves every window whichever rule sizes it, so both rules read its betas.
_TILT_BETA_KEYS = tuple(TILT_BETA_MARKER_KEYS[source] for source in TILT_POSE_SOURCES)
RULE_MARKER_CONSTANT_KEYS = {
    'v2': ('crop_size_scale', 'crop_min_fov_deg', 'crop_max_fov_deg', 'crop_aspect_w_over_h',
           'crop_max_stored_width') + _TILT_BETA_KEYS,
    'v3': ('crop_min_fov_deg', 'crop_max_fov_deg', 'crop_aspect_w_over_h', 'crop_max_stored_width',
           'v3_camera_height_m', 'v3_blend_deg', 'v3_dist_cap_m', 'v3_context_width_m') + _TILT_BETA_KEYS,
}


# The marker fields _read_rule_marker type-checks: the rule ids and every recorded constant.
RULE_MARKER_SCALAR_KEYS = ('crop_rule_version', 'previous_crop_rule_version', 'distance_estimator',
                           'tilt_correction') + tuple(
    sorted(set(key for keys in RULE_MARKER_CONSTANT_KEYS.values() for key in keys)))


def _rule_constants(tilt_correction=False):
    """Every sizing constant, as crop_rule.json records it. Read at call time, not import time.

    Each pose record's tilt beta is TILT_BETA_BY_POSE_SOURCE's value when the correction is on and 0.0 when
    it is off:
    beta 0 is exactly the identity, so "off" and "beta 0" are one fact, and a store cut off-then-on is
    a same-rule constant change the existing check warns about."""
    constants = {TILT_BETA_MARKER_KEYS[source]: float(TILT_BETA_BY_POSE_SOURCE[source]) if tilt_correction
                 else 0.0 for source in TILT_POSE_SOURCES}
    constants.update({'crop_size_scale': CROP_SIZE_SCALE,
            'crop_min_fov_deg': CROP_MIN_FOV_DEG,
            'crop_max_fov_deg': CROP_MAX_FOV_DEG,
            'crop_aspect_w_over_h': CROP_ASPECT_W_OVER_H,
            'crop_max_stored_width': CROP_MAX_STORED_WIDTH,
            'v3_camera_height_m': V3_CAMERA_HEIGHT_M,
            'v3_blend_deg': V3_BLEND_DEG,
            'v3_dist_cap_m': V3_DIST_CAP_M,
            'v3_context_width_m': V3_CONTEXT_WIDTH_M})
    return constants


def write_rule_marker(destination_dir, force=False, city=None, sizing_rule=CROP_RULE_VERSION,
                      tilt_correction=False):
    """Record which sizing rule cut this crop store, in the store, and warn if it disagrees.

    `sizing_rule` is the rule THIS run cuts with, and it is what the marker records and what the
    disagreement check compares - not the module default, which says nothing about a --sizing-rule v3 run.
    Every rule's constants are written whichever one is selected, so the schema is one superset that
    older readers keep working on. Under the SAME rule id it also compares the constants that rule
    reads against the recorded ones and warns naming each that moved: a refit v3 still calls itself v3.

    A crop directory is derived data with no other provenance: a JPEG does not say what geometry
    produced it, and existing crops are the resume marker so they are not re-cut without --force. That
    makes a MIXED store the ordinary consequence of upgrading the rule -- run against a store cut under
    v1 and the new crops are 3:2 while the old ones stay square, and the directory looks exactly like a
    consistent one to a consumer that trains on all of it.

    The version therefore has to live next to the crops rather than in a line of stdout that scrolls
    past on a cron run. A disagreement is a warning and not an error: the mixed store is a real thing
    an operator may be deliberately topping up, and refusing to run would strand it. What must not
    happen is that it goes unrecorded.

    `force` changes only what that warning says (#153 m3). Without it the store is left mixed and the
    remedy is a --force run. With it, THIS run is the remedy: it re-cuts every label it reaches under the
    running rule, and the marker is rewritten now, at the start, so the store is not one geometry until
    the run finishes (and not even then for a label it skips - see bulk_extract_crops' stale_kept). A
    forced run used to be told to re-run with --force, and then report its own re-cuts.

    The history is STICKY (#157 review item 1). `rules_seen` lists every rule the store has been run
    under, in order of first use, and `constants_seen` every value each rule's constants have had; both
    are only ever appended to. A v2/v3 mix is 3:2 on both sides, so nothing on disk but this file can
    tell it apart from a clean store - and before these two keys, one warned run was enough to lose it:
    the rewrite named the new rule, and the next run compared against that and said nothing. So the
    warnings fire on every run whose rule or constants are not the only ones the store has seen, a
    --force run included: like the manifest flag below, a forced pass cannot tell from here that it
    reached every crop, so nothing it does clears the history. docs/cropper.md gives the manual reset.

    A marker that exists but cannot be read is provenance destroyed, not a fresh store: it is warned
    about on both channels, recorded as `unknown` (for good, in `rules_seen`), and its bytes are kept
    beside it as `crop_rule.json.unreadable-<UTC timestamp>` rather than overwritten. Under main() a
    marker that is not a JSON object never gets here - check_store_city refuses it, since it cannot say
    whose store it is - so what reaches this path there is an object whose rule fields are malformed.
    Its city and manifest keys are still carried forward: they are check_store_city's and the manifest's
    to judge, not the rule history's.

    It also says whether the provenance manifest (#111) has a KNOWN gap (MANIFEST_NO_KNOWN_GAP). The rule
    version cannot answer that - a v2 store cropped before the manifest existed and one cropped after both
    say v2 - and a crop cut before the manifest gets a row only if a --force pass re-cuts it. So when the
    manifest is about to be started (it is not on disk yet) this records the rule starting it and whether
    the store already held crops, and every later run carries those two answers forward: by then the
    crops on disk are the manifest's own, and re-deriving from them would call a whole manifest partial.
    A manifest on disk with no such record is `null` for both - unknown, not whole.

    The flag only ever goes from true to false (#153 M3). bulk_extract_crops turns it false through
    _record_manifest_gap when a run knows it left a crop without a row, and this carries the false
    forward like the true. Nothing turns it back, --force included: a forced pass cannot tell from here
    that it reached every row-less crop (one whose label is absent from its metadata keeps no row), and a
    re-derivation that could would be a walk of the whole store. So the flag is a record of what runs
    reported, never a coverage check - a run killed between a crop's rename and its row reports nothing.

    It records the city the store belongs to (check_store_city, which runs before this and refuses a
    different one). `city` None - a caller below main() that has none to give - carries the recorded city
    forward rather than erasing it, since erasing it would re-open the store to the next city.

    `tilt_correction` (#191) is recorded twice: each pose record's beta among the constants (0.0 when off - see
    _rule_constants), so the same-rule check warns about a store cut off-then-on and constants_seen keeps
    that history for good, and `tilt_correction: 'on' | 'off'` at top level for a reader. A marker written
    before these keys existed is read as beta 0.0 for every rule it names (_marker_history): that code never
    corrected, so a --tilt-correction top-up of such a store warns, unlike a constant merely unrecorded.

    :return: the rule version already on disk; 'unknown' if a marker exists but cannot be read; None
             if this is a fresh store.
    """
    if sizing_rule not in CROP_RULE_VERSIONS:
        raise ValueError("unknown sizing rule %r; expected one of %r" % (sizing_rule, CROP_RULE_VERSIONS))
    path = os.path.join(destination_dir, CROP_RULE_MARKER)
    # The raw object, for the keys that are not the rule history's: the city, and the manifest's record.
    marker = {}
    try:
        with open(path, encoding='utf-8') as f:
            marker = json.load(f)
    except (OSError, ValueError):
        pass
    if not isinstance(marker, dict):
        marker = {}
    if city is None and isinstance(marker.get('city'), str):
        city = marker['city']
    # Named once a pre-city manifest has been set aside (#159 step 1), and carried forward after that.
    pre_city = marker.get(MANIFEST_PRE_CITY)
    if os.path.exists(os.path.join(destination_dir, PROVENANCE_MANIFEST_PRE_CITY)):
        pre_city = PROVENANCE_MANIFEST_PRE_CITY

    if os.path.exists(os.path.join(destination_dir, PROVENANCE_MANIFEST)):
        manifest_started_under = marker.get('provenance_manifest_started_under')
        no_known_gap = marker.get(MANIFEST_NO_KNOWN_GAP)
    else:
        manifest_started_under = sizing_rule
        no_known_gap = not _store_holds_crops(destination_dir)

    running = _rule_constants(tilt_correction)
    recorded, unreadable = _read_rule_marker(path)
    if unreadable:
        kept = _keep_unreadable_marker(path)
        message = ("Crop store %s has a %s that could not be read, so the rule its existing crops were "
                   "cut under is unknown; it is recorded as 'unknown'%s. Check the store before "
                   "training on it." % (destination_dir, CROP_RULE_MARKER,
                                        ' and the unreadable file kept as %s' % kept if kept else ''))
        print(message)
        logging.warning(message)
        previous, rules_seen, constants_seen = 'unknown', ['unknown'], {}
    else:
        previous = recorded.get('crop_rule_version')
        rules_seen, constants_seen = _marker_history(recorded)

    # The remedy depends on --force (#153 m3): without it the store stays mixed and the remedy is a forced
    # run; with it, this run is the remedy, and the store is not one rule until it finishes. The plain form
    # also names the manual reset: the history is sticky through --force, so after a whole forced re-cut
    # this warning still fires, and "re-run with --force" alone is a loop no forced pass can leave.
    reset = (" rules_seen only grows, so a store since re-cut whole under %s (--force) reads the same way and "
             "the marker cannot tell: once a whole-city re-cut is done, remove rules_seen, constants_seen and "
             "previous_crop_rule_version from %s (docs/cropper.md, 'The reset')." % (sizing_rule,
                                                                                    CROP_RULE_MARKER))
    others = [rule for rule in rules_seen if rule != sizing_rule]
    if others and not unreadable:
        mixed = ("Crop store %s was cut under sizing rule %s and this run uses %s (%s rules_seen: %s). "
                 % (destination_dir, ' and '.join(others), sizing_rule, CROP_RULE_MARKER,
                    ', '.join(rules_seen)))
        if force:
            message = mixed + ("This run is re-cutting every label it reaches under %s (--force), and %s is "
                               "rewritten now to name %s: until the run finishes, crops it has not reached "
                               "are still %s, so finish the run before training on the store."
                               % (sizing_rule, CROP_RULE_MARKER, sizing_rule, ' or '.join(others)))
        else:
            message = mixed + ("Existing crops are re-cut only under --force, so without it the ones cut "
                               "under %s keep that geometry and any crop this run cuts is %s beside them. "
                               "To make the store one rule it has to be re-cut under this run's constants: "
                               "re-run with --force." % (' and '.join(others), sizing_rule) + reset)
        print(message)
        logging.warning(message)

    # Same rule id, and a rule is only as fixed as its constants: a refit v3 still calls itself v3.
    # Only the constants THIS rule reads are compared, and only values the store has recorded - a marker
    # from before the constants were written is silent rather than a false alarm. The tilt betas are the
    # exception: their pre-#191 value is known (0.0), and _marker_history records it.
    seen_for_rule = constants_seen.get(sizing_rule, {})
    changed = ['%s=%r and this run uses %r' % (key, value, running[key])
               for key in RULE_MARKER_CONSTANT_KEYS[sizing_rule]
               for value in seen_for_rule.get(key, [])
               if value != running[key]]
    if changed:
        mixed = "Crop store %s was cut under sizing rule %s with %s. " % (destination_dir, sizing_rule,
                                                                          '; '.join(changed))
        if force:
            message = mixed + ("This run is re-cutting every label it reaches with this run's (--force): "
                               "until the run finishes, crops it has not reached keep the other value, so "
                               "finish the run before training on the store.")
        else:
            message = mixed + ("Existing crops are re-cut only under --force, so the ones cut under the "
                               "other value keep it and any crop this run cuts uses this run's. To make the "
                               "store one rule it has to be re-cut under this run's constants: re-run "
                               "with --force." + reset)
        print(message)
        logging.warning(message)

    if sizing_rule not in rules_seen:
        rules_seen.append(sizing_rule)
    for key in RULE_MARKER_CONSTANT_KEYS[sizing_rule]:
        values = constants_seen.setdefault(sizing_rule, {}).setdefault(key, [])
        if running[key] not in values:
            values.append(running[key])

    with atomic_output_path(path) as tmp_path:
        with open(tmp_path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(dict(running,
                           crop_rule_version=sizing_rule,
                           distance_estimator=CROP_RULE_DISTANCE_ESTIMATOR[sizing_rule],
                           tilt_correction='on' if tilt_correction else 'off',
                           previous_crop_rule_version=previous,
                           rules_seen=rules_seen,
                           constants_seen=constants_seen,
                           city=city,
                           provenance_manifest=PROVENANCE_MANIFEST,
                           provenance_manifest_started_under=manifest_started_under,
                           **{MANIFEST_NO_KNOWN_GAP: no_known_gap, MANIFEST_PRE_CITY: pre_city}),
                      f, indent=1, sort_keys=True)
    return previous


def _read_rule_marker(path):
    """(marker dict, unreadable?) for crop_rule.json. Absent is ({}, False); present but not a marker
    this code can read - bad JSON, not an object, a malformed history, or a rule field
    (RULE_MARKER_SCALAR_KEYS) that is not a string, a number or null (a rule id of [] would otherwise
    raise TypeError looking up its constants) - is ({}, True).

    Only the rule's own fields are type-checked. The marker also carries #111's and #159's keys, and
    one of them, MANIFEST_NO_KNOWN_GAP, is a JSON bool by design: checking every field, as this did
    before the two met, called every marker a manifest-era run had written unreadable."""
    try:
        with open(path, encoding='utf-8') as f:
            recorded = json.load(f)
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return {}, True
    if not isinstance(recorded, dict):
        return {}, True
    rules_seen = recorded.get('rules_seen', [])
    constants_seen = recorded.get('constants_seen', {})
    if not (isinstance(rules_seen, list) and all(isinstance(rule, str) for rule in rules_seen)
            and isinstance(constants_seen, dict)
            and all(isinstance(keys, dict) and all(isinstance(values, list) for values in keys.values())
                    for keys in constants_seen.values())):
        return {}, True
    if not all(value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool))
               for key, value in recorded.items() if key in RULE_MARKER_SCALAR_KEYS):
        return {}, True
    return recorded, False


def _marker_history(recorded):
    """The store's (rules_seen, constants_seen), seeded from what the marker names at top level.

    The top-level keys are the LAST run's rule and constants, so they belong to the history whatever it
    says: that seeds a marker written before the history existed (whose crop_rule_version and
    previous_crop_rule_version are all it can say), and it means a hand-edited constant is compared.
    """
    rules_seen = list(recorded.get('rules_seen', []))
    constants_seen = {rule: {key: list(values) for key, values in keys.items()}
                      for rule, keys in recorded.get('constants_seen', {}).items()}
    for rule in (recorded.get('previous_crop_rule_version'), recorded.get('crop_rule_version')):
        if isinstance(rule, str) and rule not in rules_seen:
            rules_seen.append(rule)
    last = recorded.get('crop_rule_version')
    for key in RULE_MARKER_CONSTANT_KEYS.get(last, ()):
        if key in recorded:
            values = constants_seen.setdefault(last, {}).setdefault(key, [])
            if recorded[key] not in values:
                values.append(recorded[key])
    # A rule the store was cut under with no tilt beta recorded was run by code from before the tilt
    # correction (#191), which never corrected: its crops are beta 0, a recorded fact rather than a guess.
    # Seeded so the first --tilt-correction top-up of such a store warns that it is mixed, and so the sticky
    # history keeps the 0.0 (#193 review finding 1) - without it, every store that predates the flag read as
    # all corrected after one top-up.
    for rule in rules_seen:
        if rule in RULE_MARKER_CONSTANT_KEYS:
            seen = constants_seen.setdefault(rule, {})
            for key in _TILT_BETA_KEYS:
                if not seen.get(key):
                    seen[key] = [0.0]
    return rules_seen, constants_seen


def _keep_unreadable_marker(path):
    """Move an unreadable marker aside as `<path>.unreadable-<UTC timestamp>`; return the new name, or
    None if it could not be moved (the rewrite that follows then replaces it, as it always did)."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    kept = '%s.unreadable-%s' % (path, stamp)
    suffix = 0
    while os.path.exists(kept):
        suffix += 1
        kept = '%s.unreadable-%s-%d' % (path, stamp, suffix)
    try:
        os.replace(path, kept)
    except OSError as e:
        logging.warning("Could not keep unreadable %s aside: %s", path, e)
        return None
    return os.path.basename(kept)


def _record_manifest_gap(destination_dir):
    """Turn crop_rule.json's MANIFEST_NO_KNOWN_GAP false, keeping every other key (#153 M3).

    Called at the end of a run that knows it left a crop without a row, and by the manifest's first open
    just before it cuts a previous run's torn row (#153 final F1). Rewritten atomically like the
    marker itself.

    An ABSENT marker is rebuilt around the one key: there is nothing on disk to lose, and
    write_rule_marker restores the rule keys next run. A marker that exists but cannot be read - a
    transient read error, unparseable JSON, JSON that is not an object - is NOT overwritten, and this
    raises instead (#153 final F2): rebuilding it around the one key threw away every constant,
    crop_rule_version, provenance_manifest_started_under (for good, since the next run carries it forward)
    and previous_crop_rule_version (so a real rule change got no mixed-store warning).

    Raises on a failed read or write - the caller reports it, because the crops are fine and it is the
    RECORD of the gap that did not land.
    """
    path = os.path.join(destination_dir, CROP_RULE_MARKER)
    try:
        with open(path, encoding='utf-8') as f:
            marker = json.load(f)
    except FileNotFoundError:
        marker = {}
    if not isinstance(marker, dict):
        raise ValueError("%s holds %s, not a JSON object; left as it is"
                         % (CROP_RULE_MARKER, type(marker).__name__))
    marker[MANIFEST_NO_KNOWN_GAP] = False
    with atomic_output_path(path) as tmp_path:
        with open(tmp_path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(marker, f, indent=1, sort_keys=True)


class CropWindowMostlyBlackError(Exception):
    """The window about to be written is more than CROP_MAX_BLACK_FRACTION exactly-black (#164).

    Raised by make_single_crop before anything is written; bulk_extract_crops counts it black_content.
    `fraction` is the measured share, `box` the CropBox that was cut."""

    def __init__(self, fraction, box):
        super().__init__("%.1f%% of the cut window is black (limit %.0f%%)"
                         % (100 * fraction, 100 * CROP_MAX_BLACK_FRACTION))
        self.fraction = fraction
        self.box = box


def make_single_crop(pano, pano_x, pano_y, output_filename, draw_mark=False, sizing_rule=CROP_RULE_VERSION,
                     centre=None):
    """
    Makes a crop around the object of interest and saves it atomically.

    Geometry per compute_crop_box: x wraps at the equirectangular seam, y clamps by shifting, so the
    crop is real imagery edge to edge (#47).

    Geometry is not content (#164): a window can be edge to edge inside the pano and still be black,
    because the STORED pano holds black where Google served nothing. So the window is judged as
    extract_crop returns it - before the storage downscale and before the mark - and if more than
    CROP_MAX_BLACK_FRACTION of it is exactly black, CropWindowMostlyBlackError is raised and nothing is
    written. This runs only on a crop about to be written: bulk_extract_crops never reaches here for a
    crop already on disk without --force, so crops cut before the check are never re-judged.

    :param pano: an open PIL.Image, or a path to one. bulk_extract_crops opens each pano once and passes
                 the image (a 13312x6656 pano is ~250 MB decoded and a 16384x8192 one 384 MB; re-opening
                 per label decoded it once per label); the path form is kept for one-off use.
    :param pano_x: x-pixel of label on the GSV image
    :param pano_y: y-pixel of label on the GSV image
    :param output_filename: name of file for saving
    :param draw_mark: if a dot should be drawn at the label position in the crop
    :param sizing_rule: which rule in CROP_RULE_VERSIONS sizes the window (default v2). The content check
                        below runs on the window whichever rule sized it.
    :param centre: (x, y) to centre the window on - the tilt-corrected point (#191) - instead of
                   (pano_x, pano_y). The window is still SIZED at the stored pano_y, and the mark goes at
                   the centre, which is where the label is in the stored tiles.
    :return: the CropBox that was cut, so the caller can count a de-centred (shifted) crop without
             recomputing the geometry.
    :raises CropWindowMostlyBlackError: the window is mostly black; nothing was written.
    """
    close_after = False
    if not hasattr(pano, 'crop'):
        pano = Image.open(pano)
        close_after = True
    try:
        pano_width, pano_height = pano.size

        # Sized at the stored pano_y, never the corrected one (#191): v2 was fit on stored coordinates, and
        # v3's depression is below the gravity horizon, which is what the stored y encodes.
        centre_x, centre_y = (pano_x, pano_y) if centre is None else centre
        box = compute_crop_box(centre_x, centre_y,
                               crop_window_width(pano_y, pano_width, pano_height, sizing_rule),
                               pano_width, pano_height)
        window = extract_crop(pano, box.left, box.top, box.width, box.height)
        fraction = black_fraction(window)
        if fraction > CROP_MAX_BLACK_FRACTION:
            raise CropWindowMostlyBlackError(fraction, box)
        cropped = downscale_for_storage(window)

        if draw_mark:
            # Draw on the crop, never the source pano: the pano image is shared by every label on it, so a
            # mark on the source would leak this label's dot into its neighbours' crops. Where the dot
            # goes is label_position_in_crop's answer and not this branch's - the seam modulo and the
            # vertical shift are properties of the geometry, not of drawing. Drawn after the downscale
            # with the scale passed through, so the dot is a fixed size in the file rather than shrinking
            # with the window it happened to be cut from.
            draw = ImageDraw.Draw(cropped)
            r = 10
            mark_x, mark_y = label_position_in_crop(centre_x, centre_y, box, pano_width,
                                                    scale=cropped.size[0] / box.width)
            draw.ellipse((mark_x - r, mark_y - r, mark_x + r, mark_y + r), fill=128)

        # The crop file is its own resume marker (bulk_extract_crops skips existing ones), so a mid-write
        # crash must not leave a truncated .jpg the next run trusts - same contract as every write in
        # downloaders/. Under --force (#83) the same rename is what keeps the crop being replaced whole
        # until its successor is finished. format= is explicit because the temp path ends in .part, not .jpg.
        with atomic_output_path(output_filename) as tmp_path:
            cropped.save(tmp_path, format='JPEG')
        return box
    finally:
        if close_after:
            pano.close()


def resolve_label_type_id(row):
    """The numeric label type id for a row, from either field name (#123).

    `label_type_id` wins when the row carries a usable one, because an export that has it is the older
    shape and its id is authoritative; `label_type` is the name every current deployment serves and is
    mapped through LABEL_TYPE_IDS_BY_NAME. A blank cell counts as absent, the _absent() rule, so a row
    carrying both columns with an empty id still resolves off the name rather than dying on int('').

    Raises ValueError naming the value for an unrecognised name OR an unrecognised id, and KeyError when
    the row has neither column - all of which bulk_extract_crops already counts as one malformed row.
    Deliberately not a silent default: a label type this map has never heard of means the enum moved
    upstream, and filing those crops under a guessed id would poison a training directory with no way to
    tell afterwards.

    The id path is checked against the same enum as the name path, and that symmetry is the point. An
    unchecked int() accepted 99, 0 and -3 as label types and wrote <crop-dir>/99/ as a success, which is
    the identical poisoning this function's name path refuses - a guarantee that held on one half of the
    input space was worse than no guarantee, because the docstring claimed both.

    The id is read STRICTLY, by `_exact_label_type_id`, because int() is far more permissive than the
    docstring's "checked against the same enum" implies: it silently accepted 3.7 as 3, True as 1, '+3'
    and b'3' as 3, and - the one that matters - '1_0' as 10, Python's underscore digit grouping turning a
    plausible cell into Pedestrian Signal with nothing raised. Membership in the enum cannot catch those,
    because every one of them lands on a REAL id.

    >>> resolve_label_type_id({'label_type': 'SurfaceProblem'})
    4
    >>> resolve_label_type_id({'label_type_id': '1', 'label_type': 'CurbRamp'})
    1
    """
    if not _absent(row.get('label_type_id')):
        raw = row['label_type_id']
        # No try/except around this call: _exact_label_type_id raises ValueError, already naming the
        # column and the value, for every shape it refuses. Wrapping it re-raised a generic message and
        # made its three specific ones dead strings - which is why rewording them survived the suite.
        label_type_id = _exact_label_type_id(raw)
        if label_type_id not in LABEL_TYPE_NAMES_BY_ID:
            raise ValueError("unrecognised label_type_id %r" % (raw,))
        return label_type_id
    name = row.get('label_type')
    if _absent(name):
        # Neither column carried a value. KeyError so the message names a field rather than reading as
        # a bad cast, and so the CSV intake's up-front guard and this one fail the same way. It names
        # BOTH spellings: a current deployment sends only `label_type`, so an operator sent looking for
        # `label_type_id` alone is sent after a column no endpoint serves - the harm this either/or was
        # written to avoid, reintroduced in the error message.
        raise KeyError('neither label_type nor label_type_id')
    name = str(name).strip()
    if name not in LABEL_TYPE_IDS_BY_NAME:
        raise ValueError("unrecognised label_type %r" % (name,))
    return LABEL_TYPE_IDS_BY_NAME[name]


def systemic_failure_line(counts):
    """One line for a run whose errors dominate it, or None when they do not (#136).

    Reads the counts; never writes them. The alarm is a second reading of numbers the loop already
    produced, so it cannot move a label between buckets and the reconciliation invariant is untouched -
    which is the whole reason it is a separate function taking the finished dict rather than a counter
    maintained alongside the others.

    The two guards do NOT divide the quiet cases between them, and two rounds of review got this wrong
    in opposite directions before it was written as a table. Measured:

        input        shipped   without errors<=0   without total<=0
        {0,  0}      None      None                None
        {0,  1}      None      None                ZeroDivisionError
        {10, 0}      None      None                None
        {10, 4}      None      None                None
        {10, 5}      FIRES     FIRES               FIRES
        {-2, 1}      None      None                FIRES, "(-50.0%)"

    So: for any total > 0 the threshold test ALONE silences every quiet case, because errors <= 0
    already satisfies errors < fraction * total at any positive fraction - a 100% missing_pano or 100%
    skipped_existing run is silenced by the arithmetic, not by a guard. `total <= 0` is the only clause
    with a distinguishing input, and it is a division guard: without it {'total': 0, 'errors': 1}
    formats 100.0 * 1 / 0 and raises inside the run summary, making the alarm the one fatal thing in a
    loop whose contract is that nothing in it is fatal. It also short-circuits first, so on an empty run
    it is the clause that returns and `errors <= 0` is never evaluated.

    `errors <= 0` is therefore REDUNDANT at the shipped fraction, and kept deliberately as a statement
    of intent rather than as a load-bearing guard - which is why the mutant that drops it is equivalent
    and cannot be killed. Do not read its presence as evidence that something needs it.

    The denominator is `total` - every label the run was handed - and not the subset it actually tried
    to cut. That is the documented invariant's denominator, and it keeps the two skip outcomes honest:
    a city whose pano store is still catching up is 100% missing_pano and is not a crop failure, while
    a re-run over a finished store is 100% skipped_existing and did nothing wrong. The cost is a known
    blind spot: a mature store topping up a handful of labels, all of which fail to write, is a small
    fraction of a large total and does not trip this. That run still exits 1 and still logs a warning
    per label, up to LOG_WARNINGS_PER_KIND - which is the whole signal it had before - and sharpening
    it needs a denominator with its own degenerate cases, so it is left for a run that actually shows up.

    >>> systemic_failure_line({'total': 0, 'errors': 0}) is None
    True
    >>> systemic_failure_line({'total': 100, 'errors': 3}) is None
    True
    >>> systemic_failure_line({'total': 100, 'errors': 100}).startswith(SYSTEMIC_FAILURE_BANNER)
    True
    """
    total, errors = counts['total'], counts['errors']
    if total <= 0 or errors <= 0 or errors < SYSTEMIC_ERROR_FRACTION * total:
        return None
    return ("%s: %d of %d labels errored (%.1f%%). At that rate this is one cause rather than that "
            "many independent per-row faults - check the label metadata's shape (a cvMetadata schema "
            "move is what did this in #123), the pano store passed to -s, and whether -o is writable, "
            "before reading the per-label reasons in crop.log."
            % (SYSTEMIC_FAILURE_BANNER, errors, total, 100.0 * errors / total))


def _clip(text, limit):
    """text, or its first `limit` characters and '...' when it is longer."""
    return text if len(text) <= limit else text[:limit] + '...'


def _identifying_field(row, key):
    """repr of row[key], clipped, or '?' when it cannot be read: an absent key, a blank or null value,
    or a row that is not a mapping at all. For naming a label in a warning about the row being bad, so
    it must never raise itself."""
    value = row.get(key) if isinstance(row, dict) else None
    if _absent(value):
        return '?'
    return _clip(repr(value), LOG_ID_MAX_CHARS)


class WarningBudget:
    """A per-run, per-kind cap on the crop loop's per-label warnings (#139).

    `warning(kind, ...)` logs like logging.warning until LOG_WARNINGS_PER_KIND lines of that kind have
    been written, logs ONE notice when it first drops one, and after that only counts. It never touches
    the crop loop's counts dict - the caller increments its bucket unconditionally and then calls this,
    so what is logged can never change what is counted. `summary()` is the one end-of-run line.

    The cap is read from the module at each call rather than frozen at construction, so a test can
    lower it with monkeypatch.

    >>> budget = WarningBudget()
    >>> budget.summary() is None
    True
    """

    def __init__(self):
        self.logged = collections.Counter()
        self.suppressed = collections.Counter()

    def warning(self, kind, message, *args):
        if self.logged[kind] < LOG_WARNINGS_PER_KIND:
            self.logged[kind] += 1
            logging.warning(message, *args)
            return
        if not self.suppressed[kind]:
            logging.warning("Logged %d %s warnings this run; further ones are suppressed for the rest of "
                            "it, and the counts in the run summary stay exact.",
                            LOG_WARNINGS_PER_KIND, kind)
        self.suppressed[kind] += 1

    def summary(self):
        """The end-of-run total, or None if nothing was suppressed."""
        total = sum(self.suppressed.values())
        if not total:
            return None
        by_kind = ', '.join('%s: %d' % (kind, n) for kind, n in self.suppressed.items())
        return ("Suppressed %d per-label warnings this run (%s); every one of those labels is still "
                "counted in the run summary." % (total, by_kind))


def bulk_extract_crops(labels_to_crop, path_to_gsv_scrapes, destination_dir, mark_label=False, force=False,
                       city=None, sizing_rule=CROP_RULE_VERSION, tilt_correction=False):
    """Extract one crop per label into <destination_dir>/<label_type_id>/<label_id>.jpg.

    destination_dir is ONE city's store - main() passes <crop-dir>/<city>/ (#159) - and `city` is that
    city, recorded in its marker and on every manifest row. A caller below main() hands a store directly.

    Failure taxonomy: nothing here is fatal. A missing pano image is counted as missing_pano; a corrupt
    pano, malformed row, or failed write is counted as an error and logged; both leave the remaining labels
    running (#48 - one truncated JPEG used to kill a job tens of thousands of labels in). A pano whose header
    opens but whose body cannot be decoded is decoded once, lazily, by the first label that reaches the
    write; that label and every later one that reaches the write is an error, with one crop.log line for
    the pano (#164). Labels a preflight or skipped_existing decides never need the body, so their buckets
    do not depend on where they sit in the list. That includes the
    output side: a full store or a read-only mount is one counted error per label, not an exception out of
    this function with the counts lost. Crops on disk are the resume marker: existing ones are counted as
    skipped_existing and everything failed here is simply re-attempted on the next run.

    NOTE for re-runs on an existing store: without `force`, a crop already on disk is never re-cut, so a
    store cropped before the #47 seam fix or under sizing rule v1 keeps those crops. `force=True` (#83)
    re-cuts them instead, each through atomic_output_path, so a failed write leaves the old crop whole.

    Raises ProductionCropStoreError, before writing anything, if destination_dir looks like the
    production canvas-capture store - see refuse_production_crop_store. That one IS fatal, deliberately:
    it is a statement about the destination, not about any label.

    :return: counts dict. The disjoint outcomes reconcile, including on re-runs:

                 success + skipped_existing + missing_pano + dims_mismatch + out_of_frame
                     + black_content + no_pose + errors == total

             Those eight are DISJOINT_OUTCOMES. no_pose (#191) is a label about to be cut, under tilt_correction=True, whose pano has no usable
             pose beside it (always 0 without the flag; a crop already on disk is skipped_existing, since the pose is
             looked up only for a label reaching the write): skipped, not an error, and cut by a later run only if a
             pose appears. black_content (#164) is a label whose cut window was more than
             CROP_MAX_BLACK_FRACTION exactly-black: nothing is written and it is not an error, so a re-run cuts it once
             the pano is repaired. The COUNT_ANNOTATIONS are NOT among them: shifted_vertically annotates a success
             whose window had to move to stay inside the pano, so the crop exists but the label is off-centre in it;
             recut annotates a success that replaced a crop already on disk (force=True only); stale_kept annotates a
             label, under force=True, that the run did not write - a dims_mismatch or out_of_frame skip, a missing_pano,
             a no_pose skip, a black_content withhold, or an error because its pano could not be opened or decoded - and
             whose crop was already on disk, which therefore stays as whatever rule cut it. Adding a key without putting
             it in exactly one of the two is how the invariant went stale before; tests/test_crop_runner.py asserts the
             sum, and the key set, from the dict rather than from this docstring.

             The summary ends with systemic_failure_line()'s alarm when errors dominate the run (#136).
             That is a second READING of these numbers and adds no bucket of its own - which is what
             keeps the invariant above out of its way.
    """
    # Once, up front: an unknown rule is the caller's mistake, not a per-label fault, and it must fail
    # before the marker below records a rule nothing was cut under.
    if sizing_rule not in CROP_RULE_VERSIONS:
        raise ValueError("unknown sizing rule %r; expected one of %r" % (sizing_rule, CROP_RULE_VERSIONS))

    counts = dict.fromkeys(DISJOINT_OUTCOMES + COUNT_ANNOTATIONS, 0)
    counts['total'] = len(labels_to_crop)

    # Before the first write of any kind - the makedirs included - because what it protects cannot be
    # regenerated.
    refuse_production_crop_store(destination_dir)
    # Likewise, and for the same reason: another city's crops share these file names (#159).
    if city is not None:
        check_store_city(destination_dir, city)

    # Before any crop is cut, so a run that dies partway still leaves the store saying what it holds.
    os.makedirs(destination_dir, exist_ok=True)
    # Before the marker, so a refusal leaves it untouched, and so the marker sees the manifest it will
    # actually be appending to (#159 step 1).
    set_aside_pre_city_manifest(destination_dir)
    write_rule_marker(destination_dir, force=force, city=city, sizing_rule=sizing_rule,
                      tilt_correction=tilt_correction)

    # Parse rows up front and group labels by pano (preserving first-seen order), so each pano JPEG is
    # decoded exactly once for all its labels.
    labels_by_pano = {}

    def crops_on_disk(labels):
        """How many of a pano's labels already have a crop - stale_kept for a pano the run cannot use.
        A stat per label, but only on the slow paths that call it (a missing or unreadable pano)."""
        return sum(os.path.exists(os.path.join(destination_dir, str(label[2]), str(label[3]) + '.jpg'))
                   for label in labels)

    # Every per-label warning below goes through this, so a systemic fault cannot flood crop.log (#139).
    budget = WarningBudget()
    # Opened before the loop, so a run that cuts nothing still leaves the file (and its header) behind.
    manifest = ProvenanceManifest(destination_dir, city, sizing_rule)
    unrecorded = 0
    # The stale_kept labels the content check withheld, for the stale_kept summary's addendum (#164).
    stale_black_content = 0
    # Likewise for the no_pose skips (#191); and the crops the correction cut, per pose record.
    stale_no_pose = 0
    corrected_by_source = collections.Counter()
    close_failure = None
    try:
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
                label_type = resolve_label_type_id(row)
                label_id = int(row['label_id'])
                if not (math.isfinite(pano_x) and math.isfinite(pano_y)):
                    raise ValueError("non-finite label position (%r, %r)" % (row['pano_x'], row['pano_y']))
                meta_dims = _metadata_dims(row)
                provenance = tuple(_provenance_value(row.get(field)) for field in PROVENANCE_FIELDS)
            # OverflowError: json.load reads 1e999 and Infinity as float('inf'), and int() of that raises
            # OverflowError rather than ValueError - uncaught, it ended the whole run (#170 review).
            except (KeyError, TypeError, ValueError, OverflowError) as e:
                counts['errors'] += 1
                # Named fields first, then the reason and the row clipped: a whole row repr'd is ~300-500 B,
                # and it was 260,000 of those that flooded crop.log (#139). The count above is not
                # conditional on the line being written.
                budget.warning('malformed_row', "Skipping malformed label row (label_id=%s, pano_id=%s): %s; "
                               "row: %s", _identifying_field(row, 'label_id'),
                               _identifying_field(row, 'pano_id'),
                               _clip(str(e), LOG_ROW_REPR_MAX_CHARS), _clip(repr(row), LOG_ROW_REPR_MAX_CHARS))
                continue
            labels_by_pano.setdefault(pano_id, []).append(
                (pano_x, pano_y, label_type, label_id, meta_dims, provenance))

        processed = counts['errors']
        made_dirs = set()
        for pano_id, labels in labels_by_pano.items():
            pano_img_path = os.path.join(path_to_gsv_scrapes, pano_id[:2], pano_id + ".jpg")

            if not os.path.exists(pano_img_path):
                counts['missing_pano'] += len(labels)
                processed += len(labels)
                if force:
                    counts['stale_kept'] += crops_on_disk(labels)
                print("Panorama image not found: %s (%d labels skipped)" % (pano_img_path, len(labels)))
                logging.warning("Skipped %d labels on pano %s due to missing image.", len(labels), pano_id)
                continue

            try:
                pano = Image.open(pano_img_path)
            except Exception as e:
                counts['errors'] += len(labels)
                processed += len(labels)
                if force:
                    counts['stale_kept'] += crops_on_disk(labels)
                budget.warning('cannot_open', "Skipped %d labels on pano %s: cannot open %s (%s)",
                               len(labels), pano_id, pano_img_path, e)
                continue

            # Not `with pano:` - Image.__exit__ has been a no-op since Pillow 11, so the `with` form silently
            # stopped closing anything while requirements.txt still allows the older Pillow where it did.
            # close() is what actually releases the decoded buffer, which is the whole cost decode-once
            # accepts (~250 MB for a 13312x6656 pano, 384 MB for a 16384x8192 one).
            #
            # Decoding stays lazy (#164): the preflights and skipped_existing read only the header, so a
            # finished store never decodes a pano. The first label that reaches the write decodes it; if
            # that fails - a truncated body behind a good header - the failure is remembered and the pano
            # is never decoded again. Pillow keeps im.tile after a failed load, so every crop() used to
            # re-decode the whole file and log a crop_failed line per label.
            decoded = False
            decode_error = None
            undecodable = 0
            try:
                # The pose, like the decode, is looked up lazily (#193 review): only for a label about to be
                # cut, and once per pano, so a finished store never reads a pose file and a crop already on
                # disk is skipped_existing whatever its pano's pose. pose stays None without the flag.
                pose = None
                pose_looked_up = False
                pose_reason = pose_error = None
                no_pose_labels = pose_error_labels = 0
                for pano_x, pano_y, label_type, label_id, meta_dims, provenance in labels:
                    processed += 1
                    print("Cropping label %d of %d (pano %s)" % (processed, counts['total'], pano_id))

                    destination_folder = os.path.join(destination_dir, str(label_type))
                    crop_destination = os.path.join(destination_folder, str(label_id) + ".jpg")

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
                        if force and os.path.exists(crop_destination):
                            counts['stale_kept'] += 1
                        budget.warning(
                            'dims_mismatch', "Label %d on pano %s: metadata says %dx%d but the stored image is %dx%d; "
                            "skipping rather than mis-centring the crop",
                            label_id, pano_id, meta_dims[0], meta_dims[1], pano.size[0], pano.size[1])
                        continue

                    # A pano_y outside the image cannot be recovered: the poles are not adjacent, so
                    # compute_crop_box clamps to one, and the result is clean imagery of a place the label is
                    # not in - a quieter failure than the black bar it replaced, and one --mark-label cannot
                    # even reveal (the dot lands off-crop). pano_x gets no such check on purpose: column 0 and
                    # column width are the same place in the world, so any finite x is read correctly by the
                    # seam modulo, and rows storing pano_x == pano_width crop fine.
                    #
                    # Under --tilt-correction the corrected y is tested too, below, once the pose is known.
                    if not 0 <= pano_y < pano.size[1]:
                        counts['out_of_frame'] += 1
                        if force and os.path.exists(crop_destination):
                            counts['stale_kept'] += 1
                        budget.warning(
                            'out_of_frame', "Label %d on pano %s: pano_y %s is outside the %dx%d image; "
                            "skipping rather than clamping it to a pole", label_id, pano_id, pano_y,
                            pano.size[0], pano.size[1])
                        continue

                    existed = os.path.exists(crop_destination)
                    if existed and not force:
                        counts['skipped_existing'] += 1
                        continue

                    centre_x, centre_y = pano_x, pano_y
                    if tilt_correction:
                        if not pose_looked_up:
                            # The pose that matches this JPEG's scrape era (pano_pose.resolve_pano_pose), never
                            # guessed. The readers turn an unreadable file into no pose with a reason, so a raise
                            # here is a fault of ours, not of the pano: an error, retried next run, and the run
                            # goes on (nothing in the crop loop is fatal).
                            pose_looked_up = True
                            try:
                                pose, pose_reason = pano_pose.resolve_pano_pose(pano_img_path,
                                                                                depth_suffix=DEPTH_ARTIFACT_SUFFIX)
                            except Exception as e:
                                pose_error = e
                        if pose_error is not None:
                            counts['errors'] += 1
                            pose_error_labels += 1
                            if force and existed:
                                counts['stale_kept'] += 1
                            continue
                        if pose is None:
                            # A skip like missing_pano, not an error: nothing is written, and under --force an
                            # old crop stays as it was.
                            counts['no_pose'] += 1
                            no_pose_labels += 1
                            if force and existed:
                                counts['stale_kept'] += 1
                                stale_no_pose += 1
                            continue
                        beta = TILT_BETA_BY_POSE_SOURCE[pose.source]
                        centre_x, centre_y = pano_pose.corrected_pixel(pano_x, pano_y, pano.size[0], pano.size[1],
                                                                       pose.pitch_deg, pose.roll_deg, beta)
                        # The stored y is inside (checked above); the exact rotation keeps the corrected one in
                        # [0, h], so this fires only at the nadir row. Same bucket, the corrected y named.
                        if not 0 <= centre_y < pano.size[1]:
                            counts['out_of_frame'] += 1
                            if force and existed:
                                counts['stale_kept'] += 1
                            budget.warning(
                                'out_of_frame', "Label %d on pano %s: pano_y %s (tilt-corrected to %s) is outside "
                                "the %dx%d image; skipping rather than clamping it to a pole", label_id, pano_id,
                                pano_y, centre_y, pano.size[0], pano.size[1])
                            continue
                    if not decoded and decode_error is None:
                        try:
                            pano.load()
                            decoded = True
                        except Exception as e:
                            decode_error = e
                    if decode_error is not None:
                        # One counted error per label, like a pano that cannot be opened, but one crop.log
                        # line for the pano (after this loop), not one per label - so a truncated pano
                        # with many labels cannot spend the crop_failed budget real write failures need.
                        counts['errors'] += 1
                        undecodable += 1
                        if force and existed:
                            counts['stale_kept'] += 1
                        continue
                    try:
                        # Once per label type, not per label: exist_ok still costs a stat, and over sshfs
                        # that is a network round trip for each of a city's ~400k labels. Inside the try
                        # because an OSError here - a full store, a read-only mount, an sshfs drop - must be
                        # one counted error like any other write failure, not the end of the run (#48).
                        if destination_folder not in made_dirs:
                            os.makedirs(destination_folder, exist_ok=True)
                            made_dirs.add(destination_folder)
                        # centre only when corrected, so a run without the flag makes the call it always
                        # made (#191).
                        corrected_centre = {} if pose is None else {'centre': (centre_x, centre_y)}
                        box = make_single_crop(pano, pano_x, pano_y, crop_destination,
                                               draw_mark=mark_label, sizing_rule=sizing_rule, **corrected_centre)
                    except CropWindowMostlyBlackError as e:
                        # Not an error (#164): the pano on disk holds black where imagery should be, and
                        # nothing was written, so a re-run cuts this label once the pano is repaired.
                        # Under --force an old crop stays exactly as it was, like a preflight skip.
                        counts['black_content'] += 1
                        if force and existed:
                            counts['stale_kept'] += 1
                            stale_black_content += 1
                        budget.warning('black_content', "Label %d on pano %s: %.0f%% of the cut window is "
                                       "black (limit %.0f%%); not written - the pano on disk holds black "
                                       "where imagery should be", label_id, pano_id, 100 * e.fraction,
                                       100 * CROP_MAX_BLACK_FRACTION)
                        continue
                    except Exception as e:
                        counts['errors'] += 1
                        budget.warning('crop_failed', "Failed to crop label %d on pano %s: %s",
                                       label_id, pano_id, e)
                        continue
                    counts['success'] += 1
                    if existed:
                        counts['recut'] += 1
                    if pose is not None:
                        corrected_by_source[pose.source] += 1

                    # After the crop is on disk and counted, never instead of it (#111). A failed append is
                    # NOT an error: the crop is the resume marker, so a re-run skips it and could never write
                    # the row - counting it would break "errors retry next run" and put one label in two
                    # buckets. It is logged per label and totalled in the summary instead.
                    try:
                        manifest.record(label_id, pano_id, provenance)
                    except Exception as e:
                        unrecorded += 1
                        budget.warning('provenance_unrecorded',
                                       "Label %d on pano %s: cropped, but its provenance row was not written "
                                       "to %s (%s)", label_id, pano_id, PROVENANCE_MANIFEST, e)
                    if box.shifted:
                        # The crop is real imagery containing the label, but the label is not at its
                        # centre. Counted rather than merely logged: a consumer that assumes centring
                        # needs a number, and #54 wants it as a per-label covariate.
                        counts['shifted_vertically'] += 1
                        logging.info("Label %d on pano %s: window shifted to stay inside the pano "
                                     "(top=%d), so the label sits %d px from the crop's centre",
                                     label_id, pano_id, box.top,
                                     abs(int(centre_y - box.top - box.height / 2)))
                    if pose is None:
                        logging.info('%s.jpg %s %s %s', label_id, pano_id, pano_x, pano_y)
                    else:
                        logging.info('%s.jpg %s %s %s -> %s %s (pitch %s, roll %s from %s; beta %s)',
                                     label_id, pano_id, pano_x, pano_y, centre_x, centre_y, pose.pitch_deg,
                                     pose.roll_deg, pose.source, beta)
                if no_pose_labels:
                    budget.warning('no_pose', "Skipped %d labels on pano %s: no pose for the tilt correction (%s)",
                                   no_pose_labels, pano_id, pose_reason)
                if pose_error_labels:
                    budget.warning('crop_failed', "Failed to crop %d labels on pano %s: resolving its pose raised "
                                   "%s: %s", pose_error_labels, pano_id, type(pose_error).__name__, pose_error)
                if undecodable:
                    # The cannot_open kind, since it is the same fault one step later: the header opened,
                    # the body did not. "decode", not "open", in the text, so the two stay tellable apart.
                    budget.warning('cannot_open', "Skipped %d labels on pano %s: cannot decode %s (%s); "
                                   "counted as errors without decoding it again per label",
                                   undecodable, pano_id, pano_img_path, decode_error)
            finally:
                pano.close()
    finally:
        # In a finally, and guarded (#153 M1): a close that raises on the full-store night used to take
        # the run summary, the unrecorded total and the #136 alarm down with it - the three lines that
        # night exists to produce. The crops are on disk either way; only whether the last rows reached
        # the store is in doubt, so it is said on both channels and the run goes on to its summary.
        try:
            manifest.close()
        except Exception as e:
            close_failure = e
            message = ("The provenance manifest %s could not be closed cleanly (%s): the crops are on "
                       "disk, but rows appended this run may not all have reached the store."
                       % (PROVENANCE_MANIFEST, e))
            logging.error('%s', message)
            print(message)
        # Also in the finally, so a run killed after losing a row still says so in the marker. Each of
        # the two is a crop on disk this run knows has no row, or may not (a failed close). A torn row
        # found at open is the third, and was recorded before it was cut (ProvenanceManifest._open).
        if unrecorded or close_failure is not None:
            try:
                _record_manifest_gap(destination_dir)
            except Exception as e:
                message = ("CropRunner could not record the gap in %s (%s): its %s still reads as it did "
                           "when this run started, but this run left crops without a row in %s."
                           % (CROP_RULE_MARKER, e, MANIFEST_NO_KNOWN_GAP, PROVENANCE_MANIFEST))
                logging.error('%s', message)
                print(message)

    print("Finished.")
    # Echoed here as well as written to <crop-dir>/crop_rule.json, because the summary is what an
    # operator reads and the marker is what a consumer reads. The marker is the one that matters: a
    # line of stdout scrolls past on a cron run, and a crop store carries no other provenance.
    print("Crop sizing rule %s (recorded in %s)." % (sizing_rule, CROP_RULE_MARKER))
    if tilt_correction:
        # Said only when on, so a run without the flag prints what it always did (#191). The crops the
        # correction actually cut, per pose record and its beta - not the labels parsed, which include the
        # ones that were skipped or failed (#193 review).
        message = ("Tilt correction on (recorded in %s): %d crops cut from an xml pose (beta %s), %d from an "
                   "npz pose (beta %s)." % (CROP_RULE_MARKER,
                                            corrected_by_source[pano_pose.POSE_SOURCE_XML],
                                            TILT_BETA_BY_POSE_SOURCE[pano_pose.POSE_SOURCE_XML],
                                            corrected_by_source[pano_pose.POSE_SOURCE_NPZ],
                                            TILT_BETA_BY_POSE_SOURCE[pano_pose.POSE_SOURCE_NPZ]))
        logging.info('%s', message)
        print(message)
    print("%d crops extracted, %d already existed, %d skipped because the panorama image was missing, "
          "%d skipped on a metadata/image dimension mismatch, %d skipped for a label position outside "
          "the image, %d withheld for a mostly black window, %d skipped for no pano pose, %d errors, of %d "
          "labels total."
          % (counts['success'], counts['skipped_existing'], counts['missing_pano'],
             counts['dims_mismatch'], counts['out_of_frame'], counts['black_content'], counts['no_pose'],
             counts['errors'], counts['total']))
    if counts['shifted_vertically']:
        print("%d of those crops were shifted to stay inside the pano, so their label is not at the "
              "crop's centre." % counts['shifted_vertically'])
    if counts['recut']:
        print("%d of those crops were re-cut over one already on disk (--force)." % counts['recut'])
    if counts['stale_kept']:
        # Both channels: a forced run promises a store cut under one rule, and these labels break it.
        # Whether to delete such a crop is a decision nobody has made, so it is counted and said instead.
        # crop.log's lines are not promised to name them (#153 final F5): three of the four kinds are
        # capped per run, and none says whether its label kept an old crop.
        message = ("%d labels skipped under --force - by a preflight, a missing pano or an unreadable pano - "
                   "kept a crop already on disk: the run never reached their write, so the old crop was "
                   "neither re-cut nor removed and stays as whatever rule cut it, while %s says %s. "
                   "crop.log's dims_mismatch, out_of_frame and cannot_open lines (up to %d of each) and its "
                   "missing-pano lines include them, without marking which kept an old crop."
                   % (counts['stale_kept'], CROP_RULE_MARKER, sizing_rule, LOG_WARNINGS_PER_KIND))
        if stale_black_content:
            # Appended, never rewording the sentence above: the content check reaches the write and then
            # declines it, so "skipped by a preflight" does not describe these (#164).
            message += (" %d of them were withheld by the content check (black_content) rather than skipped "
                        "by a preflight; crop.log's black_content lines (up to %d) include them."
                        % (stale_black_content, LOG_WARNINGS_PER_KIND))
        if stale_no_pose:
            # Appended for the same reason (#191): a pano with no pose is its own skip, not a preflight.
            message += (" %d of them were on a pano with no pose for the tilt correction (no_pose); crop.log's "
                        "no_pose lines (up to %d, one per pano) include them."
                        % (stale_no_pose, LOG_WARNINGS_PER_KIND))
        logging.warning('%s', message)
        print(message)
    if counts['black_content']:
        # Both channels (#164): black_content is not an error, so the exit code stays 0, and a run that
        # withheld every label would otherwise complete silently - the #101 shape.
        message = ("%d labels were withheld because more than %.0f%% of their cut window was black "
                   "(black_content): the pano on disk holds black where imagery should be (a stitch that ran "
                   "past what Google serves - #156 - or a pre-#68 fallback). Nothing was written for them, so "
                   "a re-run cuts them once the pano is re-downloaded; crop.log names up to %d of them."
                   % (counts['black_content'], 100 * CROP_MAX_BLACK_FRACTION, LOG_WARNINGS_PER_KIND))
        logging.warning('%s', message)
        print(message)

    if counts['no_pose']:
        # Both channels (#191), black_content's reason: no_pose is not an error, so the exit code stays 0,
        # and a run that skipped every label for want of a pose would otherwise complete silently.
        message = ("%d labels were skipped because their pano has no pose for the tilt correction (no_pose), and "
                   "nothing was written for them. A later run cuts them only if a pose appears beside the pano, "
                   "i.e. the depth phase writes its .depth.npz; a pano the depth phase ledgers unavailable, an npz "
                   "without a finite pitch and roll, and an .xml that is incomplete or unreadable stay no_pose. "
                   "crop.log names up to %d of the panos and why." % (counts['no_pose'], LOG_WARNINGS_PER_KIND))
        logging.warning('%s', message)
        print(message)

    if manifest.torn_rows_cut:
        # Both channels: a previous run was killed mid-append, and the crop that row was for is on disk.
        message = ("%s ended in a torn row, which was cut away: a previous run was killed while appending "
                   "it, so one crop on disk has no row. %s in %s is now false."
                   % (PROVENANCE_MANIFEST, MANIFEST_NO_KNOWN_GAP, CROP_RULE_MARKER))
        logging.warning('%s', message)
        print(message)
    if unrecorded:
        # Both channels: the crops are fine, but a consumer reading the manifest as "every crop here"
        # would now be wrong about these. Each promise is the exact one (#153 m4): crop.log's lines are
        # capped per kind, and a --force re-run is the one thing that does write their rows.
        message = ("%d crops were written without a row in %s (crop.log names up to %d of them); their "
                   "provenance is not recorded: a plain re-run skips existing crops and will not record "
                   "it; --force re-cuts them and writes their rows."
                   % (unrecorded, PROVENANCE_MANIFEST, LOG_WARNINGS_PER_KIND))
        logging.warning('%s', message)
        print(message)
    suppressed = budget.summary()
    if suppressed:
        # crop.log only: the lines it accounts for were never on stdout, whose summary above is whole.
        logging.warning('%s', suppressed)
    alarm = systemic_failure_line(counts)
    if alarm:
        # Both channels, the depth phase's pattern, and last so it is the line left on screen: stdout
        # is how an operator hears about it tonight, crop.log is what is still there next week. The
        # numbers above say the same thing, but a rate has to be read off them - and the failure this
        # exists for buries them under one warning per label.
        logging.error('%s', alarm)
        print(alarm)
    return counts


def run(sidewalk_server_fqdn, label_metadata_file, gsv_pano_path, crop_destination_path, mark_label=False,
        force=False, city=None, sizing_rule=CROP_RULE_VERSION, tilt_correction=False):
    """Load the label metadata and extract every crop - the whole job, minus process-level setup.

    main() owns argv parsing, directory creation, and logging; this seam takes plain arguments so tests can
    drive the real intake -> crop loop in-process (the #52.1 shape). crop_destination_path is one city's
    store, <crop-dir>/<city>/, which main() composes from -o and --city (#159).
    """
    print("Cropping labels")
    label_infos = load_label_metadata(sidewalk_server_fqdn, label_metadata_file)
    return bulk_extract_crops(label_infos, gsv_pano_path, crop_destination_path, mark_label=mark_label,
                              force=force, city=city, sizing_rule=sizing_rule, tilt_correction=tilt_correction)


def main(argv=None):
    """Process-level setup, then run(): everything a `python3 CropRunner.py ...` invocation does.

    Exceptions propagate, argparse errors exit 2, and an unrecognized -f extension exits with a message -
    not the NameError it used to be.

    :return: 0; 1 if any label errored, or if a label-type shard of -o cannot be listed (said on
             both channels, nothing cut); EXIT_REFUSED_DESTINATION if -o looks like the production
             crop store, or holds a directory the guard cannot list, or is recorded as another
             city's store or has a crop_rule.json that cannot say whose (nothing is created or written,
             crop.log included), or is a pre-#159 flat store rather than a root of per-city stores
             (refuse_legacy_crop_root); EXIT_REFUSED_DESTINATION too for a crop_provenance.csv whose header
             this run cannot append under (after crop.log opens, before any crop). 1 is deliberately not keyed
             on "did every label produce a crop": missing panos are the normal state of a city whose
             scrape is still catching up, while `errors` only ever counts things that should not have
             happened - a corrupt pano, a malformed row, a failed write - so it is the half worth
             waking someone for.
    """
    args = build_parser().parse_args(argv)

    # First, before -o is created and before crop.log is opened inside it: the refusal must not itself
    # leave a file in the store it is refusing. Logging is not configured yet, so logging.error reaches
    # stderr through the root logger's last-resort handler - stdout plus stderr, both channels.
    try:
        refuse_production_crop_store(args.o)
    except ProductionCropStoreError as e:
        print("CropRunner: %s" % e)
        logging.error('%s', e)
        return EXIT_REFUSED_DESTINATION
    # -o is a root holding one store per city (#159): label_id restarts in every deployment, so a crop's
    # file name is unique only inside its city's directory. Everything below - the marker, the manifest,
    # crop.log and the crops - is this city's store, never the root.
    # A pre-#159 flat store at -o is not a root: refused before the city store is created inside it.
    try:
        refuse_legacy_crop_root(args.o, args.city)
    except LegacyCropStoreError as e:
        print("CropRunner: %s" % e)
        logging.error('%s', e)
        return EXIT_REFUSED_DESTINATION

    store = os.path.join(args.o, args.city)

    # Again at the store (#159). Both layouts are city-first, so from -o the scan reaches a production city
    # directory only at depth 1, where the LabelType-name signal is not read: one whose type directories
    # are still empty passes the root-level scan, and crop.log would be the first file written into it.
    try:
        refuse_production_crop_store(store)
    except ProductionCropStoreError as e:
        print("CropRunner: %s" % e)
        logging.error('%s', e)
        return EXIT_REFUSED_DESTINATION

    # Same place, same rule: a store recorded as another city's (renamed or copied into this name) is
    # refused before anything - crop.log included - is written into it.
    try:
        check_store_city(store, args.city)
    except CropStoreCityError as e:
        print("CropRunner: %s" % e)
        logging.error('%s', e)
        return EXIT_REFUSED_DESTINATION

    # exist_ok: a re-run, or an operator pre-creating the dir, races on the exists check. Note this is not
    # a claim that two CropRunners may share a store: crops are written through a fixed
    # <label_id>.jpg.part, so concurrent runs over the same labels would fight over that temp path.
    os.makedirs(store, exist_ok=True)

    # crop.log lives next to the crops it describes, NOT the CWD (which under cron is wherever the process
    # happened to start - the DownloadRunner #49 lesson), and in the city's store, not the root.
    configure_logging(os.path.join(store, 'crop.log'))

    raise_decompression_bomb_ceiling()

    try:
        counts = run(sidewalk_server_fqdn=args.d, label_metadata_file=args.f, gsv_pano_path=args.s,
                     crop_destination_path=store, mark_label=args.mark_label, force=args.force,
                     city=args.city, sizing_rule=args.sizing_rule, tilt_correction=args.tilt_correction)
    except CropStoreUnlistableError as e:
        # Both channels, and into crop.log, which is configured by now: as a traceback it reached stderr
        # only (#153 final F6). Exit 1, not EXIT_REFUSED_DESTINATION - nothing judged -o to be the
        # production store; it could not be read, and nothing was cut.
        logging.error('%s', e)
        print("CropRunner: %s" % e)
        return 1
    except ProvenanceManifestHeaderError as e:
        # A verdict about the store, reached before any crop was cut: re-running changes nothing until a
        # person moves the file, so it is a refusal (3), not a label error (1).
        logging.error('%s', e)
        print("CropRunner: %s" % e)
        return EXIT_REFUSED_DESTINATION
    return 1 if counts['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
