The production sweep's own outputs, one folder per city: black_bands.csv.gz (the ledger, gzipped
unchanged), black_band_worklist.csv, stdout.txt (the tool's stdout with the farm, store and output
roots rewritten to <farm>, <store-root> and <out>), and done.json when the city's frozen list was
swept to the end.

In a sampled city (design "sample" in ../frame.json) the work-list lists the SAMPLE's panos only,
not the city's.

Session main1 (aborted 2026-11-22 15:12:52 Pacific by the 3x rule, see ../abort-main1.json) left
chicago-il at 772 of 2,785 frozen ids and knox-oh at 1,902 of 2,624. Session main2 (2026-11-23, one reader)
resumed both from the same farms, without a redraw, and completed them: every city now has done.json.

The dispatcher sessions' own logs are ../run-<session>.log.txt: the host's run-<session>.log, renamed only
so the repository's *.log ignore rule does not drop them.
