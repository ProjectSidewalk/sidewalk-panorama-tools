"""Fetch the desk-study corpora: /v3/api/rawLabels?filetype=csv, into the gitignored
reports/scripts/.cache/. Skips files that already exist (delete one to re-fetch it). rawLabels is a
moving target — labels accrue and gsv_data refreshes — so a fresh fetch will NOT reproduce a committed
summary bit-for-bit; each committed artifact records the fetch date it corresponds to.

Two corpora, two directories:

* **`.cache/rawlabels/` — the six GSV cities.** Three whose deployments span the 2021-01-01 legacy
  boundary (seattle-wa, cdmx, newberg-or) and three that are mid/post-179-heavy (columbus-oh,
  amsterdam, oradell-nj), mixing large/small and US/non-US imagery. This is the corpus the
  pre-registration's §3 draws from.
* **`.cache/rawlabels-mapillary/` — Mapillary-sourced deployments** (richmond). Separate because the
  study scripts glob a directory, so mixing them would silently redefine "the six cities".

    python reports/scripts/fetch_rawlabels.py
"""

import os
import sys
import urllib.request

CITIES = {
    'seattle-wa': 'https://sidewalk-sea.cs.washington.edu',
    'columbus-oh': 'https://sidewalk-columbus.cs.washington.edu',
    'cdmx': 'https://sidewalk-cdmx.cs.washington.edu',
    'newberg-or': 'https://sidewalk-newberg.cs.washington.edu',
    'amsterdam': 'https://sidewalk-amsterdam.cs.washington.edu',
    'oradell-nj': 'https://sidewalk-oradell.cs.washington.edu',
}

# Mapillary-sourced deployments, cached to a SEPARATE directory. Not a stylistic choice: every study
# script takes a directory and globs '*.csv' over it, so a Mapillary city dropped into the six-city
# cache would silently join the GSV corpus and move every committed artifact. Keeping the corpora in
# different directories is what keeps "the six cities" meaning the six cities.
MAPILLARY_CITIES = {
    'richmond': 'https://sidewalk-richmond.cs.washington.edu',
}

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')
DEST = os.path.join(_CACHE, 'rawlabels')
MAPILLARY_DEST = os.path.join(_CACHE, 'rawlabels-mapillary')


def fetch(cities, dest):
    os.makedirs(dest, exist_ok=True)
    for city, base in cities.items():
        path = os.path.join(dest, f'{city}.csv')
        if os.path.exists(path):
            print(f'{city}: cached ({os.path.getsize(path):,} bytes)')
            continue
        url = f'{base}/v3/api/rawLabels?filetype=csv'
        print(f'{city}: fetching {url}', flush=True)
        tmp = path + '.part'
        try:
            urllib.request.urlretrieve(url, tmp)
            os.replace(tmp, path)
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            print(f'{city}: FAILED ({e})', file=sys.stderr)
            continue
        print(f'{city}: {os.path.getsize(path):,} bytes')


def main():
    fetch(CITIES, DEST)
    fetch(MAPILLARY_CITIES, MAPILLARY_DEST)


if __name__ == '__main__':
    main()
