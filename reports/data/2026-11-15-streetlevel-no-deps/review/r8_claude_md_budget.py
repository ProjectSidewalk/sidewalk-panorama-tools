"""Review step 8: the exact CLAUDE.md edit proposed in 237-plan-final.md, measured against master's file.

    python -I <this file> <path to master's CLAUDE.md>
Prints the before/after character counts (the ratchet is `< 30,000`, tests/test_docs.py) and fails if any
old string is not found exactly once, or if the edit is not net <= 0.
"""
import sys

NL = chr(10)
EDITS = [
    ("pip3 install -r requirements.txt -c constraints.txt   # the production box's pip freeze (#167); "
     "drop -c on a Python with no wheel for a pin (3.14)" + NL,
     "pip3 install -r requirements.txt -c constraints.txt   # box's freeze (#167); no -c on 3.14" + NL
     + "pip3 install --no-deps -r requirements-nodeps.txt -c constraints.txt   # #237" + NL),
    ("pip3 install -r requirements.txt -r requirements-dev.txt -c constraints.txt" + NL,
     "pip3 install -r requirements.txt -r requirements-dev.txt -c constraints.txt" + NL
     + "pip3 install --no-deps -r requirements-nodeps.txt -c constraints.txt" + NL),
    ("CI (`.github/workflows/tests.yml`) runs the suite on Ubuntu 22.04 / Python 3.10, the production baseline, "
     "against `constraints.txt` (the box's `pip freeze`, #167). `tests-latest.yml` runs it weekly with no "
     "constraints and never on a PR, to show drift. A requirement and its pin change together "
     "(`tests/test_constraints.py` checks this). The refresh procedure is in `docs/ops.md` under "
     "\"Refreshing `constraints.txt`\". There is no linter configured.",
     "CI (`tests.yml`) runs it on Ubuntu 22.04 / Python 3.10, the production baseline, pinned by "
     "`constraints.txt` (the box's `pip freeze`, #167) and `constraints-ci.txt`; weekly `tests-latest.yml` runs "
     "unpinned, gating nothing. `tests/test_constraints.py` keeps requirements and pins in step; refresh: "
     "`docs/ops.md`, \"Refreshing `constraints.txt`\". No linter."),
]


def main(path):
    with open(path, encoding='utf-8') as f:
        text = f.read()
    before = len(text)
    for old, new in EDITS:
        assert text.count(old) == 1, 'not found exactly once: %r' % old[:60]
        text = text.replace(old, new)
        print('edit: %+d characters' % (len(new) - len(old)))
    print('CLAUDE.md: %d -> %d characters (net %+d; ratchet < 30000)' % (before, len(text), len(text) - before))
    return 0 if len(text) <= before else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
