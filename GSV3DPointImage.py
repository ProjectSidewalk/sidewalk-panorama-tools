from PIL import Image
import math
import numpy as np
import os.path

import GSVImage
import GSVTopDownImage
from pylab import *
from scipy import ndimage
from utilities import *

import SidewalkDB as SDB


class GSV3DPointImage(object):
    def __init__(self, path):
        """
         A constructor. This method takes a path to depth.txt. For example:
         '../data/GSV/5umV8SPGE1jidFGstzcQDA/' . It then reads the depth.txt and parse
         the 3D point data. Note depth.txt contains 512x256x3 points, representing (x, y, z)
         in a Cartesian coordinate of 512x256 points (which corresponds to the actual GSV image).
         Also note that the origin of the space is (supposedly) the position of a camera on a SV car,
         which means the z-value of the origin is 1 to 2 meters higher than the ground. 
        """
        if type(path) != str and len(str) > 0:
            raise ValueError('path should be a string')
        if path[-1] != '/':
            path += '/'

        ensure_dir(path)
        self.path = path
        self.pano_id = self.path.split('/')[-1]
        self.depth_filename = path + 'depth.txt'
        self.image_filename = path + 'images/pano.jpg'

        with open(self.depth_filename, 'rb') as f:
            depth = loadtxt(f)
    
        self.depth = depth
        self.px = depth[:, 0::3]
        self.py = depth[:, 1::3]
        self.pz = depth[:, 2::3]
        
        #
        # Height and width of the 3D point image.
        self.height, self.width = self.px.shape
        self.gsv_depth_height = self.height
        self.gsv_depth_width = self.width 
        self.gsv_image_width = GSVImage.GSVImage.gsv_image_width
        self.gsv_image_height = GSVImage.GSVImage.gsv_image_height

        
        #        
        # Compute normal vectors at each point
        # At each point, compute the vectors going outwards from the point by 
        # taking 8 neighborign points (at the edge of the image you won't get all 8 neighbors)
        # Then calculate the normal vector by generating and solving a homogeneous equation (Ax = 0) by svd.
        if not os.path.isfile(path + 'normal.txt'):
        #if True:
            normal_matrix = zeros((self.height, self.width * 3)) 
            for row_idx in range(self.height):
                for col_idx in range(self.width):
                    vectors = []
                    p = array([self.px[row_idx, col_idx], self.py[row_idx, col_idx], self.pz[row_idx, col_idx]])
                    if math.isnan(p[0]) or math.isnan(p[1]) or math.isnan(p[2]):
                        # If p is nan, normal is also nan
                        normal = array([float('nan'), float('nan'), float('nan')])
                        normal_matrix[row_idx, 3 * col_idx] = normal[0]
                        normal_matrix[row_idx, 3 * col_idx + 1] = normal[1]
                        normal_matrix[row_idx, 3 * col_idx + 2] = normal[2]
                    else:
                        vec_indices = [(row_idx - 1, col_idx - 1), (row_idx - 1, col_idx), (row_idx - 1, col_idx + 1),
                                       (row_idx, col_idx - 1), (row_idx, col_idx + 1),
                                       (row_idx + 1, col_idx - 1), (row_idx + 1, col_idx), (row_idx + 1, col_idx + 1)]
                        
                        for idx in vec_indices:
                            ri = idx[0]
                            ci = idx[1]
                            # Check for the corner cases and the case where one of the vector is nan.
                            if ri > 0 and ri < self.height and ci > 0 and ci < self.width:
                                if not math.isnan(self.px[idx]) and not math.isnan(self.py[idx]) and not math.isnan(self.pz[idx]):  
                                    vec = array([self.px[idx], self.py[idx], self.pz[idx]]) - p
                                    vectors.append(vec)
                    
                        # You need at least 3 vectors to calculate normal vectors
                        if len(vectors) > 2:
                            vectors = array(vectors)
                            U, S, Vh = svd(vectors)
                            normal = array(Vh[-1])
                            normal = normal / sqrt(sum(normal ** 2))
                        else:
                            normal = array([float('nan'), float('nan'), float('nan')])
                        
        
                        normal_matrix[row_idx, 3 * col_idx] = normal[0]
                        normal_matrix[row_idx, 3 * col_idx + 1] = normal[1]
                        normal_matrix[row_idx, 3 * col_idx + 2] = normal[2]
                    
            savetxt(path + 'normal.txt', normal_matrix)
        else:
            normal_matrix = loadtxt(path + 'normal.txt')
        
        self.normal = normal_matrix
        self.nx = normal_matrix[:, 0::3]
        self.ny = normal_matrix[:, 1::3]
        self.nz = normal_matrix[:, 2::3]                

        return

    def depth_to_png(self, destination, mode='gray'):
        """
        Convert the depth map to grayscale png image
        """
        destination_dir = '/'.join(destination.split('/')[:-1])
        dir = os.path.dirname(destination_dir)
        dir = dir.replace('~', os.path.expanduser('~'))
        if not os.path.exists(dir):
            raise ValueError('The directory ' + str(dir) + ' does not exist.')
        destination = destination.replace('~', os.path.expanduser('~'))
        if mode != 'gray':
            pass
        else:
            #
            # Grayscale image
            depth = sqrt(self.px ** 2 + self.py ** 2 + self.pz ** 2)

            #
            # Morphology operation. Get rid of black peppers.
            from scipy import ndimage
            kernel = array([[-1, -1, -1], [-1, 8, -1], [-1, -1, -1]])
            dr = ndimage.convolve(depth, kernel, mode='reflect', cval=0.0)
            # Image.fromarray(array(depth, dtype=float)).show()
            # Image.fromarray(array(dr, dtype=float)).show()
            # Image.open(self.image_filename).resize((512,256)).convert('L').show()
            #
            imarray = asarray(Image.open(self.image_filename).resize((512,256)).convert('L'))

            import matplotlib.pyplot as plt
            from skimage.data import lena

            # depth = depth / 100
            depth_image = Image.fromarray(array(depth, dtype=float))
            # normal_image = normal_image.convert('1')
            depth_image.show()
            #normal_image.save(destination, 'PNG')

    def get_overlay_value(self, x, y, overlay="depth", verbose=False):
        """
         Given the image coordinate (x, y), return the interpolated overlay value
        """
        
        if verbose:
            print()
            print('x, y: ', x, y)
            
        gsv_im_width = self.gsv_image_width
        gsv_im_height = self.gsv_image_height
        x = (float(x) / gsv_im_width) * self.width 
        y = (float(y) / gsv_im_height) * self.height
        x_floor = math.floor(x)
        x_ceil = math.ceil(x)
        y_floor = math.floor(y)
        y_ceil = math.ceil(y)
        
        #
        # Modify incase points do not form a rectangle
        if x_floor == x_ceil:
            if x_floor < self.width:
                x_ceil += 1
            else:
                x_floor -= 1
        if y_floor == y_ceil:
            if y_floor < self.height:
                y_ceil += 1
            else:
                y_floor -= 1
        
        if overlay == 'depth':
            depth = sqrt(self.px ** 2 + self.py ** 2 + self.pz ** 2)
            points = [(x_floor, y_floor, depth[y_floor, x_floor]),
                      (x_ceil, y_floor, depth[y_floor, x_ceil]),
                      (x_ceil, y_ceil, depth[y_ceil, x_ceil]),
                      (x_floor, y_ceil, depth[y_ceil, x_floor])
                      ]
            value = bilinear_interpolation(x, y, points)
        elif overlay == 'normal_z_component':
            # Get interpolated z-component of normal vectors
            normal_z = self.nz * 255.
            points = [(x_floor, y_floor, normal_z[y_floor, x_floor]),
                      (x_ceil, y_floor, normal_z[y_floor, x_ceil]),
                      (x_ceil, y_ceil, normal_z[y_ceil, x_ceil]),
                      (x_floor, y_ceil, normal_z[y_ceil, x_floor])
                      ]
            value = bilinear_interpolation(x, y, points)
        else:
            return False
        
        return value 

    def normal_to_png(self, destination, morphology=False):
        """
         Convert the normal vector map to monochrome png image.

        """

        #
        # Make sure there is a proper destination
        destination_dir = '/'.join(destination.split('/')[:-1])
        dir = os.path.dirname(destination_dir)
        dir = dir.replace('~', os.path.expanduser('~'))
        if not os.path.exists(dir):
            raise ValueError('The directory ' + str(dir) + ' does not exist.')
        destination = destination.replace('~', os.path.expanduser('~'))

        #
        # Convert a normal bitmap to a png image
        normal_array = array(self.nz * 255, dtype=uint8)
        if morphology:
            binary_array = normal_array > 128
            binary_array = ndimage.binary_opening(binary_array, iterations=2)
            binary_array = ndimage.binary_closing(binary_array, iterations=2)
            binary_array = binary_array.astype(np.uint8)
            normal_array = binary_array * 255
        normal_image = Image.fromarray(normal_array)
        normal_image = normal_image.convert('1')
        normal_image.save(destination, 'PNG')
        return

    def point_to_3d_distance(self, x, y, verbose=True):
        """
        This method converts an image point (x, y) into a 3D distance.
        """
        #
        # First get 4 points available on the depth image that are closest to the passed point 
        depth_x = self.gsv_depth_width * (float(x) / self.gsv_image_width)
        depth_y = self.gsv_depth_height * (float(y) / self.gsv_image_height)
        depth_x_floor = floor(depth_x)
        depth_y_floor = floor(depth_y)
        depth_x_ceil = ceil(depth_x)
        depth_y_ceil = ceil(depth_y)
        
        if depth_x_ceil == self.gsv_depth_width:
            depth_x_ceil_ = 0
        else:
            depth_x_ceil_ = depth_x_ceil
        
        points = [
                  (depth_x_floor, depth_y_floor), 
                  (depth_x_floor, depth_y_ceil), 
                  (depth_x_ceil, depth_y_floor),
                  (depth_x_ceil, depth_y_ceil)
                  ] 

        depth_3d_x_values = [(point[0], point[1], self.px[point[1], depth_x_ceil_]) for point in points]
        depth_3d_y_values = [(point[0], point[1], self.py[point[1], depth_x_ceil_]) for point in points]
        depth_3d_z_values = [(point[0], point[1], self.pz[point[1], depth_x_ceil_]) for point in points]
        
        depth_3d_x_value = bilinear_interpolation(depth_x, depth_y, depth_3d_x_values)
        depth_3d_y_value = bilinear_interpolation(depth_x, depth_y, depth_3d_y_values)
        depth_3d_z_value = bilinear_interpolation(depth_x, depth_y, depth_3d_z_values)
        distance = math.sqrt(depth_3d_x_value ** 2 + depth_3d_y_value ** 2 + depth_3d_z_value ** 2)

        return distance
    
    def point_to_latlng(self, x, y, verbose=True):
        """
         This method converts an image point (x, y) into a latlng coordinate
         
         Todos.
         - You need to take care of corner cases
        """
        #
        # First get 4 points available on the depth image that are closest to the passed point 
        depth_x = self.gsv_depth_width * (float(x) / self.gsv_image_width)
        depth_y = self.gsv_depth_height * (float(y) / self.gsv_image_height)
        depth_x_floor = floor(depth_x)
        depth_y_floor = floor(depth_y)
        depth_x_ceil = ceil(depth_x)
        depth_y_ceil = ceil(depth_y)
        
        if depth_x_ceil == self.gsv_depth_width:
            depth_x_ceil_ = 0
        else:
            depth_x_ceil_ = depth_x_ceil
        
        points = [
                  (depth_x_floor, depth_y_floor), 
                  (depth_x_floor, depth_y_ceil), 
                  (depth_x_ceil, depth_y_floor),
                  (depth_x_ceil, depth_y_ceil)
                  ] 
        
        
            
        
        depth_3d_x_values = [(point[0], point[1], self.px[point[1], depth_x_ceil_]) for point in points]
        depth_3d_y_values = [(point[0], point[1], self.py[point[1], depth_x_ceil_]) for point in points]
        
        #
        # Corner cases
        # if depth_x_ceil == depth_x_floor:
        # if depth_y_ceil == depth_y_floor:
        # if x is at the left most corner
        # print depth_3d_x_values
        depth_3d_x_value = bilinear_interpolation(depth_x, depth_y, depth_3d_x_values)
        depth_3d_y_value = bilinear_interpolation(depth_x, depth_y, depth_3d_y_values)
        distance = math.sqrt(depth_3d_x_value ** 2 + depth_3d_y_value ** 2)
        
        with open(self.path + 'meta.xml', 'rb') as xml: 
            tree = ET.parse(xml)
            root = tree.getroot()
            yaw_deg = float(root.find('projection_properties').get('pano_yaw_deg'))
            yaw_deg = (yaw_deg + 180) % 360
        heading = (360 * (float(x) / self.gsv_image_width) + yaw_deg) % 360 
        latlng = distance_to_latlng(self.path, distance, heading)
        
        #latlng = point_to_latlng(self.path, (depth_3d_x_value, depth_3d_y_value)) # This function is is utilities
        if verbose: latlng 
        return latlng
    
    def save_overlay(self, outfile, overlay_type="depth", mask=None):
        """
         Save the image with overlay layer
        """
        overlay_layer_intensity = 186
        panorama_image_intensity = 70
        if overlay_type == 'depth':
            # Show depth map on top of a GSV image
            pass
        elif overlay_type == 'vertical_coordinate':
            # Highlight points with vertical coordinate less than a threshold 
            pass
        elif overlay_type == 'normal_z_component':
            # Show z-component of normal vectors at each point on top of a GSV image 
            pass
        elif overlay_type == 'mask':
            # Caution!
            # I am assuming mask is 256x512 numpy array with dtype=uint8
            mask_image = Image.fromarray(mask)
            mask_image = mask_image.convert('RGB')
            overlay = asarray(mask_image)
            overlay = overlay.astype('float')
            overlay = (overlay / 255) * overlay_layer_intensity
        else:
            pass
        
        # Format the panorama image
        panorama_image = Image.open(self.image_filename)
        panorama_image = panorama_image.resize((512, 256))
        panorama_image = panorama_image.convert('RGB')
        panorama_image = asarray(panorama_image)
        panorama_image = panorama_image.astype('float')
        panorama_image = (panorama_image / 256) * panorama_image_intensity
        
        # Blend panorama and overlay
        im_array = overlay + panorama_image
        im_array = im_array.astype('uint8')
        im = Image.fromarray(im_array)
        im.save(outfile, 'PNG')
        return
    
    def show_overlay(self, im_shape=(1024, 512), overlay_type="depth", transparency=0.7, morphology=False, mask=None):
        """
         Show the GSV image with another layer on top of it.
         overlay_type: depth, normal_z_component, 
        """
        overlay_layer_intensity = int(255. * transparency)
        panorama_image_intensity = int(255. * (1 - transparency))
        if overlay_type == 'depth':
            # Show depth map on top of a GSV image
            depth_threshold = 50  # 50
            
            depth = sqrt(self.px ** 2 + self.py ** 2 + self.pz ** 2)
            temp_mask = isnan(depth)
            depth[isnan(depth)] = depth_threshold
            depth[depth > depth_threshold] = depth_threshold
            depth = depth - depth.min()
            depth = depth / depth.max()
            depth = 255 - 255 * depth
            
            # Treating an array with NaN
            # http://stackoverflow.com/questions/5480694/numpy-calculate-averages-with-nans-removed
            #minimum_depth = ma.masked_array(depth, isnan(depth)).min()
            #depth = depth - minimum_depth
            #maximum_depth = ma.masked_array(depth, isnan(depth)).max()
            #depth = 256 - 256 * (depth / maximum_depth) 
            
            overlay = depth.astype('uint8')
            overlay = Image.fromarray(overlay).convert('RGB')
            overlay = asarray(overlay)
            overlay = overlay.astype('float')
            overlay[:,:,2] = 0.
            overlay[:,:,0] = 255.
            overlay[:,:,1] = 255. - overlay[:,:,1]
            overlay[temp_mask, :] = 0
            overlay = (overlay / 256) * overlay_layer_intensity

        elif overlay_type == 'vertical_coordinate':
            # Highlight points with vertical coordinate less than a threshold 
            vertical_axis_threshold = -2.5
            
            overlay = self.pz            
            overlay[isnan(overlay)] = 0
            overlay[overlay > vertical_axis_threshold] = 0
            overlay[overlay < vertical_axis_threshold] = 255
            overlay = overlay.astype('uint8')
            
            overlay = Image.fromarray(overlay).convert('RGB')
            overlay = asarray(overlay)
            overlay = overlay.astype('float')
            overlay = (overlay / 256) * overlay_layer_intensity
        elif overlay_type == 'normal_z_component':
            # Show z-component of normal vectors at each point on top of a GSV image 
            
            normal_array = array(self.nz * 255, dtype=uint8)
            if morphology:
                binary_array = normal_array > 128
                binary_array = ndimage.binary_opening(binary_array, iterations=2)
                binary_array = ndimage.binary_closing(binary_array, iterations=2)
                binary_array = binary_array.astype(np.uint8)
                normal_array = binary_array * 255
            
            normal_image = Image.fromarray(normal_array)
            
            normal_image = normal_image.convert('RGB')
            
            # Blending. Superimpose one image on top of another.
            # http://stackoverflow.com/questions/5605174/python-pil-function-to-divide-blend-two-images
            overlay = asarray(normal_image)
            #overlay.flags.writeable = True
            #thresh = median(overlay[overlay>0]) - 0.5    
            #overlay[overlay > thresh] = 255
            #overlay[overlay < thresh] = 0
            overlay = overlay.astype('float')
            overlay = (overlay / 255) * overlay_layer_intensity
        elif overlay_type == 'mask':
            # Caution!
            # I am assuming mask is 512x1024 numpy array with dtype=uint8
            mask_image = Image.fromarray(mask.astype(np.uint8))
            mask_image = mask_image.convert('RGB')
            overlay = asarray(mask_image)
            overlay = overlay.astype(np.float)
            overlay = (overlay / 255) * overlay_layer_intensity
        else:
            panorama_image = Image.open(self.image_filename)
            panorama_image = panorama_image.resize((512, 256))
            panorama_image.show()
            return
        
        # Format the panorama image
        # Resize Image.fromarray(overlay.astype(np.uint8), 'RGB').resize((13312, 6656)).show()
        panorama_image = Image.open(self.image_filename)
        panorama_image = panorama_image.resize(im_shape)
        panorama_image = panorama_image.convert('RGB')
        panorama_image = asarray(panorama_image)
        panorama_image = panorama_image.astype(np.float)
        panorama_image = (panorama_image / 256) * panorama_image_intensity
        
        # Blend panorama and overlay
        if overlay.shape != panorama_image.shape:
            overlay = Image.fromarray(overlay.astype(np.uint8), 'RGB').resize((panorama_image.shape[1], panorama_image.shape[0]))
            overlay = np.asarray(overlay)
        im_array = overlay + panorama_image
        im_array = im_array.astype(np.uint8)
        im = Image.fromarray(im_array)
        im.show()
        return

    def top_down_to_pano(self, src_image, size=(1024, 512)):
        """
        This method maps the top down view back to the original panorama view.
        The method can also map the mask image (currently only gray scale images) to orignal
        panorama view. You can pass the image parameter to do so.

        Fist, the method creates the mapping from top down image to panorama image
        Based on the mapping that you've created, you map the pixel values.
        """
        #
        # Get the output image size and scale relative to the 3d point data
        pano_im_width = size[0]
        pano_im_height = size[1]
        scale = pano_im_width / 512

        #
        # Get th size of 3d point image
        height_3d, width_3d = self.px.shape
        x_grid, y_grid = mgrid[0:height_3d, 0:width_3d]

        #
        # Interpolate the depth data to match the output image size
        grid_points = np.array([x_grid.flatten(), y_grid.flatten()]).T
        grid_points = grid_points * scale
        x_image_grid, y_image_grid = mgrid[0:pano_im_height, 0:pano_im_width]

        px = self.px.flatten()
        py = self.py.flatten()
        pz = self.pz.flatten()

        from scipy.interpolate import griddata
        px_new = griddata(grid_points, px, (x_image_grid, y_image_grid), method='linear')
        py_new = griddata(grid_points, py, (x_image_grid, y_image_grid), method='linear')
        pz_new = griddata(grid_points, pz, (x_image_grid, y_image_grid), method='linear')

        #
        # Vectorize
        x = arange(0, pano_im_width)
        y = arange(0, pano_im_height)
        xx, yy = np.meshgrid(x, y)

        def pixel_mapping(x, y):
            """ numpy function. This takes numpy arrays x and y. This function then maps pixels in
            a Street View image to a 2-D top down image.
            """
            depth_3d_x_values = px_new[y, x]
            depth_3d_y_values = py_new[y, x]

            #
            # Create a masked array
            # http://blog.remotesensing.io/2013/05/Dealing-with-no-data-using-NumPy-masked-array-operations/
            # invert
            # http://docs.scipy.org/doc/numpy/reference/generated/numpy.invert.html
            masked_depth_3d_x_values = np.ma.masked_invalid(depth_3d_x_values)
            masked_depth_3d_y_values = np.ma.masked_invalid(depth_3d_y_values)

            #
            # Intersect x-mask and y-mask and create new masked_depth values just in
            # case we have something like (x, y, z) = (nan, 1, nan), which we want to neglect
            mask = masked_depth_3d_x_values.mask + masked_depth_3d_y_values.mask
            masked_depth_3d_x_values = np.ma.array(depth_3d_x_values, mask=mask)
            masked_depth_3d_y_values = np.ma.array(depth_3d_y_values, mask=mask)

            #
            # Get the top down image pixel coordinate
            top_down_y_pixel_coordinates = (masked_depth_3d_y_values * 10 + GSVTopDownImage.GSVTopDownImage.height / 2).astype(int)
            top_down_x_pixel_coordinates = (masked_depth_3d_x_values * 10 + GSVTopDownImage.GSVTopDownImage.width / 2).astype(int)

            #
            # Eliminate pixels that are outside of the top down image
            mask = (top_down_x_pixel_coordinates >= 1000) + (top_down_x_pixel_coordinates < 0) + (top_down_y_pixel_coordinates >= 1000) + (top_down_y_pixel_coordinates < 0)
            top_down_x_pixel_coordinates = np.ma.array(top_down_x_pixel_coordinates, mask=mask)
            top_down_y_pixel_coordinates = np.ma.array(top_down_y_pixel_coordinates, mask=mask)

            #
            # Make arrays of top_down_image pixel coordinates
            mask = np.invert(top_down_x_pixel_coordinates.mask)
            xx = top_down_x_pixel_coordinates.data[mask]
            yy = top_down_y_pixel_coordinates.data[mask]

            return y_image_grid[mask], x_image_grid[mask], src_image[yy, xx]

        y_coordinates, x_coordinates, pixel_values = pixel_mapping(xx, yy)

        blank_image = np.zeros((size[1], size[0], pixel_values[0].size)).astype(np.uint8)
        blank_image[x_coordinates, y_coordinates, :] = array([array(pixel_values)]).T

        return blank_image.astype(np.uint8)
            
def batch_decode_normal_image():
    #
    # Retrive task panoramas and store them into TaskPanoramaTable
    sql = "SELECT * FROM TaskPanoramas WHERE TaskDescription=%s"
    with SDB.SidewalkDB() as db:
        records = db.query(sql, ('PilotTask_v2_MountPleasant'))
    
    #
    # The constructor of GSV3DPointImage creates a normal image    
    for record in records:
        pano_id = record[1]
        gsv = GSV3DPointImage('../data/GSV/' + pano_id + '/') 

def batch_get_normal_mask():
    # This function retrieves panorama ids and 
        #
    # Retrive task panoramas and store them into TaskPanoramaTable
    sql = "SELECT * FROM TaskPanoramas WHERE TaskDescription=%s"
    with SDB.SidewalkDB() as db:
        records = db.query(sql, ('PilotTask_v2_MountPleasant'))
    
    #
    # The constructor of GSV3DPointImage creates a normal image    
    for record in records:
        pano_id = record[1]
        gsv_3d_point_image = GSV3DPointImage('../data/GSV/' + pano_id + '/')
        filename = '../data/temp/masked_images_2/' + pano_id + '.png'
        gsv_3d_point_image.normal_to_png(filename, morphology=True)
    return


def depth_features_for_UIST_scenes():
    filename = "../data/temp/StreetPixels.csv"
    out_filename = "../data/temp/SceneDepth.csv"
    import csv
    with open(out_filename, "w") as of:
        writer = csv.writer(of)
        writer.writerow(["PanoramaId", "DepthMean", "DepthMedian", "DepthStdev", "DepthMax", "DepthMin"])
        with open(filename, 'r') as f:
            reader = csv.reader(f)
            next(f)
            for line in reader:
                #print line
                path = '../data/GSV/%s/' % (line[0])
                gsv_3d = GSV3DPointImage(path)
                arr = np.ma.masked_invalid(np.sqrt(gsv_3d.px ** 2 + gsv_3d.py ** 2 + gsv_3d.pz ** 2))
                depth_mean = arr.mean()
                depth_median = np.ma.median(arr)
                depth_stdev = arr.std()
                depth_max = arr.max()
                depth_min = arr.min()
                # sprint "%s,%0.2f,%0.2f,%0.2f,%0.2f,%0.2f" % (line[0], depth_mean, depth_median, depth_stdev, depth_max, depth_min)
                writer.writerow([line[0], depth_mean, depth_median, depth_stdev, depth_max, depth_min])


def script():
    # panorama_ids = [
    #     "_AUz5cV_ofocoDbesxY3Kw",
    #     "0C6PG3Zpuwz11kZKfG_vUg",
    #     "D-2VNbhqOqYAKTU0hFneIw",
    #     "-dlUzxwCI_-k5RbGw6IlEg",
    #     "inU6vka3Fzz8xFkD50OkcA",
    #     "MoooLIfIUXg4Id8UfCobmw",
    #     "pc68VExSXDKWHaXqmYyNew",
    #     "q3hvBgZVagEv2wgrPgJ6og",
    #     "QaklWtS6F4qXTdmXzynhxQ",
    #     "qbPS050BhVdsW9Jh7bbLRA",
    #     "QF4m3RsaH8qNayiRq7GeSQ",
    #     "SSkaybYviuU_u0MHRoZMJw",
    #     "tId8wkF-MITThzEUOlIWXA",
    #     "U0koc8H_RE_E2eM-DFsoYQ",
    #     "uCxyTYfPDd7efWvxnYQSSg",
    #     "VsP0gcbV2Yv-WX_NlbVHTQ",
    #     "vVlss1eLpiYU9AsLi-Didg",
    #     "ZtTnE0fh5firrU4yDAhCCw",
    #     "ZWl97D059PURIRRbvb5AEA"
    #     ]
    panorama_ids = [
        # "h7ZW0_VasRt3vhevz1mjeg",
        "Aw67wmndIEG7DT3jLFXH6g"
    ]
    for panorama_id in panorama_ids:
        im_3d = GSV3DPointImage('../data/GSV/%s/' % panorama_id)
        im_3d.show_overlay((4096, 2048))

    return

if __name__ == "__main__":
    print "GSV3DPointImage.py"
    script()






