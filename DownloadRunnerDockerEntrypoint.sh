#!/bin/bash
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn [options]
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn user@host:/remote/path port [options]
# Options: --all-panos, --skip-depth, --max-runtime MINUTES, --min-depth-runtime MINUTES, --max-depth-requests N

mkdir -p /tmp/download_dest
if [ -f /app/id_rsa ]; then
    chmod 600 /app/id_rsa
fi

# Parse optional parameters at the end
all_panos=""
skip_depth=""
max_runtime=""
min_depth_runtime=""
max_depth_requests=""

# Process arguments from the end
while [[ $# -gt 0 ]]; do
    case "${@: -1}" in
        "--all-panos")
            all_panos="--all-panos"
            set -- "${@:1:$(($#-1))}"
            ;;
        "--skip-depth")
            skip_depth="--skip-depth"
            set -- "${@:1:$(($#-1))}"
            ;;
        "--attempt-depth")
            # Deprecated: depth download is now on by default. Still recognized so invocations passing it don't
            # fall through to the usage-error path below.
            echo "WARNING: --attempt-depth is deprecated; depth download is now on by default (use --skip-depth to disable)"
            set -- "${@:1:$(($#-1))}"
            ;;
        *)
            # Check for --flag VALUE pairs (value is last, flag is second-to-last)
            if [[ $# -ge 2 && "${@: -2:1}" == "--max-runtime" ]]; then
                max_runtime="--max-runtime ${@: -1}"
                set -- "${@:1:$(($#-2))}"
            elif [[ $# -ge 2 && "${@: -2:1}" == "--min-depth-runtime" ]]; then
                min_depth_runtime="--min-depth-runtime ${@: -1}"
                set -- "${@:1:$(($#-2))}"
            elif [[ $# -ge 2 && "${@: -2:1}" == "--max-depth-requests" ]]; then
                max_depth_requests="--max-depth-requests ${@: -1}"
                set -- "${@:1:$(($#-2))}"
            else
                break
            fi
            ;;
    esac
done

# NB: every optional flag parsed above must appear in BOTH DownloadRunner.py invocations below — a flag that is
# parsed but not forwarded is silently ignored.
# If one param, just download to /tmp. If three params, this means a host and port has been supplied.
if [ $# -eq 1 ]; then
    python3 DownloadRunner.py $1 /tmp/download_dest $all_panos $skip_depth $max_runtime $min_depth_runtime $max_depth_requests
elif [ $# -eq 3 ]; then
    echo "Mounting $2 port $3 for $1"
    sshfs -o IdentityFile=/app/id_rsa,StrictHostKeyChecking=no $2 /tmp/download_dest -p $3 && python3 DownloadRunner.py $1 /tmp/download_dest $all_panos $skip_depth $max_runtime $min_depth_runtime $max_depth_requests; umount /tmp/download_dest
else
    echo "Usage:"
    echo "  ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn [options]"
    echo "  ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn user@host:/remote/path port [options]"
    echo "Options: --all-panos, --skip-depth, --max-runtime MINUTES, --min-depth-runtime MINUTES, --max-depth-requests N"
fi
