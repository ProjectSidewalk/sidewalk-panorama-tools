"""Tests for the corpus fetcher.

The fetcher had no tests, and the one behaviour that most needs pinning is *where* it writes:
every study globs `*.csv` over a directory, so a file landing in the wrong one silently changes
the corpus behind committed artifacts rather than failing. CLAUDE.md records this as the reason
Mapillary cities get their own cache directory; the 54-deployment `--all` sweep needs the same
separation, and it pulls richmond-va, a Mapillary deployment, among others.
"""

import io
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'reports', 'scripts'))

import fetch_rawlabels as fr  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect both destinations into tmp_path and stub the network."""
    dest = tmp_path / 'rawlabels'
    dest_all = tmp_path / 'rawlabels-all'
    monkeypatch.setattr(fr, 'DEST', str(dest))
    monkeypatch.setattr(fr, 'DEST_ALL', str(dest_all))

    fetched = []

    def fake_urlopen(url, timeout=None):
        fetched.append(url)
        return io.BytesIO(b'label_id\n1\n')

    monkeypatch.setattr(fr.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setattr(fr, 'all_cities', lambda: {
        'seattle-wa': 'https://sidewalk-sea.cs.washington.edu',
        'richmond-va': 'https://sidewalk-richmond.cs.washington.edu',
        'crowdstudy': 'https://sidewalk-crowdstudy.cs.washington.edu',
    })
    return dest, dest_all, fetched


class TestAllGetsItsOwnDirectory:

    def test_the_two_destinations_are_not_the_same_directory(self):
        """The whole point: an --all sweep must not be able to land in the study corpus."""
        assert fr.DEST != fr.DEST_ALL

    def test_all_writes_outside_the_study_corpus(self, sandbox):
        """--all pulls every deployment, including Mapillary ones. If those land in
        .cache/rawlabels/ then era_replay, click_noise, clamp_census, offaxis_covariate,
        photometa_census and this study all silently analyze 54 cities instead of six."""
        dest, dest_all, _ = sandbox
        fr.main(['--all'])
        assert sorted(os.listdir(dest_all)) == ['crowdstudy.csv', 'richmond-va.csv',
                                                'seattle-wa.csv']
        assert not os.path.exists(dest) or os.listdir(dest) == []

    def test_the_study_corpus_fetch_still_writes_to_dest(self, sandbox):
        """The eight-city default is unchanged."""
        dest, dest_all, _ = sandbox
        fr.main([])
        got = sorted(os.listdir(dest))
        assert got == sorted(f'{c}.csv' for c in fr.CITIES)
        assert not os.path.exists(dest_all) or os.listdir(dest_all) == []

    def test_a_cached_city_in_one_tree_does_not_satisfy_the_other(self, sandbox):
        """The skip-if-exists check must be per-destination, or an --all sweep would be
        considered already done because the eight-city fetch ran earlier."""
        dest, dest_all, fetched = sandbox
        fr.main([])
        fetched.clear()
        fr.main(['--all'])
        assert any('richmond' in u for u in fetched)
        assert any('sidewalk-sea' in u for u in fetched), \
            'seattle was skipped in the --all tree because it existed in the study tree'


class TestFetchDoesNotHangForever:

    def test_the_download_passes_a_timeout(self, tmp_path, monkeypatch):
        """§7 records a deployment that did not respond. With no timeout that is a permanent
        block partway through a multi-gigabyte sweep rather than a logged FAILED line."""
        monkeypatch.setattr(fr, 'DEST', str(tmp_path / 'd'))
        monkeypatch.setattr(fr, 'CITIES', {'x': 'https://example.invalid'})
        seen = {}

        def fake_urlopen(url, timeout=None):
            seen['timeout'] = timeout
            return io.BytesIO(b'label_id\n1\n')

        monkeypatch.setattr(fr.urllib.request, 'urlopen', fake_urlopen)
        fr.main([])
        assert seen.get('timeout') is not None, 'the download was issued with no timeout'
        assert seen['timeout'] > 0
