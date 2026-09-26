# !/usr/bin/python3

import argparse
import collections
import csv
import json
import logging
import logging.handlers
import math
import os
import random
import signal
import sys
import time
from datetime import datetime
from os.path import exists

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from downloaders import DownloadResult, download_pano, gsv, mapillary, store_sftp
from downloaders.common import raise_decompression_bomb_ceiling


def _reservation_minutes(value):
    """argparse type= for --min-depth-runtime: a finite, non-negative float.

    Without this, a negative value or nan silently made no reservation and inf silently zeroed the image
    phase — a misconfiguration should fail the run at parse time, not misbehave quietly for months.
    """
    try:
        minutes = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid float value: %r" % (value,))
    if math.isnan(minutes) or math.isinf(minutes) or minutes < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number of minutes: %r" % (value,))
    return minutes


# The stop reason that means "a budget stopped this phase; more time would have kept it going". It is the
# same string downloaders.gsv writes for the depth phase (DEPTH_STOP_MAX_RUNTIME) and the same one
# scrape_queue compares against to decide who gets an extra pass (#43), so all three are pinned to each
# other by tests: a rename in one alone would silently turn every city into "finished" and leave the
# night's leftover window unspent.
STOP_MAX_RUNTIME = 'max-runtime'


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('d', help='sidewalk_server_domain - FQDN of SidewalkWebpage server to fetch pano list from, i.e. sidewalk-columbus.cs.washington.edu')
    parser.add_argument('s', help='storage_path - location to store scraped panos')
    parser.add_argument('-c', nargs='?', default=None, help='csv_path - location of csv from which to read pano metadata')
    parser.add_argument('--all-panos', action='store_true', help='Download images for all panos that users visited, even if no labels were added on them. Does not affect depth, which always covers every pano.')
    parser.add_argument('--skip-depth', action='store_true', help='Skip downloading GSV depth maps (downloaded by default via the streetlevel library).')
    parser.add_argument('--max-runtime', type=float, default=None, metavar='MINUTES', help='Stop starting new downloads after this many minutes have elapsed.')
    parser.add_argument('--min-depth-runtime', type=_reservation_minutes, default=0.0, metavar='MINUTES', help='Reserve the last MINUTES of --max-runtime for the depth phase when the depth ledger shows unresolved work, so an image backlog cannot starve depth. This is a reservation carved out of the image phase\'s start budget, not a hard floor on depth wall time: the image phase stops STARTING new panos once its share is spent (a pano already in flight can overrun into the reserved slice), and depth still ends at --max-runtime, so it also gets any slack images leave. If the reservation meets or exceeds --max-runtime, NO images are downloaded that run. Default 0 (no reservation); it is a share of --max-runtime, and the production queue passes 6 of a 12-minute slot. Ignored without --max-runtime or with --skip-depth.')
    parser.add_argument('--max-depth-requests', type=int, default=None, metavar='N', help='Stop the depth phase after this many depth metadata requests.')
    parser.add_argument('--depth-block-latch', default=None, metavar='PATH', help='Where to remember that Google refused this host, so the next city in the queue stands down instead of rediscovering the block with fresh requests. Defaults to a file in the system temp directory - LOCAL disk, not the pano store, because it is a fact about this host and because the storage dir given to a run belongs to a single city. A latch younger than 6 hours skips the depth phase entirely; images are unaffected.')
    parser.add_argument('--depth-pace-state', default=None, metavar='PATH', help='Where the depth pacer remembers the request interval this host has EARNED, so the next city in the queue opens there instead of ramping down from depth_start_interval again. Only earned speed is remembered - a back-off or a refusal resets it - and a file older than a day is ignored. Defaults to a file in the system temp directory beside the block latch, for the same reasons.')
    parser.add_argument('--run-summary-file', default=None, metavar='PATH', help='Write a small JSON object naming what stopped each phase (image_stop, depth_stop) to PATH. This is how scrape_queue decides which cities still have work and so get an extra pass over the leftover window (#43); nothing else reads it, and without the flag nothing is written. Deliberately has no default: a default path would write into whatever CWD cron happened to start in.')
    # Store mode (#30). The help text says the two operator facts in so many words, and a test pins them.
    parser.add_argument('--from-store', type=store_sftp.remote_city_id, default=None, metavar='CITY_ID', help='Pull already-scraped panoramas for this city from the Project Sidewalk pano store over SFTP instead of downloading them from the imagery provider. The default, without this flag, is to download from the provider yourself. This mode is for collaborators with a working relationship with the Project Sidewalk team, who issue the SFTP credentials it needs; read them from PS_SFTP_HOST / PS_SFTP_BASE (and optionally PS_SFTP_USER / PS_SFTP_PORT / PS_SFTP_KEY) or the matching --sftp-* flags. CITY_ID is the city folder on the store (e.g. seattle-wa) and has no default. Google is never contacted in this mode: the depth phase is skipped unless --with-depth pulls the stored artifacts instead. A pano the store does not have yet is retried next run, never written off.')
    parser.add_argument('--with-depth', action='store_true', help='With --from-store: also pull the stored .depth.npz for every GSV pano that lacks one locally. Writes nothing to depth_log.csv.')
    parser.add_argument('--sftp-host', default=None, help='With --from-store: the pano store host (default: $PS_SFTP_HOST).')
    parser.add_argument('--sftp-base', default=None, help='With --from-store: the pano store root on that host (default: $PS_SFTP_BASE).')
    parser.add_argument('--sftp-user', default=None, help='With --from-store: SSH user (default: $PS_SFTP_USER, else ~/.ssh/config).')
    parser.add_argument('--sftp-port', default=None, help='With --from-store: SSH port (default: $PS_SFTP_PORT, else ~/.ssh/config).')
    parser.add_argument('--sftp-key', default=None, help='With --from-store: SSH private key (default: $PS_SFTP_KEY, else ~/.ssh/config).')
    # Deprecated no-op, kept for one release so existing invocations don't crash argparse.
    parser.add_argument('--attempt-depth', action='store_true', help=argparse.SUPPRESS)
    return parser


def configure_logging(log_path):
    """Set up run-wide logging to log_path (scrape.log on the pano store).

    Rotation (10 MB x 3) bounds growth now that the file persists across runs instead of dying with the
    container; each DEBUG record is a synchronous write over sshfs, which is also why urllib3's per-request
    chatter is capped at WARNING. If the log file itself can't be opened, fall back to stderr with one loud
    warning rather than killing the scrape: the log is evidence, not cargo.

    mapillary.TokenRedactionFilter is added to the HANDLER, not the root logger: a Logger's own filters run
    only for records it originates itself, while a Handler's filters run for every record that reaches it
    regardless of which logger (root, 'urllib3', anything a future dependency adds) produced it. This is the
    one handler every record in the process funnels through, so it's the only place a filter added here is
    guaranteed to see a leak no matter which module's message carries it (2026-09 PR #100 review, finding 2).
    """
    try:
        handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=3)
        fallback_error = None
    except OSError as e:
        handler = logging.StreamHandler()
        fallback_error = e
    handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
    handler.addFilter(mapillary.TokenRedactionFilter())
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    if fallback_error is not None:
        logging.warning("Could not open %s (%s); logging to stderr for this run", log_path, fallback_error)
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def progress_check(csv_pano_log_path):
    """Read the image ledger once: every ledgered pano id, plus the prior counters seeded into this run's.

    A row means "resolved": downloaded == 1 counts as a prior success (skipped this run), 0 as a permanent
    failure (the source has nothing for this pano); either way the pano is never re-attempted. Transient
    failures are not ledgered at all (#41), so they are absent here and retry next run.

    Row-tolerant on the gsv._load_depth_log model: a line torn by a crash mid-append (or a float minted by
    the old rewrite path) is skipped, so a damaged ledger degrades to re-attempting a few panos instead of a
    ParserError that crashes every future run (#55). Reads with csv, not pandas, so the id type can never
    depend on what the ids happen to look like (#46).

    TWO widths are legal, and this is the one place that knows it. Rows were `pano_id,downloaded` until
    2026-09-10 and are `pano_id,downloaded,fetched_at` after it (#114), so every long-lived store holds
    both: the header and the first million rows are two fields, everything appended since is three. Nothing
    rewrites the old rows - see docs/ops.md - so tolerating the mixture here is not a migration step, it is
    the permanent state.

    Written as a membership test rather than `len(row) >= 2` deliberately. The looser form would accept a
    four-field row, and a row with a surplus field is the signature of a torn append or a stray comma, which
    is exactly the damage the tolerance above exists to survive rather than to trust. Widening this to a new
    width is a decision, not a default.

    The `!= 2` this replaced was load-bearing in the dangerous direction: it silently `continue`d on every
    three-field row, so a timestamped ledger parsed as EMPTY. Nothing raises - the whole corpus reads as
    unattempted, permanent `downloaded=0` verdicts stop being terminal and are re-requested against Google
    every night, duplicate rows accumulate, and the prior-failure counters that seed log.csv's column 9 all
    read zero. That is the shape of a rollback to a pre-#114 build, and it is why the writer and this reader
    ship in the same commit.

    The depth and refetch ledgers keep their own `!= 2`: neither gained a column, and a shared helper here
    would let a future widening of one silently widen all three.
    """
    ledgered_ids, total_processed, total_success = set(), 0, 0
    with open(csv_pano_log_path, newline='') as f:
        for row in csv.reader(f):
            if len(row) not in (2, 3) or row[0] == 'pano_id' or row[1] not in ('0', '1'):
                continue
            ledgered_ids.add(row[0])
            total_processed += 1
            total_success += row[1] == '1'
    return ledgered_ids, total_processed, total_success, total_processed - total_success


def _normalize_pano_records(records):
    """Coerce pano_id to str at the intake boundary, drop empty/'tutorial' rows, dedupe keeping the first.

    Numeric (Mapillary) ids otherwise arrive as ints and crash every pano_id[:2] shard slice, and set
    membership against the ledger's string ids silently misses (#46). Centralised so the CSV and webserver
    paths cannot drift - the CSV path also gains the tutorial/empty filter the webserver path always had.
    """
    unique_ids = set()
    kept = []
    for record in records:
        raw = record.get('pano_id')
        # The float-nan case is the JSON path, not the CSV one: Python's json parses a bare NaN literal by
        # default, so response.json() can hand us one, and str() would keep it as the id 'nan' - a real
        # shard path, na/nan.jpg. (It used to be the CSV path too: pd.read_csv returned nan for a blank
        # cell even under dtype={'pano_id': str}. csv.DictReader returns '', which falls through to the
        # empty check below.)
        if raw is None or (isinstance(raw, float) and math.isnan(raw)):
            pano_id = ''
        else:
            pano_id = str(raw)
        if pano_id in unique_ids:
            continue
        if not pano_id or pano_id == 'tutorial':
            print("Pano ID is an empty string or is for tutorial")
            continue
        record['pano_id'] = pano_id
        unique_ids.add(pano_id)
        kept.append(record)
    return kept


# has_labels spellings a hand-made CSV may reasonably use. Everything else raises, because
# select_image_panos tests the value for truth and every non-empty string is true - so an unrecognised
# spelling would silently mean 'labelled' and quietly undo the --all-panos split.
_TRUE_SPELLINGS = frozenset(('true', 't', 'yes', 'y', '1'))
_FALSE_SPELLINGS = frozenset(('false', 'f', 'no', 'n', '0'))


def _parse_has_labels(raw, metadata_csv_path):
    """Coerce a CSV has_labels cell to a real bool. Blank or absent counts as labelled.

    pd.read_csv typed this column from its contents: bool for True/False, int64 for 1/0, float64 (nan) for
    a blank, and str for anything else - INCLUDING ' True ' with padding. The two str cases were silently
    truthy, so a padded or typo'd cell downloaded the pano and said nothing.
    """
    if raw is None:
        return True
    text = raw.strip().lower()
    if not text:
        return True
    if text in _TRUE_SPELLINGS:
        return True
    if text in _FALSE_SPELLINGS:
        return False
    raise ValueError("%s has an unreadable has_labels value %r; expected one of %s"
                     % (metadata_csv_path, raw,
                        ', '.join(sorted(_TRUE_SPELLINGS | _FALSE_SPELLINGS))))


def fetch_pano_ids_csv(metadata_csv_path):
    """
    Loads pano metadata from a CSV file (downloaded from the server). Dedupes on pano_id.
    Expected to include the same columns as /adminapi/panos, notably `source`.

    Read with csv, not pandas (#72), so no field's type depends on what the values happen to look like -
    the inference that caused #46 and #55. Every cell is a str; the two exceptions below are the fields
    whose consumers need something else.

    utf-8-sig because a hand-made CSV out of Excel carries a BOM, which would otherwise glue itself to the
    first fieldname and fire the column guard on a perfectly good file.
    """
    with open(metadata_csv_path, newline='', encoding='utf-8-sig') as csv_file:
        reader = csv.DictReader(csv_file)
        # Fail loudly on a header typo. -c exists for hand-made CSVs, and _normalize_pano_records would
        # otherwise read every row's missing id as blank and filter the whole file out - a run that
        # downloads nothing, prints one 'empty string or tutorial' line per row, and exits 0. fieldnames is
        # None for an empty file, and `'pano_id' not in None` is a TypeError, so check that first.
        if not reader.fieldnames or 'pano_id' not in reader.fieldnames:
            raise ValueError("%s has no 'pano_id' column; found %r"
                             % (metadata_csv_path, reader.fieldnames))
        records = [_normalize_csv_row(row, metadata_csv_path) for row in reader]
    return _normalize_pano_records(records)


def _normalize_csv_row(row, metadata_csv_path):
    """One DictReader row as the rest of the pipeline expects it.

    Blank cells become None because that is what the consumers test for: gsv.download_single_pano reads the
    dims as `int(v) if v is not None else None`, and a '' walks past that guard into int('').

    Surplus fields are dropped. DictReader files them under the key None; pandas did something far worse
    with the same input - it consumed the first column as the frame's index, so every field shifted by one
    and the real pano_id vanished out of the record entirely, without raising.
    """
    record = {key: (value if value else None) for key, value in row.items() if key is not None}
    if 'has_labels' in record:
        record['has_labels'] = _parse_has_labels(record['has_labels'], metadata_csv_path)
    return record


def fetch_pano_ids_from_webserver(sidewalk_server_fqdn):
    """
    Fetch pano metadata from /adminapi/panos on sidewalk_server_fqdn.

    Each entry is a dict with: pano_id, width, height, lat, lng, camera_heading, camera_pitch, source, has_labels.

    Returns every pano the server knows about. Source-specific dispatch happens at download time, and the
    --all-panos / has_labels split happens in select_image_panos() - the depth phase wants the whole corpus, so
    filtering here would hide unlabelled panos from it.
    """
    # requests with retries and a timeout, like everything else in the repo. The raw http.client this replaced
    # had no timeout (a hung server stalled the nightly run indefinitely), no status check (a 500 or a proxy
    # error page surfaced as an unexplained JSONDecodeError), and never closed the connection (#51).
    with requests.Session() as session:
        # Parity with the http.client path this replaced: no env-proxy routing, no env CA overrides. Session
        # would otherwise newly honour HTTP(S)_PROXY / NO_PROXY / REQUESTS_CA_BUNDLE on the scraper boxes.
        session.trust_env = False
        # read=0: if the read timeout below ever does trip, retrying is just hammering the admin endpoint
        # with the same slow query five more times — fail once instead. Connect failures still retry.
        retry = Retry(total=5, connect=5, read=0, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retry)
        # Both schemes, so a redirect hop to http:// can't silently fall back to the retry-less default adapter.
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        # (connect, read) timeouts. The read half is generous because it applies per socket op INCLUDING the
        # wait for the status line, and /adminapi/panos most likely buffers the whole JSON server-side before
        # sending its first byte — on a multi-million-pano city that can take minutes, and it's exactly the
        # fetch this timeout exists to protect.
        response = session.get('https://%s/adminapi/panos' % (sidewalk_server_fqdn), timeout=(30, 600))
        response.raise_for_status()
        jsondata = response.json()

    # The JSON should carry string ids already; normalising here makes that structural (#46).
    return _normalize_pano_records(jsondata)


def select_image_panos(pano_infos, include_all_panos):
    """
    Narrow the pano list to what the image phase should download.

    --all-panos gates images only: depth is wanted for every pano including ones nobody has labelled, and it costs
    one metadata request per pano either way, so the depth phase always gets the full list.

    A pano with no has_labels key counts as labelled. That preserves the -c path's behaviour for hand-made CSVs,
    which have always downloaded everything in the file regardless of --all-panos.
    """
    if include_all_panos:
        return pano_infos
    return [p for p in pano_infos if p.get('has_labels', True)]


def filter_supported_sources(pano_infos, require_credentials=True):
    """
    Drop panos we can't download in this run, preserving the server's ordering, with a one-time warning per
    reason.

    Supported sources: gsv, panoramax, and mapillary when MAPILLARY_ACCESS_TOKEN is set. Filtered-out panos
    are NOT written to pano_id_log.csv, so a later run with the token / updated code can still pick them up.

    Order-preserving on purpose (#40): the old implementation regrouped the list by source as a side effect
    of bucketing for the warnings, which put every GSV pano ahead of every Mapillary one - on a city whose
    GSV backlog exceeds --max-runtime, Mapillary then made zero progress, indefinitely and invisibly. A
    filter has no business reordering its input; download_panorama_images shuffles what it actually attempts,
    which is where starvation has to be prevented. The counts below exist only for the warnings.

    @param require_credentials False in store mode (#30): the store already holds the bytes, so a Mapillary
        pano needs no token there - and a collaborator was never meant to have one. Unknown sources are
        still dropped either way: a source this code has never heard of is a signal, not a pull.
    """
    source_counts = {}
    for p in pano_infos:
        source = p.get('source')
        source_counts[source] = source_counts.get(source, 0) + 1

    # Mapillary gets its own warning naming the missing token, so it is never also reported as an
    # unsupported source - hence 'known' rather than reusing 'supported' in the loop below.
    #
    # Panoramax (#110) is in BOTH sets unconditionally: the catalog is keyless, so unlike Mapillary there is
    # no environment variable that can make a city's panos undownloadable and nothing to warn about. Adding
    # the word here is the whole of what stands between Bayonne opening (2026-09-18) and a city whose every
    # pano is silently dropped every night - the run completes, log.csv says it had nothing to do, and
    # CropRunner reports missing_pano for every label (#101's failure shape).
    known = {'gsv', 'mapillary', 'panoramax'}
    supported = {'gsv', 'panoramax'}
    # Both warnings go to stdout AND to scrape.log, the depth phase's pattern (#52 item 6). stdout is what
    # cron mails, which is how an operator finds out tonight; scrape.log is what is still there next week
    # when someone asks why a city's Mapillary panos never arrived. Either channel alone loses one of those.
    if source_counts.get('mapillary'):
        if not require_credentials or mapillary.is_token_set():
            supported.add('mapillary')
        else:
            logging.warning("%d Mapillary panos skipped - set %s to download them",
                            source_counts['mapillary'], mapillary.TOKEN_ENV_VAR)
            print("WARNING: %d Mapillary panos skipped — set %s to download them"
                  % (source_counts['mapillary'], mapillary.TOKEN_ENV_VAR))

    for source, count in source_counts.items():
        if source not in known:
            logging.warning("%d panos with unsupported source %r skipped", count, source)
            print("WARNING: %d panos with unsupported source %r skipped" % (count, source))

    return [p for p in pano_infos if p.get('source') in supported]


class ImageLedger:
    """<storage>/pano_id_log.csv for one phase; see progress_check for what a row means.

    Constructing it reads the prior rows once (ids, prior_total, prior_success, prior_fail). Entering it opens
    the append handle, writing the header when this run creates the file; record() appends one row, flushes
    it and remembers the id. Shared by the image loop and the store pull (#30), so the header, the mode and
    the line terminator have one definition - a third hand-copied block is the one that forgets a clause.

    One handle held for the whole phase, appended and flushed per row - the depth ledger's pattern (#55).
    The old shape opened/closed the file per pano over sshfs, and carried a dead 'update' branch that, when
    the #46 dtype mismatch made it reachable, rewrote the ENTIRE file per pano with mode='w' - O(n^2) per
    run, and a crash mid-rewrite truncated the only image ledger in place.
    """

    def __init__(self, storage_path):
        self.path = os.path.join(storage_path, "pano_id_log.csv")
        if exists(self.path):
            self.ids, self.prior_total, self.prior_success, self.prior_fail = progress_check(self.path)
        else:
            self.ids, self.prior_total, self.prior_success, self.prior_fail = set(), 0, 0, 0
        self._file = None
        self._writer = None

    def __enter__(self):
        ledger_existed = exists(self.path)
        self._file = open(self.path, 'a', newline='')
        # lineterminator='\n': csv.writer's excel default is '\r\n', but every existing image ledger was
        # written by pandas to_csv, whose default is os.linesep - '\n' on the Linux scraper boxes. Without
        # this pin, appending to a years-old ledger would mix line endings in one file and hand ops greps a
        # trailing '\r' on the downloaded column.
        self._writer = csv.writer(self._file, lineterminator='\n')
        if not ledger_existed:
            # Only a ledger this run CREATES gets the three-column header. An existing store keeps its
            # two-column one above three-field rows, which looks wrong to a person running `head -1` and is
            # correct anyway: rewriting a production ledger in place is the O(n^2) truncate-on-crash path
            # the docstring warns about, over a file that is the only record of what has been scraped.
            # progress_check reads by position and skips the header by value, so a stale one is inert.
            self._writer.writerow(['pano_id', 'downloaded', 'fetched_at'])
            self._file.flush()
            # Group-writable like depth_log.csv: other lab users' runs append to the same store.
            try:
                os.chmod(self.path, 0o664)
            except OSError:
                # Lost the exists()/open() race to another user's run: their file, their modes. The ledger is
                # already open and writable, so this must not take the phase down - the same call in both
                # downloaders' shard-dir setup swallows it for the same reason.
                pass
        return self

    def record(self, pano_id, downloaded, fetched_at):
        """Append one `pano_id,downloaded,fetched_at` row, flush it, and remember the id."""
        self._writer.writerow([pano_id, downloaded, fetched_at])
        self._file.flush()
        self.ids.add(pano_id)

    def __exit__(self, *exc_info):
        self._file.close()
        # Explicitly falsy: a truthy return would swallow SIGTERM's SystemExit(143) mid-phase, and the run would
        # carry on into the next phase and exit 0 (#49). The inline `with open(...)` this replaced could not.
        return False


# Consecutive permanent (downloaded=0) verdicts from ONE source that stop this run ledgering that source
# (#113). A source absent from this table has no breaker, which is the default and the common case.
#
# The failure this guards is not a property of any pano: a Mapillary token that has lost the needed scope
# would be answered the way Meta's Graph family commonly answers a permission-denied field - by OMITTING it
# from an otherwise-healthy 200 record - which is byte-for-byte the shape original_rendition_url is required
# to read as a permanent verdict (#99). No body check can separate the two, so the defence cannot be one; a
# breaker does not depend on knowing which way it goes. The cost of being wrong is asymmetric and measured:
# a permanent row is never revisited, and undoing 161 of them on 2026-09-01 meant hand-editing
# pano_id_log.csv on the shared store.
#
# Keyed on source rather than on the no-rendition verdict because the base rates differ by over two orders
# of magnitude. Measured over the production ledgers 2026-09-06: richmond-va, the only Mapillary city, has 0
# permanent verdicts in 9,229 rows - the whole corpus, after the 2026-09-05 catch-up - while the large GSV
# cities run 7.9-8.4% (seattle-wa 14,603/183,682, chicago-il 22,985/272,755), because retired imagery is
# permanent and ordinary. Rule of three bounds the Mapillary rate at 3/9,229 = 0.0325%, so the measured
# separation is 8.427/0.0325 = 259x. (It read "three orders" until the 2026-09-09 review; that would need the
# Mapillary rate under 0.0084%, which 9,229 rows cannot establish - it would take ~35,700 clean ones.)
# At 8.4% three in a row arrives about every 1,700 panos, so a source-blind breaker would stop a healthy GSV
# city roughly that often - most nights during a backfill, less for a mature city attempting fewer. Keying it this way also means a new source (#110) declares its
# own threshold rather than growing a second bespoke breaker.
#
# Three rather than one, for MAX_CONSECUTIVE_UNDERSIZED's reason (refetch_panos.py): one image legitimately
# without a rendition is not impossible, three in a row is not that. The loop shuffles its candidates, so
# even at a rate the measurement cannot rule out (under 0.033%, rule of three on 0/9,229) three adjacent
# legitimate verdicts is not a run this fleet will see.
#
# Panoramax (#110) takes an entry as of its first city, and is the source with the widest permanent-verdict
# exposure: three shapes found one, against Mapillary's one. Two of them are wholesale failures wearing a
# per-pano face. An affirmed non-360 field of view is refused because a flat picture stored as
# `<pano_id>.jpg` crops plausibly and wrongly - but 323 of the 1,000 pictures in the Bayonne bbox ARE flat
# 92-degree photographs (reports/2026-09-08-panoramax-api.md), and the only thing keeping them out of the
# corpus is that the app filters its own search to 360. That filter is a property of the layer above, which
# this scraper cannot verify; if it ever stops holding, every flat picture the app hands us is written off
# permanently. Likewise a missing `hd` asset, which one federated instance could stop publishing for all of
# its pictures at once. The candidates are shuffled, so at the ~32% flat rate a broken filter would produce
# would trip this within about ninety panos on the first night, and a trip is exit 1 and cron mail - the
# loud, correctable direction. Three legitimate ones adjacent in a shuffled healthy corpus is not.
MAX_CONSECUTIVE_PERMANENT_FAILURES = {'mapillary': 3, 'panoramax': 3}


def download_panorama_images(storage_path, pano_infos, run_start_monotonic=None, max_runtime_minutes=None,
                             tripped_sources=None, stop_reasons=None):
    """Download every eligible pano, ledgering each permanent verdict, and return the log.csv counters.

    @param tripped_sources An optional set the phase adds each breaker-tripped source to. An out-parameter
        rather than a sixth return value on purpose: the returned tuple IS log.csv fields 7-11 and
        log_analyzer reads those positionally, so widening it with a field that is not a log column is how a
        transposition gets introduced - the trap TestEveryDownloadResultLandsInItsOwnCounter exists for.
        (refetch_panos.refetch_pano takes `measurements` the same way, for the same reason.)
    @param stop_reasons An optional dict the phase records why it stopped early into, under 'image_stop' -
        STOP_MAX_RUNTIME, or left as None if it worked through its whole list. Same out-parameter reasoning
        as tripped_sources, and the depth phase fills 'depth_stop' in the same dict. scrape_queue reads it
        to decide who still has work (#43); nothing here or in log.csv depends on it.
    """
    success_count, skipped_count, fallback_success_count, fail_count, total_completed = 0, 0, 0, 0, 0

    # The attempted-pano ledger, in 'storage' alongside the pano results (see progress_check for semantics).
    # Reading it here; the append handle is opened by the `with` below.
    ledger = ImageLedger(storage_path)
    df_id_set, prior_total = ledger.ids, ledger.prior_total
    prior_success, prior_fail = ledger.prior_success, ledger.prior_fail
    # Seed counters from the log so "skipped" in the progress line includes panos already
    # downloaded on previous runs (same semantics as the original code).
    skipped_count = prior_success
    fail_count = prior_fail
    total_completed = prior_total
    # Partition before attempting anything, then shuffle - the depth phase's pattern (gsv.download_depth_maps).
    # Iteration order is otherwise the server's, and since #41 a transiently-failing pano is never ledgered, so
    # it keeps its place at the head of that order forever: a cluster of panos that fail every night would be
    # re-attempted first every night, spending --max-runtime before the loop ever reaches new work. Ledgering
    # every attempt used to guarantee the frontier advanced; nothing does now, so the shuffle has to.
    # This also covers the #40 fallback: if /adminapi/panos itself ever returns a source-clustered list,
    # filter_supported_sources preserving that order no longer starves the sources behind the first cluster.
    candidates = [p for p in pano_infos if p['pano_id'] not in df_id_set]
    # Denominator = previously logged + panos we'll attempt this run, so it can never be exceeded.
    total_panos = prior_total + len(candidates)
    random.shuffle(candidates)

    # Breaker state, per source and per run (#113). `tripped` is shared with the caller when it passed a set.
    consecutive_permanent = {}
    tripped = set() if tripped_sources is None else tripped_sources
    breaker_skipped = 0

    with ledger:
        for pano_info in candidates:
            pano_id = pano_info['pano_id']
            # candidates is already filtered against the ledger; this still catches a duplicate id surviving
            # intake, which would otherwise be downloaded and ledgered twice.
            if pano_id in df_id_set:
                continue
            source = pano_info.get('source')
            if source in tripped:
                # Not attempted, not counted, not ledgered: the breaker's whole point is that this source's
                # answers are not trustworthy tonight, so the pano comes back next run untouched (#41).
                breaker_skipped += 1
                continue
            if max_runtime_minutes is not None and run_start_monotonic is not None:
                # time.monotonic, not the wall clock: an NTP step or DST transition must not stretch or shrink
                # the budget (#51).
                elapsed_minutes = (time.monotonic() - run_start_monotonic) / 60.0
                if elapsed_minutes >= max_runtime_minutes:
                    print("IMAGEDOWNLOAD: Max runtime of %.1f minutes reached (%.1f elapsed). Stopping." % (max_runtime_minutes, elapsed_minutes))
                    # Recorded only HERE, where the phase actually gave up with panos still in its list -
                    # not wherever --max-runtime is merely set. A city that finished its list inside the
                    # budget must report no stop at all, or the queue re-runs it for nothing.
                    if stop_reasons is not None:
                        stop_reasons['image_stop'] = STOP_MAX_RUNTIME
                    break
            start_time = time.time()
            print("IMAGEDOWNLOAD: Processing pano %s " % (pano_id))
            try:
                result_code = download_pano(storage_path, pano_info)
                if result_code == DownloadResult.success:
                    success_count += 1
                elif result_code == DownloadResult.fallback_success:
                    fallback_success_count += 1
                elif result_code == DownloadResult.skipped:
                    skipped_count += 1
                elif result_code == DownloadResult.failure:
                    fail_count += 1
                downloaded = 0 if result_code == DownloadResult.failure else 1

            except Exception as e:
                # Transient (network, storage, a bug): counted in THIS run's failures but NOT ledgered, so
                # the pano is re-attempted next run - the depth ledger's semantics (#41). Only the
                # downloader's own verdict (DownloadResult.failure above: the source has nothing for this
                # pano) is permanent and writes the terminal 0-row.
                fail_count += 1
                downloaded = None
                result_code = None      # not a verdict, so the breaker below neither counts nor forgives it
                logging.error("IMAGEDOWNLOAD: Failed to download pano %s due to error %s", pano_id, str(e))

            limit = MAX_CONSECUTIVE_PERMANENT_FAILURES.get(source)
            if limit is not None:
                # `downloaded == 0` is exactly the permanent verdict. ONLY A REAL SUCCESS RESETS - not a
                # transient failure, and not a skip.
                #
                # A transient reset was the first version of this and it defeated the breaker on the exact
                # fault it was built for (2026-09-09 review). Mapillary answers "does not exist OR missing
                # permissions" with 400/100/33, which raises; and every retired image answers that way on
                # EVERY run, forever, because a transient is never ledgered and so is a candidate again the
                # next night. The scope-less token meanwhile produces the other shape, the omitted-field 200
                # that IS the permanent verdict. So in a mature city the candidate set is mostly retired
                # images shuffled uniformly among the live ones, every one of them resetting the count: the
                # run writes false permanent rows for most of the live panos and may never trip at all. The
                # bound the breaker advertises has to be a bound per run, and a raise is not evidence that
                # the source is answering honestly - it is no evidence about the source at all.
                #
                # A skip does not reset either, and for a stronger reason: it is os.path.isfile() returning
                # true, so the source was never contacted.
                if downloaded == 0:
                    consecutive_permanent[source] = consecutive_permanent.get(source, 0) + 1
                    if consecutive_permanent[source] >= limit:
                        tripped.add(source)
                        # Withheld deliberately, so tripping at 3 costs 2 false rows rather than 3: this
                        # verdict is the evidence that the source has stopped answering honestly, and writing
                        # off the pano that proved it is the one thing that must not happen.
                        downloaded = None
                        # Both channels, the depth phase's pattern: stdout is what cron mails tonight,
                        # scrape.log is what is still there next week when the ledger is being repaired.
                        logging.error("IMAGEDOWNLOAD: %d consecutive permanent failures from source %s. "
                                      "Stopping ledgering it for the rest of this run; its remaining panos "
                                      "are left unattempted and retry next run (#113).", limit, source)
                        print("IMAGEDOWNLOAD: WARNING - %d consecutive permanent failures from source %s. "
                              "That is a condition of the run, not of the panos, so nothing more from this "
                              "source is ledgered tonight." % (limit, source))
                elif result_code in (DownloadResult.success, DownloadResult.fallback_success):
                    consecutive_permanent[source] = 0
            total_completed = success_count + fallback_success_count + fail_count + skipped_count

            if downloaded is not None:
                # fetched_at goes LAST so the id and the verdict stay in columns 1 and 2, which is what every
                # `cut -d, -f1` / `-f2` and `awk -F, '$2 == 0'` in docs/ops.md reads. (A `grep ',0$'` does NOT
                # survive this - the row now ends in the stamp - which is why the docs no longer suggest one.)
                # It reuses log_timestamp for the same reason log.csv's column 1 does (#101): a stamp that
                # does not say which clock it is on is silently 7-8 hours out the moment a host is not on
                # UTC, and there is no second timestamp convention on this store to get that wrong differently.
                #
                # A skip gets a BLANK stamp. `skipped` is os.path.isfile() returning true: the pixels were
                # fetched by some earlier run whose row is missing - one killed between the atomic save and
                # this append, a ledger deleted as the force-retry lever docs/ops.md describes, a torn row the
                # reader dropped, a store assembled by copy. The only evidence of WHEN is the file's mtime,
                # and this row is never rewritten, so stamping now would relabel a 2019 fetch as today's for
                # ever - on exactly the question (which rendering is this?) the column exists to answer. Blank
                # means unknown, the same thing a two-field row from before #114 means.
                fetched_at = '' if result_code == DownloadResult.skipped else log_timestamp()
                ledger.record(pano_id, downloaded, fetched_at)

            print("IMAGEDOWNLOAD: Completed %d of %d (%d success, %d fallback success, %d failed, %d skipped)"
                  % (total_completed, total_panos, success_count, fallback_success_count, fail_count, skipped_count))
            print("--- %s seconds ---" % (time.time() - start_time))

    if tripped:
        # Both channels, like the per-trip message above: this is the half that carries the unattempted count
        # and the repair pointer, which is exactly what someone needs a week later reading scrape.log while
        # editing the ledger. It was print-only until the 2026-09-09 review.
        summary = ("IMAGEDOWNLOAD: WARNING - breaker tripped for %s; %d pano(s) were left unattempted and "
                   "nothing was ledgered for them, so they retry next run. Check that source's credentials "
                   "before the next run, then look for false downloaded=0 rows in pano_id_log.csv."
                   % (', '.join(sorted(tripped)), breaker_skipped))
        logging.error("%s", summary)
        print(summary)

    logging.debug(
        "IMAGEDOWNLOAD: Final result: Completed %d of %d (%d success, %d fallback success, %d failed, %d skipped)",
        total_completed,
        total_panos,
        success_count,
        fallback_success_count,
        fail_count,
        skipped_count)

    return success_count, fallback_success_count, fail_count, skipped_count, total_completed


def _budget_spent(run_start_monotonic, max_runtime_minutes, prefix):
    """True (and says so on stdout) once --max-runtime is spent, on the monotonic clock (#51)."""
    if max_runtime_minutes is None or run_start_monotonic is None:
        return False
    elapsed_minutes = (time.monotonic() - run_start_monotonic) / 60.0
    if elapsed_minutes < max_runtime_minutes:
        return False
    print("%s: Max runtime of %.1f minutes reached (%.1f elapsed). Stopping."
          % (prefix, max_runtime_minutes, elapsed_minutes))
    return True


def _pull_in_batches(settings, storage_path, pano_ids, suffix, verifier, run_start_monotonic, max_runtime_minutes,
                     tripped, stop_reasons, stop_key, prefix):
    """Yield (chunk, {pano_id: PullOutcome}) one sftp session at a time - the store pull's loop (#30).

    Written once for both passes, so the budget check and the session-failure handling cannot drift apart.

    The budget is checked BEFORE each batch and never inside one, so a batch in flight overruns --max-runtime
    by at most store_sftp.BATCH_SIZE transfers. A stop is recorded in stop_reasons[stop_key] only here, where
    the pass gave up with ids still in its list - the image loop's rule.

    A StoreSessionError is a condition of the run, not of any pano (auth, host, host key, the cd probe): the
    pass stops with the rest unattempted, uncounted and unledgered, says so on BOTH channels - stdout is what
    cron mails tonight, scrape.log is what is still there next week - and adds store_sftp.STORE_SOURCE_NAME
    to `tripped`, which main() turns into exit 1. The message is already redacted by store_sftp.
    """
    for start in range(0, len(pano_ids), store_sftp.BATCH_SIZE):
        if _budget_spent(run_start_monotonic, max_runtime_minutes, prefix):
            if stop_reasons is not None:
                stop_reasons[stop_key] = STOP_MAX_RUNTIME
            return
        chunk = pano_ids[start:start + store_sftp.BATCH_SIZE]
        try:
            outcomes = store_sftp.pull_batch(settings, storage_path, chunk, suffix, verifier)
        except store_sftp.StoreSessionError as e:
            message = ("%s: WARNING - the pano store session failed, so this pass stopped with %d pano(s) "
                       "unattempted; nothing was ledgered for them and they retry next run. Check the "
                       "PS_SFTP_* settings and the city id. sftp said: %s"
                       % (prefix, len(pano_ids) - start, e))
            logging.error("%s", message)
            print(message)
            tripped.add(store_sftp.STORE_SOURCE_NAME)
            return
        yield chunk, outcomes


def _report_pull_trouble(prefix, noun, outcome_counts):
    """The closing lines for outcomes an operator must hear about tonight, on BOTH channels (#155 review
    item 14): stdout is the night's message, scrape.log is what is still there next week. Per-pano detail
    stays in scrape.log only. `unplaced` is the one a retry will not fix - it is local storage (a full disk,
    a dropped mount) - so a run where every pano turned `unplaced` must not read as a quiet "N failed"."""
    messages = {
        store_sftp.PullOutcome.truncated:
            "%s: WARNING - %d %s arrived incomplete or unreadable and were discarded; not ledgered, retried "
            "next run; see scrape.log",
        store_sftp.PullOutcome.unplaced:
            "%s: WARNING - %d %s verified but could not be placed on local storage (disk full? mount "
            "dropped?); not ledgered, retried next run; see scrape.log",
    }
    for outcome, template in messages.items():
        if outcome_counts.get(outcome):
            message = template % (prefix, outcome_counts[outcome], noun)
            logging.warning("%s", message)
            print(message)


def _partition_for_pull(storage_path, pano_ids, suffix, prefix):
    """Split ids into (to_pull, already_local, unsafe) without touching the network.

    A file already on disk is decided before any batching, so an already-complete store costs zero sessions.
    An id the batch cannot carry (store_sftp.is_batch_safe_id) is logged and never reaches sftp.
    """
    to_pull, local, unsafe = [], [], []
    for pano_id in pano_ids:
        if not store_sftp.is_batch_safe_id(pano_id):
            logging.warning("%s: pano %r skipped - its id cannot be written into an sftp batch", prefix, pano_id)
            unsafe.append(pano_id)
        elif os.path.isfile(store_sftp.local_final_path(storage_path, pano_id, suffix)):
            local.append(pano_id)
        else:
            to_pull.append(pano_id)
    return to_pull, local, unsafe


def pull_panos_from_store(storage_path, pano_infos, settings, run_start_monotonic=None, max_runtime_minutes=None,
                          tripped_sources=None, stop_reasons=None):
    """Store-mode twin of download_panorama_images (#30): copy each pano off the Project Sidewalk store.

    Returns the same 5-tuple - log.csv fields 7-11 - with fallback_success fixed at 0, and seeds its counters
    from the ledger exactly as the image loop does.

    Ledger semantics, the part that must not drift: a pulled pano and one already on disk are ledgered
    downloaded=1; NOTHING else is ever ledgered. A pano the store does not hold tonight (absent), a transfer
    that failed verification (truncated), one that could not be placed, and an id the batch cannot carry
    are counted in this run's failures and left without a row, because the nightly scrape may add the pano
    tomorrow - "not on the store" is a fact about tonight, not about the pano. `fetched_at` is blank on every
    row: the provider was not contacted, and when it last was is not something the pull knows (docs/ops.md).

    MAX_CONSECUTIVE_PERMANENT_FAILURES is not consulted: there is no permanent verdict here for it to count.
    A failed SESSION stops the phase and adds store_sftp.STORE_SOURCE_NAME to tripped_sources (see
    _pull_in_batches). Candidates are shuffled before chunking, the image loop's reasoning: an absent pano is
    unledgered, so a stable order would put the same absent block in the first batch every night.
    """
    tripped = set() if tripped_sources is None else tripped_sources
    ledger = ImageLedger(storage_path)
    success_count = 0
    skipped_count = ledger.prior_success
    fail_count = ledger.prior_fail
    outcome_counts = collections.Counter()

    # dict.fromkeys: order-preserving dedupe, so a duplicate id surviving intake is pulled and ledgered once.
    candidate_ids = [pano_id for pano_id in dict.fromkeys(p['pano_id'] for p in pano_infos)
                     if pano_id not in ledger.ids]
    total_panos = ledger.prior_total + len(candidate_ids)
    to_pull, local, unsafe = _partition_for_pull(storage_path, candidate_ids, store_sftp.IMAGE_SUFFIX,
                                                 'STOREPULL')
    fail_count += len(unsafe)
    random.shuffle(to_pull)

    with ledger:
        for pano_id in local:
            # The scrape's `skipped`: the file is the resume marker, so register it with a blank stamp.
            skipped_count += 1
            ledger.record(pano_id, 1, '')

        for chunk, outcomes in _pull_in_batches(settings, storage_path, to_pull, store_sftp.IMAGE_SUFFIX,
                                                store_sftp.is_complete_jpeg, run_start_monotonic,
                                                max_runtime_minutes, tripped, stop_reasons, 'image_stop',
                                                'STOREPULL'):
            for pano_id in chunk:
                outcome = outcomes[pano_id]
                if outcome == store_sftp.PullOutcome.pulled:
                    success_count += 1
                    ledger.record(pano_id, 1, '')
                    logging.info("STOREPULL: pano %s pulled", pano_id)
                else:
                    fail_count += 1
                    outcome_counts[outcome] += 1
                    logging.warning("STOREPULL: pano %s %s; not ledgered, retried next run", pano_id,
                                    outcome.value)
            print("STOREPULL: Completed %d of %d (%d pulled, %d failed, %d skipped)"
                  % (success_count + fail_count + skipped_count, total_panos, success_count, fail_count,
                     skipped_count))

    total_completed = success_count + fail_count + skipped_count
    if outcome_counts[store_sftp.PullOutcome.absent]:
        message = ("STOREPULL: %d pano(s) not on the store tonight; not ledgered, retried next run"
                   % outcome_counts[store_sftp.PullOutcome.absent])
        logging.warning("%s", message)
        print(message)
    _report_pull_trouble('STOREPULL', 'pano(s)', outcome_counts)
    if unsafe:
        message = ("STOREPULL: WARNING - %d pano id(s) cannot be written into an sftp batch and were skipped; "
                   "see scrape.log" % len(unsafe))
        logging.warning("%s", message)
        print(message)
    logging.debug("STOREPULL: Final result: Completed %d of %d (%d pulled, %d failed, %d skipped)",
                  total_completed, total_panos, success_count, fail_count, skipped_count)
    return success_count, 0, fail_count, skipped_count, total_completed


def pull_depth_from_store(storage_path, gsv_pano_infos, settings, run_start_monotonic=None,
                          max_runtime_minutes=None, tripped_sources=None, stop_reasons=None):
    """--with-depth (#30): pull the stored .depth.npz for every GSV pano lacking one locally.

    Returns (pulled, failed, skipped, total) - log.csv fields 13-16. Covers the whole GSV corpus it is
    given (the depth phase's view: not narrowed by --all-panos, not tied to tonight's image candidates), so a
    re-run with --with-depth after an image-only pull still fetches the artifacts.

    Reads and writes NO depth_log.csv. None is needed: gsv.download_depth_maps ledgers an artifact it finds
    on disk without a row as `saved`, at zero requests, so a later Google-mode run reconciles for free. An
    absent artifact is expected (Google had no depth, so the store has none) and counts in field 14, which
    docs/ops.md already says is not an alert signal.
    """
    tripped = set() if tripped_sources is None else tripped_sources
    pano_ids = list(dict.fromkeys(p['pano_id'] for p in gsv_pano_infos))
    to_pull, local, unsafe = _partition_for_pull(storage_path, pano_ids, store_sftp.DEPTH_SUFFIX, 'STOREDEPTH')
    random.shuffle(to_pull)
    pulled, failed = 0, len(unsafe)
    outcome_counts = collections.Counter()
    for chunk, outcomes in _pull_in_batches(settings, storage_path, to_pull, store_sftp.DEPTH_SUFFIX,
                                            store_sftp.is_complete_npz, run_start_monotonic, max_runtime_minutes,
                                            tripped, stop_reasons, 'depth_stop', 'STOREDEPTH'):
        for pano_id in chunk:
            outcome = outcomes[pano_id]
            if outcome == store_sftp.PullOutcome.pulled:
                pulled += 1
                logging.info("STOREDEPTH: pano %s depth artifact pulled", pano_id)
            else:
                failed += 1
                outcome_counts[outcome] += 1
                logging.info("STOREDEPTH: pano %s depth artifact %s", pano_id, outcome.value)
        print("STOREDEPTH: Completed %d of %d (%d pulled, %d not pulled, %d skipped)"
              % (pulled + failed + len(local), len(pano_ids), pulled, failed, len(local)))
    # An absent artifact is expected (Google had no depth), so only the two troubles get a closing line.
    _report_pull_trouble('STOREDEPTH', 'artifact(s)', outcome_counts)
    return pulled, failed, len(local), pulled + failed + len(local)


# Fields per log.csv row: timestamp, 5 xml-stub, 6 image, 5 depth, 1 total duration, then the depth corpus
# size (#43). Positional, parsed by our log-analyzer tooling. The full column table lives in docs/ops.md.
LOG_CSV_FIELD_COUNT = 19

# 1-based position of the depth corpus size - the number of GSV panos the depth phase was given. It is the one
# number the analyzer cannot derive from the other 18: field 16 says how many panos are resolved, and only this
# says out of how many, which is what a backfill's progress and ETA are computed from. Appended at the END of
# the row so no existing position moves, and written from the finally rather than after the depth phase because
# it is known before any phase runs - so a crashed run still records it, and only a run that died in the
# pano-list fetch itself leaves it blank.
DEPTH_ELIGIBLE_FIELD = 19


def log_timestamp(now=None):
    """This run's start_time for log.csv: ISO-8601 local time carrying an explicit UTC offset.

    It was `str(datetime.now())` - host-local time with nothing to say which clock that is (#101). That was
    only harmless while every scraper host ran UTC. The moment the schedule is pinned to a real timezone, or
    a host moves, every consumer that treats the column as UTC is silently off by the offset:
    log_analyzer.analyze compares it against `datetime.now(timezone.utc)`, and a 7-8 hour error is invisible
    at a multi-day staleness threshold right up until it isn't. A log line that does not say which clock it
    is on is also the direct reason a 13:30-Pacific slot read as "runs at 20:30, seems fine" for months.

    Always rendered through .astimezone(), so a naive argument picks up the host's offset and an aware one is
    converted rather than silently written without one - there is no way to call this and get a timestamp
    that omits the clock.

    Two deliberate details of the format:
      - space separator, not 'T', so the column stays byte-compatible with every `cut -d, -f1` and eyeball
        that reads it today. pandas' format="ISO8601" accepts either, and so does datetime.fromisoformat.
      - timespec='microseconds', so every row is the same width. str(datetime) omits ".ffffff" when the
        microsecond lands on exactly 0, which is why a long-lived log holds two widths and why read_log has
        to pin format="ISO8601" rather than let pandas infer. Fixing that at the source costs nothing here.
    """
    return (datetime.now() if now is None else now).astimezone().isoformat(sep=' ', timespec='microseconds')


def _duration_minutes(start_monotonic, end_monotonic):
    """A phase's log.csv duration, in whole minutes, measured on the monotonic clock.

    These were differences of wall-clock datetime.now() readings. That is safe on a UTC host, and stops being
    safe the moment the schedule is pinned to a DST-observing timezone (#101): the Pacific night window the
    queue runs in contains 02:00 local, so twice a year a run that spans the transition records a duration an
    hour out. log_analyzer's rule 4 flags a run over 3x the median, so the November transition would have
    produced a fleet-wide burst of "abnormally long run" warnings with nothing actually wrong. The budgets
    already moved to time.monotonic() for exactly this reason (#51); the durations they are compared against
    had not.
    """
    return int(round((end_monotonic - start_monotonic) / 60.0))


def write_log_csv_row(storage_location, fields):
    """Append one run's row to <storage_location>/log.csv, blank-padded to the full LOG_CSV_FIELD_COUNT columns.

    Blank means the phase never finished - visibly missing data, not a fake zero. If the append itself fails
    (the classic cause: the sshfs store went away mid-run), the joined row is printed to stderr before the
    exception escapes, so this run's counts survive somewhere cron can mail.
    """
    assert len(fields) <= LOG_CSV_FIELD_COUNT, \
        "log.csv row has %d fields, more than the %d the analyzer parses: %r" \
        % (len(fields), LOG_CSV_FIELD_COUNT, fields)
    row = ",".join(str(f) for f in fields + [''] * (LOG_CSV_FIELD_COUNT - len(fields)))
    try:
        with open(os.path.join(storage_location, "log.csv"), 'a') as log:
            log.write("\n" + row)
    except BaseException:
        print("Failed to append this run's row to log.csv; it was: %s" % row, file=sys.stderr)
        raise


def run_scraper_and_log_results(storage_location, image_pano_infos, depth_pano_infos, skip_depth,
                                max_runtime_minutes=None, max_depth_requests=None, min_depth_runtime=0.0,
                                depth_block_latch=None, depth_pace_state=None, stop_reasons=None,
                                store_settings=None, with_depth=False):
    """Run the image and depth phases and append this run's row to log.csv.

    Fields are accumulated as each phase completes and the row is written once, in a finally, padded to the
    full width with blanks. A crash mid-run therefore still yields a parseable full-width line that keeps every
    completed phase's counts (a failure in the depth phase must not discard what the image phase downloaded),
    while the phases that never finished stay visibly blank rather than turning into fake zeros (#49). The
    one field that is not a phase result - the depth corpus size, DEPTH_ELIGIBLE_FIELD - is known up front
    and lands on every row that gets this far, crashed or not.

    @param storage_location Root of the pano store (log.csv and the ledgers live here).
    @param image_pano_infos Panos eligible for image download (narrowed by --all-panos).
    @param depth_pano_infos Every supported pano; the depth phase filters this to source == 'gsv' itself.
    @param min_depth_runtime Minutes of max_runtime_minutes reserved for the depth phase (see the flag's help).
    @param stop_reasons An optional dict each phase records what stopped it into ('image_stop', 'depth_stop').
        Both keys are seeded to None up front, so "the phase ran and nothing stopped it" is distinguishable
        from "the phase never got to run" - which is the whole distinction scrape_queue's extra passes turn
        on (#43).
    @param store_settings A store_sftp.StoreSettings to run in store mode (#30): the image phase becomes
        pull_panos_from_store, the reservation is not taken, and the depth phase never contacts Google - it
        is (0, 0, 0, 0), or pull_depth_from_store when with_depth is set. The row's layout is unchanged.
    @return The set of sources whose breaker tripped this run (#113), empty when none did - or
        store_sftp.STORE_SOURCE_NAME when a store-mode session failed (#30), the same alarm for the same
        reason: the run stopped trusting where its imagery comes from. It rides back rather than into
        log.csv: the row's fields are counts of work, parsed by position, and an alarm is not one - the exit
        code already delivers it through scrape_queue and cron mail. (Field 19, the depth corpus size, IS a
        count of work, which is why it went into the row and the breaker did not.)
    """
    # The wall clock supplies the one thing it is good for - when this run happened, stamped with its offset
    # so a reader knows which clock that is (#101). Everything measuring an INTERVAL - the budgets (#51) and
    # every duration column - reads time.monotonic() instead, so an NTP step or a DST transition can neither
    # stretch a budget nor invent an hour of runtime.
    start_time = datetime.now()
    run_start_monotonic = time.monotonic()

    # Seeded before anything can stop: a key present and None means "this phase finished its list", which is
    # what tells scrape_queue the city is DONE rather than merely unobserved.
    if stop_reasons is not None:
        stop_reasons.setdefault('image_stop', None)
        stop_reasons.setdefault('depth_stop', None)

    # Depth maps are GSV-only; the depth phase's view of the corpus is computed up front because the budget
    # split below needs it too - and because its size is log.csv's last field (#43): the denominator every
    # progress figure for the backfill needs, and the one number nothing else in the row carries.
    gsv_panos = [p for p in depth_pano_infos if p.get('source') == 'gsv']
    depth_eligible = len(gsv_panos)

    # Both phases share --max-runtime (it exists to keep the run inside its daily cron slot, per #38, and that
    # constraint doesn't care which phase spends the clock), but the image phase must leave the reserved tail so
    # an image backlog — a mapathon is the canonical case — can't starve the depth backfill night after night
    # (#43). The reservation is only taken when the depth ledger shows unresolved work: once a city is fully
    # backfilled the depth phase returns in milliseconds, and reserving for it would burn image throughput for
    # nothing. Depth still ends at the total, so slack from a light image night rolls to depth rather than being
    # lost. No reservation when depth is skipped: the image phase keeps the whole window.
    image_max_runtime = max_runtime_minutes
    if store_settings is None and max_runtime_minutes is not None and not skip_depth and min_depth_runtime > 0:
        depth_backlog = gsv.count_unresolved_depth(storage_location, gsv_panos)
        if depth_backlog:
            image_max_runtime = max(0.0, max_runtime_minutes - min_depth_runtime)
            print("Budget: %.1f min total; image phase capped at %.1f min (%.1f min reserved for depth; "
                  "backlog: %d panos)"
                  % (max_runtime_minutes, image_max_runtime, min_depth_runtime, depth_backlog))
            if image_max_runtime == 0.0 and max_runtime_minutes > 0:
                # Loud on stdout because cron mails it: without this, a zero-image night reads like ordinary
                # budget exhaustion and a misconfigured fleet would silently stop downloading images.
                print("WARNING: --min-depth-runtime (%g) >= --max-runtime (%g); NO images will be downloaded "
                      "this run" % (min_depth_runtime, max_runtime_minutes))
        else:
            print("Budget: no unresolved depth work; image phase gets the full %.1f min"
                  % (max_runtime_minutes,))

    fields = [log_timestamp(start_time)]
    tripped_sources = set()
    try:
        # There is no XML metadata phase (that endpoint died in 2022; depth now comes from streetlevel below),
        # but its log.csv columns are stubbed with the values every production run has always written so the
        # positional format parsed by scraper-log-analyzer doesn't shift. Deliberately the image
        # list's length, which is what this counted before depth stopped honouring --all-panos.
        xml_res = (0, 0, len(image_pano_infos), len(image_pano_infos))
        xml_end_monotonic = time.monotonic()
        fields += [xml_res[0], xml_res[1], xml_res[2], xml_res[3],
                   _duration_minutes(run_start_monotonic, xml_end_monotonic)]

        # The budget arguments are passed by keyword deliberately: several changes have rewritten these call
        # sites, and a positional resolution can put a datetime where a monotonic float belongs — a TypeError
        # that only fires when --max-runtime is set, i.e. in the nightly cron and never in the suite.
        if store_settings is not None:
            im_res = pull_panos_from_store(storage_location, image_pano_infos, store_settings,
                                           run_start_monotonic=run_start_monotonic,
                                           max_runtime_minutes=image_max_runtime,
                                           tripped_sources=tripped_sources,
                                           stop_reasons=stop_reasons)
        else:
            im_res = download_panorama_images(storage_location, image_pano_infos,
                                              run_start_monotonic=run_start_monotonic,
                                              max_runtime_minutes=image_max_runtime,
                                              tripped_sources=tripped_sources,
                                              stop_reasons=stop_reasons)
        im_end_monotonic = time.monotonic()
        fields += [im_res[0], im_res[1], im_res[2], im_res[3], im_res[4],
                   _duration_minutes(xml_end_monotonic, im_end_monotonic)]

        # The depth phase runs after the image phase and ends at the shared --max-runtime, so it gets the
        # reserved tail (when one was taken) plus whatever slack the image phase left. It iterates the full
        # pano list — not the pano_id_log.csv-gated image loop, and not narrowed by --all-panos — which is what
        # backfills depth for panos downloaded in earlier runs and for panos nobody has labelled.
        if store_settings is not None:
            # Store mode never contacts Google (#30). A session failure in the image pass already tripped the
            # run; the depth pass would only fail the same way, so it is not attempted.
            if with_depth and store_sftp.STORE_SOURCE_NAME not in tripped_sources:
                depth_res = pull_depth_from_store(storage_location, gsv_panos, store_settings,
                                                  run_start_monotonic=run_start_monotonic,
                                                  max_runtime_minutes=max_runtime_minutes,
                                                  tripped_sources=tripped_sources,
                                                  stop_reasons=stop_reasons)
            else:
                depth_res = (0, 0, 0, 0)
        elif skip_depth:
            depth_res = (0, 0, 0, 0)
        else:
            depth_res = gsv.download_depth_maps(storage_location, gsv_panos,
                                                run_start_monotonic=run_start_monotonic,
                                                max_runtime_minutes=max_runtime_minutes,
                                                max_requests=max_depth_requests,
                                                block_latch_path=depth_block_latch,
                                                pace_state_path=depth_pace_state,
                                                stop_reasons=stop_reasons)
        depth_end_monotonic = time.monotonic()
        fields += [depth_res[0], depth_res[1], depth_res[2], depth_res[3],
                   _duration_minutes(im_end_monotonic, depth_end_monotonic)]

        fields.append(_duration_minutes(run_start_monotonic, depth_end_monotonic))
    finally:
        # Whatever the phases managed to record, then blanks up to the corpus-size field, then the corpus size
        # itself. On a completed run the padding is empty; on a crashed one it is the unfinished phases.
        fields += [''] * (DEPTH_ELIGIBLE_FIELD - 1 - len(fields))
        fields.append(depth_eligible)
        write_log_csv_row(storage_location, fields)
    return tripped_sources


def _write_run_summary(path, stop_reasons):
    """Write the queue's run summary, or warn and carry on.

    Evidence, not cargo - the same trade configure_logging makes. scrape_queue falls back to its old
    elapsed-time heuristic when no summary arrives, so a temp file that could not be written costs one
    imprecise re-run decision; raising here would cost the whole night's scrape.
    """
    try:
        with open(path, 'w') as f:
            json.dump(stop_reasons, f, allow_nan=False)
    except OSError as e:
        logging.warning("Could not write the run summary to %s (%s)", path, e)
        print("WARNING: could not write the run summary to %s (%s)" % (path, e))


def run(sidewalk_server_fqdn, storage_location, pano_metadata_csv=None, all_panos=False, skip_depth=False,
        max_runtime_minutes=None, min_depth_runtime=0.0, max_depth_requests=None, depth_block_latch=None,
        depth_pace_state=None, run_summary_path=None, store_settings=None, with_depth=False):
    """Fetch the pano list, narrow it, and run the scrape - the whole job, minus process-level setup.

    main() owns argv parsing, directory creation, logging, and signal handling; this seam takes plain
    arguments (defaults mirror the flags') so tests can drive the real fetch -> filter -> phase orchestration
    in-process (#52.1).

    @param store_settings A store_sftp.StoreSettings for store mode (#30); None (the default) scrapes the
        imagery providers as always.
    @return run_scraper_and_log_results' set of breaker-tripped sources, which main() turns into its exit
        code (#113).
    """
    # Seeded here rather than inside run_scraper_and_log_results, so a crash in the pano-list fetch below
    # still leaves the queue a summary saying no phase stopped on a budget - which is true, and is what
    # keeps a webserver outage from reading as "this city has a backlog" on elapsed time alone.
    stop_reasons = {'image_stop': None, 'depth_stop': None}
    try:
        return _run_phases(sidewalk_server_fqdn, storage_location, pano_metadata_csv, all_panos, skip_depth,
                           max_runtime_minutes, min_depth_runtime, max_depth_requests, depth_block_latch,
                           depth_pace_state, stop_reasons, store_settings, with_depth)
    finally:
        if run_summary_path is not None:
            _write_run_summary(run_summary_path, stop_reasons)


def _run_phases(sidewalk_server_fqdn, storage_location, pano_metadata_csv, all_panos, skip_depth,
                max_runtime_minutes, min_depth_runtime, max_depth_requests, depth_block_latch,
                depth_pace_state, stop_reasons, store_settings=None, with_depth=False):
    """run()'s body, minus the run-summary bookkeeping its finally owns."""
    if store_settings is not None:
        # The city id, never the host: connection details stay out of stdout and scrape.log (#30). Both
        # channels, because a store row in log.csv is not distinguishable from a scrape's and this line is the
        # record of which store city the run pulled - scrape.log is where that is still readable next week.
        banner = ("Store mode: pulling already-scraped panoramas for %s from the Project Sidewalk pano store "
                  "(no request goes to Google, Mapillary or Panoramax)" % store_settings.remote_city)
        logging.info("%s", banner)
        print(banner)
    # Access Project Sidewalk API to get Pano IDs for city
    print("Fetching pano-ids")

    try:
        if pano_metadata_csv is not None:
            pano_infos = fetch_pano_ids_csv(pano_metadata_csv)
        else:
            pano_infos = fetch_pano_ids_from_webserver(sidewalk_server_fqdn)
        pano_infos = filter_supported_sources(pano_infos, require_credentials=store_settings is None)
        image_pano_infos = select_image_panos(pano_infos, all_panos)
    except BaseException:
        # A crash before the scrape starts - a webserver outage being the single most likely nightly failure -
        # must still leave both kinds of evidence (#49): the traceback in scrape.log, and a blank-padded
        # log.csv row whose real timestamp shows a run started and produced nothing.
        logging.exception("Run crashed before the scrape started")
        write_log_csv_row(storage_location, [log_timestamp()])
        raise

    # Uncomment this to test on a smaller subset of the pano_info.
    # import random
    # n = 3
    # if len(pano_infos) > n:
    #     pano_infos = random.sample(pano_infos, n)

    # In store mode without --with-depth no depth pass runs at all, so say so rather than read as if one will.
    depth_note = ' (not pulled: no --with-depth)' if store_settings is not None and not with_depth else ''
    print("Panos: %d supported, %d eligible for image download, %d GSV panos eligible for depth%s"
          % (len(pano_infos), len(image_pano_infos), sum(1 for p in pano_infos if p.get('source') == 'gsv'),
             depth_note))

    # Use pano_id list and associated info to gather panos from respective APIs
    print("Fetching Panoramas")
    try:
        tripped_sources = run_scraper_and_log_results(
            storage_location, image_pano_infos, pano_infos, skip_depth,
            max_runtime_minutes=max_runtime_minutes,
            max_depth_requests=max_depth_requests, min_depth_runtime=min_depth_runtime,
            depth_block_latch=depth_block_latch, depth_pace_state=depth_pace_state, stop_reasons=stop_reasons,
            store_settings=store_settings, with_depth=with_depth)
    except BaseException:
        # run_scraper_and_log_results's own finally has already written the evidence row; this puts the
        # traceback - otherwise stderr-only, the exact channel that dies with the container - into scrape.log
        # too (#49).
        logging.exception("Run failed")
        raise
    return tripped_sources


def main(argv=None):
    """Process-level setup, then run(): everything a `python3 DownloadRunner.py ...` invocation does.

    Exceptions propagate (the interpreter prints the traceback and exits 1) and argparse errors exit 2,
    exactly as the pre-#52 module-scope script behaved.

    Returns 1 when an image-source breaker tripped (#113) and 0 otherwise, so a run that stopped trusting a
    source reads as a failed city to scrape_queue.py and reaches cron's mail-on-failure. Returned rather
    than exited, so tests can drive the whole flow in-process - the shape scrape_queue.main and
    CropRunner.main already use.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    store_settings = _resolve_store_mode(parser, args)

    if args.attempt_depth:
        print("WARNING: --attempt-depth is deprecated and ignored; depth download is now on by default "
              "(use --skip-depth to disable).")

    # min_depth_runtime > 0 implies the operator typed the flag (the default is 0), so tell them when the
    # combination they ran it in means it cannot do anything.
    if store_settings is None and args.min_depth_runtime > 0 and (args.max_runtime is None or args.skip_depth):
        print("WARNING: --min-depth-runtime has no effect %s; no time will be reserved for the depth phase."
              % ("with --skip-depth" if args.skip_depth else "without --max-runtime"))

    # Process-level policy, the same one CropRunner sets: the Mapillary display copy (#115) re-opens the
    # panorama it just wrote, and a Mapillary equirect over Pillow's 89 MP default would otherwise warn on
    # every one - or, past 2x that, raise, which _write_display_copy swallows into "no sidecar, ever" for
    # exactly the widest images. The GSV path never needs it (its raster is built in memory, not decoded).
    #
    # Kept although common.WRITE_DISPLAY_COPIES is off (2026-09-09), so this is currently the policy for a
    # path nothing takes. It is correct either way and is needed the moment the switch is flipped; removing
    # it would make re-enabling the copy a two-file change whose second file is easy to miss, and the failure
    # it prevents is silent - a warning per pano, then no sidecar on precisely the widest images.
    raise_decompression_bomb_ceiling()

    # exist_ok: concurrent city runs (or the operator pre-creating the dir) race on the exists check.
    os.makedirs(args.s, exist_ok=True)

    # scrape.log lives on the pano store next to log.csv, NOT the CWD: cron runs this from whatever directory
    # it likes, so a relative path scatters the log - and every per-pano failure detail - somewhere nobody
    # looks (#49; it was worse under the old Docker image, where the CWD died with the container). Configured
    # once here at startup so every part of the run logs to the same file - including a crash in the pano-list
    # fetch, which happens before any phase's own code gets a chance to run.
    configure_logging(os.path.join(args.s, 'scrape.log'))

    # A stop - `systemctl stop`, a cron timeout wrapper, an operator's kill - sends SIGTERM, which CPython by
    # default dies from without running finally blocks, taking the log.csv evidence row with it (#49).
    # Translate it into a SystemExit carrying the conventional 128+15 code, so cleanup runs and the exit still
    # reads as a signal death.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    print("Starting run with pano list fetched from %s and destination path %s" % (args.d, args.s))

    tripped_sources = run(sidewalk_server_fqdn=args.d, storage_location=args.s, pano_metadata_csv=args.c,
                          all_panos=args.all_panos, skip_depth=args.skip_depth,
                          max_runtime_minutes=args.max_runtime, min_depth_runtime=args.min_depth_runtime,
                          max_depth_requests=args.max_depth_requests,
                          depth_block_latch=args.depth_block_latch, depth_pace_state=args.depth_pace_state,
                          run_summary_path=args.run_summary_file, store_settings=store_settings,
                          with_depth=args.with_depth)
    return 1 if tripped_sources else 0


# The depth flags store mode ignores, as (attribute, flag, default). Store mode never contacts Google, so none
# of them can do anything; each one given says so rather than being silently dropped.
_DEPTH_ONLY_FLAGS = (('skip_depth', '--skip-depth', False), ('min_depth_runtime', '--min-depth-runtime', 0.0),
                     ('max_depth_requests', '--max-depth-requests', None),
                     ('depth_block_latch', '--depth-block-latch', None),
                     ('depth_pace_state', '--depth-pace-state', None))
_SFTP_FLAGS = (('sftp_host', '--sftp-host'), ('sftp_base', '--sftp-base'), ('sftp_user', '--sftp-user'),
               ('sftp_port', '--sftp-port'), ('sftp_key', '--sftp-key'))


def _resolve_store_mode(parser, args):
    """Validate the store-mode flags (#30) and return a store_sftp.StoreSettings, or None outside store mode.

    Runs before the storage dir, scrape.log or the SIGTERM handler exist, so missing connection settings are
    a usage error (exit 2, naming the PS_SFTP_* variables) and leave nothing behind. Store mode implies
    --skip-depth; args.skip_depth is set here so every later reader agrees.
    """
    if args.from_store is None:
        if args.with_depth:
            parser.error("--with-depth pulls depth artifacts from the pano store and needs --from-store")
        for attr, flag in _SFTP_FLAGS:
            if getattr(args, attr) is not None:
                # The flag's name only - its value may be a host.
                print("WARNING: %s has no effect without --from-store" % flag)
        return None

    try:
        settings = store_sftp.resolve_settings(args.from_store, host=args.sftp_host, base=args.sftp_base,
                                               user=args.sftp_user, port=args.sftp_port, key=args.sftp_key)
        # Here, not first inside the phase: there it is a traceback after the storage dir and scrape.log exist.
        store_sftp.check_storage_path(args.s)
    except ValueError as e:
        parser.error(str(e))
    for attr, flag, default in _DEPTH_ONLY_FLAGS:
        if getattr(args, attr) != default:
            print("WARNING: %s has no effect in store mode (Google is never contacted)" % flag)
    args.skip_depth = True
    return settings


if __name__ == '__main__':
    sys.exit(main())
