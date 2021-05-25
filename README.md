# sidewalk-panorama-tools

This repository contains a set of Python scripts, intended to be used with data from [Project Sidewalk](https://github.com/ProjectSidewalk/SidewalkWebpage). They will take
label data pulled from the Sidewalk database, and after downloading the appropriate panoramas from Google Street View, create a folder
full of JPEG crops of these labels. These crops can be used for ML and computer vision applications.

The scripts were written on a Linux system, and specifically tested only on Ubuntu 20.04 64-bit. However, any Linux distro should
work as long as the required python packages listed in `requirements.txt` can be installed. Usage on any other OS will likely require
recompiling the `decode_depthmap` binary for your system using [this source](https://github.com/jianxiongxiao/ProfXkit/blob/master/GoogleMapsScraper/decode_depthmap.cpp).

If running on cloud hosts, note that at least 2GB RAM is recommended as these scripts may crash on very low memory systems - this is due to the size of the images processed.

### Setup (Ubuntu 20.04)
1. Install required prerequisites:
```bash
sudo apt-get install libfreetype6-dev libxft-dev python-dev libjpeg8-dev libblas-dev liblapack-dev libatlas-base-dev gfortran python-tk
```
2. Install python packages (Repository has been updated to run using Python3):
```bash
pip install -r requirements.txt
```
### Usage

#### Order of execution
1. `DownloadRunner.py`
2. `CropRunner.py`

`DownloadRunner.py` and `CropRunner.py` are the scripts you should run. `DownloadRunner.py` downloads panorama images
from Google Street View and saves the data to a folder of your choice. Previously `DownloadRunner.py` would also download the relevant depth data and metadata from Google Street View, but this is no longer publicly available. As a result you must use load a csv file with this required metadata for this script to run correctly. 


`CropRunner.py` creates crops of the object classes from the downloaded GSV panoramas images. `DownloadRunner.py` 
should be executed before `CropRunner.py`.

#### To run `DownloadRunner`:

Previously DownloadRunner accessed the Project Sidewalk server to download the list of panorama ids. It is recommended to use the csv file contained in the folder. The path to this file should be set in the variable `metadata_csv_path`.

Simply update the destination path variable at the top of the file to specify the save location. Then run `python DownloadRunner.py`. 

#### To run `CropRunner`:

`CropRunner` requires some data about the labels in CSV format. This is also contained in the aforementioned csv file. You can set the path to this file using the variable `csv_export_path`. For an example of a valid CSV file, see `samples/labeldata.csv`.

Update the variables at the top of the file with the path to the CSV file, the path to the folder of panoramas retrieved by DownloadRunner,
and the path to the save destination. Then run `python CropRunner.py`.

#### Configuration File `config.py`

Additional settings can be configured for `DownloadRunner.py` in the configuration file `config.py`. 

* `thread_count` - the number of threads you wish to run in parallel. As this uses asyncio and is an I/O task, the higher the count the faster the operation, but you will need to test what the upper limit is for your own device and network connection.
* `proxies` - if you wish to use a proxy when downloading, update this dictionary with the relevant details, otherwise leave as is and no proxy will be used. 
* `headers` - this is a list of real headers that is used when making requests. You can add to this list, edit it, or leave as is. 

Note that the numbers in the `label_type_id` column correspond to these label types:
| label_type_id  | label type |
| ------------- | ------------- |
| 1 | Curb Ramp |
| 2 | Missing Curb Ramp |
| 3 | Obstacle in a Path |
| 4 | Surface Problem |
| 5 | Other |
| 6 | Can't see the sidewalk |
| 7 | No Sidewalk |
