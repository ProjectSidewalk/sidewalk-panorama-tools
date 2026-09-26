Do not open until you have recorded all 48 verdicts.

This folder holds the answer key of the #54 endpoint-C adjudication (`key.json`: which of A/B/C is the
stored, leak and antileak window on every sheet), the salt of the hash in `../key.sha256`, and the
verdicts of any judge who has already finished (the implementing model's preliminary pass is one).
Reading any of it before judging breaks the blind, and nothing can repair that afterwards.

The study plan (section 3.3) said to commit the key only after adjudication. It was
committed early by mistake and is sealed here instead, so the study stays reproducible from the tree.
