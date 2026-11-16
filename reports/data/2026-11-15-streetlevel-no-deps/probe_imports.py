"""Measure which distributions an import of given modules actually needs (#237).

Run with the python of a venv that holds ONLY `streetlevel` installed with --no-deps. For each target
module, in order, it imports the module in a child interpreter; on `ModuleNotFoundError: No module named X`
it installs the distribution that provides X (with --no-deps, at the constraints pin, so that distribution's
own requirements are discovered the same way rather than taken on pip's word) and tries again. The result is
the closure of distributions the import really reaches, in the order they were first missed.

Usage:
    <venv>/bin/python probe_imports.py <constraints.txt> <out.json> <module> [<module> ...]

Writes a JSON record to <out.json> and prints a transcript to stdout. Exits 1 if an import fails for any
reason other than a missing module it can install (the transcript carries the traceback).
"""

import json
import re
import subprocess
import sys

# import name -> distribution name, where they differ. Anything absent maps to itself.
MODULE_TO_DIST = {
    'google': 'protobuf',
    'PIL': 'pillow',
    'Crypto': 'pycryptodome',
    'equilib': 'pyequilib',
    'attr': 'attrs',
    'yaml': 'pyyaml',
    'coordinates_converter': 'CoordinatesConverter',
    'coordinatesconverter': 'CoordinatesConverter',
    'CoordinatesConverter': 'CoordinatesConverter',
    'typing_extensions': 'typing_extensions',
    'exceptiongroup': 'exceptiongroup',
    'async_timeout': 'async-timeout',
}

# Reaching any of these means the --no-deps plan does not work as intended; stop and say so.
HEAVY = {'torch', 'pyequilib', 'triton'}

_MISSING = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")


def try_import(module):
    """Import `module` in a child of this interpreter. Returns (ok, missing_top_level_or_None, output)."""
    proc = subprocess.run([sys.executable, '-c', 'import importlib; importlib.import_module(%r)' % module],
                          capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return True, None, out
    found = _MISSING.findall(out)
    return False, (found[-1].split('.')[0] if found else None), out


def install(dist, constraints):
    cmd = [sys.executable, '-m', 'pip', 'install', '--no-deps', '-c', constraints, dist]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    lines = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
    return proc.returncode, ' '.join(cmd[1:]), lines[-1] if lines else ''


def main(argv):
    constraints, out_path, targets = argv[1], argv[2], argv[3:]
    record = {'python': sys.version, 'targets': [], 'installed_in_order': []}
    failed = False
    for module in targets:
        entry = {'module': module, 'missing_then_installed': []}
        record['targets'].append(entry)
        for _ in range(60):
            ok, missing, output = try_import(module)
            if ok:
                print('OK      import %s' % module)
                entry['ok'] = True
                break
            if missing is None:
                print('FAILED  import %s (not a missing module):\n%s' % (module, output))
                entry['ok'] = False
                entry['error'] = output
                failed = True
                break
            dist = MODULE_TO_DIST.get(missing, missing)
            print('MISSING import %s -> No module named %r -> distribution %s' % (module, missing, dist))
            if dist.lower() in HEAVY:
                print('STOP    %s is one of the packages this change exists to drop' % dist)
                entry['ok'] = False
                entry['error'] = 'reached %s' % dist
                failed = True
                break
            code, cmd, last = install(dist, constraints)
            print('        pip %s -> exit %d: %s' % (cmd, code, last))
            if code != 0:
                entry['ok'] = False
                entry['error'] = 'pip could not install %s: %s' % (dist, last)
                failed = True
                break
            entry['missing_then_installed'].append({'module': missing, 'distribution': dist})
            record['installed_in_order'].append(dist)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(record, f, indent=2)
        f.write('\n')
    print('closure, in the order first missed: %s' % record['installed_in_order'])
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
