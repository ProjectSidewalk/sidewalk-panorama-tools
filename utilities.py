import numpy as np
import os
import scipy.stats as spstats

from math import radians, cos, sin, asin, sqrt
from pylab import *

try:
    from xml.etree import cElementTree as ET
except ImportError as e:
    from xml.etree import ElementTree as ET

EARTH_RADIUS_M = 6371000
EARTH_RADIUS_KM = 6371


def basic_stats(value_array, verbose=False):
    """ This method takes an array like object and returns median, mean, stdev, min and max values """
    if len(value_array) == 0:
        return 0, 0, 0, 0, 0
    median = np.median(value_array)
    mean = np.mean(value_array)
    stdev = np.std(value_array)
    min = np.min(value_array)
    max = np.max(value_array)
    total = len(value_array)
    
    if verbose:
        print("Median:", "%.2f" % median)
        print("Mean:", "%.2f" % mean)
        print("Std:", "%.2f" % stdev)
        print("Min:", "%.2f" % min)
        print("Max:", "%.2f" % max)
        print("Total:", total)
    
    return median, mean, stdev, min, max, total


def bilinear_interpolation(x, y, points):
    '''Interpolate (x,y) from values associated with four points.

    The four points are a list of four triplets:  (x, y, value).
    The four points can be in any order.  They should form a rectangle.

        >>> bilinear_interpolation(12, 5.5,
        ...                        [(10, 4, 100),
        ...                         (20, 4, 200),
        ...                         (10, 6, 150),
        ...                         (20, 6, 300)])
        165.0
    
    Code written by Raymond Hettinger. Check:
    http://stackoverflow.com/questions/8661537/how-to-perform-bilinear-interpolation-in-python
    
    Modified by Kotaro.
    In case four points have same x values or y values, perform linear interpolation
    '''
    # See formula at:  http://en.wikipedia.org/wiki/Bilinear_interpolation

    points = sorted(points)               # order points by x, then by y
    (x1, y1, q11), (_x1, y2, q12), (x2, _y1, q21), (_x2, _y2, q22) = points


    if (x1 == _x1) and (x1 == x2) and (x1 == _x2):
        if x != x1:
            raise ValueError('(x, y) not on the x-axis')
        if y == y1:
            return q11
        return (q11 * (_y2 - y) + q22 * (y - y1)) / ((_y2 - y1) + 0.0)
    if (y1 == _y1) and (y1 == y2) and (y1 == _y2):
        if y != y1 :
            raise ValueError('(x, y) not on the y-axis')
        if x == x1:
            return q11
        return (q11 * (_x2 - x) + q22 * (x - x1)) / ((_x2 - x1) + 0.0)
            

    if x1 != _x1 or x2 != _x2 or y1 != _y1 or y2 != _y2:
        raise ValueError('points do not form a rectangle')
    if not x1 <= x <= x2 or not y1 <= y <= y2:
        print("x, y, x1, x2, y1, y2", x, y, x1, x2, y1, y2)
        raise ValueError('(x, y) not within the rectangle')

    return (q11 * (x2 - x) * (y2 - y) +
            q21 * (x - x1) * (y2 - y) +
            q12 * (x2 - x) * (y - y1) +
            q22 * (x - x1) * (y - y1)
           ) / ((x2 - x1) * (y2 - y1) + 0.0)


def chunks(l, n, lazy=True):
    """ Yield successive n-sized chunks from l.
    http://stackoverflow.com/questions/312443/how-do-you-split-a-list-into-evenly-sized-chunks-in-python
    """
    for i in xrange(0, len(l), n):
        yield l[i:i+n]
        
        
def distance_to_latlng(path, distance, heading):
    """
     This function takes a path, GSV image point, and heading
     http://www.movable-type.co.uk/scripts/latlong.html
    """
    with open(path + 'meta.xml', 'rb') as xml: 
        tree = ET.parse(xml)
        root = tree.getroot()
        yaw_deg = float(root.find('projection_properties').get('pano_yaw_deg'))
        yaw_deg = (yaw_deg + 180) % 360
        lat = float(root.find('data_properties').get('lat'))
        lng = float(root.find('data_properties').get('lng'))
        

    #R = 6371000 # Earch radius in meters
    R = 6353000 #Wikipedia
    #R = 6384000
    d = distance
    # bearing = (heading + yaw_deg) % 360
    #bearing = math.radians(heading + yaw_deg)
    bearing = math.radians(heading)
    
    lat_radian = math.radians(lat)
    # lng_radian = math.radians(lng)
    
    plat = math.asin(math.sin(lat_radian) * math.cos(d/R) + math.cos(lat_radian) * math.sin(d/R) * math.cos(bearing))
    plng = math.atan2(math.sin(bearing) * math.sin(d / R) * math.cos(lat_radian), math.cos(d / R) - math.sin(lat_radian) * math.sin(plat));
    
    plat = math.degrees(plat)
    plng = lng + math.degrees(plng)

    return (plat, plng)
    

def ensure_dir(path, verbose=False):
    """
     This function checkes if the given path exists. if not, it creates a new path
     http://stackoverflow.com/questions/273192/python-best-way-to-create-directory-if-it-doesnt-exist-for-file-write
    """
    if path[-1] != '/':
        path = path + '/'
    
    d = os.path.dirname(path)
    if not os.path.exists(d):
        os.makedirs(d)
        if verbose:
            print('The directory "' + path + '" does not exist. Created a new directory.')
    else:
        if verbose:
            print('Directory exists.')
    return


def find_outliers(data, mode='quartile', m=2):
    # Similar to reject_outliers function. Howerver, this function returns the indices (a boolean array) of the
    # outliers instead of the actual values
    if type(data) != np.array:
        data = np.array(data)

    if mode == 'stdev':
        return abs(data - np.mean(data)) > m * np.std(data)
    else:
        percentile_lower = spstats.scoreatpercentile(data, per=25)
        percentile_upper = spstats.scoreatpercentile(data, per=75)

        upper = data > percentile_upper
        lower = data < percentile_lower
        return upper + lower


def frequency(arr):
    """
     This function returns a frequency of items in an array
    """
    frequency_holder = {}
    for item in arr:
        if not item in frequency_holder:
            frequency_holder[item] = 0
        frequency_holder[item] += 1
    return frequency_holder


def haversine(lon1, lat1, lon2, lat2, unit="km"):
    """
    Haversine formula
    Calculate the great circle distance between two points 
    on the earth (specified in decimal degrees)
    http://stackoverflow.com/questions/4913349/haversine-formula-in-python-bearing-and-distance-between-two-gps-points
    
    It returns the distance in km by default 
    """
    # convert decimal degrees to radians 
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    # haversine formula 
    dlon = lon2 - lon1 
    dlat = lat2 - lat1 
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a)) 
    #km = 6367 * c
    km = EARTH_RADIUS_KM * c
    
    if unit == "m":
        return km * 1000
    else:
        return km 

def histogram(hist_data, header=None, x_axis_title=None, step_size=1):
    """
     Takes a list of interval data and plot.
     
     !Deprecated! Use Chart.py.
    """

    med_val, mean_val, std_val, min_val, max_val, count_val = basic_stats(hist_data)
    annotations = [
                   'Count: %d' % count_val, 
                   'Max: %.2f' % max_val,
                   'Min: %.2f' % min_val,
                   'Stdev: %.2f' % std_val,
                   'Mean: %.2f' % mean_val,
                   'Median: %.2f' % med_val
                   ]
    
    frequencies, bin_edges = np.histogram(hist_data, bins=arange(0, max(hist_data)+ step_size, step_size))


    fig = figure()
    ax = fig.add_subplot(111, autoscale_on=False)
    
    #
    # Define the domain and range
    if max(hist_data) < 1:
        ax.axis([0, max(hist_data), 0, max(frequencies) + 1])
    else:
        ax.axis([1, max(hist_data), 0, max(frequencies) + 1])
        
    #
    # Put the stats about the data
    for i, annotation in enumerate(annotations):
        ax.annotate(annotation, xy=(-10, max(frequencies) - 15 * i - 10),
                xycoords='axes points',
                verticalalignment='top',
                horizontalalignment='right',
                fontsize=14)
    
    if header: title(header)
    if x_axis_title: xlabel(x_axis_title)
    
    #
    # Use rhist and rstyle to prettify the graph
    rhist(ax, hist_data, label=None)
    ax.legend()
    rstyle(ax)
    show()
    return
    

def interpolated_3d_point(xi, yi, x_3d, y_3d, z_3d, scale=26):
    """
     This function takes a GSV image point (xi, yi) and 3d point cloud data (x_3d, y_3d, z_3d) and 
     returns its estimated 3d point. 
    """
    xi = float(xi) / scale
    yi = float(yi) / scale
    xi1 = int(math.floor(xi))
    xi2 = int(math.ceil(xi))
    yi1 = int(math.floor(yi))
    yi2 = int(math.ceil(yi))
    
    if xi1 == xi2 and yi1 == yi2:
        val_x = x_3d[yi1, xi1]
        val_y = y_3d[yi1, xi1]
        val_z = z_3d[yi1, xi1]
    else:
        points_x = ((xi1, yi1, x_3d[yi1, xi1]),   (xi1, yi2, x_3d[yi2, xi1]), (xi2, yi1, x_3d[yi1, xi2]), (xi2, yi2, x_3d[yi2, xi2]))         
        points_y = ((xi1, yi1, y_3d[yi1, xi1]),   (xi1, yi2, y_3d[yi2, xi1]), (xi2, yi1, y_3d[yi1, xi2]), (xi2, yi2, y_3d[yi2, xi2]))
        points_z = ((xi1, yi1, z_3d[yi1, xi1]),   (xi1, yi2, z_3d[yi2, xi1]), (xi2, yi1, z_3d[yi1, xi2]), (xi2, yi2, z_3d[yi2, xi2]))                  
        val_x = bilinear_interpolation(xi, yi, points_x)
        val_y = bilinear_interpolation(xi, yi, points_y)
        val_z = bilinear_interpolation(xi, yi, points_z)
    
    return val_x, val_y, val_z

           
def point_inside_polygon(x, y, poly):
    """
     This function checks whether a given point (x, y) is in a polygon poly.
     
     From http://www.ariel.com.au/a/python-point-int-poly.html
    """
    n = len(poly)
    inside =False

    p1x, p1y = poly[0]
    for i in range(n+1):
        p2x,p2y = poly[i % n]
        if y > min(p1y,p2y):
            if y <= max(p1y,p2y):
                if x <= max(p1x,p2x):
                    if p1y != p2y:
                        xinters = (y-p1y)*(p2x-p1x)/(p2y-p1y)+p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x,p1y = p2x,p2y

    return inside


def points_to_latlng(path, points):
    '''
     This function wraps point_to_latlng to get latlng coordinates of a list of points
    '''
    latlngs = [point_to_latlng(path, point) for point in points]
    return latlngs        


def point_to_latlng(path, point):
    """
    This function converts a 3D (x, y) coordinate on depth data provided on a GSV image into latlng coordinate. 
    :param path:
        e.g., '../data/GSV/rP_WcfFFp3V23ESWa59p4Q/'
    :param points: 
        e.g., (18.3720218935 -1.45833482249)
    """
    # xml = open(path + 'meta.xml', 'rb')
    with open(path, 'rb') as xml:
        tree = ET.parse(xml)
        root = tree.getroot()
        yaw_deg = float(root.find('projection_properties').get('pano_yaw_deg'))
        yaw_deg = (yaw_deg + 180) % 360
        lat = float(root.find('data_properties').get('lat'))
        lng = float(root.find('data_properties').get('lng'))
        yaw_radian = radians(yaw_deg)
        rotation_matrix = array([[cos(yaw_radian), -sin(yaw_radian)], [sin(yaw_radian), cos(yaw_radian)]])
    
    rotated_x, rotated_y = rotation_matrix.dot(array(point))
    
    #
    # http://www.movable-type.co.uk/scripts/latlong.html
    #plat = lat + rotated_y * (0.00001 / 1.1132)
    #plng = lng + rotated_x * (0.00001 / 1.1132)
    R = EARTH_RADIUS_M # m
    d = math.sqrt(rotated_x * rotated_x + rotated_y * rotated_y)
    bearing = math.atan2(rotated_x, rotated_y)
    # bearing = -bearing
    
    lat_radian = math.radians(lat)
    lng_radian = math.radians(lng)
    
    plat = math.asin(math.sin(lat_radian) * math.cos(d/R) + math.cos(lat_radian) * math.sin(d/R) * math.cos(bearing))
    plng = math.atan2(math.sin(bearing) * math.sin(d / R) * math.cos(lat_radian), math.cos(d / R) - math.sin(lat_radian) * math.sin(plat));
    
    plat = math.degrees(plat)
    plng = lng + math.degrees(plng)
    #plat = 

    return (plat, plng)


def reject_outliers(data, mode='quartile', m=2):
    # http://stackoverflow.com/questions/11686720/is-there-a-numpy-builtin-to-reject-outliers-from-a-list
    # http://stackoverflow.com/questions/2374640/how-do-i-calculate-percentiles-with-python-numpy
    # http://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.scoreatpercentile.html#scipy.stats.scoreatpercentile
    if type(data) != np.array:
        data = np.array(data)
    
    if mode == 'stdev':
        return data[abs(data - np.mean(data)) < m * np.std(data)]
    else:
        percentile_lower = spstats.scoreatpercentile(data, per=25)
        percentile_upper = spstats.scoreatpercentile(data, per=75)
    
        data = data[data < percentile_upper]
        data = data[data > percentile_lower]
    return data


def split_list(alist, wanted_parts=1):
    """
    http://stackoverflow.com/questions/752308/split-array-into-smaller-arrays
    """
    length = len(alist)
    return [ alist[i*length // wanted_parts: (i+1)*length // wanted_parts] 
             for i in range(wanted_parts) ]


"""
 Styling matplotlib by Bicubic
 http://messymind.net/2012/07/making-matplotlib-look-like-ggplot/
"""
def rstyle(ax): 
    """Styles an axes to appear like ggplot2
    Must be called after all plot and axis manipulation operations have been carried out (needs to know final tick spacing)
    """
    #set the style of the major and minor grid lines, filled blocks
    ax.grid(True, 'major', color='w', linestyle='-', linewidth=1.4)
    ax.grid(True, 'minor', color='0.92', linestyle='-', linewidth=0.7)
    ax.patch.set_facecolor('0.85')
    ax.set_axisbelow(True)
    
    #set minor tick spacing to 1/2 of the major ticks
    ax.xaxis.set_minor_locator(MultipleLocator( (plt.xticks()[0][1]-plt.xticks()[0][0]) / 2.0 ))
    ax.yaxis.set_minor_locator(MultipleLocator( (plt.yticks()[0][1]-plt.yticks()[0][0]) / 2.0 ))
    
    #remove axis border
    for child in ax.get_children():
        if isinstance(child, matplotlib.spines.Spine):
            child.set_alpha(0)
       
    #restyle the tick lines
    for line in ax.get_xticklines() + ax.get_yticklines():
        line.set_markersize(5)
        line.set_color("gray")
        line.set_markeredgewidth(1.4)
    
    #remove the minor tick lines    
    for line in ax.xaxis.get_ticklines(minor=True) + ax.yaxis.get_ticklines(minor=True):
        line.set_markersize(0)
    
    #only show bottom left ticks, pointing out of axis
    rcParams['xtick.direction'] = 'out'
    rcParams['ytick.direction'] = 'out'
    ax.xaxis.set_ticks_position('bottom')
    ax.yaxis.set_ticks_position('left')
    
    
    if ax.legend_ != None:
        lg = ax.legend_
        lg.get_frame().set_linewidth(0)
        lg.get_frame().set_alpha(0.5)
        
        
def rhist(ax, data, **keywords):
    """Creates a histogram with default style parameters to look like ggplot2
    Is equivalent to calling ax.hist and accepts the same keyword parameters.
    If style parameters are explicitly defined, they will not be overwritten
    """
    
    defaults = {
                'facecolor' : '0.3',
                'edgecolor' : '0.28',
                'linewidth' : '1',
                'bins' : 100
                }
    
    for k, v in defaults.items():
        if k not in keywords: keywords[k] = v
    
    return ax.hist(data, **keywords)


def rbox(ax, data, **keywords):
    """Creates a ggplot2 style boxplot, is eqivalent to calling ax.boxplot with the following additions:
    
    Keyword arguments:
    colors -- array-like collection of colours for box fills
    names -- array-like collection of box names which are passed on as tick labels

    """

    hasColors = 'colors' in keywords
    if hasColors:
        colors = keywords['colors']
        keywords.pop('colors')
        
    if 'names' in keywords:
        ax.tickNames = plt.setp(ax, xticklabels=keywords['names'] )
        keywords.pop('names')
    
    bp = ax.boxplot(data, **keywords)
    pylab.setp(bp['boxes'], color='black')
    pylab.setp(bp['whiskers'], color='black', linestyle = 'solid')
    pylab.setp(bp['fliers'], color='black', alpha = 0.9, marker= 'o', markersize = 3)
    pylab.setp(bp['medians'], color='black')
    
    numBoxes = len(data)
    for i in range(numBoxes):
        box = bp['boxes'][i]
        boxX = []
        boxY = []
        for j in range(5):
          boxX.append(box.get_xdata()[j])
          boxY.append(box.get_ydata()[j])
        boxCoords = zip(boxX,boxY)
        
        if hasColors:
            boxPolygon = Polygon(boxCoords, facecolor = colors[i % len(colors)])
        else:
            boxPolygon = Polygon(boxCoords, facecolor = '0.95')
            
        ax.add_patch(boxPolygon)
    return bp

if __name__=='__main__':
    pass
    #ensure_dir('../data/DepthMap/test/')
