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

import csv
import logging
import os
from PIL import Image, ImageDraw

# *****************************************
# Update paths below                      *
# *****************************************

# Path to CSV data from database - Place in 'metadata'
csv_export_path = "samples/labeldata.csv"
# Path to panoramas downloaded using DownloadRunner.py. Reference correct directory
gsv_pano_path = "/tmp/download_dest/"
# Path to location for saving the crops
destination_path = "/crops/"

# Mark the center of the crop?
mark_label = True

logging.basicConfig(filename='crop.log', level=logging.DEBUG)

try:
    from xml.etree import cElementTree as ET
except ImportError as e:
    from xml.etree import ElementTree as ET


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


def bulk_extract_crops(path_to_db_export, path_to_gsv_scrapes, destination_dir, mark_label=False):
    csv_file = open(path_to_db_export)
    csv_f = csv.reader(csv_file)
    counter = 0
    no_metadata_fail = 0
    no_pano_fail = 0

    for row in csv_f:
        if counter == 0:
            counter += 1
            continue

        pano_id = row[0]
        print(pano_id)
        pano_x = float(row[1])
        pano_y = float(row[2])
        label_type = int(row[3])
        label_id = int(row[7])

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


bulk_extract_crops(csv_export_path, gsv_pano_path, destination_path, mark_label=mark_label)
