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
# Names reach tar NUL-separated on stdin, never as words on its command line: a store file named
# `scrape.log --checkpoint-action=exec=...` was read as an option, and tar ran it. No --verbatim-files-from:
# --null implies it from GNU tar 1.29, and older ones reject the flag.
echo "BEGIN-PAYLOAD"
{ for f in scrape_queue.log*; do [ -f "$f" ] && printf '%s\0' "$f"; done
  for c in */; do c=${c%/}; [ -f "$c/log.csv" ] || continue
      for f in "$c"/log.csv "$c"/scrape.log*; do [ -f "$f" ] && printf '%s\0' "$f"; done; done
  printf '%s\0' washington-dc/pano_id_log.csv; } | tar czf - --null -T - 2>/dev/null | base64
echo "END-PAYLOAD"
