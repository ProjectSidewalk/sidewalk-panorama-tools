#!/bin/bash
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn user@host:/remote/path port

mkdir -p /tmp/download_dest
chmod 600 /app/id_rsa

# Parse optional parameters at the end
all_panos=""
attempt_depth=""

# Process arguments from the end
while [[ $# -gt 0 ]]; do
    case "${@: -1}" in
        "--all-panos")
            all_panos="--all-panos"
            set -- "${@:1:$(($#-1))}"
            ;;
        "--attempt-depth")
            attempt_depth="--attempt-depth"
            set -- "${@:1:$(($#-1))}"
            ;;
        *)
            # Not an optional parameter, stop processing
            break
            ;;
    esac
done

# If one param, just download to /tmp. If three params, this means a host and port has been supplied.
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
