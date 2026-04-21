# Google Street View panorama downloader.
#
# Stitches 512x512 tiles from Google's undocumented CBK endpoint into a single equirectangular JPEG. The XML
# metadata / depth-map branch of this module has been dead since 2022 (endpoint removed); it stays here so that if the
# endpoint returns, --attempt-depth still works.

import asyncio
import fnmatch
import logging
import math
import os
import random
import stat
from io import BytesIO
from subprocess import call

import aiohttp
import backoff
import requests
from PIL import Image
from aiohttp import web  # noqa: F401  (imported for aiohttp.web.HTTPServerError)
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from config import headers_list, proxies, thread_count

try:
    from xml.etree import cElementTree as ET
except ImportError:
    from xml.etree import ElementTree as ET

from .common import DownloadResult

# Normalize proxy config: treat the sentinel placeholders in config.py as unset.
_proxies = dict(proxies)
if _proxies.get('http') == 'http://' or _proxies.get('https') == 'https://':
    _proxies['http'] = None
    _proxies['https'] = None


def _random_header():
    return random.choice(headers_list)


def _request_session():
    session = requests.Session()
    retry = Retry(total=5, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def _get_response(url, session, stream=False):
    response = session.get(url, headers=_random_header(), proxies=_proxies, stream=stream)
    if not stream:
        return response
    return response.raw


def download_single_pano(storage_path, pano_info):
    pano_id = pano_info['pano_id']
    pano_dims = (pano_info.get('width'), pano_info.get('height'))

    base_url = 'https://maps.google.com/cbk?output=tile&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4'

    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        os.makedirs(destination_dir)
        os.chmod(destination_dir, 0o775 | stat.S_ISGID)

    filename = pano_id + ".jpg"
    out_image_name = os.path.join(destination_dir, filename)

    # Skip download if image already exists.
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    final_image_width = int(pano_dims[0]) if pano_dims[0] is not None else None
    final_image_height = int(pano_dims[1]) if pano_dims[1] is not None else None
    zoom = None

    session = _request_session()

    # Check XML metadata for image width/height max zoom if its downloaded.
    xml_metadata_path = os.path.join(destination_dir, pano_id + ".xml")
    if os.path.isfile(xml_metadata_path):
        print(xml_metadata_path)
        with open(xml_metadata_path, 'rb') as pano_xml:
            try:
                tree = ET.parse(pano_xml)
                root = tree.getroot()

                # Get the number of zoom levels.
                for child in root:
                    if child.tag == 'data_properties':
                        zoom = int(child.attrib['num_zoom_levels'])
                        if final_image_width is None:
                            final_image_width = int(child.attrib['width'])
                        if final_image_height is None:
                            final_image_height = int(child.attrib['height'])

                # If there is no zoom in the XML, then we skip this and try some zoom levels below.
                if zoom is not None:
                    # Check if the image exists (occasionally we will have XML but no JPG).
                    test_url = f'{base_url}&zoom={zoom}&x=0&y=0&panoid={pano_id}'
                    test_request = _get_response(test_url, session, stream=True)
                    test_tile = Image.open(test_request)
                    if test_tile.convert("L").getextrema() == (0, 0):
                        return DownloadResult.failure
            except Exception:
                pass

    # If we did not find image width/height from API or XML, then set download to failure.
    if final_image_width is None or final_image_height is None:
        return DownloadResult.failure

    # If we did not find a zoom level in the XML above, then try a couple zoom level options here.
    if zoom is None:
        url_zoom_3 = f'{base_url}&zoom=3&x=0&y=0&panoid={pano_id}'
        url_zoom_5 = f'{base_url}&zoom=5&x=0&y=0&panoid={pano_id}'

        req_zoom_3 = _get_response(url_zoom_3, session, stream=True)
        im_zoom_3 = Image.open(req_zoom_3)
        req_zoom_5 = _get_response(url_zoom_5, session, stream=True)
        im_zoom_5 = Image.open(req_zoom_5)

        # In some cases (e.g., old GSV images), we don't have zoom level 5, so Google returns a transparent image. This
        # means we need to set the zoom level to 3. Google also returns a transparent image if there is no imagery.
        # So check at both zoom levels. How to check:
        # http://stackoverflow.com/questions/14041562/python-pil-detect-if-an-image-is-completely-black-or-white
        if im_zoom_5.convert("L").getextrema() != (0, 0):
            zoom = 5
        elif im_zoom_3.convert("L").getextrema() != (0, 0):
            zoom = 3
        else:
            # Can't determine zoom.
            return DownloadResult.failure

    final_im_dimension = (final_image_width, final_image_height)

    def generate_gsv_urls(zoom):
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
        async with session.get(url[1], proxy=_proxies["http"], headers=_random_header()) as response:
            head_content = response.headers['Content-Type']
            # Ensures content type is an image.
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

    blank_image = Image.new('RGB', final_im_dimension, (0, 0, 0, 0))
    sites = generate_gsv_urls(zoom)
    all_pano_images = asyncio.get_event_loop().run_until_complete(download_all_gsv_images(sites))

    for cell_image in all_pano_images:
        img = Image.open(BytesIO(cell_image[1]))
        img = img.resize((512, 512))
        x, y = int(str.split(cell_image[0])[0]), int(str.split(cell_image[0])[1])
        blank_image.paste(img, (512 * x, 512 * y))

    if zoom == 3:
        blank_image = blank_image.resize(final_im_dimension, Image.ANTIALIAS)
    blank_image.save(out_image_name, 'jpeg')
    os.chmod(out_image_name, 0o664)
    return DownloadResult.success


def download_panorama_metadata_xmls(storage_path, pano_infos):
    """Bulk-download the XML metadata that backs depth-map generation.

    Expected to fail end-to-end since 2022 (endpoint removed); only invoked when --attempt-depth is passed.
    """
    total_panos = len(pano_infos)
    success_count = 0
    fail_count = 0
    skipped_count = 0
    total_completed = 0

    for pano_info in pano_infos:
        pano_id = pano_info['pano_id']
        print("METADOWNLOAD: Processing pano %s " % (pano_id))
        try:
            result_code = _download_single_metadata_xml(storage_path, pano_id)
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


def _download_single_metadata_xml(storage_path, pano_id):
    base_url = "https://maps.google.com/cbk?output=xml&cb_client=maps_sv&hl=en&dm=1&pm=1&ph=1&renderer=cubic,spherical&v=4&panoid="

    destination_folder = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_folder):
        os.makedirs(destination_folder)
        os.chmod(destination_folder, 0o775 | stat.S_ISGID)

    filename = pano_id + ".xml"
    destination_file = os.path.join(destination_folder, filename)
    if os.path.isfile(destination_file):
        return DownloadResult.skipped

    url = base_url + pano_id

    session = _request_session()
    req = _get_response(url, session)

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
    for root, dirnames, filenames in os.walk(path_to_scrapes):
        for filename in fnmatch.filter(filenames, '*.xml'):
            xml_location = os.path.join(root, filename)

            pano_id = filename[:-4]
            print("GENERATEDEPTH: Processing pano %s " % (pano_id))

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
