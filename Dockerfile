FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
COPY . /app
WORKDIR /app
# python3-dev + gcc: streetlevel pins pyfrpc==0.2.13, whose wheel coverage is spotty — with a compiler present
# its sdist still builds.
RUN apt-get update && \
    apt-get install -y --no-install-recommends sshfs python3 python3-pip python3-dev gcc ca-certificates && \
    rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir -r requirements.txt
ENTRYPOINT ["./DownloadRunnerDockerEntrypoint.sh"]
CMD []
