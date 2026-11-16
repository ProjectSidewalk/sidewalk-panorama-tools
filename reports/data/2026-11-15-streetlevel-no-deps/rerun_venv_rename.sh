#!/bin/bash
# #244 review round: why the box move swaps in a symlink rather than renaming the new venv to .venv. A venv with
# the box's pip (22.0.2) is built at one path, renamed, and asked for its prefix and its pip, both ways.
# Same seed and distutils shim as rerun_box_pip22.sh. Run as:  wsl -d Ubuntu -- bash <this file>
# Deletes nothing; refuses an existing work directory.
set -u
S="$HOME/claude-work/237/venv-rename-b"
if [ -e "$S" ]; then echo "$S already exists; this script deletes nothing"; exit 1; fi
mkdir -p "$S" && cd "$S" || exit 1
~/.local/bin/uv venv -q --seed --python /usr/bin/python3.10 built-as-237
built-as-237/bin/python -m pip uninstall -q -y wheel packaging
built-as-237/bin/python -m pip install -q 'pip==22.0.2' 'setuptools==59.6.0' 2>&1 | grep -v -i 'upgrad\|version 22'
cp -a built-as-237 shim-source   # the shim's target must survive the renames below
mkdir shim && ln -s "$S/shim-source/lib/python3.10/site-packages/setuptools/_distutils" shim/distutils
export PYTHONPATH="$S/shim" SETUPTOOLS_USE_DISTUTILS=stdlib
show() {
  echo "   sys.prefix: $($1/bin/python -c 'import sys; print(sys.prefix)' | sed "s#$S#<S>#")"
  echo "   first line of $1/bin/pip: $(head -1 $1/bin/pip | sed "s#$S#<S>#")"
  "$1/bin/pip" --version > /dev/null 2>"$S/err.txt"; echo "   $1/bin/pip --version exit: $? $(sed "s#$S#<S>#g" "$S/err.txt" | head -1)"
  "$1/bin/python" -m pip --version > /dev/null 2>&1; echo "   $1/bin/python -m pip --version exit: $?"
}
echo "== as built"; show built-as-237
echo "== renamed: mv built-as-237 .venv"; mv built-as-237 .venv; show .venv
mv .venv built-as-237
echo "== symlinked: ln -s built-as-237 .venv"; ln -s built-as-237 .venv; show .venv
