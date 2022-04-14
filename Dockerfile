FROM ubuntu:20.04
ARG DEBIAN_FRONTEND=noninteractive
COPY . /app
WORKDIR /app
RUN apt-get update
RUN apt-get install -y sshfs libfreetype6-dev libxft-dev python-dev libjpeg8-dev libblas-dev liblapack-dev libatlas-base-dev gfortran python3 python3-pip
RUN pip3 install -r requirements.txt
ENTRYPOINT ["./DownloadRunnerDockerEntrypoint.sh"]
CMD []
