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
RULE_FILES = sorted(os.path.join(RULES_DIR, f)
                    for f in os.listdir(os.path.join(REPO_ROOT, RULES_DIR)) if f.endswith('.md'))
NESTED_CLAUDE_MDS = sorted(os.path.relpath(os.path.join(root, 'CLAUDE.md'), REPO_ROOT)
                           for root, dirs, files in os.walk(REPO_ROOT)
                           if 'CLAUDE.md' in files and os.path.abspath(root) != REPO_ROOT
                           and '.git' not in root and 'worktrees' not in root)
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
    """The `paths:` globs of a rules file's YAML frontmatter, or None if it has no frontmatter/paths.

    Parsed by hand rather than with a YAML library: the format used here is one list under one key, and a
    parser dependency for that would be the first non-test dependency the suite pulls in for a docs check."""
    with open(os.path.join(REPO_ROOT, path), encoding='utf-8') as f:
        lines = f.read().split('\n')
    if not lines or lines[0].strip() != '---':
        return None
    globs, in_paths = [], False
    for line in lines[1:]:
        if line.strip() == '---':
            break
        if line.strip() == 'paths:':
            in_paths = True
            continue
        if in_paths and line.strip().startswith('- '):
            globs.append(line.strip()[2:].strip().strip('"\''))
        elif line.strip() and not line.startswith(' '):
            in_paths = False
    return globs if in_paths or globs else None


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
    import glob
    globs = _frontmatter_paths(rule)
    assert globs, f'{rule} has no `paths:` frontmatter, so it would load at startup'
    for pattern in globs:
        matches = glob.glob(os.path.join(REPO_ROOT, pattern), recursive=True)
        assert matches, f'{rule}: the glob {pattern!r} matches no file in the repo'


def test_the_startup_guidance_stays_under_claude_codes_limit():
    startup = ['CLAUDE.md'] + [r for r in RULE_FILES if not _frontmatter_paths(r)]
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
