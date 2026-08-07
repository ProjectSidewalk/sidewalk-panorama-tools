#!/bin/bash
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn [options]
# ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn user@host:/remote/path port [options]
# Options: --all-panos, --skip-depth, --max-runtime MINUTES, --max-depth-requests N

mkdir -p /tmp/download_dest
if [ -f /app/id_rsa ]; then
    chmod 600 /app/id_rsa
fi

# Parse optional parameters at the end
all_panos=""
skip_depth=""
max_runtime=""
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
# The container's exit status must be DownloadRunner's: cron-level monitoring only sees the exit code, and the
# old `... && python3 ...; umount ...` form reported umount's status, so a crashed scrape (or a mount that
# never came up) exited 0 (#49).
# If one param, just download to /tmp. If three params, this means a host and port has been supplied.
if [ $# -eq 1 ]; then
    # exec makes python3 the container process itself: `docker stop`'s SIGTERM reaches it directly, and its
    # exit code is the container's. Nothing to clean up here - the destination is container-local /tmp.
    exec python3 DownloadRunner.py $1 /tmp/download_dest $all_panos $skip_depth $max_runtime $max_depth_requests
elif [ $# -eq 3 ]; then
    echo "Mounting $2 port $3 for $1"
    if ! sshfs -o IdentityFile=/app/id_rsa,StrictHostKeyChecking=no $2 /tmp/download_dest -p $3; then
        echo "ERROR: sshfs mount of $2 failed; not starting the scrape" >&2
        exit 1
    fi
    # Unmount via a trap so it happens even when the runner crashes. A failed unmount means data may not have
    # been flushed to the store, so it must not read as a clean night: warn on stderr and turn an otherwise-
    # clean exit nonzero - while a failing runner's own, more diagnostic exit code still wins.
    trap 'rc=$?; if ! umount /tmp/download_dest; then echo "WARNING: umount of /tmp/download_dest failed" >&2; [ "$rc" -eq 0 ] && rc=1; fi; exit $rc' EXIT
    # Run the scrape in the background and forward SIGTERM: docker stop TERMs only this script, and a
    # foreground child would be orphaned still writing while the EXIT trap unmounts the store. Forwarded,
    # the runner's own handler turns TERM into a clean SystemExit that writes the log.csv evidence row,
    # and umount runs only after the runner has actually exited.
    python3 DownloadRunner.py $1 /tmp/download_dest $all_panos $skip_depth $max_runtime $max_depth_requests &
    runner_pid=$!
    term_received=""
    trap 'term_received=1; kill -TERM "$runner_pid" 2>/dev/null' TERM
    wait "$runner_pid"
    runner_rc=$?
    if [ -n "$term_received" ]; then
        # The first wait returned because the TERM trap fired, not because the runner exited; wait again
        # (the runner is still unreaped) for its real status.
        wait "$runner_pid"
        runner_rc=$?
    fi
    exit "$runner_rc"
else
    echo "Usage:"
    echo "  ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn [options]"
    echo "  ./DownloadRunnerDockerEntrypoint sidewalk_server_fqdn user@host:/remote/path port [options]"
    echo "Options: --all-panos, --skip-depth, --max-runtime MINUTES, --max-depth-requests N"
    exit 1
fi
