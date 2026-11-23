The production sweep's own outputs, one folder per city: black_bands.csv.gz (the ledger, gzipped
unchanged), black_band_worklist.csv, stdout.txt (the tool's stdout with the farm, store and output
roots rewritten to <farm>, <store-root> and <out>), and done.json when the city's frozen list was
swept to the end.

In a sampled city (design "sample" in ../frame.json) the work-list lists the SAMPLE's panos only,
not the city's.

PARTIAL as of session main1 (aborted 2026-11-22 15:12:52 Pacific by the 3x rule, see ../abort.json):
chicago-il (772 of 2,785 frozen ids) and knox-oh (1,902 of 2,624) have no done.json. What they hold is
the sorted-id prefix of their frozen lists. Every other city is complete.
