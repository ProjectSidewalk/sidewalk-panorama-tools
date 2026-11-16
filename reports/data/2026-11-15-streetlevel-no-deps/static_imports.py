"""Static cross-check for #237: every import statement in the streetlevel modules this repo loads.

probe_imports.py proves what an import needs at IMPORT time. It cannot see an import written inside a
function, which only runs when that function is called. This script reads the source of every `streetlevel.*`
module that is in sys.modules after the given imports and lists every import statement in it, at any depth
(module level or inside a function/class), with whether its top-level package resolves in this venv.

An unresolved name at module level cannot happen (the import would have failed). An unresolved name inside a
function is a dependency this venv does not have that would only be missed on a call of that function, so
each one is printed with its file, line and enclosing function.

Usage:
    <venv>/bin/python static_imports.py <module> [<module> ...]
"""

import ast
import importlib
import importlib.util
import os
import sys


def enclosing(stack):
    names = [n.name for n in stack if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return '.'.join(names) or '<module>'


def walk(node, stack, found):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                found.append((alias.name.split('.')[0], child.lineno, enclosing(stack)))
        elif isinstance(child, ast.ImportFrom):
            if child.level == 0 and child.module:
                found.append((child.module.split('.')[0], child.lineno, enclosing(stack)))
        walk(child, stack + [child], found)


def resolves(top):
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError):
        return False


def main(argv):
    for module in argv[1:]:
        importlib.import_module(module)
    loaded = sorted(name for name in sys.modules if name == 'streetlevel' or name.startswith('streetlevel.'))
    stdlib = set(sys.stdlib_module_names)
    unresolved = []
    for name in loaded:
        path = getattr(sys.modules[name], '__file__', None)
        if not path or not path.endswith('.py'):
            continue
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        found = []
        walk(tree, [], found)
        third = sorted({(top, where != '<module>') for top, _, where in found
                        if top not in stdlib and top != 'streetlevel'})
        short = path[path.index('streetlevel'):].replace(os.sep, '/')
        print('%s  (%s)' % (name, short))
        for top, lazy in third:
            ok = resolves(top)
            print('    %-22s %-12s %s' % (top, 'in-function' if lazy else 'module-level', 'ok' if ok else 'UNRESOLVED'))
        for top, lineno, where in found:
            if top not in stdlib and top != 'streetlevel' and not resolves(top):
                unresolved.append('%s:%d in %s imports %s' % (short, lineno, where, top))
    print('--- streetlevel modules loaded: %d' % len(loaded))
    print('--- unresolved imports: %d' % len(unresolved))
    for line in unresolved:
        print('    ' + line)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
