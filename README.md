# sidewalk-panorama-tools

## About
This repository contains a set of Python scripts, intended to be used with data from [Project Sidewalk](https://github.com/ProjectSidewalk/SidewalkWebpage). The purpose of these scripts are to create crops of sidewalk accessibility issues/features usable for ML and computer vision applications from Google Streetview Panoramas via crowd-sourced label data from Project Sidewalk. 

The scripts are intended to be run inside a Docker container running Ubuntu 20.04 64-bit. However, one should be able to run these scripts on most Linux distros without the need for Docker, assuming the Python packages listed in `requirements.txt` can be installed. Additional effort would be required to use the downloader on a Mac or Windows machine without Docker.

There are two main scripts of note: [DownloadRunner.py](DownloadRunner.py) and [CropRunner.py](CropRunner.py). Both should be fully functional, but only the downloader is actively in use (a new version is in the works), so we may not notice bugs with the cropper as quickly. More details on both below!

**Note:** At least 2GB RAM is recommended, as these scripts may crash on very low memory systems due to the size of the images processed.

## Downloader
1. [Install  Docker Desktop](https://www.docker.com/get-started).
1. Run `git clone https://github.com/ProjectSidewalk/sidewalk-panorama-tools.git` in the directory where you want to put the code.
1. Create the Docker image
  ```
  docker build --no-cache --pull -t projectsidewalk/scraper:v5 <path-to-pano-tools-repo>
  ```
1. You can then run the downloader using the following command:
  ```
  docker run --cap-add SYS_ADMIN --device=/dev/fuse --security-opt apparmor:unconfined projectsidewalk/scraper:v5 <project-sidewalk-url>
  ```
  Where the `<project-sidewalk-url>` looks like `sidewalk-columbus.cs.washington.edu` if you want data from Columbus. If you visit that URL, you will see a dropdown menu with a list of publicly deployed cities that you can pull data from.
1. Right now the data is stored in a temporary directory in the Docker container. You could set up a shared volume for it, but for now you can just copy the data over using `docker cp <container-id>:/tmp/download_dest/ <local-storage-location>`, where `<local-storage-location>` is the place on your local machine where you want to save the files. You can find the `<container-id>` using `docker ps -a`.

Additional settings can be configured for `DownloadRunner.py` in the configuration file `config.py`. 
* `thread_count` - the number of threads you wish to run in parallel. As this uses asyncio and is an I/O task, the higher the count the faster the operation, but you will need to test what the upper limit is for your own device and network connection.
* `proxies` - if you wish to use a proxy when downloading, update this dictionary with the relevant details, otherwise leave as is and no proxy will be used. 
* `headers` - this is a list of real headers that is used when making requests. You can add to this list, edit it, or leave as is. 

## Cropper

`CropRunner.py` creates crops of the accessibility features from the downloaded GSV panoramas images via label data from Project Sidewalk. The script requires some data about the labels in CSV format. You can set the path to this file using the variable `csv_export_path`. For an example of a valid CSV file, see `samples/labeldata.csv`.

Update the variables at the top of the file with the path to the CSV file, the path to the folder of panoramas retrieved by `DownloadRunner`,
and the path to the save destination. Then run `python3 CropRunner.py`.

**Note** We have noticed some error in the y-position of labels on the panorama. We believe that this either comes from a bug in the GSV API, or it may be there there is some metadata that Google is not providing us. The errors are relatively small and in the y-direction. As of Apr 2023 we are working on an alternative cropper that attempts to correct for these errors, but it is in development. The version here should work pretty well for now though!

## Class Labels Reference

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
| 9 | Crosswalk |
| 10 | Pedestrian Signal |

## Suggested Improvements

* `CroppRunner.py` should use data from our servers instead of a CSV.
* `CropRunner.py` - implement multi core usage when creating crops. Currently runs on a single core, most modern machines
  have more than one core so would give a speed up for cropping 10's of thousands of images and objects.
* Add logic to `progress_check()` function so that it can register if their is a network failure and does not log the pano id as visited and failed.
* Project Sidewalk group to delete old or commented code once they decide it is no longer required (all code which used the previously available XML data).

## Depth Maps
Depth maps are calculated using downloaded metadata from Google Street View. The endpoint being used to gather the needed XML metadata for depth map calculation isn't a publicly supported API endpoint from Google. It has been only sporadically available throughout 2022, and as of Apr 2023, has been unavailable for the past nine months. We continue to include the code to download the XML and decode the depth data in our download scripts on the off chance that the endpoint comes back online at some point.

**Note:** Decoding the depth maps on an OS other than Linux will likely require recompiling the `decode_depthmap` binary for your system using [this source](https://github.com/jianxiongxiao/ProfXkit/blob/master/GoogleMapsScraper/decode_depthmap.cpp).

## Old Code We've Removed
In PR [#24](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/23), we removed some old code. Some was related to our Tohme paper from 2014, some had to do with using depth maps for cropping images. Given that no one seems to be using the Tohme code (those on our team don't even know how it works) and Google has removed access to their depth data API, we removed this code in Apr 2023. We are hoping that this will simplify the repository, making it easier to make use of our newer work, while making it easier to maintain the code that's actually being used.

If any of this code ever needs to be revived, it exists in the git history, and can be found in the PR linked above!
