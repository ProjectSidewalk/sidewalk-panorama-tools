"""Every committed artifact under reports/data/ must be readable by something other than Python.

Python's `json` module accepts the non-standard tokens `NaN`, `Infinity` and `-Infinity` on both
read and write, with no flag needed. That makes a broken artifact completely invisible from inside
this repo: the study scripts write it, the study tests read it back, everything is green, and the
file is rejected by jq, by `JSON.parse`, and by most non-Python readers.

That is not hypothetical here — reports/data/2026-08-09-photometa-census.json shipped with 4,916
bare `NaN` tokens (pre-merge review, 2026-08-10; `df.where(df.notna(), None)` silently coerces the
None back to NaN on float columns). This file is the check that would have caught it, so it runs
over the whole directory rather than over one known-bad artifact.
"""

import glob
import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, 'reports', 'data')

DATA_FILES = sorted(glob.glob(os.path.join(DATA_DIR, '*.json')))


def _reject_non_standard(token):
    raise ValueError(f'non-standard JSON token {token!r}')


def test_the_data_directory_is_not_empty():
    """Guards the guard: a glob that silently matches nothing would make every test below vacuous."""
    assert DATA_FILES, f'no committed JSON found under {DATA_DIR}'


@pytest.mark.parametrize('path', DATA_FILES, ids=[os.path.basename(p) for p in DATA_FILES])
def test_committed_json_is_strict_and_portable(path):
    """Strict RFC-8259: no NaN/Infinity, and it must parse as UTF-8."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    json.loads(text, parse_constant=_reject_non_standard)


@pytest.mark.parametrize('path', DATA_FILES, ids=[os.path.basename(p) for p in DATA_FILES])
def test_committed_json_contains_no_bare_nan_token(path):
    """Belt to the parser's braces, and a far clearer failure message than a parse error when a
    regenerated artifact regresses."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    for token in ('NaN', 'Infinity'):
        assert f': {token}' not in text and f':{token}' not in text, \
            f'{os.path.basename(path)} contains a bare `{token}` value'


def test_the_check_would_fail_on_a_bad_file(tmp_path):
    """Discrimination: the assertions above must actually reject what Python happily writes."""
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'x': float('nan')}), encoding='utf-8')
    text = bad.read_text(encoding='utf-8')
    assert 'NaN' in text, 'json.dump writes the bare token by default — that is the whole problem'
    with pytest.raises(ValueError):
        json.loads(text, parse_constant=_reject_non_standard)
    with pytest.raises(ValueError):
        json.dumps({'x': float('nan')}, allow_nan=False)
