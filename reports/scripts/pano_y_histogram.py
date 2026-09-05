"""How many labels fall in the polar bands that `fover` served at half resolution?

    python reports/scripts/pano_y_histogram.py [city-fqdn ...] [--write-worklist]

Answers the open question on #73: whether the panoramas already in the store need re-downloading. Pulls
/adminapi/labels/cvMetadata (public, no auth - the same endpoint CropRunner uses) and bins every label's
pano_y against the measured band edges. Sidewalk labels are ground features, so the BOTTOM band is the one
that matters.

Writes reports/data/2026-08-07-pano-y-histogram.json and reports/figures/2026-08-07-pano-y-histogram.png.
The raw cvMetadata dumps are ~100 MB per city and are NOT committed; they are cached next to this script
(gitignored) so re-runs are cheap.

--write-worklist additionally emits the panorama ids behind the "panoramas to re-fetch" count, as
reports/data/<WORKLIST_DATE>-fover-refetch-worklist-<city>.csv.gz, which refetch_panos.py consumes directly.
That list is the whole reason the re-download question is tractable: affected LABELS are identifiable by
geometry even though affected PANORAMAS are not identifiable by image analysis (findings 6 and 7 of
reports/2026-08-07-cbk-tile-resolution.md), so the work-list can be computed rather than detected.

Band edges come from the full-grid sweeps in tests/fixtures/tiles/fover_band_map.json, and
tests/test_gsv_tile_contract.py asserts the two agree - if the sweep is ever re-captured and the band moves,
that test fails rather than this analysis silently going stale.
"""

import argparse
import csv
import gzip
import json
import os
import sys
from collections import Counter

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.dirname(HERE)
REPO = os.path.dirname(REPORTS)
sys.path.insert(0, HERE)
from studyfmt import display_path  # noqa: E402

CACHE = os.path.join(HERE, '.cache')
DATA = os.path.join(REPORTS, 'data', '2026-08-07-pano-y-histogram.json')
FIGURE = os.path.join(REPORTS, 'figures', '2026-08-07-pano-y-histogram.png')

# Work-lists carry their own date because they are a later artifact than the histogram above and are meant to
# be regenerated when a city's labelling has moved on - the histogram's findings are fixed at the date they
# were measured, a work-list is an instruction to go and do something now.
WORKLIST_DATE = '2026-08-19'

DEFAULT_CITIES = ['sidewalk-seattle.cs.washington.edu', 'sidewalk-columbus.cs.washington.edu']

TILE = 512
# pano height -> (first full-resolution tile row, last full-resolution tile row inclusive, rows in grid).
# Measured, not assumed: see fover_band_map.json.
BAND_ROWS = {8192: (5, 10, 16), 6656: (4, 8, 13)}


def band_edges(pano_height):
    """(top, bottom) pixel bounds of the full-resolution band, or None for a geometry we did not sweep."""
    rows = BAND_ROWS.get(pano_height)
    if rows is None:
        return None
    return rows[0] * TILE, (rows[1] + 1) * TILE


def fetch(city):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, 'cvmetadata_%s.json' % city.split('.')[0])
    if not os.path.exists(path):
        url = 'https://%s/adminapi/labels/cvMetadata' % city
        print('  fetching %s ...' % url)
        response = requests.get(url, stream=True, timeout=(30, 900))
        response.raise_for_status()
        with open(path, 'wb') as f:
            for chunk in response.iter_content(1 << 16):
                f.write(chunk)
    print('  %s (%.1f MB cached)' % (os.path.basename(path), os.path.getsize(path) / 1e6))
    with open(path, encoding='utf8') as f:
        return json.load(f)


def analyse(rows):
    # panos_with_bottom_band_label is keyed by pano_id rather than being a bare set: the count is the
    # histogram's finding, and the keys plus their frame are the work-list refetch_panos.py consumes.
    # len() reads the same either way, so summarise() below is unchanged.
    out = {'labels_binned': 0, 'zones': Counter(), 'rows_past_edge': Counter(),
           'bottom_band_by_label_type': Counter(), 'pano_shapes': Counter(),
           'panos': set(), 'panos_with_bottom_band_label': {},
           'skipped_no_pano_dimensions': 0}
    for r in rows:
        y, height, pano = r.get('pano_y'), r.get('pano_height'), r.get('pano_id')
        if y is None or height is None or pano is None:
            # Mostly third-party photospheres (base64-style ids), which never went through the CBK path.
            out['skipped_no_pano_dimensions'] += 1
            continue
        out['pano_shapes']['%sx%s' % (r.get('pano_width'), height)] += 1
        out['panos'].add(pano)
        edges = band_edges(height)
        if edges is None:
            out['zones']['unswept geometry'] += 1
            continue
        top, bottom = edges
        out['labels_binned'] += 1
        if y >= bottom:
            out['zones']['bottom band (half-res)'] += 1
            entry = out['panos_with_bottom_band_label'].setdefault(
                pano, {'pano_id': pano, 'width': r.get('pano_width'), 'height': height,
                       'band_labels': 0, 'min_pano_y': y})
            entry['band_labels'] += 1
            # The shallowest band label on this panorama - the one whose crop reaches furthest up into the
            # full-resolution band. Carried so a study can stratify by depth into the band without
            # re-pulling 100 MB of cvMetadata, and to give the committed work-list a deterministic order.
            # It is NOT a processing priority: refetch_panos.py shuffles its candidates.
            entry['min_pano_y'] = min(entry['min_pano_y'], y)
            out['rows_past_edge'][(y - bottom) // TILE] += 1
            out['bottom_band_by_label_type'][r.get('label_type_id')] += 1
        elif y < top:
            out['zones']['top band (half-res)'] += 1
        else:
            out['zones']['full resolution'] += 1
    return out


def summarise(city, a):
    binned = max(a['labels_binned'], 1)
    bottom = a['zones']['bottom band (half-res)']
    return {
        'city': city,
        'label_records': sum(a['pano_shapes'].values()) + a['skipped_no_pano_dimensions'],
        'labels_binned': a['labels_binned'],
        'skipped_no_pano_dimensions': a['skipped_no_pano_dimensions'],
        'pano_shapes': dict(a['pano_shapes'].most_common()),
        'zones': dict(a['zones']),
        'pct_full_resolution': round(100.0 * a['zones']['full resolution'] / binned, 2),
        'pct_bottom_band': round(100.0 * bottom / binned, 2),
        'distinct_panos': len(a['panos']),
        'panos_with_bottom_band_label': len(a['panos_with_bottom_band_label']),
        'pct_panos_to_refetch': round(100.0 * len(a['panos_with_bottom_band_label'])
                                      / max(len(a['panos']), 1), 2),
        'bottom_band_rows_past_edge': {str(k): v for k, v in sorted(a['rows_past_edge'].items())},
        'pct_bottom_band_within_one_tile_row_of_the_edge':
            round(100.0 * a['rows_past_edge'][0] / max(bottom, 1), 1),
        'bottom_band_by_label_type': {str(k): v for k, v in a['bottom_band_by_label_type'].most_common()},
    }


def figure(results, per_city_y):
    """Label density against pano_y, with the band edges marked. 16384x8192 panos only - by far the bulk,
    and mixing geometries would put two different band edges on one axis."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print('  (matplotlib not installed; skipping the figure)')
        return

    top, bottom = band_edges(8192)
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for city, ys in per_city_y.items():
        ax.hist(ys, bins=160, range=(0, 8192), histtype='step', linewidth=1.4,
                label='%s (n=%d)' % (city.split('.')[0].replace('sidewalk-', ''), len(ys)))
    ax.axvspan(0, top, color='tab:red', alpha=0.10)
    ax.axvspan(bottom, 8192, color='tab:red', alpha=0.10)
    for edge in (top, bottom):
        ax.axvline(edge, color='tab:red', linewidth=1.1, linestyle='--')
    ceiling = ax.get_ylim()[1]
    ax.text(bottom + 90, ceiling * 0.92, 'half-res below y=%d' % bottom, color='tab:red', fontsize=9)
    # Below the legend, which occupies the top-left corner.
    ax.text(90, ceiling * 0.66, 'half-res above y=%d' % top, color='tab:red', fontsize=9)
    ax.set_xlabel('pano_y  (16384x8192 panoramas; 0 = top of frame, 4096 = horizon)')
    ax.set_ylabel('labels')
    ax.set_title('Label distribution vs the fover half-resolution bands')
    ax.legend(loc='upper left', fontsize=9)
    fig.tight_layout()
    fig.savefig(FIGURE, dpi=120)
    print('  wrote %s' % display_path(FIGURE, REPO))


def city_slug(city):
    """A filename-safe city name from its FQDN: sidewalk-seattle.cs.washington.edu -> seattle.

    Deliberately derived rather than mapped onto the log analyzer's `city_id` (seattle-wa, columbus-oh):
    log_analyzer/cities.csv carries no hostname, so any mapping between the two would be a hand-maintained
    table that goes stale the first time a city is deployed without someone remembering it. A derived slug
    can be wrong about house style but cannot be wrong about which city it came from.
    """
    return city.split('.')[0].replace('sidewalk-', '')


def worklist_path(city):
    return os.path.join(REPORTS, 'data',
                        '%s-fover-refetch-worklist-%s.csv.gz' % (WORKLIST_DATE, city_slug(city)))


def write_worklist(path, entries):
    """The panoramas behind the "would need re-fetching" count, as refetch_panos.py's intake format.

    Gzipped CSV, following the 2026-08-10-repairs-*.csv.gz precedent: ~8k ids per city is too much to read
    in a diff but small enough to commit, and committing it is what makes the pass reproducible - the
    alternative is a work-list that lives in whoever ran the script's home directory.

    Sorted deepest-first by min_pano_y, then by pano_id, so the committed file is deterministic and diffs
    cleanly between regenerations. That is all the order is for. refetch_panos.py shuffles its candidates -
    a stable order would let a persistently failing head block stall every run against its breaker - so a
    pass cut short by a budget did a random slice of this list, not its head, and nothing about processing
    priority should be read into the ordering here.
    """
    ordered = sorted(entries.values(), key=lambda e: (-e['min_pano_y'], e['pano_id']))
    with gzip.open(path, 'wt', newline='', encoding='utf8') as f:
        writer = csv.DictWriter(f, fieldnames=['pano_id', 'width', 'height', 'band_labels', 'min_pano_y'])
        writer.writeheader()
        for entry in ordered:
            writer.writerow(entry)
    return len(ordered)


def build_parser():
    parser = argparse.ArgumentParser(
        description='Bin every label\'s pano_y against the half-resolution bands `fover` produced (#73).')
    parser.add_argument('cities', nargs='*', default=None, metavar='CITY-FQDN',
                        help='Project Sidewalk hostnames. Default: %s.' % ', '.join(DEFAULT_CITIES))
    parser.add_argument('--write-worklist', action='store_true',
                        help='Write the affected panorama ids per city, as '
                             'reports/data/%s-fover-refetch-worklist-<city>.csv.gz, for refetch_panos.py. '
                             'Implies --no-analysis: a work-list is an instruction to act now, while the '
                             'histogram is a measurement dated %s.'
                             % (WORKLIST_DATE, os.path.basename(DATA)[:10]))
    parser.add_argument('--no-analysis', action='store_true',
                        help='Do not rewrite the dated histogram artifact or its figure. The histogram is a '
                             'measurement taken on %s and quoted by date in '
                             'reports/2026-08-07-cbk-tile-resolution.md, so re-running the script months '
                             'later would silently restate a finding under a label saying it was measured '
                             'then. Implied by --write-worklist.'
                             % os.path.basename(DATA)[:10])
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.write_worklist:
        # A work-list run is never also a re-measurement: the two flags were always paired in the docs, and
        # forgetting the second one rewrote a dated artifact with today's data under yesterday's date.
        args.no_analysis = True
    cities = args.cities or DEFAULT_CITIES
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    results, per_city_y = [], {}
    for city in cities:
        print('=== %s' % city)
        rows = fetch(city)
        a = analyse(rows)
        s = summarise(city, a)
        results.append(s)
        per_city_y[city] = [r['pano_y'] for r in rows
                            if r.get('pano_height') == 8192 and r.get('pano_y') is not None]
        print('  %d labels binned | %.2f%% full resolution | %.2f%% bottom band | '
              '%d of %d panos would need re-fetching (%.2f%%)'
              % (s['labels_binned'], s['pct_full_resolution'], s['pct_bottom_band'],
                 s['panos_with_bottom_band_label'], s['distinct_panos'], s['pct_panos_to_refetch']))
        if args.write_worklist:
            path = worklist_path(city)
            n = write_worklist(path, a['panos_with_bottom_band_label'])
            print('  wrote %s (%d panoramas)' % (display_path(path, REPO), n))

    payload = {
        'question': 'Do the panoramas already in the store need re-downloading after the fover fix (#73)?',
        'method': 'Every label from /adminapi/labels/cvMetadata, binned by pano_y against the measured '
                  'full-resolution band. A label is counted as affected if its CENTRE falls in a half-res '
                  'band, which overstates the effect - crops extend upward into the full-resolution band.',
        'band_edges_px': {str(h): list(band_edges(h)) for h in BAND_ROWS},
        'band_source': 'tests/fixtures/tiles/fover_band_map.json (full-grid sweeps)',
        'caveat': 'Records with no pano_width/pano_height are skipped: they are third-party photospheres '
                  '(base64-style ids) that never went through the CBK tile path.',
        'cities': results,
    }
    if args.no_analysis:
        print('\n(--no-analysis: left %s and its figure as measured)' % display_path(DATA, REPO))
        return 0
    with open(DATA, 'w') as f:
        json.dump(payload, f, indent=1)
    print('\nwrote %s' % display_path(DATA, REPO))
    figure(results, per_city_y)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
