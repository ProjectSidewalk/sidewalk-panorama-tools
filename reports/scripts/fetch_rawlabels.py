"""Fetch the era-replay study corpus: /v3/api/rawLabels?filetype=csv for the six study cities,
into the gitignored reports/scripts/.cache/rawlabels/. Skips files that already exist (delete one
to re-fetch it). rawLabels is a moving target — labels accrue and gsv_data refreshes — so a fresh
fetch will NOT reproduce the committed summary bit-for-bit; the committed
reports/data/2026-08-09-era-replay-summary.json corresponds to the 2026-08-09 fetch.

Two destinations, and they must stay separate: the study corpus lands in .cache/rawlabels/, and
`--all` (every deployment, for the rollout census) lands in .cache/rawlabels-all/. Every study
globs *.csv over a directory, so mixing them silently redefines the corpus behind every committed
artifact — see the DEST_ALL comment.

The six era-replay cities: three whose deployments span the 2021-01-01 legacy boundary (seattle-wa,
cdmx, newberg-or) and three that are mid/post-179-heavy (columbus-oh, amsterdam, oradell-nj), mixing
large/small and US/non-US imagery. The off-target-markers study added teaneck-nj and chicago-il, the
homes of SidewalkWebpage#4842's two example labels (14955, 30652).

    python reports/scripts/fetch_rawlabels.py
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

DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache', 'rawlabels')

# --all gets its own tree, and must keep it. Every study globs *.csv over a directory, so a
# deployment dropped into DEST silently joins the study corpus and moves every committed artifact
# -- and the roster includes Mapillary deployments (richmond-va), which is the same hazard
# fetch to .cache/rawlabels-mapillary/ exists to prevent. There is no re-separating them
# afterwards: the fetcher skips files that already exist, so the pollution is sticky.
DEST_ALL = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache', 'rawlabels-all')

# Per-socket-operation, not total: a multi-gigabyte city is fine, an unresponsive deployment is
# not. §7's all-deployment sweep hit one that never answered.
FETCH_TIMEOUT_SEC = 60


def all_cities():
    """Every deployment from the public cities API (55 at last count), keyed by its city_id —
    the same ids the study-corpus dict uses. The record bug lived in the shared client, so the
    all-cities sweep is how 'fix it everywhere' gets measured."""
    with urllib.request.urlopen(CITIES_API, timeout=30) as r:
        payload = json.load(r)
    cities = payload if isinstance(payload, list) else payload['cities']
    return {c['city_id']: c['url'] for c in cities}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--all', action='store_true',
                    help='fetch every deployment from the cities API into .cache/rawlabels-all/ '
                         'instead of the six-city era-replay corpus + teaneck/chicago into '
                         '.cache/rawlabels/')
    args = ap.parse_args(argv)
    cities = all_cities() if args.all else CITIES
    dest = DEST_ALL if args.all else DEST

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


if __name__ == '__main__':
    main()
