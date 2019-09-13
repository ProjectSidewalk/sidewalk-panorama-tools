FROM ubuntu:18.04
ARG DEBIAN_FRONTEND=noninteractive
COPY . /app
WORKDIR /app
RUN apt-get update
RUN apt-get install -y python-pip sshfs libfreetype6-dev libxft-dev python-dev libjpeg8-dev libblas-dev liblapack-dev libatlas-base-dev gfortran python-tk
RUN pip install -r requirements.txt
ENTRYPOINT ["./DownloadRunnerDockerEntrypoint.sh"]
CMD []