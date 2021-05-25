# sidewalk-panorama-tools

## 1.0 About
This repository contains a set of Python scripts, intended to be used with data from [Project Sidewalk](https://github.com/ProjectSidewalk/SidewalkWebpage). Previously they took label data pulled from the Sidewalk database. In the new implementation the metadata must be provided by CSV file. After downloading the appropriate panoramas from Google Street View, it will create a folder full of JPEG crops of these labels. These crops can be used for ML and computer vision applications.

The scripts were written on a Linux system, and specifically tested only on Ubuntu 20.04 64-bit. However, any Linux distro should
work as long as the required python packages listed in `requirements.txt` can be installed. 

Previously depth maps were calculated using downloaded metadata from Google Street View. This data is no longer available online, but the method of setting this up on different operating systems has been maintained for reference below: 

* Usage on any other OS will likely require
recompiling the `decode_depthmap` binary for your system using [this source](https://github.com/jianxiongxiao/ProfXkit/blob/master/GoogleMapsScraper/decode_depthmap.cpp).

**NB:** If running on cloud hosts, note that at least 2GB RAM is recommended as these scripts may crash on very low memory systems - this is due to the size of the images processed.

## 2.0 Set-Up and Running 

### 2.1 Setup (Ubuntu 20.04 & Python3)

#### 2.1.1 Ubuntu 20.04 Prerequisites
1. Install required prerequisites:
```bash
sudo apt-get install libfreetype6-dev libxft-dev python-dev libjpeg8-dev libblas-dev liblapack-dev libatlas-base-dev gfortran python-tk
```

#### 2.1.2 Python3 Requirements
2. Install python packages (repository has been updated to run using Python3):
```bash
pip install -r requirements.txt
```

#### 2.1.3 CSV Containing Metadata

To download and crop the GSV images, a csv containing the metadata is required. 
* **TBC**

### 2.2 Usage

#### 2.2.1 Order of execution
1. `DownloadRunner.py`
2. `CropRunner.py`

`DownloadRunner.py` and `CropRunner.py` are the scripts you should run. `DownloadRunner.py` downloads panorama images
from Google Street View and saves the data to a folder of your choice. Previously `DownloadRunner.py` would also download the relevant depth data and metadata from Google Street View, but this is no longer publicly available. As a result you must load a csv file with this required metadata for the script to run correctly. 


`CropRunner.py` creates crops of the object classes from the downloaded GSV panoramas images. `DownloadRunner.py` 
should be executed before `CropRunner.py`.

#### 2.2.2 Running `DownloadRunner.py`

Previously DownloadRunner accessed the Project Sidewalk server to download the list of panorama ids. It is recommended to use the csv file contained in the folder. The path to this file should be set in the variable `metadata_csv_path`.

Simply update the destination path variable at the top of the file to specify the save location. Then run `python DownloadRunner.py`. 

#### 2.2.3 Running `CropRunner.py`

`CropRunner` requires some data about the labels in CSV format. This is also contained in the aforementioned csv file. You can set the path to this file using the variable `csv_export_path`. For an example of a valid CSV file, see `samples/labeldata.csv`.

Update the variables at the top of the file with the path to the CSV file, the path to the folder of panoramas retrieved by `DownloadRunner`,
and the path to the save destination. Then run `python CropRunner.py`.

#### 2.2.4 Configuration File `config.py`

Additional settings can be configured for `DownloadRunner.py` in the configuration file `config.py`. 

* `thread_count` - the number of threads you wish to run in parallel. As this uses asyncio and is an I/O task, the higher the count the faster the operation, but you will need to test what the upper limit is for your own device and network connection.
* `proxies` - if you wish to use a proxy when downloading, update this dictionary with the relevant details, otherwise leave as is and no proxy will be used. 
* `headers` - this is a list of real headers that is used when making requests. You can add to this list, edit it, or leave as is. 

### 2.3 Additional Functions To Be Aware Of

When running `DownloadRunner.py` checks are carried out to see if images have been downloaded already via the filesystem. This can slow things down if each time you re-start `DownloadRunner.py` it believes an image has not yet been downloaded and retries, but this image is not available or has an error. To prevent this additional functionality was added via the functions `check_download_failed_previously` and `progress_check()`. 

These functions simply keep track of which panorama ids have been visited previously, and if the download resulted in success or failure. Be aware while this does speed up the process of multiple runs of DownloadRunner.py (when unable to run in one continuous session) this can cause issues if your internet connection fails while running this script. This is as it may believe it has visited the link and failed, rather than a connection error being the root cause of failure. Logic could be added in the future to discern if it is a network error causing the issue. 

## 3.0 Class Labels

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
