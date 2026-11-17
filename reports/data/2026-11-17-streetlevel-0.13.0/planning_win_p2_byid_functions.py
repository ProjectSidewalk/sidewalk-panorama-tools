"""#237 Part 2: are the functions this repo calls textually identical in two wheels? Reads only; run with -I.

    python -I p2_byid_functions.py <old.whl> <new.whl>
"""
import ast
import hashlib
import sys
import zipfile

WANTED = {'streetlevel/streetview/api.py': ['build_find_panorama_by_id_request_url', 'find_panorama_by_id'],
          'streetlevel/util.py': ['get_json'],
          'streetlevel/streetview/depth.py': ['parse', 'parse_header', 'parse_planes', 'compute_depth_map',
                                              'decode_b64', 'get_uint16', 'get_float32'],
          'streetlevel/streetview/parse.py': ['parse_panorama_message', 'parse_panorama_id_response']}


def segments(whl):
    out = {}
    with zipfile.ZipFile(whl) as z:
        for path, names in WANTED.items():
            src = z.read(path).decode()
            found = {n.name: ast.get_source_segment(src, n) for n in ast.parse(src).body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for name in names:
                out[(path, name)] = found.get(name)
    return out


old, new = segments(sys.argv[1]), segments(sys.argv[2])
for key in sorted(old):
    a, b = old[key], new[key]
    state = 'MISSING' if a is None or b is None else ('identical' if a == b else 'CHANGED')
    digest = hashlib.sha256((b or '').encode()).hexdigest()[:12]
    print('%-9s %s::%s  new sha256[:12]=%s' % (state, key[0], key[1], digest))
