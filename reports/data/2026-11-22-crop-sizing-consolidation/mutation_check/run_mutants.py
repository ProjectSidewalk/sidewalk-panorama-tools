"""Mutation checks for tests/test_crop_sizing_consolidation_report.py (#186). Each mutant is a copy of the study
script with one textual change, written to a temporary directory placed first on PYTHONPATH, so the test imports
it instead of the real one; the checkout is never edited. A mutant is killed when the named tests fail.

    python reports/data/2026-11-22-crop-sizing-consolidation/mutation_check/run_mutants.py .

Output of the 2026-11-22 run: mutants.out.txt beside this file. M13 was first written as `max(..., key=-abs(...))`,
which returns the FIRST of tied items just as `min` does, so it was an equivalent mutant and survived; it is now
written as a scan in reverse order, which is the real "last on a tie".
"""
import os
import shutil
import subprocess
import sys
import tempfile

WT = os.path.abspath(sys.argv[1])
WORK = tempfile.mkdtemp(prefix='mutants-186-')
SRC = os.path.join(WT, 'reports', 'scripts', 'crop_sizing_consolidation.py')
TEST = 'tests/test_crop_sizing_consolidation_report.py'

MUTANTS = [
    ('M1 pitch and roll swapped', "s * ramp['pitch'], s * float(pano_pose.wrap_deg(ramp['roll']))",
     "s * float(pano_pose.wrap_deg(ramp['roll'])), s * ramp['pitch']", 'T_b_below'),
    ('M2 tilt sign flipped', 's = sign * multiplier * beta', 's = -sign * multiplier * beta', 'T_b_below'),
    ('M3 roll not wrapped', "s * float(pano_pose.wrap_deg(ramp['roll']))", "s * float(ramp['roll'])",
     'roll_is_wrapped'),
    ('M4 every configuration sized at r.y', "    if config in ('S-grav', 'Off'):\n        return [modelled_stored_point(r, beta, multiplier=multiplier)[1] for r in ramps]",
     "    if config in ('S-grav', 'Off'):\n        return [r['y'] for r in ramps]", 'sizing_at_g_changes'),
    ('M5 Off sized at r.y', "    if config == 'S-rig':\n        return [r['y'] for r in ramps]",
     "    if config in ('S-rig', 'Off'):\n        return [r['y'] for r in ramps]", 'identical_size_metrics'),
    ('M6 bootstrap resamples ramps', "cols.setdefault((r['city'], r['pano_id']), []).append(j)",
     "cols.setdefault((r['city'], '%s#%d' % (r['pano_id'], j)), []).append(j)", 'resamples_panos'),
    ('M7 only the first ramp of a drawn pano counted', 'W[b, cols[(city, pano_id)]] = count',
     'W[b, cols[(city, pano_id)][0]] = count', 'resamples_panos'),
    ('M8 criterion 2 ignores the cities', 'if favours_first and agree(True) >= min_agree:',
     'if favours_first and agree(True) >= 0:', 'criterion_2'),
    ('M9 a city at 0 agrees', "((c < 0) if want_negative else (c > 0))", "((c <= 0) if want_negative else (c >= 0))",
     'criterion_2'),
    ('M10 percentile rank floored', 'k = np.rint(q / 100.0 * (total - 1))', 'k = np.floor(q / 100.0 * (total - 1))',
     'expanded_list'),
    ('M11 sd with ddof 0', 'var = (W * (x[None, :] - mean[:, None]) ** 2).sum(1) / (total - 1)',
     'var = (W * (x[None, :] - mean[:, None]) ** 2).sum(1) / total', 'unit_weights'),
    ('M12 leave-one-out keeps the held-out city', "others = [r for r in ramps if r['city'] != held_out]",
     'others = list(ramps)', 'other_cities_only'),
    ('M13 last grid value on a tie', 'return grid[min(range(len(grid)), key=lambda i: abs(p50s[i] - target))]',
     'return grid[min(reversed(range(len(grid))), key=lambda i: abs(p50s[i] - target))]', 'first_on_a_tie'),
    ('M14 power reads the gold apron', "apron = (grav_w if truth == 'g' else rig_w) * fill0 * np.exp(sigma * z)",
     "apron = np.array([r['box_w'] for r in ramps])", 'without_apron_extents'),
    ('M15 tilt verdict ignores a contrary metric', "if 'first' in verdicts and 'second' not in verdicts:",
     "if 'first' in verdicts:", 'combines_the_two_metrics'),
    ('M16 v2 scale ignored', "deg = CropRunner.elevation_px_to_deg(CropRunner.predict_crop_size(size_y, pano_h) * k, pano_h)",
     "deg = CropRunner.elevation_px_to_deg(CropRunner.predict_crop_size(size_y, pano_h) * 2.5, pano_h)",
     'scale_multiplies'),
    ('M17 geo-v1.5 near clamp 1 m', 'GEO_D_MIN_M, GEO_D_MAX_M = 2.0, 40.0', 'GEO_D_MIN_M, GEO_D_MAX_M = 1.0, 40.0',
     'square_rules'),
    ('M18 square window not shifted at the pole', 'top = max(0, min(ideal_top, pano_h - size))', 'top = ideal_top',
     'square_window'),
    ('M19 S-grav centred on g', "centre = modelled_stored_point(r, beta, multiplier=multiplier) if config == 'Off' else None",
     "centre = modelled_stored_point(r, beta, multiplier=multiplier) if config in ('Off', 'S-grav') else None",
     'centres_on_the_modelled_point'),
    # Review 1 of PR #254 (finding 6): the reviewer's 17 mutants, each strings adapted to the helpers the fix round
    # factored out, each run against SYNTHETIC tests only (never TestTheArtifactRegenerates). Before the fix round
    # N1, N1b, N2, N2b and N21 survived the whole file, and N3-N5, N7-N9, N11, N16, N20 and N22 died only to the
    # regeneration test.
    ('N1 tilt arm needs one GSV city', 'metrics=TILT_METRICS, min_agree=2):', 'metrics=TILT_METRICS, min_agree=1):',
     'need_both_gsv'),
    ('N1b tilt arm ignores the cities', 'metrics=TILT_METRICS, min_agree=2):', 'metrics=TILT_METRICS, min_agree=0):',
     'need_both_gsv'),
    ('N2 criterion 2 two of four at the call site', "list(cities.values()), better, 3)",
     "list(cities.values()), better, 2)", 'three_of_four'),
    ('N2b criterion 2 four of four at the call site', "list(cities.values()), better, 3)",
     "list(cities.values()), better, 4)", 'three_of_four'),
    ('N3 holds if any fold agrees', 'return all(d is not None and d != 0 and (d > 0) == (pooled > 0)',
     'return any(d is not None and d != 0 and (d > 0) == (pooled > 0)', 'boundaries or three_of_four'),
    ('N4 folds read at the band-centre constants',
     "pair_constants[held] = {'v2': pm_v2, 'v3': rows['v3_match_v2'], 'A': rows['A_match_v2']}",
     "pair_constants[held] = {'v2': pm_v2, 'v3': rows['v3_band'], 'A': rows['A_band']}", 'reads_its_rows'),
    ('N5 floor read on the lower end', 'return clearing_interval[1] is not None and clearing_interval[1] < CLEARING_FLOOR',
     'return clearing_interval[0] is not None and clearing_interval[0] < CLEARING_FLOOR', 'boundaries'),
    ('N6 power truth drawn at r.y', "apron = (grav_w if truth == 'g' else rig_w) * fill0",
     "apron = (rig_w if truth == 'g' else rig_w) * fill0", 'power_rises or truth_can_be'),
    ('N7 power verdict needs one city', 'DISCRIMINATING[metric], len(cities))', 'DISCRIMINATING[metric], 1)',
     'need_both_gsv'),
    ('N8 S-neg not negated', 'return [modelled_stored_point(r, beta, sign=-1.0, multiplier=multiplier)[1] for r in ramps]',
     'return [modelled_stored_point(r, beta, sign=1.0, multiplier=multiplier)[1] for r in ramps]', 's_neg_is'),
    ('N9 weighted R2 drops the weights from sxx',
     'sxy, sxx, syy = (W * dx * dy).sum(1), (W * dx * dx).sum(1), (W * dy * dy).sum(1)',
     'sxy, sxx, syy = (W * dx * dy).sum(1), (dx * dx).sum(1) * W.mean(1), (W * dy * dy).sum(1)', 'with_counts'),
    ('N11 criterion 6 compares S-rig minus S-grav', "main = compare_configs(gsv_ramps, W_gsv, boxes['S-grav'], boxes['S-rig'])",
     "main = compare_configs(gsv_ramps, W_gsv, boxes['S-rig'], boxes['S-grav'])", 'follow_g_y'),
    ('N16 replicate target is the pooled p50', '    targets_v2 = weighted_percentile(v2_fills, W, 50)\n    targets_band',
     '    targets_v2 = np.full(len(W), v2_fill_p50(ramps))\n    targets_band', 'own_v2_p50'),
    ('N18 report tables name the losing side',
     "return {'first': '%s better' % first, 'second': '%s better' % second}.get(verdict, 'not distinguishable')",
     "return {'first': '%s better' % second, 'second': '%s better' % first}.get(verdict, 'not distinguishable')",
     'every_table'),
    ('N20 MDE takes the largest multiplier', "'mde_multiplier': min(reached) if reached else None",
     "'mde_multiplier': max(reached) if reached else None", 'smallest_multiplier'),
    ('N21 per city needs only the provider difference',
     "return bool(band_block['mapillary_minus_gsv']['differs'] and band_block['annapolis_minus_richmond']['differs'])",
     "return bool(band_block['mapillary_minus_gsv']['differs'] or band_block['annapolis_minus_richmond']['differs'])",
     'boundaries'),
    ('N22 in band reads only the lower edge', 'return BAND[0] <= fill_p50 <= BAND[1]', 'return BAND[0] <= fill_p50',
     'boundaries'),
]

results = []
for name, old, new, k in MUTANTS:
    with open(SRC, encoding='utf-8') as f:
        text = f.read()
    assert text.count(old) == 1, (name, text.count(old))
    d = os.path.join(WORK, 'm')
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    with open(os.path.join(d, 'crop_sizing_consolidation.py'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(text.replace(old, new))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([d, os.path.join(WT, 'reports', 'scripts'), WT]),
               PYTHONDONTWRITEBYTECODE='1')
    proc = subprocess.run([sys.executable, '-m', 'pytest', TEST, '-q', '-p', 'no:cacheprovider', '-k', k],
                          cwd=WT, env=env, capture_output=True, text=True)
    tail = [l for l in proc.stdout.splitlines() if 'passed' in l or 'failed' in l or 'error' in l][-1:]
    killed = proc.returncode != 0
    results.append((name, k, killed, tail[0] if tail else proc.stdout[-200:]))
    print('%-48s %-32s %s  %s' % (name, k, 'KILLED' if killed else 'SURVIVED', tail[0] if tail else ''), flush=True)
shutil.rmtree(WORK, ignore_errors=True)
print('killed %d of %d' % (sum(r[2] for r in results), len(results)))
