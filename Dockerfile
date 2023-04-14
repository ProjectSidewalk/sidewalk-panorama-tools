FROM ubuntu:20.04
ARG DEBIAN_FRONTEND=noninteractive
COPY . /app
WORKDIR /app
RUN apt-get update
RUN apt-get install -y python3 python3-pip
RUN pip3 install -r requirements.txt
ENTRYPOINT ["./DownloadRunnerDockerEntrypoint.sh"]
CMD []
