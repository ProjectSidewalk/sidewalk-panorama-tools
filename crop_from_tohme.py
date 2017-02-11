
import xml.etree.ElementTree
from utilities import *
import logging
from PIL import Image, ImageDraw
from random import randint

logging.basicConfig(filename='tohme3.log', level=logging.DEBUG)
logging.info("Logging started!")
class BoundingBox:
    def __init__(self, newId, newXmin, newXmax, newYmin, newYmax):
        self.panoId = newId
        self.xmin = newXmin
        self.xmax = newXmax
        self.ymin = newYmin
        self.ymax = newYmax

    def __str__(self):
        return "PanoID: " + self.panoId + " (" + str(self.xmin) + ", " + str(self.xmax) + ", " + str(
            self.ymin) + ", " + str(self.ymax) + ")"

    def get_center(self):
        return (self.xmin+float(self.xmax))/2.0, (self.ymin+float(self.ymax))/2.0

    def get_width(self):
        return self.xmax - self.xmin

    def get_height(self):
        return self.ymax - self.ymin


def get_depth_at_location(path_to_depth_txt, xi, yi):
    depth_location = path_to_depth_txt

    filename = depth_location

    # print(filename)

    with open(filename, 'rb') as f:
        depth = loadtxt(f)

    depth_x = depth[:, 0::3]
    depth_y = depth[:, 1::3]
    depth_z = depth[:, 2::3]


    val_x, val_y, val_z = interpolated_3d_point(xi, yi, depth_x, depth_y, depth_z)
    # print 'depth_x, depth_y, depth_z', val_x, val_y, val_z
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


def predict_crop_size(x, y, im_width, im_height, path_to_depth_file):
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
    try:
        depth = get_depth_at_location(path_to_depth_file, x, y)
        depth_x = depth[0]
        depth_y = depth[1]
        depth_z = depth[2]

        distance = math.sqrt(depth_x ** 2 + depth_y ** 2 + depth_z ** 2)
        print("Distance is " + str(distance))
        if distance == "nan":
            print("Distance is not a number.")
            # If no depth data is available, use position in panorama as fallback
            # Calculate distance from point to image center
            dist_to_center = math.sqrt((x - im_width / 2) ** 2 + (y - im_height / 2) ** 2)
            # Calculate distance from point to center of left edge
            dist_to_left_edge = math.sqrt((x - 0) ** 2 + (y - im_height / 2) ** 2)
            # Calculate distance from point to center of right edge
            dist_to_right_edge = math.sqrt((x - im_width) ** 2 + (y - im_height / 2) ** 2)

            min_dist = min([dist_to_center, dist_to_left_edge, dist_to_right_edge])

            crop_size = (4.0 / 15.0) * min_dist + 200

            logging.info("Depth data unavailable; using crop size " + str(crop_size))
        else:
            # crop_size = (30700.0/37.0)-(300.0/37.0)*distance
            # crop_size = 2600 - 220*distance
            # crop_size = (5875.0/3.0)-(275.0/3.0)*distance
            crop_size = 2050 - 110 * distance
            crop_size = 8725.6 * (distance ** -1.192)
            if crop_size < 50:
                crop_size = 50
            elif crop_size > 1500:
                crop_size = 1500

            # logging.info("Distance " + str(distance) + "Crop size " + str(crop_size))
    except IOError:
        # If no depth data is available, use position in panorama as fallback
        # Calculate distance from point to image center
        dist_to_center = math.sqrt((x - im_width / 2) ** 2 + (y - im_height / 2) ** 2)
        # Calculate distance from point to center of left edge
        dist_to_left_edge = math.sqrt((x - 0) ** 2 + (y - im_height / 2) ** 2)
        # Calculate distance from point to center of right edge
        dist_to_right_edge = math.sqrt((x - im_width) ** 2 + (y - im_height / 2) ** 2)

        min_dist = min([dist_to_center, dist_to_left_edge, dist_to_right_edge])

        crop_size = (4.0 / 15.0) * min_dist + 200

        logging.info("Depth data unavailable; using crop size " + str(crop_size))

    return crop_size

def scale_coords(xi, yi, orig_width, orig_height, new_width, new_height):
    scaled_x = (new_width / float(orig_width)) * xi
    scaled_y = (new_height / float(orig_height)) * yi
    return scaled_x, scaled_y


def parse_voc_xmls(path_to_voc_xmls):
    """
    Takes a path to a folder of PASCAL VOC formatted xml annotation files and returns a list of BoundingBoxes found
    in all of the xml files combined.
    :param path_to_voc_xmls: Path to a folder containing PASCAL VOC xml annotations.
    :return: List of BoundingBox objects
    """
    bounding_boxes = []
    for filename in os.listdir(path_to_voc_xmls):
        if filename.endswith(".xml"):
            xml_path = os.path.join(path_to_voc_xmls, filename)
            print(xml_path)
            e = xml.etree.ElementTree.parse(xml_path).getroot()

            objs = e.findall('object')

            for ix, obj in enumerate(objs):
                bbox = obj.find('bndbox')
                # Make pixel indexes 0-based
                x1 = float(bbox.find('xmin').text)
                y1 = float(bbox.find('ymin').text)
                x2 = float(bbox.find('xmax').text)
                y2 = float(bbox.find('ymax').text)

                bounding_boxes.append(BoundingBox(filename[:-4], x1, x2, y1, y2))
            continue
        else:
            continue
    return bounding_boxes

nodepth = 0
for bbox in parse_voc_xmls("/home/anthony/Downloads/Tohme_dataset/2048_VOCformat/Annotations"):
    # Path to VOC data
    voc_dir = "/home/anthony/Downloads/Tohme_dataset/2048_VOCformat"

    # Get depth at center of bounding box
    bbox_center = bbox.get_center()
    center_x = bbox_center[0]
    center_y = bbox_center[1]
    # Scale coordinates up to full-size panorama image
    # Our images our 2048x1024; full size is 13312x6656
    center_scaled = scale_coords(center_x, center_y, 2048, 1024, 13312, 6656)
    # Retrieve the depth at the scaled location
    scrape_dir = "/mnt/umiacs/Panoramas/tohme"
    pano_id = bbox.panoId
    depth_file_path = os.path.join(scrape_dir, pano_id[:2], pano_id+".depth.txt")

    # Predict the crop size from the depth
    depth = get_depth_at_location(depth_file_path, center_scaled[0], center_scaled[1])
    crop_size = predict_crop_size(center_scaled[0], center_scaled[1], 13312, 6656, depth_file_path)
    # Scale back down to 2048x1024
    crop_size = (1.0/6.0)*crop_size
    # Load the panorama
    pano_path = os.path.join(voc_dir, "JPEGImages", pano_id+".jpg")
    im = Image.open(pano_path)
    # Make the crops

    try:
        crop_width = crop_size
        crop_height = crop_size

        top_left_x = center_x - crop_width / 2
        top_left_y = center_y - crop_height / 2
        cropped_square = im.crop((top_left_x, top_left_y, top_left_x + crop_width, top_left_y + crop_height))
        # Save the output
        random_filename_padding = randint(1000,9999)
        output_dir = "tohme_crops3"
        output_filename = os.path.join(output_dir, pano_id+str(random_filename_padding)+".jpg")
        cropped_square.save(output_filename)
        print("Output file "+pano_id+str(random_filename_padding)+".jpg")
        logging.info("Image: "+pano_id+str(random_filename_padding)+" Predicted crop "+str(crop_size)+"x"+str(crop_size)+"; actual "+str(bbox.get_width())+"x"+str(bbox.get_height())+"; depth "+str(depth))

        print("Predicted crop "+str(crop_size)+"x"+str(crop_size)+"; actual "+str(bbox.get_width())+"x"+str(bbox.get_height()))
    except (ValueError, IndexError) as e:
        logging.info("Skipped crop for "+pano_id+"; no depth information at point.")
        continue
