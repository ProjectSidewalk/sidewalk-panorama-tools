# !/usr/bin/python3

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import time
from datetime import datetime
from os.path import exists

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from downloaders import DownloadResult, download_pano, gsv, mapillary


parser = argparse.ArgumentParser()
parser.add_argument('d', help='sidewalk_server_domain - FDQN of SidewalkWebpage server to fetch pano list from, i.e. sidewalk-columbus.cs.washington.edu')
parser.add_argument('s', help='storage_path - location to store scraped panos')
parser.add_argument('-c', nargs='?', default=None, help='csv_path - location of csv from which to read pano metadata')
parser.add_argument('--all-panos', action='store_true', help='Download images for all panos that users visited, even if no labels were added on them. Does not affect depth, which always covers every pano.')
parser.add_argument('--skip-depth', action='store_true', help='Skip downloading GSV depth maps (downloaded by default via the streetlevel library).')
parser.add_argument('--max-runtime', type=float, default=None, metavar='MINUTES', help='Stop starting new downloads after this many minutes have elapsed.')
parser.add_argument('--max-depth-requests', type=int, default=None, metavar='N', help='Stop the depth phase after this many depth metadata requests.')
# Deprecated no-op, kept for one release so existing invocations don't crash argparse.
parser.add_argument('--attempt-depth', action='store_true', help=argparse.SUPPRESS)
args = parser.parse_args()

sidewalk_server_fqdn = args.d
storage_location = args.s
pano_metadata_csv = args.c
all_panos = args.all_panos
skip_depth = args.skip_depth
max_runtime_minutes = args.max_runtime
max_depth_requests = args.max_depth_requests

if args.attempt_depth:
    print("WARNING: --attempt-depth is deprecated and ignored; depth download is now on by default "
          "(use --skip-depth to disable).")

print(sidewalk_server_fqdn)
print(storage_location)
print(pano_metadata_csv)
print(all_panos)

# exist_ok: concurrent city runs (or the operator pre-creating the dir) race on the exists check.
os.makedirs(storage_location, exist_ok=True)


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


# scrape.log lives on the pano store next to log.csv, NOT the CWD: in Docker the CWD is /app inside the
# container, so a relative path would discard the log - and every per-pano failure detail - when the container
# exits (#49). Configured once here at startup so every part of the run logs to the same file - including a
# crash in the pano-list fetch below, which happens before any phase's own code gets a chance to run.
configure_logging(os.path.join(storage_location, 'scrape.log'))

# docker stop sends SIGTERM, which CPython by default dies from without running finally blocks - taking the
# log.csv evidence row with it (#49). Translate it into a SystemExit carrying the conventional 128+15 code, so
# cleanup runs and the exit still reads as a signal death.
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

print("Starting run with pano list fetched from %s and destination path %s" % (sidewalk_server_fqdn, storage_location))


def progress_check(csv_pano_log_path):
    """
    Checks download status via a csv: log as skipped if downloaded == 1, failure if download == 0.
    This speeds things up instead of trying to re-download broken links or images.
    NB: This will not check if the failure was due to internet connection being unavailable etc. so use with caution.
    """
    df_pano_id_check = pd.read_csv(csv_pano_log_path, dtype={'pano_id': str})
    df_id_set = set(df_pano_id_check['pano_id'])
    total_processed = len(df_pano_id_check.index)
    total_success = df_pano_id_check['downloaded'].sum()
    total_failed = total_processed - total_success
    return df_id_set, total_processed, total_success, total_failed


def fetch_pano_ids_csv(metadata_csv_path):
    """
    Loads pano metadata from a CSV file (downloaded from the server). Dedupes on pano_id.
    Expected to include the same columns as /adminapi/panos, notably `source`.
    """
    df_meta = pd.read_csv(metadata_csv_path)
    df_meta = df_meta.drop_duplicates(subset=['pano_id']).to_dict('records')
    return df_meta


def fetch_pano_ids_from_webserver():
    """
    Fetch pano metadata from /adminapi/panos.

    Each entry is a dict with: pano_id, width, height, lat, lng, camera_heading, camera_pitch, source, has_labels.

    Returns every pano the server knows about. Source-specific dispatch happens at download time, and the
    --all-panos / has_labels split happens in select_image_panos() - the depth phase wants the whole corpus, so
    filtering here would hide unlabelled panos from it.
    """
    unique_ids = set()
    pano_info = []
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

    for value in jsondata:
        pano_id = value["pano_id"]
        if pano_id in unique_ids:
            continue
        if pano_id and pano_id != 'tutorial':
            unique_ids.add(pano_id)
            pano_info.append(value)
        else:
            print("Pano ID is an empty string or is for tutorial")
    assert len(unique_ids) == len(pano_info)
    return pano_info


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
    Drop panos we can't download in this run, with a one-time warning per reason.

    Supported sources: gsv, mapillary (mapillary requires MAPILLARY_ACCESS_TOKEN). Filtered-out panos are NOT written to
    pano_id_log.csv, so a later run with the token / updated code can still pick them up.
    """
    by_source = {}
    for p in pano_infos:
        by_source.setdefault(p.get('source'), []).append(p)

    kept = list(by_source.pop('gsv', []))

    mapillary_panos = by_source.pop('mapillary', [])
    if mapillary_panos:
        if mapillary.is_token_set():
            kept.extend(mapillary_panos)
        else:
            print("WARNING: %d Mapillary panos skipped — set %s to download them"
                  % (len(mapillary_panos), mapillary.TOKEN_ENV_VAR))

    for source, panos in by_source.items():
        print("WARNING: %d panos with unsupported source %r skipped" % (len(panos), source))

    return kept


def download_panorama_images(storage_path, pano_infos, run_start_monotonic=None, max_runtime_minutes=None):
    success_count, skipped_count, fallback_success_count, fail_count, total_completed = 0, 0, 0, 0, 0

    # csv log file for pano_id failures, place in 'storage' folder (alongside pano results)
    csv_pano_log_path = os.path.join(storage_path, "pano_id_log.csv")
    columns = ['pano_id', 'downloaded']
    if not exists(csv_pano_log_path):
        df_pano_id_log = pd.DataFrame(columns=columns)
        df_pano_id_log.to_csv(csv_pano_log_path, mode='w', header=True, index=False)
    else:
        df_pano_id_log = pd.read_csv(csv_pano_log_path)
    processed_ids = set(df_pano_id_log['pano_id'])

    df_id_set, prior_total, prior_success, prior_fail = progress_check(csv_pano_log_path)
    # Seed counters from the log so "skipped" in the progress line includes panos already
    # downloaded on previous runs (same semantics as the original code).
    skipped_count = prior_success
    fail_count = prior_fail
    total_completed = prior_total
    # Denominator = previously logged + panos we'll attempt this run, so it can never be exceeded.
    new_panos = sum(1 for p in pano_infos if p['pano_id'] not in df_id_set)
    total_panos = prior_total + new_panos

    for pano_info in pano_infos:
        pano_id = pano_info['pano_id']
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
            fail_count += 1
            downloaded = 0
            logging.error("IMAGEDOWNLOAD: Failed to download pano %s due to error %s", pano_id, str(e))
        total_completed = success_count + fallback_success_count + fail_count + skipped_count

        if pano_id not in processed_ids:
            df_data_append = pd.DataFrame([[pano_id, downloaded]], columns=columns)
            df_data_append.to_csv(csv_pano_log_path, mode='a', header=False, index=False)
            processed_ids.add(pano_id)
        else:
            df_pano_id_log = pd.read_csv(csv_pano_log_path)
            df_pano_id_log.loc[df_pano_id_log['pano_id'] == pano_id, 'downloaded'] = downloaded
            df_pano_id_log.to_csv(csv_pano_log_path, mode='w', header=True, index=False)

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


def write_log_csv_row(fields):
    """Append one run's row to log.csv, blank-padded to the full 18 columns.

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


def run_scraper_and_log_results(image_pano_infos, depth_pano_infos, skip_depth, max_runtime_minutes=None,
                                max_depth_requests=None):
    """Run the image and depth phases and append this run's row to log.csv.

    Fields are accumulated as each phase completes and the row is written once, in a finally, padded to the
    full 18 with blanks. A crash mid-run therefore still yields a parseable full-width line that keeps every
    completed phase's counts (a failure in the depth phase must not discard what the image phase downloaded),
    while the phases that never finished stay visibly blank rather than turning into fake zeros (#49).

    @param image_pano_infos Panos eligible for image download (narrowed by --all-panos).
    @param depth_pano_infos Every supported pano; the depth phase filters this to source == 'gsv' itself.
    """
    start_time = datetime.now()
    # Wall-clock datetimes feed the log; the runtime budget gets a monotonic reference instead (#51).
    run_start_monotonic = time.monotonic()
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

        # Keyword args on the budget parameters: #63 rewrites this same line, and a positional merge resolution
        # would silently slot a datetime into the monotonic parameter — keywords make that collision loud.
        im_res = download_panorama_images(storage_location, image_pano_infos,
                                          run_start_monotonic=run_start_monotonic,
                                          max_runtime_minutes=max_runtime_minutes)
        im_end_time = datetime.now()
        im_duration = int(round((im_end_time - xml_end_time).total_seconds() / 60.0))
        fields += [im_res[0], im_res[1], im_res[2], im_res[3], im_res[4], im_duration]

        # Depth maps are GSV-only. This phase runs after the image phase sharing the same --max-runtime budget,
        # so on catch-up days images (the primary artifact) win the whole window. It iterates the full pano
        # list — not the pano_id_log.csv-gated image loop, and not narrowed by --all-panos — which is what
        # backfills depth for panos downloaded in earlier runs and for panos nobody has labelled.
        if skip_depth:
            depth_res = (0, 0, 0, 0)
        else:
            gsv_panos = [p for p in depth_pano_infos if p.get('source') == 'gsv']
            depth_res = gsv.download_depth_maps(storage_location, gsv_panos,
                                                run_start_monotonic=run_start_monotonic,
                                                max_runtime_minutes=max_runtime_minutes,
                                                max_requests=max_depth_requests)
        depth_end_time = datetime.now()
        depth_duration = int(round((depth_end_time - im_end_time).total_seconds() / 60.0))
        fields += [depth_res[0], depth_res[1], depth_res[2], depth_res[3], depth_duration]

        fields.append(int(round((depth_end_time - start_time).total_seconds() / 60.0)))
    finally:
        write_log_csv_row(fields)


# Access Project Sidewalk API to get Pano IDs for city
print("Fetching pano-ids")

try:
    if pano_metadata_csv is not None:
        pano_infos = fetch_pano_ids_csv(pano_metadata_csv)
    else:
        pano_infos = fetch_pano_ids_from_webserver()
    pano_infos = filter_supported_sources(pano_infos)
    image_pano_infos = select_image_panos(pano_infos, all_panos)
except BaseException:
    # A crash before the scrape starts - a webserver outage being the single most likely nightly failure - must
    # still leave both kinds of evidence (#49): the traceback in scrape.log, and a blank-padded log.csv row
    # whose real timestamp shows a run started and produced nothing.
    logging.exception("Run crashed before the scrape started")
    write_log_csv_row([str(datetime.now())])
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
    run_scraper_and_log_results(image_pano_infos, pano_infos, skip_depth, max_runtime_minutes, max_depth_requests)
except BaseException:
    # run_scraper_and_log_results's own finally has already written the evidence row; this puts the traceback -
    # otherwise stderr-only, the exact channel that dies with the container - into scrape.log too (#49).
    logging.exception("Run failed")
    raise
