"""Fetch the era-replay study corpus: /v3/api/rawLabels?filetype=csv for the six study cities,
into the gitignored reports/scripts/.cache/rawlabels/. Skips files that already exist (delete one
to re-fetch it). rawLabels is a moving target — labels accrue and gsv_data refreshes — so a fresh
fetch will NOT reproduce the committed summary bit-for-bit; the committed
reports/data/2026-08-09-era-replay-summary.json corresponds to the 2026-08-09 fetch.

The six era-replay cities: three whose deployments span the 2021-01-01 legacy boundary (seattle-wa,
cdmx, newberg-or) and three that are mid/post-179-heavy (columbus-oh, amsterdam, oradell-nj), mixing
large/small and US/non-US imagery. The record-staleness study added teaneck-nj and chicago-il, the
homes of SidewalkWebpage#4842's two example labels (14955, 30652).

    python reports/scripts/fetch_rawlabels.py
"""

import argparse
import json
import os
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


def all_cities():
    """Every deployment from the public cities API (55 at last count), keyed by its city_id —
    the same ids the study-corpus dict uses. The record bug lived in the shared client, so the
    all-cities sweep is how 'fix it everywhere' gets measured."""
    with urllib.request.urlopen(CITIES_API, timeout=30) as r:
        payload = json.load(r)
    cities = payload if isinstance(payload, list) else payload['cities']
    return {c['city_id']: c['url'] for c in cities}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--all', action='store_true',
                    help='fetch every deployment from the cities API instead of the six-city '
                         'era-replay corpus + teaneck/chicago')
    args = ap.parse_args()
    cities = all_cities() if args.all else CITIES

    os.makedirs(DEST, exist_ok=True)
    for city, base in cities.items():
        path = os.path.join(DEST, f'{city}.csv')
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


if __name__ == '__main__':
    main()
