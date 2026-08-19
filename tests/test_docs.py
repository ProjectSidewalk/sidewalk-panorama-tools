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

# Sources named one by one, so a rename fails this test instead of quietly dropping the file out of coverage.
# CLAUDE.md earns its place here for the same reason the Python sources do: it is a pointer document that
# cites docs/ pages by path, and nothing else checks those.
NAMED_SOURCES = [
    'CLAUDE.md',
    'DownloadRunner.py', 'CropRunner.py', 'migrate_depth_artifacts.py', 'config.py',
    os.path.join('downloaders', 'gsv.py'), os.path.join('downloaders', 'mapillary.py'),
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
