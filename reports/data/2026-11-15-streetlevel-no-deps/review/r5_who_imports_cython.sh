#!/bin/bash
# Review step 5: r2_callpaths.py saw an attempted `import cython` (caught by its importer, never loaded). Who?
set -u
V="$HOME/claude-work/237/venv-review-pip22d"
SP="$V/lib/python3.10/site-packages"
echo "== source lines importing cython (any case), outside the cython-generated .so files"
grep -rnE "^\s*(import cython|from cython|import Cython|from Cython)" "$SP" --include=*.py | grep -v "/pip/" | head -20
echo "== the same attempt, with the caller's stack"
cd "$HOME/claude-work/237/repo" && "$V/bin/python" -I -c "
import builtins, sys, traceback
sys.path.insert(0, '.')
real = builtins.__import__
def imp(name, g=None, l=None, f=(), lvl=0):
    if lvl == 0 and name.split('.')[0].lower() == 'cython':
        print('import %r attempted from:' % name)
        traceback.print_stack(limit=6)
    return real(name, g, l, f, lvl)
builtins.__import__ = imp
from downloaders import gsv
from streetlevel.streetview import api
"
