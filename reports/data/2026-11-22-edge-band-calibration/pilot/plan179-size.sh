#!/bin/bash
# REDACTED copy for the repo (2026-11-22): the store mount path is <store-root>, the scratch file system
# and the account's scratch directory are <scratch> and <scratch-dir>. Otherwise as run. Superseded pilot.
# Read-only sizing probe for the #179 calibration plan. Writes nothing anywhere.
ROOT=<store-root>
echo "== host"; hostname; uptime; nproc
echo "== python"; python3 -V; python3 -c 'import PIL, numpy; print("PIL", PIL.__version__, "numpy", numpy.__version__)'
python3 -c 'import requests, urllib3; print("requests", requests.__version__, "urllib3", urllib3.__version__)' 2>&1 | tail -1
python3 -c 'import venv, ensurepip; print("venv+ensurepip ok")' 2>&1 | tail -1
ls /usr/bin/python3* 2>/dev/null | tr '\n' ' '; echo
echo "== scratch"; df -h <scratch> | tail -1; ls -ld <scratch-dir> 2>&1
echo "== store root"; ls -la $ROOT | head -8; ls $ROOT | wc -l
echo "== per city: ledger lines, shard dirs, band files"
for d in $ROOT/*/; do
  c=$(basename $d)
  n=$(wc -l < $d/pano_id_log.csv 2>/dev/null || echo NA)
  s=$(ls $d | awk 'length($0)==2' | wc -l)
  b=$(ls $d | grep -c -i 'black_band' )
  echo "$c,$n,$s,$b"
done
echo "== timing"
nice python3 - <<'EOF'
import os, re, time, random
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
ROOT = '<store-root>'
side = re.compile(r'\.w(\d+)\.jpg$')
random.seed(179)
for city in ('seattle-wa', 'chicago-il', 'richmond-va', 'bayonne-fr', 'juneau-ak'):
    croot = os.path.join(ROOT, city)
    try:
        shards = sorted(s for s in os.listdir(croot) if len(s) == 2 and os.path.isdir(os.path.join(croot, s)))
    except OSError as e:
        print(city, 'ERR', e); continue
    picks = []
    t0 = time.time()
    for s in random.sample(shards, min(6, len(shards))):
        names = sorted(n for n in os.listdir(os.path.join(croot, s))
                       if n.endswith('.jpg') and n[:2] == s and not side.search(n))
        picks += [os.path.join(croot, s, n) for n in random.sample(names, min(3, len(names)))]
    tlist = time.time() - t0
    tread = tdec = 0.0; nbytes = 0; dims = {}; bands = []
    for p in picks:
        t = time.time()
        with open(p, 'rb') as f:
            data = f.read()
        tread += time.time() - t; nbytes += len(data)
        t = time.time()
        with Image.open(p) as im:
            w, h = im.size
            im.draft('L', (max(1, w // 8), max(1, h // 8)))
            luma = im.convert('L')
        lw, lh = luma.size
        # bottom band, exact and near, the detector's shape
        def depth(maxl):
            n = 0
            for y in range(lh - 1, -1, -1):
                hist = luma.crop((0, y, lw, y + 1)).histogram()
                if sum(hist[:maxl + 1]) / float(lw) < 0.98:
                    break
                n += 1
            return n / float(lh)
        b0, b2 = depth(0), depth(2)
        tdec += time.time() - t
        dims[(w, h)] = dims.get((w, h), 0) + 1
        if b2 > 0: bands.append((os.path.basename(p), round(b0, 4), round(b2, 4)))
    n = len(picks) or 1
    print('%s shards=%d picked=%d list=%.2fs read=%.3fs/pano decode=%.3fs/pano mean=%.1fMB dims=%s bands=%s'
          % (city, len(shards), len(picks), tlist, tread / n, tdec / n, nbytes / n / 1e6, dims, bands))
EOF
echo "== done"
