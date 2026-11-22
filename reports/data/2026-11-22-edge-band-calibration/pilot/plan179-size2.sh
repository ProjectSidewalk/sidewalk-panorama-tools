#!/bin/bash
# REDACTED copy for the repo (2026-11-22): the store mount path is <store-root>, and the line that listed
# the account's groups is removed. Otherwise as run. Superseded pilot.
# Read-only follow-up probe for the #179 calibration plan. Writes nothing anywhere.
ROOT=<store-root>
echo "== groups"; # (the group-listing command is removed from this copy)
echo "== imports"; for m in aiohttp backoff scipy pyproj pytest; do python3 -c "import $m; print('$m', getattr($m,'__version__','?'))" 2>&1 | tail -1; done
echo "== tools"; which git curl pip3 2>&1; git --version
echo "== pypi"; curl -sS -m 15 -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/pillow/ 2>&1
echo "== github"; curl -sS -m 15 -o /dev/null -w '%{http_code}\n' https://github.com/ProjectSidewalk/sidewalk-panorama-tools 2>&1
echo "== non-dir entries at the store root"; find $ROOT -maxdepth 1 ! -type d | head
echo "== old scrapes"; ls $ROOT/old_scrapes | head -5; ls $ROOT/oldest_scrapes | head -5
echo "== city-root files (seattle-wa, juneau-ak)"; ls -la $ROOT/seattle-wa | awk 'length($NF)!=2' | head -20; ls -la $ROOT/juneau-ak | awk 'length($NF)!=2' | head -20
echo "== id forms"
nice python3 - <<'EOF'
import os, re
ROOT = '<store-root>'
side = re.compile(r'\.w(\d+)\.jpg$')
def form(i):
    if i.startswith('CAoS'): return 'photosphere'
    if i.isascii() and i.isdigit(): return 'numeric'
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', i): return 'uuid'
    if len(i) == 22: return 'gsv22'
    return 'other'
for city in ('beringen-be', 'goiania-br', 'brussels-be', 'kaysville-ut', 'laurens-ia', 'sacramento-ca',
             'richmond-va', 'juneau-ak', 'bayonne-fr', 'la-piedad', 'validation-study', 'newport-ky'):
    croot = os.path.join(ROOT, city)
    forms = {}; n = 0; ex = {}
    for s in sorted(os.listdir(croot)):
        p = os.path.join(croot, s)
        if len(s) != 2 or not os.path.isdir(p): continue
        for name in os.listdir(p):
            if name.endswith('.jpg') and name[:2] == s and not side.search(name):
                f = form(name[:-4]); forms[f] = forms.get(f, 0) + 1; n += 1; ex.setdefault(f, name[:-4])
    print(city, n, forms, {k: v[:12] + '...' for k, v in ex.items()})
EOF
echo "== full-detector look at 4 non-GSV panos (bottom, right, top; exact and near)"
nice python3 - <<'EOF'
import os
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
ROOT = '<store-root>'
def run(luma, lines, box, maxl):
    n = 0
    for v in lines:
        strip = luma.crop(box(v)); hist = strip.histogram()
        if sum(hist[:maxl + 1]) / float(strip.width * strip.height) < 0.98: break
        n += 1
    return n
for city, pid in (('juneau-ak', '947478730505514'), ('juneau-ak', '315808614957628'),
                  ('richmond-va', '915292510049907'), ('richmond-va', '2576203436081781')):
    p = os.path.join(ROOT, city, pid[:2], pid + '.jpg')
    with Image.open(p) as im:
        w, h = im.size
        im.draft('L', (w // 8, h // 8)); luma = im.convert('L')
    lw, lh = luma.size
    out = {}
    for maxl in (0, 2):
        out[maxl] = (run(luma, range(lh - 1, -1, -1), lambda y: (0, y, lw, y + 1), maxl),
                     run(luma, range(lw - 1, -1, -1), lambda x: (x, 0, x + 1, lh), maxl),
                     run(luma, range(lh), lambda y: (0, y, lw, y + 1), maxl))
    print(city, pid, (w, h), 'reduced', (lw, lh), 'lines bottom/right/top exact', out[0], 'near', out[2],
          'mtime', int(os.stat(p).st_mtime))
EOF
echo "== done"
