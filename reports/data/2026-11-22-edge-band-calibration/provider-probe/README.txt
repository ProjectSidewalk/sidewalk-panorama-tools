The provider check on the 18 class-U panoramas (#179, decision 7), run on 2026-11-24 by the orchestrating
agent (claude-fable-5-2) at the project owner's request, after the owner's look found all 18 to be missing
imagery. Read-only requests to the GSV tile endpoint, one at a time, 1.5 s apart; nothing on the store was read
or written.

probe.csv      72 tile requests made by probe_tiles.py: for each of the 18 panos in ../unresolved.csv, three
               tiles of the last grid row (left, middle, right) and one control tile from the row above, at the
               frame's zoom (5 for 13312x6656, 3 for 3328x1664).
control.csv    The positive control: 8 clean (no band) 13312x6656 washington-dc panos drawn at random (seed 1)
               from the committed ledger, a mid-frame tile (y=6) and a bottom-row tile (y=12) each. TRANSCRIBED
               from the probe run's printed output into this CSV, not written by a script.
probe_tiles.py The script that made probe.csv, as run.

The tile bodies are not committed: all 72 for the 18 panos are one identical 1,184-byte, 512x512, 100% exact-black
tile (sha256 59f24b4ce6eed9b24b3440439330a5fd2fe431f13d575ab6a7701b407bc14221), which is the endpoint's answer for a pano with no imagery.
