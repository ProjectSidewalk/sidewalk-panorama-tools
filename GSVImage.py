try:
    import cv2
    import cv2.cv as cv
except ImportError as e:
    cv2 = None
    cv = None
# http://stackoverflow.com/questions/9226258/why-does-python-cv2-modules-depend-on-old-cv

import os
import unittest

import GSV3DPointImage as gsv3d

from copy import deepcopy
from PIL import Image, ImageDraw
from pylab import *
from random import randint
from utilities import *

try:
    from xml.etree import cElementTree as ET
except ImportError as e:
    from xml.etree import ElementTree as ET

gsv_image_width = 13312
gsv_image_height = 6656
im_width = gsv_image_width
im_height = gsv_image_height

class GSVImage(object):
    gsv_image_width = 13312
    gsv_image_height = 6656
    im_width = gsv_image_width
    im_height = gsv_image_height
    
    
    def __init__(self, path):
        """
         A constructor. This method takes a path to GSV data files.
         For example: ../data/GSV/5umV8SPGE1jidFGstzcQDA/
        """
        self.im_width = 13312  # deprecated
        self.im_height = 6656  # deprecated
        self.gsv_image_width = 13312
        self.gsv_image_height = 6656
        self.pano_id = path.split('/')[-1]
        
        ensure_dir(path)
        self.path = path

    def adjust_aspect_ratio(self, boundingbox, aspect_ratio):
        patch_height = boundingbox['y_max'] - boundingbox['y_min']
        patch_width = patch_height * aspect_ratio

        if boundingbox['boundary']:
            x_min = boundingbox['x_min'] - GSVImage.im_width
        else:
            x_min = boundingbox['x_min']
        boundingbox_width = boundingbox['x_max'] - x_min

        # If patch_width is larger than the boundingbox_width, use the patch_width.
        # Otherwise use the bounding_box width and modify the height of the bounding box
        if boundingbox_width < patch_width:
            x_center = (boundingbox['x_max'] + x_min) / 2

            # Set x_max and x_min
            x_min = x_center - patch_width / 2

            if x_min < 0:
                boundingbox['boundary'] = True
                boundingbox['x_min'] = GSVImage.im_width + x_min
            else:
                boundingbox['boundary'] = False
                boundingbox['x_min'] = x_min

            boundingbox['x_max'] = x_center + patch_width / 2

            boundingbox['x_max'] = int(boundingbox['x_max'])
            boundingbox['x_min'] = int(boundingbox['x_min'])
        else:
            patch_width = boundingbox_width
            patch_height = boundingbox_width / aspect_ratio
            y_center = (boundingbox['y_max'] + boundingbox['y_min']) / 2
            boundingbox['y_max'] = int(y_center + patch_height / 2)
            boundingbox['y_min'] = int(y_center - patch_height / 2)
        return boundingbox

    def crop_user_bounding_boxes(self, boundingboxes, output_filename, aspect_ratio=None, show_image=False,
                                 overwrite=False, verbose=False):
        filename = self.path + 'images/pano.jpg'
        # ensure_dir(directory)
        im = Image.open(filename)
        filenames = []
        for idx, boundingbox in enumerate(boundingboxes):
            #
            # Crop
            filename = output_filename + '_' + str(idx) + '.jpg'
            filenames.append(filename)
            if verbose:
                print(filename)
            if os.path.isfile(filename) and not overwrite:
                print(filename, 'File already exists')
                return 

            if aspect_ratio is not None:
                boundingbox = self.adjust_aspect_ratio(boundingbox, aspect_ratio)

            if boundingbox['boundary']:
                patch_height = boundingbox['y_max'] - boundingbox['y_min']
                patch_width = (GSVImage.im_width - boundingbox['x_min']) + boundingbox['x_max'] 
                im_dimension = (patch_width, patch_height)
                patch = Image.new('RGBA', im_dimension, (0, 0, 0, 0))
                
                # Crop and paste the first half of the bounding box
                box = (int(boundingbox['x_min']), int(boundingbox['y_min']), GSVImage.im_width,
                       int(boundingbox['y_max']))
                partial = im.crop(box)
                patch.paste(partial, (0, 0))
                
                # Crop and paste the last half of the bounding box
                box = (0, int(boundingbox['y_min']), int(boundingbox['x_max']), int(boundingbox['y_max']))
                partial = im.crop(box)
                patch.paste(partial, (GSVImage.im_width - boundingbox['x_min'], 0))
            else:
                # http://stackoverflow.com/questions/1076638/trouble-using-python-pil-library-to-crop-and-save-image
                box = (int(boundingbox['x_min']), int(boundingbox['y_min']), int(boundingbox['x_max']),
                       int(boundingbox['y_max']))
                patch = im.crop(box)
            
            if show_image:
                patch.show()
            
            patch.save(filename, 'JPEG')
    
        return filenames

    def crop_negative_bounding_boxes(self, boundingboxes, negative_filenames, overlay='normal_z_component',
                                     overlap_ratio=0.5, aspect_ratio=None, show_image=True, verbose=False):
        """
        This method takes a list of ground truth bounding boxes (*) and a filename header (**).
        (*) E.g., [{
            'boundary': False,
            'x_min': 300,
            'x_max': 700,
            'y_min': 300,
            'y_max': 600
        }, ...]), and
        (**) "../data/temp/negative/
        """
        if len(boundingboxes) == 0:
            return
        num_ground = 0
        num_patches = 1
        
        #
        # Find the smallest boudning box and use it as the cropping size
        filename = self.path + 'images/pano.jpg'
        # ensure_dir(directory)
        im = Image.open(filename)
        gsv_3d_point_image = gsv3d.GSV3DPointImage(self.path)
        positive_boundingboxes = deepcopy(boundingboxes)
 
        area = float('inf')
        for boundingbox in boundingboxes:
            if boundingbox['boundary']:
                bb_width = boundingbox['x_max'] + (self.im_width - boundingbox['x_min'])
            else:
                bb_width = boundingbox['x_max'] - boundingbox['x_min']
            
            bb_area = bb_width * (boundingbox['y_max'] - boundingbox['y_min'])
            if area > bb_area: 
                area = bb_area
                bb = boundingbox
        
        if bb['boundary']:
            crop_width = bb['x_max'] - (bb['x_min'] - self.im_width)
        else:
            crop_width = bb['x_max'] - bb['x_min']
        crop_height = bb['y_max'] - bb['y_min']
        
        #
        # For each label, crop 9 negative image patches
        half_crop_height = int(crop_height / 2)
        half_crop_width = int(crop_width / 2)
        
        padding_top = int((crop_height + 1) / 2)
        padding_bottom = padding_top
        padding_left = int((crop_width + 1) / 2)
        padding_right = padding_left
        negative_bounding_boxes = []
        
        for current_bb in boundingboxes:
            #
            # Randomly choose a point on the image.
            # Make sure the negative example does not overlap with other bounding boxes.
            # If overlay is specified, crop image patches only from the masked area
            bb_w = current_bb['x_max'] - current_bb['x_min'] # Bounding box width
            bb_h = current_bb['y_max'] - current_bb['y_min'] # Bounding box height
            num_around_ramp = num_patches - num_ground
            for idx in range(num_ground):
                print('neg', len(negative_bounding_boxes)) # debug
                cropped = False
                while not cropped:
                    y = randint(padding_top, self.im_height - padding_bottom)
                    x = randint(padding_left, self.im_width - padding_right)
                    crop_bb = {
                            'boundary': False,
                            'x_min': x - half_crop_width,
                            'x_max': x + half_crop_width,
                            'y_min': y - half_crop_height,
                            'y_max': y + half_crop_height
                               }
                    #
                    # Debug
                    #print 'neg', len(negative_bounding_boxes), negative_bounding_boxes
                    #box = (crop_bb['x_min'], crop_bb['y_min'], crop_bb['x_max'], crop_bb['y_max'])
                    #patch = im.crop(box) 
                    #patch.show()
                    
                    do_continue = False
                    for boundingbox in positive_boundingboxes:
                        areaoverlap = bounding_box_area_overlap(crop_bb, boundingbox)
                        if areaoverlap > overlap_ratio:
                            # Area overlap too big
                            do_continue = True
                            break
                    if do_continue:
                        continue
                    for boundingbox in negative_bounding_boxes:
                        areaoverlap = bounding_box_area_overlap(crop_bb, boundingbox)
                        if areaoverlap > overlap_ratio:
                            do_continue = True
                            break
                    if do_continue:
                        continue
                    # if overlay is specified, check if (x, y) is on the masked area
                    if overlay:
                        overlay_value = gsv_3d_point_image.get_overlay_value(x, y, overlay='normal_z_component')
                        overlay_threshold = 10
                        if np.isnan(overlay_value) or overlay_value < overlay_threshold:
                            # print 'Weak overlay value: ', overlay_value
                            do_continue = True
                    if do_continue:
                        continue
                    negative_bounding_boxes.append(crop_bb)
                    cropped = True
            #
            # Crop negative image patches around the current boundning box (current_bb)
            for idx in range(num_ground, num_patches):
                cropped = False
                num_loop = 0
                
                
                while not cropped:
                    #
                    # Crop an image patch.
                    # Make sure the area overlap between a negative patch and a positive patch is below a threshold
                    # Try to collect patches around positive patches, but if it is not possible, collect patches from 
                    # the masked area (ground)
                    num_loop += 1
                    #y = randint(current_bb['y_min'] - 2 * padding_top, current_bb['y_max'] + 2 * padding_bottom)
                    y = randint(current_bb['y_min'] - crop_height, current_bb['y_max']) # + crop_height)
                    
                    if idx % 2 == 0:
                        # x = randint(current_bb['x_min'] - padding_left, current_bb['x_min'])
                        x = randint(current_bb['x_min'] - 1.5 * crop_width, current_bb['x_min'] - 0.5 * crop_width)
                        if x < padding_left or x > self.im_width - padding_right:
                            x = randint(current_bb['x_max'], current_bb['x_max'] + 0.5 * crop_width)
                    else:
                        # x = randint(current_bb['x_max'], current_bb['x_max'] + padding_left)
                        x = randint(current_bb['x_max'], current_bb['x_max'] + 0.5 * crop_width)
                        if x < padding_left or x > self.im_width - padding_right:
                            x = randint(current_bb['x_min'] - 1.5 * crop_width, current_bb['x_min'] - 0.5 * crop_width)
                    
                    if num_loop > 200:
                        print("random")
                        y = randint(padding_top, self.im_height - padding_bottom)
                        x = randint(padding_left, self.im_width - padding_right)
                    
                    if x < padding_left or x > self.im_width - padding_right or y < padding_top or y > self.im_height - padding_bottom:
                        print('bounding box out of range')
                        continue
                    crop_bb = {
                            'boundary': False,
                            'x_min': x - half_crop_width,
                            'x_max': x + half_crop_width,
                            'y_min': y - half_crop_height,
                            'y_max': y + half_crop_height
                               }
                                        #
                    # Debug
                    #print 'neg', len(negative_bounding_boxes), negative_bounding_boxes
                    #box = (crop_bb['x_min'], crop_bb['y_min'], crop_bb['x_max'], crop_bb['y_max'])
                    #patch = im.crop(box) 
                    #patch.show()
                    
                    do_continue = False
                    for pos_bb in positive_boundingboxes:
                        areaoverlap = bounding_box_area_overlap(crop_bb, pos_bb)
                        # box = (pos_bb['x_min'], pos_bb['y_min'], pos_bb['x_max'], pos_bb['y_max'])
                        # patch = im.crop(box)
                        # patch.show()
                        
                        if areaoverlap > overlap_ratio:
                            # Area overlap too big
                            do_continue = True
                            break
                    
                    if do_continue:
                        continue
                    
                    for neg_bb in negative_bounding_boxes:
                        areaoverlap = bounding_box_area_overlap(crop_bb, neg_bb)
                        if areaoverlap > 0.5:
                            do_continue = True
                            break

                    if do_continue:
                        continue
                    # if overlay is specified, check if (x, y) is on the masked area
                    if overlay:
                        overlay_value = gsv_3d_point_image.get_overlay_value(x, y, overlay='normal_z_component')
                        overlay_threshold = 10
                        if np.isnan(overlay_value) or overlay_value < overlay_threshold:
                            # print 'Weak overlay value: ', overlay_value
                            do_continue = True
                        
                    if do_continue:
                        continue
                    
                    negative_bounding_boxes.append(crop_bb)
                    cropped = True
        
        #
        # Crop bounding boxes for negative image patches
        filenames = []
        for i, boundingbox in enumerate(negative_bounding_boxes):
            outline = str(boundingbox['x_min']) + ' ' + str(boundingbox['y_min']) + ' '
            outline += str(boundingbox['x_max']) + ' ' + str(boundingbox['y_min']) + ' '
            outline += str(boundingbox['x_max']) + ' ' + str(boundingbox['y_max']) + ' '
            outline += str(boundingbox['x_min']) + ' ' + str(boundingbox['y_max'])
            
            box = (boundingbox['x_min'], boundingbox['y_min'], boundingbox['x_max'], boundingbox['y_max'])
            patch = im.crop(box) 
            #if show_image:
                #patch.show()
                
            
            filename = negative_filenames + '_' + str(i) + '.jpg'
            filenames.append(filename)

            patch.save(filename, 'JPEG')
                
        return filenames, negative_bounding_boxes
    
    def crop_negative_image_patches(self, outlines, output_filename, overlap_ratio=0.5, aspect_ratio=None, overlay=None, show_image=False):
        """
         This method takes outlines, and crops image patches that do not overlap with outlines.
         If overlay (mask) is specified, crop negative patches only from masked region.
        """
        filename = self.path + '/images/pano.png'
        # ensure_dir(directory)
        im = Image.open(filename)
        gsv_3d_point_image = gsv3d.GSV3DPointImage(self.path)
        
        boundingboxes = []        
        for outline in outlines:
            print('---')
            xys = outline.strip().split(' ')
            xs = []
            ys = []
            points = []
            for x, y in zip(xys[0::2], xys[1::2]):
                p = user_point_to_sv_image_point(self.path, {'x': x, 'y': y})
                p = (int(x), int(y))
                points.append(p)
            
            # Compute the bounding box of the passed label points
            # If aspect_ratio is passed, format the bounding box so the 
            # aspect ratio of the cropped image will be consistent across
            # all the image patches 
            boundingbox = sv_image_points_to_bounding_box(points)
            if aspect_ratio and (type(aspect_ratio) == int or type(aspect_ratio) == float):
                patch_height = boundingbox['y_max'] - boundingbox['y_min']
                patch_width = patch_height * aspect_ratio 
                
                if boundingbox['boundary']:
                    x_min = boundingbox['x_min'] - GSVImage.im_width
                else:
                    x_min = boundingbox['x_min']
                boundingbox_width = boundingbox['x_max'] - x_min
                
                # If patch_width is larger than the boundingbox_width, use the patch_width.
                # Otherwise use the bounding_box width and modify the height of the bounding box
                if boundingbox_width < patch_width:
                    x_center = (boundingbox['x_max'] + x_min) / 2
                    
                    # Set x_max and x_min
                    x_min = x_center - patch_width / 2
                    
                    if x_min < 0:
                        boundingbox['boundary'] = True
                        boundingbox['x_min'] = GSVImage.im_width + x_min
                    else:
                        boundingbox['boundary'] = False
                        boundingbox['x_min'] = x_min
                    
                    boundingbox['x_max'] = x_center + patch_width / 2
                    
                    boundingbox['x_max'] = int(boundingbox['x_max'])
                    boundingbox['x_min'] = int(boundingbox['x_min'])
                else:
                    patch_width = boundingbox_width
                    patch_height = boundingbox_width / aspect_ratio
                    y_center = (boundingbox['y_max'] + boundingbox['y_min']) / 2
                    boundingbox['y_max'] = int(y_center + patch_height / 2) 
                    boundingbox['y_min'] = int(y_center - patch_height / 2)
            boundingboxes.append(boundingbox)
        
        if len(boundingboxes) == 0:
            return
        #
        # Find the smallest boudning box and use it as the cropping size        
        area = float('inf')
        for boundingbox in boundingboxes:
            
            if boundingbox['boundary']:
                bb_width = boundingbox['x_max'] + (self.im_width - boundingbox['x_min'])
            else:
                bb_width = boundingbox['x_max'] - boundingbox['x_min']
            
            print('bb list: ', boundingbox)
            print('bb width: ', bb_width)
            
            bb_area = bb_width * (boundingbox['y_max'] - boundingbox['y_min'])
            if area > bb_area: 
                area = bb_area
                bb = boundingbox
        
        print('smallest bb: ', bb)
        print('smallest bb width: ', bb_width)
        if bb['boundary']:
            crop_width = bb['x_max'] - (bb['x_min'] - self.im_width)
        else:
            crop_width = bb['x_max'] - bb['x_min']
        crop_height = bb['y_max'] - bb['y_min']
        
        #
        # For each label, crop 9 negative image patches
        half_crop_height = int(crop_height / 2)
        half_crop_width = int(crop_width / 2)
        
        padding_top = int((crop_height + 1) / 2)
        padding_bottom = padding_top
        padding_left = int((crop_width + 1) / 2)
        padding_right = padding_left
        print('crop width, crop height', crop_width, crop_height)
        negative_bounding_boxes = []
        for outline_idx, outline in enumerate(outlines):
            #
            # Randomly choose a point on the image.
            # Make sure the negative example does not overlap with other bounding boxes.
            # If overlay is specified, crop image patches only from the masked area
            for idx in range(9):
                cropped = False
                while not cropped:
                    y = randint(padding_top, self.im_height - padding_bottom)
                    x = randint(padding_left, self.im_width - padding_right)
                    print("image size: width, height", self.im_width, self.im_height)
                    print("crop size: width, height = ", crop_width, crop_height)
                    print("paddings: ", padding_left, padding_top, padding_right, padding_bottom)
                    #print '-- Random: x, y = ', x, y
                    crop_bb = {
                            'boundary': False,
                            'x_min': x - half_crop_width,
                            'x_max': x + half_crop_width,
                            'y_min': y - half_crop_height,
                            'y_max': y + half_crop_height
                               }
                    print(crop_bb)
                    
                    crop_bb_does_not_overlap_with_other_bb = True
                    for boundingbox in boundingboxes:
                        areaoverlap = bounding_box_area_overlap(crop_bb, boundingbox)
                        if areaoverlap > overlap_ratio:
                            # Area overlap too big
                            continue
                    for boundingbox in negative_bounding_boxes:
                        areaoverlap = bounding_box_area_overlap(crop_bb, boundingbox)
                        if areaoverlap > overlap_ratio:
                            continue
                    
                    # if overlay is specified, check if (x, y) is on the masked area
                    if overlay:
                        overlay_value = gsv_3d_point_image.get_overlay_value(x, y, overlay='normal_z_component')
                        overlay_threshold = 10
                        if np.isnan(overlay_value) or overlay_value < overlay_threshold:
                            # print 'Weak overlay value: ', overlay_value
                            continue
                    
                    negative_bounding_boxes.append(crop_bb)
                    cropped = True

        # Crop bounding boxes                        
        for i, boundingbox in enumerate(negative_bounding_boxes):
            outline = str(boundingbox['x_min']) + ' ' + str(boundingbox['y_min']) + ' '
            outline += str(boundingbox['x_max']) + ' ' + str(boundingbox['y_min']) + ' '
            outline += str(boundingbox['x_max']) + ' ' + str(boundingbox['y_max']) + ' '
            outline += str(boundingbox['x_min']) + ' ' + str(boundingbox['y_max'])
            
            box = (boundingbox['x_min'], boundingbox['y_min'], boundingbox['x_max'], boundingbox['y_max'])
            patch = im.crop(box) 
            if show_image:
                patch.show()
            
            filename = output_filename + '_' + str(i) + '.jpg'
            patch.save(filename, 'JPEG')
                
        return

    def crop_user_outline(self, outline, output_filename, aspect_ratio=None, show_image=False):
        """
         This method takes an outline, output_filename, 
        """
            
        if type(outline) != str:
            raise ValueError('First parameter should be str.')
        if type(output_filename) != str:
            raise ValueError('Second parameter should be str.')
        
        outline_length = len(outline.strip().split(' ')) 
        if outline_length % 2 != 0 or outline_length < 6:
            raise ValueError('Illegal number of outline point coordinates')
        
        return crop_user_outline(self.path, outline, output_filename, aspect_ratio, show_image)
    
    def crop_user_outlines(self, outlines, output_filename, aspect_ratio=None, show_image=False):
        """
         This method takes outlines, output_filename, 
        """
        path = self.path
            
        if type(outlines) != list:
            raise ValueError('First parameter should be list.')
        if type(output_filename) != str:
            raise ValueError('Second parameter should be str.')

        for i, outline in enumerate(outlines):
            outline_length = len(outline.strip().split(' ')) 
            if outline_length % 2 != 0 or outline_length < 6:
                raise ValueError('Illegal number of outline point coordinates')

            filename = output_filename + '_' + str(i) + '.jpg'
            crop_user_outline(self.path, outline, filename, aspect_ratio, show_image)
        return
    
    def get_image_latlng(self):
        """
        This method returns a latlng position of this image. 
        """
        xml = open(self.path + 'meta.xml', 'rb')
        tree = ET.parse(xml)
        data = tree.find('data_properties').attrib
        lat = float(data['lat'])
        lng = float(data['lng'])
        return lat, lng
    
    def get_pano_id(self):
        """
        Return the panorama id of this image
        """
        return self.pano_id

    def plot_bounding_boxes(self, bounding_boxes, image_size=None, width=5, outline='red', output_file=None):
        """
         This method renders detected bounding boxes on an image. Each bounding box should have
         the format of [(x1, y1), (x2, y2)]. If image size is give,  then shrink the image accordingly

         Color Examples: "red", "#92d050", "#00b050
         Open image file
         http://scikit-image.org/docs/dev/auto_examples/applications/plot_morphology.html
        """
        filename = self.path + 'images/pano.jpg'
        
        """
        import matplotlib.pyplot as plt
        from skimage import io
        im = io.imread(filename)
        plt.imshow(im)
        """

        im = Image.open(filename)

        #
        # Draw bounding boxes
        # http://effbot.org/imagingbook/imagedraw.htm
        draw = ImageDraw.Draw(im)
        
        for box in bounding_boxes:
            x1 = box[0][0]
            y1 = box[0][1]
            x2 = box[1][0]
            y2 = box[1][1]
            for i in range(0, width):
                if ((x1 + i) >= (x2 - i)) or ((y1 + i) >= (y2 - i)):
                    break 
                new_box = [(x1 + i, y1 + i), (x2 - i, y2 - i)] 
                draw.rectangle(new_box, outline=outline)
            
        del draw
        if image_size:
            im = im.resize(image_size)
        im.show()
        
        if output_file:
            im.save(output_file, 'PNG')
        return
    
    def plot_user_outline(self, outline, user_point=True):
        """
        This function plots a user provided GSV outline (a set of points) on an actual GSV image.
        
        :param path: 
            A path to a directory where GSV data are stored
        :type path: 
            str. E.g., '12082 -490 12118 -411 12017 -388 11764 -374 11764 -420 11852 -462 11934 -490'
            Note that
        :param outline: 
            A set of GSV image points provided through the Street View Labeler Interface. Set user_point to True if
            you are passing image points.

        :type outline:
            list.
        """
        # Go through every 2 items
        # http://stackoverflow.com/questions/5389507/iterating-over-every-two-elements-in-a-list
        filename = self.path + 'images/pano.jpg'
        im = Image.open(filename)
        draw = ImageDraw.Draw(im)
        
        xys = outline.strip().split(' ')
        xs = []
        ys = []
        im_xys = []
        
        points = [] 
        for x, y in zip(xys[0::2], xys[1::2]):
            if user_point:
                p = self.user_point_to_sv_image_point({'x': x, 'y': y})
            else:
                p = (int(x), int(y))
            points.append(p)
            r = 10
            draw.ellipse((p[0]-r, p[1]-r, p[0]+r, p[1]+r), fill='rgb(200,0,0)')
            r = 5
            draw.ellipse((p[0]-r, p[1]-r, p[0]+r, p[1]+r), fill='white')
    
        for i, p in enumerate(points):
            draw.line((points[i-1][0], points[i-1][1], points[i][0], points[i][1]), fill='red')
            
        figure()
        imshow(im)
        title('PanoId: ' + self.path[:-1].split('/')[-1])
        
        show()
        return
        
    def plot_user_outlines(self, outlines):
        """
        This function plots a set of user provided GSV outlines
        
        :param outline: 
            A set of user provided GSV image points
        :type outline:
            list.
        """
        # Go through every 2 items
        # http://stackoverflow.com/questions/5389507/iterating-over-every-two-elements-in-a-list
        filename = self.path + '/images/pano.png'
        im = Image.open(filename)
        draw = ImageDraw.Draw(im)
        
        for outline in outlines:
            xys = outline.strip().split(' ')
            xs = []
            ys = []
            im_xys = []
            
            points = [] 
            for x, y in zip(xys[0::2], xys[1::2]):
                p = self.user_point_to_sv_image_point({'x': x, 'y': y})
                points.append(p)
                r = 20
                draw.ellipse((p[0]-r, p[1]-r, p[0]+r, p[1]+r), fill='rgb(200,0,0)')
                r = 15
                draw.ellipse((p[0]-r, p[1]-r, p[0]+r, p[1]+r), fill='white')
        
            for i, p in enumerate(points):
                draw.line((points[i-1][0], points[i-1][1], points[i][0], points[i][1]), fill='red', width=10)
                
        figure()
        imshow(im)
        title('PanoId: ' + self.path[:-1].split('/')[-1])
        
        show()
    
    def user_point_to_sv_image_point(self, point):
        """
        This function converts a GSV image point coordinate provided by user through CSI interface to 
        a true GSV image coordinate
        """
        return user_point_to_sv_image_point(self.path, point)

    def show(self, size=False):
        """
        This method shows the corresponding GSV panorama image

        options can take size
        http://www.learnpython.org/Multiple_Function_Arguments
        """
        if os.path.isfile(self.path + 'images/pano.jpg'):
            im = Image.open(self.path + 'images/pano.jpg')

            if size and type(size) == tuple:
                im = im.resize(size, Image.ANTIALIAS)
            im.show()
        else:
            raise Exception(self.path + 'images/pano.jpg does not exist')
        return

    def sv_image_point_to_user_point(self, point, image_size=None):
        """
         This method converts sv_image point (x, y) on a street view image (e.g., points that constitutes
         a curb ramp bounding box detected by a program) into user point (or point on SV image on SV API).
        """
        return sv_image_point_to_user_point(self.path, point, image_size=image_size)
    
    def sv_image_point_to_pov(self, point, image_size=None):
        """
        This method converts finds pov for the point
        """
        return sv_image_point_to_pov(self.path, point, image_size=image_size)

    
    def sv_image_points_to_bounding_box(self, points):
        return sv_image_points_to_bounding_box(points)
    

def sv_image_point_to_pov(path, point, image_size=None):
    """
    Pass an image point coordinate and convert it to pov
    """
    x = point[0]
    y = point[1]

    if image_size:
        w = image_size[0]
        h = image_size[1]
        x = x * GSVImage.gsv_image_width / w
        y = y * GSVImage.gsv_image_height / h

    xml = open(path + 'meta.xml', 'rb')
    tree = ET.parse(xml)
    root = tree.getroot()
    pano_yaw_deg = float(root.find('projection_properties').get('pano_yaw_deg'))

    heading = (360. * (float(x) / GSVImage.gsv_image_width)) + (pano_yaw_deg - 180)
    heading = (heading + 360) % 360

    pitch = 90 - 180 * (float(y) / GSVImage.gsv_image_height)

    pov = {'heading': heading,
           'pitch': pitch,
           'zoom': 1
           }
    return pov

def crop_user_outline(input_path, outline, output_filename, aspect_ratio=None, show_image=False):
    """
     This function crops an image patch from the GSV image passed in input_path by forming a bounding box from
     the passed outline. It will save the output image patch in output_path
     
     :param input_path: A path to a directory where GSV files are stored.
     :type input_path: str.
     :param output_filename: A name of an output image path
     :type output_filename: str.
     :param outline: A set of user provided GSV image points
     :type outline: list.
     :param aspect_ratio: An aspect ratio (width/height) of image patches to crop. (1:aspect_ratio)
     :param show_image: A flag to indicate whether the function should show the cropped image or not.
     :type show_image: bool. 
    """
    filename = input_path + '/images/pano.png'
    # ensure_dir(directory)
    im = Image.open(filename)
    
    xys = outline.strip().split(' ')
    xs = []
    ys = []
    points = []
    for x, y in zip(xys[0::2], xys[1::2]):
        p = user_point_to_sv_image_point(input_path, {'x': x, 'y': y})
        points.append(p)
    
    # Compute the bounding box of the passed label points
    # If aspect_ratio is passed, format the bounding box so the 
    # aspect ratio of the cropped image will be consistent across
    # all the image patches 
    boundingbox = sv_image_points_to_bounding_box(points)
    if aspect_ratio and (type(aspect_ratio) == int or type(aspect_ratio) == float):
        patch_height = boundingbox['y_max'] - boundingbox['y_min']
        patch_width = patch_height * aspect_ratio 
        
        if boundingbox['boundary']:
            x_min = boundingbox['x_min'] - GSVImage.im_width
        else:
            x_min = boundingbox['x_min']
        boundingbox_width = boundingbox['x_max'] - x_min
        
        # If patch_width is larger than the boundingbox_width, use the patch_width.
        # Otherwise use the bounding_box width and modify the height of the bounding box
        if boundingbox_width < patch_width:
            x_center = (boundingbox['x_max'] + x_min) / 2
            
            # Set x_max and x_min
            x_min = x_center - patch_width / 2
            
            if x_min < 0:
                boundingbox['boundary'] = True
                boundingbox['x_min'] = GSVImage.im_width + x_min
            else:
                boundingbox['boundary'] = False
                boundingbox['x_min'] = x_min
            
            boundingbox['x_max'] = x_center + patch_width / 2
            
            boundingbox['x_max'] = int(boundingbox['x_max'])
            boundingbox['x_min'] = int(boundingbox['x_min'])
        else:
            patch_width = boundingbox_width
            patch_height = boundingbox_width / aspect_ratio
            y_center = (boundingbox['y_max'] + boundingbox['y_min']) / 2
            boundingbox['y_max'] = int(y_center + patch_height / 2) 
            boundingbox['y_min'] = int(y_center - patch_height / 2)
    
    # boundingbox['boundary'] indicates whether the bounding box goes over the boundary of a SV image
    # If it does (i.e., boundingbox['boundary'] == True), take care of it. Otherwise just crop it.
    if boundingbox['boundary']:
        patch_height = boundingbox['y_max'] - boundingbox['y_min']
        patch_width = (GSVImage.im_width - boundingbox['x_min']) + boundingbox['x_max'] 
        im_dimension=(patch_width, patch_height)
        patch = Image.new('RGBA', im_dimension, (0, 0, 0, 0))
        
        # Crop and paste the first half of the bounding box
        box = (boundingbox['x_min'], boundingbox['y_min'], GSVImage.im_width, boundingbox['y_max'])
        partial = im.crop(box)
        patch.paste(partial, (0, 0))
        
        # Crop and paste the last half of the bounding box
        box = (0, boundingbox['y_min'], boundingbox['x_max'], boundingbox['y_max'])
        partial = im.crop(box)
        patch.paste(partial, (GSVImage.im_width - boundingbox['x_min'], 0))
    else:
        # http://stackoverflow.com/questions/1076638/trouble-using-python-pil-library-to-crop-and-save-image
        box = (boundingbox['x_min'], boundingbox['y_min'], boundingbox['x_max'], boundingbox['y_max'])
        patch = im.crop(box)
    
    if show_image:
        figure()
        imshow(patch)
        title('PanoId: ' + input_path[:-1].split('/')[-1])
        show()
    
    patch.save(output_filename, 'JPEG')
    
    return


def user_point_to_sv_image_point(path, point):
    """
    This function converts a GSV image point coordinate provided by user through CSI interface to 
    a true GSV image coordinate
    
    :param path: 
        A path to a directory where GSV files for the target panorama are stored
        E.g.,  path: '../data/GSV/MO1a01Frnzs4IgoGxo1XvQ/'
    :type path: str.
    :param point: 
        A GSV image point provided by a user through CSI interface. 
        If string, pass it like '3152 50', i.e., 'x-coordinate y-coordinate'
        If dict, pass it like '{x: 3152, y:50}'
    :type point: str.
    """
    
    if type(point) == str:
        p = point.strip().split(' ')
        if len(p) != 2:
            raise ValueError("plot_user_point() expect the second input format to be 'x y'")
        x = int(p[0].strip())
        y = int(p[1].strip())
    elif type(point) == dict:
        x = int(point['x'])
        y = int(point['y'])
    elif type(point) == tuple:
        x = int(point[0])
        y = int(point[1])
    else:
        x = int(point[0])
        y = int(point[1])
    
    # Extract the sv meta data.
    xml = open(path + 'meta.xml', 'rb')
    tree = ET.parse(xml)
    root = tree.getroot()
    pano_yaw_deg = float(root.find('projection_properties').get('pano_yaw_deg'))
    # tilt_yaw_deg = float(root.find('projection_properties').get('tilt_yaw_deg'))
    yaw_deg = pano_yaw_deg # - tilt_yaw_deg
    
    im_width = GSVImage.im_width
    im_height = GSVImage.im_height

    # Translate a point to adjust its coordinate to the local image.
    y = y
    x = ((540 - yaw_deg) / 360) * im_width + x
    x = x % im_width
    y = im_height / 2 - y

    x = int(x)
    y = int(y)
    return x, y
    

def sv_image_points_to_bounding_box(points):
    '''
    :param points:
         A list of GSV image points provided by users through the CSI interface.
         E.g., [(2049, 3818), (2085, 3739), (1984, 3716), (1731, 3702), (1731, 3748), (1819, 3790), (1901, 3818)]
    :returns: 
        A bounding box. x_min/x_max/y_min/y_max and whether the boudning box is split by the vertical image boundary or not. 
        If the bounding box is split by the vertical image boundary, then x_max is the largest value between x = [0, gsv_im_width/2], and
        x_min is the smallest value between x = [gsv_im_width/2, gsv_im_width]
    '''    
    boundary = False
    x_min = 1000000
    x_max = -1
    y_min = 1000000
    y_max = -1000000
    
    #
    # Check if the outline points are split by the vertical image boundary.
    for point in points:
        if point[0] < x_min:
            x_min = point[0]
        if point[0] > x_max:
            x_max = point[0]
        if point[1] < y_min:
            y_min = point[1]
        if point[1] > y_max:
            y_max = point[1]
            
    if x_max - x_min > 3500:
        boundary = True
    
    #
    # Split in two cases.
    if boundary:
        x_min = 1000000
        x_max = -1
        for point in points:
            # x min and max
            if point[0] < GSVImage.im_width / 2:
                if point[0] > x_max:
                    x_max = point[0]
            if point[0] > GSVImage.im_width / 2:
                if point[0] < x_min:
                    x_min = point[0]
    
    return {'boundary' : boundary,
            'x_min': x_min, 
            'x_max': x_max,
            'y_min': y_min,
            'y_max': y_max}


def sv_image_point_to_user_point(file_path, point, image_size=None):
    """
    This method converts a Street View image point (x, y) on a street view image (e.g., one of bounding points that
    forms a curb ramp bounding box that is detected by a detector) into user point (or point on SV API image).
    """
    (x, y) = point
    x = int(x)
    y = int(y)
    
    im_width = GSVImage.im_width
    im_height = GSVImage.im_height
    
    if image_size:
        w = image_size[0]
        h = image_size[1]
        x = x * im_width / w
        y = y * im_height / h 

    #    
    # Extract the sv meta data.
    xml = open(file_path + 'meta.xml', 'rb')
    tree = ET.parse(xml)
    root = tree.getroot()
    pano_yaw_deg = float(root.find('projection_properties').get('pano_yaw_deg'))
    yaw_deg = pano_yaw_deg 
    #
    # Translate a point to adjust its coordinate to the local image.
    y = im_height / 2 - y
    x = x - ((540 - yaw_deg) / 360) * im_width
    x = (x + im_width) % im_width
    

    x = int(x)
    y = int(y)
    return x, y


def bounding_box_area_overlap(bb1, bb2):
    """
     This function takes two bounding boxes and calculates the area overlap.
     
     Todo. There is a corner case that I'm not capturing.
     There could be a boundary label and non-boundary label that could intersect.
    """
    if bb1['boundary']:
        bb1_x_min = bb1['x_min'] - GSVImage.im_width
    else:
        bb1_x_min = bb1['x_min']
    if bb2['boundary']:
        bb2_x_min = bb2['x_min'] - GSVImage.im_width
    else:
        bb2_x_min = bb2['x_min']
    
    #
    # To make the function simple, manipulate data so the bb2 will always have smaller x_min
    if bb1_x_min < bb2_x_min:
        temp = bb1_x_min
        bb1_x_min = bb2_x_min
        bb2_x_min = temp
        temp = bb1
        bb1 = bb2
        bb2 = temp
    
    bb1_x_max = bb1['x_max']
    bb2_x_max = bb2['x_max']
    
    bb1_y_max = bb1['y_max']
    bb2_y_max = bb2['y_max']
    bb1_y_min = bb1['y_min']
    bb2_y_min = bb2['y_min']
    
    if bb2_x_max < bb1_x_min:
        overlap = 0
    elif bb2_x_max < bb1_x_max:
        if bb2_y_min < bb1_y_min:
            if bb2_y_max < bb1_y_min:
                overlap = 0
            elif bb2_y_max < bb1_y_max:
                overlap = (bb2_y_max - bb1_y_min) * (bb2_x_max - bb1_x_min)
            else:
                overlap = (bb1_y_max - bb1_y_min) * (bb2_x_max - bb1_x_min)
        elif bb2_y_min < bb1_y_max:
            #
            # bb1_y_min < bb2_y_min < bb1_y_max:
            if bb2_y_max < bb1_y_max:
                overlap = (bb2_y_max - bb2_y_min) * (bb2_x_max - bb1_x_min)
            else:
                overlap = (bb1_y_max - bb2_y_min) * (bb2_x_max - bb1_x_min)
        else:
            #
            # bb2_y_min > bb1_y_max
            overlap = 0
    else:
        #
        # bb2_x_max > bb1_x_max
        if bb2_y_min < bb1_y_min:
            if bb2_y_max < bb1_y_min:
                overlap = 0
            elif bb2_y_max < bb1_y_max:
                overlap = (bb2_y_max - bb1_y_min) * (bb1_x_max - bb1_x_min)
            else:
                overlap = (bb1_y_max - bb1_y_min) * (bb1_x_max - bb1_x_min)
        elif bb2_y_min < bb1_y_max:
            #
            # bb1_y_min < bb2_y_min < bb1_y_max:
            if bb2_y_max < bb1_y_max:
                overlap = (bb2_y_max - bb2_y_min) * (bb1_x_max - bb1_x_min)
            else:
                overlap = (bb1_y_max - bb2_y_min) * (bb1_x_max - bb1_x_min)
        else:
            #
            # bb2_y_min > bb1_y_max
            overlap = 0
    
    area_bb1 = (bb1_y_max - bb1_y_min) * (bb1_x_max - bb1_x_min)
    area_bb2 = (bb2_y_max - bb2_y_min) * (bb2_x_max - bb2_x_min)
    area = area_bb1 + area_bb2 - overlap
    return float(overlap) / area

if __name__ == '__main__':
    print("GSVImage.py")

