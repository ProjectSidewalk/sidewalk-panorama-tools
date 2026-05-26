#!/bin/bash
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn [options]
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn user@host:/remote/path port [options]
# Options: --all-panos, --attempt-depth, --max-runtime MINUTES

mkdir -p /tmp/download_dest
if [ -f /app/id_rsa ]; then
    chmod 600 /app/id_rsa
fi

# Parse optional parameters at the end
all_panos=""
attempt_depth=""
max_runtime=""

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
            # Check for --max-runtime VALUE (value is last, flag is second-to-last)
            if [[ $# -ge 2 && "${@: -2:1}" == "--max-runtime" ]]; then
                max_runtime="--max-runtime ${@: -1}"
                set -- "${@:1:$(($#-2))}"
            else
                break
            fi
            ;;
    esac
done

# If one param, just download to /tmp. If three params, this means a host and port has been supplied.
if [ $# -eq 1 ]; then
    python3 DownloadRunner.py $1 /tmp/download_dest $all_panos $max_runtime
elif [ $# -eq 3 ]; then
    echo "Mounting $2 port $3 for $1"
    sshfs -o IdentityFile=/app/id_rsa,StrictHostKeyChecking=no $2 /tmp/download_dest -p $3 && python3 DownloadRunner.py $1 /tmp/download_dest $all_panos $max_runtime; umount /tmp/download_dest
else
    echo "Usage:"
    echo "  ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn [options]"
    echo "  ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn user@host:/remote/path port [options]"
    echo "Options: --all-panos, --attempt-depth, --max-runtime MINUTES"
fi
