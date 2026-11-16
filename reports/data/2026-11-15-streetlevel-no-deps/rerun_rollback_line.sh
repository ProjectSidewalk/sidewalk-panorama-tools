#!/bin/bash
# #244 final review: the swap, rollback and removal lines of docs/ops.md ("Moving the box to the --no-deps install"),
# taken from that page verbatim, run in every state of its table plus the steady state after the old venv is gone,
# twice each. The venvs are stand-ins (directories with a marker file and an executable bin/python), since these
# lines only rename, link and remove. Also: the line this replaces, in the state that broke it; a later rebuild's
# atomic repoint; and `python3 -m venv` through the link.
# Run as:  wsl -d Ubuntu -- bash <this file>       Works in a new directory it creates; the only rm -rf it runs is
# the documented removal line, on the stand-in .venv-before-237 it made itself.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
OPS="$HERE/../../../docs/ops.md"
S="$HOME/claude-work/237/rollback-line"
if [ -e "$S" ]; then echo "$S already exists"; exit 1; fi
mkdir -p "$S" || exit 1
SWAP=$(grep -m1 -E '^\[ -x \.venv-237/bin/python \] && mv \.venv \.venv-before-237' "$OPS" | sed 's/ *#.*$//')
ROLLBACK=$(grep -m1 -E '^if \[ -d \.venv-before-237 \]' "$OPS")
REMOVE=$(grep -m1 -E '^ *rm -rf -- \.venv-before-237' "$OPS" | sed 's/^ *//; s/ *#.*$//')
OLD='[ -L .venv ] && unlink .venv; [ -e .venv ] || mv .venv-before-237 .venv'
echo "swap:     $SWAP"; echo "rollback: $ROLLBACK"; echo "remove:   $REMOVE"
fake() { mkdir -p "$1/bin" && echo "$2" > "$1/WHICH" && printf '#!/bin/sh\necho %s\n' "$2" > "$1/bin/python" && chmod +x "$1/bin/python"; }
state() { printf '   state: .venv=%s | .venv-237=%s | .venv-before-237=%s\n' \
  "$( [ -L .venv ] && echo "link->$(readlink .venv)" || ([ -d .venv ] && echo "dir($(cat .venv/WHICH))" || echo NONE) )" \
  "$([ -d .venv-237 ] && echo yes || echo no)" "$([ -d .venv-before-237 ] && echo yes || echo no)"; }
run() { echo "+ $1"; bash -c "$1" 2>&1 | sed 's/^/     /'; echo "     [exit ${PIPESTATUS[0]}]"; state; }
case_dir() { cd "$S" && mkdir "$1" && cd "$1" && echo "======== $1"; }

case_dir 01-the-old-line-after-removal
fake .venv-237 new; ln -s .venv-237 .venv; state
run "$OLD"
echo "   ^ the line this replaces: the link is gone and nothing replaced it"

case_dir 02-before-the-swap
fake .venv old; fake .venv-237 new; state
run "$ROLLBACK"; run "$ROLLBACK"

case_dir 03-killed-inside-the-swap
fake .venv old; fake .venv-237 new; mv .venv .venv-before-237; state
run "$ROLLBACK"; run "$ROLLBACK"

case_dir 04-after-the-swap
fake .venv old; fake .venv-237 new; run "$SWAP"
run "$ROLLBACK"; run "$ROLLBACK"

case_dir 05-killed-inside-the-rollback
fake .venv old; fake .venv-237 new; bash -c "$SWAP"; unlink .venv; state
run "$ROLLBACK"; run "$ROLLBACK"

case_dir 06-swap-typed-twice
fake .venv old; fake .venv-237 new; run "$SWAP"; run "$SWAP"
echo "   stray link inside the old venv: $(ls -l .venv-before-237/.venv 2>/dev/null | sed 's/.* -> /-> /')"
run "$ROLLBACK"; run "$ROLLBACK"

case_dir 07-swap-without-a-built-venv-237
fake .venv old; state
run "$SWAP"
echo "   ^ refused: .venv untouched"

case_dir 08-steady-state-after-removal
fake .venv old; fake .venv-237 new; bash -c "$SWAP"; state
echo "+ readlink .venv: $(readlink .venv)"; echo "+ ls -d .venv*: $(ls -d .venv* | tr '\n' ' ')"
run "$REMOVE"
run "$ROLLBACK"; run "$ROLLBACK"
echo "   .venv/bin/python still answers: $(.venv/bin/python)"

case_dir 09-a-later-rebuild-repoints-the-link-atomically
fake .venv-237 new; ln -s .venv-237 .venv; fake .venv-300 newer; state
run "ln -s .venv-300 .venv.new && mv -T .venv.new .venv"
echo "   .venv/bin/python answers: $(.venv/bin/python)"

case_dir 10-python3-m-venv-through-the-link
mkdir .venv-237; ln -s .venv-237 .venv; state
run "/usr/bin/python3.10 -m venv --without-pip .venv"
echo "   .venv-237 contents: $(ls -A .venv-237 | tr '\n' ' ')"
