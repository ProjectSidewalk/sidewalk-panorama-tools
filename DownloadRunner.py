# !/usr/bin/python

# ****************************************
# Specify the file storage location below
# ****************************************
storage_location = "/home/anthony/Downloads/test/"

from SidewalkDB import *

import os
import httplib
import json
import logging
import cStringIO

import urllib
import urllib2

from PIL import Image
from random import shuffle
import fnmatch


def check_download_failed_previously(panoId):
    if panoId in open('scrape.log').read():
        return True
    else:
        return False


def fetch_pano_ids_from_webserver():
    unique_ids = []
    conn = httplib.HTTPSConnection("sidewalk.umiacs.umd.edu")
    conn.request("GET", "/adminapi/labels/panoid")
    r1 = conn.getresponse()
    data = r1.read()
    # print(data)
    jsondata = json.loads(data)

    for value in jsondata["features"]:
        if value["properties"]["gsv_panorama_id"] not in unique_ids:
            unique_ids.append(value["properties"]["gsv_panorama_id"])
    return unique_ids


def download_panorama_images(storage_path, pano_list=None):
    logging.basicConfig(filename='scrape.log', level=logging.DEBUG)

    if pano_list is None:
        unique_ids = fetch_pano_ids_from_webserver()
    else:
        unique_ids = pano_list

    counter = 0
    failed = 0
    im_dimension = (512 * 26, 512 * 13)
    blank_image = Image.new('RGBA', im_dimension, (0, 0, 0, 0))
    base_url = 'http://maps.google.com/cbk?'
    shuffle(unique_ids)
    for pano_id in unique_ids:
        print '-- Extracting images for', pano_id,

        destination_dir = os.path.join(storage_path, pano_id[:2])
        if not os.path.isdir(destination_dir):
            os.makedirs(destination_dir)

        filename = pano_id + ".jpg"

        out_image_name = os.path.join(destination_dir, filename)
        if os.path.isfile(out_image_name):
            print 'File already exists.'

            counter = counter + 1
            print 'Completed ' + str(counter) + ' of ' + str(len(unique_ids))
            continue

        for y in range(13):
            for x in range(26):
                url_param = 'output=tile&zoom=5&x=' + str(x) + '&y=' + str(
                    y) + '&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4&panoid=' + pano_id
                url = base_url + url_param

                # Open an image, resize it to 512x512, and paste it into a canvas
                req = urllib.urlopen(url)
                file = cStringIO.StringIO(req.read())
                im = Image.open(file)
                im = im.resize((512, 512))

                blank_image.paste(im, (512 * x, 512 * y))

                # Wait a little bit so you don't get blocked by Google
                # sleep_in_milliseconds = float(delay) / 1000
                # sleep(sleep_in_milliseconds)
            print '.',
        print

        # In some cases (e.g., old GSV images), we don't have zoom level 5,
        # so Google returns a tranparent image. This means we need to set the
        # zoom level to 3.

        # Check if the image is transparent
        # http://stackoverflow.com/questions/14041562/python-pil-detect-if-an-image-is-completely-black-or-white
        extrema = blank_image.convert("L").getextrema()
        if extrema == (0, 0):
            print("Panorama %s is an old image and does not have the tiles for zoom level")
            temp_im_dimension = (int(512 * 6.5), int(512 * 3.25))
            temp_blank_image = Image.new('RGBA', temp_im_dimension, (0, 0, 0, 0))
            for y in range(3):
                for x in range(7):
                    url_param = 'output=tile&zoom=3&x=' + str(x) + '&y=' + str(
                        y) + '&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4&panoid=' + pano_id
                    url = base_url + url_param
                    # Open an image, resize it to 512x512, and paste it into a canvas
                    req = urllib.urlopen(url)
                    file = cStringIO.StringIO(req.read())
                    im = Image.open(file)
                    im = im.resize((512, 512))

                    temp_blank_image.paste(im, (512 * x, 512 * y))

                    # Wait a little bit so you don't get blocked by Google
                    # sleep_in_milliseconds = float(delay) / 1000
                    # sleep(sleep_in_milliseconds)
                print '.',
            print
            temp_blank_image = temp_blank_image.resize(im_dimension, Image.ANTIALIAS)  # resize
            temp_blank_image.save(out_image_name, 'jpeg')
        else:
            blank_image.save(out_image_name, 'jpeg')
        print 'Done.'
        counter += 1
        print 'Completed ' + str(counter) + ' of ' + str(len(unique_ids))
    return


def download_panorama_depthdata(storage_path, decode=True, pano_list=None):
    '''
     This method downloads a xml file that contains depth information from GSV. It first
     checks if we have a folder for each pano_id, and checks if we already have the corresponding
     depth file or not.
    '''

    base_url = "http://maps.google.com/cbk?output=xml&cb_client=maps_sv&hl=en&dm=1&pm=1&ph=1&renderer=cubic,spherical&v=4&panoid="

    if pano_list is None:
        pano_ids = fetch_pano_ids_from_webserver()
    else:
        pano_ids = pano_list

    for pano_id in pano_ids:
        print '-- Extracting depth data for', pano_id, '...',
        # Check if the directory exists. Then check if the file already exists and skip if it does.
        destination_folder = os.path.join(storage_path, pano_id[:2])
        if not os.path.isdir(destination_folder):
            os.makedirs(destination_folder)

        filename = pano_id + ".xml"
        destination_file = os.path.join(destination_folder, filename)
        if os.path.isfile(destination_file):
            print 'File already exists.'
            continue

        url = base_url + pano_id
        with open(destination_file, 'wb') as f:
            req = urllib2.urlopen(url)
            for line in req:
                f.write(line)

        print 'Done.'

    return


def generate_depthmapfiles(path_to_scrapes):
    # Iterate through all .xml files in specified path, recursively
    for root, dirnames, filenames in os.walk(path_to_scrapes):
        for filename in fnmatch.filter(filenames, '*.xml'):
            xml_location = os.path.join(root, filename)
            print(xml_location)
            print(filename)

            # Pano id is XML filename minus the extension
            pano_id = filename[:-4]

            # Generate a .depth.txt file for the .xml file
            output_file = os.path.join(root, pano_id + ".depth.txt")
            call(["./decode_depthmap", xml_location, output_file])


pano_list = fetch_pano_ids_from_webserver()

download_panorama_images(storage_location, pano_list=pano_list)  # Trailing slash required
download_panorama_depthdata(storage_location, pano_list=pano_list)
generate_depthmapfiles(storage_location)
