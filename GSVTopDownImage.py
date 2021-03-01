import GSVImage
import numpy as np
import os

from PIL import Image


class GSVTopDownImage(object):
    width = 1000
    height = 1000

    def __init__(self, path):
        """
        The constructor. This method takes a path to top
        """
        if type(path) != str and len(str) > 0:
            raise ValueError('path should be a string')
        if path[-1] != '/':
            path += '/'

        self.path = path
        self.pano_id = self.path.split('/')[-1]
        self.filename = 'topdown.png'
        self.file_path = self.path + 'images/topdown.png'
        if not os.path.isfile(self.file_path):
            raise ValueError(self.file_path + ' does not exist.')

        return

    def get_image(self):
        """
        This method returns the image array
        """
        return np.asarray(Image.open(self.file_path))

    def show(self, size=False):
        """
        This method shows the topdown.png image
        """
        if os.path.isfile(self.file_path):
            im = Image.open(self.file_path)

            if size and type(size) == tuple:
                im = im.resize(size, Image.ANTIALIAS)
            im.show()
        else:
            raise Exception(self.path + 'images/pano.jpg does not exist')
        return


if __name__ == '__main__':
    print("GSVTopDownImage.py")

    pano_id = '-015tl-_IqAuhn4X2_km6Q'
    gsv_topdown = GSVTopDownImage('../data/GSV/' + pano_id + '/')
