"""Fetch the desk-study corpora: /v3/api/rawLabels?filetype=csv, into the gitignored
reports/scripts/.cache/. Skips files that already exist (delete one to re-fetch it). rawLabels is a
moving target — labels accrue and gsv_data refreshes — so a fresh fetch will NOT reproduce a committed
summary bit-for-bit; each committed artifact records the fetch date it corresponds to.

**Three destinations, and they must stay separate.** Every study script takes a directory and globs
`*.csv` over it, so a file landing in the wrong one silently redefines the corpus behind every
committed artifact rather than failing:

* **`.cache/rawlabels/` — the GSV study corpus.** Three deployments spanning the 2021-01-01 legacy
  boundary (seattle-wa, cdmx, newberg-or) and three that are mid/post-179-heavy (columbus-oh,
  amsterdam, oradell-nj), mixing large/small and US/non-US imagery; this is the corpus the
  pre-registration's §3 draws from. The off-target-markers study added teaneck-nj and chicago-il,
  the homes of SidewalkWebpage#4842's two example labels (14955, 30652).
* **`.cache/rawlabels-mapillary/` — Mapillary-sourced deployments** (richmond). Separate because the
  census machinery treats these as a different rig, and because dropping one into the GSV cache
  would silently move every committed six-city number.
* **`.cache/rawlabels-all/` — every deployment**, for the rollout census (`--all`). The roster
  includes Mapillary deployments, so this is the same hazard as above at 54x the scale.

    python reports/scripts/fetch_rawlabels.py            # the GSV corpus + the Mapillary corpus
    python reports/scripts/fetch_rawlabels.py --all      # every deployment, into rawlabels-all/
"""

import argparse
import json
import os
import shutil
import sys
import urllib.request

# Any deployment serves the full deployment roster; Seattle is just a stable place to ask.
CITIES_API = 'https://sidewalk-sea.cs.washington.edu/v3/api/cities'

CITIES = {
    'seattle-wa': 'https://sidewalk-sea.cs.washington.edu',
    'columbus-oh': 'https://sidewalk-columbus.cs.washington.edu',
    'cdmx': 'https://sidewalk-cdmx.cs.washington.edu',
    'newberg-or': 'https://sidewalk-newberg.cs.washington.edu',
    'amsterdam': 'https://sidewalk-amsterdam.cs.washington.edu',
    'oradell-nj': 'https://sidewalk-oradell.cs.washington.edu',
    'teaneck-nj': 'https://sidewalk-teaneck.cs.washington.edu',
    'chicago-il': 'https://sidewalk-chicago.cs.washington.edu',
}

# Mapillary-sourced deployments, cached to a SEPARATE directory. Not a stylistic choice: see the
# module docstring — every study globs a directory, so mixing corpora moves committed artifacts.
MAPILLARY_CITIES = {
    'richmond': 'https://sidewalk-richmond.cs.washington.edu',
}

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')
DEST = os.path.join(_CACHE, 'rawlabels')
MAPILLARY_DEST = os.path.join(_CACHE, 'rawlabels-mapillary')

# --all gets its own tree, and must keep it. The roster includes richmond, a Mapillary deployment,
# and there is no re-separating them afterwards: the fetcher skips files that already exist, so the
# pollution is sticky and the only recovery is knowing which of 54 files to delete.
DEST_ALL = os.path.join(_CACHE, 'rawlabels-all')

# Per-socket-operation, not total: a multi-gigabyte city is fine, an unresponsive deployment is
# not. The all-deployment sweep hit one that never answered.
FETCH_TIMEOUT_SEC = 60


def all_cities():
    """Every deployment from the public cities API (55 at last count), keyed by its city_id —
    the same ids the study-corpus dict uses. The record bug lived in the shared client, so the
    all-cities sweep is how 'fix it everywhere' gets measured."""
    with urllib.request.urlopen(CITIES_API, timeout=30) as r:
        payload = json.load(r)
    cities = payload if isinstance(payload, list) else payload['cities']
    return {c['city_id']: c['url'] for c in cities}


def fetch(cities, dest):
    """Fetch each city's rawLabels into `dest`, skipping what is already cached."""
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
            with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SEC) as r:
                with open(tmp, 'wb') as f:
                    shutil.copyfileobj(r, f)
            os.replace(tmp, path)
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            print(f'{city}: FAILED ({e})', file=sys.stderr)
            continue
        print(f'{city}: {os.path.getsize(path):,} bytes')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--all', action='store_true',
                    help='fetch every deployment from the cities API into .cache/rawlabels-all/ '
                         'instead of the study corpora in .cache/rawlabels/ and '
                         '.cache/rawlabels-mapillary/')
    args = ap.parse_args(argv)
    if args.all:
        fetch(all_cities(), DEST_ALL)
        return
    fetch(CITIES, DEST)
    fetch(MAPILLARY_CITIES, MAPILLARY_DEST)


if __name__ == '__main__':
    main()
