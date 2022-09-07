# !/usr/bin/python3

from SidewalkDB import *
import os
from sys import argv
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
import argparse
import asyncio
import aiohttp
from aiohttp import web
import backoff
from io import BytesIO
import math

try:
    from xml.etree import cElementTree as ET
except ImportError as e:
    from xml.etree import ElementTree as ET


class Enum(object):
    def __init__(self, tuplelist):
        self.tuplelist = tuplelist

    def __getattr__(self, name):
        return self.tuplelist.index(name)


DownloadResult = Enum(('skipped', 'success', 'fallback_success', 'failure'))

delay = 0

# Check proxy settings, if none provided (default) set proxies to False
if proxies['http'] == "http://" or proxies['https'] == "https://":
    proxies['http'] = None
    proxies['https'] = None

parser = argparse.ArgumentParser()
parser.add_argument('d', help='sidewalk_server_domain - FDQN of SidewalkWebpage server to fetch pano list from, i.e. sidewalk-sea.cs.washington.edu')
parser.add_argument('s', help='storage_path - location to store scraped panos')
parser.add_argument('-c', nargs='?', default=None, help='csv_path - location of csv from which to read pano metadata')
args = parser.parse_args()

sidewalk_server_fqdn = args.d # argv[1]
storage_location = args.s # argv[2]
pano_metadata_csv = args.c

print(sidewalk_server_fqdn)
print(storage_location)
print(pano_metadata_csv)
# sidewalk_server_fqdn = "sidewalk-sea.cs.washington.edu" # TODO: use as defaults?
# storage_location = "download_data/"  # The path to where you want to store downloaded GSV panos

if not os.path.exists(storage_location):
    os.makedirs(storage_location)

print("Starting run with pano list fetched from %s and destination path %s" % (sidewalk_server_fqdn, storage_location))


def new_random_delay():
    """
    New random delay value generated
    :return: int between 50 and 250 in steps of 3
    """
    return random.randrange(100, 200, 3)


def random_header():
    """
    Takes the headers provided from the config file and randomly selections and returns one each time this function
    is called.
    :return: a randomly selected header file.
    """
    headers = random.choice(headers_list)
    return headers


# Set up the requests session for better robustness/respect of crawling
# https://stackoverflow.com/questions/23013220/max-retries-exceeded-with-url-in-requests
# Server errors while using proxy - https://findwork.dev/blog/advanced-usage-python-requests-timeouts-retries-hooks/
def request_session():
    """
    Sets up a request session to be used for duration of scripts operation.
    :return: session
    """
    session = requests.Session()
    retry = Retry(total=10, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def get_response(url, session, stream=False):
    """
    Uses requests library to get response
    :param url: url to visit
    :param session: requests session
    :param stream: Default False
    :return: response
    """
    response = session.get(url, headers=random_header(), proxies=proxies, stream=stream)

    if not stream:
        return response
    else:
        return response.raw


def progress_check(csv_pano_log_path):
    """
    Checks download status via a csv: log as skipped if downloaded == 1, failure if download == 0.
    This speeds things up instead of trying to re-download broken links or images.
    NB: This will not check if the failure was due to internet connection being unavailable etc. so use with caution.
    :param csv_pano_log_path:
    :return: pano_ids processed, total count of processed, count of success, count of failure
    """
    # temporary skip/speed up of processed panos
    df_pano_id_check = pd.read_csv(csv_pano_log_path)
    df_id_set = set(df_pano_id_check['gsv_pano_id'])
    total_processed = len(df_pano_id_check.index)
    total_success = df_pano_id_check['downloaded'].sum()
    total_failed = total_processed - total_success
    return df_id_set, total_processed, total_success, total_failed


# Not currently used - data retrieved from Project Sidewalk API
def extract_panowidthheight(path_to_metadata_xml):
    pano = {}
    pano_xml = open(path_to_metadata_xml, 'rb')
    tree = ET.parse(pano_xml)
    root = tree.getroot()
    for child in root:
        if child.tag == 'data_properties':
            pano[child.tag] = child.attrib

    return int(pano['data_properties']['image_width']), int(pano['data_properties']['image_height'])

# Fallback function to get unique pano_ids in case we want to determine panoramas for scraping from a CSV
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
    df_meta = df_meta.drop_duplicates(subset=['gsv_panorama_id']).to_dict('records')
    return df_meta


def fetch_pano_ids_from_webserver():
    unique_ids = set()
    pano_info = []
    conn = http.client.HTTPSConnection(sidewalk_server_fqdn)
    conn.request("GET", "/adminapi/panos")
    r1 = conn.getresponse()
    data = r1.read()
    jsondata = json.loads(data)

    # Structure of JSON data
    # [
    #     {
    #         "gsv_panorama_id": "example-id",
    #         "image_width": 16384,
    #         "image_height": 8192
    #     },
    #     ...
    # ]
    for value in jsondata:
        pano_id = value["gsv_panorama_id"]
        if pano_id not in unique_ids:
            # Check if the pano_id is an empty string.
            if pano_id and pano_id != 'tutorial':
                unique_ids.add(pano_id)
                pano_info.append(value)
            else:
                print("Pano ID is an empty string or is for tutorial")
        else:
            print("Duplicate pano ID")
    assert len(unique_ids) == len(pano_info)
    return pano_info


def download_panorama_images(storage_path, pano_infos):
    logging.basicConfig(filename='scrape.log', level=logging.DEBUG)
    success_count, skipped_count, fallback_success_count, fail_count, total_completed = 0, 0, 0, 0, 0
    total_panos = len(pano_infos)

    # csv log file for pano_id failures, place in 'storage' folder (alongside pano results)
    csv_pano_log_path = os.path.join(storage_path, "gsv_panorama_id_log.csv")
    columns = ['gsv_pano_id', 'downloaded']
    if not exists(csv_pano_log_path):
        df_pano_id_log = pd.DataFrame(columns=columns)
        df_pano_id_log.to_csv(csv_pano_log_path, mode='w', header=True, index=False)
    else:
        df_pano_id_log = pd.read_csv(csv_pano_log_path)
    processed_ids = list(df_pano_id_log['gsv_pano_id'])

    df_id_set, total_completed, skipped_count, fail_count = progress_check(csv_pano_log_path)

    for pano_info in pano_infos:
        pano_id = pano_info['gsv_panorama_id']
        if pano_id in df_id_set:
            continue
        start_time = time.time()
        print("IMAGEDOWNLOAD: Processing pano %s " % (pano_id))
        try:
            pano_dims = (pano_info['image_width'], pano_info['image_height'])
            result_code = download_single_pano(storage_path, pano_id, pano_dims)
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


def download_single_pano(storage_path, pano_id, pano_dims):
    base_url = 'https://maps.google.com/cbk?output=tile&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4'

    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        os.makedirs(destination_dir)
        os.chmod(destination_dir, 0o775 | stat.S_ISGID)

    filename = pano_id + ".jpg"
    out_image_name = os.path.join(destination_dir, filename)

    # Skip download if image already exists
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    final_image_width = int(pano_dims[0]) if pano_dims[0] is not None else None
    final_image_height = int(pano_dims[1]) if pano_dims[1] is not None else None
    zoom = None

    session = request_session()

    # Check XML metadata for image width/height max zoom if its downloaded.
    xml_metadata_path = os.path.join(destination_dir, pano_id + ".xml")
    if os.path.isfile(xml_metadata_path):
        print(xml_metadata_path)
        with open(xml_metadata_path, 'rb') as pano_xml:
            tree = ET.parse(pano_xml)
            root = tree.getroot()

            # Get the number of zoom levels.
            for child in root:
                if child.tag == 'data_properties':
                    zoom = int(child.attrib['num_zoom_levels'])
                    if final_image_width is None: final_image_width = int(child.attrib['image_width'])
                    if final_image_height is None: final_image_height = int(child.attrib['image_height'])

            # If there is no zoom in the XML, then we skip this and try some zoom levels below.
            if zoom is not None:
                # Check if the image exists (occasionally we will have XML but no JPG).
                test_url = f'{base_url}&zoom={zoom}&x=0&y=0&panoid={pano_id}'
                test_request = get_response(test_url, session, stream=True)
                test_tile = Image.open(test_request)
                if test_tile.convert("L").getextrema() == (0, 0):
                    return DownloadResult.failure

    # If we did not find image width/height from API or XML, then set download to failure.
    if final_image_width is None or final_image_height is None:
        return DownloadResult.failure

    # If we did not find a zoom level in the XML above, then try a couple zoom level options here.
    if zoom is None:
        url_zoom_3 = f'{base_url}&zoom=3&x=0&y=0&panoid={pano_id}'
        url_zoom_5 = f'{base_url}&zoom=5&x=0&y=0&panoid={pano_id}'

        req_zoom_3 = get_response(url_zoom_3, session, stream=True)
        im_zoom_3 = Image.open(req_zoom_3)
        req_zoom_5 = get_response(url_zoom_5, session, stream=True)
        im_zoom_5 = Image.open(req_zoom_5)

        # In some cases (e.g., old GSV images), we don't have zoom level 5, so Google returns a
        # transparent image. This means we need to set the zoom level to 3. Google also returns a
        # transparent image if there is no imagery. So check at both zoom levels. How to check:
        # http://stackoverflow.com/questions/14041562/python-pil-detect-if-an-image-is-completely-black-or-white
        if im_zoom_5.convert("L").getextrema() != (0, 0):
            zoom = 5
        elif im_zoom_3.convert("L").getextrema() != (0, 0):
            zoom = 3
        else:
            # can't determine zoom
            return DownloadResult.failure

    final_im_dimension = (final_image_width, final_image_height)

    def generate_gsv_urls(zoom):
        """
        Generates all valid urls of GSV tiles to be downloaded for stitching into single panorama.
        :param zoom: the valid/working zoom value for this pano_id
        :return: a list of all valid urls to be accessed for downloading the panorama
        """
        sites_gsv = []
        for y in range(int(math.ceil(final_image_height / 512.0))):
            for x in range(int(math.ceil(final_image_width / 512.0))):
                url = f'{base_url}&zoom={zoom}&x={str(x)}&y={str(y)}&panoid={pano_id}'
                sites_gsv.append((str(x) + " " + str(y), url))
        return sites_gsv

    @backoff.on_exception(backoff.expo, (aiohttp.web.HTTPServerError, aiohttp.ClientError, aiohttp.ClientResponseError,
                                         aiohttp.ServerConnectionError, aiohttp.ServerDisconnectedError,
                                         aiohttp.ClientHttpProxyError), max_tries=10)
    async def download_single_gsv(session, url):
        """
        Downloads a single 512x512 panorama tile
        :param session: requests sessions object
        :param url: the url to be accessed where the target image is
        :return: a list containing - x and y position of the download image, downloaded image
        """
        # TODO: possibly not needed
        # # If not using proxies, delay for a little bit to avoid hammering the server
        # if proxies["http"] is None:
        #     time.sleep(new_random_delay() / 1000)
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
        """
        For the given list of sites/urls that make up a single GSV panorama, starts the connections, breaks each of the
        sites into tasks, then runs these tasks through asyncio.
        :param sites: list of all valid urls that make up the image
        :return: responses from the tasks which contains all the images and their position x and y data
        (needed for stitching)
        """
        conn = aiohttp.TCPConnector(limit=thread_count)
        async with aiohttp.ClientSession(raise_for_status=True, connector=conn) as session:
            tasks = []
            for url in sites:
                task = asyncio.ensure_future(download_single_gsv(session, url))
                tasks.append(task)
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            return responses

    blank_image = Image.new('RGB', final_im_dimension, (0, 0, 0, 0))
    sites = generate_gsv_urls(zoom)
    all_pano_images = asyncio.get_event_loop().run_until_complete(download_all_gsv_images(sites))

    for cell_image in all_pano_images:
        img = Image.open(BytesIO(cell_image[1]))
        img = img.resize((512, 512))
        x, y = int(str.split(cell_image[0])[0]), int(str.split(cell_image[0])[1])
        blank_image.paste(img, (512 * x, 512 * y))

    # TODO: sleep after entire pano downlaoded versus each tile?

    if zoom == 3:
        blank_image = blank_image.resize(final_im_dimension, Image.ANTIALIAS)
    blank_image.save(out_image_name, 'jpeg')
    os.chmod(out_image_name, 0o664)
    return DownloadResult.success


def download_panorama_metadata_xmls(storage_path, pano_infos):
    '''
     This method downloads a xml file that contains depth information from GSV. It first
     checks if we have a folder for each pano_id, and checks if we already have the corresponding
     depth file or not.
    '''
    total_panos = len(pano_infos)
    success_count = 0
    fail_count = 0
    skipped_count = 0
    total_completed = 0

    for pano_info in pano_infos:
        pano_id = pano_info['gsv_panorama_id']
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


def download_single_metadata_xml(storage_path, pano_id):
    base_url = "https://maps.google.com/cbk?output=xml&cb_client=maps_sv&hl=en&dm=1&pm=1&ph=1&renderer=cubic,spherical&v=4&panoid="

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
    lineOne = req.content.splitlines()[0]
    lineFive = req.content.splitlines()[4]

    if lineOne == b'<?xml version="1.0" encoding="UTF-8" ?><panorama/>' or lineFive == b'  <title>Error 404 (Not Found)!!1</title>':
        return DownloadResult.failure
    else:
        with open(destination_file, 'wb') as f:
            f.write(req.content)
        os.chmod(destination_file, 0o664)

        return DownloadResult.success


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


def run_scraper_and_log_results(pano_infos):
    start_time = datetime.now()
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write("\n%s" % (str(start_time)))

    xml_res = download_panorama_metadata_xmls(storage_location, pano_infos)
    xml_end_time = datetime.now()
    xml_duration = int(round((xml_end_time - start_time).total_seconds() / 60.0))
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write(",%d,%d,%d,%d,%d" % (xml_res[0], xml_res[1], xml_res[2], xml_res[3], xml_duration))

    im_res = download_panorama_images(storage_location, pano_infos)
    im_end_time = datetime.now()
    im_duration = int(round((im_end_time - xml_end_time).total_seconds() / 60.0))
    with open(os.path.join(storage_location, "log.csv"), 'a') as log:
        log.write(",%d,%d,%d,%d,%d,%d" % (im_res[0], im_res[1], im_res[2], im_res[3], im_res[4], im_duration))

    depth_res = generate_depthmapfiles(storage_location)
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
    pano_infos = fetch_pano_ids_from_webserver()


# Uncomment this to test on a smaller subset of the pano_info
# pano_infos = random.sample(pano_infos, 10)
print(len(pano_infos))
# print(pano_infos)

# Use pano_id list and associated info to gather panos from GSV API
print("Fetching Panoramas")
run_scraper_and_log_results(pano_infos)
