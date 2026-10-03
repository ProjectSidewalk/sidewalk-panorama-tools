"""The README was split into docs/ pages, which turns prose into something that can rot silently.

Three failure modes this pins, all of which the split itself could have introduced:

1. **A link points at a file that isn't there** — a page renamed, or a link written from memory.
2. **An anchor points at a heading that isn't there.** These are the easiest to get wrong: an anchor is a
   slug of a heading, so retitling a section breaks every link into it, and nothing about the rendered page
   looks different - the jump just lands at the top. Same-page `#anchor` links count: they rot for exactly
   the same reason, and dropping them (which this test did until the fix below) left the depth page's own
   "read this first" callout unchecked.
3. **A page is orphaned.** A doc nobody links to from the README's documentation map is, for a reader
   arriving at the front door, the same as a doc that does not exist.

It also checks the other direction: prose and code that cite a `docs/*.md` page by path (there are several -
the log.csv column table, the log analyzer's setup, the depth artifact's invariants, and every pointer in
CLAUDE.md) name a file that exists. Those citations are the reason a reader trusts the comment instead of
re-deriving the behaviour.

Anchors are slugged the way GitHub does it: lowercase, drop everything that is not a word character,
whitespace, or hyphen, then turn each remaining whitespace character into a hyphen. Runs of whitespace are
deliberately *not* collapsed, because GitHub does not collapse them either - `A — B` slugs to `a--b`.
"""

import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, 'docs')

# Markdown pages that are part of the documentation set. reports/ has its own index test.
PAGES = ['README.md', 'CONTRIBUTING.md'] + [
    os.path.join('docs', f) for f in sorted(os.listdir(DOCS_DIR)) if f.endswith('.md')
]

# The guidance Claude Code loads: the root file (every session), the path-scoped rules files (when a file
# matching their `paths:` globs is read) and the nested CLAUDE.md files (when a file under their directory
# is read). One list, so every test that scans "the guidance" scans all of it - a rule moved out of the root
# file would otherwise drop out of the docs-path and log.csv-width checks in silence.
RULES_DIR = os.path.join('.claude', 'rules')

# Directories inside a checkout that hold someone else's files, not this checkout's guidance: git's own,
# virtualenvs, and the other checkouts Claude Code creates under .claude/worktrees/. Matched as path
# COMPONENTS relative to the checkout, never as substrings of the absolute path - a checkout that itself sits
# under a `worktrees` or `*.github.io` directory saw no nested CLAUDE.md at all that way (#192 review S1).
_NOT_THIS_CHECKOUT = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache'}


def _rule_files(root):
    """Every .claude/rules/**/*.md, recursively - Claude Code discovers subdirectories too (review S2)."""
    found = []
    for d, dirs, files in os.walk(os.path.join(root, RULES_DIR)):
        dirs.sort()
        found += [os.path.relpath(os.path.join(d, f), root) for f in files if f.endswith('.md')]
    return sorted(found)


def _nested_claude_mds(root):
    found = []
    for d, dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        parts = () if rel == os.curdir else tuple(rel.split(os.sep))
        dirs[:] = [x for x in dirs
                   if x not in _NOT_THIS_CHECKOUT and parts + (x,) != ('.claude', 'worktrees')]
        if parts and 'CLAUDE.md' in files:
            found.append(os.path.join(rel, 'CLAUDE.md'))
    return sorted(found)


RULE_FILES = _rule_files(REPO_ROOT)
NESTED_CLAUDE_MDS = _nested_claude_mds(REPO_ROOT)
GUIDANCE_FILES = ['CLAUDE.md'] + RULE_FILES + NESTED_CLAUDE_MDS

# Sources named one by one, so a rename fails this test instead of quietly dropping the file out of coverage.
# The guidance files earn their place here for the same reason the Python sources do: they are pointer
# documents that cite docs/ pages by path, and nothing else checks those.
NAMED_SOURCES = GUIDANCE_FILES + [
    'DownloadRunner.py', 'CropRunner.py', 'migrate_crop_store.py', 'migrate_depth_artifacts.py', 'config.py',
    os.path.join('downloaders', 'gsv.py'), os.path.join('downloaders', 'mapillary.py'),
    os.path.join('downloaders', 'panoramax.py'),
    os.path.join('downloaders', 'common.py'), os.path.join('log_analyzer', 'analyze.py'),
    os.path.join('assets', 'make_banner.py'),
]

# Everything that may cite a docs page in a comment, a docstring, or a paragraph.
CITING_SOURCES = NAMED_SOURCES + [
    os.path.join('tests', f) for f in sorted(os.listdir(os.path.join(REPO_ROOT, 'tests')))
    if f.endswith('.py')]

FENCE = re.compile(r'^\s*(```|~~~)')
LINK = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
HEADING = re.compile(r'^#{1,6}\s+(.*?)\s*$')
DOCS_PATH_IN_CODE = re.compile(r'docs/[A-Za-z0-9_.-]+\.md')


def _uncode(path):
    """The page's text with fenced code blocks removed - a URL or a `# heading` inside an example is not a
    link and not a heading, and treating them as such is how this kind of test starts lying."""
    out, in_fence = [], False
    with open(os.path.join(REPO_ROOT, path), encoding='utf-8') as f:
        for line in f:
            if FENCE.match(line):
                in_fence = not in_fence
                continue
            if not in_fence:
                out.append(line)
    return out


def slug(heading):
    text = re.sub(r'`', '', heading).strip().lower()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'\s', '-', text)


def _anchors(path):
    return {slug(m.group(1)) for line in _uncode(path) for m in [HEADING.match(line)] if m}


def _links(path):
    """(target, anchor) for every relative link on the page. External links are dropped.

    Two things here are deliberate, and both were bugs first:

    * **The scan runs over the joined text, not line by line.** These pages hard-wrap at ~110 characters, so
      a `[text](target)` whose brackets straddle a newline is an ordinary thing to write - and a per-line
      regex cannot see one, which made every assertion below skip it in silence. (`[^\\]]*` already spans
      newlines, so joining is the whole fix.) A bracket and a paren from two different paragraphs are not a
      link, hence the blank-line guard.
    * **A same-page `#anchor` resolves to this page** rather than being dropped. Those rot exactly like a
      cross-page anchor does, and dropping them left one unchecked on the depth page.
    """
    found = []
    for m in LINK.finditer(''.join(_uncode(path))):
        if '\n\n' in m.group(0):
            continue
        target = ''.join(m.group(1).split())      # a wrapped target carries the newline and its indent
        if target.startswith(('http://', 'https://', 'mailto:')):
            continue
        file_part, _, anchor = target.partition('#')
        found.append((file_part or os.path.basename(path), anchor))
    return found


def _all_links():
    return [(page, target, anchor) for page in PAGES for target, anchor in _links(page)]


def _ids(triples):
    return [f'{page}->{target}' + (f'#{anchor}' if anchor else '') for page, target, anchor in triples]


def test_the_link_scan_finds_links():
    """Guards the guard: a fence-handling or regex change that matched nothing would make every assertion
    below pass over an empty list. Pinned loosely - the point is 'the scan is alive', not a magic number."""
    assert len(PAGES) >= 8, PAGES
    links = _all_links()
    assert len(links) >= 30, f'only {len(links)} relative links found across {len(PAGES)} pages'
    assert any(anchor for _, _, anchor in links), 'no anchored links found - the anchor tests are vacuous'


@pytest.mark.parametrize('link', _all_links(), ids=_ids(_all_links()))
def test_every_relative_link_resolves(link):
    page, target, _ = link
    resolved = os.path.normpath(os.path.join(REPO_ROOT, os.path.dirname(page), target))
    assert os.path.exists(resolved), f'{page} links to {target}, which does not exist'


@pytest.mark.parametrize('link', [l for l in _all_links() if l[2] and l[1].endswith('.md')],
                         ids=_ids([l for l in _all_links() if l[2] and l[1].endswith('.md')]))
def test_every_cross_page_anchor_exists(link):
    page, target, anchor = link
    target_page = os.path.relpath(
        os.path.normpath(os.path.join(REPO_ROOT, os.path.dirname(page), target)), REPO_ROOT)
    available = _anchors(target_page)
    assert anchor in available, (
        f'{page} links to {target}#{anchor}, but {target_page} has no such heading. '
        f'Its anchors are: {sorted(available)}')


def test_the_slugger_matches_githubs_rules():
    """The anchor test is only as good as this function, and every case here is one this repo's own
    headings actually exercise."""
    assert slug('Storage layout') == 'storage-layout'
    assert slug('The `log.csv` columns') == 'the-logcsv-columns'
    assert slug("Being a good citizen of Google's servers") == 'being-a-good-citizen-of-googles-servers'
    assert slug('What the depth product is (and isn\'t)') == 'what-the-depth-product-is-and-isnt'
    assert slug('The Docker image (Aug 2026)') == 'the-docker-image-aug-2026'
    # An em dash is dropped, and the spaces on either side of it each become a hyphen.
    assert slug('Downloader — `DownloadRunner.py`') == 'downloader--downloadrunnerpy'


@pytest.mark.parametrize('doc', [os.path.basename(p) for p in PAGES if p.startswith('docs')])
def test_every_docs_page_is_linked_from_the_readme(doc):
    """A page the front door does not point at is invisible to everyone who did not write it."""
    targets = {target for target, _ in _links('README.md')}
    assert f'docs/{doc}' in targets, f'docs/{doc} is not linked from README.md'


def test_the_ops_log_csv_table_has_one_row_per_field():
    """`docs/ops.md` is now the third copy of the `log.csv` column list, after `LOG_CSV_FIELD_COUNT` and the
    analyzer's `LOG_COLUMNS` — and it is the copy an operator reads while deciding whether a run is broken.

    The other two are pinned to each other by `test_log_analyzer`; this pins the prose to them. `log.csv` is
    positional and headerless, so a table that has drifted by one row misnumbers every field after the drift
    and there is nothing in the file itself to catch it against.
    """
    import DownloadRunner

    with open(os.path.join(REPO_ROOT, 'docs', 'ops.md'), encoding='utf-8') as f:
        numbers = [int(n) for n in re.findall(r'^\|\s*(\d+)\s*\|', f.read(), re.M)]

    assert numbers == list(range(1, DownloadRunner.LOG_CSV_FIELD_COUNT + 1))


# Every place the prose states the CURRENT width of a log.csv row. Deliberately phrase-specific: the pages also
# say "18-field rows" and "an 18-name header" about the rows written before field 19, which are history, not a
# stale count, so a bare "18" is not an error.
LOG_CSV_WIDTH_CLAIMS = (
    r'(\d+)-column `log\.csv`',
    r'One (\d+)-column row per run',
    r'one row of (\d+) positional',
    r'[Pp]arses the (\d+) positional columns',
    r'reads the \[(\d+) positional columns',
    r'(\d+) comma-separated fields',
    r'appends a full (\d+)-field row',
    r'log\.csv` has (\d+) fields',
)


def test_the_prose_states_the_current_log_csv_width():
    """The ops.md table is pinned row by row above; the sentences that state the row's width are not, and
    they are what a reader quotes. Field 19 (#124) moved the width by hand in every one of these sentences
    across CLAUDE.md, the README and docs/, and a missed one would have read exactly like a current one.
    Every pattern must still match somewhere: a reworded sentence that no pattern reads would otherwise retire
    its check silently, and a total-count floor cannot see that once the corpus has more claims than the floor
    (the #187 review reworded one of eleven claims to a wrong count, and a floor of ten passed it)."""
    import DownloadRunner

    pages = GUIDANCE_FILES + PAGES
    found = []
    for page in pages:
        with open(os.path.join(REPO_ROOT, page), encoding='utf-8') as f:
            text = ' '.join(f.read().split())
        for pattern in LOG_CSV_WIDTH_CLAIMS:
            found += [(page, pattern, int(n)) for n in re.findall(pattern, text)]

    unmatched = [p for p in LOG_CSV_WIDTH_CLAIMS if not any(fp == p for _, fp, _ in found)]
    assert not unmatched, f'no page states the log.csv width in these phrasings any more: {unmatched}'
    assert all(n == DownloadRunner.LOG_CSV_FIELD_COUNT for _, _, n in found), found


@pytest.mark.parametrize('source', CITING_SOURCES)
def test_docs_paths_cited_in_code_exist(source):
    # Asserted, not skipped: skipping on a missing file meant a rename silently retired the check instead of
    # failing it, which is the same silent-rot failure this module exists to prevent.
    path = os.path.join(REPO_ROOT, source)
    assert os.path.exists(path), f'{source} is gone or was renamed - update NAMED_SOURCES'
    with open(path, encoding='utf-8') as f:
        text = f.read()
    for cited in sorted(set(DOCS_PATH_IN_CODE.findall(text))):
        assert os.path.exists(os.path.join(REPO_ROOT, cited)), f'{source} cites {cited}, which does not exist'


# Claude Code loads the root CLAUDE.md, every unscoped .claude/rules/*.md file and every @import into every
# session, and warns once that startup set passes 150,000 characters ("CLAUDE.md is over the 150.0k-char
# limit"); past that, it is context nobody asked for. The root file alone reached 158,330 on 2026-09-29. It was
# trimmed to 145,600 and then split: the per-module rules moved into path-scoped rules files and nested
# CLAUDE.md files, which load only when a matching file is read, and the root keeps what applies everywhere.
# The tool's limit is pinned so the startup set can never again grow past it unnoticed; the root file's own
# ratchet is set from its measured size after the split, in .coveragerc's fail_under spirit: raised only by
# the change that earns it, never quietly.
STARTUP_CHAR_LIMIT = 150_000
ROOT_CLAUDE_MD_CHAR_RATCHET = 45_000


def _chars(path):
    with open(os.path.join(REPO_ROOT, path), encoding='utf-8') as f:
        return len(f.read())


def _frontmatter_paths(path):
    """The `paths:` globs of a rules file's frontmatter, or None if it has none (raises ValueError, see below)."""
    with open(os.path.join(REPO_ROOT, path), encoding='utf-8') as f:
        return _parse_frontmatter_paths(f.read())


# One frontmatter value: double-quoted, single-quoted, or bare. A bare one may not start with a character YAML
# gives a meaning to (`*` is an alias, `[`/`{` open a flow collection, ...) and may not hold a quote, a ` #`
# comment or a `: ` mapping. Quoted ones may not hold their own quote (no escapes are understood).
_SCALAR = r'''(?:"([^"\\]*)"|'([^']*)'|([^\s"'#&*!|>%@`{}\[\],?:-][^"'#]*?))'''
_ITEM = re.compile(r'\s*- ' + _SCALAR + r'\s*')
_INLINE = re.compile(_SCALAR)


def _scalar(match):
    value = next(g for g in match.groups() if g is not None)
    if ': ' in value:
        raise ValueError(f'{value!r} would parse as a mapping, not a glob')
    return value


def _parse_frontmatter_paths(text):
    """Parse the one frontmatter shape the rules files use, and refuse everything else (#192 review S3).

    Not a YAML parser, and deliberately not lenient: Claude Code ignores frontmatter that YAML cannot parse and
    then loads the file at startup, so a best-guess reading of a malformed file (an unterminated quote used to
    pass here as a valid glob) reports scoping that does not exist. PyYAML is not a dependency of this repo and
    is not worth becoming one for a docs check, so the subset is: a `paths:` key holding a block list of
    scalars, or the documented comma-separated string. Anything else raises ValueError."""
    lines = [line.rstrip('\r') for line in text.split('\n')]
    if lines[0] != '---':
        return None
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip() == '---'), None)
    if end is None:
        raise ValueError('the frontmatter opens with --- and is never closed')
    body = [line for line in lines[1:end] if line.strip()]
    if not body:
        return None
    key = re.fullmatch(r'paths:(.*)', body[0])
    if not key:
        raise ValueError(f'frontmatter line {body[0]!r}: only a `paths:` key is understood')
    inline = key.group(1).strip()
    if inline:
        match = _INLINE.fullmatch(inline)
        if not match or len(body) > 1:
            raise ValueError(f'`paths: {inline}` is not a quoted or bare comma-separated string')
        globs = [g.strip() for g in _scalar(match).split(',')]
        if not all(globs):
            raise ValueError(f'`paths: {inline}` has an empty entry')
        return globs
    globs = []
    for line in body[1:]:
        match = _ITEM.fullmatch(line)
        if not match:
            raise ValueError(f'frontmatter line {line!r} is not a `  - "glob"` list item')
        globs.append(_scalar(match))
    return globs or None


def _expand_braces(pattern):
    match = re.search(r'\{([^{}]*)\}', pattern)
    if not match:
        return [pattern]
    return [expanded for alternative in match.group(1).split(',')
            for expanded in _expand_braces(pattern[:match.start()] + alternative + pattern[match.end():])]


def _glob_files(pattern, root=REPO_ROOT):
    """The FILES a rules glob matches, repo-relative. Claude Code matches globs against file paths, so a
    pattern that only names a directory loads nothing; it does expand `{a,b}`, which Python's glob does not
    (#192 review N5)."""
    import glob
    found = set()
    for expanded in _expand_braces(pattern):
        found.update(os.path.relpath(m, root) for m in glob.glob(os.path.join(root, expanded), recursive=True)
                     if os.path.isfile(m))
    return sorted(found)


def _loads_at_startup(path):
    """True for a rules file with no `paths:` - and for one whose frontmatter is not understood, because
    Claude Code drops frontmatter YAML rejects and loads the file as if it were unscoped."""
    try:
        return not _frontmatter_paths(path)
    except ValueError:
        return True


def test_the_guidance_set_is_what_the_split_left():
    """Guards the guards below: an empty rules directory would make every per-file test vacuous."""
    assert len(RULE_FILES) >= 6, RULE_FILES
    assert set(NESTED_CLAUDE_MDS) >= {os.path.join('log_analyzer', 'CLAUDE.md'),
                                      os.path.join('reports', 'scripts', 'CLAUDE.md')}, NESTED_CLAUDE_MDS


@pytest.mark.parametrize('rule', RULE_FILES)
def test_every_rules_file_is_path_scoped_and_every_glob_matches_something(rule):
    """A rules file without `paths:` loads into every session - the root file's problem, moved. A glob that
    matches nothing never loads at all, and the module it described is then edited without its rules, which
    reads exactly like the file never existing."""
    try:
        globs = _frontmatter_paths(rule)
    except ValueError as e:
        pytest.fail(f'{rule}: {e}. Claude Code would ignore this frontmatter and load the file at startup')
    assert globs, f'{rule} has no `paths:` frontmatter, so it would load at startup'
    for pattern in globs:
        matches = _glob_files(pattern)
        assert matches, f'{rule}: the glob {pattern!r} matches no file in the repo'


def test_the_startup_guidance_stays_under_claude_codes_limit():
    startup = ['CLAUDE.md'] + [r for r in RULE_FILES if _loads_at_startup(r)]
    total = sum(_chars(p) for p in startup)
    assert total < STARTUP_CHAR_LIMIT, (
        f'the guidance loaded at startup ({startup}) is {total:,} characters, over the '
        f'{STARTUP_CHAR_LIMIT:,} Claude Code warns at. Move detail into a path-scoped rules file or the '
        'docs/ page it belongs to, rather than raising this number.')


def test_the_root_claude_md_stays_small():
    chars = _chars('CLAUDE.md')
    assert chars < ROOT_CLAUDE_MD_CHAR_RATCHET, (
        f'CLAUDE.md is {chars:,} characters, over its {ROOT_CLAUDE_MD_CHAR_RATCHET:,} ratchet. It holds only '
        'what applies everywhere; a module\'s rules belong in its .claude/rules/ file. Raise the ratchet only '
        'in the change that earns it, and say why in this comment.')


# The discovery and the parser above decide what every guidance test sees, so they are tested on their own
# (#192 review S1-S3, N5): each case here is one the first version got wrong while the real tree passed.

def _write_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(str(root), *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)


SCOPED = '---\npaths:\n  - "a.py"\n---\n# a rule\n'


def test_discovery_does_not_depend_on_where_the_checkout_sits(tmp_path):
    """The filter is on the path relative to the checkout. A checkout under `.claude/worktrees/` (where
    Claude Code puts its own), under a `.git/worktrees/` admin dir, or in a `*.github.io` folder must see the
    same files as one at D:/Git - the first version tested substrings of the absolute path and saw nothing."""
    root = str(tmp_path / '.git' / 'worktrees' / 'site.github.io' / 'wt')
    _write_tree(root, {
        'CLAUDE.md': 'root',
        '.claude/rules/a.md': SCOPED,
        'log_analyzer/CLAUDE.md': 'nested',
        'reports/scripts/CLAUDE.md': 'nested',
        # Inside the checkout, these are not this checkout's guidance.
        '.git/hooks/CLAUDE.md': 'not guidance',
        '.venv/lib/CLAUDE.md': 'not guidance',
        '.claude/worktrees/other/CLAUDE.md': 'another checkout',
        '.claude/worktrees/other/log_analyzer/CLAUDE.md': 'another checkout',
        '.claude/worktrees/other/.claude/rules/b.md': SCOPED,
    })
    assert _nested_claude_mds(root) == [os.path.join('log_analyzer', 'CLAUDE.md'),
                                        os.path.join('reports', 'scripts', 'CLAUDE.md')]
    assert _rule_files(root) == [os.path.join('.claude', 'rules', 'a.md')]


def test_rules_files_in_subdirectories_are_discovered(tmp_path):
    """Claude Code discovers .claude/rules/**/*.md recursively. A rules file one directory down escaped every
    check (an unscoped 160k-character one citing a dead docs/ page passed the whole suite)."""
    _write_tree(tmp_path, {'.claude/rules/a.md': SCOPED, '.claude/rules/sub/deeper/b.md': '# unscoped\n',
                           '.claude/rules/notes.txt': 'not a rules file'})
    assert _rule_files(str(tmp_path)) == [os.path.join('.claude', 'rules', 'a.md'),
                                          os.path.join('.claude', 'rules', 'sub', 'deeper', 'b.md')]


@pytest.mark.parametrize('text, expected', [
    ('---\npaths:\n  - "a.py"\n  - \'b/**\'\n  - c/d.py\n---\nbody\n', ['a.py', 'b/**', 'c/d.py']),
    ('---\npaths: "a.py, docs/*.md"\n---\n', ['a.py', 'docs/*.md']),
    ('---\npaths: a.py, tests/x.py\n---\n', ['a.py', 'tests/x.py']),
    ('---\npaths:\n---\n', None),
    ('# no frontmatter\n---\n', None),
], ids=['list', 'quoted-csv', 'bare-csv', 'empty', 'none'])
def test_the_frontmatter_parser_reads_the_forms_claude_code_accepts(text, expected):
    """A YAML list, or the documented comma-separated string. An empty `paths:` is YAML null: no scoping."""
    assert _parse_frontmatter_paths(text) == expected


@pytest.mark.parametrize('text', [
    '---\npaths:\n  - "CropRunner.py\n  - "b.py"\n---\n',
    '---\npaths:\n  - \'a.py\n---\n',
    '---\npaths:\n  - "a"b.py"\n---\n',
    '---\npaths:\n  - "a.py"\n',
    '---\npaths:\n  - *.py\n---\n',
    '---\npaths: ["a.py"]\n---\n',
    '---\npath:\n  - "a.py"\n---\n',
    '---\npaths:\n  - "a.py"\nother: 1\n---\n',
    '---\npaths:\n  - "a.py" # why\n---\n',
    '---\npaths:\n  "a.py"\n---\n',
], ids=['unterminated-double', 'unterminated-single', 'stray-quote', 'never-closed', 'unquoted-alias',
        'flow-list', 'misspelt-key', 'unknown-key', 'trailing-comment', 'not-a-list-item'])
def test_the_frontmatter_parser_refuses_what_it_does_not_understand(text):
    """Claude Code ignores frontmatter YAML cannot parse and loads the file as if it had no `paths:` - at
    startup. The parser must never read such a file as scoped, so anything outside the small subset it fully
    understands is an error rather than a best guess (an unterminated quote used to pass as a valid glob)."""
    with pytest.raises(ValueError):
        _parse_frontmatter_paths(text)


def test_a_rules_file_whose_frontmatter_is_not_understood_counts_as_startup_context(tmp_path):
    _write_tree(tmp_path, {'bad.md': '---\npaths:\n  - "CropRunner.py\n---\n# rule\n'})
    assert _loads_at_startup(str(tmp_path / 'bad.md'))


def test_a_glob_must_match_a_file_and_braces_expand():
    """Claude Code matches a glob against file paths, so a bare directory pattern loads nothing; it does expand
    `{a,b}`, which Python's glob does not."""
    assert _glob_files('reports/plans') == []
    assert os.path.join('tests', 'test_docs.py') in _glob_files('tests/test_docs.{py,md}')
