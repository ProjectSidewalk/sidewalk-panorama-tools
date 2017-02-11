# sidewalk-panorama-tools

This repository contains a set of Python scripts, intended to be used with data from [Project Sidewalk](https://github.com/ProjectSidewalk/SidewalkWebpage). They will take
label data pulled from the Sidewalk database, and after downloading the appropriate panoramas from Google Street View, create a folder
full of JPEG crops of these labels. These crops can be used for ML and computer vision applications.

The scripts were written on a Linux system, and specifically tested only on Ubuntu 16.04 64-bit. However, any Linux distro should
work as long as the required python packages listed in `requirements.txt` can be installed. Usage on any other OS will likely require
recompiling the `decode_depthmap` binary for your system using [this source](https://github.com/jianxiongxiao/ProfXkit/blob/master/GoogleMapsScraper/decode_depthmap.cpp).

If running on cloud hosts, note that at least 2GB RAM is recommended as these scripts may crash on very low memory systems.

### Setup (Ubuntu 16.04)
1. Install required prerequisites:
```bash
sudo apt-get install libfreetype6-dev libxft-dev python-dev libjpeg8-dev libblas-dev liblapack-dev libatlas-base-dev gfortran python-tk
```
2. Install python packages
```bash
pip install -r requirements.txt
```
### Usage
`DownloadRunner.py` and `CropRunner.py` are the scripts you should run. `DownloadRunner.py` downloads panorama images, depth data, and metadata
from Google Street View and saves the data to a folder of your choice. `CropRunner.py` creates crops using the downloaded images. Predictably,
`DownloadRunner.py`
should be executed before `CropRunner.py`.

#### To run `DownloadRunner`:

Simply update the destination path variable at the top of the file to specify the save location. Then run `python DownloadRunner.py`.

#### To run `CropRunner`:

`CropRunner` requires some data about the labels in CSV format, which can be exported from the Sidewalk database using the SQL query in
`samples/getFullLabelList.sql`. For an example of a valid CSV file, see `samples/labeldata.csv`.

Update the variables at the top of the file with the path to the CSV file, the path to the folder of panoramas retrieved by DownloadRunner,
and the path to the save destination. Then run `python CropRunner.py`.
