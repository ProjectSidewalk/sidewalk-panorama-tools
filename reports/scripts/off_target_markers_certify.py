"""SidewalkWebpage#4842 Stage 3: the 2026-08-11 census (before) against a post-repair sweep (after), per city.

    python reports/scripts/off_target_markers_certify.py reports/data/2026-08-11-off-target-markers-all-cities.json \\
        reports/data/2026-09-12-off-target-markers-all-cities-after.json
"""
import json, sys
before = json.load(open(sys.argv[1]))['cities']
after = json.load(open(sys.argv[2]))['cities']
rows = []
for city in sorted(set(before) | set(after), key=lambda c: -(before.get(c, {}).get('in_window', {}).get('class_counts', {}) and sum(v for k, v in before[c]['in_window']['class_counts'].items() if k != 'exact') or 0)):
    b = before.get(c := city, {}).get('in_window', {}); a = after.get(c, {}).get('in_window', {})
    def stale(w):
        cc = w.get('class_counts', {}); return sum(v for k, v in cc.items() if k != 'exact')
    if not b.get('n') and not a.get('n'):
        continue
    bv, av = b.get('visibility', {}), a.get('visibility', {})
    rows.append((c, b.get('n', '-'), stale(b) if b else '-', bv.get('pct_ge_4px', '-'), bv.get('pct_ge_30px', '-'), bv.get('max_px', '-'),
                 a.get('n', 'n/a'), stale(a) if a else 'n/a', av.get('pct_ge_4px', 'n/a'), av.get('pct_ge_30px', 'n/a'), av.get('max_px', 'n/a'),
                 a.get('last_miss', 'n/a')))
print('| city | in-window (before) | stale before | ≥4 px before | ≥30 px before | max px before | in-window (after) | stale after | ≥4 px after | ≥30 px after | max px after |')
print('|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
tb = ta = sb = sa = 0
for r in rows:
    print('| ' + ' | '.join(str(x) for x in r[:11]) + ' |')
    if isinstance(r[1], int): tb += r[1]; sb += r[2]
    if isinstance(r[6], int): ta += r[6]; sa += r[7]
print(f'\nTotals: before {tb:,} in-window / {sb:,} stale ({100*sb/max(tb,1):.2f}%) ; after {ta:,} in-window / {sa:,} stale ({100*sa/max(ta,1):.2f}%)')
print('\nAfter-only detail (class counts of any residual):')
for c in sorted(after):
    w = after[c].get('in_window', {}); cc = {k: v for k, v in w.get('class_counts', {}).items() if k != 'exact'}
    if cc: print(f'  {c}: {cc}  max_px={w["visibility"].get("max_px")}  p99={w["visibility"].get("p99_px")}')
