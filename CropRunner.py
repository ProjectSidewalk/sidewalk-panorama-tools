"""
** Crop Extractor for Project Sidewalk **

Given label metadata from the Project Sidewalk database, this script will
extract JPEG crops of the features that have been labeled. The required metadata
may be obtained by running the SQL query in "samples/getFullLabelList.sql" on the
Sidewalk database, and exporting the results in CSV format. You must supply the
path to the CSV file containing this data below. You can find an example of what
this file should look like in "samples/labeldata.csv".

Additionally, you should have downloaded original panorama
images from Street View using DownloadRunner.py. You will need to supply the
path to the folder containing these files.

"""

import sys
import csv
import logging
import os
from PIL import Image, ImageDraw
import json
import requests
from requests.adapters import HTTPAdapter
import urllib3
from urllib3.util.retry import Retry
import pandas as pd
import argparse

# *****************************************
# Update paths below                      *
# *****************************************

# Mark the center of the crop?
mark_label = True

logging.basicConfig(filename='crop.log', level=logging.DEBUG)

try:
    from xml.etree import cElementTree as ET
except ImportError as e:
    from xml.etree import ElementTree as ET

parser =  argparse.ArgumentParser()
group_parser = parser.add_mutually_exclusive_group(required=True)
group_parser.add_argument('-d', nargs='?', help='sidewalk_server_domain - FDQN of SidewalkWebpage server to fetch label list from, i.e. sidewalk-columbus.cs.washington.edu')
group_parser.add_argument('-f', nargs='?', help='metadata_file - path to file containing label_ids and their properties. It may be csv or json. i.e. samples/labeldata.csv')
parser.add_argument('-s', default='/tmp/download_dest/', help='directory_of_panos - path to directory containing panoramas downloaded using DownloadRunner.py. default=/tmp/download_dest/')
parser.add_argument('-c', default='/crops/', help='directory_of_crops - path to location for saving the crops. default=/crops/')
args = parser.parse_args()

# FDQN SidewalkWebpage server
sidewalk_server_fdqn = args.d
# Path to json or CSV data from database
label_metadata_file = args.f
# Path to panoramas downloaded using DownloadRunner.py.
gsv_pano_path = args.s
# Path to location for saving the crops
crop_destination_path = args.c

def request_session():
    """
    Sets up a request session to properly handle server HTTP requests to gather metadata from
    webserver. Handles possible HTTP errors to retry several times
    :return: session
    """
    session = requests.Session()
    retries = Retry(total=5,
                    connect=5,
                    status_forcelist=[429, 500, 502, 503, 504],
                    backoff_factor=1,
                    raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('https://', adapter)
    return session

# This function may be deprecated, since json is the only and current file format of metadata
def fetch_label_ids_csv(metadata_csv_path):
    """
    Reads metadata from a csv. Useful for old csv formats of cvMetadata such as cv-metadata-seatle.csv
    :param metadata_csv_path: The path to the metadata csv file and the file's name eg. sample/metadata-seattle.csv
    :return: A list of dicts containing the follow metadata: gsv_panorama_id, pano_x, pano_y, zoom, label_type_id,
             camera_heading, heading, pitch, label_id, width, height, tile_width, tile_height, image_date, imagery_type,
             pano_lat, pano_lng, label_lat, label_lng, computation_method, copyright
    """
    df_meta = pd.read_csv(metadata_csv_path)
    df_meta = df_meta.drop_duplicates(subset=['gsv_panorama_id']).to_dict('records')
    return df_meta

def json_to_list(jsondata):
    """
    Transforms json like object to a list of dict to be read in bulk_extract_crops() to crop panos with label metadata
    :param jsondata: json object containing label ids and their associated properties
    :return: A list of dicts containing the following metadata: label_id, gsv_panorama_id, label_type_id, agree_count,
    disagree_count, notsure_count, pano_width, pano_height, pano_x, pano_y, canvas_width, canvas_height, canvas_x,
    canvas_y, zoom, heading, pitch, camera_heading, camera_pitch
    """
    unique_label_ids = set()
    label_info = []
    
    for value in jsondata:
        label_id = value["label_id"]
        if label_id not in unique_label_ids:
            unique_label_ids.add(label_id)
            label_info.append(value)
        else:
            print("Duplicate label ID")
    assert len(unique_label_ids) == len(label_info)
    return label_info

def fetch_cvMetadata_from_file(metadata_json_path):
    """
    Reads json file to exctact labels.
    :param metadata_file_path: the path of the json file containing all label ids and their associated data.
    :return: A list of dicts containing the following metadata: label_id, gsv_panorama_id, label_type_id, agree_count,
    disagree_count, notsure_count, pano_width, pano_height, pano_x, pano_y, canvas_width, canvas_height, canvas_x,
    canvas_y, zoom, heading, pitch, camera_heading, camera_pitch
    """
    with open(metadata_json_path) as json_file:
        json_meta = json.load(json_file)
    return json_to_list(json_meta)

# https://stackoverflow.com/questions/54356759/python-requests-how-to-determine-response-code-on-maxretries-exception
def fetch_cvMetadata_from_server(server_fdqn):
    """
    Function that uses HTTP request to server to fetch cvMetadata. Then parses the data to json and transforms it
    into list of dicts. Each element associates to a single label.
    :return: list of labels
    """

    url='https://' + server_fdqn + '/adminapi/labels/cvMetadata'

    session = request_session()
    try:
        print("Getting metadata from web server")
        response = session.get(url)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        logging.error('HTTPError: {}'.format(e))
        print("Cannot fetch metadata from webserver. Check log file")
        sys.exit(1)
    except urllib3.exceptions.MaxRetryError as e:
        logging.error('Retries: '.format(e))
        print("Cannot fetch metadata from webserver. Check log file")
        sys.exit(1)

    jsondata = response.json()
    return json_to_list(jsondata)

def predict_crop_size(pano_y, pano_height):
    """
    I honestly have no idea what the math behind this is supposed to be, but if gives reasonably sized crops! When
    written, it just used pano_y. But the y-pixel location was actually y = pnoa_height / 2 - pano_y. Since we don't
    know what's going on with the math here, I just reverse-engineered the old input instead of rewriting the func.
    """
    old_pano_y = pano_height / 2 - pano_y
    crop_size = 0
    distance = max(0, 19.80546390 + 0.01523952 * old_pano_y)

    if distance > 0:
        crop_size = 8725.6 * (distance ** -1.192)
    if crop_size > 1500 or distance == 0:
        crop_size = 1500
    if crop_size < 50:
        crop_size = 50

    return crop_size


def make_single_crop(path_to_image, pano_x, pano_y, output_filename, draw_mark=False):
    """
    Makes a crop around the object of interest
    :param path_to_image: where the GSV pano is stored
    :param pano_x: x-pixel of label on the GSV image
    :param pano_y: y-pixel of label on the GSV image
    :param output_filename: name of file for saving
    :param draw_mark: if a dot should be drawn in the centre of the object/image
    :return: none
    """
    pano = Image.open(path_to_image)
    draw = ImageDraw.Draw(pano)

    pano_width = pano.size[0]
    pano_height = pano.size[1]
    print(pano_width, pano_height)

    predicted_crop_size = predict_crop_size(pano_y, pano_height)
    crop_width = predicted_crop_size
    crop_height = predicted_crop_size

    r = 10
    if draw_mark:
        draw.ellipse((pano_x - r, pano_y - r, pano_x + r, pano_y + r), fill=128)

    print("Plotting at " + str(pano_x) + "," + str(pano_y))

    top_left_x = pano_x - crop_width / 2
    top_left_y = pano_y - crop_height / 2
    cropped_square = pano.crop((top_left_x, top_left_y, top_left_x + crop_width, top_left_y + crop_height))
    cropped_square.save(output_filename)

    return


def bulk_extract_crops(label_infos, path_to_gsv_scrapes, destination_dir, mark_label=False):
    counter = 0
    no_metadata_fail = 0
    no_pano_fail = 0

    for row in label_infos:
        if counter == 0:
            counter += 1
            continue

        pano_id = row['gsv_panorama_id']
        print(pano_id)
        pano_x = float(row['pano_x'])
        pano_y = float(row['pano_y'])
        label_type = int(row['label_type_id'])
        label_id = int(row['label_id'])

        pano_img_path = os.path.join(path_to_gsv_scrapes, pano_id[:2], pano_id + ".jpg")

        print(pano_img_path)
        # Extract the crop
        if os.path.exists(pano_img_path):
            counter += 1
            destination_folder = os.path.join(destination_dir, str(label_type))
            if not os.path.isdir(destination_folder):
                os.makedirs(destination_folder)

            crop_destination = os.path.join(destination_dir, str(label_type), str(label_id) + ".jpg")

            if not os.path.exists(crop_destination):
                make_single_crop(pano_img_path, pano_x, pano_y, crop_destination, draw_mark=mark_label)
                print("Successfully extracted crop to " + str(label_id) + ".jpg")
                logging.info(f'{str(label_id)}.jpg {pano_id} {str(pano_x)} {str(pano_y)} {str(label_id)}')
                logging.info("---------------------------------------------------")
        else:
            no_pano_fail += 1
            print("Panorama image not found.")
            logging.warning("Skipped label id " + str(label_id) + " due to missing image.")

    print("Finished.")
    print(str(no_pano_fail) + " extractions failed because panorama image was not found.")
    print(str(no_metadata_fail) + " extractions failed because metadata was not found.")

print("Cropping labels")

if label_metadata_file is not None:
    file_path = os.path.splitext(label_metadata_file)
    if file_path[-1] == ".csv":
        label_infos = fetch_label_ids_csv(label_metadata_file)
    elif file_path[-1] == ".json":
        label_infos = fetch_cvMetadata_from_file(label_metadata_file)
else:
    label_infos = fetch_cvMetadata_from_server(sidewalk_server_fdqn)

bulk_extract_crops(label_infos, gsv_pano_path, crop_destination_path, mark_label=mark_label)
