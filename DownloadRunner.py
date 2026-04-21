# !/usr/bin/python3

import argparse
import http.client
import json
import logging
import os
import time
from datetime import datetime
from os.path import exists

import pandas as pd

from downloaders import DownloadResult, download_pano, gsv, mapillary


parser = argparse.ArgumentParser()
parser.add_argument('d', help='sidewalk_server_domain - FDQN of SidewalkWebpage server to fetch pano list from, i.e. sidewalk-columbus.cs.washington.edu')
parser.add_argument('s', help='storage_path - location to store scraped panos')
parser.add_argument('-c', nargs='?', default=None, help='csv_path - location of csv from which to read pano metadata')
parser.add_argument('--all-panos', action='store_true', help='Run on all panos that users visited, even if no labels were added on them.')
parser.add_argument('--attempt-depth', action='store_true', help='Attempt do download depth data (we believe that this endpoint was removed in 2022 and expect depth download to always fail).')
args = parser.parse_args()

sidewalk_server_fqdn = args.d
storage_location = args.s
pano_metadata_csv = args.c
all_panos = args.all_panos
attempt_depth = args.attempt_depth

print(sidewalk_server_fqdn)
print(storage_location)
print(pano_metadata_csv)
print(all_panos)

if not os.path.exists(storage_location):
    os.makedirs(storage_location)

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


def fetch_pano_ids_from_webserver(include_all_panos):
    """
    Fetch pano metadata from /adminapi/panos.

    Each entry is a dict with: pano_id, width, height, lat, lng, camera_heading, camera_pitch, source, has_labels.

    Source-specific dispatch happens at download time, so all sources are kept here.
    """
    unique_ids = set()
    pano_info = []
    conn = http.client.HTTPSConnection(sidewalk_server_fqdn)
    conn.request("GET", "/adminapi/panos")
    r1 = conn.getresponse()
    data = r1.read()
    jsondata = json.loads(data)

    for value in jsondata:
        pano_id = value["pano_id"]
        has_labels = value["has_labels"]
        if (include_all_panos or has_labels) and pano_id not in unique_ids:
            if pano_id and pano_id != 'tutorial':
                unique_ids.add(pano_id)
                pano_info.append(value)
            else:
                print("Pano ID is an empty string or is for tutorial")
    assert len(unique_ids) == len(pano_info)
    return pano_info


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


def download_panorama_images(storage_path, pano_infos):
    logging.basicConfig(filename='scrape.log', level=logging.DEBUG)
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


def run_scraper_and_log_results(pano_infos, attempt_depth):
    start_time = datetime.now()
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write("\n%s" % (str(start_time)))

    # XML metadata + depth-map processing are GSV-only.
    xml_res = ()
    if attempt_depth:
        gsv_panos = [p for p in pano_infos if p.get('source') == 'gsv']
        xml_res = gsv.download_panorama_metadata_xmls(storage_location, gsv_panos)
    else:
        xml_res = (0, 0, len(pano_infos), len(pano_infos))
    xml_end_time = datetime.now()
    xml_duration = int(round((xml_end_time - start_time).total_seconds() / 60.0))
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write(",%d,%d,%d,%d,%d" % (xml_res[0], xml_res[1], xml_res[2], xml_res[3], xml_duration))

    im_res = download_panorama_images(storage_location, pano_infos)
    im_end_time = datetime.now()
    im_duration = int(round((im_end_time - xml_end_time).total_seconds() / 60.0))
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write(",%d,%d,%d,%d,%d,%d" % (im_res[0], im_res[1], im_res[2], im_res[3], im_res[4], im_duration))

    depth_res = ()
    if attempt_depth:
        depth_res = gsv.generate_depthmapfiles(storage_location)
    else:
        depth_res = (0, 0, 0, 0)
    depth_end_time = datetime.now()
    depth_duration = int(round((depth_end_time - im_end_time).total_seconds() / 60.0))
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write(",%d,%d,%d,%d,%d" % (depth_res[0], depth_res[1], depth_res[2], depth_res[3], depth_duration))

    total_duration = int(round((depth_end_time - start_time).total_seconds() / 60.0))
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write(",%d" % (total_duration))


# Access Project Sidewalk API to get Pano IDs for city
print("Fetching pano-ids")

if pano_metadata_csv is not None:
    pano_infos = fetch_pano_ids_csv(pano_metadata_csv)
else:
    pano_infos = fetch_pano_ids_from_webserver(all_panos)

pano_infos = filter_supported_sources(pano_infos)

# Uncomment this to test on a smaller subset of the pano_info.
# import random
# n = 3
# if len(pano_infos) > n:
#     pano_infos = random.sample(pano_infos, 3)

print(len(pano_infos))

# Use pano_id list and associated info to gather panos from respective APIs
print("Fetching Panoramas")
run_scraper_and_log_results(pano_infos, attempt_depth)
