# !/usr/bin/python2

from SidewalkDB import *
from sys import argv

import os
import stat
import httplib
import json
import logging
import cStringIO
import enum

import urllib
import urllib2

from PIL import Image
from random import shuffle
import fnmatch

from subprocess import PIPE,STDOUT

try:
    from xml.etree import cElementTree as ET
except ImportError, e:
    from xml.etree import ElementTree as ET

class Enum(object): 
    def __init__(self, tupleList):
            self.tupleList = tupleList

    def __getattr__(self, name):
            return self.tupleList.index(name)

DownloadResult = Enum(('skipped', 'success', 'fallback_success', 'failure'))

delay = 0

if len(argv) != 3:
    print("Usage: python DownloadRunner.py sidewalk_server_domain storage_path")
    print("    sidewalk_server_domain - FDQN of SidewalkWebpage server to fetch pano list from")
    print("    storage_path - location to store scraped panos")
    print("    Example: python DownloadRunner.py sidewalk-sea.cs.washington.edu /destination/path")
    exit(0)

sidewalk_server_fqdn = argv[1]
storage_location = argv[2]

if not os.path.exists(storage_location):
    os.mkdir(storage_location)

print("Starting run with pano list fetched from %s and destination path %s" % (sidewalk_server_fqdn, storage_location))

def check_download_failed_previously(panoId):
    if panoId in open('scrape.log').read():
        return True
    else:
        return False

def extract_panowidthheight(path_to_metadata_xml):
    pano = {}
    pano_xml = open(path_to_metadata_xml, 'rb')
    tree = ET.parse(pano_xml)
    root = tree.getroot()
    for child in root:
        if child.tag == 'data_properties':
            pano[child.tag] = child.attrib
    
    return (int(pano['data_properties']['image_width']),int(pano['data_properties']['image_height']))


def fetch_pano_ids_from_webserver():
    unique_ids = []
    conn = httplib.HTTPSConnection(sidewalk_server_fqdn)
    conn.request("GET", "/adminapi/labels/panoid")
    r1 = conn.getresponse()
    data = r1.read()
    # print(data)
    jsondata = json.loads(data)

    for value in jsondata["features"]:
        if value["properties"]["gsv_panorama_id"] not in unique_ids:
            #Check if the pano_id is an empty string
            if value["properties"]["gsv_panorama_id"]:
                unique_ids.append(value["properties"]["gsv_panorama_id"])
            else:
                print "Pano ID is an empty string"
    return unique_ids


def download_panorama_images(storage_path, pano_list):
    logging.basicConfig(filename='scrape.log', level=logging.DEBUG)

    success_count = 0
    skipped_count = 0
    fallback_success_count = 0
    fail_count = 0
    total_completed = 0
    total_panos = len(pano_list)

    for pano_id in pano_list:
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
        except Exception as e:
            fail_count += 1
            logging.error("IMAGEDOWNLOAD: Failed to download pano %s due to error %s", pano_id, str(e))
        total_completed = success_count + fallback_success_count + fail_count + skipped_count
        print("IMAGEDOWNLOAD: Completed %d of %d (%d success, %d fallback success, %d failed, %d skipped)" 
        % (total_completed, total_panos, success_count, fallback_success_count, fail_count, skipped_count))

    logging.debug("IMAGEDOWNLOAD: Final result: Completed %d of %d (%d success, %d fallback success, %d failed, %d skipped)",
                    total_completed, 
                    total_panos, 
                    success_count, 
                    fallback_success_count, 
                    fail_count, 
                    skipped_count)
    return

def download_single_pano(storage_path, pano_id):
    base_url = 'http://maps.google.com/cbk?'
    pano_xml_path = os.path.join(storage_path, pano_id[:2], pano_id + ".xml")
    
    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        os.makedirs(destination_dir)
        os.chmod(destination_dir, 0775 | stat.S_ISGID)

    filename = pano_id + ".jpg"
    out_image_name = os.path.join(destination_dir, filename)

    # Skip download if image already exists
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    final_image_height = 6656
    final_image_width = 13312
    if sidewalk_server_fqdn == 'sidewalk-sea.cs.washington.edu':
        final_image_width = 16384
        final_image_height = 8192
    try:
        (final_image_width, final_image_height) = extract_panowidthheight(pano_xml_path)
    except Exception as e:
        print("IMAGEDOWNLOAD - WARN - using fallback pano size for %s" % (pano_id))
    final_im_dimension = (final_image_width, final_image_height)

    # In some cases (e.g., old GSV images), we don't have zoom level 5, so Google returns a
    # transparent image. This means we need to set the zoom level to 3. Google also returns a
    # transparent image if there is no imagery. So check at both zoom levels. How to check:
    # http://stackoverflow.com/questions/14041562/python-pil-detect-if-an-image-is-completely-black-or-white
    req_zoom_5 = urllib.urlopen('http://maps.google.com/cbk?output=tile&zoom=5&x=0&y=0&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4&panoid=' + pano_id)
    im_zoom_5 = Image.open(cStringIO.StringIO(req_zoom_5.read()))
    req_zoom_3 = urllib.urlopen('http://maps.google.com/cbk?output=tile&zoom=3&x=0&y=0&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4&panoid=' + pano_id)
    im_zoom_3 = Image.open(cStringIO.StringIO(req_zoom_3.read()))

    if im_zoom_5.convert("L").getextrema() != (0, 0):
        fallback = False
        zoom = 5
        image_width = final_image_width
        image_height = final_image_height
    elif im_zoom_3.convert("L").getextrema() != (0, 0):
        fallback = True
        zoom = 3
        image_width = 3328
        image_height = 1664
    else:
        return DownloadResult.failure

    im_dimension = (image_width, image_height)
    blank_image = Image.new('RGB', im_dimension, (0, 0, 0, 0))

    for y in range(int(round(image_height / 512.0))):
        for x in range(int(round(image_width / 512.0))):
            url_param = 'output=tile&zoom=' + str(zoom) + '&x=' + str(x) + '&y=' + str(
                y) + '&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4&panoid=' + pano_id
            url = base_url + url_param

            # Open an image, resize it to 512x512, and paste it into a canvas
            req = urllib.urlopen(url)
            file = cStringIO.StringIO(req.read())

            im = Image.open(file)
            im = im.resize((512, 512))

            blank_image.paste(im, (512 * x, 512 * y))

            # Wait a little bit so you don't get blocked by Google
            sleep_in_milliseconds = float(delay) / 1000
            sleep(sleep_in_milliseconds)

    if fallback:
        blank_image = blank_image.resize(final_im_dimension, Image.ANTIALIAS)
        blank_image.save(out_image_name, 'jpeg')
        os.chmod(out_image_name, 0664)
        return DownloadResult.fallback_success
    else:
        blank_image.save(out_image_name, 'jpeg')
        os.chmod(out_image_name, 0664)
        return DownloadResult.success

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
    return

def download_single_metadata_xml(storage_path, pano_id):
    base_url = "http://maps.google.com/cbk?output=xml&cb_client=maps_sv&hl=en&dm=1&pm=1&ph=1&renderer=cubic,spherical&v=4&panoid="

    # Check if the directory exists. Then check if the file already exists and skip if it does.
    destination_folder = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_folder):
        os.makedirs(destination_folder)
        os.chmod(destination_folder, 0775 | stat.S_ISGID)

    filename = pano_id + ".xml"
    destination_file = os.path.join(destination_folder, filename)
    if os.path.isfile(destination_file):
        return DownloadResult.skipped

    url = base_url + pano_id

    # Check if the XML file is empty. If not, write it out to a file and set the permissions.
    req = urllib2.urlopen(url)
    firstline = req.readline()
    if firstline == '<?xml version="1.0" encoding="UTF-8" ?><panorama/>':
        return DownloadResult.failure
    else:
        with open(destination_file, 'wb') as f:
            f.write(firstline)
            for line in req:
                f.write(line)
        os.chmod(destination_file, 0664)

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
                continue

            output_code = call(["./decode_depthmap", xml_location, output_file])
            if output_code == 0:
                os.chmod(output_file, 0664)
                success_count += 1
            else:
                fail_count += 1
                logging.error("GENERATEDEPTH: Could not create depth.txt for pano %s, error code was %s", pano_id, str(output_code))
            total_completed = fail_count + success_count + skip_count
            print("GENERATEDEPTH: Completed %d of %d (%d success, %d failed, %d skipped)" %
                (total_completed, total_panos, success_count, fail_count, skip_count))

    logging.debug("GENERATEDEPTH: Final result: Completed %d of %d (%d success, %d failed, %d skipped)",
        total_completed, total_panos, success_count, fail_count, skip_count)

print "Fetching pano-ids"
pano_list = fetch_pano_ids_from_webserver()
pano_list.remove('tutorial')
shuffle(pano_list)

##### Debug Line - remove for prod ##########
# pano_list = [pano_list[111], pano_list[112]]
#############################################

print "Fetching Panoramas"
download_panorama_metadata_xmls(storage_location, pano_list=pano_list)
download_panorama_images(storage_location, pano_list)  # Trailing slash required
generate_depthmapfiles(storage_location)
