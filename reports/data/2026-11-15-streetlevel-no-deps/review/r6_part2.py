"""Review step 6: re-check the Part 2 finding from PyPI's JSON API and the 0.13.0 wheel the planner downloaded.

    python3 -I <this file> <pypi-json-file> <path to streetlevel-0.13.0 wheel> <path to 0.12.11 wheel>
"""
import ast
import json
import re
import sys
import zipfile


def module_imports(source):
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split('.')[0])
    return found


def main(pypi, new_whl, old_whl):
    with open(pypi, encoding='utf-8') as f:
        data = json.load(f)
    print('== PyPI: latest version %s' % data['info']['version'])
    for v in sorted(data['releases'], key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r'[.]', s)]):
        if v.startswith('0.12.1') or v.startswith('0.13'):
            files = data['releases'][v]
            print('   %-8s %s  %s' % (v, min((f['upload_time_iso_8601'] for f in files), default='-'),
                                     ', '.join(f['filename'] for f in files)))
    stdlib = set(sys.stdlib_module_names)
    for label, whl in (('0.12.11', old_whl), ('0.13.0', new_whl)):
        with zipfile.ZipFile(whl) as z:
            depth = z.read('streetlevel/streetview/depth.py').decode()
            api = z.read('streetlevel/streetview/api.py').decode()
            sv = [n for n in z.namelist() if n.startswith('streetlevel/streetview/') and n.endswith('.py')]
            third = set()
            for name in sv + ['streetlevel/util.py', 'streetlevel/geo.py', 'streetlevel/exif.py']:
                if name in z.namelist():
                    third |= module_imports(z.read(name).decode())
        print('== %s: the offset line of depth.parse_header' % label)
        print('   ' + next(l.strip() for l in depth.splitlines() if '"offset"' in l))
        body = api[api.index('def find_panorama_by_id('):]
        body = body[:body.index('\nasync def') if '\nasync def' in body else len(body)]
        print('== %s: find_panorama_by_id' % label)
        print('\n'.join('   ' + l for l in body.strip().splitlines()[:14]))
        print('== %s: third-party top-level imports in streetview/*, util, geo, exif: %s'
              % (label, sorted(third - stdlib - {'streetlevel'})))


if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:]))
