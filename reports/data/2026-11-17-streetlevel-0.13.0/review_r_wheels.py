import ast, hashlib, sys, zipfile
a, b = sys.argv[1], sys.argv[2]
za, zb = zipfile.ZipFile(a), zipfile.ZipFile(b)
for z, p in ((za, a), (zb, b)):
    print(p.rsplit('/', 1)[-1], hashlib.sha256(open(p, 'rb').read()).hexdigest())
    meta = [n for n in z.namelist() if n.endswith('METADATA')][0]
    md = z.read(meta).decode()
    print('  ', [l for l in md.splitlines() if l.startswith(('Metadata-Version', 'Requires-Python'))],
          'Requires-Dist:', sum(l.startswith('Requires-Dist') for l in md.splitlines()))
def funcs(z, path):
    src = z.read(path).decode()
    tree = ast.parse(src)
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = ast.get_source_segment(src, node)
    return out, src
targets = {'streetlevel/streetview/api.py': ['find_panorama_by_id', 'build_find_panorama_by_id_request_url'],
           'streetlevel/util.py': ['get_json'],
           'streetlevel/streetview/depth.py': ['parse', 'parse_header', 'compute_depth_map', 'parse_planes', 'decode_b64'],
           'streetlevel/streetview/parse.py': ['parse_panorama_message', 'parse_panorama_id_response']}
for path, names in targets.items():
    fa, sa = funcs(za, path); fb, sb = funcs(zb, path)
    for n in names:
        print(path, n, 'IDENTICAL' if fa.get(n) == fb.get(n) else ('MISSING' if n not in fa or n not in fb else 'DIFFERS'))
    # module-level import lines
    ia = [l for l in sa.splitlines() if l.startswith(('import ', 'from '))]
    ib = [l for l in sb.splitlines() if l.startswith(('import ', 'from '))]
    if ia != ib: print('  imports changed:', sorted(set(ib) - set(ia)), 'removed', sorted(set(ia) - set(ib)))
fa, _ = funcs(za, 'streetlevel/util.py'); fb, _ = funcs(zb, 'streetlevel/util.py')
import difflib
print(''.join(difflib.unified_diff(fa['get_json'].splitlines(True), fb['get_json'].splitlines(True))))
fa, _ = funcs(za, 'streetlevel/streetview/depth.py'); fb, _ = funcs(zb, 'streetlevel/streetview/depth.py')
print(''.join(difflib.unified_diff(fa['parse_header'].splitlines(True), fb['parse_header'].splitlines(True))))
