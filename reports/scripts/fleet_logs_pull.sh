#!/bin/bash
# Stream the pano store's run logs to stdout as a base64 tar.gz, for reports/scripts/fleet_thresholds.py pack.
#
#   bash fleet_logs_pull.sh <store-root> > payload.txt
#   sed -n '/BEGIN-PAYLOAD/,/END-PAYLOAD/p' payload.txt | grep -v PAYLOAD | base64 -d | tar xzf - -C <raw-dir>
#
# Read-only, and writes nothing on the host it runs on: the archive goes to stdout, so it can be run through
# a remote shell that only allows a self-removing script. <store-root> is the directory holding .pano-store
# and one directory per city (the queue's --store-root; see docs/ops.md). It has no default on purpose.
# Takes every city's log.csv and scrape.log*, the queue's scrape_queue.log*, and washington-dc's image ledger.
set -u
B=${1:?usage: fleet_logs_pull.sh <store-root>}
cd "$B" || { echo "NO STORE"; exit 3; }
LIST=$( { ls scrape_queue.log* 2>/dev/null
          for c in */; do c=${c%/}; [ -f "$c/log.csv" ] || continue; ls "$c"/log.csv "$c"/scrape.log* 2>/dev/null; done
          echo washington-dc/pano_id_log.csv; } )
echo "BEGIN-PAYLOAD"
tar czf - $LIST 2>/dev/null | base64
echo "END-PAYLOAD"
