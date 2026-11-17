"""#237 Part 2: compare two streetlevel wheels WITHOUT importing or installing either.

    python -I p2_wheel_diff.py <pypi-json> <old.whl> <new.whl> [--full-diff PATH ...]
"""
import difflib
import hashlib
import json
import sys
import zipfile


def main(argv):
    pypi, old, new = argv[:3]
    full = argv[4:] if len(argv) > 3 and argv[3] == '--full-diff' else []
    with open(pypi, encoding='utf-8') as f:
        data = json.load(f)
    print('== PyPI latest: %s' % data['info']['version'])
    for v in ('0.12.10', '0.12.11', '0.12.12', '0.12.13', '0.12.14', '0.13.0', '0.13.1'):
        files = data['releases'].get(v)
        if files is None:
            print('   %-8s (no such release)' % v)
            continue
        for fl in files:
            print('   %-8s %s  %s  sha256=%s yanked=%s requires_python=%s' % (
                v, fl['upload_time_iso_8601'], fl['filename'], fl['digests']['sha256'], fl['yanked'],
                fl['requires_python']))
    newer = [v for v in data['releases'] if v.startswith('0.13') or v.startswith('0.14') or v.startswith('1.')]
    print('   releases at or past 0.13: %s' % sorted(newer))
    zo, zn = zipfile.ZipFile(old), zipfile.ZipFile(new)
    for label, z, path in (('old', zo, old), ('new', zn, new)):
        with open(path, 'rb') as f:
            print('== %s wheel sha256 %s' % (label, hashlib.sha256(f.read()).hexdigest()))
        meta = next(n for n in z.namelist() if n.endswith('.dist-info/METADATA'))
        lines = z.read(meta).decode().splitlines()
        print('\n'.join('   ' + l for l in lines if l.startswith(('Version', 'Requires-Dist', 'Requires-Python',
                                                                  'Provides-Extra'))))
    so = {n for n in zo.namelist() if n.endswith('.py')}
    sn = {n for n in zn.namelist() if n.endswith('.py')}
    print('== .py files only in old: %s' % sorted(so - sn))
    print('== .py files only in new: %s' % sorted(sn - so))
    print('== per-file change (lines +/-), files present in both')
    for name in sorted(so & sn):
        a, b = zo.read(name).decode().splitlines(), zn.read(name).decode().splitlines()
        if a == b:
            print('   same      %s' % name)
            continue
        d = list(difflib.unified_diff(a, b, lineterm='', n=0))
        plus = sum(1 for l in d if l.startswith('+') and not l.startswith('+++'))
        minus = sum(1 for l in d if l.startswith('-') and not l.startswith('---'))
        print('   +%-4d -%-4d %s' % (plus, minus, name))
    for name in full:
        a = zo.read(name).decode().splitlines() if name in so else []
        b = zn.read(name).decode().splitlines() if name in sn else []
        print('== full diff: %s' % name)
        print('\n'.join(difflib.unified_diff(a, b, 'old/' + name, 'new/' + name, lineterm='', n=2)))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
