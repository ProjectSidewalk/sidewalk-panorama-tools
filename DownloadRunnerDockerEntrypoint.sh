#!/bin/bash
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn user@host:/remote/path port

mkdir -p /tmp/download_dest
chmod 600 /app/id_rsa

# Check if last argument is --all-panos
all_panos=""
args=("$@")
if [[ "${@: -1}" == "--all-panos" ]]; then
    all_panos="--all-panos"
    # Remove last argument
    set -- "${@:1:$(($#-1))}"
fi
if [ $# -eq 1 ]; then
    python3 DownloadRunner.py $1 /tmp/download_dest $all_panos
elif [ $# -eq 3 ]; then
    echo "Mounting $2 port $3 for $1"
    sshfs -o IdentityFile=/app/id_rsa,StrictHostKeyChecking=no $2 /tmp/download_dest -p $3 && python3 DownloadRunner.py $1 /tmp/download_dest $all_panos; umount /tmp/download_dest
else
    echo "Usage:"
    echo "  ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn"
    echo "  ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn user@host:/remote/path port"
fi