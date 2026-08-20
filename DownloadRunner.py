# !/usr/bin/python3

import argparse
import csv
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

from downloaders import DownloadResult, download_pano, gsv, mapillary


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


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('d', help='sidewalk_server_domain - FQDN of SidewalkWebpage server to fetch pano list from, i.e. sidewalk-columbus.cs.washington.edu')
    parser.add_argument('s', help='storage_path - location to store scraped panos')
    parser.add_argument('-c', nargs='?', default=None, help='csv_path - location of csv from which to read pano metadata')
    parser.add_argument('--all-panos', action='store_true', help='Download images for all panos that users visited, even if no labels were added on them. Does not affect depth, which always covers every pano.')
    parser.add_argument('--skip-depth', action='store_true', help='Skip downloading GSV depth maps (downloaded by default via the streetlevel library).')
    parser.add_argument('--max-runtime', type=float, default=None, metavar='MINUTES', help='Stop starting new downloads after this many minutes have elapsed.')
    parser.add_argument('--min-depth-runtime', type=_reservation_minutes, default=0.0, metavar='MINUTES', help='Reserve the last MINUTES of --max-runtime for the depth phase when the depth ledger shows unresolved work, so an image backlog cannot starve depth. This is a reservation carved out of the image phase\'s start budget, not a hard floor on depth wall time: the image phase stops STARTING new panos once its share is spent (a pano already in flight can overrun into the reserved slice), and depth still ends at --max-runtime, so it also gets any slack images leave. If the reservation meets or exceeds --max-runtime, NO images are downloaded that run. Default 0 (no reservation); the production crontab should pass 60. Ignored without --max-runtime or with --skip-depth.')
    parser.add_argument('--max-depth-requests', type=int, default=None, metavar='N', help='Stop the depth phase after this many depth metadata requests.')
    # Deprecated no-op, kept for one release so existing invocations don't crash argparse.
    parser.add_argument('--attempt-depth', action='store_true', help=argparse.SUPPRESS)
    return parser


def configure_logging(log_path):
    """Set up run-wide logging to log_path (scrape.log on the pano store).

    Rotation (10 MB x 3) bounds growth now that the file persists across runs instead of dying with the
    container; each DEBUG record is a synchronous write over sshfs, which is also why urllib3's per-request
    chatter is capped at WARNING. If the log file itself can't be opened, fall back to stderr with one loud
    warning rather than killing the scrape: the log is evidence, not cargo.
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


def progress_check(csv_pano_log_path):
    """Read the image ledger once: every ledgered pano id, plus the prior counters seeded into this run's.

    A row means "resolved": downloaded == 1 counts as a prior success (skipped this run), 0 as a permanent
    failure (the source has nothing for this pano); either way the pano is never re-attempted. Transient
    failures are not ledgered at all (#41), so they are absent here and retry next run.

    Row-tolerant on the gsv._load_depth_log model: a line torn by a crash mid-append (or a float minted by
    the old rewrite path) is skipped, so a damaged ledger degrades to re-attempting a few panos instead of a
    ParserError that crashes every future run (#55). Reads with csv, not pandas, so the id type can never
    depend on what the ids happen to look like (#46).
    """
    ledgered_ids, total_processed, total_success = set(), 0, 0
    with open(csv_pano_log_path, newline='') as f:
        for row in csv.reader(f):
            if len(row) != 2 or row[0] == 'pano_id' or row[1] not in ('0', '1'):
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


def filter_supported_sources(pano_infos):
    """
    Drop panos we can't download in this run, preserving the server's ordering, with a one-time warning per
    reason.

    Supported sources: gsv, mapillary (mapillary requires MAPILLARY_ACCESS_TOKEN). Filtered-out panos are NOT written to
    pano_id_log.csv, so a later run with the token / updated code can still pick them up.

    Order-preserving on purpose (#40): the old implementation regrouped the list by source as a side effect
    of bucketing for the warnings, which put every GSV pano ahead of every Mapillary one - on a city whose
    GSV backlog exceeds --max-runtime, Mapillary then made zero progress, indefinitely and invisibly. A
    filter has no business reordering its input; download_panorama_images shuffles what it actually attempts,
    which is where starvation has to be prevented. The counts below exist only for the warnings.
    """
    source_counts = {}
    for p in pano_infos:
        source = p.get('source')
        source_counts[source] = source_counts.get(source, 0) + 1

    # Mapillary gets its own warning naming the missing token, so it is never also reported as an
    # unsupported source - hence 'known' rather than reusing 'supported' in the loop below.
    known = {'gsv', 'mapillary'}
    supported = {'gsv'}
    # Both warnings go to stdout AND to scrape.log, the depth phase's pattern (#52 item 6). stdout is what
    # cron mails, which is how an operator finds out tonight; scrape.log is what is still there next week
    # when someone asks why a city's Mapillary panos never arrived. Either channel alone loses one of those.
    if source_counts.get('mapillary'):
        if mapillary.is_token_set():
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


def download_panorama_images(storage_path, pano_infos, run_start_monotonic=None, max_runtime_minutes=None):
    success_count, skipped_count, fallback_success_count, fail_count, total_completed = 0, 0, 0, 0, 0

    # The attempted-pano ledger, in 'storage' alongside the pano results (see progress_check for semantics).
    csv_pano_log_path = os.path.join(storage_path, "pano_id_log.csv")
    ledger_existed = exists(csv_pano_log_path)
    if ledger_existed:
        df_id_set, prior_total, prior_success, prior_fail = progress_check(csv_pano_log_path)
    else:
        df_id_set, prior_total, prior_success, prior_fail = set(), 0, 0, 0
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

    # One handle held for the whole phase, appended and flushed per row - the depth ledger's pattern (#55).
    # The old shape opened/closed the file per pano over sshfs, and carried a dead 'update' branch that,
    # when the #46 dtype mismatch made it reachable, rewrote the ENTIRE file per pano with mode='w' - O(n^2)
    # per run, and a crash mid-rewrite truncated the only image ledger in place.
    with open(csv_pano_log_path, 'a', newline='') as ledger_file:
        # lineterminator='\n': csv.writer's excel default is '\r\n', but every existing image ledger was
        # written by pandas to_csv, whose default is os.linesep - '\n' on the Linux scraper boxes. Without
        # this pin, appending to a years-old ledger would mix line endings in one file and hand ops greps a
        # trailing '\r' on the downloaded column.
        ledger = csv.writer(ledger_file, lineterminator='\n')
        if not ledger_existed:
            ledger.writerow(['pano_id', 'downloaded'])
            ledger_file.flush()
            # Group-writable like depth_log.csv: other lab users' runs append to the same store.
            try:
                os.chmod(csv_pano_log_path, 0o664)
            except OSError:
                # Lost the exists()/open() race to another user's run: their file, their modes. The ledger is
                # already open and writable, so this must not take the phase down - the same call in both
                # downloaders' shard-dir setup swallows it for the same reason.
                pass

        for pano_info in candidates:
            pano_id = pano_info['pano_id']
            # candidates is already filtered against the ledger; this still catches a duplicate id surviving
            # intake, which would otherwise be downloaded and ledgered twice.
            if pano_id in df_id_set:
                continue
            if max_runtime_minutes is not None and run_start_monotonic is not None:
                # time.monotonic, not the wall clock: an NTP step or DST transition must not stretch or shrink
                # the budget (#51).
                elapsed_minutes = (time.monotonic() - run_start_monotonic) / 60.0
                if elapsed_minutes >= max_runtime_minutes:
                    print("IMAGEDOWNLOAD: Max runtime of %.1f minutes reached (%.1f elapsed). Stopping." % (max_runtime_minutes, elapsed_minutes))
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
                logging.error("IMAGEDOWNLOAD: Failed to download pano %s due to error %s", pano_id, str(e))
            total_completed = success_count + fallback_success_count + fail_count + skipped_count

            if downloaded is not None:
                ledger.writerow([pano_id, downloaded])
                ledger_file.flush()
                df_id_set.add(pano_id)

            print("IMAGEDOWNLOAD: Completed %d of %d (%d success, %d fallback success, %d failed, %d skipped)"
                  % (total_completed, total_panos, success_count, fallback_success_count, fail_count, skipped_count))
            print("--- %s seconds ---" % (time.time() - start_time))

    logging.debug(
        "IMAGEDOWNLOAD: Final result: Completed %d of %d (%d success, %d fallback success, %d failed, %d skipped)",
        total_completed,
        total_panos,
        success_count,
        fallback_success_count,
        fail_count,
        skipped_count)

    return success_count, fallback_success_count, fail_count, skipped_count, total_completed


# Fields per log.csv row: timestamp, 5 xml-stub, 6 image, 5 depth, 1 total duration. Positional, parsed by
# our log-analyzer tooling. The full column table lives in README.md's "Ops notes".
LOG_CSV_FIELD_COUNT = 18


def write_log_csv_row(storage_location, fields):
    """Append one run's row to <storage_location>/log.csv, blank-padded to the full 18 columns.

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
                                max_runtime_minutes=None, max_depth_requests=None, min_depth_runtime=0.0):
    """Run the image and depth phases and append this run's row to log.csv.

    Fields are accumulated as each phase completes and the row is written once, in a finally, padded to the
    full 18 with blanks. A crash mid-run therefore still yields a parseable full-width line that keeps every
    completed phase's counts (a failure in the depth phase must not discard what the image phase downloaded),
    while the phases that never finished stay visibly blank rather than turning into fake zeros (#49).

    @param storage_location Root of the pano store (log.csv and the ledgers live here).
    @param image_pano_infos Panos eligible for image download (narrowed by --all-panos).
    @param depth_pano_infos Every supported pano; the depth phase filters this to source == 'gsv' itself.
    @param min_depth_runtime Minutes of max_runtime_minutes reserved for the depth phase (see the flag's help).
    """
    start_time = datetime.now()
    # Wall-clock datetimes feed the log; the runtime budget gets a monotonic reference instead (#51).
    run_start_monotonic = time.monotonic()

    # Depth maps are GSV-only; the depth phase's view of the corpus is computed up front because the budget
    # split below needs it too.
    gsv_panos = [p for p in depth_pano_infos if p.get('source') == 'gsv']

    # Both phases share --max-runtime (it exists to keep the run inside its daily cron slot, per #38, and that
    # constraint doesn't care which phase spends the clock), but the image phase must leave the reserved tail so
    # an image backlog — a mapathon is the canonical case — can't starve the depth backfill night after night
    # (#43). The reservation is only taken when the depth ledger shows unresolved work: once a city is fully
    # backfilled the depth phase returns in milliseconds, and reserving for it would burn image throughput for
    # nothing. Depth still ends at the total, so slack from a light image night rolls to depth rather than being
    # lost. No reservation when depth is skipped: the image phase keeps the whole window.
    image_max_runtime = max_runtime_minutes
    if max_runtime_minutes is not None and not skip_depth and min_depth_runtime > 0:
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

    fields = [str(start_time)]
    try:
        # There is no XML metadata phase (that endpoint died in 2022; depth now comes from streetlevel below),
        # but its log.csv columns are stubbed with the values every production run has always written so the
        # positional 18-column format parsed by scraper-log-analyzer doesn't shift. Deliberately the image
        # list's length, which is what this counted before depth stopped honouring --all-panos.
        xml_res = (0, 0, len(image_pano_infos), len(image_pano_infos))
        xml_end_time = datetime.now()
        xml_duration = int(round((xml_end_time - start_time).total_seconds() / 60.0))
        fields += [xml_res[0], xml_res[1], xml_res[2], xml_res[3], xml_duration]

        # The budget arguments are passed by keyword deliberately: several changes have rewritten these call
        # sites, and a positional resolution can put a datetime where a monotonic float belongs — a TypeError
        # that only fires when --max-runtime is set, i.e. in the nightly cron and never in the suite.
        im_res = download_panorama_images(storage_location, image_pano_infos,
                                          run_start_monotonic=run_start_monotonic,
                                          max_runtime_minutes=image_max_runtime)
        im_end_time = datetime.now()
        im_duration = int(round((im_end_time - xml_end_time).total_seconds() / 60.0))
        fields += [im_res[0], im_res[1], im_res[2], im_res[3], im_res[4], im_duration]

        # The depth phase runs after the image phase and ends at the shared --max-runtime, so it gets the
        # reserved tail (when one was taken) plus whatever slack the image phase left. It iterates the full
        # pano list — not the pano_id_log.csv-gated image loop, and not narrowed by --all-panos — which is what
        # backfills depth for panos downloaded in earlier runs and for panos nobody has labelled.
        if skip_depth:
            depth_res = (0, 0, 0, 0)
        else:
            depth_res = gsv.download_depth_maps(storage_location, gsv_panos,
                                                run_start_monotonic=run_start_monotonic,
                                                max_runtime_minutes=max_runtime_minutes,
                                                max_requests=max_depth_requests)
        depth_end_time = datetime.now()
        depth_duration = int(round((depth_end_time - im_end_time).total_seconds() / 60.0))
        fields += [depth_res[0], depth_res[1], depth_res[2], depth_res[3], depth_duration]

        fields.append(int(round((depth_end_time - start_time).total_seconds() / 60.0)))
    finally:
        write_log_csv_row(storage_location, fields)


def run(sidewalk_server_fqdn, storage_location, pano_metadata_csv=None, all_panos=False, skip_depth=False,
        max_runtime_minutes=None, min_depth_runtime=0.0, max_depth_requests=None):
    """Fetch the pano list, narrow it, and run the scrape - the whole job, minus process-level setup.

    main() owns argv parsing, directory creation, logging, and signal handling; this seam takes plain
    arguments (defaults mirror the flags') so tests can drive the real fetch -> filter -> phase orchestration
    in-process (#52.1).
    """
    # Access Project Sidewalk API to get Pano IDs for city
    print("Fetching pano-ids")

    try:
        if pano_metadata_csv is not None:
            pano_infos = fetch_pano_ids_csv(pano_metadata_csv)
        else:
            pano_infos = fetch_pano_ids_from_webserver(sidewalk_server_fqdn)
        pano_infos = filter_supported_sources(pano_infos)
        image_pano_infos = select_image_panos(pano_infos, all_panos)
    except BaseException:
        # A crash before the scrape starts - a webserver outage being the single most likely nightly failure -
        # must still leave both kinds of evidence (#49): the traceback in scrape.log, and a blank-padded
        # log.csv row whose real timestamp shows a run started and produced nothing.
        logging.exception("Run crashed before the scrape started")
        write_log_csv_row(storage_location, [str(datetime.now())])
        raise

    # Uncomment this to test on a smaller subset of the pano_info.
    # import random
    # n = 3
    # if len(pano_infos) > n:
    #     pano_infos = random.sample(pano_infos, n)

    print("Panos: %d supported, %d eligible for image download, %d GSV panos eligible for depth"
          % (len(pano_infos), len(image_pano_infos), sum(1 for p in pano_infos if p.get('source') == 'gsv')))

    # Use pano_id list and associated info to gather panos from respective APIs
    print("Fetching Panoramas")
    try:
        run_scraper_and_log_results(storage_location, image_pano_infos, pano_infos, skip_depth,
                                    max_runtime_minutes=max_runtime_minutes,
                                    max_depth_requests=max_depth_requests, min_depth_runtime=min_depth_runtime)
    except BaseException:
        # run_scraper_and_log_results's own finally has already written the evidence row; this puts the
        # traceback - otherwise stderr-only, the exact channel that dies with the container - into scrape.log
        # too (#49).
        logging.exception("Run failed")
        raise


def main(argv=None):
    """Process-level setup, then run(): everything a `python3 DownloadRunner.py ...` invocation does.

    Exceptions propagate (the interpreter prints the traceback and exits 1) and argparse errors exit 2,
    exactly as the pre-#52 module-scope script behaved.
    """
    args = build_parser().parse_args(argv)

    if args.attempt_depth:
        print("WARNING: --attempt-depth is deprecated and ignored; depth download is now on by default "
              "(use --skip-depth to disable).")

    # min_depth_runtime > 0 implies the operator typed the flag (the default is 0), so tell them when the
    # combination they ran it in means it cannot do anything.
    if args.min_depth_runtime > 0 and (args.max_runtime is None or args.skip_depth):
        print("WARNING: --min-depth-runtime has no effect %s; no time will be reserved for the depth phase."
              % ("with --skip-depth" if args.skip_depth else "without --max-runtime"))

    # exist_ok: concurrent city runs (or the operator pre-creating the dir) race on the exists check.
    os.makedirs(args.s, exist_ok=True)

    # scrape.log lives on the pano store next to log.csv, NOT the CWD: in Docker the CWD is /app inside the
    # container, so a relative path would discard the log - and every per-pano failure detail - when the
    # container exits (#49). Configured once here at startup so every part of the run logs to the same file -
    # including a crash in the pano-list fetch, which happens before any phase's own code gets a chance to run.
    configure_logging(os.path.join(args.s, 'scrape.log'))

    # docker stop sends SIGTERM, which CPython by default dies from without running finally blocks - taking the
    # log.csv evidence row with it (#49). Translate it into a SystemExit carrying the conventional 128+15 code,
    # so cleanup runs and the exit still reads as a signal death.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    print("Starting run with pano list fetched from %s and destination path %s" % (args.d, args.s))

    run(sidewalk_server_fqdn=args.d, storage_location=args.s, pano_metadata_csv=args.c,
        all_panos=args.all_panos, skip_depth=args.skip_depth, max_runtime_minutes=args.max_runtime,
        min_depth_runtime=args.min_depth_runtime, max_depth_requests=args.max_depth_requests)


if __name__ == '__main__':
    main()
