'''
Created on May 10, 2013

@author: kotarohara
'''
import cStringIO
import math
import os
import subprocess
import urllib
import urllib2
import GSVImage

from copy import deepcopy
# from GSVImage import user_point_to_sv_image_point, sv_image_points_to_bounding_box
from PIL import Image, ImageDraw
from pylab import *
from subprocess import call, check_output
from time import sleep
from utilities import *

from SidewalkDB import *
from pymysql import OperationalError

try:
    from xml.etree import cElementTree as ET
except ImportError, e:
    from xml.etree import ElementTree as ET

class GSVScraper(object):
    '''
    classdocs
    '''
    def __init__(self, data_dir='../data/GSV/', database="sidewalk"):
        '''
        Constructor
        '''
        self.pano_ids = []
        self.coordinates = []
        self.data_dir = data_dir
        try:
            self.db = SidewalkDB(database=database)
        except OperationalError, e:
            self.db = None
        return

    def decode_depthmap(self):
        """
        Decode depth.xml
        """
        for pano_id in self.pano_ids:
            if os.path.isfile(self.data_dir + pano_id + '/depth.txt'):
                print 'File already exists.'
            else:
                decode_depthmap('../data/GSV/' + pano_id + '/depth.xml', '../data/GSV/' + pano_id + '/depth.txt', verbose=True)
        return

    def depth_first_search(self, depth=5, bounding_box=None, poly=None, verbose=False):
        """
        This function
        
        :param depth: The depth of the map. 
        """
        if len(self.pano_ids) <= 0:
            raise ValueError('No pano id provided')
        all_panoramas = []

        seed_panoramas = deepcopy(self.pano_ids)
        
        for pano in seed_panoramas:
            if verbose:
                print 'Seed pano: ', pano
                print 'Extracting connected panoramas',
            visited_panoramas = []
            passed_panorama = []
            panorama_stack = [pano]
            
            while len(panorama_stack) > 0:
                if verbose: print '.',
                if verbose:
                    print panorama_stack
                curr_pano = panorama_stack[-1]
                if curr_pano not in visited_panoramas:
                    visited_panoramas.append(curr_pano)
                    
                # If the length of the stack is higher than the depth, 
                # pop the stack and continue
                # Otherwise, see the top panorama in the stack
                # Mark the top panorama as visited
                # Find the next panorama that is not visited and push it on the stack
                if len(panorama_stack) > depth:
                    leaf_pano = panorama_stack.pop()
                else:
                    links = self.get_pano_links(curr_pano)
                    all_done = True
                    for link_pano in links:
                        if link_pano not in visited_panoramas and link_pano not in passed_panorama:
                            # visited_panoramas.append(link_pano)
                            
                            #
                            # Check if link_pano is a Google provided panoramas as opposed to user provied panoramas
                            if not self.pano_is_provided_by_users(link_pano):
                            # Check if link_pano is in the bounding box
                                if poly:
                                    if self.pano_is_in_polygon(link_pano, poly):
                                        panorama_stack.append(link_pano)
                                    else:
                                        passed_panorama.append(link_pano)
                                elif bounding_box:
                                    if self.pano_is_in_bounding_box(link_pano, bounding_box):
                                        panorama_stack.append(link_pano)
                                    else:
                                        passed_panorama.append(link_pano)
                                else:
                                    panorama_stack.append(link_pano)
                            else:
                                continue
                            all_done = False
                            break
                    if all_done:
                        panorama_stack.pop()
            all_panoramas += visited_panoramas
            if verbose:
                print
                print
                
        all_panoramas = list(set(all_panoramas))
        return all_panoramas
    
    def get_intersections(self, panoramas, thresh=2):
        """
        This method takes a list of panorama ids and returns the ones that has more than 2(thresh) links (intersectiosn)
        """
        
        intersections = []
        for pano in panoramas:
            links = self.get_pano_links(pano)
            links = filter(lambda x: self.pano_is_provided_by_google(x), links)
            if len(links) > thresh:
                intersections.append(pano)
        return intersections

    def get_pano_coordinate(self, pano_id):
        """
         This method takes a panorama id and returns the lat/lng coordinate
         
         :param pano_id: panorama id
        """
        self.get_pano_metadata([pano_id])
        xml = open(self.data_dir + pano_id + '/meta.xml', 'rb')
        tree = ET.parse(xml)
        data = tree.find('data_properties').attrib
        lat = float(data['lat'])
        lng = float(data['lng'])
        return (lat, lng)

    def get_projection_properties(self, pano_id):
        """
        This method takes a panorama id and returns the projection properties including yaw_degree and
        """
        self.get_pano_metadata([pano_id])
        xml = open(self.data_dir + pano_id + '/meta.xml', 'rb')
        tree = ET.parse(xml)
        data = tree.find('projection_properties').attrib
        yaw = float(data['pano_yaw_deg'])
        pitch = float(data['tilt_pitch_deg'])
        return (yaw, pitch)

    def get_pano_depthdata(self, decode=True, delay=1000.):
        '''
         This method downloads a xml file that contains depth information from GSV. It first
         checks if we have a folder for each pano_id, and checks if we already have the corresponding
         depth file or not.  
        '''

        base_url = "http://maps.google.com/cbk?output=xml&cb_client=maps_sv&hl=en&dm=1&pm=1&ph=1&renderer=cubic,spherical&v=4&panoid="
        for pano_id in self.pano_ids:
            print '-- Extracting depth data for', pano_id, '...',
            # Check if the directory exists. Then check if the file already exists and skip if it does.
            ensure_dir(self.data_dir + pano_id)
            if os.path.isfile(self.data_dir + pano_id + '/depth.xml'):
                print 'File already exists.'
                continue
            
            url = base_url + pano_id
            with open(self.data_dir + pano_id + '/depth.xml', 'wb') as f:
                req = urllib2.urlopen(url)
                for line in req:
                    f.write(line)
                    
            # Wait a little bit so you don't get blocked by Google
            sleep_in_seconds = float(delay) / 1000
            sleep(sleep_in_seconds)
            
            print 'Done.'
        
        if decode:
            self.decode_depthmap()

        return
    
    def get_pano_id(self, lat, lng, verbose=False):
        """
        This method gets the closest panorama id from the given latlng coordinate
        """
        url_header = 'http://cbk0.google.com/cbk?output=xml&ll='
        url = url_header + str(lat) + ',' + str(lng)
        pano_id = None
         
        try:
            pano_xml = urllib.urlopen(url)
            tree = ET.parse(pano_xml)
            root = tree.getroot()
        
            pano_id = root.find('data_properties').get('pano_id')
        except AttributeError:
            pass
        # Wait a little bit so you don't get blocked by Google
        sleep_in_milliseconds = float(1000) / 1000
        sleep(sleep_in_milliseconds)

        return pano_id
    
    def get_pano_image(self, delay=100.):
        '''
         This function collects panorama images and stitch them together
         With zoom=5, there are 26x13 images. 
         http://stackoverflow.com/questions/7391945/how-do-i-read-image-data-from-a-url-in-python
        '''        
        'http://maps.google.com/cbk?output=tile&zoom=5&x=1&y=12&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4&panoid=rP_WcfFFp3V23ESWa59p4Q'        
        im_dimension = (512 * 26, 512 * 13)
        blank_image = Image.new('RGBA', im_dimension, (0, 0, 0, 0))
        base_url = 'http://maps.google.com/cbk?'

        for pano_id in self.pano_ids:
            print '-- Extracting images for', pano_id,
            ensure_dir(self.data_dir + pano_id)
            ensure_dir(self.data_dir + pano_id + '/images/')
            out_image_name = self.data_dir + pano_id + '/images/pano.jpg'
            if os.path.isfile(out_image_name):
                print 'File already exists.'
                continue

            for y in range(13): 
                for x in range(26):
                    url_param = 'output=tile&zoom=5&x=' + str(x) + '&y=' + str(y) + '&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4&panoid=' + pano_id
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
                print '.',
            print

            # In some cases (e.g., old GSV images), we don't have zoom level 5, so
            # we need to set the zoom level to 3.
            if array(blank_image)[:, :, :3].sum() == 0:
                print "Panorama %s is an old image and does not have the tiles for zoom level"
                temp_im_dimension = (int(512 * 6.5), int(512 * 3.25))
                temp_blank_image = Image.new('RGBA', temp_im_dimension, (0, 0, 0, 0))
                for y in range(3):
                    for x in range(7):
                        url_param = 'output=tile&zoom=3&x=' + str(x) + '&y=' + str(y) + '&cb_client=maps_sv&fover=2&onerr=3&renderer=spherical&v=4&panoid=' + pano_id
                        url = base_url + url_param
                        # Open an image, resize it to 512x512, and paste it into a canvas
                        req = urllib.urlopen(url)
                        file = cStringIO.StringIO(req.read())
                        im = Image.open(file)
                        im = im.resize((512, 512))

                        temp_blank_image.paste(im, (512 * x, 512 * y))

                        # Wait a little bit so you don't get blocked by Google
                        sleep_in_milliseconds = float(delay) / 1000
                        sleep(sleep_in_milliseconds)
                    print '.',
                print
                temp_blank_image = temp_blank_image.resize(im_dimension, Image.ANTIALIAS)  # resize
                temp_blank_image.save(out_image_name, 'jpeg')
            else:
                blank_image.save(out_image_name, 'jpeg')
            print 'Done.'
        return

    def get_pano_links(self, pano):
        """
        This method takes a panorama id and returns a set of linked panorama ids
        
        :param pano: A GSV panorama id
        """
        self.get_pano_metadata([pano])
        xml = open(self.data_dir + pano + '/meta.xml', 'rb')
        tree = ET.parse(xml)
        links = tree.findall('annotation_properties/link')
        
        linked_panos = []
        for link in links:
            linked_panos.append(link.attrib['pano_id'])
            #linked_panos.append(link.attrib)
        """
        yaw_deg = float(root.find('projection_properties').get('pano_yaw_deg'))
        lat = float(root.find('data_properties').get('lat'))
        lng = float(root.find('data_properties').get('lng'))
        yaw_radian = radians(yaw_deg)
        rotation_matrix = array([[cos(yaw_radian), -sin(yaw_radian)], [sin(yaw_radian), cos(yaw_radian)]])
        """
        return linked_panos
    
    def get_pano_metadata(self, pano_ids=None, delay=1000., save_as_file=True, target_dir=None, verbose=False):
        """
        This function collects Google Street View panorama metadata that corresponds to the nearest GSV panoramas.
        E.g.,
        http://jamiethompson.co.uk/web/2010/05/15/google-streetview-static-api/
        http://cbk0.google.com/cbk?output=xml&ll=51.494966,-0.146674
        """
        
        if not pano_ids:
            pano_ids = self.pano_ids
        elif type(pano_ids) != list:
            raise ValueError('pano_ids must be a list of GSV panorama ids') 
        
        
        api_header = 'http://cbk0.google.com/cbk?output=xml'
        for pano_id in pano_ids:
            if verbose:
                print '-- Extracting metadata for', pano_id, '...',
            # Check if the directory exists. Then check if the file already exists and skip if it does.
            # Check file: http://stackoverflow.com/questions/82831/how-do-i-check-if-a-file-exists-using-python
            if target_dir is None:
                target_dir = self.data_dir
            ensure_dir(target_dir + pano_id + '/')
            # ensure_dir(self.data_dir + pano_id + '/')
            if os.path.isfile(target_dir + pano_id + '/meta.xml'):
                if verbose:
                    print 'File already exists.'
                continue
            
            url = api_header + '&panoid=' + pano_id
            req = urllib2.urlopen(url)

            if save_as_file:

                with open(target_dir + pano_id + '/meta.xml', 'w+') as my_file:
                    for line in req:
                        my_file.write(line)



            # Wait a little bit so you don't get blocked by Google
            sleep_in_milliseconds = float(delay) / 1000
            sleep(sleep_in_milliseconds)
            if verbose:
                print 'Done.'
        
        return
    
    def pano_is_provided_by_users(self, link_pano):
        """
        This method checks if the panorama has level_id attribute (which exists only in user provided panorama images.)
        """
        self.get_pano_metadata([link_pano])
        xml = open(self.data_dir + link_pano + '/meta.xml', 'rb')
        try:
            # print link_pano
            tree = ET.parse(xml)
        except ET.ParseError:
            print link_pano
            raise
        
        if tree.find('levels') != None:
            return True
        elif tree.find('data_properties/attribution_name') != None:
            return True
        else:
            return False
    
    def pano_is_provided_by_google(self, link_pano):
        """
        This method checks if the panorama is provided by Google
        """
        if self.pano_is_provided_by_users(link_pano):
            return False
        else:
            return True
        
    
    def pano_is_in_bounding_box(self, link_pano, bounding_box):
        """
            :param link_pano: A panorama id.
            :param bounding_box: 
                A tuple of latitudes and longitudes that defines a bounding box of which part of the map you wnat to look at.
                Format is (min_lat, max_lat, min_lng, max_lng)
                E.g., (38.896231,38.897934,-77.029755,-77.025109)
        """
        min_lat = bounding_box[0]
        max_lat = bounding_box[1]
        min_lng = bounding_box[2]
        max_lng = bounding_box[3]
        lat, lng = self.get_pano_coordinate(link_pano)
        """self.get_pano_metadata([link_pano])
        xml = open(self.data_dir + link_pano + '/meta.xml', 'rb')
        tree = ET.parse(xml)
        data = tree.find('data_properties').attrib
        lat = float(data['lat'])
        lng = float(data['lng'])"""
        # coffee
        
        if min_lat < lat and max_lat > lat and min_lng < lng and max_lng > lng:
            return True
        else:
            return False

    def pano_is_in_polygon(self, pano_id, poly):
        """
         This method takes a panorama id, retrieves the latlng coordinate, and checks if
         the coordinate is in the polygon.
        """
        lat, lng = self.get_pano_coordinate(pano_id)
        return point_inside_polygon(lng, lat, poly)
    
    def set_pano_ids(self, pano_ids):
        '''
         This method sets pano_ids of your interest. This method creates data folders
         to store all bunch of crap you will download from Street View and name it with
         pano_id.
        '''
        self.pano_ids = pano_ids
        
        for pano_id in pano_ids:
            ensure_dir(self.data_dir + pano_id + '/')
            
"""
 Helper functions
"""        


def read_depth_file(path, show_image=True):
    """
    This function reads a 3D point-cloud data from the file generated by ./decode_depthmap. (depth.txt)
    The depth.txt contains (x, y, z) points in the Cartesian coordinate. The origin is set to the position
    of a camera on a SV car. So the z of the origin is about 1 or 2 meters higher from the ground.   
    
    Todo. Need to investigate if the 3D data takes into account of camera tilt at non-flat locations.
    """
    filename = path + 'depth.txt'
    image_name = path + 'images/pano.png'
    pano_im = array(Image.open(image_name))

    with open(filename, 'rb') as f:
        depth = loadtxt(f)

    depth_x = depth[:, 0::3]
    depth_y = depth[:, 1::3]
    depth_z = depth[:, 2::3]

    figure()
    im = imshow(pano_im)
    fig = gcf()
    ax = gca()

    class EventHandler:
        def __init__(self):
            self.prev_x = 0
            self.prev_y = 0
            self.prev_z = 0
            fig.canvas.mpl_connect('button_press_event', self.onpress)

        def onpress(self, event):
            '''
             On press, do bilinear interpolation
             http://en.wikipedia.org/wiki/Bilinear_interpolation
             http://stackoverflow.com/questions/8661537/how-to-perform-bilinear-interpolation-in-python
            '''
            if event.inaxes != ax:
                return
            xi, yi = (int(round(n)) for n in (event.xdata, event.ydata))
            # value = im.get_array()[xi,yi]
            # color = im.cmap(im.norm(value))
            
            val_x, val_y, val_z = interpolated_3d_point(xi, yi, depth_x, depth_y, depth_z)            
            print 'depth_x, depth_y, depth_z', val_x, val_y, val_z
            
            user_points = [(val_x, val_y)]
            latlngs = points_to_latlng(path, user_points)
            lat = latlngs[0][0]
            lng = latlngs[0][1]
            print 'lat, lng:', lat, lng
            print 'Distance from previous point:', math.sqrt(math.pow((val_x - self.prev_x),2) + math.pow((val_y - self.prev_y), 2) + math.pow((val_z - self.prev_z), 2))
            self.prev_x = val_x
            self.prev_y = val_y
            self.prev_z = val_z

    handler = EventHandler()
    show()
    return


def decode_depthmap(file_in, file_out, verbose=True):
    """
     This function executes ./decode_depthmap . The decode_depthmap retrieves 3D point-cloud data 
     from the file_in (depth.xml) and spits out the result. 

     call function
     http://stackoverflow.com/questions/89228/calling-an-external-command-in-python
    """    
    if verbose: print '-- Decoding depth data...', 
    if os.path.isfile(file_out):
        print 'File already exists.'
        return
    
    import platform
    
    operating_system = platform.system()
    
    if operating_system == 'Windows':
        # Windows
        #
        # Caution!!! I have worked on this for a couple of hours, but I could not run the decode_depthmap_win.exe 
        # from PyLab using subprocess.call. Quick walk around is to run the python script from the cmd.exe
        # Will investigate the solution in future.
        # http://stackoverflow.com/questions/3022013/windows-cant-find-the-file-on-subprocess-call
        # http://stackoverflow.com/questions/10236260/subprocess-pydev-console-vs-cmd-exe
        
        # pwd = os.path.dirname(os.path.abspath(__file__))
        # bin_dir = "\\".join(pwd.split("\\")[:-1]) + "\\bin"
        # my_env = os.environ.copy()
        # my_env["PATH"] += os.pathsep + bin_dir

        call(["../bin/decode_depthmap_win.exe", file_in, file_out])
        #popen = subprocess.Popen(["../bin/decode_depthmap_win.exe", file_in, file_out], creationflags=subprocess.CREATE_NEW_CONSOLE)
        #popen.wait()
        #out = check_output([bin_dir + "\decode_depthmap_win.exe", file_in, file_out], env=my_env)
        #if verbose: print out
    else:
        # Mac
        call(["../bin/decode_depthmap", file_in, file_out])
    print 'Done.'
    return


def plot_user_points(pano_id):
    # Image constant
    records = []
    sql = """
SELECT LabelTypeId, svImageX, svImageY, PanoYawDeg FROM Label
INNER JOIN LabelPosition
ON Label.LabelId = LabelPosition.LabelId
INNER JOIN Panorama
ON LabelGSVPanoramaId = Panorama.GSVPanoramaId
INNER JOIN PanoramaProjectionProperty
ON Panorama.GSVPanoramaId = PanoramaProjectionProperty.GSVPanoramaId
WHERE LabelGSVPanoramaId = %s 
AND LabelTypeId=1
    """
    from BusStopDB import BusStopDB
    with BusStopDB() as db:
        records = db.query(sql, (pano_id))

    im_width = GSVImage.GSVImage.im_width
    im_height = GSVImage.GSVImage.im_height
    PanoYawDeg = float(records[0][3])
    
    filename = '../data/GSV/' + pano_id + '/images/pano.png'
    im = Image.open(filename)
    draw = ImageDraw.Draw(im)
    for i, record in enumerate(records):
        # PIL draw circle
        # http://stackoverflow.com/questions/2980366/draw-circle-pil-python
        # http://www.pythonware.com/library/pil/handbook/imagedraw.htm
        # User input data 
        sv_image_x = int(record[1]) - 100
        sv_image_y = int(record[2])
        x = ((PanoYawDeg / 360) * im_width  + sv_image_x) % im_width
        y = im_height / 2 - sv_image_y
        r = 30
        draw.ellipse((x-r, y-r, x+r, y+r), fill=128)
        
    figure()
    imshow(im)
    show()
    return

"""
 Helper functions
"""
def batch_decode_depth_data():
    #
    # Retrive task panoramas and store them into TaskPanoramaTable
    sql = "SELECT * FROM TaskPanoramas WHERE TaskDescription=%s"
    with SidewalkDB() as db:
        records = db.query(sql, ('PilotTask_v2_MountPleasant'))
        
    pano_ids = [record[1] for record in records]
    scraper = GSVScraper()
    scraper.set_pano_ids(pano_ids)
    scraper.get_pano_depthdata()

def format_pano_metadata(pano_id, delay=1000.0, verbose=False):
    """
    This function takes a pano_id (e.g., dWeBDzGMXwQv5fu1GoNy8Q) and
    returns a Google Street View panorama metadata that corresponds to the nearest GSV panorama.
    E.g.,

    http://jamiethompson.co.uk/web/2010/05/15/google-streetview-static-api/
    http://cbk0.google.com/cbk?output=xml&ll=51.494966,-0.146674
    """

    NOT_PROVIDED = 'Not provided'
    api_header = 'http://cbk0.google.com/cbk?'
    api_parameter = 'output=xml'
    api_parameter += '&panoid=' + pano_id
    api_path = api_header + api_parameter
    try:
        pano = {'pano_id': pano_id}
        do_sleep = False
        if os.path.isfile('../data/GSV/' + pano_id + '/meta.xml'):
            pano_xml = '../data/GSV/' + pano_id + '/meta.xml'
        else:
            do_sleep = True
            pano_xml = urllib.urlopen(api_path)
        tree = ET.parse(pano_xml)
        root = tree.getroot()

        for child in root:
            if child.tag == 'data_properties':
                pano[child.tag] = child.attrib
                pano[child.tag]['copyright'] = child.find('copyright').text.strip().replace('\xa9', 'Copyright')

                if child.find('text') != None and child.find('text').text != None:
                    pano[child.tag]['text'] = child.find('text').text.strip()
                else:
                    pano[child.tag]['text'] = NOT_PROVIDED

                if child.find('street_range') != None and child.find('street_range').text is not None:
                    pano[child.tag]['street_range'] = child.find('street_range').text.strip()
                else:
                    pano[child.tag]['street_range'] = NOT_PROVIDED

                if child.find('region') is not None and child.find('region').text is not None:
                    pano[child.tag]['region'] = child.find('region').text.strip()
                else:
                    pano[child.tag]['region'] = NOT_PROVIDED

                if child.find('country') is not None and child.find('country').text is not None:
                    pano[child.tag]['country'] = child.find('country').text.strip()
                else:
                    pano[child.tag]['country'] = NOT_PROVIDED
            elif child.tag == 'projection_properties':
                pano[child.tag] = child.attrib
            elif child.tag == 'annotation_properties':
                pano['links'] = []
                for item in child:
                    if item.tag == 'link':
                        link_attrib = {}
                        link_attrib = item.attrib

                        if item.find('link_text') != None and item.find('link_text').text != None:
                            link_attrib['link_text'] = item.find('link_text').text.strip()
                        else:
                            link_attrib['link_text'] = NOT_PROVIDED

                        pano['links'].append(link_attrib)
        pano['intersection'] = {'lat' : pano['data_properties']['lat'], 'lng' : pano['data_properties']['lng']}
        if verbose:
            print pano
    except:
        raise XMLAcquisitionError('Exception: Failed reading xml.')

    if do_sleep:
        sleep_in_milliseconds = delay / 1000
        sleep(sleep_in_milliseconds)
    return pano


def get_nearby_pano_ids(pano_id, max_step_size=2, delay=2000.0, verbose=False):
    """
     This function performs breadth first search of GSV panoarama scenes
    """
    queue = [{'step_size': 0, 'pano_id': pano_id, 'origin_pano_id': pano_id}]
    visited = []
    ret = []

    while queue:
        pano_item = queue.pop(0)
        if pano_item['step_size'] > max_step_size:
            break

        if pano_item['pano_id'] not in visited:
            visited.append(pano_item['pano_id'])
            ret.append(pano_item)
            pano_data = get_pano_metadata(pano_item['pano_id'], verbose)
            linked_pano_ids = [link['pano_id'] for link in pano_data['links']]
            for linked_pano_id in linked_pano_ids:
                queue.append({'step_size' : pano_item['step_size'] + 1, 'pano_id': linked_pano_id, 'origin_pano_id': pano_id})

    return ret


def get_pano_metadata(pano_id, verbose=False):
    """
    This function takes a pano_id (e.g., dWeBDzGMXwQv5fu1GoNy8Q) and
    returns a Google Street View panorama metadata that corresponds to the nearest GSV panorama.
    E.g.,

    http://jamiethompson.co.uk/web/2010/05/15/google-streetview-static-api/
    http://cbk0.google.com/cbk?output=xml&ll=51.494966,-0.146674
    """
    gsv = GSVScraper.GSVScraper()
    NOT_PROVIDED = 'Not provided'

    api_header = 'http://cbk0.google.com/cbk?'
    api_parameter = 'output=xml'
    api_parameter += '&panoid=' + pano_id
    api_path = api_header + api_parameter
    try:
        pano = {'pano_id' : pano_id}

        gsv.get_pano_metadata([pano_id])
        pano_xml = open('../data/GSV/' + pano_id + '/meta.xml', 'rb')
        tree = ET.parse(pano_xml)
        root = tree.getroot()

        for child in root:
            if child.tag == 'data_properties':
                pano[child.tag] = child.attrib
                pano[child.tag]['copyright'] = child.find('copyright').text.strip().replace('\xa9', 'Copyright')

                if child.find('text') != None and child.find('text').text != None:
                    pano[child.tag]['text'] = child.find('text').text.strip()
                else:
                    pano[child.tag]['text'] = NOT_PROVIDED

                if child.find('street_range') != None and child.find('street_range').text != None:
                    pano[child.tag]['street_range'] = child.find('street_range').text.strip()
                else:
                    pano[child.tag]['street_range'] = NOT_PROVIDED

                if child.find('region') != None and child.find('region').text != None:
                    pano[child.tag]['region'] = child.find('region').text.strip()
                else:
                    pano[child.tag]['region'] = NOT_PROVIDED

                if child.find('country') != None and child.find('country').text != None:
                    pano[child.tag]['country'] = child.find('country').text.strip()
                else:
                    pano[child.tag]['country'] = NOT_PROVIDED
            elif child.tag == 'projection_properties':
                pano[child.tag] = child.attrib
            elif child.tag == 'annotation_properties':
                pano['links'] = []
                for item in child:
                    if item.tag == 'link':
                        link_attrib = {}
                        link_attrib = item.attrib

                        if item.find('link_text') != None and item.find('link_text').text != None:
                            link_attrib['link_text'] = item.find('link_text').text.strip()
                        else:
                            link_attrib['link_text'] = NOT_PROVIDED

                        pano['links'].append(link_attrib)
        pano['bus_stop'] = {'lat' : pano['data_properties']['lat'], 'lng' : pano['data_properties']['lng']}
        if verbose:
            print pano
    except:
        raise

    return pano

def get_nearest_pano_metadata(latlng, delay=1000.0, verbose='True'):
    """
    This function takes a latlng object (e.g. {'lat': '38.9015110', 'lng': '-77.0188500'}) and
    returns a Google Street View panorama metadata that corresponds to the nearest GSV panorama.
    E.g.,

    delay: delay in milliseconds

    http://jamiethompson.co.uk/web/2010/05/15/google-streetview-static-api/
    http://cbk0.google.com/cbk?output=xml&ll=51.494966,-0.146674
    """
    NOT_PROVIDED = 'Not provided'

    api_header = 'http://cbk0.google.com/cbk?'
    api_parameter = 'output=xml'
    api_parameter += '&ll=' + latlng['lat'] + ',' + latlng['lng']
    api_path = api_header + api_parameter

    if verbose:
        print api_path

    try:
        pano = {'bus_stop': latlng}
        pano_xml = urllib.urlopen(api_path)
        tree = ET.parse(pano_xml)
        root = tree.getroot()

        for child in root:
            if child.tag == 'data_properties':
                pano[child.tag] = child.attrib
                pano[child.tag]['copyright'] = child.find('copyright').text.strip().replace('\xa9', 'Copyright')

                if child.find('text') is not None and child.find('text').text is not None:
                    pano[child.tag]['text'] = child.find('text').text.strip()
                else:
                    pano[child.tag]['text'] = NOT_PROVIDED

                if child.find('street_range') is not None and child.find('street_range').text is not None:
                    pano[child.tag]['street_range'] = child.find('street_range').text.strip()
                else:
                    pano[child.tag]['street_range'] = NOT_PROVIDED

                if child.find('region') is not None and child.find('region').text is not None:
                    pano[child.tag]['region'] = child.find('region').text.strip()
                else:
                    pano[child.tag]['region'] = NOT_PROVIDED

                if child.find('country') is None and child.find('country').text is not None:
                    pano[child.tag]['country'] = child.find('country').text.strip()
                else:
                    pano[child.tag]['country'] = NOT_PROVIDED
            elif child.tag == 'projection_properties':
                pano[child.tag] = child.attrib
            elif child.tag == 'annotation_properties':
                pano['links'] = []
                for item in child:
                    if item.tag == 'link':
                        link_attrib = item.attrib

                        if item.find('link_text') is not None and item.find('link_text').text is not None:
                            link_attrib['link_text'] = item.find('link_text').text.strip()
                        else:
                            link_attrib['link_text'] = NOT_PROVIDED

                        pano['links'].append(link_attrib)

        if verbose:
            print pano
    except:
        raise XMLAcquisitionError('Exception: Failed reading xml.')

    sleep_in_milliseconds = float(delay) / 1000
    sleep(sleep_in_milliseconds)
    return pano


def collect_all_busstop_depth_data():
    with SidewalkDB(database="busstop") as db:
        sql = "select SourceGSVPanoramaId, TargetGSVPanoramaId from PanoramaLink"
        records = db.query(sql)

    panorama_ids = []
    for record in records:
        panorama_ids.append(record[0])
        panorama_ids.append(record[1])

    panorama_ids = list(set(panorama_ids))

    scraper = GSVScraper()
    scraper.set_pano_ids(['2V_YrQbwf45Mx9WTJ79ojg'])
    scraper.get_pano_metadata()
    scraper.get_pano_image()
    scraper.get_pano_depthdata()
    for panorama_id in panorama_ids:
        scraper.set_pano_ids([panorama_id])
        scraper.get_pano_depthdata()

if __name__ == '__main__':
    print "GSVScraper"
    collect_all_busstop_depth_data()





