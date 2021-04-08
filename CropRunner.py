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
import GSVImage
import fnmatch
import logging
from utilities import *
from PIL import Image, ImageDraw
from matplotlib.pyplot import imshow, figure, gcf, gca, show
import numpy as np

# *****************************************
# Update paths below                      *
# *****************************************

# Path to CSV data from database
csv_export_path = "metadata/csv-metadata-seattle_label_data.csv"
# Path to panoramas downloaded using DownloadRunner.py
gsv_pano_path = "/media/robert/1TB HDD/testing"
# Path to location for saving the crops
destination_path = "Crops/"

# Mark the center of the crop?
mark_center = True

logging.basicConfig(filename='crop.log', level=logging.DEBUG)

try:
    from xml.etree import cElementTree as ET
except ImportError as e:
    from xml.etree import ElementTree as ET


def extract_panoyawdeg(path_to_metadata_xml):
    pano = {}
    pano_xml = open(path_to_metadata_xml, 'rb')
    tree = ET.parse(pano_xml)
    root = tree.getroot()
    for child in root:
        if child.tag == 'projection_properties':
            pano[child.tag] = child.attrib
    print(pano['projection_properties']['pano_yaw_deg'])

    return pano['projection_properties']['pano_yaw_deg']


def crop_box_helper(path_to_scrapes, path_to_labeldata_csv, target_label_type=2):
    target = open("cropboxes.log", 'w')
    target.truncate();

    for root, dirnames, filenames in os.walk(path_to_scrapes):
        for filename in fnmatch.filter(filenames, '*.depth.txt'):
            depth_location = os.path.join(root, filename)

            pano_id = filename[:-10]
            # Search through CSV for labels that exist in this panorama
            csv_file = open(path_to_labeldata_csv)
            csv_f = csv.reader(csv_file)
            num_matching_labels = 0

            path_to_xml = os.path.join(root, pano_id + ".xml")
            image_name = os.path.join(root, pano_id + ".jpg")
            pano_im = Image.open(image_name)

            for row in csv_f:
                # Skip the header row
                if row[0] == "gsv_panorama_id":
                    continue
                csv_pano_id = row[0]
                sv_image_x = float(row[1])

                sv_image_y = float(row[2])
                label_type = int(row[3])
                photographer_heading = float(row[4])
                heading = float(row[5])
                label_id = int(row[7])

                pano_yaw_deg = 180 - photographer_heading
                if csv_pano_id == pano_id and label_type == target_label_type:
                    num_matching_labels += 1
                    im_width = GSVImage.GSVImage.im_width
                    im_height = GSVImage.GSVImage.im_height

                    draw = ImageDraw.Draw(pano_im)
                    # sv_image_x = sv_image_x - 100
                    x = ((float(pano_yaw_deg) / 360) * im_width + sv_image_x) % im_width
                    y = im_height / 2 - sv_image_y
                    r = 50
                    draw.ellipse((x - r, y - r, x + r, y + r), fill=128)
            if num_matching_labels == 0:
                print("Skipping image because no labels of interest were found.")
                continue
            else:
                print("Found " + str(num_matching_labels) + " labels of interest in this image.")

            figure()
            im = imshow(pano_im)
            fig = gcf()
            ax = gca()

            class EventHandler:
                def __init__(self):
                    self.top_left_x = None
                    self.top_left_y = None
                    self.bottom_right_x = None
                    self.bottom_right_y = None
                    fig.canvas.mpl_connect('button_press_event', self.onpress)

                def onpress(self, event):

                    if event.inaxes != ax:
                        return
                    xi, yi = (int(round(n)) for n in (event.xdata, event.ydata))
                    if self.top_left_x is None and self.bottom_right_x is None:
                        print("Pressed 1")
                        self.top_left_x = xi
                        self.top_left_y = yi
                    elif self.top_left_x is not None and self.bottom_right_x is None:
                        print("Pressed 2")
                        self.bottom_right_x = xi
                        self.bottom_right_y = yi
                        # Find the center
                        center_x = (self.top_left_x + self.bottom_right_x) / 2
                        center_y = (self.top_left_y + self.bottom_right_y) / 2
                        # Get the depth at the center
                        depth = get_depth_at_location(depth_location, center_x, center_y)
                        crop_width = abs(self.top_left_x - self.bottom_right_x)
                        crop_height = abs(self.top_left_y - self.bottom_right_y)
                        target = open("cropboxes.log", 'a')
                        target.write(
                            str(center_x) + "," + str(center_y) + "," + str(depth[0]) + "," + str(depth[1]) + "," + str(
                                depth[2]) + "," + str(crop_width) + "," + str(crop_height) + "\n")
                        target.close()
                        self.top_left_x = None
                        self.top_left_y = None
                        self.bottom_right_x = None
                        self.bottom_right_y = None

            handler = EventHandler()
            show()


def extract_tiltyawdeg(path_to_metadata_xml):
    pano = {}
    pano_xml = open(path_to_metadata_xml, 'rb')
    tree = ET.parse(pano_xml)
    root = tree.getroot()
    for child in root:
        if child.tag == 'projection_properties':
            pano[child.tag] = child.attrib
    print(pano['projection_properties']['tilt_yaw_deg'])

    return pano['projection_properties']['tilt_yaw_deg']


def get_depth_at_location(path_to_depth_txt, xi, yi):
    depth_location = path_to_depth_txt

    filename = depth_location

    print(filename)

    with open(filename, 'rb') as f:
        depth = np.loadtxt(f)

    depth_x = depth[:, 0::3]
    depth_y = depth[:, 1::3]
    depth_z = depth[:, 2::3]

    val_x, val_y, val_z = interpolated_3d_point(xi, yi, depth_x, depth_y, depth_z)
    print('depth_x, depth_y, depth_z', val_x, val_y, val_z)
    return val_x, val_y, val_z


def predict_crop_size_by_position(x, y, im_width, im_height):
    print("Predicting crop size by panorama position")
    dist_to_center = math.sqrt((x - im_width / 2) ** 2 + (y - im_height / 2) ** 2)
    # Calculate distance from point to center of left edge
    dist_to_left_edge = math.sqrt((x - 0) ** 2 + (y - im_height / 2) ** 2)
    # Calculate distance from point to center of right edge
    dist_to_right_edge = math.sqrt((x - im_width) ** 2 + (y - im_height / 2) ** 2)

    min_dist = min([dist_to_center, dist_to_left_edge, dist_to_right_edge])

    crop_size = (4.0 / 15.0) * min_dist + 200

    logging.info("Depth data unavailable; using crop size " + str(crop_size))

    return crop_size


# def predict_crop_size(x, y, im_width, im_height, path_to_depth_file):
#     """
#     # Calculate distance from point to image center
#     dist_to_center = math.sqrt((x-im_width/2)**2 + (y-im_height/2)**2)
#     # Calculate distance from point to center of left edge
#     dist_to_left_edge = math.sqrt((x-0)**2 + (y-im_height/2)**2)
#     # Calculate distance from point to center of right edge
#     dist_to_right_edge = math.sqrt((x - im_width) ** 2 + (y - im_height/2) ** 2)
# 
#     min_dist = min([dist_to_center, dist_to_left_edge, dist_to_right_edge])
# 
#     crop_size = (4.0/15.0)*min_dist + 200
# 
#     print("Min dist was "+str(min_dist))
#     """
#     crop_size = 0
# 
#     istance = max(0, 19.80546390 + 0.01523952 * sv_image_y)
# 
#     try:
#         depth = get_depth_at_location(path_to_depth_file, x, y)
#         depth_x = depth[0]
#         depth_y = depth[1]
#         depth_z = depth[2]
# 
#         distance = math.sqrt(depth_x ** 2 + depth_y ** 2 + depth_z ** 2)
#         print("Distance is " + str(distance))
#         if distance == "nan":
#             print("Distance is not a number.")
#             # If no depth data is available, use position in panorama as fallback
#             # Calculate distance from point to image center
#             dist_to_center = math.sqrt((x - im_width / 2) ** 2 + (y - im_height / 2) ** 2)
#             # Calculate distance from point to center of left edge
#             dist_to_left_edge = math.sqrt((x - 0) ** 2 + (y - im_height / 2) ** 2)
#             # Calculate distance from point to center of right edge
#             dist_to_right_edge = math.sqrt((x - im_width) ** 2 + (y - im_height / 2) ** 2)
# 
#             min_dist = min([dist_to_center, dist_to_left_edge, dist_to_right_edge])
# 
#             crop_size = (4.0 / 15.0) * min_dist + 200
# 
#             logging.info("Depth data unavailable; using crop size " + str(crop_size))
#         else:
#             # crop_size = (30700.0/37.0)-(300.0/37.0)*distance
#             # crop_size = 2600 - 220*distance
#             # crop_size = (5875.0/3.0)-(275.0/3.0)*distance
#             crop_size = 2050 - 110 * distance
#             crop_size = 8725.6 * (distance ** -1.192)
#             if crop_size < 50:
#                 crop_size = 50
#             elif crop_size > 1500:
#                 crop_size = 1500
# 
#             logging.info("Distance " + str(distance) + "Crop size " + str(crop_size))
#     except IOError:
#         # If no depth data is available, use position in panorama as fallback
#         # Calculate distance from point to image center
#         dist_to_center = math.sqrt((x - im_width / 2) ** 2 + (y - im_height / 2) ** 2)
#         # Calculate distance from point to center of left edge
#         dist_to_left_edge = math.sqrt((x - 0) ** 2 + (y - im_height / 2) ** 2)
#         # Calculate distance from point to center of right edge
#         dist_to_right_edge = math.sqrt((x - im_width) ** 2 + (y - im_height / 2) ** 2)
# 
#         min_dist = min([dist_to_center, dist_to_left_edge, dist_to_right_edge])
# 
#         crop_size = (4.0 / 15.0) * min_dist + 200
# 
#         logging.info("Depth data unavailable; using crop size " + str(crop_size))
# 
#     return crop_size

def predict_crop_size(sv_image_y):
    """
    # Calculate distance from point to image center
    dist_to_center = math.sqrt((x-im_width/2)**2 + (y-im_height/2)**2)
    # Calculate distance from point to center of left edge
    dist_to_left_edge = math.sqrt((x-0)**2 + (y-im_height/2)**2)
    # Calculate distance from point to center of right edge
    dist_to_right_edge = math.sqrt((x - im_width) ** 2 + (y - im_height/2) ** 2)

    min_dist = min([dist_to_center, dist_to_left_edge, dist_to_right_edge])

    crop_size = (4.0/15.0)*min_dist + 200

    print("Min dist was "+str(min_dist))
    """

    crop_size = 0
    distance = max(0, 19.80546390 + 0.01523952 * sv_image_y)

    if distance > 0:
        crop_size = 8725.6 * (distance ** -1.192)
    if crop_size > 1500:
        crop_size = 1500
    elif distance <= 0 or crop_size < 50:
        crop_size = 50

    return crop_size


# def make_single_crop(path_to_image, sv_image_x, sv_image_y, PanoYawDeg, output_filename, path_to_depth, draw_mark=False):
#     im_width = GSVImage.GSVImage.im_width
#     im_height = GSVImage.GSVImage.im_height
#     im = Image.open(path_to_image)
#     draw = ImageDraw.Draw(im)
#     # sv_image_x = sv_image_x - 100
#     x = ((float(PanoYawDeg) / 360) * im_width + sv_image_x) % im_width
#     y = im_height / 2 - sv_image_y
#
#     r = 10
#     if draw_mark:
#         draw.ellipse((x - r, y - r, x + r, y + r), fill=128)
#
#     print("Plotting at " + str(x) + "," + str(y) + " using yaw " + str(PanoYawDeg))
#
#     # Crop rectangle around label
#     cropped_square = None
#     try:
#         predicted_crop_size = predict_crop_size(x, y, im_width, im_height, path_to_depth)
#         crop_width = predicted_crop_size
#         crop_height = predicted_crop_size
#         print(x, y)
#         top_left_x = x - crop_width / 2
#         top_left_y = y - crop_height / 2
#         cropped_square = im.crop((top_left_x, top_left_y, top_left_x + crop_width, top_left_y + crop_height))
#     except (ValueError, IndexError) as e:
#
#         predicted_crop_size = predict_crop_size_by_position(x, y, im_width, im_height)
#         crop_width = predicted_crop_size
#         crop_height = predicted_crop_size
#         print(x, y)
#         top_left_x = x - crop_width / 2
#         top_left_y = y - crop_height / 2
#         cropped_square = im.crop((top_left_x, top_left_y, top_left_x + crop_width, top_left_y + crop_height))
#     cropped_square.save(output_filename)
#
#     return

def make_single_crop(path_to_image, sv_image_x, sv_image_y, PanoYawDeg, output_filename, draw_mark=False):
    im = Image.open(path_to_image)
    draw = ImageDraw.Draw(im)

    im_width = im.size[0]
    im_height = im.size[1]
    print(im_width, im_height)

    predicted_crop_size = predict_crop_size(sv_image_y)
    crop_width = predicted_crop_size
    crop_height = predicted_crop_size

    # Work out scaling factor based on image dimensions
    scaling_factor = im_width / 13312
    sv_image_x *= scaling_factor
    sv_image_y *= scaling_factor


    x = ((float(PanoYawDeg) / 360) * im_width + sv_image_x) % im_width
    y = im_height / 2 - sv_image_y

    r = 10
    if draw_mark:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=128)

    print("Plotting at " + str(x) + "," + str(y) + " using yaw " + str(PanoYawDeg))


    print(x, y)
    top_left_x = x - crop_width / 2
    top_left_y = y - crop_height / 2
    cropped_square = im.crop((top_left_x, top_left_y, top_left_x + crop_width, top_left_y + crop_height))

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
        sv_image_x = float(row[1])
        sv_image_y = float(row[2])
        label_type = int(row[3])
        photographer_heading = float(row[4])
        heading = float(row[5])
        label_id = int(row[7])

        pano_img_path = os.path.join(path_to_gsv_scrapes, pano_id[:2], pano_id + ".jpg")

        print("Photographer heading is " + str(photographer_heading))
        print("Viewer heading is " + str(heading))
        pano_yaw_deg = 180 - photographer_heading

        print("Yaw:" + str(pano_yaw_deg))

        # Extract the crop
        if os.path.exists(pano_img_path):
            counter += 1
            destination_folder = os.path.join(destination_dir, str(label_type))
            if not os.path.isdir(destination_folder):
                os.makedirs(destination_folder)

            crop_destination = os.path.join(destination_dir, str(label_type), str(label_id) + ".jpg")

            if not os.path.exists(crop_destination):
                make_single_crop(pano_img_path, sv_image_x, sv_image_y, pano_yaw_deg, crop_destination, draw_mark=mark_label)
                print("Successfully extracted crop to " + str(label_id) + ".jpg")
                logging.info(str(label_id) + ".jpg" + " " + pano_id + " " + str(sv_image_x)
                             + " " + str(sv_image_y) + " " + str(pano_yaw_deg) + " " + str(label_id))
                logging.info("---------------------------------------------------")
        else:
            no_pano_fail += 1
            print("Panorama image not found.")
            logging.warn("Skipped label id " + str(label_id) + " due to missing image.")

    print("Finished.")
    print(str(no_pano_fail) + " extractions failed because panorama image was not found.")
    print(str(no_metadata_fail) + " extractions failed because metadata was not found.")


bulk_extract_crops(csv_export_path, gsv_pano_path, destination_path, mark_label=mark_center)
# crop_box_helper(gsv_pano_path, csv_export_path)
