# !/usr/bin/python3

from SidewalkDB import *
import os
from os.path import exists
import stat
import http.client
import json
import logging
from datetime import datetime
import time
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from PIL import Image
import fnmatch
import pandas as pd
import random
from config import headers_list, proxies, thread_count
import asyncio
import aiohttp
from aiohttp import web
import backoff
from io import BytesIO


try:
    from xml.etree import cElementTree as ET
except ImportError as e:
    from xml.etree import ElementTree as ET


class Enum(object):
    def __init__(self, tupleList):
        self.tupleList = tupleList

    def __getattr__(self, name):
        return self.tupleList.index(name)


DownloadResult = Enum(('skipped', 'success', 'fallback_success', 'failure'))

delay = 0

# Check proxy settings, if none provided (default) set proxies to False
if proxies['http'] == "http://" or proxies['https'] == "https://":
    proxies['http'] = None
    proxies['https'] = None

# if len(argv) != 3:
#     print("Usage: python DownloadRunner.py sidewalk_server_domain storage_path")
#     print("    sidewalk_server_domain - FDQN of SidewalkWebpage server to fetch pano list from")
#     print("    storage_path - location to store scraped panos")
#     print("    Example: python DownloadRunner.py sidewalk-sea.cs.washington.edu /destination/path")
#     exit(0)

# sidewalk_server_fqdn = argv[1]
sidewalk_server_fqdn = "sidewalk-sea.cs.washington.edu"
storage_location = "testing/"  # The path to where you want to store downloaded GSV panos
metadata_csv_path = "metadata/csv-metadata-seattle.csv"  # Path to csv containing all required metadata
if not os.path.exists(storage_location):
    os.mkdir(storage_location)

# comment out for now, will use csv for data
print("Starting run with pano list fetched from %s and destination path %s" % (sidewalk_server_fqdn, storage_location))


def new_random_delay():
    """
    New random delay value generated
    :return: int between 50 and 250 in steps of 3
    """
    return random.randrange(100, 200, 3)


# Choose header at random from the list
def random_header():
    headers = random.choice(headers_list)
    return headers


# Set up the requests session for better robustness/respect of crawling
# https://stackoverflow.com/questions/23013220/max-retries-exceeded-with-url-in-requests
# Server errors while using proxy - https://findwork.dev/blog/advanced-usage-python-requests-timeouts-retries-hooks/
def request_session():
    """

    :return:
    """
    session = requests.Session()
    retry = Retry(total=10, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def get_response(url, session, stream=False):
    """

    :param url:
    :param session:
    :param stream:
    :return:
    """

    response = session.get(url, headers=random_header(), proxies=proxies, stream=stream)

    if not stream:
        return response
    else:
        return response.raw


def check_download_failed_previously(panoId):
    if panoId in open('scrape.log').read():
        return True
    else:
        return False


def progress_check(csv_pano_log_path):
    # temporary skip/speed up of processed panos
    df_pano_id_check = pd.read_csv(csv_pano_log_path)
    df_id_set = set(df_pano_id_check['gsv_pano_id'])
    total_processed = len(df_pano_id_check.index)
    total_success = df_pano_id_check['downloaded'].sum()
    total_failed = total_processed - total_success
    return df_id_set, total_processed, total_success, total_failed


def extract_pano_width_height_csv(df_meta, pano_id):
    pano_id_row = df_meta[df_meta['gsv_panorama_id'] == pano_id]
    image_width = int(pano_id_row['image_width'])
    image_height = int(pano_id_row['image_height'])
    return image_width, image_height


# Broken, needs to reference csv for width and height
def extract_panowidthheight(path_to_metadata_xml):
    pano = {}
    pano_xml = open(path_to_metadata_xml, 'rb')
    tree = ET.parse(pano_xml)
    root = tree.getroot()
    for child in root:
        if child.tag == 'data_properties':
            pano[child.tag] = child.attrib

    return int(pano['data_properties']['image_width']), int(pano['data_properties']['image_height'])


def fetch_pano_ids_csv(metadata_csv_path):
    """
    Function loads the provided metadata csv file (downloaded from the server) as a dataframe. This dataframe replaces
    all the information that previously needed to be gather from Google maps, such as image size, image capture
    coordinates etc. This dataframe replaces the previously used fetch_pano_ids_from_webserver() function.
    :param metadata_csv_path: The path to the metadata csv file and the file's name eg. metadata/csv_meta.csv
    :return: A dataframe containing the follow metadata headings: gsv_panorama_id	sv_image_x, sv_image_y, zoom,
    label_type_id, photographer_heading, heading, pitch, label_id, image_width, image_height, tile_width, tile_height,
    image_date, imagery_type, panorama_lat, panorama_lng, label_lat,
    label_lng,
    """
    df_meta = pd.read_csv(metadata_csv_path)
    df_meta = df_meta.drop_duplicates(subset=['gsv_panorama_id'])
    assert df_meta.shape == (52208, 21)  # assertion check for csv provided by Mikey, needs to be updated for future use
    return df_meta


# No longer using webserver, keep for future server request implementation
def fetch_pano_ids_from_webserver():
    unique_ids = []
    conn = http.client.HTTPSConnection(sidewalk_server_fqdn)
    conn.request("GET", "/adminapi/labels/panoid")
    r1 = conn.getresponse()
    data = r1.read()
    # print(data)
    jsondata = json.loads(data)

    for value in jsondata["features"]:
        if value["properties"]["gsv_panorama_id"] not in unique_ids:
            # Check if the pano_id is an empty string
            if value["properties"]["gsv_panorama_id"]:
                unique_ids.append(value["properties"]["gsv_panorama_id"])
            else:
                print("Pano ID is an empty string")
    return unique_ids


def download_panorama_images(storage_path, df_meta):
    logging.basicConfig(filename='scrape.log', level=logging.DEBUG)
    pano_list = df_meta['gsv_panorama_id']
    success_count, skipped_count, fallback_success_count, fail_count, total_completed = 0, 0, 0, 0, 0
    total_panos = len(pano_list)

    # csv log file for pano_id failures, place in 'storage' folder (alongside pano results)
    csv_pano_log_path = storage_location + "gsv_panorama_id_log.csv"
    columns = ['gsv_pano_id', 'downloaded']
    if not exists(csv_pano_log_path):
        df_pano_id_log = pd.DataFrame(columns=columns)
        df_pano_id_log.to_csv(csv_pano_log_path, mode='w', header=True, index=False)
    else:
        df_pano_id_log = pd.read_csv(csv_pano_log_path)
    processed_ids = list(df_pano_id_log['gsv_pano_id'])

    df_id_set, total_completed, success_count, fail_count = progress_check(csv_pano_log_path)
    pano_list = ['Ej9LLVoYjEPowe8LpOYnug']
    for pano_id in pano_list:
        # if pano_id in df_id_set:
        #     continue
        start_time = time.time()
        print("IMAGEDOWNLOAD: Processing pano %s " % (pano_id))
        try:
            result_code = download_single_pano(storage_path, pano_id)
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
        else:
            df_pano_id_log = pd.read_csv(csv_pano_log_path)
            df_pano_id_log.loc[df_pano_id_log['gsv_pano_id'] == pano_id, 'downloaded'] = downloaded
            df_pano_id_log.to_csv(csv_pano_log_path, mode='w', header=True, index=False)
            processed_ids.append(pano_id)

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


# Update to use df to get meta information. Also function is very long, would be good to break up into sub-functions...
def download_single_pano(storage_path, pano_id):
    base_url = 'http://maps.google.com/cbk?'

    pano_xml_path = os.path.join(storage_path, pano_id[:2], pano_id + ".xml")

    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        os.makedirs(destination_dir)
        os.chmod(destination_dir, 0o775 | stat.S_ISGID)

    filename = pano_id + ".jpg"
    out_image_name = os.path.join(destination_dir, filename)

    # Skip download if image already exists
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    # default values
    final_image_height = 6656
    final_image_width = 13312

    if sidewalk_server_fqdn == 'sidewalk-sea.cs.washington.edu':
        final_image_width = 16384
        final_image_height = 8192

    url_zoom_3 = 'http://maps.google.com/cbk?output=tile&zoom=3&x=0&y=0&cb_client=maps_sv&fover=2&onerr=3&renderer=' \
                 'spherical&v=4&panoid='
    url_zoom_5 = 'http://maps.google.com/cbk?output=tile&zoom=5&x=0&y=0&cb_client=maps_sv&fover=2&onerr=3&renderer=' \
                 'spherical&v=4&panoid='

    url_zoom_5_edge = 'http://maps.google.com/cbk?output=tile&zoom=5&x=26&y=0&cb_client=maps_sv&fover=2&onerr=3&renderer=' \
                 'spherical&v=4&panoid='

    session = request_session()

    req_zoom_3 = get_response(url_zoom_3 + pano_id, session, stream=True)
    im_zoom_3 = Image.open(req_zoom_3)

    req_zoom_5 = get_response(url_zoom_5 + pano_id, session, stream=True)
    im_zoom_5 = Image.open(req_zoom_5)

    req_zoom_5_extreme = get_response(url_zoom_5_edge + pano_id, session, stream=True)
    im_zoom_5_extreme = Image.open(req_zoom_5_extreme)

    if im_zoom_5.convert("L").getextrema() != (0, 0):
        fallback = False
        zoom = 5
        # check how far the GSV image canvas goes to (will decide image final width)
        if im_zoom_5_extreme.convert("L").getextrema() != (0, 0):
            image_width = final_image_width
            image_height = final_image_height
            print("Image set to large size")
        else:
            image_width = 13312
            image_height = 6656
            print("Image set to smaller size, final_im_dimension updates")
    elif im_zoom_3.convert("L").getextrema() != (0, 0):
        fallback = True
        zoom = 3
        image_width = 3328
        image_height = 1664
    else:
        return DownloadResult.failure

    final_im_dimension = (image_width, image_height)

    def generate_gsv_urls(zoom):
        sites_gsv = []
        for y in range(int(round(image_height / 512.0))):
            for x in range(int(round(image_width / 512.0))):
                url_param = 'output=tile&zoom=' + str(zoom) + '&x=' + str(x) + '&y=' + str(
                    y) + '&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4&panoid=' + pano_id
                url = base_url + url_param
                sites_gsv.append((str(x) + " " + str(y), url))
        return sites_gsv

    @backoff.on_exception(backoff.expo, (aiohttp.web.HTTPServerError, aiohttp.ClientError, aiohttp.ClientResponseError,
                                         aiohttp.ServerConnectionError, aiohttp.ServerDisconnectedError,
                                         aiohttp.ClientHttpProxyError), max_tries=10)
    async def download_single_gsv(session, url):

        async with session.get(url[1], proxy=proxies["http"], headers=random_header()) as response:
            head_content = response.headers['Content-Type']
            # ensures content type is an image
            if head_content[0:10] != "image/jpeg":
                raise aiohttp.ClientResponseError(response.request_info, response.history)
            image = await response.content.read()
            return [url[0], image]

    @backoff.on_exception(backoff.expo,
                          (aiohttp.web.HTTPServerError, aiohttp.ClientError, aiohttp.ClientResponseError, aiohttp.ServerConnectionError,
                           aiohttp.ServerDisconnectedError, aiohttp.ClientHttpProxyError), max_tries=10)
    async def download_all_gsv_images(sites):
        conn = aiohttp.TCPConnector(limit=thread_count)
        async with aiohttp.ClientSession(raise_for_status=True, connector=conn) as session:
            tasks = []
            for url in sites:
                task = asyncio.ensure_future(download_single_gsv(session, url))
                tasks.append(task)
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            return responses

    im_dimension = (image_width, image_height)
    blank_image = Image.new('RGB', im_dimension, (0, 0, 0, 0))
    sites = generate_gsv_urls(zoom)
    all_pano_images = asyncio.get_event_loop().run_until_complete(download_all_gsv_images(sites))

    for cell_image in all_pano_images:
        img = Image.open(BytesIO(cell_image[1]))
        img = img.resize((512, 512))
        x, y = int(str.split(cell_image[0])[0]), int(str.split(cell_image[0])[1])
        blank_image.paste(img, (512 * x, 512 * y))

    if fallback:
        blank_image = blank_image.resize(final_im_dimension, Image.ANTIALIAS)
        blank_image.save(out_image_name, 'jpeg')
        os.chmod(out_image_name, 0o664)
        return DownloadResult.fallback_success
    else:
        blank_image.save(out_image_name, 'jpeg')
        os.chmod(out_image_name, 0o664)
        return DownloadResult.success


# Broken, no longer needed, reference csv instead
def download_panorama_metadata_xmls(storage_path, pano_list):
    '''
     This method downloads a xml file that contains depth information from GSV. It first
     checks if we have a folder for each pano_id, and checks if we already have the corresponding
     depth file or not.
    '''

    base_url = "http://maps.google.com/cbk?output=xml&cb_client=maps_sv&hl=en&dm=1&pm=1&ph=1&renderer=cubic,spherical&v=4&panoid="

    total_panos = len(pano_list)
    success_count = 0
    fail_count = 0
    skipped_count = 0
    total_completed = 0

    for pano_id in pano_list:
        print("METADOWNLOAD: Processing pano %s " % (pano_id))
        try:
            result_code = download_single_metadata_xml(storage_path, pano_id)
            if result_code == DownloadResult.failure:
                fail_count += 1
            elif result_code == DownloadResult.success:
                success_count += 1
            elif result_code == DownloadResult.skipped:
                skipped_count += 1
        except Exception as e:
            fail_count += 1
            logging.error("METADOWNLOAD: Failed to download metadata for pano %s due to error %s", pano_id, str(e))
        total_completed = fail_count + success_count + skipped_count
        print("METADOWNLOAD: Completed %d of %d (%d success, %d failed, %d skipped)" %
              (total_completed, total_panos, success_count, fail_count, skipped_count))

    logging.debug("METADOWNLOAD: Final result: Completed %d of %d (%d success, %d failed, %d skipped)",
                  total_completed, total_panos, success_count, fail_count, skipped_count)
    return (success_count, fail_count, skipped_count, total_completed)


# No longer downloading, reference csv (for now)
def download_single_metadata_xml(storage_path, pano_id):
    base_url = "http://maps.google.com/cbk?output=xml&cb_client=maps_sv&hl=en&dm=1&pm=1&ph=1&renderer=cubic,spherical&v=4&panoid="

    # Check if the directory exists. Then check if the file already exists and skip if it does.
    destination_folder = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_folder):
        os.makedirs(destination_folder)
        os.chmod(destination_folder, 0o775 | stat.S_ISGID)

    filename = pano_id + ".xml"
    destination_file = os.path.join(destination_folder, filename)
    if os.path.isfile(destination_file):
        return DownloadResult.skipped

    url = base_url + pano_id

    session = request_session()
    req = get_response(url, session)

    # Check if the XML file is empty. If not, write it out to a file and set the permissions.
    # req = session.get(url, headers=random_header(), proxies=proxies)
    firstline = req.content.splitlines()[0]

    if firstline == '<?xml version="1.0" encoding="UTF-8" ?><panorama/>':
        return DownloadResult.failure
    else:
        with open(destination_file, 'wb') as f:
            f.write(firstline)
            for line in req:
                f.write(line)
        os.chmod(destination_file, 0o664)

        return DownloadResult.success


# No longer available, remove....
def generate_depthmapfiles(path_to_scrapes):
    success_count = 0
    fail_count = 0
    skip_count = 0
    total_completed = 0
    # Iterate through all .xml files in specified path, recursively
    for root, dirnames, filenames in os.walk(path_to_scrapes):
        for filename in fnmatch.filter(filenames, '*.xml'):
            xml_location = os.path.join(root, filename)

            # Pano id is XML filename minus the extension
            pano_id = filename[:-4]
            print("GENERATEDEPTH: Processing pano %s " % (pano_id))

            # Generate a .depth.txt file for the .xml file
            output_file = os.path.join(root, pano_id + ".depth.txt")
            if os.path.isfile(output_file):
                skip_count += 1
            else:
                output_code = call(["./decode_depthmap", xml_location, output_file])
                if output_code == 0:
                    os.chmod(output_file, 0o664)
                    success_count += 1
                else:
                    fail_count += 1
                    logging.error("GENERATEDEPTH: Could not create depth.txt for pano %s, error code was %s", pano_id,
                                  str(output_code))
            total_completed = fail_count + success_count + skip_count
            print("GENERATEDEPTH: Completed %d (%d success, %d failed, %d skipped)" %
                  (total_completed, success_count, fail_count, skip_count))

    logging.debug("GENERATEDEPTH: Final result: Completed %d (%d success, %d failed, %d skipped)",
                  total_completed, success_count, fail_count, skip_count)
    return success_count, fail_count, skip_count, total_completed


def run_scraper_and_log_results(df_meta):
    start_time = datetime.now()
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write("\n%s" % (str(start_time)))

    # xml_res = download_panorama_metadata_xmls(storage_location, pano_list=pano_list)
    xml_end_time = datetime.now()
    xml_duration = int(round((xml_end_time - start_time).total_seconds() / 60.0))
    # with open(os.path.join(storage_location, "log.csv"), 'a') as log:
    #     log.write(",%d,%d,%d,%d,%d" % (xml_res[0], xml_res[1], xml_res[2], xml_res[3], xml_duration))

    # im_res = download_panorama_images(storage_location, pano_list)  # Trailing slash required
    im_res = download_panorama_images(storage_location, df_meta)  # Trailing slash required

    im_end_time = datetime.now()
    im_duration = int(round((im_end_time - xml_end_time).total_seconds() / 60.0))
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write(",%d,%d,%d,%d,%d,%d" % (im_res[0], im_res[1], im_res[2], im_res[3], im_res[4], im_duration))

    # depth_res = generate_depthmapfiles(storage_location)
    depth_end_time = datetime.now()
    # depth_duration = int(round((depth_end_time - im_end_time).total_seconds() / 60.0))
    # with open(os.path.join(storage_location, "log.csv"), 'a') as log:
    #     log.write(",%d,%d,%d,%d,%d" % (depth_res[0], depth_res[1], depth_res[2], depth_res[3], depth_duration))

    total_duration = int(round((depth_end_time - start_time).total_seconds() / 60.0))
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write(",%d" % (total_duration))


# replace with call to make metadata dataframe
print("Fetching pano-ids")
# pano_list = fetch_pano_ids_from_webserver()
# pano_list.remove('tutorial')

# Initialisation of dataframe with downloaded metadata
df_meta = fetch_pano_ids_csv(metadata_csv_path)

##### Debug Line - remove for prod ##########
# pano_list = [pano_list[111], pano_list[112]]
#############################################

print("Fetching Panoramas")
# run_scraper_and_log_results()

run_scraper_and_log_results(df_meta)
